"""Données chiffrées pour vérifier les affirmations (émissions, par habitant,
cumulées, part mondiale). Source : jeu de données CO2/GES d'Our World in Data,
qui compile Global Carbon Project, Jones et al., Climate Watch, etc."""

from __future__ import annotations

import csv
import io
from typing import Any

from . import db, fetch, ingest

OWID_URL = "https://raw.githubusercontent.com/owid/co2-data/master/owid-co2-data.csv"
INDICATORS = {
    "co2": ("MtCO2", "Émissions de CO2 fossiles + industrie (hors UTCATF)"),
    "co2_including_luc": ("MtCO2", "Émissions de CO2 y compris changement d'usage des terres"),
    "co2_per_capita": ("tCO2/hab", "CO2 par habitant"),
    "share_global_co2": ("%", "Part des émissions mondiales de CO2"),
    "cumulative_co2": ("MtCO2", "CO2 cumulé depuis 1750"),
    "share_global_cumulative_co2": ("%", "Part du CO2 cumulé mondial"),
    "total_ghg": ("MtCO2e", "GES totaux (y.c. UTCATF)"),
    "total_ghg_excluding_lucf": ("MtCO2e", "GES totaux hors UTCATF"),
    "ghg_per_capita": ("tCO2e/hab", "GES par habitant"),
    "coal_co2": ("MtCO2", "CO2 du charbon"),
    "oil_co2": ("MtCO2", "CO2 du pétrole"),
    "gas_co2": ("MtCO2", "CO2 du gaz"),
    "methane": ("MtCO2e", "Méthane"),
    "population": ("hab", "Population"),
    "gdp": ("USD 2011 PPA", "PIB"),
}
SOURCE = "OWID co2-data (Global Carbon Project et al.)"


def load_owid(conn) -> dict[str, Any]:
    client = fetch.Client()
    res = client.get(OWID_URL)
    if not res.ok:
        return {"erreur": f"{res.status} {res.error}"}
    ingest.register(conn, res.content, url=OWID_URL, mime="text/csv",
                    meta={"title": "Our World in Data — CO2 and GHG emissions dataset", "level": "3",
                          "org": "Our World in Data", "doc_type": "donnees"}, source_id="data-owid")
    reader = csv.DictReader(io.StringIO(res.content.decode("utf-8")))
    rows = []
    for r in reader:
        iso = r.get("iso_code") or f"X:{r['country']}"
        try:
            year = int(r["year"])
        except (KeyError, ValueError):
            continue
        if year < 1950:
            continue
        for ind, (unit, _) in INDICATORS.items():
            v = r.get(ind)
            if v not in (None, ""):
                rows.append((iso, r["country"], year, ind, float(v), unit, SOURCE))
    conn.execute("DELETE FROM emissions WHERE source=?", (SOURCE,))
    conn.executemany("INSERT OR REPLACE INTO emissions VALUES(?,?,?,?,?,?,?)", rows)
    conn.commit()
    return {"lignes": len(rows), "derniere_annee": max(r[2] for r in rows) if rows else None}


def load_all(conn) -> dict[str, Any]:
    return {"owid": load_owid(conn)}


