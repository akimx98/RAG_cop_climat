"""Extraction des positions (qui dit quoi, sur quel dossier) dans les sources PUBLIQUES de la COP.

Garde-fou contre l'invention : une position n'est gardée que si sa citation figure mot pour mot dans la source.
Sources traitées : comptes rendus et analyses publics reçus depuis le début de la période (ENB, TWN, Carbon Brief,
Climate Home, ECO, lettres reçues par l'adresse de veille…), et messages Bluesky suivis.
Jamais les documents restreints ni les notes de terrain.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from . import config, live, llm

SINCE = config.LIVE_SINCE
# Documents sans date de publication : seulement ceux reçus après la mise en service (pas l'ancien stock).
UNDATED_SINCE = config.LIVE_UNDATED_SINCE
ACTORS = ("EU, G77 and China, AOSIS, LDC, AGN, LMDC, Arab Group, AILAC, ABU, EIG, Umbrella Group, BASIC, LLDC, "
          "CfRN, Présidence COP31 (présidence turque ou australienne des négociations, quand elle parle en tant que "
          "présidence), ou un pays par son nom anglais court sans parenthèse (Türkiye, Australia, China, India, "
          "Saudi Arabia, United States, Brazil, Japan…)")
ALIASES = {"european union": "EU", "eu27": "EU", "g77/china": "G77 and China", "g77 & china": "G77 and China",
           "g77": "G77 and China", "african group": "AGN", "african group of negotiators": "AGN",
           "least developed countries": "LDC", "ldcs": "LDC", "like-minded developing countries": "LMDC",
           "alliance of small island states": "AOSIS", "sids": "AOSIS", "turkey": "Türkiye",
           "environmental integrity group": "EIG", "arab group": "Arab Group", "umbrella": "Umbrella Group",
           "cop31 presidency": "Présidence COP31", "presidency": "Présidence COP31", "us": "United States",
           "usa": "United States", "uk": "UK", "united kingdom": "UK",
           # Formes longues, sigles et noms français (soumissions CCNUCC, requêtes des utilisateurs).
           "eu": "EU", "ue": "EU", "union européenne": "EU", "union europeenne": "EU",
           "european union and its member states": "EU", "g77 and china": "G77 and China",
           "group of 77 and china": "G77 and China", "g-77 and china": "G77 and China", "g77+china": "G77 and China",
           "g77 et chine": "G77 and China", "g77 + chine": "G77 and China", "ldc": "LDC", "pma": "LDC",
           "least developed countries group": "LDC", "ldc group": "LDC", "pays les moins avancés": "LDC",
           "agn": "AGN", "groupe africain": "AGN", "african group of negotiators on climate change": "AGN",
           "aosis": "AOSIS", "lmdc": "LMDC", "lmdcs": "LMDC", "like-minded developing countries group": "LMDC",
           "lldc": "LLDC", "lldcs": "LLDC", "landlocked developing countries": "LLDC", "ailac": "AILAC",
           "independent association of latin america and the caribbean": "AILAC", "alba": "ALBA", "basic": "BASIC",
           "eig": "EIG", "groupe arabe": "Arab Group", "arab group of negotiators": "Arab Group",
           "umbrella group": "Umbrella Group", "groupe parapluie": "Umbrella Group", "chine": "China",
           "inde": "India", "arabie saoudite": "Saudi Arabia", "brésil": "Brazil", "japon": "Japan",
           "états-unis": "United States", "etats-unis": "United States", "royaume-uni": "UK",
           "turquie": "Türkiye", "republic of türkiye": "Türkiye", "people's republic of china": "China",
           "cfrn": "CfRN", "coalition for rainforest nations": "CfRN", "coalition of rainforest nations": "CfRN",
           "bolivarian alliance for the peoples of our america": "ALBA", "alba group": "ALBA", "alba+": "ALBA",
           "g-77/china": "G77 and China", "g77/chine": "G77 and China", "g-77": "G77 and China",
           "présidence": "Présidence COP31", "presidence": "Présidence COP31", "presidence cop31": "Présidence COP31",
           "présidence cop31": "Présidence COP31", "cop 31 presidency": "Présidence COP31",
           "cop31 presidencies": "Présidence COP31", "cop 31 presidencies": "Présidence COP31",
           "présidence de la cop31": "Présidence COP31", "turkish presidency": "Présidence COP31"}


def normalize_actor(name: str) -> str:
    """Nom d'acteur canonique, partagé par positions, soumissions et comparaisons (« European Union » → « EU »,
    « Least Developed Countries (LDCs) Group » → « LDC », « UE » → « EU »). Égalité stricte ensuite :
    « Mali » ne doit jamais trouver « Somalia »."""
    raw = re.sub(r"\s+", " ", (name or "").replace("\u200b", "").strip())
    raw = re.sub(r"^(the|le|la|les)\s+", "", raw, flags=re.I)
    # « Arab Group Statement », « G77 submission » : le support n'est pas l'acteur.
    raw = re.sub(r"\s+(statement|declaration|déclaration|submission|soumission|intervention)s?$", "", raw, flags=re.I)
    if raw.lower() in ALIASES:
        return ALIASES[raw.lower()]
    for inner in re.findall(r"\(([A-Z0-9][A-Z0-9.&+-]{1,10}s?)\)", raw):  # vrai sigle : « … (LDCs) Group »
        if inner.strip().lower() in ALIASES:
            return ALIASES[inner.strip().lower()]
    # Parenthèse finale retirée seulement si c'est un sigle ou « group » : « Congo (Democratic Republic of the) »
    # ne doit pas devenir « Congo ».
    base = re.sub(r"\s*\((?:[A-Z0-9][A-Z0-9.&+-]{1,10}s?|group|groupe|grupo)\)\s*$", "", raw).strip()
    for cand in (base, re.sub(r"\s+group$", "", base, flags=re.I), re.sub(r"\s+group$", "", re.sub(r"\s*\([^)]*\)", "", raw), flags=re.I)):
        if cand.lower() in ALIASES:
            return ALIASES[cand.lower()]
    return base[:80]
STANCE_VALUES = ["priorité", "soutient", "propose", "réserve", "ne soutient pas", "s'oppose", "ligne rouge", "neutre"]

SYSTEM = """Tu es analyste pour un outil indépendant de suivi de la COP31 (Antalya, novembre 2026).
On te donne UNE source publique (compte rendu de négociation, article, lettre d'information, message).
Relève les POSITIONS exprimées par des Parties ou groupes de négociation sur les sujets de la COP :
qui (acteur), sur quel dossier, quel enjeu précis, quelle attitude, résumé fidèle, citation EXACTE.

Règles strictes :
- N'invente rien. Chaque position doit s'appuyer sur un extrait recopié MOT POUR MOT de la source (champ quote,
  20 à 300 caractères, sans le modifier ni le traduire). Pas d'extrait exact possible → ne retiens pas la position.
- Acteur = une Partie ou un groupe de négociation (pas un journaliste, une ONG ou un expert, sauf s'il rapporte
  une position d'une Partie : l'acteur est alors la Partie). Noms normalisés : {actors}.
- Dossier = un identifiant de la liste fournie ; « autre » si aucun ne convient.
- summary : une phrase en français, neutre, qui dit ce que veut l'acteur.
- said_on : date de la prise de position si la source la donne (AAAA-MM-JJ), sinon "".
- EXHAUSTIF : parcours la source section par section, point de l'ordre du jour par point (ACE, CTCN, renforcement
  des capacités, genre, finance…) ; une déclaration de groupe donne une position par section, même courte.
