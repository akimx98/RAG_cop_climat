"""Courrier entrant : dépôt de documents par mail et abonnement aux lettres d'information.

Deux sortes d'adresses sur ``config.MAIL_DOMAIN`` :

- ``depot-<code>@`` : une par collègue, le code est secret (comme l'adresse MCP). Tout ce qui y
  arrive vient donc de lui : pièces jointes et corps du mail entrent au corpus, **restreints** par
  défaut (``[public]`` dans l'objet pour les rendre publics). Il reçoit un accusé générique sur Telegram.
- ``veille@`` : adresse d'abonnement aux lettres (ENB, Carbon Brief…). Les mails des expéditeurs
  autorisés entrent comme documents publics de niveau 4 ; les autres (premier envoi d'une lettre,
  mails de confirmation d'abonnement) attendent dans la boîte « Courrier » de l'admin.

Le serveur SMTP (``serve``) ne fait que trier les destinataires et déposer le message brut dans
``data/mail/spool`` ; le worker le traite (``process_spool``). Toute autre adresse est refusée au
RCPT : pas de relais ouvert, pas de rétrodiffusion.
"""

from __future__ import annotations

import email
import email.policy
import json
import re
import secrets
import sqlite3
import ssl
import sys
import time
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from . import alerts, config, db, ingest, telegram

SPOOL = "spool"
ARCHIVE = "archive"
VEILLE = "veille"
_DEPOT = re.compile(r"^depot-([a-z0-9]{6,32})$")
_CONFIRM = re.compile(
    r"confirm|verify|v[ée]rifi|activ|valid|opt.?in|abonnement|subscription|subscribe|inscription|welcome|bienvenue",
    re.I)
# Expéditeur déjà autorisé : on ne retient que les vraies demandes d'action (lien à cliquer), pas les
# lettres ordinaires dont l'objet contient « activists », « validated », « subscription »…
_CONFIRM_STRICT = re.compile(
    r"confirm (your|my|the)|please confirm|verify (your|my)|v[ée]rifiez|confirmez|activate your|activez votre|"
    r"double.?opt|complete your (sign.?up|subscription|registration)", re.I)
_FWD = re.compile(r"^\s*((fwd?|tr|fw|re|réf|ref)\s*:\s*)+", re.I)
_PUBLIC_TAG = re.compile(r"\[\s*public\s*\]", re.I)
MIN_BODY = 200


# --- adresses ----------------------------------------------------------------------------

def deposit_address(conn: sqlite3.Connection, user: dict) -> str:
    code = user.get("mail_code")
    if not code:
        code = new_code(conn, user["id"])
    return f"depot-{code}@{config.MAIL_DOMAIN}"


def new_code(conn: sqlite3.Connection, user_id: int) -> str:
    code = secrets.token_hex(6)
    conn.execute("UPDATE users SET mail_code=? WHERE id=?", (code, user_id))
    conn.commit()
    return code


def veille_address() -> str:
    return f"{VEILLE}@{config.MAIL_DOMAIN}"


def route(conn: sqlite3.Connection, address: str) -> tuple[str, dict | None] | None:
    """('veille', None), ('depot', utilisateur) ou None si l'adresse n'existe pas."""
    local, _, domain = address.lower().strip().strip("<>").rpartition("@")
    if domain != config.MAIL_DOMAIN.lower():
        return None
    if local == VEILLE:
        return VEILLE, None
    m = _DEPOT.match(local)
    if m:
        row = conn.execute("SELECT * FROM users WHERE mail_code=? AND active=1", (m.group(1),)).fetchone()
        if row and (row["is_admin"] or row["can_restricted"]):  # jamais d'adresse de dépôt active pour un invité
            return "depot", dict(row)
    return None


# --- liste des expéditeurs autorisés pour veille@ ----------------------------------------

def allowed_senders(conn: sqlite3.Connection) -> list[str]:
    return json.loads(db.meta_get(conn, "mail_allow", "[]") or "[]")


