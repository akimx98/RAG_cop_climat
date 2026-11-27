"""Courrier entrant : abonnement aux lettres d'information.

Une seule adresse sur ``config.MAIL_DOMAIN`` (le dépôt de documents par mail, ``depot-<code>@``, est retiré) :

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

def veille_address() -> str:
    return f"{VEILLE}@{config.MAIL_DOMAIN}"


def route(conn: sqlite3.Connection, address: str) -> tuple[str, dict | None] | None:
    """('veille', None), ou None si l'adresse n'existe pas (le dépôt par mail est retiré)."""
    local, _, domain = address.lower().strip().strip("<>").rpartition("@")
    if domain != config.MAIL_DOMAIN.lower():
        return None
    if local == VEILLE:
        return VEILLE, None
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
        telegram.send(user["telegram_chat_id"], f"📥 Dépôt par mail reçu : {what}.")
    return len(ids)


# --- lettres d'information : texte complet ------------------------------------------------

# Liens de pistage, de désabonnement et de gestion du compte : jamais retenus comme liens de contenu.
_TRACKING = re.compile(
    r"clicks?\.|mlsend|cmail\d*\.com|createsend|list-manage|mailchimp|mcusercontent|updatemyprofile|forwardtomyfriend|"
    r"unsubscribe|d[ée]sabonn|desinscri|/preferences|sendgrid|hubspot|substack\.com/redirect|mailerlite|brevo|sendinblue|"
    r"t\.co/|bit\.ly|facebook\.com|twitter\.com|x\.com/|linkedin\.com|instagram\.com|youtube\.com|bsky\.app|/donate", re.I)
# Partie texte de remplacement (« votre logiciel ne peut pas afficher le HTML ») : la lettre est dans le HTML.
_STUB = re.compile(r"can.?t display HTML|cannot display HTML|unable to display|ne peut pas afficher|"
                   r"view (this|the) (e-?mail|newsletter)|view it in your browser|version en ligne|"
                   r"afficher dans (votre|le) navigateur", re.I)
_BLOCK = ("p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote", "section", "article", "ul", "ol")
# Expéditeurs dont la lettre est un compte rendu de négociation (niveau 4, type compte_rendu).
_REPORT_SENDERS = re.compile(r"earth negotiations|iisd|carbon ?brief|third world network|twn|climate home", re.I)


def html_to_text(html: str) -> str:
    """Texte lisible d'une lettre HTML (mise en page en tableaux) : un bloc par ligne, sans styles ni scripts."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "head", "title", "meta", "noscript"]):
        tag.decompose()
    for tag in soup.find_all(style=re.compile(r"display\s*:\s*none|max-height\s*:\s*0", re.I)):
        tag.decompose()  # texte d'aperçu caché (« preheader »)
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(_BLOCK):
        tag.insert_before("\n")
        tag.append("\n")
    lines = [" ".join(l.split()) for l in soup.get_text("").split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def content_links(msg: EmailMessage) -> list[tuple[str, str]]:
    """Liens de contenu de la lettre (articles, comptes rendus), sans pistage ni réseaux sociaux. Les deux parties
    sont lues : le HTML passe souvent par un redirecteur de pistage, la partie texte donne l'adresse directe."""
    text, html = _body(msg)
    found: list[tuple[str, str]] = []
    if html:
        for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
            found.append((" ".join(a.get_text(" ").split())[:120], a["href"].strip()))
    for m in re.finditer(r"(?:([^\n\[]{3,120})\s*)?\[?(https?://[^\s<>\"'\])]+)", text or ""):
        found.append(((m.group(1) or "").strip(), m.group(2)))
    seen, out = set(), []
    for label, href in found:
        href = href.rstrip(".,;")
        if not href.startswith(("http://", "https://")) or _TRACKING.search(href) or href in seen:
            continue
        seen.add(href)
        out.append((label or href, href))
    return out


def newsletter_text(msg: EmailMessage) -> str:
    """Texte complet d'une lettre : la partie HTML dès qu'elle est plus riche que la partie texte (ou que celle-ci
    n'est qu'un message de remplacement), puis la liste des liens de contenu."""
    text, html = _body(msg)
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    body = text
    if html:
        rendered = html_to_text(html)
        if len(rendered) > 1.2 * len(text) or (_STUB.search(text[:600]) and len(rendered) > len(text) * 0.8) \
                or len(text) < MIN_BODY:
            body = rendered
    links = [(l, h) for l, h in content_links(msg) if h not in body]
    if links:
        body += "\n\nLiens de la lettre :\n" + "\n".join(f"- {l} [{h}]" if l != h else f"- {h}" for l, h in links[:120])
    return body


def _mail_date(msg: EmailMessage) -> str | None:
    from email.utils import parsedate_to_datetime
    from zoneinfo import ZoneInfo

    try:
        dt = parsedate_to_datetime(msg.get("Date", ""))
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d")
    return dt.astimezone(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")


def newsletter_meta(msg: EmailMessage) -> dict[str, Any]:
    name, sender = parseaddr(msg.get("From", ""))
    org = name or sender.rpartition("@")[2]
    day = _mail_date(msg)
    subject = _clean_subject(msg.get("Subject", ""))
    title = f"{org} — {subject}" + (f" ({day})" if day and day not in subject else "")
    report = bool(_REPORT_SENDERS.search(f"{name} {sender}"))
    return {"level": "4", "doc_type": "compte_rendu" if report else "analyse", "org": org, "title": title,
            "published": day}


def ingest_newsletter(conn, msg: EmailMessage, rel: str, inbox_id: int | None = None) -> list[int]:
    """Lettre d'un expéditeur autorisé → document public de niveau 4, texte complet, daté du jour d'envoi.
    Chaque lettre est un document à part entière (jamais une « version » de la lettre de la veille)."""
    meta = newsletter_meta(msg)
    common = dict(source_id="veille-mail", access="public")
    ids = []
    body = newsletter_text(msg)
    if len(body) >= MIN_BODY:
        doc_id, _, _ = ingest.ingest_bytes(conn, body.encode("utf-8"), url=None, mime="text/plain",
                                           filename="lettre.txt", meta=dict(meta), **common)
        ids.append(doc_id)
    for fname, ctype, data in _attachments(msg):
        if fname.lower().endswith((".pdf", ".docx")):
            doc_id, _, _ = ingest.ingest_bytes(conn, data, url=None, mime=ctype, filename=fname,
                                               meta={k: v for k, v in meta.items() if k != "title"}, **common)
            ids.append(doc_id)
    if inbox_id:
        conn.execute("UPDATE mail_inbox SET status='ingere', doc_ids=? WHERE id=?", (json.dumps(ids), inbox_id))
        conn.commit()
    return ids


def reread_newsletters(conn, since: str | None = None, dry_run: bool = False) -> dict[str, int]:
    """Relit les lettres déjà reçues (fichiers .eml archivés) avec l'extraction actuelle et remplace, sur place,
    le texte de chaque document (même identifiant : liens, alertes et notes restent valides). Les positions sont
    réextraites au passage suivant. Sert après une correction de l'extraction (texte complet, date, titre)."""
    counts = {"relues": 0, "remplacees": 0, "inchangees": 0, "absentes": 0}
    rows = conn.execute("""SELECT * FROM mail_inbox WHERE mailbox=? AND status='ingere' AND received_at>=?
                           ORDER BY id""", (VEILLE, since or "")).fetchall()
    for row in rows:
        path = config.DATA_DIR / row["path"]
        if not path.exists():
            counts["absentes"] += 1
            continue
        msg = load(row["path"])
        counts["relues"] += 1
        ids = json.loads(row["doc_ids"] or "[]")
        body_doc = next((d for d in (conn.execute("SELECT * FROM documents WHERE id=?", (i,)).fetchone() for i in ids)
                         if d and Path(d["raw_path"] or "").suffix in (".txt", ".html")), None)
        meta = newsletter_meta(msg)
        body = newsletter_text(msg).encode("utf-8")
        if body_doc is None:
            if not dry_run and len(body) >= MIN_BODY:
                new_ids = ingest_newsletter(conn, msg, row["path"])
                conn.execute("UPDATE mail_inbox SET doc_ids=? WHERE id=?", (json.dumps(list(dict.fromkeys(ids + new_ids))), row["id"]))
                conn.commit()
                counts["remplacees"] += 1
            continue
        from . import fetch

        digest = fetch.sha256(body)
        if digest == body_doc["sha256"] and body_doc["title"] == meta["title"] and body_doc["published"] == meta["published"]:
            counts["inchangees"] += 1
            continue
        if dry_run:
            counts["remplacees"] += 1
            continue
        other = conn.execute("SELECT id FROM documents WHERE sha256=? AND id<>?", (digest, body_doc["id"])).fetchone()
        if other:
            counts["inchangees"] += 1  # ce texte est déjà au corpus sous un autre identifiant
            continue
        _digest, raw = fetch.store_raw(body, ".txt")
        conn.execute("""UPDATE documents SET sha256=?, raw_path=?, mime='text/plain', size=?, title=?, published=?,
                          doc_type=?, org=?, lineage_key=NULL, status='pending' WHERE id=?""",
                     (digest, str(raw.relative_to(config.DATA_DIR)), len(body), meta["title"], meta["published"],
                      meta["doc_type"], meta["org"], body_doc["id"]))
        if _has_table(conn, "extraction_log"):
            conn.execute("DELETE FROM extraction_log WHERE doc_id=?", (body_doc["id"],))
        conn.commit()
        ingest.index_document(conn, body_doc["id"])
        counts["remplacees"] += 1
    if not dry_run:
        renumber_mail_versions(conn)
    return counts


def _has_table(conn, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def renumber_mail_versions(conn) -> int:
    """Lettres reçues avant la correction : chacune redevient un document à part (version 1, lignée propre)."""
    n = 0
    for d in conn.execute("""SELECT id, sha256 FROM documents WHERE source_id='veille-mail'
                             AND (lineage_key IS NULL OR lineage_key NOT LIKE 'mail:%' OR version<>1)""").fetchall():
        conn.execute("UPDATE documents SET lineage_key=?, version=1 WHERE id=?", (f"mail:{d['sha256'][:24]}", d["id"]))
        n += 1
    conn.commit()
    return n


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
    inflight = config.MAIL_DIR / INFLIGHT
    inflight.mkdir(parents=True, exist_ok=True)
    for path in sorted(spool.glob("*.eml")):
        archive.mkdir(parents=True, exist_ok=True)
        target = archive / path.name
        rel = str(target.relative_to(config.DATA_DIR))
        tries = int(path.name.count(RETRY_TAG))
        # Marqueur AVANT de déplacer la lettre, effacé une fois traitée : sinon reprise au démarrage.
        marker = inflight / f"{path.name}.json"
        marker.write_text(json.dumps({"rel": rel, "spool": path.name, "tries": tries}))
        path.rename(target)
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
        marker.unlink(missing_ok=True)
    if done:
        alerts.run(conn)
    return done


INFLIGHT = "en_cours"
RETRY_TAG = "-reprise"


def requeue_interrupted(conn) -> int:
    """Au démarrage : lettres dont le traitement a été interrompu (arrêt du worker) remises dans la file, une fois ;
    interrompues deux fois, enregistrées en erreur (et signalées) au lieu d'être perdues en silence."""
    inflight = config.MAIL_DIR / INFLIGHT
    if not inflight.exists():
        return 0
    n = 0
    for marker in sorted(inflight.glob("*.json")):
        try:
            info = json.loads(marker.read_text())
            src = config.DATA_DIR / info["rel"]
            if not src.exists():
                if (config.MAIL_DIR / SPOOL / info.get("spool", "")).is_file():
                    continue   # arrêt entre le marqueur et le déplacement : la lettre est encore dans la file
                from . import monitor

                monitor.report("worker : courrier entrant", f"Interruption: lettre {info['rel'][-40:]} introuvable "
                                                            "après un traitement interrompu", conn=conn)
                continue
            if info.get("tries", 0) >= 1:
                _record(conn, "?", None, "", src.name, info["rel"], "erreur", "traitement interrompu deux fois")
                from . import monitor

                monitor.report("worker : courrier entrant", f"Interruption: lettre {src.name[:40]} abandonnée après "
                                                            "deux traitements interrompus (voir /admin/courrier)",
                               conn=conn)
            else:
                spool = config.MAIL_DIR / SPOOL
                spool.mkdir(parents=True, exist_ok=True)
                src.rename(spool / f"{src.stem}{RETRY_TAG}{src.suffix}")
                n += 1
        finally:
            marker.unlink(missing_ok=True)
    return n


def watch_forever(stop=None, interval: int = 10) -> None:
    conn = db.connect()
    try:
        if requeue_interrupted(conn):
            print("[mail] lettres interrompues remises dans la file", file=sys.stderr, flush=True)
    except Exception as exc:
        from . import monitor

        monitor.report("worker : courrier entrant", exc, conn=conn)
    while not (stop and stop.is_set()):
        try:
            process_spool(conn)
            from . import watchdog

            watchdog.beat("courrier", conn)
        except Exception as exc:
            from . import monitor

            monitor.report("worker : courrier entrant", exc, conn=conn)
        time.sleep(interval)


def inbox(conn: sqlite3.Connection, limit: int = 80) -> list[dict[str, Any]]:
    rows = conn.execute("""SELECT m.*, u.email AS user_email FROM mail_inbox m LEFT JOIN users u ON u.id=m.user_id
                           ORDER BY m.id DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]