- Compte rendu de négociation (Earth Negotiations Bulletin, TWN…) : chaque attribution compte. « X, supported by Y
  and Z, called for… » → une position pour X, Y et Z (même attitude, même citation) ; « opposed by W » → une
  position pour W, attitude contraire, même citation. Les noms en MAJUSCULES sont des Parties ou des groupes.
- Pas de doublon. Si la source ne contient aucune position de Partie, renvoie une liste vide."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["positions"],
    "properties": {"positions": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["actor", "dossier", "issue", "stance", "summary", "quote", "said_on"],
        "properties": {
            "actor": {"type": "string"}, "dossier": {"type": "string"}, "issue": {"type": "string"},
            "stance": {"type": "string", "enum": STANCE_VALUES}, "summary": {"type": "string"},
            "quote": {"type": "string"}, "said_on": {"type": "string"},
        }}}},
}


def _norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", re.sub(r"-\s*\n\s*", "", s)).strip()


def _dossier_list() -> str:
    return "\n".join(f"- {d['slug']} : {d['nom']}" for d in live.dossiers())


def _quote_key(quote: str) -> str:
    return re.sub(r"[^\w]+", " ", _norm(quote or "")).strip()[:200]


def _already_known(conn, actor: str, quote: str) -> bool:
    key = _quote_key(quote)
    return any(_quote_key(r[0]) == key for r in conn.execute(
        "SELECT quote FROM positions WHERE actor=? AND access='public'", (actor,)))


