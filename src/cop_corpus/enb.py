"""Earth Negotiations Bulletin (IISD) : comptes rendus quotidiens en PDF.

Les pages HTML de l'ENB sont derrière un challenge Cloudflare (403, jamais contourné), mais les PDF des numéros
quotidiens se téléchargent normalement. Leur adresse est régulière :
``https://enb.iisd.org/sites/default/files/<aaaa-mm>/enb<vol><nnn><langue>.pdf`` (``enb12909e.pdf`` : volume 12,
numéro 909, anglais ; ``f`` : français). On essaie chaque numéro à partir du premier de la session, dans les
dossiers du mois courant et du mois précédent, jusqu'à quelques numéros absents d'affilée.

Le numéro, la date de parution et la journée couverte sont lus dans l'en-tête du PDF (jamais devinés) : le n° 909
(« COP 31 #8 ») paraît le mercredi 18 novembre et rend compte du mardi 17. Le titre du document les reprend.

La lettre quotidienne de l'ENB (reçue sur veille@) ne contient qu'un chapeau par sujet et des liens : vers les
« highlights » et le « daily report » d'une journée (même contenu que le PDF du lendemain, rattaché par la journée
couverte), et vers des pages de side events (HTML seulement, bloquées : seul le chapeau de la lettre est au
corpus). ``coverage`` fait ce bilan lettre par lettre.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Callable

from . import catalog, config, db, extract, fetch, ingest

URL = "https://enb.iisd.org/sites/default/files/{folder}/enb{vol}{num:03d}{lang}.pdf"
URL_RE = re.compile(r"/enb(\d{2})(\d{3})([a-z])\.pdf$")
LANGS = {"e": "en", "f": "fr", "s": "es"}
_DAY = re.compile(r"(?:Conference|Conférence|Conferencia)[^:]{0,80}:\s*\n?\s*\n?\s*"
                  r"((?:[A-Za-zéû]+day|[Ll]undi|[Mm]ardi|[Mm]ercredi|[Jj]eudi|[Vv]endredi|[Ss]amedi|[Dd]imanche)"
                  r",?\s+\d{1,2}(?:er)?\s+[A-Za-zéû]+\s+20\d\d)", re.S)
_SUMMARY = re.compile(r"(Summary of the|Résumé de la)[^\n]{0,200}", re.I)


def _folders(raw: dict) -> list[str]:
    now = datetime.now(timezone.utc)
    prev = (now.year - (now.month == 1), 12 if now.month == 1 else now.month - 1)
    out = [f"{now.year}-{now.month:02d}", f"{prev[0]}-{prev[1]:02d}"]
    return list(dict.fromkeys([str(f) for f in raw.get("folders") or []] + out))


def _known(conn, vol: int, num: int, lang: str) -> sqlite3.Row | None:
    return conn.execute("SELECT url, last_status FROM url_state WHERE url LIKE ? ORDER BY last_status=200 DESC LIMIT 1",
                        (f"%/enb{vol}{num:03d}{lang}.pdf",)).fetchone()


def run(conn, client: fetch.Client, source: catalog.Source, bump: Callable[[str], None]) -> None:
    raw = source.raw
    vol, first = int(raw.get("vol", 12)), int(raw["first"])
    langs = [str(l) for l in raw.get("langs") or ["e"]]
    stop_after = int(raw.get("stop_after_misses", 2))
    last = int(raw.get("last", first + 60))
    folders = _folders(raw)
    # Plus haut numéro déjà trouvé (toutes langues) : en dessous, un numéro absent n'est réessayé que s'il est récent.
    hi = first - 1
    for r in conn.execute("SELECT url FROM url_state WHERE url LIKE '%enb.iisd.org/sites/default/files/%' AND last_status=200"):
        m = URL_RE.search(r["url"])
        if m and int(m.group(1)) == vol:
            hi = max(hi, int(m.group(2)))
    for lang in langs:
        misses, num = 0, first
        while num <= last and (num <= hi or misses < stop_after):
            state = _known(conn, vol, num, lang)
            if state and state["last_status"] == 200:
                doc = conn.execute("SELECT id FROM documents WHERE url=? ORDER BY id DESC LIMIT 1", (state["url"],)).fetchone()
                if doc:
                    annotate(conn, doc["id"])
                bump("known")
                misses, num = 0, num + 1
                continue
            if state and num < hi - 2:
                bump("absent")  # trou ancien (numéro jamais paru dans cette langue) : pas de nouvel essai
                num += 1
                continue
            outcome = "absent"
            for folder in folders:
                url = URL.format(folder=folder, vol=vol, num=num, lang=lang)
                outcome = ingest.fetch_one(conn, client, url, source, dict(source.meta, lang=LANGS.get(lang, lang)), recheck=False)
                if outcome in ("new", "duplicate", "known", "unchanged"):
                    doc = conn.execute("SELECT id FROM documents WHERE url=? ORDER BY id DESC LIMIT 1", (url,)).fetchone()
                    if doc:
                        annotate(conn, doc["id"])
                    break
            bump(outcome)
            if outcome in ("new", "duplicate", "known", "unchanged"):
                misses = 0
                hi = max(hi, num)
            else:
                misses += 1
            num += 1


def header(pages: list[extract.Page]) -> dict:
    """Numéro, date de parution et journée couverte, lus dans l'en-tête du PDF."""
    p1 = pages[0].text if pages else ""
    p2 = pages[1].text[:300] if len(pages) > 1 else ""
    out: dict = {}
    m = re.search(r"Vol\.\s*(\d+)\s*No\.\s*(\d+)", p1[:1500] + "\n" + p2)
    if m:
        out["vol"], out["no"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"COP\s*(\d+)\s*#\s*(\d+)", p1[:400])
    if m:
        out["cop"], out["issue"] = int(m.group(1)), int(m.group(2))
    # Parution : en-tête courant de la page 2 (« Earth Negotiations Bulletin / Wednesday, 18 November 2026 »),
    # sinon la date de l'en-tête de la page 1 qui n'est pas la journée couverte.
    published = extract.parse_date(p2[:200]) if p2 else None
    m = _DAY.search(p1[:2500])
    if m:
        out["day_text"] = " ".join(m.group(1).split())
        out["day"] = extract.parse_date(out["day_text"])
    if not published:
        dates = [extract.parse_date(x.group(0)) for x in re.finditer(r"\d{1,2}(?:er)?\s+[A-Za-zéû]+\s+20\d\d", p1[:1500])]
        dates = [d for d in dates if d and d != out.get("day")]
        published = dates[0] if dates else None
    if published:
        out["published"] = published
    m = _SUMMARY.search(p1[:2500])
    if m and not out.get("day"):
        out["summary"] = " ".join(m.group(0).split())[:160]
    return out


