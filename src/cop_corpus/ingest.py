"""Enregistrement des documents (reçu, lignée de versions) et indexation."""

from __future__ import annotations

import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from . import catalog, config, db, extract, fetch

_write_lock = threading.Lock()  # sérialise les écritures SQLite lourdes dans un même processus

GENERIC_LINK_TEXT = {
    "download", "télécharger", "telecharger", "read more", "en savoir plus", "english", "français",
    "francais", "español", "espanol", "pdf", "here", "ici", "view", "voir", "document", "open",
    "download pdf", "download file", "full text", "texte intégral",
}


# --- lignées -------------------------------------------------------------------

VERSION_NOISE = re.compile(
    r"\b(v(ersion)?\s*\d+|rev(\.|ision)?\s*\d*|draft|projet|unedited|advance|adv|"
    r"\d{1,2}[:h]\d{2}|\d{1,2}\s+(nov|november|novembre)\w*|\d{4}-\d{2}-\d{2}|"
    r"\d{1,2}/\d{1,2}(/\d{2,4})?)\b",
    re.I,
)


def normalize_title(title: str) -> str:
    t = VERSION_NOISE.sub(" ", title.lower())
    t = re.sub(r"[^a-z0-9àâçéèêëîïôûùüÿœ]+", " ", t)
    return " ".join(t.split())[:120]


def lineage_key(url: str | None, symbol: str | None, title: str | None, doc_type: str | None,
                lang: str | None = None) -> str:
    if symbol:
        base = re.sub(r"/(Rev|Corr)\.\d+$", "", symbol)
        return f"sym:{base}|{lang or '?'}"
    if doc_type == "projet_texte" and title:
        return f"title:{normalize_title(title)}"
    if url:
        return f"url:{url}"
    return f"title:{normalize_title(title or '')}"


def _title_from_url(url: str) -> str:
    name = unquote(Path(urlparse(url).path).name)
    name = re.sub(r"\.(pdf|docx?|html?)$", "", name, flags=re.I)
    return re.sub(r"[_\-]+", " ", name).strip() or url


# --- enregistrement -------------------------------------------------------------