def allow_sender(conn: sqlite3.Connection, sender: str) -> None:
    items = allowed_senders(conn)
    sender = sender.lower().strip()
    if sender and sender not in items:
        db.meta_set(conn, "mail_allow", json.dumps(sorted(items + [sender])))


def is_allowed(conn: sqlite3.Connection, sender: str) -> bool:
    sender = sender.lower()
    domain = sender.rpartition("@")[2]
    return any(a == sender or a == domain or domain.endswith("." + a) for a in allowed_senders(conn))


# --- serveur SMTP ------------------------------------------------------------------------

class _Handler:
    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        if not route(db.get(), address):
            return "550 5.1.1 Adresse inconnue"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        if len(envelope.content or b"") > config.MAIL_MAX_MB * 1024 * 1024:
            return "552 5.3.4 Message trop volumineux"
        folder = config.MAIL_DIR / SPOOL
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"
        head = (f"X-Cop-Rcpt: {', '.join(envelope.rcpt_tos)}\r\n"
                f"X-Cop-Peer: {session.peer[0] if session.peer else ''}\r\n"
                f"X-Cop-TLS: {'oui' if session.ssl else 'non'}\r\n").encode()
        tmp = folder / f".{name}.tmp"
        tmp.write_bytes(head + envelope.original_content)
        tmp.rename(folder / f"{name}.eml")
        return "250 OK"  # réponses SMTP en ASCII strict


def _tls_context() -> ssl.SSLContext | None:
    cert, key = Path(config.MAIL_CERT), Path(config.MAIL_KEY)
    if not (cert.exists() and key.exists()):
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)
    return ctx


def serve(host: str = "0.0.0.0", port: int = 2525) -> None:
    from aiosmtpd.controller import Controller
    from aiosmtpd.smtp import SMTP

    class _Controller(Controller):
        def factory(self):
            # contexte TLS relu à chaque connexion : suit les renouvellements du certificat par Caddy
            return SMTP(self.handler, hostname=config.MAIL_DOMAIN, tls_context=_tls_context(),
                        require_starttls=False, data_size_limit=int(config.MAIL_MAX_MB * 1024 * 1024),
                        ident="COP31 veille")

    tls = "STARTTLS actif" if _tls_context() else "SANS TLS (certificat introuvable)"
    ctl = _Controller(_Handler(), hostname=host, port=port)
    ctl.start()
    print(f"[mail] SMTP sur {host}:{port} pour @{config.MAIL_DOMAIN}, {tls}", file=sys.stderr, flush=True)
    try:
        while True:
            time.sleep(3600)
    finally:
        ctl.stop()


# --- traitement --------------------------------------------------------------------------

def _clean_subject(subject: str) -> str:
    return _FWD.sub("", _PUBLIC_TAG.sub("", subject or "")).strip() or "(sans objet)"


def _attachments(msg: EmailMessage):
    """Pièces jointes, y compris celles d'un mail transféré « en pièce jointe » (message/rfc822)."""
    for part in msg.walk():
        if part.get_content_type() == "message/rfc822":
            continue
        name = part.get_filename()
        if not name or part.is_multipart():
            continue
        if part.get_content_maintype() == "image" and part.get("Content-ID") and "inline" in (part.get("Content-Disposition") or ""):
            continue  # logos et signatures intégrés au corps
        data = part.get_payload(decode=True)
        if data:
            yield name, part.get_content_type(), data


def _body(msg: EmailMessage) -> tuple[str, str]:
    """(texte, html) du corps principal."""
    text = html = ""
    for part in msg.walk():
        if part.get_filename() or part.is_multipart():
            continue
        ctype = part.get_content_type()
        try:
            content = part.get_content()
        except Exception:
            continue
        if ctype == "text/plain" and not text:
            text = content
        elif ctype == "text/html" and not html:
            html = content
    return text, html


