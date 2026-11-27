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


# Destinataires du dernier relevé réussi : repli quand la base ne répond pas (alerte envoyée quand même, #20).
_last_admins: dict[str, list[str]] = {"chats": [], "emails": []}
# Repli sans base : dernier envoi par clé d'erreur, dans ce processus (même cadence que error_events).
_mem_sent: dict[str, datetime] = {}
_mem_lock = threading.Lock()


def _mem_recent(key: str, now: datetime) -> datetime | None:
    """Envoi du repli pour `key` depuis moins de RESEND_MINUTES ; purge les entrées plus anciennes. Sous _mem_lock."""
    for k in [k for k, t in _mem_sent.items() if now - t >= timedelta(minutes=RESEND_MINUTES)]:
        del _mem_sent[k]
    return _mem_sent.get(key)


def admin_chats(conn) -> list[str]:
    rows = conn.execute("SELECT telegram_chat_id FROM users WHERE is_admin=1 AND active=1 "
                        "AND telegram_chat_id IS NOT NULL").fetchall()
    _last_admins["chats"] = [r[0] for r in rows]
    return list(_last_admins["chats"])


def admin_emails(conn) -> list[str]:
    _last_admins["emails"] = [r[0] for r in conn.execute(
        "SELECT email FROM users WHERE is_admin=1 AND active=1 AND email LIKE '%@%'")]
    return list(_last_admins["emails"])


def _fallback(place: str, kind: str, msg: str, background: bool) -> bool:
    """La base ne permet pas de compter l'erreur (verrou de plus de 30 s, base illisible) : l'alerte part quand même,
    au plus une fois par erreur et par RESEND_MINUTES dans ce processus, vers les administrateurs du dernier relevé.
    Même contenu que l'alerte normale (lieu, type, début du message)."""
    key, now = _key(place, kind, msg), _now()
    if not (_last_admins["chats"] or _last_admins["emails"]):
        log.error("ALERTE NON TRANSMISE (base indisponible, destinataires inconnus) : [%s] %s", place, kind)
        return False   # rien n'est noté : l'erreur suivante retentera dès que les destinataires seront connus
    with _mem_lock:
        if _mem_recent(key, now):
            return False
        _mem_sent[key] = now
    text = (f"⚠️ <b>Erreur en production</b> : {html.escape(place)} (base indisponible : non comptée)\n"
            f"<code>{html.escape(kind)}: {html.escape(msg[:300])}</code>\n"
            f"Détails : docker logs cop31-app-1 / cop31-worker-1")
    args = (list(_last_admins["chats"]), text, list(_last_admins["emails"]))
    if background:
        _spawn(_send, *args)
        return True
    return _send(*args)


def _send(chats: list[str], text: str, emails: list[str] | None = None) -> bool:
    """Telegram d'abord ; si aucun message n'est parti (pas de compte relié, jeton révoqué, Telegram injoignable),
    e-mail aux administrateurs. Vrai si au moins un message est parti. Une alerte ne doit jamais se perdre en silence."""
    from . import mailout, telegram

    sent = False
    if config.TELEGRAM_TOKEN:
        for chat in chats:
            try:
                sent = telegram.send(chat, text) or sent
            except Exception:
                log.exception("alerte Telegram non envoyée")
    if not sent and emails and mailout.enabled():
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        for to in emails:
            try:
                sent = mailout.send(to, "[COP31] " + plain.splitlines()[0][:120], plain) or sent
            except Exception:
                log.exception("alerte e-mail non envoyée")
    if not sent:
        log.error("ALERTE NON TRANSMISE (ni Telegram ni e-mail) : %s", re.sub(r"<[^>]+>", "", text)[:300])
    return sent


def notify_admins(text: str, conn=None, background: bool = True) -> None:
    """Message aux administrateurs (Telegram, à défaut e-mail), hors regroupement des erreurs."""
    try:
        conn = conn or db.get()
        args = (admin_chats(conn), text, admin_emails(conn))
        if background:
            _spawn(_send, *args)
        else:
            _send(*args)
    except Exception:
        log.exception("notify_admins a échoué")


def _spawn(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()   # Telegram peut être lent : on ne bloque pas


def report(place: str, err: BaseException | str, *, conn=None, background: bool = True, urgent: bool = False) -> bool:
    """Signale une erreur. Renvoie True si une alerte est partie. Ne lève jamais d'exception.
    Hors session (config.SESSION=0), seule une erreur `urgent` (blocage du chien de garde) part aux administrateurs ;
    les autres sont seulement comptées dans error_events (relevées par la surveillance quotidienne)."""
    kind = msg = None
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
        if not (config.SESSION or urgent):
            conn.commit()
            return False
        sent_at = row["sent_at"] if row else None
        if sent_at and now - datetime.strptime(sent_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) \
                < timedelta(minutes=RESEND_MINUTES):
            conn.commit()
            return False
        with _mem_lock:
            fell_back = _mem_recent(key, now)
        if fell_back:   # déjà partie par le repli pendant la panne de la base : notée, pas renvoyée (#28)
            conn.execute("UPDATE error_events SET unsent=0, sent_at=? WHERE key=?", (_fmt(fell_back), key))
            conn.commit()
            return False
        n = (row["unsent"] if row else 0) + 1
        conn.execute("UPDATE error_events SET unsent=0, sent_at=? WHERE key=?", (_fmt(now), key))
        conn.commit()
        chats, emails = admin_chats(conn), admin_emails(conn)
        times = f" ({n} fois depuis la dernière alerte)" if n > 1 else ""
        text = (f"⚠️ <b>Erreur en production</b> : {html.escape(place)}{times}\n"
                f"<code>{html.escape(kind)}: {html.escape(msg[:300])}</code>\n"
                f"Détails : docker logs cop31-app-1 / cop31-worker-1")
        if background:
            _spawn(_send, chats, text, emails)
            return True
        return _send(chats, text, emails)
    except Exception:
        log.exception("monitor.report a échoué")
        if kind is None:
            return False
        try:
            return _fallback(place, kind, msg, background)
        except Exception:
            log.exception("repli de monitor.report a échoué")
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