def tidy(conn) -> dict[str, int]:
    """Remise en ordre idempotente des positions relevées : noms d'acteurs renormalisés (alias ajoutés depuis :
    « Arab Group Statement » → « Arab Group », « Presidence COP31 » → « Présidence COP31 »), puis doublons retirés
    (même acteur, même citation : un article repris par deux flux devenait deux documents)."""
    live.ensure_schema(conn)
    renamed = 0
    for r in conn.execute("SELECT DISTINCT actor FROM positions").fetchall():
        canon = normalize_actor(r["actor"])
        if canon and canon != r["actor"]:
            renamed += conn.execute("UPDATE positions SET actor=? WHERE actor=?", (canon, r["actor"])).rowcount
    seen: set[tuple[str, str, str]] = set()
    dupes = []
    for r in conn.execute("SELECT id, actor, quote, source_kind FROM positions ORDER BY id"):
        k = (r["source_kind"] == "carbonbrief" and "cb" or "x", r["actor"], _quote_key(r["quote"]))
        if not k[2]:
            continue
        if k in seen:
            dupes.append((r["id"],))
        else:
            seen.add(k)
    conn.executemany("DELETE FROM positions WHERE id=?", dupes)
    conn.commit()
    return {"renommees": renamed, "doublons": len(dupes)}


def _store(conn, items: list[dict], source_text: str, *, kind: str, label: str, url: str | None,
           doc_id: int | None, default_date: str | None) -> int:
    slugs = {d["slug"] for d in live.dossiers()}
    hay = _norm(source_text)
    n = 0
    for p in items:
        quote = (p.get("quote") or "").strip()
        if len(quote) < 15 or _norm(quote) not in hay:
            continue  # citation introuvable telle quelle dans la source : rejetée
        dossier = p.get("dossier") if p.get("dossier") in slugs else None
        actor = normalize_actor(p["actor"])
        if _already_known(conn, actor, quote):
            continue  # même citation du même acteur déjà relevée (article repris par deux flux, autre source)
        key = "x|" + hashlib.sha1(f"{doc_id}|{url}|{p['actor']}|{_norm(quote)[:120]}".encode()).hexdigest()
        cur = conn.execute(
            """INSERT OR IGNORE INTO positions(key,dossier,actor,issue,stance,summary,quote,source_kind,source_label,
                 source_url,doc_id,said_on,created_at,access) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'public')""",
            (key, dossier, actor, p["issue"].strip()[:200], p["stance"], p["summary"].strip()[:500],
             quote[:400], kind, label[:200], url, doc_id, p.get("said_on") or default_date, live.now_iso()))
        n += cur.rowcount
    conn.commit()
    return n


# Sources lues pour l'extraction : comptes rendus de NÉGOCIATION et déclarations officielles des groupes et
# institutions (préfixes de source_id du catalogue). La presse généraliste (The Guardian, Le Monde, AFP…) et les
# think tanks sont exclus : ils produisaient des « positions » hors négociation (sondages, débats nationaux).
POSITION_SOURCES = ("twn-", "climatehome", "carbonbrief", "enb-", "veille-mail", "can-eco",   # comptes rendus
                    "g77-", "aosis-", "tr-cop31-", "eu-dgclima", "eu-statements")             # déclarations
