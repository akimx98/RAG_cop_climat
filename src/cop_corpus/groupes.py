"""Groupes de négociation de la CCNUCC : qui est membre de quoi, avec une SOURCE et une date pour chaque appartenance.

Définition des groupes et de leur source : catalog/groupes.yaml. Règle : pas de liste publique sourcée → pas de
membres ; le groupe est alors « connu, liste non vérifiée » (nombre annoncé par la CCNUCC), jamais complété de
mémoire. Une liste dont le nombre de membres diffère du nombre attendu est marquée « ecart ».

Les noms de pays (variantes onusiennes : « Naoero », « St. Lucia », « Lao People's Dem. Republic »…) sont ramenés
à un code ISO3 via les noms de la table `emissions` (Our World in Data) et la table VARIANTES ci-dessous ; un nom non
résolu est gardé tel quel et signalé (`non_resolus`), jamais écarté en silence. Le code ISO3 relie les membres aux
soumissions (noms officiels de la CCNUCC) et aux NDC (codes ISO3).
"""

from __future__ import annotations

import re
import sys
import unicodedata
from typing import Any

import yaml
from bs4 import BeautifulSoup

from . import config, db, fetch

SCHEMA = """
CREATE TABLE IF NOT EXISTS grp_groups (
  id TEXT PRIMARY KEY, nom TEXT, type TEXT, source TEXT, attendu INTEGER, n INTEGER, statut TEXT,
  recupere_le TEXT, erreur TEXT, non_resolus TEXT
);
CREATE TABLE IF NOT EXISTS grp_members (
  groupe TEXT, iso3 TEXT, nom TEXT, nom_source TEXT, depuis TEXT, source TEXT, recupere_le TEXT,
  PRIMARY KEY (groupe, nom_source)
);
CREATE INDEX IF NOT EXISTS grp_members_iso3 ON grp_members(iso3);
"""

# Variantes de noms (sources onusiennes, CCNUCC, AOSIS, G77) → ISO3. Complète les noms d'Our World in Data.
VARIANTES = {
    "naoero": "NRU", "nauru": "NRU", "st lucia": "LCA", "st. lucia": "LCA", "saint lucia": "LCA",
    "st kitts and nevis": "KNA", "st. kitts and nevis": "KNA", "saint kitts and nevis": "KNA",
    "st vincent & grenadines": "VCT", "st. vincent and the grenadines": "VCT", "saint vincent and the grenadines": "VCT",
    "sao tome & principe": "STP", "sao tome and principe": "STP", "micronesia": "FSM",
    "micronesia (federated states of)": "FSM", "lao people's dem. republic": "LAO",
    "lao people's democratic republic": "LAO", "democratic rep. of the congo": "COD",
    "democratic republic of the congo": "COD", "congo": "COG", "republic of the congo": "COG",
    "united republic of tanzania": "TZA", "plurinational state of bolivia": "BOL", "bolivia (plurinational state of)": "BOL",
    "republic of moldova": "MDA", "iran (islamic republic of)": "IRN", "venezuela (bolivarian republic of)": "VEN",
    "democratic people's republic of korea": "PRK", "republic of korea": "KOR", "the republic of korea": "KOR",
    "viet nam": "VNM", "syrian arab republic": "SYR", "state of palestine": "PSE", "brunei darussalam": "BRN",
    "cote d'ivoire": "CIV", "côte d'ivoire": "CIV", "cabo verde": "CPV", "eswatini": "SWZ", "dominicanrepublic": "DOM",
    "lebanese republic": "LBN", "comoros": "COM", "libya": "LBY",
    "czechia": "CZE", "czech republic": "CZE", "holy see": "VAT", "united arab emirates": "ARE", "the netherlands": "NLD", "netherlands": "NLD", "united states": "USA",
    "united states of america": "USA", "the united states": "USA", "united kingdom": "GBR",
    "united kingdom of great britain and northern ireland": "GBR", "türkiye": "TUR", "turkiye": "TUR",
    "russian federation": "RUS", "north macedonia": "MKD", "timor-leste": "TLS", "gambia": "GMB",
    "cook islands": "COK", "niue": "NIU", "european union": None,
}

UNFCCC_PAGE = "https://unfccc.int/process-and-meetings/parties-non-party-stakeholders/parties/party-groupings"


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


