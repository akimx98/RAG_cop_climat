"""Programmes des pavillons et side events de la COP31 : base de recherche (titre, résumé, intervenants, thèmes).

Pour répondre à des questions transversales du type « combien de pavillons ont parlé des forêts tropicales ? »
ou « quels pavillons ont organisé des événements critiques des solutions techniques ? ».

Sources :
- cop31.tr, site officiel de la Présidence : programme de la Blue Zone (pavillons des délégations, zone des
  événements spéciaux…) et de la Green Zone (hubs, pavillon de la Türkiye…). Les pages publiques embarquent le
  programme complet en données structurées (titre, organisateur, lieu, horaire, résumé, thèmes, mots-clés).
  Relevé toutes les 6 h ; aucune API privée n'est appelée (robots.txt exclut /api/).
- side events officiels (SEORS ou, à défaut, planner) et événements hors site du planner, repris de la table
  `agenda_events` (voir agenda.py).
- relevés complémentaires de pavillons publiés ailleurs (fichiers JSONL dans catalog/pavillons/, voir
  `import_jsonl`).
Les intervenants sont extraits des résumés quand les organisateurs les y ont listés (souvent incomplet) : le
nombre d'événements sans intervenants connus est toujours donné. Aucune coordonnée personnelle n'est conservée.
"""

from __future__ import annotations

import csv
import hashlib
import html as htmlmod
import json
import re
import sys
import threading
from pathlib import Path
from typing import Any

import numpy as np

from . import config, db, embed, fetch

SOURCES = {"blue": "https://cop31.tr/blue-zone-programme", "green": "https://cop31.tr/programme-calendar"}
SOURCE_LABELS = {"cop31.tr": "Programme officiel de la Présidence COP31 (cop31.tr)",
                 "ext:germany": "German Climate Pavilion (german-climate-pavilion.de)",
                 "ext:eu": "Pavillon de l'UE (climate.ec.europa.eu)",
                 "ext:singapore": "Singapore Pavilion (cop-pavilion.gov.sg)",
                 "ext:cryosphere": "Cryosphere Pavilion (iccinet.org)",
                 "ext:asia_climate_solutions": "Asia Climate Solutions Pavilion (calendrier Luma de SFOC)",
                 "seors": "Side events officiels CCNUCC (SEORS)", "planner": "COP31 Schedule Planner (non officiel)",
                 "planner_off": "COP31 Schedule Planner, événements hors site (non officiel)"}