def body_text(msg: EmailMessage) -> str:
    text, html = _body(msg)
    if html and len(text.strip()) < MIN_BODY:
        text = BeautifulSoup(html, "lxml").get_text("\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def links(msg: EmailMessage, limit: int = 40) -> list[tuple[str, str]]:
    """Liens du mail (pour cliquer soi-même une confirmation d'abonnement)."""
    text, html = _body(msg)
    out: list[tuple[str, str]] = []
    if html:
        for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("http://", "https://")):
                out.append((" ".join(a.get_text(" ").split())[:90] or href[:90], href))
    else:
        out = [(u[:90], u) for u in re.findall(r"https?://[^\s<>\"')]+", text)]
    seen, uniq = set(), []
    for label, href in out:
        if href not in seen:
            seen.add(href)
            uniq.append((label, href))
    return uniq[:limit]


def _ingest_body(conn, msg: EmailMessage, title: str, **kwargs) -> int | None:
    text, html = _body(msg)
    if len(body_text(msg)) < MIN_BODY:
        return None
    content, mime, fname = ((html.encode(), "text/html", "message.html") if html and len(text.strip()) < MIN_BODY
                            else (text.encode(), "text/plain", "message.txt"))
    doc_id, _, _ = ingest.ingest_bytes(conn, content, url=None, mime=mime, filename=fname,
                                       meta=dict(kwargs.pop("meta", {}), title=title), **kwargs)
    return doc_id


def _record(conn, mailbox, user_id, sender, subject, path, status, detail="", doc_ids=None) -> int:
    cur = conn.execute(
        "INSERT INTO mail_inbox(received_at,mailbox,user_id,sender,subject,path,status,detail,doc_ids) VALUES(?,?,?,?,?,?,?,?,?)",
        (db.now(), mailbox, user_id, sender, subject, path, status, detail, json.dumps(doc_ids or [])))
    conn.commit()
    return cur.lastrowid


def _deposit(conn, msg: EmailMessage, user: dict, rel: str) -> int:
    subject = msg.get("Subject", "") or ""
    public = bool(_PUBLIC_TAG.search(subject))
    access = "public" if public else "restreint"
    title = _clean_subject(subject)
    note = f"Transmis par mail par {user.get('name') or user['email']} : {title}"
    common = dict(source_id="depot-mail", access=access, uploaded_by=user["id"], note=note)
    ids = []
    for name, ctype, data in _attachments(msg):
        doc_id, _, _ = ingest.ingest_bytes(conn, data, url=None, mime=ctype, filename=name,
                                           meta={"doc_type": None} if public else {"doc_type": "note_interne"}, **common)
        ids.append(doc_id)
    body_id = _ingest_body(conn, msg, title, meta={} if public else {"doc_type": "note_interne"}, **common)
    if body_id:
        ids.append(body_id)
    sender = parseaddr(msg.get("From", ""))[1]
    _record(conn, "depot", user["id"], sender, title, rel, "ingere" if ids else "vide", doc_ids=ids)
    if user.get("telegram_chat_id"):
        what = f"{len(ids)} document(s) ajouté(s) au corpus ({'public' if public else 'interne'})" if ids \
            else "aucun contenu exploitable (ni pièce jointe ni texte)"
        telegram.send(user["telegram_chat_id"], f"📥 Dépôt par mail reçu : {what}.\n{config.PUBLIC_BASE}/deposer")
    return len(ids)


