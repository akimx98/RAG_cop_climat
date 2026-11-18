"""Programme de la COP31 : side events, réunions officielles, événements hors site, vue d'ensemble par jour.

Sources :
- « COP31 Schedule Planner » (https://unmai-planner-cop31.netlify.app/, non officiel) : page statique publique qui
  embarque les side events officiels, le calendrier des réunions officielles (coordinations de groupes, événements
  prévus par les décisions, salles spéciales), ~1 000 événements hors site et un résumé des négociations par jour.
  Relevée toutes les 6 h. Copie figée à sa date de construction : salles et horaires peuvent avoir changé.
- SEORS (https://seors.unfccc.int/reports/events_list.html), le programme officiel des side events. Protégé par un
  anti-bot (Imperva), jamais contourné. Trois voies, la plus récente l'emporte sur le planner :
  1. relevé automatique toutes les heures par une requête HTTP ordinaire (`sync_seors`) : s'arrête sur la page de
     défi tant que l'adresse du serveur n'est pas autorisée par la CCNUCC, et importe dès qu'elle l'est ;
  2. bouton de la page Admin → Programme : un clic sur la page SEORS ouverte dans le navigateur d'un administrateur
     relève les journées et les envoie au serveur (`/admin/seors/import`) ;
  3. relevé collé à la main (`cop programme-seors releve.tsv`).
Les coordonnées personnelles des organisateurs (noms, e-mails, téléphones) ne sont jamais conservées.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import db, fetch

PLANNER_URL = "https://unmai-planner-cop31.netlify.app/"
PLANNER_LIVE_URL = PLANNER_URL + "data/live.json"
SEORS_URL = "https://seors.unfccc.int/reports/events_list.html"
PLATFORM_URL = "https://cop31platform.unfccc.int/events/cop31/agenda"
YEAR, MONTH = 2026, 11
FIRST_DAY, LAST_DAY = 9, 20
ANTALYA = timezone(timedelta(hours=3))

SLOTS = [["11:30", "13:00"], ["13:15", "14:45"], ["15:00", "16:30"], ["16:45", "18:15"], ["18:30", "20:00"]]
ROOMS = {1: "Gordiyon", 2: "Dalyan", 3: "Zerzevan", 4: "Aşık Veysel", 5: "Hattuşa", 6: "Katip Çelebi",
         7: "Hasankeyf", 8: "Bergama", 9: "Safranbolu"}
CAL_TYPES = {"coord": "réunion de coordination", "mandated": "événement prévu par une décision",
             "special": "événement spécial (pavillon CCNUCC, salles spéciales)", "press": "conférence de presse"}
KINDS = {"side_event": "side event officiel (Blue Zone, accréditation requise)",
         "reunion": "réunion officielle du calendrier CCNUCC", "hors_site": "événement hors site ou pavillon"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS agenda_events (
  key TEXT PRIMARY KEY, kind TEXT, source TEXT, days TEXT, start TEXT, end TEXT, room TEXT,
  title TEXT, organisers TEXT, summary TEXT, speakers TEXT, topics TEXT, type TEXT, access TEXT,
  link TEXT, note TEXT, fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS agenda_events_kind ON agenda_events(kind, source);
CREATE TABLE IF NOT EXISTS agenda_overview (day INTEGER PRIMARY KEY, text TEXT, fetched_at TEXT);
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


def iso_day(day: int) -> str:
    return date(YEAR, MONTH, day).isoformat()


def seors_link(day: int | None = None) -> str:
    """Programme officiel des side events, filtré sur le jour quand il est connu."""
    return f"{SEORS_URL}?session_id=COP31&scheduled_date={iso_day(day)}" if day else SEORS_URL


def _days_field(days: list[int]) -> str:
    return "," + ",".join(str(d) for d in sorted(set(days))) + ","


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"\([^)]*\)|[^a-z0-9 ]", " ", s)).strip()


def _js_literal(src: str, name: str) -> Any:
    """Lit un littéral JS simple (`var NAME={1:'a',…}` ou `[['a','b'],…]`) sans exécuter de JS."""
    m = re.search(r"var " + name + r"\s*=\s*([\[{].*?[\]}]);", src, re.S)
    if not m:
        return None
    lit = m.group(1)
    lit = re.sub(r"([{,]\s*)([A-Za-z_]\w*|\d+)\s*:", r'\1"\2":', lit)
    lit = re.sub(r"'((?:[^'\\]|\\.)*)'", lambda x: json.dumps(x.group(1).replace("\\'", "'")), lit)
    try:
        return json.loads(lit)
    except ValueError:
        return None


def _script_json(src: str, element_id: str) -> list[dict]:
    m = re.search(r'<script[^>]*id="' + element_id + r'"[^>]*>(.*?)</script>', src, re.S)
    return json.loads(m.group(1)) if m else []


def _neg_overview(src: str) -> dict[int, str]:
    m = re.search(r"var NEG=\[(.*?)\];", src, re.S)
    out: dict[int, str] = {}
    if not m:
        return out
    for label, text in re.findall(r"\[\s*'([^']*)'\s*,\s*'((?:[^'\\]|\\.)*)'\s*\]", m.group(1)):
        for d in re.findall(r"\b(\d{1,2})\b", label):
            if FIRST_DAY <= int(d) <= LAST_DAY:
                out[int(d)] = text.replace("\\'", "'")
    return out


def _off_times(when: str) -> tuple[str | None, str | None]:
    m = re.match(r"^\d{1,2} Nov, (\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2})", when or "")
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"^\d{1,2} Nov, (\d{1,2}:\d{2})", when or "")
    return (m.group(1), None) if m else (None, None)


def _side_events(items: list[dict], slots: list, rooms: dict[int, str], topics: dict) -> list[dict]:
    out = []
    for e in items:
        s = slots[e["s"]] if isinstance(e.get("s"), int) and 0 <= e["s"] < len(slots) else [None, None]
        out.append(dict(
            key=f"planner:ev:{e['id']}", kind="side_event", days=[e["d"]], start=s[0], end=s[1],
            room=f"Side Event Room {e['r']} - {rooms.get(e['r'], '?')}" if e.get("r") else None,
            title=e.get("t"), organisers=e.get("o") or [], summary=e.get("x"), speakers=e.get("sp"),
            topics=[topics.get(g, g) for g in e.get("g") or []], type=None,
            access="Blue Zone, accréditation requise", link=seors_link(e["d"]), note=None))
    return out


def _meetings(items: list[dict]) -> list[dict]:
    return [dict(
        key=f"planner:cal:{c['id']}", kind="reunion", days=[c["d"]], start=c.get("s"), end=c.get("e"),
        room=c.get("r"), title=c.get("t"), organisers=[], summary=None, speakers=None, topics=[],
        type=CAL_TYPES.get(c.get("ty"), c.get("ty")),
        access="ouverte aux observateurs" if c.get("open") else "réservée (Parties ou groupe concerné)",
        link=PLATFORM_URL, note=None) for c in items]


def parse_planner(src: str, live: dict | None = None) -> dict[str, Any]:
    """Extrait les événements du planner. Ne garde que des noms d'organisations, jamais de personnes.
    `live` : mise à jour en ligne du planner (data/live.json : {updated, ev, cal}), prise si plus récente."""
    slots = _js_literal(src, "SLOTS") or SLOTS
    rooms = {int(k): v for k, v in (_js_literal(src, "ROOMS") or ROOMS).items()}
    topics = _js_literal(src, "TOPICS") or {}
    offcat = _js_literal(src, "OFFCAT") or {}
    m = re.search(r"BUILT\s*=\s*'(\d{4}-\d{2}-\d{2})", src)
    built = m.group(1) if m else None
    ev, cal = _script_json(src, "data"), _script_json(src, "caldata")
    if live and str(live.get("updated") or "") >= (built or ""):
        ev, cal = live.get("ev") or ev, live.get("cal") or cal
        built = str(live["updated"])[:10]
    events = _side_events(ev, slots, rooms, topics) + _meetings(cal)
    m = re.search(r"var OFF=(\[.*?\]);\s*\n", src, re.S)
    for o in (json.loads(m.group(1)) if m else []):
        days = o.get("days") or ([o["d"]] if o.get("d") else [])
        start, end = _off_times(o.get("when", ""))
        events.append(dict(
            key=f"planner:off:{o['id']}", kind="hors_site", days=days, start=start, end=end,
            room=", ".join(x for x in (o.get("loc"), o.get("addr")) if x) or None, title=o.get("t"),
            organisers=[], summary=None, speakers=None, topics=[offcat.get(k, k) for k in o.get("k") or []],
            type=o.get("when"), access=o.get("acc"), link=o.get("link") or None,
            note=" ".join(x for x in (o.get("note"), f"(statut : {o['st']})" if o.get("st") else "") if x) or None))
    return {"built": built, "events": events, "overview": _neg_overview(src)}


def _upsert(conn, events: list[dict], source: str, now: str) -> None:
    conn.executemany(
        """INSERT INTO agenda_events(key,kind,source,days,start,end,room,title,organisers,summary,speakers,topics,
                                     type,access,link,note,fetched_at)
           VALUES(:key,:kind,:source,:days,:start,:end,:room,:title,:organisers,:summary,:speakers,:topics,
                  :type,:access,:link,:note,:fetched_at)
           ON CONFLICT(key) DO UPDATE SET kind=excluded.kind, days=excluded.days, start=excluded.start,
             end=excluded.end, room=excluded.room, title=excluded.title, organisers=excluded.organisers,
             summary=excluded.summary, speakers=excluded.speakers, topics=excluded.topics, type=excluded.type,
             access=excluded.access, link=excluded.link, note=excluded.note, fetched_at=excluded.fetched_at""",
        [{**e, "source": source, "days": _days_field(e["days"]), "organisers": json.dumps(e["organisers"], ensure_ascii=False),
          "topics": json.dumps(e["topics"], ensure_ascii=False), "fetched_at": now} for e in events],
    )


def sync_planner(conn, client: fetch.Client, min_events: int = 100) -> dict[str, Any]:
    ensure_schema(conn)
    res = client.get(PLANNER_URL)
    if not res.ok:
        return {"erreur": f"planner : HTTP {res.status} {res.error or ''}".strip()}
    live = None
    res_live = client.get(PLANNER_LIVE_URL)  # mise à jour en ligne prévue par le planner (absente au 13/11)
    if res_live.ok:
        try:
            live = json.loads(res_live.content)
        except ValueError:
            live = None
    parsed = parse_planner(res.content.decode("utf-8", "replace"), live if isinstance(live, dict) else None)
    events = parsed["events"]
    if len(events) < min_events:  # page changée ou tronquée : ne rien effacer
        return {"erreur": f"planner : seulement {len(events)} événements lus, base inchangée"}
    now = db.now()
    with conn:
        _upsert(conn, events, "planner", now)
        keys = [e["key"] for e in events]
        conn.execute(f"DELETE FROM agenda_events WHERE source='planner' AND key NOT IN ({','.join('?' * len(keys))})", keys)
        for day, text in parsed["overview"].items():
            conn.execute("INSERT INTO agenda_overview(day,text,fetched_at) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE "
                         "SET text=excluded.text, fetched_at=excluded.fetched_at", (day, text, now))
    db.meta_set(conn, "agenda_planner_built", parsed["built"] or "")
    db.meta_set(conn, "agenda_planner_at", now)
    by_kind: dict[str, int] = {}
    for e in events:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
    return {"construit_le": parsed["built"], **by_kind}


# --- relevé SEORS (fait dans un navigateur, importé à la main) ------------------------------------------

_PHONE = re.compile(r"^[+\d][\d\s().-]{5,}$")

# À coller dans la console du navigateur sur la page SEORS (programme officiel des side events) : copie dans le
# presse-papiers le relevé compact (jour|début|fin|salle|titre|thèmes) que lit `cop programme-seors`.
# Ne relève aucune coordonnée personnelle.
SEORS_SNIPPET = r"""
const base='/reports/events_list.html?session_id=COP31&sort_by=scheduled_date_time,room_name&scheduled_date=';
const parse=h=>[...new DOMParser().parseFromString(h,'text/html').querySelectorAll('tr')]
  .filter(r=>r.cells.length>=4&&/2026/.test(r.cells[0].textContent)).map(r=>{
  const d=r.cells[0].textContent.match(/(\d{2}) \w{3} 2026/)[1];
  const [t,...room]=r.cells[1].innerHTML.split('<br>').map(x=>x.replace(/<[^>]+>/g,'').trim());
  const [s,e]=t.split(/—|-/).map(x=>x.trim());
  const title=(r.cells[3].querySelector('strong')||r.cells[3]).textContent.trim().replace(/\s+/g,' ');
  return [d,s,e,(room.join(' ').match(/Room (\d+)/)||[,'?'])[1],title];});