SIDE_EVENTS = "Side events officiels CCNUCC"
EXTRA_DIR = Path(__file__).resolve().parents[2] / "catalog" / "pavillons"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pav_events (
  key TEXT PRIMARY KEY, source TEXT, zone TEXT, date TEXT, start TEXT, end TEXT, area TEXT, pavilion TEXT,
  organizer TEXT, title TEXT, description TEXT, speakers TEXT, session_type TEXT, themes TEXT, subtopics TEXT,
  keywords TEXT, organiser_types TEXT, participation TEXT, audience TEXT, cancelled INTEGER DEFAULT 0, link TEXT,
  fetched_at TEXT, vec_hash TEXT, vec BLOB
);
CREATE INDEX IF NOT EXISTS pav_events_pavilion ON pav_events(pavilion);
CREATE VIRTUAL TABLE IF NOT EXISTS pav_fts USING fts5(
  key UNINDEXED, title, description, pavilion, organizer, tags, speakers, tokenize='unicode61 remove_diacritics 2'
);
"""

FIELDS = ["key", "source", "zone", "date", "start", "end", "area", "pavilion", "organizer", "title", "description",
          "speakers", "session_type", "themes", "subtopics", "keywords", "organiser_types", "participation",
          "audience", "cancelled", "link"]
LISTS = ["speakers", "themes", "subtopics", "keywords", "organiser_types", "participation", "audience"]


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


def log(msg: str) -> None:
    print(f"[pavillons] {msg}", file=sys.stderr, flush=True)


# --- lecture des pages cop31.tr (Next.js : flux RSC embarqué dans la page) -------------------------------

def _rsc_text(page: str) -> str:
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', page, re.S)
    return "".join(json.loads('"' + c + '"') for c in chunks)


def _text_refs(rsc: str) -> dict[str, str]:
    """Blocs texte du flux RSC (`74:T76f,<html…>` : identifiant, longueur en octets UTF-8, contenu)."""
    raw = rsc.encode("utf-8")
    out: dict[str, str] = {}
    for m in re.finditer(rb"(?:^|[\n>\]}])([0-9a-f]{1,4}):T([0-9a-f]{1,6}),", raw):
        start = m.end()
        out[m.group(1).decode()] = raw[start:start + int(m.group(2), 16)].decode("utf-8", "replace")
    return out


def html_to_text(s: str) -> str:
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", s or "")
    s = re.sub(r"(?i)<li[^>]*>", "\n- ", s)
    s = htmlmod.unescape(re.sub(r"<[^>]+>", "", s)).replace("\r\n", "\n").replace("\r", "\n").replace("", "-")
    s = re.sub(r"[ \t ]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", "\n".join(x.strip() for x in s.split("\n"))).strip()


def parse_cop31tr(page: str, zone: str) -> list[dict[str, Any]]:
    rsc = _rsc_text(page)
    refs = _text_refs(rsc)
    dec = json.JSONDecoder()
    labels: dict[str, tuple[str, str]] = {}
    i = rsc.find('"suzgecGruplari":')
    if i >= 0:
        try:
            groups, _ = dec.raw_decode(rsc, i + len('"suzgecGruplari":'))
            for g in groups:
                for o in g.get("secenekler") or []:
                    labels[str(o["id"])] = (g["key"], o["label"])
        except ValueError:
            pass
    seen: dict[int, dict] = {}
    for m in re.finditer(r'\{"id":\d+,"title":', rsc):
        try:
            o, _ = dec.raw_decode(rsc, m.start())
        except ValueError:
            continue
        if "startTime" in o and "place" in o:
            seen[o["id"]] = o
    events = []
    for o in seen.values():
        desc = o.get("descriptionHtml") or ""
        if isinstance(desc, str) and re.fullmatch(r"\$[0-9a-f]+", desc):
            desc = refs.get(desc[1:], "")
        text = html_to_text(desc)
        tags: dict[str, list[str]] = {}
        for nid in o.get("nitelikIdleri") or []:
            if str(nid) in labels:
                k, lab = labels[str(nid)]
                tags.setdefault(k, []).append(lab)
        place = o.get("place") or ""
        parts = [p.strip() for p in place.split("›")]
        area, pavilion = (parts[0], parts[-1]) if len(parts) > 1 else (o.get("hub") or "", parts[0])
        pavilion = _pavilion_name(pavilion, area, o.get("hub") or "", zone)
        events.append(dict(
            key=f"cop31tr:{zone}:{o['id']}", source="cop31.tr", zone=zone, date=o.get("date"),
            start=o.get("startTime"), end=o.get("endTime"), area=area, pavilion=pavilion,
            organizer=o.get("organizer") or None, title=re.sub(r"\s+", " ", o.get("title") or "").strip(),
            description=text, speakers=extract_speakers(text),
            session_type=o.get("typeLabel") or ", ".join(tags.get("session-type", [])) or None,
            themes=list(dict.fromkeys((o.get("themes") or []) + tags.get("theme", []))),
            subtopics=tags.get("thematic-subtopic", []), keywords=tags.get("keyword", []),
            organiser_types=tags.get("organiser-type", []), participation=tags.get("participation", []),
            audience=tags.get("audience", []), cancelled=1 if o.get("iptal") else 0,
            link=SOURCES[zone]))
    return events


def _pavilion_name(room: str, area: str, hub: str, zone: str) -> str:
    """Nom du pavillon ou du lieu : pavillon des délégations (Blue Zone, aire B2) tel quel ; hubs de la Green Zone
    regroupés (leurs scènes ne sont pas des pavillons) ; autres salles préfixées par leur aire."""
    room, area, hub = (re.sub(r"\s+", " ", x).strip() for x in (room, area, hub))
    if zone == "green":
        if room.startswith("Türkiye Pavilion"):
            return "Türkiye Pavilion (Green Zone)"
        name = hub or area or room
        return f"Green Zone · {name}" + ("" if re.search(r"Hub|Centre|Governorship|Amphitheatre|Forum|COPArt", name) else " Hub")
    if area and not area.startswith("Area B2") and area not in room:
        return f"{area} › {room}"
    return room


# --- intervenants ----------------------------------------------------------------------------------------

_HEAD = re.compile(
    r"^\s*(?:[-•*]\s*)?(?:(?:confirmed|invited|featured|key|distinguished|expected|tentative|proposed)\s+)?"
    r"(speakers?|panel(?:l)?ists?|panel|moderat(?:or|ors|ed by|ion)|facilitat(?:or|ors|ed by)|chair(?:ed by)?|"
    r"keynotes?(?: speakers?| speech| address)?|opening(?: remarks)?|welcome(?: remarks| address)?|closing(?: remarks)?|"
    r"(?:high[- ]level )?interventions?|presenters?|discussants?|hosts?|hosted by|"
    r"intervenants?|orateurs?|conférenciers?|ponentes|konuşmacılar|moderatör)\b[^:\n]{0,40}[:\-–]\s*(.*)$", re.I)
_STOP = re.compile(r"^\s*(agenda|programme|program|objectives?|background|about|expected outcomes?|format|"
                   r"registration|contact|description|structure|key questions|outcomes?)\b", re.I)
_PERSON = re.compile(r"\b(?:H\.?E\.?|Dr\.?|Prof\.?|Mr\.?|Ms\.?|Mrs\.?|Minister|Ambassador)\s+[A-ZÀ-ÖØ-Þ]")


def _clean_person(s: str) -> str | None:
    s = re.sub(r"^(?:[^\w\"'(]|\d+[.)]|ï\s)+", "", s).strip(" ;,.")  # puces, y compris « ï » (puce mal encodée)
    s = re.sub(r"\s+", " ", s)
    if not s or len(s) > 220 or len(s) < 4 or s.lower() in ("tbc", "tbd", "to be confirmed"):
        return None
    if re.search(r"\b(TBC|TBD|to be confirmed|additional speakers)\b", s, re.I) and len(s) < 40:
        return None
    if re.search(r"https?://|@\w+\.\w+|\+\d[\d\s]{6,}", s):  # pas de coordonnées
        return None
    if " " not in s or s.endswith(":") or not re.match(r"[\w\"'(]", s) or s[0].islower():
        return None  # mot isolé, intitulé de rubrique, fin de phrase coupée
    if "," not in s and _LABEL.match(s):
        return None  # « Opening Remarks », « Panel Discussion 1 », « Q&A »…
    return s


_LABEL = re.compile(r"^(?:opening|closing|welcome|keynote|panel|fireside|remarks|session|presentation|q\s?&\s?a|"
                    r"networking|discussion|break|lunch|introduction|wrap|summary|launch|high[- ]level)\b", re.I)


def extract_speakers(text: str) -> list[dict[str, str]]:
    """Intervenants listés dans un résumé (« Speakers: », « Moderator: », listes à puces, « H.E. … »).
    Renvoie [{"role": "speaker"|"moderator"|…, "name": "Nom, fonction, organisation"}]."""
    out: list[dict[str, str]] = []
    lines = (text or "").split("\n")
    role = None
    for line in lines:
        m = _HEAD.match(line)
        if m:
            head = m.group(1).lower()
            role = ("moderator" if head.startswith(("moderat", "facilitat", "chair", "moderatör")) else
                    "keynote" if head.startswith("keynote") else
                    "host" if head.startswith("host") else
                    "opening/closing" if head.startswith(("opening", "welcome", "closing")) else "speaker")
            rest = m.group(2).strip()
            if rest:
                parts = re.split(r";|\s+-\s+(?=[A-ZÀ-Þ])|(?<=\))\s*,\s*", rest) if (";" in rest or " - " in rest or ")," in rest) else [rest]
                if len(parts) == 1 and rest.count(",") >= 3 and not _PERSON.search(rest):
                    parts = [rest]  # « Nom, fonction, organisation » : une seule personne
                for p in parts:
                    if (c := _clean_person(p)):
                        out.append({"role": role, "name": c})
            continue
        if role is None:
            continue
        if not line.strip():
            if out and out[-1]["role"] == role:
                role = role  # une ligne vide n'arrête la liste que si la suivante n'est pas une puce
            continue
        if _STOP.match(line) or len(line) > 260:
            role = None
            continue
        if re.match(r"^\s*[-•*·▪●]|\d+[.)]\s", line) or _PERSON.search(line) or len(line) < 160:
            if (c := _clean_person(line)):
                out.append({"role": role, "name": c})
        else:
            role = None
    if not out:  # pas de rubrique : personnes citées avec un titre (« H.E. …, Minister of … »)
        for m in re.finditer(r"(?:H\.E\.|Dr\.|Prof\.|Mr\.|Ms\.|Mrs\.)\s+[A-ZÀ-Þ][\w'’.-]+(?:\s+[A-ZÀ-Þ][\w'’.-]+){0,3}",
                             text or ""):
            out.append({"role": "cité", "name": m.group(0)})
    seen, uniq = set(), []
    for s in out:
        k = (s["role"], s["name"].lower())
        if k not in seen:
            seen.add(k)
            uniq.append(s)
    return uniq[:40]


# --- autres sources : side events et hors site (agenda), relevés complémentaires -------------------------

def from_agenda(conn) -> list[dict[str, Any]]:
    from . import agenda

    agenda.ensure_schema(conn)
    se_src = agenda._side_event_source(conn)
    out = []
    for r in conn.execute("SELECT * FROM agenda_events WHERE (kind='side_event' AND source=?) OR kind='hors_site'",
                          (se_src,)):
        r = dict(r)
        days = [int(d) for d in (r["days"] or "").strip(",").split(",") if d]
        if r["kind"] == "hors_site" and "cop31.tr" in (r["link"] or ""):
            continue  # déjà relevé à la source
        orgs = json.loads(r["organisers"] or "[]")
        sp_text = r["speakers"] or ""
        speakers = extract_speakers("Speakers: " + sp_text) if sp_text else []
        is_se = r["kind"] == "side_event"
        out.append(dict(
            key=f"agenda:{r['key']}", source=r["source"] if is_se else "planner_off", zone="blue" if is_se else "autre",
            date=agenda.iso_day(days[0]) if days else None, start=r["start"], end=r["end"],
            area="Side Events Area" if is_se else (r["room"] or ""),
            pavilion=SIDE_EVENTS if is_se else (r["room"] or "Hors site").split(",")[0][:120],
            organizer="; ".join(orgs) or None, title=r["title"],
            description="\n\n".join(x for x in (r["summary"], ("Speakers: " + sp_text) if sp_text else None,
                                               r["note"] if not is_se else None) if x),
            speakers=speakers, session_type="Side event" if is_se else (r["type"] or None),
            themes=json.loads(r["topics"] or "[]"), subtopics=[], keywords=[], organiser_types=[], participation=[],
            audience=[], cancelled=0, link=r["link"]))
    return out


def import_jsonl(path: Path) -> list[dict[str, Any]]:
    """Relevé complémentaire d'un pavillon (un événement par ligne) :
    {"pavilion", "date" (AAAA-MM-JJ), "start", "end", "title", "description", "speakers": [str|{role,name}],
     "organizer", "link", "source"}. Clé stable : source + date + début + titre."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        sp = [s if isinstance(s, dict) else {"role": "speaker", "name": s} for s in e.get("speakers") or []]
        desc = e.get("description") or ""
        h = hashlib.sha1(f"{e.get('date')}|{e.get('start')}|{e.get('title')}".encode()).hexdigest()[:12]
        out.append(dict(
            key=f"extra:{path.stem}:{h}", source=e.get("source") or path.stem, zone=e.get("zone") or "blue",
            date=e.get("date"), start=e.get("start"), end=e.get("end"), area=e.get("area") or "",
            pavilion=e["pavilion"], organizer=e.get("organizer"), title=e.get("title"), description=desc,
            speakers=sp or extract_speakers(desc), session_type=e.get("type"), themes=e.get("themes") or [],
            subtopics=[], keywords=e.get("keywords") or [], organiser_types=[], participation=[], audience=[],
            cancelled=0, link=e.get("link")))
    return out