# Organisations lues quelle que soit la voie d'entrée (dépôt d'URL, dépôt manuel…) : l'ENB est LA source qui
# attribue les positions en salle (« X, supported by Y, opposed by Z »).
POSITION_ORGS = ("Earth Negotiations Bulletin%", "IISD%Earth Negotiations%")


def _listed_source_sql(col: str = "d.source_id") -> tuple[str, list[str]]:
    org_col = col.rsplit(".", 1)[0] + ".org" if "." in col else "org"
    parts = [f"COALESCE({col},'') LIKE ?" for _ in POSITION_SOURCES] + [f"COALESCE({org_col},'') LIKE ?" for _ in POSITION_ORGS]
    return "(" + " OR ".join(parts) + ")", [f"{p}%" for p in POSITION_SOURCES] + list(POSITION_ORGS)


def prune_unlisted(conn) -> int:
    """Retire les positions extraites de sources qui ne sont plus lues (idempotent)."""
    live.ensure_schema(conn)
    clause, params = _listed_source_sql()
    cur = conn.execute(
        f"""DELETE FROM positions WHERE source_kind='extraction' AND doc_id IN (
              SELECT d.id FROM documents d WHERE NOT {clause})""", params)
    conn.commit()
    return cur.rowcount


def pending_docs(conn, limit: int = 10) -> list:
    live.ensure_schema(conn)
    clause, params = _listed_source_sql()
    return conn.execute(
        f"""SELECT d.* FROM documents d LEFT JOIN extraction_log x ON x.doc_id=d.id
           WHERE x.doc_id IS NULL AND d.access='public' AND d.status='indexed'
             AND COALESCE(d.source_id,'')<>'unfccc-submissions'  -- analysées par soumissions.py
             AND ((COALESCE(d.published,'') <> '' AND d.published >= ?) OR (COALESCE(d.published,'') = ''
                  AND (d.fetched_at >= ? OR d.source_id='veille-mail')))
             AND {clause}
           ORDER BY d.id DESC LIMIT ?""", (SINCE, UNDATED_SINCE, *params, limit)).fetchall()


# Une section commence par un intertitre : ligne courte sans point final (« Action for Climate Empowerment »,
# « Technology », « On finance: »), ligne en capitales, ou intitulé numéroté (« 3. Capacity-building », « II. ACE »).
_HEADING = re.compile(r"^\s*(?:(?:[IVX]{1,5}|\d{1,2}|[A-H])[.)]\s+)?[A-Z][^\n]{2,90}$")
SECTION_CHARS = 9000   # taille d'un lot envoyé au modèle : assez petit pour qu'il relève TOUS les points
MAX_TEXT = 120000


def _is_heading(line: str) -> bool:
    l = line.strip()
    if not l or len(l) > 90 or l.endswith((".", ",", ";")) or not _HEADING.match(l):
        return False
    words = l.rstrip(":").split()
    return len(words) <= 12 and (l.isupper() or l.endswith(":") or sum(w[:1].isupper() for w in words) >= len(words) / 2)


