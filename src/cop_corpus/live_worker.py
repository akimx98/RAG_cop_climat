"""Boucle de la veille en direct (fil séparé du veilleur documentaire, pour ne jamais l'attendre).

Toutes les 5 min : index Carbon Brief des textes, rattachement, récupération polie, alertes Telegram.
Toutes les 10 min : comptes Bluesky suivis, extraction des positions (sources publiques).
Toutes les 6 h : programme (planner des side events et réunions officielles).
Toutes les heures : état des dossiers et positions « Who wants what » de Carbon Brief.
7 h et 19 h (heure d'Antalya) : briefs des dossiers où il s'est passé quelque chose.
"""

from __future__ import annotations

import os
import time
import traceback

from . import agenda, briefs, db, fetch, live, llm, positions, telegram

EVERY = {"texts": 300, "social": 600, "positions": 600, "reference": 3600, "agenda": 21600, "pavillons": 21600, "soumissions": 600}


def tick(conn, client: fetch.Client, last: dict[str, float]) -> None:
    now = time.time()

    def due(name: str) -> bool:
        if now - last.get(name, 0) >= EVERY[name]:
            last[name] = now
            return True
        return False

    if due("reference"):
        live.sync_status(conn, client)
        n = live.sync_positions_cb(conn, client)
        live.log(f"référence Carbon Brief : {n} positions")
    if due("agenda"):
        agenda.log(str(agenda.sync_planner(conn, client)))
    if due("pavillons"):
        from . import pavillons

        pavillons.log(str(pavillons.sync(conn, client)))
    if due("texts"):
        new = live.sync_texts(conn, client)
        linked = live.link_existing(conn)
        got = live.retrieve_pending(conn, client)
        live.link_existing(conn)
        sent = live.notify_new_texts(conn, telegram.send)
        sent += live.notify_retrieved(conn, telegram.send)
        if new or linked or got.get("recupere") or sent:
            live.log(f"textes : {len(new)} annoncés, {linked} rattachés, {got}, {sent} alertes")
    if due("social"):
        n = live.sync_bluesky(conn, client)
        if n:
            live.log(f"Bluesky : {n} messages")
    if due("positions") and llm.enabled():
        try:
            out = positions.run(conn)
            if out.get("documents") or out.get("positions"):
                live.log(f"positions : {out}")
        except llm.BudgetExceeded as exc:
            live.log(str(exc))
    if due("soumissions") and llm.enabled() and os.environ.get("COP_SUB", "1") != "0":
        from . import soumissions

        try:
            n = soumissions.analyse_tick(conn)
            if n:
                live.log(f"soumissions : {n} analysées")
        except llm.BudgetExceeded as exc:
            live.log(str(exc))
    if llm.enabled():
        n = briefs.run_slot(conn, telegram.send)
        if n:
            live.log(f"briefs : {n} dossiers")


def run_forever(stop=None, interval: int = 60) -> None:
    if os.environ.get("COP_LIVE", "1") == "0":
        return
    conn = db.connect()
    live.ensure_schema(conn)
    client = fetch.Client()
    first = not conn.execute("SELECT 1 FROM live_texts LIMIT 1").fetchone()
    last: dict[str, float] = {}
    if first:
        # Premier démarrage : l'index existant ne déclenche pas d'avalanche d'alertes.
        try:
            live.sync_texts(conn, client)
            conn.execute("UPDATE live_texts SET notified=1")
            conn.commit()
        except Exception:
            traceback.print_exc()
    while not (stop and stop.is_set()):
        try:
            tick(conn, client, last)
        except Exception:
            traceback.print_exc()
        time.sleep(interval)