def title_for(h: dict, lang: str) -> str | None:
    if "no" not in h:
        return None
    fr = lang == "fr"
    name = "Bulletin des Négociations de la Terre" if fr else "Earth Negotiations Bulletin"
    head = f"{name} Vol. {h['vol']} No. {h['no']}"
    if h.get("issue"):
        head += f" (COP {h['cop']} #{h['issue']})"
    if h.get("day_text"):
        what = f"compte rendu du {h['day_text'][0].lower()}{h['day_text'][1:]}" if fr else f"daily report, {h['day_text']}"
    elif h.get("summary"):
        what = h["summary"]
    else:
        what = "numéro" if fr else "issue"
    pub = f" — {'paru le' if fr else 'published'} {h['published']}" if h.get("published") else ""
    return f"{head} — {what}{pub}"


def annotate(conn, doc_id: int, force: bool = False) -> bool:
    """Titre, date de parution, journée couverte et langue d'un PDF de l'ENB, lus dans son en-tête (y compris pour
    un PDF déposé à la main avec un titre et une date saisis : l'en-tête l'emporte)."""
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc or not doc["raw_path"]:
        return False
    if not force and (doc["note"] or "").startswith("ENB : ") and doc["published"]:
        return False  # déjà fait
    try:
        _t, pages = extract.extract(config.DATA_DIR / doc["raw_path"], doc["mime"] or "")
    except Exception:
        return False
    m = URL_RE.search(doc["url"] or "")
    lang = LANGS.get(m.group(3), doc["lang"]) if m else (doc["lang"] or extract.detect_lang(pages[0].text if pages else ""))
    h = header(pages)
    title = title_for(h, lang or "en")
    if not title:
        return False
    note = (f"ENB : numéro, parution{' et journée couverte' if h.get('day') else ''} lus dans l'en-tête du PDF"
            + (f" ; rend compte du {h['day']}" if h.get("day") else "") + ".")
    with ingest._write_lock:
        conn.execute("""UPDATE documents SET published=COALESCE(?, published), lang=?, session=COALESCE(?, session),
                          doc_type='compte_rendu', level='4', org='Earth Negotiations Bulletin',
                          source_id='enb-daily-pdf', note=? WHERE id=?""",
                     (h.get("published"), lang, f"COP{h['cop']}" if h.get("cop") else None, note, doc_id))
        if doc["title"] != title:
            ingest._set_title(conn, doc_id, title)
        conn.commit()
    return True


