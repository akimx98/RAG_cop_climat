"""Application HTTP unique : tableau de bord + serveur MCP.

- `/u/<jeton>/mcp` : connecteur MCP personnel (Claude, ChatGPT). Le jeton
  identifie l'utilisateur ; la requête est réécrite vers `/mcp`.
- `/mcp` nu : refusé (sauf OAuth configuré, non activé dans cette version).
- tout le reste : tableau de bord.
"""

from __future__ import annotations

import contextlib
import re
import threading
import time
from collections import defaultdict, deque

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount

from . import auth, db, web, web_live
from .mcp_server import mcp

USER_PATH = re.compile(r"^/u/([A-Za-z0-9_\-]{16,})(/mcp/?.*)$")

# Garde-fou pour les comptes invités : un assistant qui boucle ne doit pas saturer le petit serveur partagé.
GUEST_RATE, GUEST_WINDOW = 90, 60.0
_hits: dict[int, deque] = defaultdict(deque)


def rate_limited(user: dict) -> bool:
    if auth.can_contribute(user):
        return False
    now, q = time.monotonic(), _hits[user["id"]]
    while q and now - q[0] > GUEST_WINDOW:
        q.popleft()
    if len(q) >= GUEST_RATE:
        return True
    q.append(now)
    return False


class TokenPathMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            m = USER_PATH.match(path)
            if m:
                user = auth.by_token(db.get(), m.group(1))
                if not user:
                    await JSONResponse({"error": "jeton invalide ou révoqué"}, status_code=401)(scope, receive, send)
                    return
                if rate_limited(user):
                    await JSONResponse({"error": "trop de requêtes : réessayez dans une minute"}, status_code=429,
                                       headers={"Retry-After": "60"})(scope, receive, send)
                    return
                scope = dict(scope)
                scope["path"] = m.group(2).rstrip("/") or "/mcp"
                scope["raw_path"] = scope["path"].encode()
                scope["cop_user"] = user
            elif path.rstrip("/") == "/mcp":
                await JSONResponse(
                    {"error": "utiliser votre adresse personnelle /u/<jeton>/mcp (page « Claude / ChatGPT » du tableau de bord)"},
                    status_code=401,
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app() -> Starlette:
    mcp_app = mcp.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    app = Starlette(routes=[*web.routes, *web_live.routes, Mount("/", app=mcp_app)], lifespan=lifespan)
    app.add_middleware(TokenPathMiddleware)
    return app


def run(host: str = "127.0.0.1", port: int = 8000, with_worker: bool = False) -> None:
    import uvicorn

    if with_worker:
        from . import worker

        threading.Thread(target=worker.run_forever, kwargs={"bot": True, "watch": True}, daemon=True).start()
    uvicorn.run(build_app(), host=host, port=port, proxy_headers=True, forwarded_allow_ips="*")