def ingest_newsletter(conn, msg: EmailMessage, rel: str, inbox_id: int | None = None) -> list[int]:
    name, sender = parseaddr(msg.get("From", ""))
    title = f"{name or sender} — {_clean_subject(msg.get('Subject', ''))}"
    common = dict(source_id="veille-mail", access="public", meta={"level": "4", "doc_type": "analyse",
                                                                    "org": name or sender.rpartition("@")[2]})
    ids = []
    body_id = _ingest_body(conn, msg, title, **common)
    if body_id:
        ids.append(body_id)
    for fname, ctype, data in _attachments(msg):
        if fname.lower().endswith((".pdf", ".docx")):
            doc_id, _, _ = ingest.ingest_bytes(conn, data, url=None, mime=ctype, filename=fname,
                                               **{**common, "meta": dict(common["meta"])})
            ids.append(doc_id)
    if inbox_id:
        conn.execute("UPDATE mail_inbox SET status='ingere', doc_ids=? WHERE id=?", (json.dumps(ids), inbox_id))
        conn.commit()
    return ids


def _newsletter(conn, msg: EmailMessage, rel: str) -> None:
    name, sender = parseaddr(msg.get("From", ""))
    subject = msg.get("Subject", "") or ""
    allowed = is_allowed(conn, sender)
    if allowed and not _CONFIRM_STRICT.search(subject):
        ids = ingest_newsletter(conn, msg, rel)
        _record(conn, VEILLE, None, sender, subject, rel, "ingere" if ids else "vide", doc_ids=ids)
        return
    why = "confirmation d'abonnement ?" if (_CONFIRM_STRICT if allowed else _CONFIRM).search(subject) \
        else "expéditeur pas encore autorisé"
    # Une seule notification par expéditeur tant que ses mails attendent (pas une par mail).
    already = conn.execute("SELECT 1 FROM mail_inbox WHERE mailbox=? AND sender=? AND status='attente' LIMIT 1",
                           (VEILLE, sender)).fetchone()
    _record(conn, VEILLE, None, sender, subject, rel, "attente", why)
    if already:
        return
    for row in conn.execute("SELECT telegram_chat_id FROM users WHERE is_admin=1 AND active=1 AND telegram_chat_id IS NOT NULL"):
        telegram.send(row["telegram_chat_id"],
                      f"📬 veille@ : mail en attente ({why})\n{name or sender} — {subject[:120]}\n{config.PUBLIC_BASE}/admin/courrier")


def load(rel: str) -> EmailMessage:
    return email.message_from_bytes((config.DATA_DIR / rel).read_bytes(), policy=email.policy.default)


def process_spool(conn: sqlite3.Connection) -> int:
    spool = config.MAIL_DIR / SPOOL
    if not spool.exists():
        return 0
    archive = config.MAIL_DIR / ARCHIVE / time.strftime("%Y-%m")
    done = 0
    for path in sorted(spool.glob("*.eml")):
        archive.mkdir(parents=True, exist_ok=True)
        target = archive / path.name
        path.rename(target)
        rel = str(target.relative_to(config.DATA_DIR))
        try:
            msg = load(rel)
            for rcpt in (msg.get("X-Cop-Rcpt", "") or "").split(","):
                routed = route(conn, rcpt)
                if not routed:
                    continue
                kind, user = routed
                if kind == "depot":
                    _deposit(conn, msg, user, rel)
                else:
                    _newsletter(conn, msg, rel)
            done += 1
        except Exception as exc:  # un mail illisible ne bloque pas les suivants
            _record(conn, "?", None, "", path.name, rel, "erreur", repr(exc)[:300])
            print(f"[mail] erreur sur {path.name} : {exc!r}", file=sys.stderr, flush=True)
    if done:
        alerts.run(conn)
    return done


def watch_forever(stop=None, interval: int = 10) -> None:
    conn = db.connect()
    while not (stop and stop.is_set()):
        try:
            process_spool(conn)
        except Exception as exc:
            print(f"[mail] {exc!r}", file=sys.stderr, flush=True)
        time.sleep(interval)


def inbox(conn: sqlite3.Connection, limit: int = 80) -> list[dict[str, Any]]:
    rows = conn.execute("""SELECT m.*, u.email AS user_email FROM mail_inbox m LEFT JOIN users u ON u.id=m.user_id
                           ORDER BY m.id DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]