def register(
    conn: sqlite3.Connection,
    content: bytes,
    *,
    url: str | None,
    mime: str = "",
    meta: dict[str, Any] | None = None,
    source_id: str | None = None,
    access: str = "public",
    uploaded_by: int | None = None,
    filename: str | None = None,
    note: str | None = None,
) -> tuple[int, bool]:
    """Stocke l'original et crée le document. Renvoie (doc_id, nouveau ?)."""
    meta = dict(meta or {})
    ext = fetch.guess_ext(filename or url or "", mime, content)
    if filename and Path(filename).suffix:
        ext = Path(filename).suffix.lower()
    digest, path = fetch.store_raw(content, ext)
    row = conn.execute("SELECT id FROM documents WHERE sha256=?", (digest,)).fetchone()
    if row:
        return row["id"], False
    title = meta.get("title") or (Path(filename).stem if filename else None)
    with _write_lock:
        cur = conn.execute(
            """INSERT INTO documents(sha256,url,source_id,title,level,org,doc_type,session,symbol,
                 parties,topics,lang,published,fetched_at,mime,raw_path,size,access,status,uploaded_by,note)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
            (
                digest, url, source_id, title, meta.get("level"), meta.get("org"), meta.get("doc_type"),
                meta.get("session"), meta.get("symbol"), db.jdump(meta.get("parties")),
                db.jdump(meta.get("topics")), meta.get("lang"), str(meta["published"]) if meta.get("published") else None,
                db.now(), mime, str(path.relative_to(config.DATA_DIR)), len(content), access, uploaded_by, note,
            ),
        )
        conn.commit()
    return cur.lastrowid, True


def index_document(conn: sqlite3.Connection, doc_id: int) -> str:
    """Extrait, découpe et indexe un document. Renvoie le statut final."""
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    path = config.DATA_DIR / doc["raw_path"]
    if doc["doc_type"] == "donnees" or path.suffix in (".csv", ".json", ".xlsx", ".zip", ".bin"):
        conn.execute("UPDATE documents SET status='data' WHERE id=?", (doc_id,))
        conn.commit()
        return "data"
    try:
        found_title, pages = extract.extract(path, doc["mime"] or "")
    except Exception as exc:
        conn.execute("UPDATE documents SET status='error', error=? WHERE id=?", (f"{type(exc).__name__}: {exc}"[:400], doc_id))
        conn.commit()
        return "error"
    chunks = extract.chunk(pages)
    symbol = doc["symbol"] or extract.detect_symbol(doc["url"] or "", pages)
    title = doc["title"]
    if title and (extract.FILE_ID.search(title.strip()) or title.strip() == symbol or title.startswith(f"{symbol} — ")):
        title = None  # titre non informatif (identifiant de fichier) : on le recalcule
    title = title or extract.guess_title(pages, found_title) or _title_from_url(doc["url"] or "")
    decisions = list(dict.fromkeys(c.para.split(" §")[0] for c in chunks if c.para and "/" in c.para))
    if decisions and "décisions" not in title:
        title = f"{title} (décisions {', '.join(decisions[:6])}{'…' if len(decisions) > 6 else ''})"
    if symbol and symbol not in title:
        title = f"{symbol} — {title}"
    lang = doc["lang"] or extract.detect_lang(" ".join(c.text for c in chunks[:5]))
    lkey = lineage_key(doc["url"], symbol, title, doc["doc_type"], lang)
    with _write_lock:
        prev = conn.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE lineage_key=? AND id<>?", (lkey, doc_id)
        ).fetchone()["n"]
        old = [r["id"] for r in conn.execute("SELECT id FROM passages WHERE doc_id=?", (doc_id,))]
        for pid in old:
            conn.execute("DELETE FROM passages_fts WHERE rowid=?", (pid,))
            conn.execute("DELETE FROM vectors WHERE passage_id=?", (pid,))
        conn.execute("DELETE FROM passages WHERE doc_id=?", (doc_id,))
        for c in chunks:
            cur = conn.execute(
                "INSERT INTO passages(doc_id,seq,page,para,text) VALUES(?,?,?,?,?)",
                (doc_id, c.seq, c.page, c.para, c.text),
            )
            conn.execute("INSERT INTO passages_fts(rowid,text,title) VALUES(?,?,?)", (cur.lastrowid, c.text, title))
        status = "indexed" if chunks else "empty"
        conn.execute(
            """UPDATE documents SET status=?, title=?, symbol=?, lang=?, lineage_key=?, version=?,
                 n_pages=?, n_passages=?, error=NULL WHERE id=?""",
            (status, title, symbol, lang, lkey, prev + 1, len(pages), len(chunks), doc_id),
        )
        conn.commit()
    return status


def ingest_bytes(conn, content: bytes, **kwargs) -> tuple[int, bool, str]:
    doc_id, new = register(conn, content, **kwargs)
    status = index_document(conn, doc_id) if new else conn.execute(
        "SELECT status FROM documents WHERE id=?", (doc_id,)
    ).fetchone()["status"]
    return doc_id, new, status


# --- collecte d'une source ---------------------------------------------------------

def _url_seen(conn, url: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM url_state WHERE url=?", (url,)).fetchone()


def _touch_url(conn, url: str, source_id: str, res: fetch.FetchResult | None, sha: str | None) -> None:
    with _write_lock:
        conn.execute(
            """INSERT INTO url_state(url,source_id,etag,last_modified,last_sha,last_status,last_checked,first_seen)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET etag=COALESCE(excluded.etag,etag),
                 last_modified=COALESCE(excluded.last_modified,last_modified),
                 last_sha=COALESCE(excluded.last_sha,last_sha), last_status=excluded.last_status,
                 last_checked=excluded.last_checked""",
            (url, source_id, res.etag if res else None, res.last_modified if res else None, sha,
             res.status if res else None, db.now(), db.now()),
        )
        conn.commit()


def fetch_one(conn, client: fetch.Client, url: str, source: catalog.Source, meta: dict, recheck: bool) -> str:
    state = _url_seen(conn, url)
    if state and state["last_status"] == 200 and not recheck:
        return "known"
    res = client.get(url, etag=state["etag"] if state else None, last_modified=state["last_modified"] if state else None)
    if res.status == 304:
        _touch_url(conn, url, source.id, res, None)
        return "unchanged"
    if res.ok and res.url.rstrip("/").endswith("documents.un.org/error"):
        res.status = 404  # cote absente de l'ODS
        _touch_url(conn, url, source.id, res, None)
        return "absent"
    if res.ok and fetch.is_antibot(res):
        res.status = 403
        _touch_url(conn, url, source.id, res, None)
        db.log(conn, source.id, url, "antibot", "page de challenge anti-bot : non contournée")
        return "antibot"
    if not res.ok:
        _touch_url(conn, url, source.id, res, None)
        db.log(conn, source.id, url, "error", f"{res.status} {res.error or ''}")
        return "error"
    access = "restreint" if source.raw.get("access") == "restreint" else "public"
    expanded = _expand_json_html(conn, res, source, meta, access)
    if expanded is not None:
        _touch_url(conn, url, source.id, res, fetch.sha256(res.content))
        return expanded
    doc_id, new, status = ingest_bytes(
        conn, res.content, url=res.url, mime=res.mime, meta=meta, source_id=source.id, access=access
    )
    _touch_url(conn, url, source.id, res, fetch.sha256(res.content))
    if new:
        db.log(conn, source.id, url, "new", f"doc {doc_id} {status}")
        return "new"
    return "duplicate"


LANGS = {"EN": "en", "FR": "fr", "ES": "es", "AR": "ar", "RU": "ru", "ZH": "zh", "PT": "pt"}


def _expand_json_html(conn, res: fetch.FetchResult, source: catalog.Source, meta: dict, access: str) -> str | None:
    """Réponses JSON contenant une liste de documents HTML (API Climate Watch des NDC) :
    un document par élément. Renvoie None si la réponse n'a pas cette forme."""
    if "json" not in res.mime and not res.content[:1] in (b"[", b"{"):
        return None
    import json

    try:
        data = json.loads(res.content)
    except ValueError:
        return None
    items = data if isinstance(data, list) else data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict) or "html" not in items[0]:
        return None
    new_count = 0
    for item in items:
        html = item.get("html")
        if not html:
            continue
        loc = item.get("location") or {}
        country = loc.get("name") or ""
        dtype = (item.get("document_type") or "ndc").replace("_", " ")
        m = dict(meta)
        m["title"] = f"NDC — {country} — {dtype} ({item.get('language', '')})".strip()
        m["doc_type"] = meta.get("doc_type") or "ndc"
        m["lang"] = LANGS.get(str(item.get("language", "")).upper()) or meta.get("lang")
        if loc.get("iso_code3"):
            m["parties"] = [loc["iso_code3"]]
        _doc_id, new, _status = ingest_bytes(
            conn, html.encode("utf-8"), url=f"{res.url}#{item.get('id')}", mime="text/html", meta=m,
            source_id=source.id, access=access, filename=f"ndc-{item.get('id')}.html",
        )
        new_count += int(new)
    db.log(conn, source.id, res.url, "new", f"{new_count} documents depuis JSON")
    return "new" if new_count else "duplicate"