# --- synchronisation -------------------------------------------------------------------------------------

def _store(conn, events: list[dict], sources: list[str], now: str) -> None:
    rows = []
    for e in events:
        r = {k: e.get(k) for k in FIELDS}
        for k in LISTS:
            r[k] = json.dumps(r[k] or [], ensure_ascii=False)
        r["fetched_at"] = now
        rows.append(r)
    with conn:
        conn.executemany(
            f"""INSERT INTO pav_events({','.join(FIELDS)},fetched_at) VALUES({','.join(':' + f for f in FIELDS)},:fetched_at)
                ON CONFLICT(key) DO UPDATE SET {', '.join(f'{f}=excluded.{f}' for f in FIELDS[1:])},
                fetched_at=excluded.fetched_at""", rows)
        keys = [e["key"] for e in events]
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _keep(key TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM _keep")
        conn.executemany("INSERT OR IGNORE INTO _keep VALUES(?)", [(k,) for k in keys])
        conn.execute(f"DELETE FROM pav_events WHERE source IN ({','.join('?' * len(sources))}) "
                     "AND key NOT IN (SELECT key FROM _keep)", sources)


def _reindex(conn) -> None:
    with conn:
        conn.execute("DELETE FROM pav_fts")
        conn.execute(
            """INSERT INTO pav_fts(key,title,description,pavilion,organizer,tags,speakers)
               SELECT key, title, description, pavilion, organizer,
                      coalesce(themes,'')||' '||coalesce(subtopics,'')||' '||coalesce(keywords,'')||' '||coalesce(session_type,''),
                      speakers FROM pav_events""")


EXT_EVERY_H = 24  # programmes externes : une fois par jour (une requête par session pour certains)


def _external(conn, client: fetch.Client, out: dict[str, Any], force: bool) -> None:
    """Relève (au plus une fois par jour, résultat en cache dans meta) puis fusionne les programmes externes."""
    from datetime import datetime, timedelta, timezone

    from . import pavillons_ext

    for name, (collect, pattern) in pavillons_ext.COLLECTORS.items():
        cached = json.loads(db.meta_get(conn, f"pav_ext:{name}") or "{}")
        at = cached.get("at") or ""
        stale = not at or datetime.now(timezone.utc) - datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc) > timedelta(hours=EXT_EVERY_H)
        if force or stale:
            try:
                evs = collect(client)
                if evs or not cached.get("events"):
                    cached = {"at": db.now(), "events": evs}
                    db.meta_set(conn, f"pav_ext:{name}", json.dumps(cached, ensure_ascii=False))
            except Exception as exc:  # source indisponible : on garde le dernier relevé
                out[f"erreur {name}"] = str(exc)[:200]
        res = merge_external(conn, cached.get("events") or [], pattern, f"ext:{name}")
        out[name] = res