def log(msg: str) -> None:
    print(f"[groupes] {msg}", file=sys.stderr, flush=True)


def catalog() -> list[dict[str, Any]]:
    path = config.CATALOG_DIR / "groupes.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else []


def _key(name: str) -> str:
    s = unicodedata.normalize("NFKD", (name or "").replace("’", "'")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"^the\s+", "", re.sub(r"\s+", " ", s)).strip(" .*")
    return s


def iso_index(conn) -> dict[str, str]:
    """Nom (normalisé) → ISO3, depuis Our World in Data (table emissions) et VARIANTES."""
    idx: dict[str, str] = {}
    try:
        for iso, country in conn.execute("SELECT DISTINCT iso3, country FROM emissions WHERE length(iso3)=3"):
            idx[_key(country)] = iso
    except Exception:
        pass
    idx.update({k: v for k, v in VARIANTES.items() if v})
    return idx


STATE_FORMS = re.compile(r"^(hashemite kingdom of|kingdom of|republic of|people's democratic republic of|"
                         r"federal republic of|sultanate of|state of|arab republic of|islamic republic of|"
                         r"federal democratic republic of|union of)\s+")


def to_iso3(name: str, idx: dict[str, str]) -> str | None:
    """ISO3 d'un nom de pays. Une parenthèse n'est JAMAIS simplement retirée (« Congo (Democratic Republic of the) »
    n'est pas le Congo) : forme onusienne « X (Y of [the]) » réécrite en « Y of [the] X » ; seules « (the) » et un
    sigle (« (UAE) ») sont retirés."""
    k = _key(name)
    if k in idx:
        return idx[k]
    m = re.fullmatch(r"(.+?)\s*\((.+)\)", k)
    if not m:
        # Forme protocolaire (« Hashemite Kingdom of Jordan », « Sultanate of Oman ») : en dernier recours seulement,
        # après les noms exacts et VARIANTES (« Republic of Korea », « Republic of Moldova » y sont déjà).
        short = STATE_FORMS.sub("", k)
        return idx.get(short) if short != k else None
    base, inner = m.group(1).strip(), m.group(2).strip()
    if inner == "the" or re.fullmatch(r"[a-z]{2,5}", inner) and inner.upper() == re.sub(r"[^A-Z]", "", name.split("(")[-1]):
        return idx.get(base)
    if re.search(r"\b(of|of the)$", inner):  # « Democratic Republic of the » + « Congo »
        return idx.get(f"{inner} {base}")
    return None


# --- lecture des sources --------------------------------------------------------------------------------------------

def _text(html: bytes | str) -> str:
    return " ".join(BeautifulSoup(html, "lxml").get_text(" ").split())


def parse_g77(html) -> list[tuple[str, str | None]]:
    strs = [" ".join(x.split()) for x in BeautifulSoup(html, "lxml").stripped_strings]
    head = [i for i, x in enumerate(strs) if re.search(r"member states of the group of 77|printer ?friendly", x, re.I)]
    start = head[-1] + 1 if head else 0
    return [(x, None) for x in strs[start:] if not re.match(r"back|top|printer", x, re.I) and len(x) < 60]


def parse_eu(html) -> list[tuple[str, str | None]]:
    return [(n.strip(), y) for n, y in re.findall(r"([A-Z][A-Za-zà-ÿ' -]+?) EU Member State since (\d{4})", _text(html))]


def parse_ohrlls_ldc(html) -> list[tuple[str, str | None]]:
    soup = BeautifulSoup(html, "lxml")
    anchor = soup.find(string=lambda t: t and t.strip() == "Afghanistan")
    if not anchor:
        return []
    node = anchor.parent
    while node.parent is not None and len(list(node.stripped_strings)) < 20:
        node = node.parent
    return [(x.strip(" *"), None) for x in node.stripped_strings if 2 < len(x) < 60]


def parse_ohrlls_numbered(html) -> list[tuple[str, str | None]]:
    t = _text(html)
    t = t.split("Associate Members")[0]  # SIDS : les membres associés des commissions régionales ne sont pas des États
    found = re.findall(r"(\d{1,2})\.\s+(.+?)\s*\*?(?=\s+\d{1,2}\.\s|\s+\*?\s*Also Least|\s*$)", t)
    by_num = {}
    for num, name in found:
        by_num.setdefault(int(num), name.strip(" *"))
    return [(by_num[k], None) for k in sorted(by_num)]