const all=parse(await (await fetch(base)).text()); const cats=[], tag={};
for (const o of [...document.querySelector('select[name=keywords]').options].filter(o=>o.value)) {
  cats.push(o.text.trim());
  for (const r of parse(await (await fetch(base+'&keywords='+o.value)).text()))
    (tag[r.join('|')]=tag[r.join('|')]||[]).push(cats.length-1);
}
copy(['#releve\t'+new Date().toISOString(),'#cats\t'+cats.join(';')]
  .concat(all.map(r=>r.join('|')+'|'+(tag[r.join('|')]||[]).join('.'))).join('\n'));
"""


def parse_seors_html(html: str) -> list[list[str]]:
    """Lignes [jour, début, fin, n° de salle, titre] d'une page de liste SEORS (même lecture que SEORS_SNIPPET)."""
    from bs4 import BeautifulSoup

    out = []
    for tr in BeautifulSoup(html, "lxml").find_all("tr"):
        cells = tr.find_all(["td", "th"], recursive=False)
        if len(cells) < 4 or "2026" not in cells[0].get_text():
            continue
        m = re.search(r"(\d{2}) \w{3} 2026", cells[0].get_text(" "))
        if not m:
            continue
        parts = [re.sub(r"\s+", " ", BeautifulSoup(x, "lxml").get_text(" ")).strip()
                 for x in re.split(r"<br\s*/?>", cells[1].decode_contents(), flags=re.I)]
        times = re.split(r"\s*(?:—|–|-)\s*", parts[0] if parts else "")
        room = re.search(r"Room (\d+)", " ".join(parts[1:]))
        strong = cells[3].find("strong")
        title = re.sub(r"\s+", " ", (strong or cells[3]).get_text(" ")).strip()
        out.append([m.group(1), times[0] if times else "", times[1] if len(times) > 1 else "",
                    room.group(1) if room else "?", title])
    return out


