"""Programmes de pavillons publiés hors de cop31.tr (absents ou sans résumé ni intervenants sur cop31.tr).

Chaque collecteur renvoie des événements au format de pavillons.py. Requêtes HTTP simples avec l'agent
utilisateur du projet ; une source protégée par un anti-bot, une connexion ou rendue seulement par JavaScript
n'est pas collectée (Brésil, Royaume-Uni, BMD, pavillon nordique, Danemark…).
Les événements déjà présents sur cop31.tr (même pavillon, même jour, titre proche) servent à compléter la fiche
cop31.tr (résumé, intervenants) au lieu d'être dupliqués : voir `pavillons.merge_external`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import fetch
from .pavillons import extract_speakers, html_to_text

MONTHS = {"nov": 11, "november": 11, "oct": 10, "dec": 12}


def _ev(source: str, pavilion: str, date: str, start: str | None, end: str | None, title: str, desc: str,
        speakers: list[dict] | None, link: str, organizer: str | None = None, **kw: Any) -> dict[str, Any]:
    import hashlib

    h = hashlib.sha1(f"{date}|{start}|{title}".encode()).hexdigest()[:12]
    return dict(key=f"ext:{source}:{h}", source=f"ext:{source}", zone=kw.get("zone", "blue"), date=date, start=start,
                end=end, area=kw.get("area", "Area B2 Delegation Pavilions"), pavilion=pavilion, organizer=organizer,
                title=re.sub(r"\s+", " ", title).strip(), description=desc.strip(),
                speakers=speakers if speakers is not None else extract_speakers(desc),
                session_type=kw.get("session_type"), themes=kw.get("themes") or [], subtopics=[], keywords=[],
                organiser_types=[], participation=[], audience=[], cancelled=0, link=link)


def _main_text(page: str) -> str:
    page = re.sub(r"<script.*?</script>|<style.*?</style>|<svg.*?</svg>|<nav.*?</nav>|<footer.*?</footer>", "",
                  page, flags=re.S)
    m = re.search(r"<main.*?</main>", page, re.S)
    return html_to_text(m.group(0) if m else page)


def _people_blocks(lines: list[str], role: str) -> list[dict[str, str]]:
    """Blocs « - / Nom / Fonction / Organisation » (une personne par bloc)."""
    out, cur = [], []
    for ln in lines + ["-"]:
        if ln.strip() in ("-", ""):
            if cur:
                out.append({"role": role, "name": ", ".join(cur)})
            cur = []
        else:
            cur.append(ln.strip())
    return out


# --- Pavillon allemand (iveo : une page par session, rendue côté serveur) ---------------------------------

DE_BASE = "https://www.german-climate-pavilion.de"


def germany(client: fetch.Client) -> list[dict[str, Any]]:
    res = client.get(DE_BASE + "/sitemap.xml")
    if not res.ok:
        raise RuntimeError(f"sitemap HTTP {res.status}")
    urls = sorted(set(re.findall(r"<loc>([^<]*/program/[0-9a-f-]{36})</loc>", res.content.decode())))
    out = []
    for url in urls:
        r = client.get(url)
        if not r.ok:
            continue
        t = _main_text(r.content.decode("utf-8", "replace"))
        m = re.search(r"\w+day, (\d{1,2}) (\w+) (\d{4}) (\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2})", t)
        if not m:
            continue
        date = f"{m.group(3)}-{MONTHS.get(m.group(2).lower(), 11):02d}-{int(m.group(1)):02d}"
        rest = [x for x in t[m.end():].split("\n")]
        rest = rest[1:] if rest and rest[0].startswith(" (") or (rest and "UTC" in rest[0]) else rest
        body = "\n".join(rest).strip()
        title, _, body = body.partition("\n")
        desc, _, people = re.split(r"\n(?=Speakers\n|Moderat\w*\n)", body, 1)[0], "", ""
        speakers: list[dict] = []
        for head, block in re.findall(r"\n(Speakers|Moderators?|Moderation|Hosts?)\n(.*?)(?=\n(?:Speakers|Moderators?|Moderation|Hosts?)\n|$)",
                                      "\n" + body, re.S):
            speakers += _people_blocks(block.split("\n"), "moderator" if head.startswith("Moder") else "speaker")
        desc = re.sub(r"^\s*- [^\n]+\n", "", desc)  # étiquette de thème
        out.append(_ev("germany", "Germany Pavilion (German Climate Pavilion)", date, m.group(4), m.group(5), title,
                       desc, speakers, url, "BMUKN / BMZ"))
    return out


# --- Pavillon de l'UE (data.json + pages détail climate.ec.europa.eu) -------------------------------------

EU_JSON = "https://ec.europa.eu/clima/sites/cop_side_events/data/data.json"


def eu(client: fetch.Client) -> list[dict[str, Any]]:
    res = client.get(EU_JSON)
    if not res.ok:
        raise RuntimeError(f"data.json HTTP {res.status}")
    data = json.loads(res.content)
    out = []
    for day in data.get("days") or []:
        d = re.match(r"(\d{2})/(\d{2})/(\d{4})", day.get("short") or "")
        if not d:
            continue
        date = f"{d.group(3)}-{d.group(2)}-{d.group(1)}"
        for e in day.get("events") or []:
            desc, speakers = html_to_text(e.get("description") or ""), []
            link = e.get("link") or "https://climate.ec.europa.eu/"
            if e.get("link"):
                r = client.get(e["link"])
                if r.ok:
                    page = r.content.decode("utf-8", "replace")
                    t = _main_text(page)
                    t = re.sub(r"^.*?This event has ended\.\s*", "", t, flags=re.S)
                    desc = desc or re.split(r"\n\s*\w+day \d{1,2} \w+ \d{4}, \d|\nSpeakers\n|\nPractical information", t)[0]
                    desc = re.sub(r"\n?Recording of the session can be accessed here\.?", "", desc)
                    speakers = _eu_people(page)
            out.append(_ev("eu", "European Union Pavilion", date, e.get("startTime"), e.get("endTime"), e["title"],
                           desc, speakers, link, e.get("organiser"), session_type=e.get("type"),
                           themes=[e["thematic"]] if e.get("thematic") else []))
    return out


def _eu_people(page: str) -> list[dict[str, str]]:
    """Intervenants : liste schema.org (noms) complétée des fonctions lues dans la page."""
    names = []
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        for g in (d.get("@graph") or [d]) if isinstance(d, dict) else d:
            for p in (g.get("performer") or []) if isinstance(g, dict) else []:
                if isinstance(p, dict) and p.get("@type") == "Person" and p.get("name"):
                    names.append(p["name"])
    text = _main_text(page)
    mod = text.split("\nModerator", 1)[1] if "\nModerator" in text else ""
    out = []
    for n in dict.fromkeys(names):
        m = re.search(re.escape(n) + r"\s*([^\n]*)", text)
        role = "moderator" if n in mod.split("Practical information")[0] else "speaker"
        out.append({"role": role, "name": n + (", " + m.group(1).strip() if m and m.group(1).strip() else "")})
    return out


# --- Pavillon de Singapour (Gevme : tableaux par jour dans la page) --------------------------------------

SG_URL = "https://www.cop-pavilion.gov.sg/site/programme-schedule"


def singapore(client: fetch.Client) -> list[dict[str, Any]]:
    res = client.get(SG_URL)
    if not res.ok:
        raise RuntimeError(f"HTTP {res.status}")
    page = res.content.decode("utf-8", "replace")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if m:
        props = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
        page = (props.get("headTag") or "") + (props.get("bodyTag") or "") or page
    out = []
    for tm in re.finditer(r"<table.*?</table>", page, re.S):
        head = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "\n", page[max(0, tm.start() - 3000):tm.start()]).replace("\n", " | "))
        dm = re.findall(r"\b(\d{1,2})[\s|]+nov\b[\s|]*([^|]{0,80})", head, re.I)
        if not dm:
            continue
        day, theme = dm[-1]
        theme = re.sub(r"\s+", " ", html_to_text(theme)).strip()
        for row in re.findall(r"<tr.*?</tr>", tm.group(0), re.S):
            cells = re.findall(r"<td.*?</td>", row, re.S)
            if len(cells) < 4:
                continue

            def egypt(c: str) -> str | None:
                x = re.search(r'time-egypt">\s*(\d{1,2}:\d{2})', c) or re.search(r"(\d{1,2}:\d{2})", c)
                return x.group(1) if x else None

            title = html_to_text(re.sub(r"<p>.*?</p>", "", cells[2], flags=re.S)).split("\n")[0]
            if not title:
                continue
            desc = html_to_text(cells[3])
            ppl = html_to_text(cells[4]) if len(cells) > 4 else ""
            sp = extract_speakers(("Speakers:\n" if not re.match(r"\s*\w+[^:\n]{0,30}:", ppl) else "") + ppl)
            out.append(_ev("singapore", "Singapore Pavilion", f"2026-11-{int(day):02d}", egypt(cells[0]),
                           egypt(cells[1]), title, desc, sp, SG_URL, "Singapore", themes=[theme] if theme else []))
    return out


# --- Pavillon de la cryosphère (ICCI : planning en texte, un onglet par jour) -------------------------------

ICCI_URL = "https://iccinet.org/cop31-cryosphere-pavilion/schedule/"


def cryosphere(client: fetch.Client) -> list[dict[str, Any]]:
    res = client.get(ICCI_URL)
    if not res.ok:
        raise RuntimeError(f"HTTP {res.status}")
    t = _main_text(res.content.decode("utf-8", "replace"))
    out = []
    for dm in re.finditer(r"\n\w+day Nov\.? (\d{1,2}) \| ([^\n]+)\n(.*?)(?=\n\w+day Nov\.? \d{1,2} \||\nDaily Schedule|$)", t, re.S):
        day, theme, block = int(dm.group(1)), dm.group(2).strip(), dm.group(3)
        for em in re.finditer(r"(?:^|\n)(\d{1,2}:\d{2})\s+([^\n]+)((?:\n(?!\d{1,2}:\d{2}\s)[^\n]+)*)", block):
            title = em.group(2).strip()
            if re.search(r"set-up|setup|break|closed|rest day", title, re.I):
                continue
            lines = [x.strip() for x in em.group(3).split("\n") if x.strip()]
            org = lines[-1] if len(lines) >= 2 else None
            desc_lines = lines[:-1] if org else lines
            desc = "\n".join(desc_lines)
            sp = [] if len(desc) > 200 else extract_speakers("Speakers: " + desc) if desc and re.search(r"[,;]", desc) else []
            out.append(_ev("cryosphere", "Cryosphere Pavilion (ICCI)", f"2026-11-{day:02d}", em.group(1), None,
                           title, desc, sp, ICCI_URL, org, themes=[theme]))
    return out


# --- Asia Climate Solutions Pavilion (calendrier Luma public) ---------------------------------------------

LUMA_CAL = "cal-D6FxPF3EHNcsNZY"


def asia_climate_solutions(client: fetch.Client) -> list[dict[str, Any]]:
    items: list[dict] = []
    for period in ("past", "future"):
        cursor = None
        for _ in range(10):
            url = (f"https://api.lu.ma/calendar/get-items?calendar_api_id={LUMA_CAL}&period={period}"
                   f"&pagination_limit=50" + (f"&pagination_cursor={cursor}" if cursor else ""))
            r = client.get(url)
            if not r.ok:
                break
            d = json.loads(r.content)
            items += d.get("entries") or []
            cursor = d.get("next_cursor")
            if not d.get("has_more") or not cursor:
                break
    out, seen = [], set()
    for it in items:
        ev = it.get("event") or {}
        if not ev.get("api_id") or ev["api_id"] in seen:
            continue
        seen.add(ev["api_id"])
        start = datetime.fromisoformat(ev["start_at"].replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=3)))
        if (start.year, start.month) != (2026, 11):
            continue
        end = ev.get("end_at") and datetime.fromisoformat(ev["end_at"].replace("Z", "+00:00")).astimezone(
            timezone(timedelta(hours=3)))
        desc, hosts = "", []
        r = client.get(f"https://api.lu.ma/event/get?event_api_id={ev['api_id']}")
        if r.ok:
            d = json.loads(r.content)
            desc = _prosemirror(d.get("description_mirror"))
            hosts = [h.get("name") for h in d.get("hosts") or [] if h.get("name")]
        out.append(_ev("asia_climate_solutions", "Asia Climate Solutions Pavilion (SFOC)", start.date().isoformat(),
                       start.strftime("%H:%M"), end.strftime("%H:%M") if end else None, ev.get("name") or "", desc,
                       None, f"https://luma.com/{ev.get('url')}", "; ".join(hosts) or "Solutions for Our Climate"))
    return out


def _prosemirror(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text") or ""
    inner = "".join(_prosemirror(c) for c in node.get("content") or [])
    if node.get("type") in ("paragraph", "heading", "list_item", "listItem"):
        return ("- " if node["type"] in ("list_item", "listItem") else "") + inner.strip() + "\n"
    if node.get("type") == "hard_break":
        return "\n"
    return inner


COLLECTORS: dict[str, tuple[Callable[[fetch.Client], list[dict]], str]] = {
    # nom : (collecteur, motif du pavillon correspondant sur cop31.tr pour fusionner les doublons)
    "germany": (germany, "Germany Pavilion"),
    "eu": (eu, "European Union Pavilion"),
    "singapore": (singapore, "Singapore"),
    "cryosphere": (cryosphere, "Cryosphere"),
    "asia_climate_solutions": (asia_climate_solutions, "Solutions for Our Climate (SFOC)"),
}