def parse_aosis(html) -> list[tuple[str, str | None]]:
    t = _text(html)
    names = re.findall(r"[\U0001F1E6-\U0001F1FF]{2}\s*([^\U0001F1E6-\U0001F1FF]+?)(?=\s*[\U0001F1E6-\U0001F1FF]{2}|"
                       r"\s+(?:Pacific|Africa, Indian Ocean (?:&|and) South China Sea|Caribbean)\s+\d|\s*$)", t)
    return [(re.split(r"\s+[A-Z]{3,}\b|\s+\d", n)[0].strip(), None) for n in names]


def parse_un_african_states(html) -> list[tuple[str, str | None]]:
    for tbl in BeautifulSoup(html, "lxml").find_all("table"):
        cells = [" ".join(c.get_text(" ", strip=True).split()).rstrip("*") for c in tbl.find_all("td")]
        cells = [c for c in cells if c]
        if "Algeria" in cells and "Zimbabwe" in cells:
            return [(c, None) for c in cells]
    return []


def parse_las(html) -> list[tuple[str, str | None]]:
    soup = BeautifulSoup(html, "lxml")
    names = [" ".join(a.get_text(" ", strip=True).split()) for a in soup.find_all("a")
             if "CountryDataDetails" in (a.get("href") or "")]
    return [(n, None) for n in dict.fromkeys(n for n in names if n)]


PARSERS = {"g77": parse_g77, "eu": parse_eu, "ohrlls_ldc": parse_ohrlls_ldc, "ohrlls_numbered": parse_ohrlls_numbered,
           "aosis": parse_aosis, "un_african_states": parse_un_african_states, "las": parse_las}
EXTRA_PAGES = {"eu": ["?page=1"]}  # liste paginée (20 par page)


def _expected(g: dict) -> list[int]:
    a = g.get("attendu")
    return [int(x) for x in a] if isinstance(a, list) else ([int(a)] if a else [])


def _statut(g: dict, n: int) -> str:
    if _expected(g) and n not in _expected(g):
        return "ecart"
    return "verifie_par_equivalence" if g.get("equivalence") else "verifie"