SEORS_LIST = ("https://seors.unfccc.int/reports/events_list.html?session_id=COP31"
              "&sort_by=scheduled_date_time,room_name&scheduled_date=")


def sync_seors(conn, client: fetch.Client, with_themes: bool = True) -> dict[str, Any]:
    """Relevé SEORS par requête HTTP ordinaire. Page de défi anti-bot → rien n'est tenté de plus (jamais de
    contournement) : le relevé reprend tout seul quand la CCNUCC autorise l'adresse du serveur."""
    res = client.get(SEORS_LIST)
    if not res.ok:
        db.meta_set(conn, "agenda_seors_auto", f"{db.now()} erreur {res.status}")
        return {"seors": f"erreur {res.status}"}
    if fetch.is_antibot(res):
        db.meta_set(conn, "agenda_seors_auto", f"{db.now()} bloqué (anti-bot)")
        return {"seors": "bloqué par l'anti-bot (adresse du serveur non autorisée) : relevé par le navigateur"}
    html = res.content.decode("utf-8", "replace")
    rows = parse_seors_html(html)
    cats: list[str] = []
    tags: dict[str, list[int]] = {}
    if with_themes:
        from bs4 import BeautifulSoup

        sel = BeautifulSoup(html, "lxml").find("select", attrs={"name": "keywords"})
        for opt in (sel.find_all("option") if sel else []):
            if not opt.get("value"):
                continue
            cats.append(opt.get_text(strip=True))
            r = client.get(SEORS_LIST + "&keywords=" + opt["value"])
            if r.ok and not fetch.is_antibot(r):
                for row in parse_seors_html(r.content.decode("utf-8", "replace")):
                    tags.setdefault("|".join(row), []).append(len(cats) - 1)
    tsv = "\n".join([f"#releve\t{db.now()}", "#cats\t" + ";".join(cats)]
                    + ["|".join(r) + "|" + ".".join(str(i) for i in tags.get("|".join(r), [])) for r in rows])
    out = import_seors(conn, parse_seors_tsv(tsv))
    db.meta_set(conn, "agenda_seors_auto", f"{db.now()} importé ({out['importes']} événements)")
    return {"seors": out["importes"], "salle_ou_horaire_change": len(out["salle_ou_horaire_change"])}