def sections(text: str, max_chars: int = SECTION_CHARS) -> list[str]:
    """Découpe une source en lots de sections entières (intertitres), chacun ≤ max_chars : le modèle traite un lot à
    la fois et relève toutes les positions de chaque section au lieu de s'arrêter aux premières."""
    blocks: list[str] = []
    cur: list[str] = []
    for line in text.split("\n"):
        if _is_heading(line) and sum(len(x) for x in cur) > 200:
            blocks.append("\n".join(cur))
            cur = []
        cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    out: list[str] = []
    buf = ""
    for b in blocks:
        while len(b) > max_chars:  # section très longue : coupée aux paragraphes
            cut = b.rfind("\n", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            if buf:
                out.append(buf)
                buf = ""
            out.append(b[:cut])
            b = b[cut:]
        if buf and len(buf) + len(b) > max_chars:
            out.append(buf)
            buf = ""
        buf = f"{buf}\n{b}" if buf else b
    if buf.strip():
        out.append(buf)
    return [x for x in out if x.strip()]


def extract_doc(conn, doc) -> int:
    text = "\n".join(r[0] for r in conn.execute("SELECT text FROM passages WHERE doc_id=? ORDER BY seq", (doc["id"],)))
    text = text[:MAX_TEXT]
    if len(text) < 300:
        conn.execute("INSERT OR REPLACE INTO extraction_log(doc_id,at,model,n,error) VALUES(?,?,?,?,?)",
                     (doc["id"], live.now_iso(), None, 0, "trop court"))
        conn.commit()
        return 0
    parts = sections(text, SECTION_CHARS)
    n, errors = 0, []
    for i, part in enumerate(parts, 1):
        user = (f"Dossiers :\n{_dossier_list()}\n\nSource : {doc['title']} — {doc['org'] or ''} — "
                f"publiée {doc['published'] or doc['fetched_at'][:10]}"
                + (f" — partie {i}/{len(parts)} (relève toutes les positions de CETTE partie)" if len(parts) > 1 else "")
                + f"\n\n<<<\n{part}\n>>>")
        try:
            out = llm.json_call(conn, model=llm.EXTRACT_MODEL, purpose="positions", system=SYSTEM.format(actors=ACTORS),
                                user=user, schema=SCHEMA, name="positions")
            n += _store(conn, out.get("positions", []), part, kind="extraction",
                        label=f"{doc['title'] or ''} ({doc['org'] or 'source'})", url=f"/doc/{doc['id']}",
                        doc_id=doc["id"], default_date=(doc["published"] or doc["fetched_at"])[:10])
        except llm.BudgetExceeded:
            raise
        except Exception as exc:
            errors.append(f"partie {i} : {str(exc)[:120]}")
    err = "; ".join(errors)[:300] or None
    conn.execute("INSERT OR REPLACE INTO extraction_log(doc_id,at,model,n,error) VALUES(?,?,?,?,?)",
                 (doc["id"], live.now_iso(), llm.EXTRACT_MODEL, n, err))
    conn.commit()
    return n


def extract_social(conn, batch: int = 40) -> int:
    live.ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM social_posts WHERE extracted=0 AND created_at >= ? ORDER BY created_at LIMIT ?",
        (SINCE, batch)).fetchall()
    if not rows:
        return 0
    blocks = [f"[{i}] {r['author']} (@{r['handle']}), {r['created_at'][:16]} : {r['text']}" for i, r in enumerate(rows)]
    user = f"Dossiers :\n{_dossier_list()}\n\nMessages Bluesky (journalistes et observateurs) :\n" + "\n".join(blocks)
    n = 0
    try:
        out = llm.json_call(conn, model=llm.EXTRACT_MODEL, purpose="positions-bluesky",
                            system=SYSTEM.format(actors=ACTORS), user=user, schema=SCHEMA, name="positions")
        for p in out.get("positions", []):
            q = _norm(p.get("quote") or "")
            src = next((r for r in rows if q and q in _norm(r["text"] or "")), None)
            if src:
                n += _store(conn, [p], src["text"], kind="bluesky", label=f"{src['author']} sur Bluesky",
                            url=src["url"], doc_id=None, default_date=src["created_at"][:10])
    finally:
        conn.executemany("UPDATE social_posts SET extracted=1 WHERE uri=?", [(r["uri"],) for r in rows])
        conn.commit()
    return n


REQUEUE_FLAG = "positions_par_section_v1"
REQUEUE_PREFIXES = ("enb-", "twn-", "g77-", "aosis-", "tr-cop31-", "eu-dgclima", "eu-statements")


