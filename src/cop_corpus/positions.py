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
           "bolivarian alliance for the peoples of our america": "ALBA", "alba group": "ALBA", "alba+": "ALBA"}


def normalize_actor(name: str) -> str:
    """Nom d'acteur canonique, partagé par positions, soumissions et comparaisons (« European Union » → « EU »,
    « Least Developed Countries (LDCs) Group » → « LDC », « UE » → « EU »). Égalité stricte ensuite :
    « Mali » ne doit jamais trouver « Somalia »."""
    raw = re.sub(r"\s+", " ", (name or "").replace("\u200b", "").strip())
    raw = re.sub(r"^(the|le|la|les)\s+", "", raw, flags=re.I)
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
        key = "x|" + hashlib.sha1(f"{doc_id}|{url}|{p['actor']}|{_norm(quote)[:120]}".encode()).hexdigest()
        cur = conn.execute(
            """INSERT OR IGNORE INTO positions(key,dossier,actor,issue,stance,summary,quote,source_kind,source_label,
                 source_url,doc_id,said_on,created_at,access) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'public')""",
            (key, dossier, normalize_actor(p["actor"]), p["issue"].strip()[:200], p["stance"], p["summary"].strip()[:500],
             quote[:400], kind, label[:200], url, doc_id, p.get("said_on") or default_date, live.now_iso()))
        n += cur.rowcount
    conn.commit()
    return n


# Sources lues pour l'extraction : comptes rendus de NÉGOCIATION et déclarations officielles des groupes et
# institutions (préfixes de source_id du catalogue). La presse généraliste (The Guardian, Le Monde, AFP…) et les
# think tanks sont exclus : ils produisaient des « positions » hors négociation (sondages, débats nationaux).
POSITION_SOURCES = ("twn-", "climatehome", "carbonbrief", "enb-", "veille-mail", "can-eco",   # comptes rendus
                    "g77-", "aosis-", "tr-cop31-", "eu-dgclima")                               # déclarations


def _listed_source_sql(col: str = "d.source_id") -> tuple[str, list[str]]:
    return "(" + " OR ".join(f"COALESCE({col},'') LIKE ?" for _ in POSITION_SOURCES) + ")", \
        [f"{p}%" for p in POSITION_SOURCES]


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


def extract_doc(conn, doc) -> int:
    text = "\n".join(r[0] for r in conn.execute("SELECT text FROM passages WHERE doc_id=? ORDER BY seq", (doc["id"],)))
    text = text[:60000]
    if len(text) < 300:
        conn.execute("INSERT OR REPLACE INTO extraction_log(doc_id,at,model,n,error) VALUES(?,?,?,?,?)",
                     (doc["id"], live.now_iso(), None, 0, "trop court"))
        conn.commit()
        return 0
    user = (f"Dossiers :\n{_dossier_list()}\n\nSource : {doc['title']} — {doc['org'] or ''} — "
            f"publiée {doc['published'] or doc['fetched_at'][:10]}\n\n<<<\n{text}\n>>>")
    try:
        out = llm.json_call(conn, model=llm.EXTRACT_MODEL, purpose="positions", system=SYSTEM.format(actors=ACTORS),
                            user=user, schema=SCHEMA, name="positions")
        n = _store(conn, out.get("positions", []), text, kind="extraction",
                   label=f"{doc['title'] or ''} ({doc['org'] or 'source'})", url=f"/doc/{doc['id']}",
                   doc_id=doc["id"], default_date=(doc["published"] or doc["fetched_at"])[:10])
        err = None
    except llm.BudgetExceeded:
        raise
    except Exception as exc:
        n, err = 0, str(exc)[:300]
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


def run(conn, docs: int = 6) -> dict[str, int]:
    if not llm.enabled():
        return {}
    out = {"documents": 0, "positions": 0}
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
