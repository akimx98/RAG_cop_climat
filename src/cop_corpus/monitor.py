"""Erreurs de production : journal, regroupement et alerte Telegram aux administrateurs.

Toute erreur qui empêche un utilisateur d'obtenir ce qu'il demande (réponse à une question, page en erreur 500),
ou qui casse une boucle du worker, passe par `report()` :
- elle est écrite dans les journaux Docker avec sa trace ;
- elle est comptée dans la table `error_events` (une ligne par type d'erreur et par endroit) ;
- les administrateurs dont le compte est relié à Telegram reçoivent un message, au plus un toutes les
  `RESEND_MINUTES` pour une même erreur (avec le nombre d'occurrences depuis le dernier message).

Les messages ne contiennent ni question d'utilisateur ni texte de document : l'endroit, le type d'exception et
le début de son message seulement. `check_answers()` (appelé par le worker) alerte aussi quand les dernières
questions du site ont toutes échoué, même si l'erreur n'est pas passée par `report()`."""

from __future__ import annotations

import html
import logging
import re
import threading
import traceback
from datetime import datetime, timedelta, timezone

from . import config, db

log = logging.getLogger("cop.errors")
if not log.handlers:   # journaux Docker : stderr, quelle que soit la configuration d'uvicorn
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s [erreur] %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.ERROR)
    log.propagate = False
RESEND_MINUTES = 30
_ready: set[str] = set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure(conn) -> None:
    key = str(config.DATA_DIR)
    if key in _ready:
        return
    conn.execute("""CREATE TABLE IF NOT EXISTS error_events (
        key TEXT PRIMARY KEY, place TEXT, kind TEXT, message TEXT, first_at TEXT, last_at TEXT,
        count INTEGER DEFAULT 0, unsent INTEGER DEFAULT 0, sent_at TEXT)""")
    _ready.add(key)


def _describe(err: BaseException | str) -> tuple[str, str]:
    if isinstance(err, BaseException):
        return type(err).__name__, str(err)
    kind, _, msg = str(err).partition(": ")
    return (kind, msg) if msg and re.fullmatch(r"[A-Za-z_][\w.]*", kind) else ("Erreur", str(err))


def _key(place: str, kind: str, msg: str) -> str:
    # Même erreur à quelques chiffres près (identifiants, durées) = même ligne.
    return f"{place}|{kind}|{re.sub(r'[0-9]+', '#', msg)[:80]}"


def admin_chats(conn) -> list[str]:
    rows = conn.execute("SELECT telegram_chat_id FROM users WHERE is_admin=1 AND active=1 "
                        "AND telegram_chat_id IS NOT NULL").fetchall()
    return [r[0] for r in rows]


def _send(chats: list[str], text: str) -> None:
    from . import telegram

    for chat in chats:
        try:
            telegram.send(chat, text)
        except Exception:
            log.exception("alerte Telegram non envoyée")


def _spawn(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()   # Telegram peut être lent : on ne bloque pas


def report(place: str, err: BaseException | str, *, conn=None, background: bool = True) -> bool:
    """Signale une erreur. Renvoie True si une alerte est partie. Ne lève jamais d'exception."""
    try:
        kind, msg = _describe(err)
        if isinstance(err, BaseException) and err.__traceback__ is not None:
            log.error("[%s] %s", place, "".join(traceback.format_exception(err)).rstrip())
        else:
            log.error("[%s] %s: %s", place, kind, msg)
        conn = conn or db.get()
        _ensure(conn)
        key, now = _key(place, kind, msg), _now()
        row = conn.execute("SELECT count, unsent, sent_at FROM error_events WHERE key=?", (key,)).fetchone()
        if row:
            conn.execute("UPDATE error_events SET last_at=?, message=?, count=count+1, unsent=unsent+1 WHERE key=?",
                         (_fmt(now), msg[:500], key))
        else:
            conn.execute("INSERT INTO error_events(key, place, kind, message, first_at, last_at, count, unsent) "
                         "VALUES(?,?,?,?,?,?,1,1)", (key, place, kind, msg[:500], _fmt(now), _fmt(now)))
        sent_at = row["sent_at"] if row else None
        if sent_at and now - datetime.strptime(sent_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) \
                < timedelta(minutes=RESEND_MINUTES):
            conn.commit()
            return False
        n = (row["unsent"] if row else 0) + 1
        conn.execute("UPDATE error_events SET unsent=0, sent_at=? WHERE key=?", (_fmt(now), key))
        conn.commit()
        chats = admin_chats(conn)
        if not chats or not config.TELEGRAM_TOKEN:
            return False
        times = f" ({n} fois depuis la dernière alerte)" if n > 1 else ""
        text = (f"⚠️ <b>Erreur en production</b> : {html.escape(place)}{times}\n"
                f"<code>{html.escape(kind)}: {html.escape(msg[:300])}</code>\n"
                f"Détails : docker logs cop31-app-1 / cop31-worker-1")
        if background:
            _spawn(_send, chats, text)
        else:
            _send(chats, text)
        return True
    except Exception:
        log.exception("monitor.report a échoué")
        return False


def check_answers(conn, window_minutes: int = 60, streak: int = 3) -> bool:
    """Les `streak` dernières questions du site (dans l'heure) ont toutes échoué → alerte."""
    since = _fmt(_now() - timedelta(minutes=window_minutes))
    rows = conn.execute("SELECT error FROM demo_answers WHERE created_at>=? AND (error IS NULL OR error NOT IN "
                        "('conversation deleted')) ORDER BY created_at DESC LIMIT ?", (since, streak)).fetchall()
    if len(rows) < streak or any(r["error"] is None for r in rows):
        return False
    return report("site : questions en échec",
                  f"Les {streak} dernières questions ont échoué. Dernière erreur : {rows[0]['error'][:200]}",
                  conn=conn, background=False)