def _dedupe_planner(conn) -> int:
    """Le planner recopie des événements de pavillons déjà relevés à la source : on retire ces doublons."""
    from difflib import SequenceMatcher

    from .agenda import _norm

    by_day: dict[str, list[str]] = {}
    for r in conn.execute("SELECT date, title FROM pav_events WHERE source!='planner_off' AND source NOT IN ('seors','planner')"):
        by_day.setdefault(r["date"], []).append(_norm(r["title"]))
    gone = []
    for r in conn.execute("SELECT key, date, title FROM pav_events WHERE source='planner_off'"):
        t = _norm(r["title"])
        if any(SequenceMatcher(None, t, o).ratio() >= 0.8 for o in by_day.get(r["date"], [])):
            gone.append((r["key"],))
    if gone:
        with conn:
            conn.executemany("DELETE FROM pav_events WHERE key=?", gone)
    return len(gone)


def merge_external(conn, events: list[dict], pattern: str, source: str) -> dict[str, int]:
    """Un événement externe déjà présent sur cop31.tr (même jour, pavillon `pattern`, titre proche) complète la fiche
    cop31.tr (résumé plus long, intervenants) ; présent dans le planner hors site, il le remplace ; sinon il est ajouté."""
    from difflib import SequenceMatcher

    from .agenda import _norm

    enriched, replaced, added = 0, 0, []
    for e in events:
        cands = [dict(r) for r in conn.execute(
            "SELECT key, source, title, description, speakers, start FROM pav_events WHERE date=? AND "
            "((source='cop31.tr' AND pavilion LIKE ?) OR source='planner_off')", (e["date"], f"%{pattern}%"))]
        best, score = None, 0.0
        for c in cands:
            sc = SequenceMatcher(None, _norm(e["title"]), _norm(c["title"])).ratio() + (0.15 if c["start"] == e["start"] else 0)
            if sc > score:
                best, score = c, sc
        if best and score >= 0.7 and best["source"] == "cop31.tr":
            upd = {}
            if len(e["description"] or "") > len(best["description"] or "") + 40:
                upd["description"] = e["description"]
            if e["speakers"] and best["speakers"] in (None, "", "[]"):
                upd["speakers"] = json.dumps(e["speakers"], ensure_ascii=False)
            if upd:
                with conn:
                    conn.execute(f"UPDATE pav_events SET {', '.join(k + '=?' for k in upd)} WHERE key=?",
                                 [*upd.values(), best["key"]])
                enriched += 1
            continue
        if best and score >= 0.7 and best["source"] == "planner_off":
            with conn:
                conn.execute("DELETE FROM pav_events WHERE key=?", (best["key"],))
            replaced += 1
        added.append(e)
    _store(conn, added, [source], db.now())
    return {"releves": len(events), "fiches_cop31tr_completees": enriched, "ajoutes": len(added),
            "remplacent_planner": replaced}