def sync(conn, client: fetch.Client) -> dict[str, Any]:
    """Relève toutes les listes sourcées. Une source en échec garde la dernière liste connue (statut « erreur »)."""
    ensure_schema(conn)
    idx = iso_index(conn)
    pages: dict[str, bytes] = {}
    out: dict[str, Any] = {}
    now = db.now()
    for g in catalog():
        gid, parser = g["id"], g.get("parser")
        base = {"nom": g["nom"], "type": g["type"], "source": g["source"],
                "attendu": (_expected(g) or [None])[0]}
        if g.get("membres"):  # relevé daté, recopié d'une phrase citée de la source
            since = g.get("depuis") or {}
            rows = [(gid, to_iso3(n, idx), n, n, since.get(n), g["source"], str(g.get("releve")))
                    for n in g["membres"]]
            unresolved = [r[2] for r in rows if not r[1]]
            statut = _statut(g, len(rows))
            conn.execute("DELETE FROM grp_members WHERE groupe=?", (gid,))
            conn.executemany("INSERT OR REPLACE INTO grp_members(groupe,iso3,nom,nom_source,depuis,source,recupere_le) "
                             "VALUES(?,?,?,?,?,?,?)", rows)
            conn.execute("""INSERT INTO grp_groups(id,nom,type,source,attendu,n,statut,recupere_le,erreur,non_resolus)
                            VALUES(?,?,?,?,?,?,?,?,NULL,?) ON CONFLICT(id) DO UPDATE SET nom=excluded.nom,
                              type=excluded.type, source=excluded.source, attendu=excluded.attendu, n=excluded.n,
                              statut=excluded.statut, recupere_le=excluded.recupere_le, erreur=NULL,
                              non_resolus=excluded.non_resolus""",
                         (gid, base["nom"], base["type"], base["source"], base["attendu"], len(rows), statut,
                          str(g.get("releve")), db.jdump(unresolved)))
            out[gid] = f"{statut} {len(rows)} (relevé du {g.get('releve')})"
            continue
        if not parser:
            conn.execute("""INSERT INTO grp_groups(id,nom,type,source,attendu,n,statut,recupere_le,erreur,non_resolus)
                            VALUES(?,?,?,?,?,0,'non_verifie',NULL,NULL,NULL)
                            ON CONFLICT(id) DO UPDATE SET nom=excluded.nom, type=excluded.type, source=excluded.source,
                              attendu=excluded.attendu, statut='non_verifie', n=0""",
                         (gid, base["nom"], base["type"], base["source"], base["attendu"]))
            conn.execute("DELETE FROM grp_members WHERE groupe=?", (gid,))
            out[gid] = "non_verifie"
            continue
        try:
            found: list[tuple[str, str | None]] = []
            for suffix in [""] + EXTRA_PAGES.get(parser, []):
                url = g["source"] + suffix
                if url not in pages:
                    res = client.get(url)
                    if not res.ok or fetch.is_antibot(res):
                        raise RuntimeError(f"{url} : {res.status} {res.error or ''}")
                    pages[url] = res.content
                found += PARSERS[parser](pages[url])
            found += [(x, None) for x in g.get("ajout") or []]
            seen: dict[str, tuple[str, str | None]] = {}
            for name, since in found:
                seen.setdefault(" ".join(name.split()), (name, since))
            rows, unresolved = [], []
            for name, (_raw, since) in seen.items():
                iso = to_iso3(name, idx)
                if not iso:
                    unresolved.append(name)
                rows.append((gid, iso, name, name, since, g["source"], now))
            if not rows:
                raise RuntimeError("aucun membre lu (structure de la page changée ?)")
            statut = _statut(g, len(rows))
            conn.execute("DELETE FROM grp_members WHERE groupe=?", (gid,))
            conn.executemany("INSERT OR REPLACE INTO grp_members(groupe,iso3,nom,nom_source,depuis,source,recupere_le) "
                             "VALUES(?,?,?,?,?,?,?)", rows)
            conn.execute("""INSERT INTO grp_groups(id,nom,type,source,attendu,n,statut,recupere_le,erreur,non_resolus)
                            VALUES(?,?,?,?,?,?,?,?,NULL,?)
                            ON CONFLICT(id) DO UPDATE SET nom=excluded.nom, type=excluded.type, source=excluded.source,
                              attendu=excluded.attendu, n=excluded.n, statut=excluded.statut,
                              recupere_le=excluded.recupere_le, erreur=NULL, non_resolus=excluded.non_resolus""",
                         (gid, base["nom"], base["type"], base["source"], base["attendu"], len(rows), statut, now,
                          db.jdump(unresolved)))
            out[gid] = f"{statut} {len(rows)}" + (f" (non résolus : {', '.join(unresolved)})" if unresolved else "")
        except Exception as exc:
            conn.execute("""INSERT INTO grp_groups(id,nom,type,source,attendu,statut,erreur) VALUES(?,?,?,?,?,'erreur',?)
                            ON CONFLICT(id) DO UPDATE SET statut='erreur', erreur=excluded.erreur""",
                         (gid, base["nom"], base["type"], base["source"], base["attendu"], str(exc)[:300]))
            out[gid] = f"erreur : {exc}"
    conn.commit()
    db.meta_set(conn, "groupes_sync", now)
    return out


# --- consultation ----------------------------------------------------------------------------------------------------

def group_id(name: str) -> str | None:
    """« PMA », « G77 », « Groupe parapluie », « EU »… → identifiant du catalogue (None si ce n'est pas un groupe)."""
    k = _key(name)
    for g in catalog():
        if k == _key(g["id"]) or k in {_key(a) for a in g.get("alias") or []}:
            return g["id"]
    return None


def members(conn, gid: str) -> list[dict[str, Any]]:
    ensure_schema(conn)
    return [dict(r) for r in conn.execute(
        "SELECT iso3, nom, depuis, source, recupere_le FROM grp_members WHERE groupe=? ORDER BY nom", (gid,))]


