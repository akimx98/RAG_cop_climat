"""Veilleur : relève les sources surveillées à leur fréquence, indexe, calcule
les embeddings, déclenche les alertes ; bot Telegram ; récapitulatif du matin."""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from . import alerts, catalog, config, db, embed, fetch, ingest, monitor, telegram, watchdog


def _due(conn, source: catalog.Source) -> bool:
    row = conn.execute("SELECT last_run FROM source_state WHERE source_id=?", (source.id,)).fetchone()
    if not row or not row["last_run"]:
        return True
    last = datetime.strptime(row["last_run"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    minutes = source.watch_minutes
    result = conn.execute("SELECT last_result FROM source_state WHERE source_id=?", (source.id,)).fetchone()
    text = (result[0] or "") if result else ""
    if text and "'new'" not in text and any(k in text for k in ("antibot", "'error'", "'payant'")):
        minutes = max(minutes * 8, 240)  # source bloquée, payante ou en erreur : on espace (pas de martèlement)
    return datetime.now(timezone.utc) - last >= timedelta(minutes=minutes)


def watch_once(conn, client: fetch.Client) -> int:
    sources = [s for s in catalog.load() if s.watch_minutes > 0 and _due(conn, s)]
    new = 0
    if sources:
        results = ingest.run_many(sources, workers=4)
        new = sum(r.get("new", 0) for r in results.values() if isinstance(r.get("new", 0), int))
        print(f"[veille] {len(sources)} sources, {new} nouveaux", file=sys.stderr, flush=True)
    if embed.enabled():
        try:
            embed.embed_pending(conn, limit=5000)
        except Exception as exc:
            monitor.report("worker : embeddings", exc, conn=conn)
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
            watchdog.beat("soumissions", conn)
        except Exception as exc:
            monitor.report("worker : soumissions", exc, conn=conn)
        stop.wait(interval)


def indexing_forever(stop: threading.Event, interval: int = 15) -> None:
    """Fil dédié aux documents 'pending' : PDF déposés sur le site (qui ne les lit jamais lui-même), indexations
    interrompues (quarantaine). Ils bloquent les alertes, qui s'arrêtent au premier document 'pending'."""
    conn = db.connect()
    while not stop.is_set():
        try:
            out = ingest.reindex_pending(conn)
            if out:
                print(f"[indexation] {out}", file=sys.stderr, flush=True)
            watchdog.beat("indexation", conn)
        except Exception as exc:
            monitor.report("worker : indexation", exc, conn=conn)
        stop.wait(interval)


def _install_stop_handler() -> None:
    """docker stop (SIGTERM) : noté comme arrêt propre DANS le gestionnaire, puis sortie immédiate (sans attendre les
    collectes en cours, que Docker couperait par SIGKILL). Tout autre arrêt (exception, segfault, mémoire) reste
    « anormal » et sera signalé au démarrage suivant. Python en PID 1 ignore SIGTERM sans gestionnaire."""
    import os
    import signal

    def stop(*_):
        watchdog.process_stopping("worker")   # fichier, pas la base : jamais bloqué par une transaction en cours
        ingest.clear_attempts()
        os._exit(0)

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)


def run_forever(bot: bool = True, watch: bool = True, interval: int = 60) -> None:
    stop = threading.Event()
    boot = db.connect()
    _install_stop_handler()
    try:
        watchdog.process_started(boot, "worker")   # arrêt anormal précédent, arrêts anormaux en boucle
    except Exception as exc:
        monitor.report("worker : démarrage", exc, conn=boot)
    loops = {"veille": 90, "courrier": 15, "indexation": 30}   # délai maximal entre deux tours, en minutes
    if bot and config.TELEGRAM_TOKEN:
        loops["telegram"] = 10
    if config.LIVE:
        loops["direct"] = 90
    if watch and config.SUB:
        loops["soumissions"] = 60
    try:
        watchdog.expect("worker", loops, boot)
    except Exception as exc:
        monitor.report("worker : démarrage", exc, conn=boot)
    if bot and config.TELEGRAM_TOKEN:
        threading.Thread(target=telegram.poll_forever, args=(stop,), daemon=True, name="telegram").start()
        print("[bot] Telegram actif", file=sys.stderr, flush=True)
    from . import mailin

    threading.Thread(target=mailin.watch_forever, args=(stop,), daemon=True, name="mail").start()
    from . import live_worker

    threading.Thread(target=live_worker.run_forever, args=(stop,), daemon=True, name="direct").start()
    if watch and config.SUB:
        threading.Thread(target=submissions_forever, args=(stop,), daemon=True, name="soumissions").start()
    threading.Thread(target=indexing_forever, args=(stop,), daemon=True, name="indexation").start()
    conn = db.connect()
    client = fetch.Client()
    alerts.run(conn, notify=False)  # initialise le curseur sans alerter sur l'existant
    last_purge = last_check = last_watch = 0.0
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
            if time.time() - last_check > 900:  # les dernières questions du site ont-elles toutes échoué ?
                last_check = time.time()
                monitor.check_answers(conn)
            watchdog.beat("veille", conn)
        except Exception as exc:
            monitor.report("worker : veille", exc, conn=conn)
        if time.time() - last_watch >= watchdog.CHECK_EVERY:  # surveille aussi le site (et inversement)
            last_watch = time.time()
            watchdog.check(conn, "worker")
        time.sleep(interval)
