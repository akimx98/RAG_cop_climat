"""Appels au modèle de langage (OpenAI), avec suivi de la dépense et plafond.

Règle de confidentialité : seules des sources PUBLIQUES sont envoyées. Les appelants le garantissent
(positions.py ne traite que des documents `access='public'` ; briefs.py n'envoie jamais les notes internes).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from . import live

EXTRACT_MODEL = os.environ.get("COP_LLM_EXTRACT_MODEL", "gpt-5.4-mini")
WRITE_MODEL = os.environ.get("COP_LLM_WRITE_MODEL", "gpt-5.6-luna")
BUDGET_USD = float(os.environ.get("COP_LLM_BUDGET_USD", "30"))
# Prix publics (USD par million de tokens, entrée / sortie), pour le suivi du plafond.
PRICES = {"gpt-5.4-mini": (0.75, 4.50), "gpt-5.6-luna": (1.00, 6.00), "gpt-5.4-nano": (0.20, 1.25),
          "gpt-5.5": (5.00, 30.00), "gpt-4.1-mini": (0.40, 1.60)}


class BudgetExceeded(RuntimeError):
    pass


def enabled() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY")) and os.environ.get("COP_LLM", "1") != "0"


def spent(conn) -> float:
    live.ensure_schema(conn)
    return conn.execute("SELECT COALESCE(SUM(cost),0) FROM llm_usage").fetchone()[0]


def _record(conn, model: str, purpose: str, usage) -> float:
    pin, pout = PRICES.get(model, (5.0, 30.0))
    tin = getattr(usage, "prompt_tokens", 0) or 0
    tout = getattr(usage, "completion_tokens", 0) or 0
    cost = (tin * pin + tout * pout) / 1e6
    conn.execute("INSERT INTO llm_usage(at,model,purpose,input_tokens,output_tokens,cost) VALUES(?,?,?,?,?,?)",
                 (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), model, purpose, tin, tout, cost))
    conn.commit()
    return cost


def json_call(conn, *, model: str, purpose: str, system: str, user: str, schema: dict, name: str = "reponse",
              effort: str = "low", max_tokens: int = 6000) -> dict[str, Any]:
    """Appel à sortie JSON contrainte par un schéma. Lève BudgetExceeded au-delà du plafond."""
    if spent(conn) >= BUDGET_USD:
        raise BudgetExceeded(f"plafond de {BUDGET_USD} $ atteint")
    from openai import OpenAI

    client = OpenAI()
    kwargs: dict[str, Any] = dict(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}},
        max_completion_tokens=max_tokens,
    )
    if effort:
        kwargs["reasoning_effort"] = effort
    last: Exception | None = None
    for attempt in range(4):
        try:
            resp = client.chat.completions.create(**kwargs)
            _record(conn, model, purpose, resp.usage)
            return json.loads(resp.choices[0].message.content or "{}")
        except Exception as exc:  # paramètre non accepté, quota, réseau
            last = exc
            msg = str(exc)
            if "reasoning_effort" in msg and "reasoning_effort" in kwargs:
                kwargs.pop("reasoning_effort")
                continue
            time.sleep(2 ** attempt)
    raise RuntimeError(f"appel LLM impossible : {last}")


def usage_summary(conn) -> dict[str, Any]:
    live.ensure_schema(conn)
    rows = conn.execute(
        """SELECT model, purpose, COUNT(*) n, SUM(input_tokens) tin, SUM(output_tokens) tout, SUM(cost) cost
           FROM llm_usage GROUP BY model, purpose ORDER BY cost DESC""").fetchall()
    return {"depense_usd": round(spent(conn), 3), "plafond_usd": BUDGET_USD, "detail": [dict(r) for r in rows]}