# --- lettres quotidiennes : liens et couverture --------------------------------------------

_LINK = re.compile(r"https://enb\.iisd\.org/[^\s\]\)>\"']+")
_DAY_SLUG = re.compile(r"-(\d{1,2})(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)(20\d\d)$", re.I)
_MON = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def classify_link(url: str) -> tuple[str, str | None]:
    """('daily_report' | 'highlights' | 'side_event' | 'autre', journée couverte ISO ou None)."""
    path = url.split("enb.iisd.org/", 1)[-1].strip("/").split("?")[0]
    if path.startswith(("about", "fr/about")) or not path:
        return "autre", None
    m = _DAY_SLUG.search(path)
    day = f"{m.group(3)}-{_MON[m.group(2).lower()]:02d}-{int(m.group(1)):02d}" if m else None
    if "daily-report" in path:
        return "daily_report", day
    if day:
        return "highlights", day
    if path.endswith(("-cop31", "-cop-31")) or "-cop31-" in path:
        return "side_event", None
    return "autre", None


def coverage(conn, since: str = "2026-11-01") -> list[dict]:
    """Pour chaque lettre de l'ENB reçue : liens, et si la journée est couverte par un PDF du corpus."""
    reports = {}
    for r in conn.execute("""SELECT id, title, note, lang FROM documents WHERE source_id='enb-daily-pdf'
                             OR url LIKE '%enb.iisd.org/sites/default/files/%enb%.pdf'"""):
        m = re.search(r"rend compte du (\d{4}-\d{2}-\d{2})", r["note"] or "")
        if m:
            reports.setdefault(m.group(1), []).append({"doc_id": r["id"], "titre": r["title"], "langue": r["lang"]})
    out = []
    for d in conn.execute("""SELECT id, title, fetched_at FROM documents WHERE source_id='veille-mail'
                             AND org LIKE '%Earth Negotiations%' AND fetched_at>=? ORDER BY id""", (since,)):
        text = "\n".join(p["text"] for p in conn.execute("SELECT text FROM passages WHERE doc_id=? ORDER BY seq", (d["id"],)))
        links = list(dict.fromkeys(_LINK.findall(text)))
        days = sorted({day for u in links for kind, day in [classify_link(u)] if kind in ("daily_report", "highlights") and day})
        sides = [u for u in links if classify_link(u)[0] == "side_event"]
        out.append({
            "doc_id": d["id"], "titre": d["title"], "recu_le": d["fetched_at"],
            "journees": [{"jour": day, "pdf": reports.get(day, [])} for day in days],
            "side_events_non_recuperables": sides,
        })
    return out


def annotate_all(conn) -> int:
    """Relit l'en-tête de tous les PDF de l'ENB du corpus (collectés ou déposés à la main)."""
    n = 0
    for r in conn.execute("SELECT id FROM documents WHERE url LIKE '%enb.iisd.org/sites/default/files/%/enb%.pdf'").fetchall():
        n += annotate(conn, r["id"], force=True)
    return n