def sync(conn, client: fetch.Client, min_events: int = 200, force_ext: bool = False) -> dict[str, Any]:
    ensure_schema(conn)
    out: dict[str, Any] = {}
    now = db.now()
    for zone, url in SOURCES.items():
        res = client.get(url)
        if not res.ok:
            out[zone] = f"HTTP {res.status} {res.error or ''}".strip()
            continue
        events = parse_cop31tr(res.content.decode("utf-8", "replace"), zone)
        if len(events) < min_events:  # page changée ou tronquée : ne rien effacer
            out[zone] = f"seulement {len(events)} événements lus, base inchangée"
            continue
        with conn:
            conn.execute("DELETE FROM pav_events WHERE source='cop31.tr' AND zone=? AND key NOT IN (%s)"
                         % ",".join("?" * len(events)), [zone, *[e["key"] for e in events]])
        _store(conn, events, ["__none__"], now)
        out[zone] = len(events)
    ag = from_agenda(conn)
    _store(conn, ag, ["seors", "planner", "planner_off"], now)
    out["side_events_et_hors_site"] = len(ag)
    _external(conn, client, out, force_ext)
    out["doublons_planner_retires"] = _dedupe_planner(conn)
    extra = []
    for path in sorted(EXTRA_DIR.glob("*.jsonl")) if EXTRA_DIR.exists() else []:
        try:
            extra += import_jsonl(path)
        except (ValueError, KeyError) as exc:
            out[f"erreur {path.name}"] = str(exc)
    if extra:
        _store(conn, extra, sorted({e["source"] for e in extra}), now)
        out["complements"] = len(extra)
    _reindex(conn)
    db.meta_set(conn, "pav_synced_at", now)
    if embed.enabled():
        try:
            out["embeddings"] = embed_pending(conn)
        except Exception as exc:  # l'index par mots reste utilisable
            out["embeddings"] = f"erreur : {exc}"
    out["total"] = conn.execute("SELECT COUNT(*) FROM pav_events").fetchone()[0]
    return out