def run_source(source: catalog.Source, client: fetch.Client, recheck: bool = False,
               progress: Callable[[str], None] | None = None) -> dict[str, int]:
    conn = db.get()
    counts: dict[str, int] = {}

    def bump(key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    def log(url: str, detail: str) -> None:
        db.log(conn, source.id, url, "error", detail)
        bump("error")

    try:
        if source.type == "doc":
            for url in source.urls:
                bump(fetch_one(conn, client, url, source, source.meta, recheck or source.watch_minutes > 0))
        elif source.type == "crawl":
            title_from = source.raw.get("title_from", "link_text")
            for link in fetch.crawl(client, source.raw, log=log):
                meta = dict(source.meta)
                if (title_from == "link_text" and link.text and len(link.text) > 6 and "title" not in meta
                        and link.text.lower() not in GENERIC_LINK_TEXT
                        and not GENERIC_TITLE.match(link.text)):
                    meta["title"] = link.text[:300]
                bump(fetch_one(conn, client, link.url, source, meta, recheck=False))
                if progress and sum(counts.values()) % 25 == 0:
                    progress(f"{source.id}: {counts}")
        elif source.type == "rss":
            for link in fetch.rss(client, source.raw, log=log):
                meta = dict(source.meta)
                meta["title"] = link.text[:300] or None
                if link.published:
                    meta["published"] = link.published
                bump(fetch_one(conn, client, link.url, source, meta, recheck=False))
        elif source.type == "ods":
            langs = source.raw.get("langs") or ["en"]
            stop_after = int(source.raw.get("stop_after_misses", 5))
            for _series, symbols in fetch.ods_symbols(source.raw):
                misses = 0
                for symbol in symbols:
                    meta = dict(source.meta)
                    meta["symbol"] = symbol
                    meta.pop("title", None)
                    outcome = None
                    for i, lang in enumerate(langs):
                        meta["lang"] = lang
                        url = fetch.ODS_URL.format(symbol=symbol, lang=lang)
                        o = fetch_one(conn, client, url, source, meta, recheck=False)
                        bump(o)
                        if i == 0:
                            outcome = o
                            if o in ("absent", "error", "antibot"):
                                break
                    misses = misses + 1 if outcome in ("absent", "error") else 0
                    if misses >= stop_after:
                        break
        else:
            bump("unknown_type")
    except Exception as exc:
        import traceback, sys
        traceback.print_exc(file=sys.stderr)
        try:
            db.log(conn, source.id, None, "crash", f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        bump("crash")
    with _write_lock:
        conn.execute(
            """INSERT INTO source_state(source_id,last_run,last_result) VALUES(?,?,?)
               ON CONFLICT(source_id) DO UPDATE SET last_run=excluded.last_run, last_result=excluded.last_result""",
            (source.id, db.now(), str(counts)),
        )
        conn.commit()
    return counts


def run_many(sources: list[catalog.Source], workers: int = 6, recheck: bool = False,
             progress: Callable[[str], None] | None = None) -> dict[str, dict[str, int]]:
    client = fetch.Client()
    results: dict[str, dict[str, int]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_source, s, client, recheck, progress): s for s in sources}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                results[s.id] = fut.result()
            except Exception as exc:
                results[s.id] = {"crash": 1, "detail": str(exc)}  # type: ignore[dict-item]
            if progress:
                progress(f"✓ {s.id}: {results[s.id]}")
    return results


def reindex_pending(conn) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in conn.execute("SELECT id FROM documents WHERE status='pending'").fetchall():
        st = index_document(conn, row["id"])
        counts[st] = counts.get(st, 0) + 1
    if counts:
        fix_titles(conn)
    return counts


GENERIC_TITLE = re.compile(r"^(t[ée]l[ée]charger|download|lire|read)\b", re.I)
STRUCT_TITLE = re.compile(r"^(chapitre|chapter|annexes?|annex|synth[èe]se|summary|r[ée]sum[ée]|press release|"
                          r"communiqu[ée] de presse|statement|d[ée]claration|news)\b", re.I)
LANG_LABEL = {"fr": "version française", "es": "versión española", "ar": "ar", "ru": "ru", "zh": "zh"}


def _set_title(conn, doc_id: int, title: str) -> None:
    conn.execute("UPDATE documents SET title=? WHERE id=?", (title, doc_id))
    for p in conn.execute("SELECT id, text FROM passages WHERE doc_id=?", (doc_id,)).fetchall():
        conn.execute("DELETE FROM passages_fts WHERE rowid=?", (p["id"],))
        conn.execute("INSERT INTO passages_fts(rowid,text,title) VALUES(?,?,?)", (p["id"], p["text"], title))


def fix_titles(conn) -> int:
    """Rend les titres lisibles : version française sans titre → titre de la version anglaise de même cote ;
    texte de lien générique (« Télécharger le rapport complet ») → titre lu dans le document."""
    n = 0
    rows = conn.execute(
        """SELECT d.id, d.symbol, d.title,
                  (SELECT e.title FROM documents e WHERE e.symbol=d.symbol AND e.lang='en' AND e.title<>e.symbol
                     AND e.title NOT LIKE e.symbol||' (déc%' ORDER BY e.id DESC LIMIT 1) AS en
           FROM documents d
           WHERE d.symbol IS NOT NULL AND (d.title=d.symbol OR d.title LIKE d.symbol||' (décisions%')"""
    ).fetchall()
    for r in rows:
        if not r["en"]:
            continue
        rest = r["en"][len(r["symbol"]) + 3:] if r["en"].startswith(r["symbol"] + " — ") else r["en"]
        rest = re.sub(r"\s*\(décisions [^)]*\)$", "", rest)
        suffix = r["title"][len(r["symbol"]):].strip()
        _set_title(conn, r["id"], f"{r['symbol']} — {rest}" + (f" {suffix}" if suffix else ""))
        n += 1
    # Même titre que la version anglaise : signaler la langue pour ne pas confondre les deux.
    for r in conn.execute(
        """SELECT d.id, d.title, d.lang FROM documents d
           WHERE d.symbol IS NOT NULL AND d.lang IS NOT NULL AND d.lang<>'en'
             AND EXISTS (SELECT 1 FROM documents e WHERE e.symbol=d.symbol AND e.lang='en' AND e.title=d.title)"""
    ).fetchall():
        _set_title(conn, r["id"], f"{r['title']} [{LANG_LABEL.get(r['lang'], r['lang'])}]")
        n += 1
    for r in conn.execute("SELECT id, title, org, url, published FROM documents WHERE length(title) < 120").fetchall():
        title = r["title"] or ""
        generic, struct = GENERIC_TITLE.match(title), STRUCT_TITLE.match(title)
        if not (generic or struct) or title.startswith(f"{r['org']}"):
            continue
        m = re.search(r"/(20\d\d)/", r["url"] or "") or re.match(r"(\d{4})", r["published"] or "")
        year = f" ({m.group(1)})" if m else ""
        org = r["org"] or "Document"
        if generic:
            first = conn.execute("SELECT text FROM passages WHERE doc_id=? ORDER BY seq LIMIT 3", (r["id"],)).fetchall()
            guess = extract.guess_title([extract.Page(1, "\n".join(x["text"] for x in first))], None) if first else None
            new = f"{org}{year} — {guess or Path(urlparse(r['url'] or '').path).stem.replace('_', ' ').replace('-', ' ')}"
        else:
            new = f"{org}{year} — {title}"
        _set_title(conn, r["id"], new[:250])
        n += 1
    conn.commit()
    return n
