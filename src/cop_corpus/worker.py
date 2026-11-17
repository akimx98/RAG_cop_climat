"""Veilleur : relève les sources surveillées à leur fréquence, indexe, calcule
les embeddings, déclenche les alertes ; bot Telegram ; récapitulatif du matin."""

from __future__ import annotations

import html
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import alerts, catalog, config, db, embed, fetch, ingest, search, telegram


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
    if os.environ.get("COP_SUB", "1") != "0":
        try:
            from . import soumissions

            out = soumissions.harvest_tick(conn, client)
            if out:
                print(f"[soumissions] {out}", file=sys.stderr, flush=True)
        except Exception:
            traceback.print_exc()
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


def morning_digest(conn) -> None:
    """Récapitulatif quotidien (heure locale de la COP) sur Telegram."""
    if not config.TELEGRAM_TOKEN:
        return
    tz = ZoneInfo(config.TIMEZONE)
    local = datetime.now(tz)
    today = local.strftime("%Y-%m-%d")
    if local.hour < config.DIGEST_HOUR or db.meta_get(conn, "digest_day") == today:
        return
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    users = conn.execute("SELECT * FROM users WHERE active=1 AND telegram_chat_id IS NOT NULL").fetchall()
    for u in users:
        user = dict(u)
        docs = search.latest(conn, user, {"added_since": since}, limit=200)
        if not docs:
            continue
        by_level: dict[str, list] = {}
        for d in docs:
            by_level.setdefault(str(d.get("niveau", "?")), []).append(d)
        from . import i18n

        en = i18n.default_for(user) == "en"
        lines = [f"☀️ <b>{'Summary for' if en else 'Récapitulatif du'} {local.strftime('%d/%m')}</b> — {len(docs)} documents "
                 f"{'in 24h' if en else 'en 24 h'}"]
        for lvl in ["1", "2", "R", "3", "4", "5", "?"]:
            items = by_level.get(lvl)
            if not items:
                continue
            label = ("internal" if en else "internes") if lvl == "R" else f"{'level' if en else 'niveau'} {lvl}"
            lines.append(f"\n<b>{label}</b> ({len(items)})")
            for d in items[:6]:
                title = ("Internal document" if en else "Document interne") if d.get("acces") == "restreint" and not config.TELEGRAM_RESTRICTED_SNIPPETS else d.get("titre") or "?"
                lines.append(f"• <a href=\"{d['lien']}\">{html.escape(title[:110])}</a>")
        n = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND created_at>=?", (u["id"], since)).fetchone()[0]
        lines.append(f"\n🔔 {n} {'alert(s) on your topics' if en else 'alerte(s) sur vos sujets'} · {config.PUBLIC_BASE}/")
        telegram.send(u["telegram_chat_id"], "\n".join(lines))
    db.meta_set(conn, "digest_day", today)


def run_forever(bot: bool = True, watch: bool = True, interval: int = 60) -> None:
    stop = threading.Event()
    if bot and config.TELEGRAM_TOKEN:
        threading.Thread(target=telegram.poll_forever, args=(stop,), daemon=True, name="telegram").start()
        print("[bot] Telegram actif", file=sys.stderr, flush=True)
    from . import mailin

    threading.Thread(target=mailin.watch_forever, args=(stop,), daemon=True, name="mail").start()
    from . import live_worker

    threading.Thread(target=live_worker.run_forever, args=(stop,), daemon=True, name="direct").start()
    conn = db.connect()
    client = fetch.Client()
    alerts.run(conn, notify=False)  # initialise le curseur sans alerter sur l'existant
    while True:
        try:
            if watch:
                watch_once(conn, client)
            else:
                alerts.run(conn)
            morning_digest(conn)
        except Exception:
            traceback.print_exc()
        time.sleep(interval)