def _embed_text(r) -> str:
    return f"{r['pavilion']} — {r['title']}\n{(r['description'] or '')[:4000]}"


def embed_pending(conn, batch: int = 200) -> int:
    rows = [dict(r) for r in conn.execute("SELECT key,pavilion,title,description,vec_hash FROM pav_events")]
    todo = []
    for r in rows:
        h = hashlib.sha1(_embed_text(r).encode()).hexdigest()[:16]
        if r["vec_hash"] != h:
            todo.append((r, h))
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        vecs = embed.encode([_embed_text(r) for r, _ in part]).astype(np.float16)
        with conn:
            conn.executemany("UPDATE pav_events SET vec=?, vec_hash=? WHERE key=?",
                             [(v.tobytes(), h, r["key"]) for (r, h), v in zip(part, vecs)])
    if todo:
        _vec_cache.clear()
    return len(todo)


# --- interrogation ---------------------------------------------------------------------------------------

_vec_cache: dict[str, Any] = {}
_vec_lock = threading.Lock()


def _vectors(conn) -> tuple[list[str], np.ndarray]:
    stamp = db.meta_get(conn, "pav_synced_at") or ""
    with _vec_lock:
        if _vec_cache.get("stamp") != stamp:
            rows = conn.execute("SELECT key, vec FROM pav_events WHERE vec IS NOT NULL").fetchall()
            mat = (np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float16).reshape(len(rows), config.EMBED_DIM)
                   .astype(np.float32) if rows else np.zeros((0, config.EMBED_DIM), dtype=np.float32))
            _vec_cache.update(stamp=stamp, keys=[r["key"] for r in rows], mat=mat)
        return _vec_cache["keys"], _vec_cache["mat"]


def _fts_query(sujet: str) -> str:
    """« tropical forest, rainforest; "nature-based solutions" » → requête FTS5 (OU entre termes, ET dans un terme)."""
    terms = [t.strip() for t in re.split(r"[,;]|\bOR\b|\bOU\b", sujet) if t.strip()]
    out = []
    for t in terms:
        if t.startswith('"') and t.endswith('"'):
            out.append(t)
            continue
        words = [w for w in re.findall(r"[\w'’-]+\*?", t) if len(w) > 1]
        if words:
            out.append("(" + " AND ".join(f'"{w.rstrip("*")}"' + ("*" if w.endswith("*") else "") for w in words) + ")")
    return " OR ".join(out)


def _filters(pavillon, jour, zone, source) -> tuple[list[str], list[Any]]:
    from . import agenda

    where, params = ["cancelled=0"], []
    if pavillon:
        where.append("(pavilion LIKE ? OR organizer LIKE ?)")
        params += [f"%{pavillon}%"] * 2
    day = agenda.resolve_day(jour) if jour else None
    if day:
        where.append("date=?")
        params.append(agenda.iso_day(day))
    if zone:
        where.append("zone=?")
        params.append(zone)
    if source:
        where.append("source=?")
        params.append(source)
    return where, params