def requeue_for_sections(conn) -> int:
    """Une fois : les déclarations et comptes rendus de négociation déjà lus en un seul appel (positions manquantes,
    ex. la section ACE de la déclaration du G77) sont relus section par section. Les positions déjà relevées restent
    (clé unique : pas de doublon)."""
    from . import db

    if db.meta_get(conn, REQUEUE_FLAG, "") == "1":
        return 0
    live.ensure_schema(conn)
    likes = " OR ".join("COALESCE(d.source_id,'') LIKE ?" for _ in REQUEUE_PREFIXES)
    likes += " OR " + " OR ".join("COALESCE(d.org,'') LIKE ?" for _ in POSITION_ORGS)
    cur = conn.execute(
        f"""DELETE FROM extraction_log WHERE doc_id IN (SELECT d.id FROM documents d WHERE ({likes})
              AND COALESCE(d.published, d.fetched_at) >= ?)""",
        (*[f"{p}%" for p in REQUEUE_PREFIXES], *POSITION_ORGS, SINCE))
    db.meta_set(conn, REQUEUE_FLAG, "1")
    conn.commit()
    return cur.rowcount


def run(conn, docs: int = 6) -> dict[str, int]:
    if not llm.enabled():
        return {}
    out = {"documents": 0, "positions": 0}
    requeued = requeue_for_sections(conn)
    if requeued:
        out["relues_par_section"] = requeued
    pruned = prune_unlisted(conn)
    if pruned:
        out["retirees"] = pruned
    for doc in pending_docs(conn, limit=docs):
        out["positions"] += extract_doc(conn, doc)
        out["documents"] += 1
    out["positions"] += extract_social(conn)
    return out


def for_dossier(conn, slug: str, since: str | None = None, limit: int = 300) -> list[dict]:
    live.ensure_schema(conn)
    rows = conn.execute(
        """SELECT * FROM positions WHERE dossier=? AND access='public' AND (? IS NULL OR created_at>=?)
           ORDER BY COALESCE(said_on, created_at) DESC, id DESC LIMIT ?""", (slug, since, since, limit)).fetchall()
    return [dict(r) for r in rows]


def matrix(conn, slug: str) -> dict[str, Any]:
    """Acteur × enjeu, dernière attitude connue (Carbon Brief + extractions)."""
    rows = conn.execute(
        """SELECT actor, issue, stance, source_kind, source_label, source_url, said_on, created_at, summary, quote
           FROM positions WHERE dossier=? AND access='public' ORDER BY COALESCE(said_on, created_at)""", (slug,)).fetchall()
    actors: dict[str, list[dict]] = {}
    for r in rows:
        actors.setdefault(r["actor"], []).append(dict(r))
    order = sorted(actors, key=lambda a: (-len(actors[a]), a))
    return {"acteurs": order, "par_acteur": actors}


def by_actor(conn, actor: str, limit: int = 200) -> list[dict]:
    live.ensure_schema(conn)
    rows = conn.execute(
        """SELECT * FROM positions WHERE actor LIKE ? ORDER BY COALESCE(said_on, created_at) DESC LIMIT ?""",
        (f"%{actor}%", limit)).fetchall()
    return [dict(r) for r in rows]


# --- divisions internes d'un groupe ------------------------------------------------------------------------------

PRO = {"priorité", "soutient", "propose"}
CONTRA = {"ne soutient pas", "s'oppose", "ligne rouge", "réserve"}
_ISSUE_STOP = {"the", "of", "and", "to", "on", "for", "in", "a", "de", "la", "le", "les", "des", "du", "et", "en",
               "sur", "pour", "au", "aux", "l", "d", "un", "une"}


def _issue_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zà-ÿ0-9]+", (text or "").lower()) if len(w) > 2 and w not in _ISSUE_STOP}


def group_actors(conn, gid: str) -> dict[str, str]:
    """Acteurs (noms canoniques) qui relèvent d'un groupe : lui-même, ses sous-groupes, ses membres (liste sourcée).
    {acteur: rôle}."""
    from . import groupes
    from .soumissions import SUBGROUPS

    out = {normalize_actor(gid): "groupe"}
    for sg in SUBGROUPS.get(gid, ()):
        out.setdefault(sg, "sous-groupe")
    for m in groupes.members(conn, gid):
        if m.get("nom"):
            out.setdefault(normalize_actor(m["nom"]), "membre")
    return out


