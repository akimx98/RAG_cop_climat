"""Textes de session publiés sur docs.unfccc.int, repérés sans attendre l'index de Carbon Brief.

Chaque document de session a une fiche numérotée à la suite, ``https://docs.unfccc.int/documents/<n°>`` (le projet
sur le Fonds pour l'adaptation du 18/11 est le n° 10000349). Ce site n'a pas d'anti-bot : la fiche donne la cote,
le titre, la date et l'heure de publication, le type, la session, le point de l'ordre du jour et le lien du PDF.

Toutes les 10 minutes, on essaie les numéros qui suivent le plus haut déjà vu, jusqu'à quelques fiches absentes
d'affilée (404 : pas encore publié). Une fiche de la conférence d'Antalya devient un texte annoncé (``live_texts``),
avec la même clé que celle de l'index de Carbon Brief (cote, sinon adresse du PDF) : la récupération, le
rattachement au dossier et les alertes suivent le circuit habituel, et l'index de Carbon Brief, quand il arrive,
complète la même ligne (thème, crochets, options, note). Voie de secours si l'index tarde ou s'arrête.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from . import db, fetch, live

BASE = "https://docs.unfccc.int/documents/{n}"
FIRST = 10000253          # plus ancien texte de session de la COP31 relevé par l'index de Carbon Brief
CONFERENCE = "Antalya"    # fiches retenues : celles de la conférence d'Antalya
META_KEY = "docs_unfccc_last"
_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def parse_landing(html: str, url: str) -> dict[str, Any] | None:
    """Champs d'une fiche docs.unfccc.int (None si ce n'est pas une fiche de document)."""
    soup = BeautifulSoup(html, "lxml")
    lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]

    def after(label: str) -> str | None:
        for i, l in enumerate(lines[:-1]):
            if l.lower() == label.lower():
                return lines[i + 1]
        return None

    pdf = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/files/en/" in href and href.lower().endswith(".pdf"):
            pdf = urljoin(url, quote(href, safe="/:%"))
            break
    if not pdf:
        for a in soup.find_all("a", href=True):
            if "/files/" in a["href"] and a["href"].lower().endswith(".pdf"):
                pdf = urljoin(url, quote(a["href"], safe="/:%"))
                break
    title = None
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = " ".join(h1.get_text(" ").split())
    symbol = after("Symbol")
    published = None
    m = re.search(r"(\d{1,2}) (\w{3}) (20\d\d) (\d{1,2}:\d{2})", after("Publication date and time") or "")
    if m and m.group(2).lower()[:3] in _MONTHS:
        published = f"{m.group(3)}-{_MONTHS[m.group(2).lower()[:3]]:02d}-{int(m.group(1)):02d}T{m.group(4)}"
    conference = after("Conference") or ""
    sessions = []
    for i, l in enumerate(lines):
        if l == "Session":
            for x in lines[i + 1:i + 6]:
                if re.fullmatch(r"(COP|CMA|CMP|SBI|SBSTA) \d+", x):
                    sessions.append(x)
                elif x not in (",",):
                    break
    agenda = None
    for i, l in enumerate(lines):
        if l == "Agenda item" and i + 2 < len(lines):
            m2 = re.match(r"([\d]+(?:\s*\(\w\)|\s*\w)?)\.", lines[i + 2])
            agenda = m2.group(1).replace("(", " ").replace(")", "").strip() if m2 else None
            break
    if not (title or symbol) or not pdf:
        return None
    return {"symbol": symbol, "title": title or symbol, "published": published, "doc_type": after("Document type"),
            "conference": conference, "body": sessions[0] if sessions else None, "agenda_item": agenda, "pdf_url": pdf}


def _highest_seen(conn) -> int:
    hi = int(db.meta_get(conn, META_KEY) or 0)
    for r in conn.execute("SELECT pdf_url FROM live_texts WHERE pdf_url LIKE 'https://docs.unfccc.int/documents/%'"):
        m = re.search(r"/documents/(\d+)/", r["pdf_url"] or "")
        if m:
            hi = max(hi, int(m.group(1)))
    return max(hi, FIRST - 1)


def sync(conn, client: fetch.Client, stop_after: int = 3, max_probe: int = 25) -> dict[str, int]:
    """Essaie les fiches qui suivent la plus haute connue ; les nouvelles fiches de la COP31 deviennent des textes
    annoncés. Une requête par numéro, arrêt après ``stop_after`` absents d'affilée."""
    live.ensure_schema(conn)
    counts = {"nouveaux": 0, "connus": 0, "autres": 0, "absents": 0}
    n, misses, last_found = _highest_seen(conn) + 1, 0, None
    for _ in range(max_probe):
        if misses >= stop_after:
            break
        url = BASE.format(n=n)
        res = client.get(url)
        if res.status == 404 or not res.ok or fetch.is_antibot(res):
            misses += 1
            counts["absents"] += 1
            n += 1
            continue
        misses, last_found = 0, n
        d = parse_landing(res.content.decode("utf-8", "replace"), url)
        if not d or CONFERENCE.lower() not in d["conference"].lower():
            counts["autres"] += 1
            n += 1
            continue
        key = d["symbol"] or d["pdf_url"]
        old = conn.execute("SELECT id FROM live_texts WHERE key=? OR pdf_url=?", (key, d["pdf_url"])).fetchone()
        if old:
            counts["connus"] += 1
        else:
            conn.execute(
                """INSERT INTO live_texts(key,body,agenda_item,symbol,title,cb_type,published,pdf_url,dossier,
                     note,first_seen,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, d["body"], d["agenda_item"], d["symbol"], d["title"], d["doc_type"], d["published"], d["pdf_url"],
                 live.guess_dossier(d["title"]), "repéré sur docs.unfccc.int avant l'index de Carbon Brief",
                 live.now_iso(), live.now_iso()))
            counts["nouveaux"] += 1
        n += 1
    if last_found:
        db.meta_set(conn, META_KEY, str(last_found))
    conn.commit()
    return counts