def search(conn, sujet: str | None = None, pavillon: str | None = None, jour: str | None = None,
           zone: str | None = None, source: str | None = None, mode: str = "hybride", seuil: float = 0.42,
           limit: int = 300) -> list[dict[str, Any]]:
    """Événements correspondants, chacun avec `correspondance` (mots, sens ou les deux) et un score."""
    ensure_schema(conn)
    where, params = _filters(pavillon, jour, zone, source)
    base = {r["key"]: dict(r) for r in conn.execute(
        f"SELECT * FROM pav_events WHERE {' AND '.join(where)}", params)}
    if not sujet:
        rows = list(base.values())
        for r in rows:
            r["correspondance"] = None
        return sorted(rows, key=lambda r: (r["pavilion"] or "", r["date"] or "", r["start"] or ""))[:limit]
    hits: dict[str, dict] = {}
    if mode in ("hybride", "mots"):
        q = _fts_query(sujet)
        if q:
            try:
                for i, r in enumerate(conn.execute("SELECT key, bm25(pav_fts, 0, 5, 1, 2, 1, 2, 0.5) AS s FROM pav_fts "
                                                   "WHERE pav_fts MATCH ? ORDER BY s", (q,))):
                    if r["key"] in base:
                        hits.setdefault(r["key"], {"mots": True, "rang_mots": i})
            except Exception as exc:
                log(f"FTS « {q} » : {exc}")
    if mode in ("hybride", "sens") and embed.enabled():
        keys, mat = _vectors(conn)
        if len(keys):
            qv = embed.encode([sujet])[0].astype(np.float32)
            scores = mat @ qv
            order = np.argsort(-scores)
            for i in order[: max(limit, 400)]:
                if scores[i] < seuil:
                    break
                k = keys[i]
                if k in base:
                    hits.setdefault(k, {})["sens"] = round(float(scores[i]), 3)
    rows = []
    for k, h in hits.items():
        r = dict(base[k])
        r["correspondance"] = "+".join(x for x in ("mots", "sens") if x in h)
        r["score_sens"] = h.get("sens")
        r["_rank"] = (0 if "mots" in h and "sens" in h else 1) - (h.get("sens") or 0)
        rows.append(r)
    rows.sort(key=lambda r: r["_rank"])
    return rows[:limit]


def _event_out(r: dict, resume: int = 450) -> dict[str, Any]:
    sp = json.loads(r["speakers"] or "[]")
    desc = r["description"] or ""
    return {k: v for k, v in {
        "id": r["key"], "titre": r["title"], "date": r["date"], "heure": "–".join(x for x in (r["start"], r["end"]) if x),
        "organisateur": r["organizer"], "type": r["session_type"],
        "resume": (desc[:resume] + ("…" if len(desc) > resume else "")) if resume else desc,
        "intervenants": [f"{s['name']} ({s['role']})" if s["role"] not in ("speaker",) else s["name"] for s in sp][:12] or None,
        "mots_cles": (json.loads(r["keywords"] or "[]") + json.loads(r["subtopics"] or "[]"))[:12] or None,
        "correspondance": r.get("correspondance"), "score_sens": r.get("score_sens"),
    }.items() if v not in (None, "", [])}


def query(conn, sujet: str | None = None, pavillon: str | None = None, jour: str | None = None,
          zone: str | None = None, mode: str = "hybride", limite: int = 80, resume: int = 450) -> dict[str, Any]:
    ensure_schema(conn)
    if not db.meta_get(conn, "pav_synced_at"):
        return {"erreur": "programme des pavillons pas encore relevé (cop pavillons-sync)"}
    rows = search(conn, sujet, pavillon, jour, zone, mode=mode, limit=2000)
    by_pav: dict[str, list[dict]] = {}
    for r in rows:
        by_pav.setdefault(r["pavilion"] or "?", []).append(r)
    order = sorted(by_pav.items(), key=lambda kv: -len(kv[1]))
    shown, groups = 0, []
    for pav, evs in order:
        evs.sort(key=lambda r: (r.get("_rank", 0), r["date"] or "", r["start"] or "")) if sujet else \
            evs.sort(key=lambda r: (r["date"] or "", r["start"] or ""))
        take = evs[: max(0, min(len(evs), limite - shown))] if shown < limite else []
        shown += len(take)
        g = {"pavillon": pav, "zone": evs[0]["zone"], "evenements_trouves": len(evs),
             "dont_par_mots": sum(1 for r in evs if "mots" in (r.get("correspondance") or "")) if sujet else None,
             "evenements": [_event_out(r, resume) for r in take]}
        groups.append({k: v for k, v in g.items() if v is not None})
    total_pav = conn.execute("SELECT COUNT(DISTINCT pavilion) FROM pav_events WHERE cancelled=0").fetchone()[0]
    out: dict[str, Any] = {
        "question": {"sujet": sujet, "pavillon": pavillon, "jour": jour, "zone": zone, "mode": mode},
        "pavillons_concernes": len(by_pav), "evenements_trouves": len(rows),
        "sur": f"{conn.execute('SELECT COUNT(*) FROM pav_events WHERE cancelled=0').fetchone()[0]} événements, "
               f"{total_pav} pavillons ou lieux",
        "par_pavillon": groups,
        "releve_le": db.meta_get(conn, "pav_synced_at"),
        "methode": (
            "Correspondance « mots » : le terme figure dans le titre, le résumé, les mots-clés ou les intervenants ; "
            "« sens » : proximité sémantique du résumé avec la question (peut inclure des faux positifs : relire "
            "le résumé avant de compter). Un comptage fiable = événements « mots » + événements « sens » vérifiés."
            if sujet else "Programme complet, groupé par pavillon."),
        "limites": ("Résumés et intervenants tels que publiés par les organisateurs (souvent incomplets) ; "
                    "programmes susceptibles d'avoir changé ; pavillons qui ne publient pas leur programme sur cop31.tr "
                    "absents ou partiels (voir `pavillons_couverture`)."),
        "sources": {k: v for k, v in SOURCE_LABELS.items()} | {"urls": list(SOURCES.values())},
    }
    if shown < len(rows):
        out["tronque"] = f"{shown} événements détaillés sur {len(rows)} (tous les pavillons sont comptés)"
    return out