def divisions(conn, gid: str, dossier: str | None = None, since: str | None = None,
              allies: int = 6, compact: bool = False) -> dict[str, Any]:
    """Divisions internes d'un groupe : sur un même enjeu (même dossier, mots de l'enjeu proches), des acteurs du
    groupe (sous-groupes, membres) soutiennent pendant que d'autres s'opposent. Pour chaque division, les acteurs
    hors groupe qui partagent chaque attitude (ex. AOSIS et PMA avec l'UE, contre le Groupe arabe et l'Inde)."""
    from . import groupes

    live.ensure_schema(conn)
    inside = group_actors(conn, gid)
    isos, idx = groupes.member_isos(conn, gid), None
    if isos:
        try:
            idx = groupes.iso_index(conn)
        except Exception:
            idx = None
    cache: dict[str, str | None] = {}

    def role(actor: str) -> str | None:
        """Rôle dans le groupe (nom exact, sinon ISO3 du membre : « Bolivia » = « Bolivia (Plurinational State of) »)."""
        if actor in inside:
            return inside[actor]
        if actor not in cache:
            iso = groupes.to_iso3(actor, idx) if idx else None
            cache[actor] = "membre" if iso and iso in isos else None
        return cache[actor]

    where, params = ["access='public'", "source_kind<>'carbonbrief'"], []
    if dossier:
        where.append("dossier=?")
        params.append(dossier)
    if since:
        where.append("created_at>=?")
        params.append(since)
    rows = [dict(r) for r in conn.execute(
        f"""SELECT dossier, actor, stance, issue, summary, quote, said_on, source_kind, source_label, source_url
            FROM positions WHERE {' AND '.join(where)} ORDER BY COALESCE(said_on, created_at) DESC LIMIT 4000""",
        params)]
    # Regroupement des enjeux : même dossier et ≥ 50 % de mots communs (enjeux libres, formulés par le modèle).
    clusters: list[dict[str, Any]] = []
    for r in rows:
        if r["stance"] not in PRO | CONTRA:
            continue
        words = _issue_words(f"{r['issue']}")
        for c in clusters:
            if c["dossier"] == r["dossier"] and words and c["words"] and \
                    len(words & c["words"]) / min(len(words), len(c["words"])) >= 0.5:
                c["rows"].append(r)
                break
        else:
            clusters.append({"dossier": r["dossier"], "words": words, "issue": r["issue"], "rows": [r]})
    out = []
    for c in clusters:
        latest: dict[str, dict] = {}
        for r in c["rows"]:  # du plus récent au plus ancien : dernière attitude de chaque acteur
            latest.setdefault(r["actor"], r)
        pro_in = [r for a, r in latest.items() if role(a) and r["stance"] in PRO]
        con_in = [r for a, r in latest.items() if role(a) and r["stance"] in CONTRA]
        if not (pro_in and con_in):
            continue

        def fmt(r):
            if compact:
                return {"acteur": r["actor"], "role": role(r["actor"]) or "hors groupe", "attitude": r["stance"],
                        "date": r["said_on"], "lien": r["source_url"]}
            return {"acteur": r["actor"], "role": role(r["actor"]) or "hors groupe", "attitude": r["stance"],
                    "citation": r["quote"], "date": r["said_on"], "source": r["source_label"], "lien": r["source_url"]}

        out.append({
            "dossier": c["dossier"], "enjeu": c["issue"],
            "pour": [fmt(r) for r in pro_in], "contre": [fmt(r) for r in con_in],
            "allies_hors_groupe_pour": [fmt(r) for a, r in latest.items()
                                        if not role(a) and r["stance"] in PRO][:allies] or None,
            "allies_hors_groupe_contre": [fmt(r) for a, r in latest.items()
                                          if not role(a) and r["stance"] in CONTRA][:allies] or None,
        })
    out.sort(key=lambda d: -(len(d["pour"]) + len(d["contre"])))
    return {"groupe": gid, "membres_et_sous_groupes": len(inside) + len(isos), "divisions": out,
            "note": "Enjeux rapprochés automatiquement (même dossier, mots proches) à partir des positions relevées : "
                    "relire les citations avant d'affirmer une division ; un silence n'est pas un accord."}
