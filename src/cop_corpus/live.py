"""Veille en direct de la COP31 : dossiers de négociation, textes en cours, positions, terrain.

Sources (accord de Carbon Brief, citation obligatoire) :
- Carbon Brief, « Tracking negotiating texts at the COP31 climate summit » : index des documents de session
  (cote, point de l'ordre du jour, heure, lien PDF, crochets, options) — feuille Google publique ;
- Carbon Brief, « Who wants what at the COP31 climate change summit » : positions des blocs et pays ;
- comptes Bluesky choisis (catalog/bluesky.yaml) : journalistes en salle, observateurs.

unfccc.int bloque les clients automatisés (anti-bot non contourné) : un texte annoncé est cherché poliment à son
adresse, puis sur l'ODS de l'ONU par sa cote ; sinon il reste « à récupérer » et un contributeur
l'ouvre dans son navigateur et le transfère (bot Telegram, page Textes, courrier).
"""

from __future__ import annotations

import csv
import html
import io
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from . import config, db, diff, fetch, ingest

CB_SHEET = "https://docs.google.com/spreadsheets/d/{id}/gviz/tq?tqx=out:csv&sheet={sheet}"
CB_TEXTS_ID = "1c2J-L_wLXI5AHJtjn_TsaVADYzudF7CCYXkKA23uM1E"
CB_WWW_ID = "1f1e2ezMXg3vN6fbBwJv5NgMab8vMSxTXzutniA3Bsnk"
CB_TEXTS_PAGE = "https://www.carbonbrief.org/interactive-tracking-negotiating-texts-at-the-cop31-climate-summit"
CB_WWW_PAGE = "https://www.carbonbrief.org/interactive-who-wants-what-at-the-cop31-climate-change-summit"
CB_CREDIT = "Carbon Brief"

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_texts (
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE,
  body TEXT, agenda_item TEXT, symbol TEXT, title TEXT, cb_type TEXT,
  published TEXT, pdf_url TEXT, cb_topic TEXT, dossier TEXT,
  pages INTEGER, brackets INTEGER, options INTEGER, note TEXT,
  first_seen TEXT, updated_at TEXT,
  doc_id INTEGER, status TEXT DEFAULT 'a_recuperer',
  attempts INTEGER DEFAULT 0, last_attempt TEXT, last_error TEXT,
  notified INTEGER DEFAULT 0, diff_notified INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS live_texts_dossier ON live_texts(dossier, published);
CREATE TABLE IF NOT EXISTS dossier_status (cb_topic TEXT PRIMARY KEY, dossier TEXT, status TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE,
  dossier TEXT, actor TEXT, issue TEXT, stance TEXT, summary TEXT, quote TEXT,
  source_kind TEXT, source_label TEXT, source_url TEXT, doc_id INTEGER,
  said_on TEXT, created_at TEXT, access TEXT DEFAULT 'public'
);
CREATE INDEX IF NOT EXISTS positions_dossier ON positions(dossier, actor);
CREATE TABLE IF NOT EXISTS extraction_log (doc_id INTEGER PRIMARY KEY, at TEXT, model TEXT, n INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS social_posts (
  uri TEXT PRIMARY KEY, handle TEXT, author TEXT, level TEXT, text TEXT, url TEXT,
  created_at TEXT, fetched_at TEXT, links TEXT, extracted INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS social_posts_created ON social_posts(created_at);
CREATE TABLE IF NOT EXISTS field_notes (
  id INTEGER PRIMARY KEY, dossier TEXT, user_id INTEGER, text TEXT, created_at TEXT, doc_id INTEGER, via TEXT
);
CREATE TABLE IF NOT EXISTS briefs (
  id INTEGER PRIMARY KEY, dossier TEXT, created_at TEXT, period_start TEXT, body TEXT, sources TEXT, model TEXT
);
CREATE TABLE IF NOT EXISTS dossier_follows (user_id INTEGER, dossier TEXT, created_at TEXT, PRIMARY KEY(user_id, dossier));
CREATE TABLE IF NOT EXISTS llm_usage (
  id INTEGER PRIMARY KEY, at TEXT, model TEXT, purpose TEXT, input_tokens INTEGER, output_tokens INTEGER, cost REAL
);
"""

_ready: set[str] = set()


def ensure_schema(conn) -> None:
    key = str(config.DB_PATH)
    if key in _ready:
        return
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(live_texts)")}
    if "diff_notified" not in cols:
        conn.execute("ALTER TABLE live_texts ADD COLUMN diff_notified INTEGER DEFAULT 0")
        conn.execute("UPDATE live_texts SET diff_notified=1")  # ne pas ré-alerter l'historique déjà traité
    conn.commit()
    _ready.add(key)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- dossiers -----------------------------------------------------------------------

_DOSSIERS: list[dict] | None = None


def dossiers() -> list[dict]:
    global _DOSSIERS
    if _DOSSIERS is None:
        path = config.CATALOG_DIR / "dossiers.yaml"
        _DOSSIERS = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else []
    return _DOSSIERS


def dossier(slug: str) -> dict | None:
    return next((d for d in dossiers() if d["slug"] == slug), None)


def dossier_for_cb_topic(topic: str) -> str | None:
    t = (topic or "").strip().lower()
    if not t:
        return None
    for d in dossiers():
        if any(t == c.lower() for c in d.get("cb", [])):
            return d["slug"]
    return "organisation" if "agenda" in t or "administrative" in t else None


def dossier_from_alias(word: str) -> str | None:
    w = (word or "").strip().lower().rstrip(":,;")
    for d in dossiers():
        if w == d["slug"] or w in [a.lower() for a in d.get("alias", [])]:
            return d["slug"]
    return None


def guess_dossier(text: str) -> str | None:
    """Dossier le plus probable d'un texte libre (alias, puis mots de la requête du dossier)."""
    low = (text or "").lower()
    best, score = None, 0
    for d in dossiers():
        s = sum(3 for a in d.get("alias", []) if re.search(rf"(?<![\w]){re.escape(a.lower())}(?![\w])", low))
        s += sum(1 for k in (d.get("requete") or "").split(",") if k.strip() and k.strip().lower() in low)
        if s > score:
            best, score = d["slug"], s
    return best


# --- Carbon Brief : feuilles publiques -----------------------------------------------

def _sheet(client: fetch.Client, sheet_id: str, sheet: str) -> list[list[str]]:
    res = client.get(CB_SHEET.format(id=sheet_id, sheet=sheet))
    if not res.ok:
        raise RuntimeError(f"feuille {sheet} : {res.status} {res.error or ''}")
    return list(csv.reader(io.StringIO(res.content.decode("utf-8", errors="replace"))))


def _cb_date(date: str, time: str) -> str | None:
    """Date Carbon Brief (mm/jj/aaaa, parfois jj/mm/aaaa) + heure → ISO."""
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", date or "")
    if not m:
        return None
    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    month, day = (a, b) if a <= 12 else (b, a)
    t = re.match(r"^\s*(\d{1,2}):(\d{2})", time or "")
    hh, mm = (int(t.group(1)), int(t.group(2))) if t else (0, 0)
    try:
        return datetime(y, month, day, hh, mm).strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return None


def _int(v: str) -> int | None:
    v = (v or "").strip().replace(",", "")
    return int(v) if v.isdigit() else None


def sync_texts(conn, client: fetch.Client) -> list[int]:
    """Lit l'index Carbon Brief ; renvoie les ids des textes nouvellement annoncés."""
    ensure_schema(conn)
    rows = _sheet(client, CB_TEXTS_ID, "Texts")
    if not rows:
        return []
    head = [h.strip().lower() for h in rows[0]]
    col = {name: head.index(name) for name in head if name}

    def g(r, name):
        i = col.get(name)
        return r[i].strip() if i is not None and i < len(r) else ""

    new_ids = []
    for r in rows[1:]:
        symbol = g(r, "symbol")
        pdf = g(r, "pdf link")
        key = symbol or pdf
        if not key:
            continue
        topic = g(r, "topic")
        vals = dict(
            body=g(r, "body"), agenda_item=g(r, "agenda item"), symbol=symbol or None, title=g(r, "title"),
            cb_type=g(r, "document type"), published=_cb_date(g(r, "date"), g(r, "time")), pdf_url=pdf or None,
            cb_topic=topic, dossier=dossier_for_cb_topic(topic), pages=_int(g(r, "pages")),
            brackets=_int(g(r, "brackets")), options=_int(g(r, "options")), note=g(r, "note") or None,
        )
        old = conn.execute("SELECT id FROM live_texts WHERE key=?", (key,)).fetchone()
        if old:
            sets = ", ".join(f"{k}=?" for k in vals)
            conn.execute(f"UPDATE live_texts SET {sets}, updated_at=? WHERE id=?", (*vals.values(), now_iso(), old["id"]))
        else:
            cur = conn.execute(
                f"INSERT INTO live_texts(key,{','.join(vals)},first_seen,updated_at) VALUES(?{',?' * len(vals)},?,?)",
                (key, *vals.values(), now_iso(), now_iso()),
            )
            new_ids.append(cur.lastrowid)
    conn.commit()
    return new_ids


def sync_status(conn, client: fetch.Client) -> int:
    ensure_schema(conn)
    rows = _sheet(client, CB_TEXTS_ID, "agenda-status")
    n = 0
    for r in rows[1:]:
        if len(r) >= 2 and r[0].strip():
            conn.execute(
                """INSERT INTO dossier_status(cb_topic,dossier,status,updated_at) VALUES(?,?,?,?)
                   ON CONFLICT(cb_topic) DO UPDATE SET dossier=excluded.dossier, status=excluded.status,
                   updated_at=excluded.updated_at""",
                (r[0].strip(), dossier_for_cb_topic(r[0]), r[1].strip(), now_iso()),
            )
            n += 1
    conn.commit()
    return n


STANCES = {"priority": "priorité", "supports": "soutient", "does not support": "ne soutient pas",
           "opposes": "s'oppose", "neutral": "neutre", "red line": "ligne rouge"}


def sync_positions_cb(conn, client: fetch.Client) -> int:
    """Positions « Who wants what » de Carbon Brief (mise à jour à chaque passage)."""
    ensure_schema(conn)
    rows = _sheet(client, CB_WWW_ID, "Sheet1")
    n = 0
    seen = set()
    for r in rows[1:]:
        if len(r) < 4 or not r[0].strip():
            continue
        actor, topic, issue, stance = (x.strip() for x in r[:4])
        source = r[4].strip() if len(r) > 4 else ""
        key = f"cb|{actor}|{topic}|{issue}"
        seen.add(key)
        conn.execute(
            """INSERT INTO positions(key,dossier,actor,issue,stance,summary,quote,source_kind,source_label,source_url,
                 said_on,created_at,access) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'public')
               ON CONFLICT(key) DO UPDATE SET stance=excluded.stance, source_url=excluded.source_url,
                 dossier=excluded.dossier""",
            (key, dossier_for_cb_topic(topic) or _www_topic(topic), actor, issue,
             STANCES.get(stance.lower(), stance.lower()), None, None, "carbonbrief",
             f"{CB_CREDIT} — Who wants what (COP31)", source or CB_WWW_PAGE, None, now_iso()),
        )
        n += 1
    conn.commit()
    return n


def _www_topic(topic: str) -> str | None:
    t = topic.lower()
    table = {"adaptation": "adaptation", "finance (art 2.1c)": "flux-financiers", "finance (art 9)": "finance",
             "finance": "finance", "global stocktake": "bilan-ndc", "just transition": "transition-juste",
             "mitigation work programme": "attenuation", "oceans": "science-oceans", "ndcs": "bilan-ndc",
             "trade": "riposte-commerce", "cop reform": "organisation", "action agenda": "organisation"}
    return table.get(t)


# --- récupération des textes annoncés -------------------------------------------------

RETRY_MINUTES = 30
MAX_ATTEMPTS = 8


def _session(body: str) -> str | None:
    m = re.match(r"^\s*(COP|CMA|CMP|SBI|SBSTA)\s*(\d+)", body or "", re.I)
    return f"{m.group(1).upper()}{m.group(2)}" if m else None


def _meta(t) -> dict[str, Any]:
    doc_type = {"Draft texts": "projet_texte", "Informal notes": "projet_texte", "Draft conclusions": "projet_texte",
                "Agendas and annotations": "ordre_du_jour", "Summary reports": "compte_rendu"}.get(t["cb_type"], "autre")
    if (t["title"] or "").lower().startswith(("draft", "proposal by the president", "projet")):
        doc_type = "projet_texte"
    return {"title": f"{t['symbol'] + ' — ' if t['symbol'] else ''}{t['title']}"[:300], "symbol": t["symbol"],
            "level": "2", "org": "UNFCCC", "doc_type": doc_type, "session": _session(t["body"]),
            "topics": [t["dossier"]] if t["dossier"] else None}


def link_existing(conn) -> int:
    """Rattache aux textes annoncés les documents déjà présents (collecte, ODS, dépôt manuel)."""
    ensure_schema(conn)
    n = 0
    for t in conn.execute("SELECT * FROM live_texts WHERE doc_id IS NULL").fetchall():
        row = None
        if t["symbol"]:
            row = conn.execute(
                "SELECT id FROM documents WHERE symbol=? AND status IN ('indexed','empty') ORDER BY (lang='en') DESC, id DESC LIMIT 1",
                (t["symbol"],)).fetchone()
        if not row and t["pdf_url"]:
            stem = Path(urlparse(t["pdf_url"]).path).stem
            row = conn.execute(
                """SELECT id FROM documents WHERE url=? OR (length(?)>4 AND (title=? OR title LIKE ? OR note LIKE ?))
                   ORDER BY id DESC LIMIT 1""",
                (t["pdf_url"], stem, stem, f"%{stem}%", f"%{stem}%")).fetchone()
        if row:
            conn.execute("UPDATE live_texts SET doc_id=?, status='recupere', updated_at=? WHERE id=?",
                         (row["id"], now_iso(), t["id"]))
            n += 1
    conn.commit()
    return n


def retrieve_pending(conn, client: fetch.Client, limit: int = 20) -> dict[str, int]:
    """Une tentative polie par texte et par période : adresse du PDF, puis ODS par la cote."""
    ensure_schema(conn)
    counts = {"recupere": 0, "bloque": 0, "absent": 0}
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=RETRY_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """SELECT * FROM live_texts WHERE doc_id IS NULL AND attempts < ?
           AND (last_attempt IS NULL OR last_attempt < ?) ORDER BY published DESC LIMIT ?""",
        (MAX_ATTEMPTS, cutoff, limit)).fetchall()
    for t in rows:
        error = None
        doc_id = None
        urls = []
        if t["pdf_url"]:
            urls.append(t["pdf_url"])
        if t["symbol"] and t["symbol"].startswith("FCCC/"):
            urls.append(fetch.ODS_URL.format(symbol=t["symbol"], lang="en"))
        for url in urls:
            res = client.get(url)
            if not res.ok:
                error = f"{res.status} {res.error or ''}".strip()
                continue
            if fetch.is_antibot(res):
                error = "anti-bot (non contourné)"
                counts["bloque"] += 1
                continue
            if "documents.un.org/error" in res.url or not res.content.startswith(b"%PDF"):
                error = "absent de l'ODS" if "documents.un.org" in url else "pas un PDF"
                continue
            doc_id, _new, _st = ingest.ingest_bytes(conn, res.content, url=t["pdf_url"] or res.url, mime=res.mime,
                                                    meta=_meta(t), source_id="live-texts")
            break
        if doc_id:
            conn.execute("UPDATE live_texts SET doc_id=?, status='recupere', attempts=attempts+1, last_attempt=?, "
                         "last_error=NULL, updated_at=? WHERE id=?", (doc_id, now_iso(), now_iso(), t["id"]))
            counts["recupere"] += 1
        else:
            conn.execute("UPDATE live_texts SET attempts=attempts+1, last_attempt=?, last_error=? WHERE id=?",
                         (now_iso(), error, t["id"]))
            counts["absent"] += 1
        conn.commit()  # jamais de verrou d'écriture pendant un téléchargement
    return counts


def attach_upload(conn, text_id: int, doc_id: int) -> None:
    """Dépôt manuel d'un texte annoncé (page Textes)."""
    ensure_schema(conn)
    t = conn.execute("SELECT * FROM live_texts WHERE id=?", (text_id,)).fetchone()
    if not t:
        return
    m = _meta(t)
    conn.execute(
        """UPDATE documents SET title=COALESCE(NULLIF(?,''),title), symbol=COALESCE(symbol,?), level=COALESCE(level,'2'),
             doc_type=COALESCE(doc_type,?), session=COALESCE(session,?), url=COALESCE(url,?) WHERE id=?""",
        (m["title"], m["symbol"], m["doc_type"], m["session"], t["pdf_url"], doc_id))
    conn.execute("UPDATE live_texts SET doc_id=?, status='recupere', updated_at=? WHERE id=?", (doc_id, now_iso(), text_id))
    conn.commit()


def texts(conn, dossier_slug: str | None = None, pending_only: bool = False, limit: int = 200) -> list[dict]:
    ensure_schema(conn)
    where, params = ["1=1"], []
    if dossier_slug:
        where.append("dossier=?")
        params.append(dossier_slug)
    if pending_only:
        where.append("doc_id IS NULL")
    rows = conn.execute(
        f"""SELECT * FROM live_texts WHERE {' AND '.join(where)}
            ORDER BY COALESCE(published, first_seen) DESC, id DESC LIMIT ?""", (*params, limit)).fetchall()
    return [dict(r) for r in rows]


# --- notes de terrain ---------------------------------------------------------------

def add_note(conn, user: dict, text: str, dossier_slug: str | None = None, via: str = "web") -> tuple[int, str | None]:
    """Note de terrain d'un contributeur (restreinte) : versée au corpus, rattachée à un dossier."""
    ensure_schema(conn)
    text = text.strip()
    if not dossier_slug:
        first, _, rest = text.partition(" ")
        dossier_slug = dossier_from_alias(first)
        if dossier_slug and rest.strip():
            text = rest.strip()
        else:
            dossier_slug = guess_dossier(text)
    d = dossier(dossier_slug) if dossier_slug else None
    stamp = datetime.now(timezone.utc).astimezone().strftime("%d/%m %H:%M")
    title = f"Note de terrain — {d['nom'] if d else 'sans dossier'} — {user.get('name') or user.get('email')} — {stamp}"
    body = f"{title}\n\n{text}\n"
    doc_id, _new, _st = ingest.ingest_bytes(
        conn, body.encode("utf-8"), url=None, mime="text/plain",
        meta={"title": title, "doc_type": "note_interne", "org": "Note de terrain",
              "topics": [dossier_slug] if dossier_slug else None},
        source_id="note-terrain", access="restreint", uploaded_by=user.get("id"), filename="note.txt")
    cur = conn.execute("INSERT INTO field_notes(dossier,user_id,text,created_at,doc_id,via) VALUES(?,?,?,?,?,?)",
                       (dossier_slug, user.get("id"), text, now_iso(), doc_id, via))
    conn.commit()
    return cur.lastrowid, dossier_slug


def notes(conn, dossier_slug: str | None = None, since: str | None = None, limit: int = 50) -> list[dict]:
    ensure_schema(conn)
    where, params = ["1=1"], []
    if dossier_slug:
        where.append("n.dossier=?")
        params.append(dossier_slug)
    if since:
        where.append("n.created_at>=?")
        params.append(since)
    rows = conn.execute(
        f"""SELECT n.*, u.name AS auteur, u.email FROM field_notes n LEFT JOIN users u ON u.id=n.user_id
            WHERE {' AND '.join(where)} ORDER BY n.id DESC LIMIT ?""", (*params, limit)).fetchall()
    return [dict(r) for r in rows]


# --- suivi de dossiers ---------------------------------------------------------------

def follow(conn, user_id: int, slug: str, on: bool = True) -> None:
    ensure_schema(conn)
    if on:
        conn.execute("INSERT OR IGNORE INTO dossier_follows(user_id,dossier,created_at) VALUES(?,?,?)",
                     (user_id, slug, now_iso()))
    else:
        conn.execute("DELETE FROM dossier_follows WHERE user_id=? AND dossier=?", (user_id, slug))
    conn.commit()


def followed(conn, user_id: int) -> set[str]:
    ensure_schema(conn)
    return {r[0] for r in conn.execute("SELECT dossier FROM dossier_follows WHERE user_id=?", (user_id,))}


def followers(conn, slug: str | None) -> list[dict]:
    """Abonnés d'un dossier ayant lié Telegram (slug None : abonnés à au moins un dossier)."""
    ensure_schema(conn)
    if slug:
        rows = conn.execute(
            """SELECT u.* FROM users u JOIN dossier_follows f ON f.user_id=u.id
               WHERE f.dossier=? AND u.active=1 AND u.telegram_chat_id IS NOT NULL""", (slug,)).fetchall()
    else:
        rows = conn.execute(
            """SELECT DISTINCT u.* FROM users u JOIN dossier_follows f ON f.user_id=u.id
               WHERE u.active=1 AND u.telegram_chat_id IS NOT NULL""").fetchall()
    return [dict(r) for r in rows]


def notify_new_texts(conn, send) -> int:
    """Alerte Telegram aux abonnés du dossier pour chaque texte annoncé pas encore signalé."""
    ensure_schema(conn)
    n = 0
    for t in conn.execute("SELECT * FROM live_texts WHERE notified=0 ORDER BY id").fetchall():
        d = dossier(t["dossier"]) if t["dossier"] else None
        link = f"{config.PUBLIC_BASE}/doc/{t['doc_id']}" if t["doc_id"] else (t["pdf_url"] or "")
        delta = since_previous(conn, t["doc_id"]) if t["doc_id"] else None

        def message(u: dict) -> str:
            from . import i18n

            en = i18n.default_for(u) == "en"
            if t["doc_id"]:
                state = "✅ retrieved" if en else "✅ récupéré"
            elif u.get("is_admin") or u.get("can_restricted"):
                state = "⏳ à récupérer — ouvrez le lien puis transférez le PDF au bot"
            else:
                state = "⏳ not in the corpus yet — opens on unfccc.int" if en else "⏳ pas encore dans le corpus — s'ouvre sur unfccc.int"
            counts = " · ".join(x for x in (
                f"{t['pages']} p." if t["pages"] else "",
                f"{t['brackets']} {'brackets' if en else 'crochets'}" if t["brackets"] is not None else "",
                f"{t['options']} options" if t["options"] else "") if x)
            name = ((d.get("nom_en") if en else None) or d["nom"]) if d else (t["cb_topic"] or "Texte")
            msg = (f"🆕 <b>{html.escape(name)}</b>\n"
                   f"{html.escape(t['symbol'] or '')} {html.escape((t['title'] or '')[:200])}\n"
                   f"{html.escape(t['body'] or '')} · {'item' if en else 'point'} {html.escape(t['agenda_item'] or '?')} · {html.escape(counts)}\n"
                   f"{state}\n{link}\n<i>Index : {CB_CREDIT}</i>")
            if delta:
                msg += f"\n📊 {html.escape(diff.delta_line(delta))}\n{config.PUBLIC_BASE}/diff/{t['doc_id']}"
            return msg

        # diff_notified : marqué dès qu'on a le texte, pour que notify_retrieved ne ré-alerte pas ce texte.
        conn.execute("UPDATE live_texts SET notified=1, diff_notified=? WHERE id=?",
                     (1 if t["doc_id"] else 0, t["id"]))
        conn.commit()
        for u in followers(conn, t["dossier"]):
            send(u["telegram_chat_id"], message(u))
        n += 1
    return n


def since_previous(conn, doc_id: int) -> dict | None:
    """Diff d'un texte avec la version précédente de sa lignée (délègue à diff, pour mock en test)."""
    return diff.since_previous(conn, doc_id)


def notify_retrieved(conn, send) -> int:
    """Seconde alerte quand un texte annoncé est enfin récupéré : analyse des crochets vs version précédente.

    Couvre les textes « à récupérer » au moment de l'annonce (anti-bot, dépôt manuel différé) : l'alerte d'arrivée
    porte le delta de crochets et d'options. Un texte récupéré dès l'annonce a déjà son delta et n'est pas repris.
    """
    ensure_schema(conn)
    n = 0
    for t in conn.execute(
        "SELECT * FROM live_texts WHERE notified=1 AND diff_notified=0 AND doc_id IS NOT NULL ORDER BY id"
    ).fetchall():
        delta = since_previous(conn, t["doc_id"])
        conn.execute("UPDATE live_texts SET diff_notified=1 WHERE id=?", (t["id"],))
        conn.commit()
        if not delta:
            continue  # première version de la lignée : l'annonce suffisait, rien à comparer
        d = dossier(t["dossier"]) if t["dossier"] else None
        msg = (f"📊 <b>{html.escape(d['nom'] if d else (t['cb_topic'] or 'Texte'))}</b> — texte récupéré\n"
               f"{html.escape(t['symbol'] or '')} {html.escape((t['title'] or '')[:200])}\n"
               f"{html.escape(diff.delta_line(delta))}\n{config.PUBLIC_BASE}/diff/{t['doc_id']}")
        for u in followers(conn, t["dossier"]):
            send(u["telegram_chat_id"], msg)
        n += 1
    return n


# --- Bluesky -------------------------------------------------------------------------

BSKY = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?actor={actor}&limit={limit}&filter=posts_no_replies"


def bluesky_accounts() -> list[dict]:
    path = config.CATALOG_DIR / "bluesky.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else []


def sync_bluesky(conn, client: fetch.Client, limit: int = 30) -> int:
    import json

    ensure_schema(conn)
    n = 0
    for acc in bluesky_accounts():
        res = client.get(BSKY.format(actor=acc["handle"], limit=limit))
        if not res.ok:
            continue
        try:
            feed = json.loads(res.content).get("feed", [])
        except ValueError:
            continue
        for item in feed:
            post = item.get("post") or {}
            rec = post.get("record") or {}
            uri = post.get("uri")
            if not uri or (post.get("author") or {}).get("handle") != acc["handle"]:
                continue  # republication d'un autre compte : ignorée
            rkey = uri.rsplit("/", 1)[-1]
            links = [f.get("uri") for fac in rec.get("facets", []) or [] for f in fac.get("features", [])
                     if f.get("uri")]
            ext = ((post.get("embed") or {}).get("external") or {}).get("uri")
            if ext:
                links.append(ext)
            cur = conn.execute(
                """INSERT OR IGNORE INTO social_posts(uri,handle,author,level,text,url,created_at,fetched_at,links)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (uri, acc["handle"], acc.get("nom"), str(acc.get("niveau", "5")), rec.get("text", ""),
                 f"https://bsky.app/profile/{acc['handle']}/post/{rkey}", (rec.get("createdAt") or "")[:19] + "Z",
                 now_iso(), db.jdump(links)))
            n += cur.rowcount
        conn.commit()
    return n


def social(conn, query_words: list[str] | None = None, since: str | None = None, limit: int = 40) -> list[dict]:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM social_posts WHERE (? IS NULL OR created_at>=?) ORDER BY created_at DESC LIMIT 2000",
        (since, since)).fetchall()
    out = []
    words = [w.strip().lower() for w in (query_words or []) if len(w.strip()) > 2]
    for r in rows:
        low = (r["text"] or "").lower()
        if words and not any(w in low for w in words):
            continue
        out.append(dict(r))
        if len(out) >= limit:
            break
    return out


def dossier_words(d: dict) -> list[str]:
    words = [k.strip() for k in (d.get("requete") or "").split(",") if k.strip()]
    words += [a for a in d.get("alias", []) if len(a) > 3]
    return words


# --- vue d'ensemble -------------------------------------------------------------------

def status_by_dossier(conn) -> dict[str, list[dict]]:
    ensure_schema(conn)
    out: dict[str, list[dict]] = {}
    for r in conn.execute("SELECT * FROM dossier_status ORDER BY cb_topic"):
        out.setdefault(r["dossier"] or "autres", []).append(dict(r))
    return out


def overview(conn, user: dict | None = None) -> list[dict]:
    """Tableau des dossiers : état, dernier texte, crochets, positions, notes, suivi."""
    ensure_schema(conn)
    status = status_by_dossier(conn)
    mine = followed(conn, user["id"]) if user else set()
    out = []
    for d in dossiers():
        s = d["slug"]
        last = conn.execute(
            "SELECT * FROM live_texts WHERE dossier=? ORDER BY COALESCE(published, first_seen) DESC LIMIT 1", (s,)).fetchone()
        out.append({
            **d,
            "etat": [x["status"] for x in status.get(s, [])],
            "nb_textes": conn.execute("SELECT COUNT(*) FROM live_texts WHERE dossier=?", (s,)).fetchone()[0],
            "a_recuperer": conn.execute("SELECT COUNT(*) FROM live_texts WHERE dossier=? AND doc_id IS NULL",
                                        (s,)).fetchone()[0],
            "dernier": dict(last) if last else None,
            "nb_positions": conn.execute("SELECT COUNT(*) FROM positions WHERE dossier=?", (s,)).fetchone()[0],
            "nb_notes": (conn.execute("SELECT COUNT(*) FROM field_notes WHERE dossier=?", (s,)).fetchone()[0]
                         if user and user.get("can_restricted") else 0),  # invités : rien sur les notes internes
            "suivi": s in mine,
        })
    return out


def bracket_series(conn, slug: str) -> list[dict]:
    """Évolution des crochets des projets de texte d'un dossier (pour la courbe)."""
    rows = conn.execute(
        """SELECT published, brackets, options, symbol, title FROM live_texts
           WHERE dossier=? AND brackets IS NOT NULL AND published IS NOT NULL ORDER BY published""", (slug,)).fetchall()
    return [dict(r) for r in rows]


def log(msg: str) -> None:
    print(f"[direct] {msg}", file=sys.stderr, flush=True)