def coverage(conn) -> dict[str, Any]:
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT pavilion, zone, source, COUNT(*) n, SUM(length(description)>80) avec_resume,
                  SUM(speakers!='[]') avec_intervenants, MIN(date) premier, MAX(date) dernier
           FROM pav_events WHERE cancelled=0 GROUP BY pavilion, zone, source ORDER BY n DESC""").fetchall()
    return {"releve_le": db.meta_get(conn, "pav_synced_at"), "pavillons": [dict(r) for r in rows],
            "total": sum(r["n"] for r in rows)}


def event(conn, key: str) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM pav_events WHERE key=?", (key,)).fetchone()
    if not r:
        return {"erreur": f"événement inconnu : {key}"}
    r = dict(r)
    out = _event_out(r, resume=0)
    out.update(pavillon=r["pavilion"], zone=r["zone"], lieu=r["area"], themes=json.loads(r["themes"] or "[]"),
               public=json.loads(r["audience"] or "[]"), acces=json.loads(r["participation"] or "[]"),
               type_organisateur=json.loads(r["organiser_types"] or "[]"), lien=r["link"],
               source=SOURCE_LABELS.get(r["source"], r["source"]), releve_le=r["fetched_at"])
    return out


def export(conn, path: Path) -> int:
    ensure_schema(conn)
    rows = [dict(r) for r in conn.execute(f"SELECT {','.join(FIELDS)}, fetched_at FROM pav_events "
                                          "ORDER BY pavilion, date, start")]
    for r in rows:
        for k in LISTS:
            r[k] = json.loads(r[k] or "[]")
    if path.suffix == ".jsonl":
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    else:
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=[*FIELDS, "fetched_at"])
            w.writeheader()
            for r in rows:
                r["speakers"] = " | ".join(f"{s['name']} [{s['role']}]" for s in r["speakers"])
                for k in LISTS[1:]:
                    r[k] = "; ".join(r[k])
                w.writerow(r)
    return len(rows)


def for_programme(conn, sujet: str | None, jour: str | None, limit: int = 25, seuil: float = 0.45) -> dict[str, Any]:
    """Événements de pavillons et hubs (hors side events officiels, déjà dans `programme`) pour un jour et un sujet,
    triés par heure : réponse à « aujourd'hui, quels événements de pavillons sur mes sujets ? »."""
    ensure_schema(conn)
    if not db.meta_get(conn, "pav_synced_at"):
        return {"evenements": [], "note": "programme des pavillons pas encore relevé"}
    rows = [r for r in search(conn, sujet, jour=jour, mode="hybride", seuil=seuil, limit=400)
            if r["source"] not in ("seors", "planner")]
    if sujet:  # les plus pertinents d'abord, puis par heure
        rows = rows[:limit]
    rows.sort(key=lambda r: (r["date"] or "", r["start"] or "99"))
    out = [{k: v for k, v in {
        "date": r["date"], "heure": "–".join(x for x in (r["start"], r["end"]) if x), "pavillon": r["pavilion"],
        "zone": {"blue": "Blue Zone", "green": "Green Zone (public)", "autre": "hors site"}.get(r["zone"], r["zone"]),
        "lieu": r["area"],  # secteur du site (« Area B2 Delegation Pavilions », « Climate Finance Hub ») ; pas de n° de stand publié
        "titre": r["title"], "organisateur": r["organizer"],
        "resume": (r["description"] or "")[:260] + ("…" if len(r["description"] or "") > 260 else ""),
        "intervenants": [s["name"] for s in json.loads(r["speakers"] or "[]")][:5] or None,
        "correspondance": r.get("correspondance"), "id": r["key"],
    }.items() if v not in (None, "", [])} for r in rows[:limit]]
    res: dict[str, Any] = {"evenements": out, "total": len(rows)}
    if not sujet and len(rows) > limit:
        res["tronque"] = f"{limit} premiers sur {len(rows)} : préciser un sujet"
    return res
