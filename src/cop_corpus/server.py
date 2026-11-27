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

from . import auth, db, usage, web, web_live
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


def _seen(user: dict) -> None:
    """Date du dernier appel MCP (au plus une écriture par heure) : coche « LLM connecté » dans la prise en main."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if (user.get("mcp_seen_at") or "")[:13] != now[:13]:
        try:
            conn = db.get()
            conn.execute("UPDATE users SET mcp_seen_at=? WHERE id=?", (now, user["id"]))
            conn.commit()
        except Exception:
            pass


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
                    await JSONResponse({"error": "invalid or revoked token"}, status_code=401)(scope, receive, send)
                    return
                if rate_limited(user):
                    await JSONResponse({"error": "too many requests: try again in a minute"}, status_code=429,
                                       headers={"Retry-After": "60"})(scope, receive, send)
                    return
                if scope.get("method") == "POST":  # vrai appel JSON-RPC, pas une adresse ouverte dans un navigateur
                    _seen(user)
                scope = dict(scope)
                scope["path"] = m.group(2).rstrip("/") or "/mcp"
                scope["raw_path"] = scope["path"].encode()
                scope["cop_user"] = user
                await usage.track_mcp(self.app, scope, receive, send)
                return
            elif path.rstrip("/") == "/mcp":
                await JSONResponse(
                    {"error": "use your personal address /u/<token>/mcp (Connect an LLM page of the website)"},
                    status_code=401,
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def server_error(request, exc):
    """Erreur 500 : alerte aux administrateurs (chemin masqué : il peut porter un jeton), page sobre."""
    from starlette.responses import PlainTextResponse

    from . import monitor
    from .logs import redact

    monitor.report(f"page {request.method} {redact(request.url.path)}", exc)
    return PlainTextResponse("Erreur interne / Internal error", status_code=500)


def load_vectors() -> None:
    from . import embed, monitor

    if not embed.enabled():
        return
    conn = db.connect()
    try:
        embed._refresh(conn)
    except Exception as exc:  # noqa: BLE001  (base verrouillée…) : chargés à la première recherche, comme avant
        monitor.report("site : chargement des vecteurs", exc, conn=conn)
    finally:
        conn.close()


THREADS = 12   # fils du pool d'anyio (40 par défaut) ; cf. docs/notes/2026-11-25-lenteur-du-site.md


def build_app() -> Starlette:
    mcp_app = mcp.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        import asyncio

        import anyio.to_thread

        from . import watchdog

        # Pages `def`, `deposer_url` et le journal MCP passent par ce pool : borné (une connexion SQLite par fil).
        anyio.to_thread.current_default_thread_limiter().total_tokens = THREADS
        # Vecteurs chargés avant la première requête (≈ 4 s ; ship.sh attend jusqu'à 60 s) : plus de première
        # recherche à froid (2 à 8,6 s mesurés). Dans un fil : la boucle reste libre ; un échec n'empêche pas de servir.
        await anyio.to_thread.run_sync(load_vectors)
        beat = asyncio.create_task(watchdog.loop_heartbeat(watchdog.ACTIVE)) if watchdog.ACTIVE else None
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            if beat:
                beat.cancel()

    app = Starlette(routes=[*web.routes, *web_live.routes, Mount("/", app=mcp_app)], lifespan=lifespan,
                    exception_handlers={Exception: server_error})
    app.add_middleware(TokenPathMiddleware)
    return app


def run(host: str = "127.0.0.1", port: int = 8000, with_worker: bool = False) -> None:
    import uvicorn

    if with_worker:
        from . import worker

        threading.Thread(target=worker.run_forever, kwargs={"bot": True, "watch": True}, daemon=True).start()
    from . import extract, ingest, watchdog
    from .logs import uvicorn_log_config

    if not with_worker:
        extract.DEFER_PDF = True   # ~250 Mo par lecture de PDF : jamais dans le conteneur du site, le worker s'en charge
        ingest.DEFER = True        # aucun format indexé par le site : un seul processus indexe (note phase 1, critère 9)
    watchdog.start_thread("app")   # battement du site, et surveillance du worker (qui surveille le site)
    uvicorn.run(build_app(), host=host, port=port, proxy_headers=True, forwarded_allow_ips="*",
                log_config=uvicorn_log_config(), timeout_graceful_shutdown=5)   # flux MCP ouverts : pas d'attente sans fin
    watchdog.process_stopping("app")   # uvicorn rend la main après un arrêt demandé (SIGTERM) : arrêt propre
