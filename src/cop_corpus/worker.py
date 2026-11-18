"""Veilleur : relève les sources surveillées à leur fréquence, indexe, calcule
les embeddings, déclenche les alertes ; bot Telegram ; récapitulatif du matin."""

from __future__ import annotations

import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from . import alerts, catalog, config, db, embed, fetch, ingest, telegram


def _due(conn, source: catalog.Source) -> bool:
    row = conn.execute("SELECT last_run FROM source_state WHERE source_id=?", (source.id,)).fetchone()
    if not row or not row["last_run"]:
        return True
    last = datetime.strptime(row["last_run"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    minutes = source.watch_minutes
    result = conn.execute("SELECT last_result FROM source_state WHERE source_id=?", (source.id,)).fetchone()
    if result and "'new'" not in (result[0] or "") and ("antibot" in (result[0] or "") or "'error'" in (result[0] or "")):
        minutes = max(minutes * 8, 240)  # source bloquée ou en erreur : on espace (pas de martèlement)
    return datetime.now(timezone.utc) - last >= timedelta(minutes=minutes)


def watch_once(conn, client: fetch.Client) -> int:
    sources = [s for s in catalog.load() if s.watch_minutes > 0 and _due(conn, s)]
    new = 0
    if sources:
        results = ingest.run_many(sources, workers=4)
        new = sum(r.get("new", 0) for r in results.values() if isinstance(r.get("new", 0), int))
        print(f"[veille] {len(sources)} sources, {new} nouveaux", file=sys.stderr, flush=True)
    ingest.reindex_pending(conn)
    if embed.enabled():
        try:
            embed.embed_pending(conn, limit=5000)
        except Exception as exc:
            print(f"[embeddings] {exc}", file=sys.stderr)
    created = alerts.run(conn)
    if created:
        print(f"[alertes] {created} notifications", file=sys.stderr, flush=True)
    return new


def digest(conn) -> None:
    """Récapitulatif Telegram de 7 h et 19 h (heure de la COP), centré sur les sujets de chacun, sans IA."""
    from . import digest as dg

    n = dg.run(conn, telegram.send)
    if n:
        print(f"[récapitulatif] {n} envoyés", file=sys.stderr, flush=True)


def submissions_once(conn, client: fetch.Client) -> dict:
    """Un passage de la collecte des soumissions (index toutes les 30 min, puis un lot de téléchargements)."""
    from . import soumissions

    out = soumissions.harvest_tick(conn, client)
    if out:
        print(f"[soumissions] {out}", file=sys.stderr, flush=True)
    return out


def submissions_forever(stop: threading.Event, interval: int = 60) -> None:
    """Fil dédié : la collecte des soumissions ne dépend pas de la veille documentaire, dont le premier passage
    après un redémarrage dure ~20 min ; elle démarre dès le lancement du worker. Les documents indexés ici sont
    vectorisés et confrontés aux alertes par la boucle principale (stock ancien exclu des alertes)."""
    conn = db.connect()
    client = fetch.Client()
    while not stop.is_set():
        try:
            submissions_once(conn, client)
        except Exception:
            traceback.print_exc()
        stop.wait(interval)


def run_forever(bot: bool = True, watch: bool = True, interval: int = 60) -> None:
    stop = threading.Event()
    if bot and config.TELEGRAM_TOKEN:
        threading.Thread(target=telegram.poll_forever, args=(stop,), daemon=True, name="telegram").start()
        print("[bot] Telegram actif", file=sys.stderr, flush=True)
    from . import mailin

    threading.Thread(target=mailin.watch_forever, args=(stop,), daemon=True, name="mail").start()
    from . import live_worker

    threading.Thread(target=live_worker.run_forever, args=(stop,), daemon=True, name="direct").start()
    if watch and config.SUB:
        threading.Thread(target=submissions_forever, args=(stop,), daemon=True, name="soumissions").start()
    conn = db.connect()
    client = fetch.Client()
    alerts.run(conn, notify=False)  # initialise le curseur sans alerter sur l'existant
    last_purge = 0.0
    while True:
        try:
            if watch:
                watch_once(conn, client)
            else:
                alerts.run(conn)
            digest(conn)
            if time.time() - last_purge > 3600:  # rétention des journaux d'usage et de la démo
                from . import usage

                last_purge = time.time()
                out = usage.purge(conn)
                if any(out.values()):
                    print(f"[usage] purge {out}", file=sys.stderr, flush=True)
        except Exception:
            traceback.print_exc()
        time.sleep(interval)