def member_isos(conn, gid: str) -> set[str]:
    ensure_schema(conn)
    return {r[0] for r in conn.execute("SELECT iso3 FROM grp_members WHERE groupe=? AND iso3 IS NOT NULL", (gid,))}


def group_info(conn, gid: str) -> dict[str, Any]:
    ensure_schema(conn)
    g = conn.execute("SELECT * FROM grp_groups WHERE id=?", (gid,)).fetchone()
    cat = next((x for x in catalog() if x["id"] == gid), {})
    out = {"groupe": gid, "nom": cat.get("nom"), "type": cat.get("type"), "source": cat.get("source"),
           "membres_annonces": cat.get("attendu"), "note": cat.get("note")}
    if g:
        out.update({"statut": g["statut"], "nb_membres_listes": g["n"], "liste_recuperee_le": g["recupere_le"],
                    "erreur": g["erreur"], "noms_non_resolus": db.jload(g["non_resolus"]) or None})
    else:
        out["statut"] = "non_releve"
    if cat.get("citation"):
        out["citation_source"] = cat["citation"]
    if cat.get("equivalence"):
        out["equivalence"] = cat["equivalence"]
    if isinstance(cat.get("attendu"), list):
        out["membres_annonces"] = " puis ".join(str(x) for x in cat["attendu"])
    if out.get("statut") == "erreur" and out.get("nb_membres_listes"):
        out["avertissement"] = "Dernière relève en échec : liste de la relève précédente (voir liste_recuperee_le)."
    if out.get("statut") in ("non_verifie", "non_releve"):
        out["avertissement"] = ("Pas de liste de membres sourcée : ne pas en déduire la composition du groupe ; citer "
                                "seulement ce que dit la source (nombre annoncé).")
    elif out.get("statut") == "ecart":
        out["avertissement"] = "Le nombre de membres lus diffère du nombre annoncé : liste à vérifier avant de s'y fier."
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def groups_of(conn, party: str) -> list[dict[str, Any]]:
    """Groupes (à liste sourcée) dont une Partie est membre, avec la source de chaque appartenance."""
    ensure_schema(conn)
    iso = to_iso3(party, iso_index(conn)) or (party.upper() if re.fullmatch(r"[A-Za-z]{3}", party.strip()) else None)
    if not iso:
        return []
    return [dict(r) for r in conn.execute(
        """SELECT m.groupe, g.nom, g.type, g.statut, m.nom_source, m.depuis, m.source, m.recupere_le
           FROM grp_members m JOIN grp_groups g ON g.id=m.groupe WHERE m.iso3=? ORDER BY g.type, m.groupe""", (iso,))]


def query(conn, groupe: str | None = None, partie: str | None = None) -> dict[str, Any]:
    ensure_schema(conn)
    if groupe:
        gid = group_id(groupe)
        if not gid:
            return {"erreur": f"groupe inconnu : {groupe}", "groupes": [g["id"] for g in catalog()]}
        out = group_info(conn, gid)
        out["membres"] = members(conn, gid)
        return out
    if partie:
        gs = groups_of(conn, partie)
        unverified = [g["id"] for g in catalog() if not g.get("parser")]
        return {"partie": partie, "iso3": to_iso3(partie, iso_index(conn)), "groupes": gs,
                "groupes_sans_liste_verifiee": unverified,
                "note": "Appartenance établie seulement pour les groupes à liste sourcée ; la Partie peut aussi faire "
                        "partie des groupes sans liste vérifiée."}
    return {"groupes": [group_info(conn, g["id"]) for g in catalog()],
            "releve_le": db.meta_get(conn, "groupes_sync")}


def party_filter_values(conn, gid: str) -> list[str]:
    """Valeurs à chercher dans documents.parties (JSON : noms CCNUCC des soumissions, ISO3 des NDC) pour un groupe."""
    ensure_schema(conn)
    vals = set()
    for iso, nom in conn.execute("SELECT iso3, nom FROM grp_members WHERE groupe=?", (gid,)):
        vals.add(nom)
        if iso:
            vals.add(iso)
            for c in conn.execute("SELECT DISTINCT country FROM emissions WHERE iso3=?", (iso,)) if _has_emissions(conn) else []:
                vals.add(c[0])
    vals.add(gid)
    return sorted(vals)


def _has_emissions(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='emissions'").fetchone() is not None