def seors_bookmarklet(post_url: str, token: str) -> str:
    """Bouton de barre de favoris : sur la page SEORS ouverte par l'administrateur, relève les journées (même code
    que SEORS_SNIPPET) et les envoie au serveur ; si le navigateur refuse l'envoi, copie le relevé à coller sur la
    page Admin → Programme."""
    body = SEORS_SNIPPET.replace("copy(", "tsv=(").rstrip().rstrip(";") + ";"
    js = ("(async()=>{let tsv='';try{" + body.replace("\n", " ")
          + f"const r=await fetch({json.dumps(post_url)},{{method:'POST',headers:{{'Authorization':'Bearer '+"
          + f"{json.dumps(token)},'Content-Type':'text/plain'}},body:tsv}});"
          "const j=await r.json();alert('Relevé SEORS envoyé : '+(j.importes||j.erreur||r.status)+' événements');"
          "}catch(e){if(tsv){try{await navigator.clipboard.writeText(tsv)}catch(_){}}"
          "alert('Envoi impossible ('+e+'). Relevé copié : le coller sur la page Admin → Programme.')}})()")
    from urllib.parse import quote

    return "javascript:" + quote(js, safe="(){}[]=:;,.'+!/?&|<>*$@-_~ ")


def seors_status(conn) -> dict[str, Any]:
    at = db.meta_get(conn, "agenda_seors_at") or ""
    age = None
    if at:
        try:
            age = round((datetime.now(timezone.utc) - datetime.fromisoformat(at.replace("Z", "+00:00"))).total_seconds() / 3600, 1)
        except ValueError:
            age = None
    return {"releve_le": at or None, "age_heures": age, "auto": db.meta_get(conn, "agenda_seors_auto"),
            "planner_construit_le": db.meta_get(conn, "agenda_planner_built")}