# Noms français et variantes courantes → nom OWID (anglais).
ALIASES = {
    "türkiye": "Turkey", "turkiye": "Turkey", "turquie": "Turkey",
    "ue": "European Union (27)", "eu": "European Union (27)", "eu27": "European Union (27)", "ue27": "European Union (27)",
    "union européenne": "European Union (27)", "union europeenne": "European Union (27)", "european union": "European Union (27)",
    "monde": "World", "allemagne": "Germany", "chine": "China", "états-unis": "United States", "etats-unis": "United States",
    "usa": "United States", "us": "United States", "inde": "India", "japon": "Japan", "royaume-uni": "United Kingdom",
    "russie": "Russia", "brésil": "Brazil", "bresil": "Brazil", "arabie saoudite": "Saudi Arabia",
    "émirats arabes unis": "United Arab Emirates", "emirats arabes unis": "United Arab Emirates", "eau": "United Arab Emirates",
    "australie": "Australia", "espagne": "Spain", "italie": "Italy", "pologne": "Poland", "pays-bas": "Netherlands",
    "belgique": "Belgium", "suède": "Sweden", "suede": "Sweden", "norvège": "Norway", "norvege": "Norway", "suisse": "Switzerland",
    "afrique du sud": "South Africa", "mexique": "Mexico", "indonésie": "Indonesia", "indonesie": "Indonesia",
    "corée du sud": "South Korea", "coree du sud": "South Korea", "égypte": "Egypt", "egypte": "Egypt", "nigéria": "Nigeria",
    "azerbaïdjan": "Azerbaijan", "azerbaidjan": "Azerbaijan", "iran": "Iran", "irak": "Iraq", "grèce": "Greece", "grece": "Greece",
    "autriche": "Austria", "danemark": "Denmark", "finlande": "Finland", "irlande": "Ireland", "hongrie": "Hungary",
    "roumanie": "Romania", "tchéquie": "Czechia", "république tchèque": "Czechia", "nouvelle-zélande": "New Zealand",
    "colombie": "Colombia", "argentine": "Argentina", "chili": "Chile", "pérou": "Peru", "maroc": "Morocco", "algérie": "Algeria",
    "kazakhstan": "Kazakhstan", "ukraine": "Ukraine", "pays à revenu élevé": "High-income countries",
    "pays à faible revenu": "Low-income countries", "afrique": "Africa", "asie": "Asia", "europe": "Europe",
}


def query(conn, country: str, indicator: str | None = None, start: int | None = None,
          end: int | None = None) -> dict[str, Any]:
    c = country.strip()
    c = ALIASES.get(c.lower(), c)
    row = conn.execute(
        """SELECT DISTINCT iso3, country FROM emissions
           WHERE iso3=? COLLATE NOCASE OR country=? COLLATE NOCASE OR country LIKE ? LIMIT 5""",
        (c.upper(), c, f"%{c}%"),
    ).fetchall()
    # Correspondance exacte d'abord (« Niger » ne doit pas donner « Nigeria »).
    row = sorted(row, key=lambda r: (r["country"].lower() != c.lower() and r["iso3"] != c.upper(), len(r["country"])))
    if not row:
        return {"erreur": f"pays ou groupe inconnu : {country}",
                "astuce": "code ISO3 (FRA, CHN…) ou nom anglais (European Union (27), World, High-income countries…)"}
    iso = row[0]["iso3"]
    inds = [indicator] if indicator else ["co2", "co2_per_capita", "share_global_co2", "total_ghg", "ghg_per_capita", "share_global_cumulative_co2"]
    out: dict[str, Any] = {"pays": row[0]["country"], "iso3": iso, "source": SOURCE,
                           "url_source": "https://github.com/owid/co2-data", "series": {}}
    if len(row) > 1:
        out["autres_correspondances"] = [r["country"] for r in row[1:]]
    for ind in inds:
        params: list[Any] = [iso, ind]
        sql = "SELECT year, value, unit FROM emissions WHERE iso3=? AND indicator=?"
        if start:
            sql += " AND year>=?"
            params.append(start)
        if end:
            sql += " AND year<=?"
            params.append(end)
        series = conn.execute(sql + " ORDER BY year", params).fetchall()
        if not start and not end:
            series = [s for s in series if s["year"] in (1990, 2005, 2015) or s["year"] >= series[-1]["year"] - 5] if series else []
        if series:
            out["series"][ind] = {
                "definition": INDICATORS.get(ind, ("", ind))[1],
                "unite": series[0]["unit"],
                "valeurs": {s["year"]: round(s["value"], 3) for s in series},
            }
    return out
