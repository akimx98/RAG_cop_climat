"""Calendrier unifié de la COP31 pour la page « Plan my day » : side events officiels, sessions des pavillons
(Blue et Green Zones), événements hors site et réunions officielles du calendrier CCNUCC, filtrables par jour, mots,
type, zone, pavillon et « mes sujets ». Sans IA : correspondance par mots (frontières de mots), en mémoire.

Les side events, pavillons et événements hors site sont dans `pav_events` (les side events y sont recopiés depuis le
planner ou SEORS, clé « agenda:… ») ; les réunions officielles sont dans `agenda_events` (kind='reunion').
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import agenda, alerts, live

KINDS = {
    "official": ("Réunion officielle", "Official meeting"),
    "side_event": ("Side event", "Side event"),
    "pavilion": ("Pavillon", "Pavilion session"),
    "off_site": ("Hors site", "Off-site"),
}
DAYS = list(range(agenda.FIRST_DAY, agenda.LAST_DAY + 1))
MAX_ROWS = 700


def _kind(r: dict) -> str:
    if r["source"] == "planner_off":
        return "off_site"
    if r["key"].startswith("agenda:") or r["source"] in ("seors", "planner"):
        return "side_event"
    return "pavilion"


def _minutes(t: str | None) -> int:
    m = re.match(r"(\d{1,2})[:h.](\d{2})", t or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 24 * 60


def _load(conn, day: int | None) -> list[dict[str, Any]]:
    from . import pavillons

    pavillons.ensure_schema(conn)
    agenda.ensure_schema(conn)
    out: list[dict[str, Any]] = []
    where, params = "cancelled=0", []
    if day:
        where += " AND date=?"
        params.append(agenda.iso_day(day))
    for r in conn.execute(f"SELECT * FROM pav_events WHERE {where}", params):
        r = dict(r)
        speakers = [s.get("name") for s in json.loads(r["speakers"] or "[]") if s.get("name")]
        tags = [t for t in json.loads(r["themes"] or "[]") + json.loads(r["keywords"] or "[]") if t][:8]
        out.append({
            "key": r["key"], "kind": _kind(r), "date": r["date"], "start": r["start"], "end": r["end"],
            "title": r["title"] or "", "place": r["pavilion"] or r["area"] or "", "zone": r["zone"],
            "organizer": r["organizer"] or "", "summary": r["description"] or "", "speakers": speakers[:12],
            "tags": tags, "link": r["link"] or "",
        })
    se_day = f"%,{day},%" if day else "%"
    for r in conn.execute("SELECT * FROM agenda_events WHERE kind='reunion' AND days LIKE ?", (se_day,)):
        r = dict(r)
        days = [int(d) for d in (r["days"] or "").strip(",").split(",") if d]
        out.append({
            "key": "agenda:" + r["key"], "kind": "official", "date": agenda.iso_day(days[0]) if days else None,
            "start": r["start"], "end": r["end"], "title": r["title"] or "", "place": r["room"] or "",
            "zone": "blue", "organizer": ", ".join(json.loads(r["organisers"] or "[]")),
            "summary": r["summary"] or r["note"] or "", "speakers": [], "tags": json.loads(r["topics"] or "[]")[:8],
            "link": r["link"] or agenda.seors_link(days[0] if days else None),
            "type": r["type"],
        })
    return out


def _terms_regex(terms: list[str]) -> re.Pattern | None:
    terms = [t.strip() for t in terms if t and len(t.strip()) >= 2]
    if not terms:
        return None
    alts = sorted({re.escape(t.lower()) for t in terms}, key=len, reverse=True)
    return re.compile(r"(?<![\w])(" + "|".join(alts) + r")(?![\w])", re.I)


def topic_terms(conn, user: dict) -> list[str]:
    """Mots des sujets de l'utilisateur : ses requêtes par mots-clés et, pour chaque dossier suivi, ses alias et sa
    requête (syntaxe des sujets : expressions séparées par des virgules)."""
    terms: list[str] = []
    for s in alerts.list_subscriptions(conn, user["id"]):
        terms += [t for t in re.split(r"[,;\n]+|\bOR\b", s["requete"]) if t.strip() and t.strip() != "*"]
    followed = live.followed(conn, user["id"])
    for d in live.dossiers():
        if d["slug"] in followed:
            terms += [a for a in d.get("alias", []) if len(a) >= 3]
            terms += re.split(r"[,;]+", d.get("requete") or "")
    return [re.sub(r'["*()]', "", t).strip() for t in terms if t.strip()]


def query(conn, day: int | None, q: str = "", kinds: list[str] | None = None, zone: str = "", place: str = "",
          terms: list[str] | None = None) -> dict[str, Any]:
    rows = _load(conn, day)
    places: dict[str, int] = {}
    for r in rows:
        if r["kind"] == "pavilion" and r["place"]:
            places[r["place"]] = places.get(r["place"], 0) + 1
    if kinds:
        rows = [r for r in rows if r["kind"] in kinds]
    if zone:
        rows = [r for r in rows if r["zone"] == zone]
    if place:
        rows = [r for r in rows if r["place"] == place]

    def text(r: dict) -> str:
        return " ".join([r["title"], r["summary"], r["organizer"], r["place"], " ".join(r["speakers"]),
                         " ".join(map(str, r["tags"]))])

    rx_q = _terms_regex([t for t in re.split(r"[,;]+", q or "")])
    if rx_q:
        rows = [r for r in rows if rx_q.search(text(r))]
    rx_t = _terms_regex(terms or []) if terms is not None else None
    if terms is not None:
        rows = [r for r in rows if rx_t and rx_t.search(text(r))]
        for r in rows:
            r["matched"] = sorted({m.group(1).lower() for m in rx_t.finditer(text(r))})[:4]
    rows.sort(key=lambda r: (r["date"] or "", _minutes(r["start"]), r["kind"] != "official", r["title"]))
    total = len(rows)
    return {"events": rows[:MAX_ROWS], "total": total, "truncated": total > MAX_ROWS,
            "places": sorted(places.items(), key=lambda kv: kv[0].lower())}


def by_slot(events: list[dict]) -> list[tuple[str, list[dict]]]:
    """Groupes par jour et heure de début (« 2026-11-18 · 11:30 »)."""
    groups: list[tuple[str, list[dict]]] = []
    for e in events:
        label = f"{e['date'] or ''}|{(e['start'] or '')[:5] or '—'}"
        if groups and groups[-1][0] == label:
            groups[-1][1].append(e)
        else:
            groups.append((label, [e]))
    return groups