def parse_seors_tsv(text: str) -> dict[str, Any]:
    """Relevé compact produit par SEORS_SNIPPET → format d'import_seors."""
    releve, cats, events = None, [], []
    for line in text.splitlines():
        if line.startswith("#releve"):
            releve = line.split("\t", 1)[1].strip()
        elif line.startswith("#cats"):
            cats = line.split("\t", 1)[1].split(";")
        elif line.strip():
            d, start, end, room, *rest = line.split("|")
            title, tags = "|".join(rest[:-1]), rest[-1]
            r = int(room) if room.isdigit() else None
            events.append({"date": iso_day(int(d)), "start": start, "end": end,
                           "room": f"Side Event Room {r} - {ROOMS.get(r, '?')}" if r else None, "title": title,
                           "cats": [cats[int(i)] for i in tags.split(".") if i.isdigit() and int(i) < len(cats)]})
    return {"releve": releve, "events": events}


def _similar(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    a, b = _norm(a), _norm(b)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def import_seors(conn, payload: dict[str, Any]) -> dict[str, Any]:
    """Importe un relevé SEORS : {"releve": "2026-11-13T10:49:25Z",
    "events": [{"date": "2026-11-13", "start": "15:00", "end": "16:30", "room": "Side Event Room 2 - Dalyan",
                "title": "…", "orgs": ["…"] (facultatif), "cats": ["Water", …]}]}.
    Remplace le relevé SEORS précédent. Organisateurs, résumé et intervenants sont repris du planner pour le même
    événement (même jour, titre proche ou organisations communes). Renvoie les salles et horaires qui ont changé."""
    ensure_schema(conn)
    rows = payload.get("events") or []
    if len(rows) < 50:
        raise ValueError(f"relevé SEORS trop court ({len(rows)} événements) : rien importé")
    planner = [dict(r) for r in conn.execute("SELECT * FROM agenda_events WHERE source='planner' AND kind='side_event'")]
    by_day: dict[str, list[dict]] = {}
    for p in planner:
        by_day.setdefault(p["days"], []).append(p)
    events, changes, used = [], [], set()
    for r in rows:
        day = int(r["date"][8:10])
        orgs = [o.strip() for o in r.get("orgs") or [] if o.strip() and not _PHONE.match(o.strip())]
        title = re.sub(r"\s+", " ", r.get("title") or "").strip()
        best, score = None, 0.0
        for p in by_day.get(_days_field([day]), []):
            if p["key"] in used:
                continue
            sc = _similar(title, p["title"]) + (0.1 if p["start"] == r.get("start") else 0)
            if orgs and {_norm(o) for o in orgs} & {_norm(o) for o in json.loads(p["organisers"] or "[]")}:
                sc += 1
            if sc > score:
                best, score = p, sc
        if score < 0.6:
            best = None
        if best:
            used.add(best["key"])
            if len(title) >= 99 and _norm(best["title"]).startswith(_norm(title)[:60]):
                title = best["title"]  # SEORS coupe les titres à 100 caractères
            moved = [f"{k} : {best[k]} → {r.get(k)}" for k in ("start", "room")
                     if r.get(k) and _norm(str(best[k])) != _norm(str(r.get(k)))]
            if moved:
                changes.append(f"{r['date']} « {title[:70]} » : " + " ; ".join(moved))
        events.append(dict(
            key=f"seors:{r['date']}:{r.get('start')}:{_norm(title)[:60]}", kind="side_event", days=[day],
            start=r.get("start"), end=r.get("end"), room=r.get("room"), title=title,
            organisers=orgs or json.loads((best or {}).get("organisers") or "[]"),
            summary=r.get("summary") or (best or {}).get("summary"),
            speakers=r.get("speakers") or (best or {}).get("speakers"),
            topics=r.get("cats") or json.loads((best or {}).get("topics") or "[]"), type=None,
            access="Blue Zone, accréditation requise", link=seors_link(day), note=None))
    at = payload.get("releve") or db.now()
    with conn:
        conn.execute("DELETE FROM agenda_events WHERE source='seors'")
        _upsert(conn, events, "seors", at)
    db.meta_set(conn, "agenda_seors_at", at)
    gone = [f"{p['days'].strip(',')}/11 {p['start']} « {p['title'][:70]} »" for p in planner if p["key"] not in used]
    return {"importes": len(events), "rapproches_du_planner": len(used), "releve": at,
            "salle_ou_horaire_change": changes, "absents_de_seors": gone}


# --- interrogation -------------------------------------------------------------------------------------

def resolve_day(jour: str | int | None, today: date | None = None) -> int | None:
    if jour in (None, ""):
        return None
    today = today or datetime.now(ANTALYA).date()
    s = str(jour).strip().lower()
    if s in ("aujourd'hui", "aujourdhui", "today", "auj"):
        d = today
    elif s in ("demain", "tomorrow"):
        d = today + timedelta(days=1)
    elif s in ("hier", "yesterday"):
        d = today - timedelta(days=1)
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        d = date.fromisoformat(s)
    elif re.fullmatch(r"\d{1,2}", s):
        return int(s)
    else:
        m = re.search(r"\b(\d{1,2})\b", s)
        return int(m.group(1)) if m else None
    return d.day if (d.year, d.month) == (YEAR, MONTH) else None


def _side_event_source(conn) -> str:
    """SEORS l'emporte sur le planner si son relevé est postérieur à la construction du planner."""
    seors = db.meta_get(conn, "agenda_seors_at") or ""
    built = db.meta_get(conn, "agenda_planner_built") or ""
    return "seors" if seors and seors[:10] >= built[:10] else "planner"


def _minutes(t: str | None) -> int:
    m = re.match(r"(\d{1,2}):(\d{2})", t or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 24 * 60


def query(conn, jour: str | int | None = None, sujet: str | None = None, types: list[str] | None = None,
          limit: int = 40, today: date | None = None) -> dict[str, Any]:
    ensure_schema(conn)
    day = resolve_day(jour, today)
    kinds = [k for k in (types or ["side_event", "reunion"]) if k in KINDS] or list(KINDS)
    se_source = _side_event_source(conn)
    where = [f"kind IN ({','.join('?' * len(kinds))})", "(kind!='side_event' OR source=?)"]
    params: list[Any] = [*kinds, se_source]
    if day:
        where.append("days LIKE ?")
        params.append(f"%,{day},%")
    terms = [t.strip() for t in re.split(r"[,;]| OR ", sujet or "") if t.strip()]
    if terms:
        where.append("(" + " OR ".join(
            "(title LIKE ? OR summary LIKE ? OR organisers LIKE ? OR topics LIKE ? OR room LIKE ? OR type LIKE ?)"
            for _ in terms) + ")")
        for t in terms:
            params += [f"%{t}%"] * 6
    rows = [dict(r) for r in conn.execute(f"SELECT * FROM agenda_events WHERE {' AND '.join(where)}", params)]
    rows.sort(key=lambda r: (int(r["days"].strip(",").split(",")[0] or 0), _minutes(r["start"]), r["kind"]))
    total = len(rows)
    out_rows = []
    for r in rows[:limit]:
        days = [int(d) for d in r["days"].strip(",").split(",") if d]
        out_rows.append({k: v for k, v in {
            "jour": ", ".join(iso_day(d) for d in days), "debut": r["start"], "fin": r["end"], "lieu": r["room"],
            "titre": r["title"], "nature": KINDS[r["kind"]], "type": r["type"],
            "organisateurs": json.loads(r["organisers"] or "[]") or None,
            "themes": json.loads(r["topics"] or "[]") or None,
            "resume": (r["summary"] or "")[:400] or None, "acces": r["access"], "remarque": r["note"],
            "lien": r["link"], "source": {"seors": "SEORS (CCNUCC)", "planner": "COP31 Schedule Planner"}[r["source"]],
            "releve_le": r["fetched_at"],
        }.items() if v not in (None, "", [])})
    out: dict[str, Any] = {"jour": iso_day(day) if day else None, "evenements": out_rows, "total": total}
    if total > limit:
        out["tronque"] = f"{limit} premiers sur {total} : préciser le jour ou le sujet"
    if day:
        ov = conn.execute("SELECT text FROM agenda_overview WHERE day=?", (day,)).fetchone()
        if ov:
            out["negociations_du_jour"] = ov["text"] + " (résumé du calendrier d'ensemble CCNUCC, via le planner)"
    built = db.meta_get(conn, "agenda_planner_built")
    seors_at = db.meta_get(conn, "agenda_seors_at")
    out["a_verifier"] = (
        "Salles et horaires bougent : vérifier chaque side event sur le programme officiel SEORS "
        f"({seors_link(day)})"
        + (f", relevé importé le {seors_at}" if se_source == "seors" else f" ; ici copie du planner du {built or '?'}")
        + ". Réunions officielles : écrans du site, Daily Programme ou plateforme COP31 (connexion requise, "
        f"{PLATFORM_URL}). Les consultations informelles par point de l'ordre du jour ne figurent dans aucune de "
        "ces listes : seul le Daily Programme les donne.")
    st = seors_status(conn)
    if se_source == "planner" or (st["age_heures"] or 0) > 24:
        out["avertissement"] = (
            f"Programme des side events possiblement périmé : relevé SEORS du {st['releve_le'] or 'jamais'}"
            + (f" ({st['age_heures']:.0f} h)" if st["age_heures"] else "")
            + f", planner construit le {built or '?'}. Toujours vérifier salle et horaire sur SEORS.")
    out["sources"] = {"seors": seors_link(day), "planner": PLANNER_URL, "plateforme": PLATFORM_URL}
    return out


def log(msg: str) -> None:
    print(f"[programme] {msg}", file=sys.stderr, flush=True)
