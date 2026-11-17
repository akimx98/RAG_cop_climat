"""Soumissions officielles à la CCNUCC (portail submissions.unfccc.int) : collecte, analyse, comparaison.

Source : l'API JSON publique qu'utilise le portail lui-même (aucune authentification, aucun anti-bot) :
- /api/CallForSubmissions : les appels (à soumissions, déclarations en plénière…), avec session, thèmes, organes ;
- /api/Submissions : chaque fichier déposé (auteur, type d'entité, co-auteurs, langue, date, chemin) ;
- /api/files/download?serverRelativeFilePath=… : le fichier original.

Chaque fichier devient un document du corpus (recherche, passages, liens) :
- soumission d'une Partie → niveau 2 (document officiel non adopté) ;
- soumission d'un observateur (ONG, OIG, agence onusienne…) → niveau 5 (position d'un acteur).

Analyse par IA (sources PUBLIQUES uniquement, voir llm.py) :
1. résumé + « points soulevés », chacun classé par la force du langage employé (constate < suggère < demande <
   exige) et accompagné d'une citation vérifiée MOT POUR MOT dans le document (sinon le point est rejeté) ;
   les points d'une Partie alimentent aussi la table `positions` (source_kind='soumission') ;
2. comparaison de deux soumissions du même appel : le modèle fusionne les points et qualifie chaque point
   (accord, différence partielle, conflit, un seul côté) ; l'indication d'alignement d'ensemble n'est PAS
   produite par le modèle mais calculée par des règles fixes (`verdict`), donc reproductible et vérifiable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from . import db, fetch, ingest, live, llm

API = "https://submissions.unfccc.int/api"
PORTAL = "https://submissions.unfccc.int/"
SOURCE_ID = "unfccc-submissions"
# Analyse automatique (le reste à la demande) : soumissions de Parties déposées depuis cette date.
AUTO_SINCE = os.environ.get("COP_SUB_ANALYSE_SINCE", "2026-01-01")
# Fichiers téléchargés par passage de la veille (le stock historique arrive progressivement, du plus récent au
# plus ancien) ; 0 = pas de téléchargement automatique.
DOWNLOAD_PER_TICK = int(os.environ.get("COP_SUB_PER_TICK", "60"))
DOC_EXT = {"pdf", "docx", "doc", "rtf", "odt", "txt", "htm", "html", "pptx", "xlsx"}
PROMPT_VERSION = "1"
ANALYSE_CHARS = 60000      # texte envoyé à l'analyse (au-delà : signalé « tronque »)
COMPARE_CHARS = 8000       # extrait de chaque texte joint aux points analysés, pour la comparaison
POSITIONS_PER_DOC = 6      # points versés dans « qui veut quoi » par soumission (les plus forts)
MAX_NEW_COMPARISONS = 4    # comparaisons nouvelles (appels IA) par requête « contre tout l'appel »

SCHEMA = """
CREATE TABLE IF NOT EXISTS sub_calls (
  id INTEGER PRIMARY KEY, title TEXT, issue TEXT, session TEXT, mandate TEXT, deadline TEXT, themes TEXT,
  bodies TEXT, audience TEXT, call_type TEXT, open INTEGER, upcoming INTEGER, modified TEXT
);
CREATE TABLE IF NOT EXISTS sub_items (
  id INTEGER PRIMARY KEY, call_id INTEGER, title TEXT, file_name TEXT, path TEXT, lang TEXT, submitted TEXT,
  entity TEXT, entity_type TEXT, kind TEXT, co_submitters TEXT, modified TEXT,
  doc_id INTEGER, status TEXT DEFAULT 'pending', tries INTEGER DEFAULT 0, error TEXT
);
CREATE INDEX IF NOT EXISTS sub_items_call ON sub_items(call_id);
CREATE INDEX IF NOT EXISTS sub_items_entity ON sub_items(entity);
CREATE INDEX IF NOT EXISTS sub_items_doc ON sub_items(doc_id);
CREATE TABLE IF NOT EXISTS sub_actors (sub_id INTEGER, actor TEXT, role TEXT, PRIMARY KEY(sub_id, actor));
CREATE INDEX IF NOT EXISTS sub_actors_actor ON sub_actors(actor);
CREATE TABLE IF NOT EXISTS sub_call_dossiers (
  call_id INTEGER, dossier TEXT, methode TEXT, score REAL, at TEXT, PRIMARY KEY(call_id, dossier)
);
CREATE INDEX IF NOT EXISTS sub_call_dossiers_dossier ON sub_call_dossiers(dossier);
CREATE TABLE IF NOT EXISTS sub_analysis (
  doc_id INTEGER PRIMARY KEY, sub_id INTEGER, summary TEXT, summary_en TEXT, matters TEXT, rejected INTEGER,
  model TEXT, prompt TEXT, at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS sub_comparisons (
  key TEXT PRIMARY KEY, a_doc INTEGER, b_doc INTEGER, points TEXT, verdict TEXT, counts TEXT, model TEXT, at TEXT
);
"""

LANGS = {"english": "en", "french": "fr", "spanish": "es", "russian": "ru", "chinese": "zh", "arabic": "ar"}
STRENGTH = {0: "aucune position", 1: "constate", 2: "suggère", 3: "demande", 4: "exige"}
STRENGTH_EN = {0: "no position", 1: "notes", 2: "suggests", 3: "calls for", 4: "demands"}
STRONG = 3  # à partir de « demande » : langage fort
RELATIONS = ["accord", "partiel", "conflit", "un_seul"]
VERDICTS = {
    "aligne": ("Potentiellement alignées", "Potentially aligned"),
    "partiel": ("Potentiellement partiellement alignées", "Potentially partially aligned"),
    "non_aligne": ("Potentiellement non alignées", "Potentially not aligned"),
    "sans_objet": ("Pas de position substantielle commune", "No substantive position available"),
}


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)
    if "iso3" not in {r[1] for r in conn.execute("PRAGMA table_info(sub_items)")}:
        conn.execute("ALTER TABLE sub_items ADD COLUMN iso3 TEXT")  # pays de l'auteur (groupes.to_iso3)
        conn.execute("CREATE INDEX IF NOT EXISTS sub_items_iso3 ON sub_items(iso3)")
        conn.commit()
    live.ensure_schema(conn)


def log(msg: str) -> None:
    print(f"[soumissions] {msg}", file=sys.stderr, flush=True)


# --- normalisation ----------------------------------------------------------------------------------

def parse_date(value: str | None) -> str | None:
    """« 2026-11-17T10:29:13Z » ou « 11/3/2026 2:28:00 PM » (format américain) → « 2026-11-17 »."""
    v = (value or "").strip()
    if not v:
        return None
    if re.match(r"\d{4}-\d{2}-\d{2}", v):
        return v[:10]
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def norm_session(value: str | None) -> str | None:
    """« COP 31 » → « COP31 », « SB 65 » → « SB65 » (forme des autres documents du corpus)."""
    v = re.sub(r"\s+", "", value or "")
    return v if v and v.upper() not in ("N/A", "NA") else None


def entity_kind(entity_type: str | None, entity: str, parties: set[str]) -> str:
    """party | observer. Le type est vide pour ~30 % des anciens dépôts : on reconnaît alors une Partie
    à son nom, s'il apparaît ailleurs avec le type « Party »."""
    t = (entity_type or "").strip().lower()
    if t == "party":
        return "party"
    if t:
        return "observer"
    return "party" if entity.strip().lower() in parties else "observer"


def co_submitters(description: str | None, entity: str) -> list[str]:
    """Co-auteurs ou groupe au nom duquel l'auteur soumet, d'après le champ Description (texte libre) :
    « on behalf of the LDC Group », « Republic of Korea », « Uruguay on behalf of G77 and China ». Un champ qui
    reprend le titre du document (commence par le nom de l'auteur sans « on behalf ») est ignoré."""
    d = (description or "").replace("\u200b", "").strip().split("\n")[0].strip()
    ent = entity.replace("\u200b", "").strip().lower()
    if not d or len(d) > 300:
        return []
    d = re.split(r"\.\s+", d, maxsplit=1)[0]  # « European Union and its Member States. This submission is… »
    m = re.search(r"\bon behalf(?: of)?\s+(?:the\s+)?(.+)$", d, re.I)
    if m:
        d = m.group(1)
    elif d.lower().startswith(ent):
        return []  # titre du document, pas des co-auteurs
    d = re.sub(r"^(this submission (was|is) )?(jointly )?(prepared|submitted|supported) by\s+", "", d, flags=re.I)
    d = re.sub(r"^of\s+(the\s+)?", "", d, flags=re.I)
    # « African Group of Negotiators as an input to the review » : coupé sur les seules tournures descriptives ;
    # jamais sur as / for / to / in seuls, présents dans des noms propres (« Coalition for Rainforest Nations »).
    d = re.split(r"\s+(?:as an? input|as inputs?|in response to|in relation to|for the purpose|pursuant to|"
                 r"to the call|regarding|on the topic of|with regard to|"
                 r"at (?:the )?(?:joint|opening|closing|apa|sbi|sbsta|sb ?\d|cop ?\d|cma ?\d|cmp ?\d|its))\b",
                 d, maxsplit=1, flags=re.I)[0]
    names = [re.sub(r"^the\s+", "", x.strip(" ."), flags=re.I) for x in re.split(r"\s*[,;]\s*", d) if x.strip(" .")]
    return [n for n in names if n.lower() != ent and len(n) < 90][:12]


GROUP_HINT = re.compile(r"group|G77|G-77|LDC|LMDC|AOSIS|AILAC|ALBA|AGN|BASIC|LLDC|SIDS|alliance|coalition|"
                        r"umbrella|EIG|arab|countries", re.I)


def speaks_for(item) -> str:
    """Auteur au sens politique : le groupe quand la Partie soumet en son nom (« Saudi Arabia » → « LMDC »)."""
    from .positions import normalize_actor

    cos = db.jload(item["co_submitters"])
    if len(cos) == 1 and GROUP_HINT.search(cos[0]):
        return normalize_actor(cos[0])
    return normalize_actor(item["entity"])


def is_document(file_name: str | None) -> bool:
    name = file_name or ""
    return "." in name and name.rsplit(".", 1)[1].lower() in DOC_EXT


def download_url(path: str) -> str:
    return f"{API}/files/download?serverRelativeFilePath={quote(path, safe='/')}"


def _clean_title(title: str) -> str:
    t = re.sub(r"^\d{6,}-*", "", title or "")          # horodatage en préfixe (202611171326---…)
    t = re.sub(r"^\d+_\d+_\d+-?", "", t)                 # identifiant SharePoint (53_29_1312…-)
    t = re.sub(r"[_]+", " ", t).strip(" -")
    return t


# --- collecte -----------------------------------------------------------------------------------------

def _get_json(client: fetch.Client, url: str) -> list[dict]:
    res = client.get(url, accept="application/json")
    if not res.ok:
        raise RuntimeError(f"{url} : {res.status} {res.error or ''}")
    data = json.loads(res.content)
    if not isinstance(data, list):
        raise RuntimeError(f"{url} : réponse inattendue")
    return data


def sync_index(conn, client: fetch.Client) -> dict[str, int]:
    """Relève la liste des appels et des soumissions (métadonnées seulement, deux requêtes)."""
    ensure_schema(conn)
    calls = _get_json(client, f"{API}/CallForSubmissions")
    subs = _get_json(client, f"{API}/Submissions")
    for c in calls:
        conn.execute(
            """INSERT INTO sub_calls(id,title,issue,session,mandate,deadline,themes,bodies,audience,call_type,open,
                 upcoming,modified) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title, issue=excluded.issue, session=excluded.session,
                 mandate=excluded.mandate, deadline=excluded.deadline, themes=excluded.themes, bodies=excluded.bodies,
                 audience=excluded.audience, call_type=excluded.call_type, open=excluded.open,
                 upcoming=excluded.upcoming, modified=excluded.modified""",
            (c["Id"], (c.get("Title") or "").strip(), (c.get("Issue") or "").strip(), norm_session(c.get("SessionName")),
             c.get("Mandate"), parse_date(c.get("Deadline")), db.jdump(c.get("Themes")), db.jdump(c.get("Bodies")),
             db.jdump(c.get("TargetAudience")), c.get("CallType"), int(bool(c.get("OpenCall"))),
             int(bool(c.get("IsUpcomingSession"))), c.get("ModifiedDate")))
    parties = {(s.get("Entity") or "").strip().lower() for s in subs if (s.get("EntityType") or "").lower() == "party"}
    known = {r[0] for r in conn.execute("SELECT id FROM sub_items")}
    for s in subs:
        entity = (s.get("Entity") or "").replace("\u200b", "").strip()
        conn.execute(
            """INSERT INTO sub_items(id,call_id,title,file_name,path,lang,submitted,entity,entity_type,kind,
                 co_submitters,modified) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET call_id=excluded.call_id, title=excluded.title,
                 file_name=excluded.file_name, entity=excluded.entity, entity_type=excluded.entity_type,
                 kind=excluded.kind, co_submitters=excluded.co_submitters, lang=excluded.lang,
                 submitted=excluded.submitted,
                 status=CASE WHEN excluded.path<>sub_items.path THEN 'pending' ELSE sub_items.status END,
                 path=excluded.path, modified=excluded.modified""",
            (s["Id"], s.get("CallForSubmissionId"), s.get("Title"), s.get("FileName"), s.get("ServerRelativeURL"),
             LANGS.get((s.get("Language") or "").strip().lower()), parse_date(s.get("SubmissionDate")), entity,
             s.get("EntityType"), entity_kind(s.get("EntityType"), entity, parties),
             db.jdump(co_submitters(s.get("Description"), entity)), s.get("ModifiedDate")))
    from . import groupes
    from .positions import normalize_actor

    idx = groupes.iso_index(conn)
    conn.executemany("UPDATE sub_items SET iso3=? WHERE id=?",
                     [(groupes.to_iso3(r["entity"], idx), r["id"]) for r in
                      conn.execute("SELECT id, entity FROM sub_items WHERE kind='party'").fetchall()])
    conn.execute("DELETE FROM sub_actors")
    rows = conn.execute("SELECT id, entity, co_submitters FROM sub_items").fetchall()
    conn.executemany("INSERT OR IGNORE INTO sub_actors(sub_id,actor,role) VALUES(?,?,?)",
                     [(r["id"], normalize_actor(r["entity"]), "auteur") for r in rows if r["entity"]]
                     + [(r["id"], normalize_actor(c), "co_auteur") for r in rows for c in db.jload(r["co_submitters"])])
    skip = [(r["id"],) for r in conn.execute("SELECT id, file_name FROM sub_items WHERE status='pending'")
            if not is_document(r["file_name"])]
    conn.executemany("UPDATE sub_items SET status='skipped' WHERE id=?", skip)
    conn.commit()
    db.meta_set(conn, "soumissions_index", db.now())
    map_calls(conn)
    return {"appels": len(calls), "soumissions": len(subs), "nouvelles": sum(1 for x in subs if x["Id"] not in known)}


def _meta(conn, item, call) -> dict[str, Any]:
    party = item["kind"] == "party"
    cos = db.jload(item["co_submitters"])
    who = item["entity"] + (f" (+ {', '.join(cos[:3])}{'…' if len(cos) > 3 else ''})" if cos else "")
    call_title = (call["title"] if call else "") or ""
    statement = call is not None and (call["call_type"] or "").lower() == "statement"
    title = f"{'Déclaration' if statement else 'Soumission'} — {who} — {_clean_title(item['title'] or '')}"
    if call_title and call_title.lower() not in title.lower():
        title += f" — appel : {call_title[:140]}"
    session = call["session"] if call else None
    if session:
        title += f" ({session})"
    return {
        # Cote propre à chaque dépôt : sinon la première cote citée dans le texte (FCCC/SB/2026/L.2…) serait prise
        # pour celle du document et des soumissions différentes passeraient pour des versions l'une de l'autre.
        "symbol": f"UNFCCC-SUB/{item['id']}",
        "title": title[:400], "level": "2" if party else "5", "org": item["entity"],
        "doc_type": "declaration" if statement else "soumission", "session": session,
        "parties": [item["entity"], *cos] if party else None,
        "topics": db.jload(call["themes"]) if call else None, "lang": item["lang"], "published": item["submitted"],
    }


def download_pending(conn, client: fetch.Client, limit: int = 100, *, since: str | None = None,
                     parties_only: bool = False, call_id: int | None = None) -> dict[str, int]:
    """Télécharge et indexe les soumissions pas encore reçues, de la plus récente à la plus ancienne."""
    ensure_schema(conn)
    where, params = ["status='pending'", "tries<3"], []
    if since:
        where.append("submitted>=?")
        params.append(since)
    if parties_only:
        where.append("kind='party'")
    if call_id:
        where.append("call_id=?")
        params.append(call_id)
    rows = conn.execute(f"SELECT * FROM sub_items WHERE {' AND '.join(where)} "
                        f"ORDER BY COALESCE(submitted,'') DESC, id DESC LIMIT ?", (*params, limit)).fetchall()
    counts: dict[str, int] = {}
    for item in rows:
        call = conn.execute("SELECT * FROM sub_calls WHERE id=?", (item["call_id"],)).fetchone()
        url = download_url(item["path"])
        res = client.get(url)
        if not res.ok or fetch.is_antibot(res):
            conn.execute("UPDATE sub_items SET tries=tries+1, error=?, status=CASE WHEN tries>=2 THEN 'error' "
                         "ELSE status END WHERE id=?", (f"{res.status} {res.error or ''}"[:200], item["id"]))
            conn.commit()
            counts["erreur"] = counts.get("erreur", 0) + 1
            continue
        doc_id, new, status = ingest.ingest_bytes(conn, res.content, url=url, mime=res.mime,
                                                  meta=_meta(conn, item, call), source_id=SOURCE_ID,
                                                  filename=item["file_name"])
        conn.execute("UPDATE sub_items SET doc_id=?, status=?, error=NULL WHERE id=?", (doc_id, status, item["id"]))
        conn.commit()
        key = status if new else "deja_connu"
        counts[key] = counts.get(key, 0) + 1
    return counts


def harvest_tick(conn, client: fetch.Client, per_tick: int | None = None) -> dict[str, Any]:
    """Un passage de la veille : index toutes les 30 min, puis un lot de téléchargements."""
    ensure_schema(conn)
    out: dict[str, Any] = {}
    last = db.meta_get(conn, "soumissions_index")
    age = (datetime.now(timezone.utc) - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
           ).total_seconds() if last else None
    if age is None or age > 1800:
        out["index"] = sync_index(conn, client)
    n = DOWNLOAD_PER_TICK if per_tick is None else per_tick
    if n:
        # D'abord tout ce qui concerne la session en cours, puis le stock, du plus récent au plus ancien.
        got = download_pending(conn, client, limit=n, since=AUTO_SINCE)
        left = n - sum(got.values())
        if left > 0:
            for k, v in download_pending(conn, client, limit=left).items():
                got[k] = got.get(k, 0) + v
        if got:
            out["telechargements"] = got
    return out


# --- consultation -------------------------------------------------------------------------------------

def _call_public(c) -> dict[str, Any]:
    return {"appel": c["id"], "titre": c["title"], "objet": c["issue"] if c["issue"] != c["title"] else None,
            "session": c["session"], "type": c["call_type"], "themes": db.jload(c["themes"]),
            "organes": db.jload(c["bodies"]), "date_limite": c["deadline"], "ouvert": bool(c["open"]),
            "mandat": c["mandate"] if c["mandate"] not in (None, "N/A") else None}


def _item_public(conn, it, with_analysis: bool = False) -> dict[str, Any]:
    out = {"soumission": it["id"], "auteur": it["entity"],
           "type_auteur": "Partie" if it["kind"] == "party" else (it["entity_type"] or "observateur"),
           "co_auteurs": db.jload(it["co_submitters"]) or None, "date": it["submitted"], "langue": it["lang"],
           "fichier": it["file_name"], "url_source": download_url(it["path"]),
           "niveau": "2" if it["kind"] == "party" else "5"}
    if it["doc_id"]:
        from . import config

        out["doc_id"] = it["doc_id"]
        out["lien"] = f"{config.PUBLIC_BASE}/doc/{it['doc_id']}"
        if with_analysis:
            a = conn.execute("SELECT summary, matters FROM sub_analysis WHERE doc_id=? AND error IS NULL",
                             (it["doc_id"],)).fetchone()
            if a:
                out["resume"] = a["summary"]
                out["nb_points"] = len(db.jload(a["matters"]))
    else:
        out["etat"] = {"pending": "pas encore téléchargée", "skipped": "format non textuel",
                       "error": "téléchargement en échec"}.get(it["status"], it["status"])
    return {k: v for k, v in out.items() if v is not None}


def list_calls(conn, *, requete: str | None = None, theme: str | None = None, session: str | None = None,
               ouverts: bool = False, limite: int = 40, dossier: str | None = None) -> list[dict[str, Any]]:
    ensure_schema(conn)
    where, params = ["1=1"], []
    if dossier:
        where.append("c.id IN (SELECT call_id FROM sub_call_dossiers WHERE dossier=?)")
        params.append(live.dossier_from_alias(dossier) or dossier)
    for word in (requete or "").split():
        where.append("(c.title LIKE ? OR c.issue LIKE ? OR c.themes LIKE ?)")
        params += [f"%{word}%"] * 3
    if theme:
        where.append("c.themes LIKE ?")
        params.append(f"%{theme}%")
    if session:
        where.append("c.session LIKE ?")
        params.append(f"%{norm_session(session) or session}%")
    if ouverts:
        where.append("(c.open=1 OR c.upcoming=1)")
    rows = conn.execute(
        f"""SELECT c.*, COUNT(i.id) n, SUM(i.kind='party') np, MAX(i.submitted) last FROM sub_calls c
            LEFT JOIN sub_items i ON i.call_id=c.id WHERE {' AND '.join(where)}
            GROUP BY c.id ORDER BY COALESCE(MAX(i.submitted), c.modified) DESC LIMIT ?""",
        (*params, limite)).fetchall()
    return [{**_call_public(r), "nb_soumissions": r["n"], "nb_parties": r["np"] or 0, "derniere": r["last"]}
            for r in rows]


def call_detail(conn, call_id: int, *, parties_only: bool = False) -> dict[str, Any] | None:
    ensure_schema(conn)
    c = conn.execute("SELECT * FROM sub_calls WHERE id=?", (call_id,)).fetchone()
    if not c:
        return None
    items = conn.execute("SELECT * FROM sub_items WHERE call_id=? AND (?=0 OR kind='party') "
                         "ORDER BY kind<>'party', submitted DESC", (call_id, int(parties_only))).fetchall()
    return {**_call_public(c), "portail": PORTAL, "nb_soumissions": len(items),
            "soumissions": [_item_public(conn, it, with_analysis=True) for it in items]}


class UnknownActor(ValueError):
    """Nom introuvable tel quel : on propose des candidats au lieu d'en choisir un en silence."""


def actor_candidates(conn, name: str, limit: int = 12) -> list[str]:
    from .positions import normalize_actor

    words = [w for w in re.findall(r"\w+", name.lower()) if len(w) > 2]
    if not words:
        return []
    like = " AND ".join("lower(actor) LIKE ?" for _ in words)
    return [r[0] for r in conn.execute(
        f"SELECT actor, COUNT(*) n FROM sub_actors WHERE {like} GROUP BY actor ORDER BY n DESC LIMIT ?",
        (*[f"%{w}%" for w in words], limit))] or [normalize_actor(name)]


def _actor_items(conn, name: str, call_id: int | None = None, indexed_only: bool = False) -> list:
    """Soumissions d'un acteur, par ÉGALITÉ sur le nom canonique (auteur ou co-auteur). Aucun résultat → UnknownActor
    avec la liste des noms proches (« Mali » ne renvoie jamais la Somalie)."""
    from .positions import normalize_actor

    ensure_schema(conn)
    actor = normalize_actor(name)
    rows = conn.execute(
        f"""SELECT DISTINCT i.*, c.title ctitle, c.session csession FROM sub_actors a JOIN sub_items i ON i.id=a.sub_id
            LEFT JOIN sub_calls c ON c.id=i.call_id
            WHERE a.actor=? {'AND i.call_id=?' if call_id else ''} {'AND i.doc_id IS NOT NULL' if indexed_only else ''}
            ORDER BY i.submitted DESC""", (actor, call_id) if call_id else (actor,)).fetchall()
    if not rows:
        cands = [c for c in actor_candidates(conn, name) if c != actor]
        raise UnknownActor(f"aucune soumission{' indexée' if indexed_only else ''} pour « {name} » (nom canonique "
                           f"« {actor} »){f' dans l’appel {call_id}' if call_id else ''}"
                           + (f" ; noms proches : {', '.join(cands)}" if cands else ""))
    return rows


def by_entity(conn, entity: str, limite: int = 60) -> list[dict[str, Any]]:
    rows = _actor_items(conn, entity)[:limite]
    return [{**_item_public(conn, r, with_analysis=True), "appel": r["call_id"], "titre_appel": r["ctitle"],
             "session": r["csession"]} for r in rows]


def item_for_doc(conn, doc_id: int):
    ensure_schema(conn)
    return conn.execute("SELECT * FROM sub_items WHERE doc_id=? ORDER BY id DESC LIMIT 1", (doc_id,)).fetchone()


def stats(conn) -> dict[str, Any]:
    ensure_schema(conn)
    r = conn.execute("""SELECT COUNT(*) n, SUM(kind='party') np, SUM(doc_id IS NOT NULL) got,
                          SUM(status='pending') pend, SUM(status IN ('skipped','error')) ko FROM sub_items""").fetchone()
    a = conn.execute("SELECT COUNT(*) FROM sub_analysis WHERE error IS NULL").fetchone()[0]
    k = conn.execute("SELECT COUNT(*) FROM sub_comparisons").fetchone()[0]
    calls = conn.execute("SELECT COUNT(*) FROM sub_calls").fetchone()[0]
    return {"appels": calls, "soumissions": r["n"], "dont_parties": r["np"], "indexees": r["got"],
            "en_attente": r["pend"], "non_textuelles_ou_echec": r["ko"], "analysees": a, "comparaisons": k,
            "index_releve_le": db.meta_get(conn, "soumissions_index")}


# --- analyse d'une soumission ------------------------------------------------------------------------------

ANALYSE_SYSTEM = """Tu analyses UNE soumission officielle à la CCNUCC (une Partie ou un observateur répond à un appel
à soumissions, ou déclaration en plénière). Outil indépendant de suivi de la COP31 ; zéro invention.

Rends :
- summary : 3 à 5 phrases en français, neutres : ce que l'auteur demande, propose ou refuse.
- summary_en : la même chose en anglais.
- matters : les points de négociation soulevés (8 au plus pour un document court, 15 au plus pour un long),
  du plus important au moins important. Pour chacun :
  - matter : le sujet en quelques mots, en anglais onusien (ex. « GGA indicators: means of implementation »).
  - position : une phrase en français, ce que veut l'auteur sur ce point.
  - strength : force du langage EMPLOYÉ PAR L'AUTEUR (pas ton jugement sur l'importance) :
      1 = constate (notes, acknowledges, recognizes, welcomes, takes note) ;
      2 = suggère (suggests, invites, encourages, could, may consider, would welcome) ;
      3 = demande (calls for, supports, proposes, recommends, should, requests, expects) ;
      4 = exige (urges, insists, must, demands, essential, non-negotiable, cannot accept, strongly oppose).
  - verb : le verbe ou la tournure qui fonde ce classement, recopié du texte.
  - stance : attitude de l'auteur, parmi {stances}.
  - dossier : identifiant de la liste fournie, ou « autre ».
  - quote : extrait recopié MOT POUR MOT du document (20 à 300 caractères, sans le modifier ni le traduire)
    qui établit la position. Pas d'extrait exact possible → ne retiens pas le point.
Ne retiens que ce que dit l'auteur (pas ce qu'il rapporte d'autres). Document vide ou purement administratif
(lettre de transmission, liste de noms) → summary bref et matters vide."""

ANALYSE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["summary", "summary_en", "matters"],
    "properties": {
        "summary": {"type": "string"}, "summary_en": {"type": "string"},
        "matters": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["matter", "position", "strength", "verb", "stance", "dossier", "quote"],
            "properties": {
                "matter": {"type": "string"}, "position": {"type": "string"},
                "strength": {"type": "integer", "enum": [1, 2, 3, 4]}, "verb": {"type": "string"},
                "stance": {"type": "string"}, "dossier": {"type": "string"}, "quote": {"type": "string"},
            }}},
    },
}


def _doc_text(conn, doc_id: int, limit: int = 60000) -> str:
    return "\n".join(r[0] for r in conn.execute("SELECT text FROM passages WHERE doc_id=? ORDER BY seq",
                                                (doc_id,)))[:limit]


def _quote_ok(quote: str, hay: str) -> bool:
    from .positions import _norm

    q = (quote or "").strip()
    return len(q) >= 15 and _norm(q) in hay


def analysis(conn, doc_id: int) -> dict[str, Any] | None:
    ensure_schema(conn)
    a = conn.execute("SELECT * FROM sub_analysis WHERE doc_id=?", (doc_id,)).fetchone()
    if not a or (a["prompt"] or "") != PROMPT_VERSION:
        return None  # pas d'analyse, ou analyse faite avec une autre version du prompt / de l'échelle
    total = conn.execute("SELECT COALESCE(SUM(length(text)+1),0) FROM passages WHERE doc_id=?", (doc_id,)).fetchone()[0]
    out = {"doc_id": doc_id, "tronque": total > ANALYSE_CHARS or None, "resume": a["summary"], "summary_en": a["summary_en"],
           "points": db.jload(a["matters"]), "points_rejetes": a["rejected"], "modele": a["model"],
           "analyse_le": a["at"], "erreur": a["error"],
           "avertissement": "Analyse produite par IA à partir du seul texte de la soumission ; chaque point porte une "
                            "citation vérifiée mot pour mot. Vérifier dans l'original avant de s'y fier."}
    return {k: v for k, v in out.items() if v not in (None, "")}


def analyze(conn, doc_id: int, *, force: bool = False) -> dict[str, Any]:
    """Résumé et points soulevés (force du langage, citation vérifiée). Mis en cache."""
    ensure_schema(conn)
    if not force:
        cached = analysis(conn, doc_id)
        if cached and not cached.get("erreur"):
            return cached
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        raise ValueError(f"document {doc_id} introuvable")
    if doc["access"] != "public":
        raise ValueError("analyse réservée aux documents publics")  # aucun document interne vers l'API
    if not llm.enabled():
        raise RuntimeError("analyse IA indisponible (pas de clé API configurée)")
    item = item_for_doc(conn, doc_id)
    text = _doc_text(conn, doc_id, ANALYSE_CHARS)
    from .positions import STANCE_VALUES, _dossier_list, _norm

    if len(text) < 200:
        out = {"summary": "Document trop court ou sans texte exploitable.", "summary_en": "", "matters": []}
    else:
        author = (item["entity"] if item else doc["org"]) or "?"
        user = (f"Dossiers :\n{_dossier_list()}\n\nDocument : {doc['title']}\nAuteur : {author}"
                f"{' (Partie)' if item and item['kind'] == 'party' else ''}\nDate : {doc['published'] or '?'}\n\n"
                f"<<<\n{text}\n>>>")
        try:
            out = llm.json_call(conn, model=llm.EXTRACT_MODEL, purpose="soumission-analyse",
                                system=ANALYSE_SYSTEM.format(stances=", ".join(STANCE_VALUES)),
                                user=user, schema=ANALYSE_SCHEMA, name="analyse")
        except llm.BudgetExceeded:
            raise
        except Exception as exc:
            conn.execute("INSERT OR REPLACE INTO sub_analysis(doc_id,sub_id,model,prompt,at,error) VALUES(?,?,?,?,?,?)",
                         (doc_id, item["id"] if item else None, llm.EXTRACT_MODEL, PROMPT_VERSION, live.now_iso(),
                          str(exc)[:300]))
            conn.commit()
            raise
    hay = _norm(text)
    slugs = {d["slug"] for d in live.dossiers()}
    kept, rejected = [], 0
    for m in out.get("matters", []):
        if not _quote_ok(m.get("quote", ""), hay):
            rejected += 1
            continue
        s = int(m.get("strength") or 1)
        kept.append({"point": m["matter"].strip()[:200], "position": m["position"].strip()[:500], "force": s,
                     "force_libelle": STRENGTH.get(s), "verbe": (m.get("verb") or "").strip()[:80],
                     "attitude": m.get("stance") if m.get("stance") in STANCE_VALUES else "neutre",
                     "dossier": m.get("dossier") if m.get("dossier") in slugs else None,
                     "citation": m["quote"].strip()[:400]})
    kept.sort(key=lambda m: -m["force"])
    conn.execute(
        """INSERT OR REPLACE INTO sub_analysis(doc_id,sub_id,summary,summary_en,matters,rejected,model,prompt,at,error)
           VALUES(?,?,?,?,?,?,?,?,?,NULL)""",
        (doc_id, item["id"] if item else None, (out.get("summary") or "").strip(), (out.get("summary_en") or "").strip(),
         json.dumps(kept, ensure_ascii=False), rejected, llm.EXTRACT_MODEL, PROMPT_VERSION, live.now_iso()))
    # Une soumission récente de Partie est une prise de position officielle : ses points les plus forts nourrissent
    # « qui veut quoi » (rubrique à part : positions écrites, pas relevées pendant la COP). Les anciennes
    # soumissions analysées à la demande restent dans l'analyse sans polluer les positions du moment.
    conn.execute("DELETE FROM positions WHERE source_kind='soumission' AND doc_id=?", (doc_id,))
    call = conn.execute("SELECT upcoming FROM sub_calls WHERE id=?", (item["call_id"],)).fetchone() if item else None
    recent = item is not None and ((item["submitted"] or "") >= AUTO_SINCE or bool(call and call["upcoming"]))
    if item and item["kind"] == "party" and recent:
        actor = speaks_for(item)
        for m in kept[:POSITIONS_PER_DOC]:
            key = "s|" + hashlib.sha1(f"{doc_id}|{actor}|{_norm(m['citation'])[:120]}".encode()).hexdigest()
            conn.execute(
                """INSERT OR IGNORE INTO positions(key,dossier,actor,issue,stance,summary,quote,source_kind,source_label,
                     source_url,doc_id,said_on,created_at,access) VALUES(?,?,?,?,?,?,?,'soumission',?,?,?,?,?,'public')""",
                (key, m["dossier"], actor, m["point"], m["attitude"], m["position"], m["citation"],
                 f"Soumission CCNUCC — {item['entity']} ({item['submitted'] or ''})"[:200], f"/doc/{doc_id}", doc_id,
                 item["submitted"], live.now_iso()))
    conn.commit()
    return analysis(conn, doc_id) or {}


def pending_analysis(conn, limit: int = 5) -> list[int]:
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT i.doc_id FROM sub_items i JOIN documents d ON d.id=i.doc_id
           LEFT JOIN sub_analysis a ON a.doc_id=i.doc_id
           WHERE i.kind='party' AND i.submitted>=? AND d.status='indexed' AND d.access='public'
             AND (a.doc_id IS NULL OR (a.error IS NOT NULL AND a.at < datetime('now','-1 day')))
           GROUP BY i.doc_id ORDER BY MAX(i.submitted) DESC LIMIT ?""", (AUTO_SINCE, limit)).fetchall()
    return [r[0] for r in rows]


def analyse_tick(conn, limit: int = 5) -> int:
    if not llm.enabled():
        return 0
    n = 0
    for doc_id in pending_analysis(conn, limit):
        try:
            analyze(conn, doc_id)
            n += 1
        except llm.BudgetExceeded:
            raise
        except Exception as exc:
            log(f"analyse {doc_id} : {exc}")
    if n:
        map_calls(conn)  # de nouveaux points peuvent rattacher un appel sans règle
    return n


# --- comparaison de deux soumissions -------------------------------------------------------------------------

COMPARE_SYSTEM = """Tu compares les positions de DEUX soumissions à la CCNUCC (A et B), en t'en tenant à ce qu'elles
disent. Outil indépendant de suivi de la COP31 ; zéro invention.

Étape 1 — points de comparaison : fusionne les points soulevés de A et de B en une seule liste. Deux points qui
traitent du même enjeu n'en font qu'un ; un point soulevé d'un seul côté est gardé. Si l'un des documents contient
un enjeu majeur portant une de ses propositions principales et absent des deux listes, ajoute-le.

Étape 2 — pour chaque point :
- point : l'enjeu en quelques mots (anglais onusien) ;
- a_position / b_position : ce que dit chacun, une phrase courte en français ("" s'il ne se prononce pas) ;
- a_force / b_force : force du langage employé par chacun : 0 = ne se prononce pas, 1 = constate, 2 = suggère,
  3 = demande, 4 = exige. Quand le point correspond à un point soulevé fourni, reprendre SA force (elle a été
  établie sur une citation vérifiée) ; ne jamais la relever ;
- relation :
  « accord » = mêmes demandes ou demandes compatibles ;
  « partiel » = différence matérielle sans exclusion mutuelle (priorités, portée ou modalités différentes) ;
  « conflit » = négation directe, exigences mutuellement exclusives, calendriers contradictoires, mécanismes
    incompatibles (ex. « sortie du charbon d'ici 2030 » / « pas de sortie avant 2040 »). Mettre en garde contre ce
    que l'autre demande, ou dire qu'il n'en est pas besoin, est aussi un conflit ;
  « un_seul » = un seul des deux documents prend position ;
- explication : une phrase en français qui justifie la relation.
Ne conclus PAS sur l'alignement d'ensemble : il est calculé ensuite par des règles fixes."""

COMPARE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["points"],
    "properties": {"points": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["point", "a_position", "b_position", "a_force", "b_force", "relation", "explication"],
        "properties": {
            "point": {"type": "string"}, "a_position": {"type": "string"}, "b_position": {"type": "string"},
            "a_force": {"type": "integer", "enum": [0, 1, 2, 3, 4]},
            "b_force": {"type": "integer", "enum": [0, 1, 2, 3, 4]},
            "relation": {"type": "string", "enum": RELATIONS}, "explication": {"type": "string"},
        }}}},
}


def verdict(points: list[dict]) -> tuple[str, dict[str, Any]]:
    """Indication d'alignement calculée par règles fixes (aucune part laissée au modèle).

    - Points « centraux » : les deux soumissions se prononcent et emploient toutes deux un langage fort
      (force ≥ 3 : demande / exige). Les points d'un seul côté ne comptent jamais.
    - Non alignées : au moins 2 points centraux en conflit, ou 1 point central en conflit avec un accord sur
      40 % ou moins des points centraux.
    - Pas de position substantielle commune : aucun point central.
    - Partiellement alignées : au moins la moitié des points centraux en différence partielle, ou un conflit sur un
      point secondaire (langage faible d'un côté au moins), ou 1 point central en conflit avec un accord sur plus de
      40 % des points centraux.
    - Alignées : sinon (aucun conflit, moins de la moitié des points centraux en différence partielle).
    """
    both = [p for p in points if p.get("relation") != "un_seul" and p.get("a_force", 0) > 0 and p.get("b_force", 0) > 0]
    central = [p for p in both if p["a_force"] >= STRONG and p["b_force"] >= STRONG]
    secondary = [p for p in both if p not in central]
    n = len(central)
    c = {r: sum(1 for p in central if p["relation"] == r) for r in ("accord", "partiel", "conflit")}
    soft_conflicts = sum(1 for p in secondary if p["relation"] == "conflit")
    counts = {"points": len(points), "points_communs": len(both), "points_centraux": n,
              "centraux_accord": c["accord"], "centraux_partiel": c["partiel"], "centraux_conflit": c["conflit"],
              "conflits_secondaires": soft_conflicts,
              "un_seul_cote": sum(1 for p in points if p not in both)}
    agree_share = c["accord"] / n if n else 0.0
    if n and c["conflit"] >= 2:
        v, rule = "non_aligne", f"{c['conflit']} points centraux en conflit (≥ 2)"
    elif n and c["conflit"] == 1 and agree_share <= 0.4:
        v, rule = "non_aligne", f"1 point central en conflit et accord sur {agree_share:.0%} des points centraux (≤ 40 %)"
    elif n == 0:
        v, rule = "sans_objet", "aucun point où les deux emploient un langage fort"
    elif c["conflit"] == 1:
        v, rule = "partiel", f"1 point central en conflit mais accord sur {agree_share:.0%} des points centraux (> 40 %)"
    elif c["partiel"] >= n / 2:
        v, rule = "partiel", f"{c['partiel']} points centraux sur {n} en différence partielle (≥ 50 %)"
    elif soft_conflicts:
        v, rule = "partiel", f"{soft_conflicts} conflit(s) sur des points secondaires (langage faible)"
    else:
        v, rule = "aligne", f"aucun conflit, {c['partiel']} point(s) central(aux) sur {n} en différence partielle (< 50 %)"
    counts["regle"] = rule
    return v, counts


def _matters_block(conn, doc_id: int) -> str:
    a = analysis(conn, doc_id) or {}
    lines = [f"- [{m['force']} {m['force_libelle']}] {m['point']} : {m['position']} (« {m['citation'][:200]} »)"
             for m in a.get("points", [])]
    return "\n".join(lines) or "(aucun point relevé)"


def _who(conn, doc_id: int) -> str:
    it = item_for_doc(conn, doc_id)
    if it:
        group = speaks_for(it)
        from .positions import normalize_actor

        return it["entity"] if group == normalize_actor(it["entity"]) else f"{group} (soumis par {it['entity']})"
    d = conn.execute("SELECT org, title FROM documents WHERE id=?", (doc_id,)).fetchone()
    return (d["org"] or d["title"] or str(doc_id)) if d else str(doc_id)


def compare(conn, doc_a: int, doc_b: int, *, force: bool = False) -> dict[str, Any]:
    """Compare deux soumissions point par point ; verdict par règles fixes. Mis en cache (paire non ordonnée)."""
    ensure_schema(conn)
    if doc_a == doc_b:
        raise ValueError("deux documents différents sont nécessaires")
    lo, hi = sorted((doc_a, doc_b))
    for d in (lo, hi):
        analyze(conn, d)
    stamps = [conn.execute("SELECT at FROM sub_analysis WHERE doc_id=?", (d,)).fetchone()[0] for d in (lo, hi)]
    # Clé : paire + version + date des deux analyses (une ré-analyse invalide la comparaison).
    key = f"{lo}|{hi}|{PROMPT_VERSION}|{stamps[0]}|{stamps[1]}"
    row = None if force else conn.execute("SELECT * FROM sub_comparisons WHERE key=?", (key,)).fetchone()
    if not row:
        user = "\n\n".join(
            f"=== Document {tag} — {_who(conn, d)} (doc {d}) ===\nPoints soulevés :\n{_matters_block(conn, d)}\n\n"
            f"Texte (début, pour le contexte) :\n<<<\n{_doc_text(conn, d, COMPARE_CHARS)}\n>>>"
            for tag, d in (("A", lo), ("B", hi)))
        out = llm.json_call(conn, model=llm.EXTRACT_MODEL, purpose="soumission-comparaison", system=COMPARE_SYSTEM,
                            user=user, schema=COMPARE_SCHEMA, name="comparaison", max_tokens=8000)
        pts = [{**p, "a_force": int(p["a_force"]), "b_force": int(p["b_force"])} for p in out.get("points", [])]
        v, counts = verdict(pts)
        conn.execute("INSERT OR REPLACE INTO sub_comparisons(key,a_doc,b_doc,points,verdict,counts,model,at) "
                     "VALUES(?,?,?,?,?,?,?,?)", (key, lo, hi, json.dumps(pts, ensure_ascii=False), v,
                                                 json.dumps(counts), llm.EXTRACT_MODEL, live.now_iso()))
        conn.commit()
        row = conn.execute("SELECT * FROM sub_comparisons WHERE key=?", (key,)).fetchone()
    pts = json.loads(row["points"])
    swap = doc_a != lo  # présenter dans l'ordre demandé
    if swap:
        pts = [{**p, "a_position": p["b_position"], "b_position": p["a_position"], "a_force": p["b_force"],
                "b_force": p["a_force"]} for p in pts]
    from . import config

    for p in pts:
        p["a_force_libelle"], p["b_force_libelle"] = STRENGTH.get(p["a_force"]), STRENGTH.get(p["b_force"])
        p["central"] = p["relation"] != "un_seul" and p["a_force"] >= STRONG and p["b_force"] >= STRONG
    order = {"conflit": 0, "partiel": 1, "accord": 2, "un_seul": 3}
    pts.sort(key=lambda p: (not p["central"], order.get(p["relation"], 9)))
    return {
        "a": {"doc_id": doc_a, "auteur": _who(conn, doc_a), "lien": f"{config.PUBLIC_BASE}/doc/{doc_a}"},
        "b": {"doc_id": doc_b, "auteur": _who(conn, doc_b), "lien": f"{config.PUBLIC_BASE}/doc/{doc_b}"},
        "indication": row["verdict"], "indication_libelle": VERDICTS[row["verdict"]][0],
        "indication_en": VERDICTS[row["verdict"]][1], "decompte": json.loads(row["counts"]), "points": pts,
        "methode": "Deux étapes. (1) IA : fusion des points des deux soumissions, qualification de chacun (accord / "
                   "partiel / conflit / un seul côté) et force du langage de chaque côté, en reprenant celle des points "
                   "analysés (citations vérifiées : analyser via soumissions(document=…)). (2) Règles fixes : "
                   "l'indication d'ensemble est calculée à partir de ces qualifications (decompte.regle). Le verdict "
                   "dépend donc aussi du jugement de l'IA à l'étape 1. Aide à l'analyse, pas un jugement : vérifier "
                   "dans les originaux.",
        "tronque": [x["doc_id"] for x in (analysis(conn, doc_a) or {}, analysis(conn, doc_b) or {}) if x.get("tronque")]
                   or None,
        "compare_le": row["at"], "modele": row["model"],
    }


def _cached_pair(conn, x: int, y: int) -> bool:
    lo, hi = sorted((x, y))
    return conn.execute("SELECT 1 FROM sub_comparisons WHERE a_doc=? AND b_doc=? AND key LIKE ?",
                        (lo, hi, f"{lo}|{hi}|{PROMPT_VERSION}|%")).fetchone() is not None


def compare_in_call(conn, doc_id: int, *, max_n: int = 6, parties_only: bool = True,
                    max_new: int = MAX_NEW_COMPARISONS) -> dict[str, Any]:
    """Compare une soumission avec les autres soumissions (de Parties) du même appel. Les comparaisons déjà en
    cache passent d'abord ; au plus `max_new` nouvelles (appels IA) par requête, pour rester rapide et sobre."""
    item = item_for_doc(conn, doc_id)
    if not item:
        raise ValueError("ce document n'est pas une soumission CCNUCC connue")
    others = conn.execute(
        """SELECT doc_id, MAX(entity) entity FROM sub_items WHERE call_id=? AND doc_id IS NOT NULL AND doc_id<>?
             AND (?=0 OR kind='party') GROUP BY doc_id ORDER BY MAX(submitted) DESC""",
        (item["call_id"], doc_id, int(parties_only))).fetchall()
    others = sorted(others, key=lambda r: not _cached_pair(conn, doc_id, r["doc_id"]))
    results, skipped, new = [], [], 0
    for r in others:
        cached = _cached_pair(conn, doc_id, r["doc_id"])
        if len(results) >= max_n or (not cached and new >= max_new):
            skipped.append(r["entity"])
            continue
        try:
            new += int(not cached)
            c = compare(conn, doc_id, r["doc_id"])
            results.append({"avec": c["b"], "indication": c["indication"], "indication_libelle": c["indication_libelle"],
                            "regle": c["decompte"].get("regle"),
                            "conflits": [p["point"] for p in c["points"] if p["relation"] == "conflit"][:5],
                            "accords": [p["point"] for p in c["points"] if p["relation"] == "accord" and p["central"]][:5]})
        except llm.BudgetExceeded:
            raise
        except Exception as exc:
            results.append({"avec": {"doc_id": r["doc_id"], "auteur": r["entity"]}, "erreur": str(exc)[:200]})
    call = conn.execute("SELECT * FROM sub_calls WHERE id=?", (item["call_id"],)).fetchone()
    return {"soumission": {"doc_id": doc_id, "auteur": _who(conn, doc_id)},
            "appel": _call_public(call) if call else item["call_id"], "comparaisons": results,
            "non_comparees": skipped or None,
            "suite": "Relancer la même demande pour comparer les suivantes (les comparaisons faites restent en cache)."
                     if skipped else None,
            "methode": "Détail point par point : comparer_soumissions(a, b)."}


def resolve_pair(conn, a: str, b: str, appel: int | None = None,
                 dossier: str | None = None) -> tuple[int, int, int | None]:
    """Deux références (doc_id ou nom d'acteur : Partie ou groupe, « EU », « UE », « LDC »…) → deux documents,
    de préférence dans le même appel (le plus récent auquel les deux ont répondu, ou `appel`).
    Renvoie (doc_a, doc_b, appel commun ou None). Nom inconnu → UnknownActor avec des noms proches."""
    ensure_schema(conn)

    def items(ref: str) -> list:
        if str(ref).strip().isdigit():
            it = item_for_doc(conn, int(ref))
            if not it:
                raise ValueError(f"le document {ref} n'est pas une soumission CCNUCC connue")
            return [it]
        return _actor_items(conn, ref, appel, indexed_only=True)

    da, db_ = items(a), items(b)
    if dossier:
        calls = set(dossier_call_ids(conn, live.dossier_from_alias(dossier) or dossier))
        da2, db2 = [x for x in da if x["call_id"] in calls], [x for x in db_ if x["call_id"] in calls]
        if not da2 or not db2:
            raise UnknownActor(f"pas de soumission de « {a if not da2 else b} » sur un appel du dossier « {dossier} »")
        da, db_ = da2, db2
    calls_b = {x["call_id"]: x for x in db_}
    for x in da:  # du plus récent au plus ancien
        if x["call_id"] in calls_b:
            return x["doc_id"], calls_b[x["call_id"]]["doc_id"], x["call_id"]
    return da[0]["doc_id"], db_[0]["doc_id"], None


def latest_doc(conn, ref: str, appel: int | None = None) -> int:
    if str(ref).strip().isdigit():
        return int(ref)
    return _actor_items(conn, ref, appel, indexed_only=True)[0]["doc_id"]



# --- rattachement des appels aux dossiers de négociation ------------------------------------------------------
# Sans IA. 1) règles sur titre, objet, thèmes et organes de l'appel (table versionnée ci-dessous, slugs de
# catalog/dossiers.yaml) ; 2) à défaut, dossier majoritaire parmi les points analysés de ses soumissions
# (part ≥ POINTS_SHARE, au moins POINTS_MIN points) ; 3) un rattachement « manuel » (CLI) prime et n'est jamais écrasé.

RULES_VERSION = "2"
POINTS_SHARE = 0.4
POINTS_MIN = 3
CALL_RULES: list[tuple[str, str, float]] = [
    # (dossier, motif sur le texte de l'appel en minuscules, score : 1 = spécifique, 0.6 = large)
    ("adaptation", r"global goal on adaptation|\bgga\b|adaptation indicator|national adaptation plan|\bnaps?\b|"
                   r"adaptation committee|adaptation fund|nairobi work programme|bel[ée]m adaptation|"
                   r"adaptation communication", 1.0),
    ("adaptation", r"\badaptation\b", 0.6),
    ("transition-juste", r"just transition", 1.0),
    # « operating entities of the Financial Mechanism » figure souvent dans la liste des INVITÉS : pas un signal.
    ("finance", r"new collective quantified goal|\bncqg\b|article 9|long-term (climate )?finance|"
                r"standing committee on finance|\bscf\b|green climate fund|\bgcf\b|global environment facility|"
                r"\bgef\b|guidance to the (operating entities of the )?financial mechanism|"
                r"review of the financial mechanism|baku to bel[ée]m", 1.0),
    ("finance", r"\bclimate finance\b|\bfinance\b", 0.6),
    ("flux-financiers", r"article 2,? paragraph 1\(c\)|2\.1\s?\(c\)|finance flows", 1.0),
    ("pertes-prejudices", r"loss and damage|warsaw international mechanism|santiago network|\bfrld\b|\bwim\b", 1.0),
    # Article 6 de l'ACCORD DE PARIS (marchés) ; l'article 6 de la CONVENTION, c'est l'ACE (technologie-capacites).
    ("article-6", r"article 6,? paragraphs? [248]|article 6\.[248]|article 6 of the paris agreement|"
                  r"non-market approach|crediting mechanism|\bitmos?\b|supervisory body|"
                  r"clean development mechanism|\bcdm\b|kyoto mechanism|joint implementation", 1.0),
    ("attenuation", r"mitigation work programme|\bmwp\b|mitigation ambition|transition(ing)? away from fossil|"
                    r"international aviation|maritime transport|bunker fuel", 1.0),
    ("attenuation", r"\bmitigation\b", 0.6),
    ("bilan-ndc", r"global stocktake|\bgst\b|uae dialogue|ndc synthesis", 1.0),
    ("bilan-ndc", r"nationally determined contribution|\bndcs?\b", 0.6),
    ("riposte-commerce", r"response measures|unilateral trade|trade and climate|\bkci\b", 1.0),
    ("transparence", r"transparency|\betf\b|biennial (transparency|update) report|\bbtrs?\b|\bbur\b|"
                     r"national communication|facilitative multilateral|methodological issues under the paris|"
                     r"article 13 of the paris agreement", 1.0),
    ("transparence", r"\breporting\b|inventor(y|ies)|compliance", 0.6),
    ("technologie-capacites", r"technology (mechanism|executive|implementation programme|needs|framework|transfer)|"
                              r"\bctcn\b|\btec\b|capacity[- ]building|\bpccb\b|action for climate empowerment|"
                              r"\bace\b|climate education|article 6 of the convention", 1.0),
    ("technologie-capacites", r"\btechnology\b", 0.6),
    ("agriculture", r"agricultur|food security|sharm el-sheikh joint work", 1.0),
    ("genre", r"gender", 1.0),
    ("science-oceans", r"research|systematic observation|\bipcc\b|ocean|earth information day", 1.0),
    ("science-oceans", r"\bscience\b", 0.6),
    ("pma-vulnerables", r"least developed countr|\bleg\b|\bldc expert|small island", 1.0),
    ("organisation", r"arrangements for intergovernmental|programme budget|administrative, financial|"
                     r"rules of procedure|cooperation with other international organi[sz]ations", 1.0),
    ("organisation", r"provisional agenda", 0.6),
    ("couverture", r"cover decision|mutir[aã]o", 1.0),
]


def rule_dossiers(call) -> dict[str, float]:
    """Dossiers d'un appel selon les règles : {slug: score}.
    1. Motifs spécifiques (1.0) sur le point de l'ordre du jour (OBJET), à défaut sur le TITRE : ils suffisent.
    2. Sinon, motifs larges (0.6) sur titre et objet, et tous les motifs sur les THÈMES du portail (génériques,
       souvent multiples), à 0.6 : dernier recours, jamais en plus d'un rattachement spécifique."""
    issue, title = (call["issue"] or "").lower(), (call["title"] or "").lower()
    main = f"{title} {issue}"
    themes = " ".join(db.jload(call["themes"])).lower()
    # Le point de l'ordre du jour (`issue`) est court et précis ; le titre est souvent une longue description qui
    # cite des sujets voisins (« …and available capacity-building… »). D'abord l'un, puis l'autre.
    for text in (issue, title):
        specific = {slug: 1.0 for slug, pattern, score in CALL_RULES if score >= 1.0 and re.search(pattern, text)}
        if specific:
            return specific
    broad: dict[str, float] = {}
    for slug, pattern, score in CALL_RULES:
        if (score < 1.0 and re.search(pattern, main)) or (themes and re.search(pattern, themes)):
            broad[slug] = 0.6
    return broad


def points_dossier(conn, call_id: int) -> tuple[str, float] | None:
    """Dossier majoritaire parmi les points analysés des soumissions de l'appel (sans IA supplémentaire)."""
    counts: dict[str, int] = {}
    for (matters,) in conn.execute(
            """SELECT a.matters FROM sub_analysis a JOIN sub_items i ON i.doc_id=a.doc_id
               WHERE i.call_id=? AND a.error IS NULL GROUP BY a.doc_id""", (call_id,)):
        for m in db.jload(matters):
            if m.get("dossier"):
                counts[m["dossier"]] = counts.get(m["dossier"], 0) + 1
    total = sum(counts.values())
    if total < POINTS_MIN:
        return None
    slug, n = max(counts.items(), key=lambda kv: kv[1])
    return (slug, round(n / total, 2)) if n / total >= POINTS_SHARE else None


def map_calls(conn) -> dict[str, int]:
    """(Re)calcule les rattachements automatiques ; les rattachements manuels sont conservés tels quels."""
    ensure_schema(conn)
    manual = {r[0] for r in conn.execute("SELECT DISTINCT call_id FROM sub_call_dossiers WHERE methode='manuel'")}
    conn.execute("DELETE FROM sub_call_dossiers WHERE methode<>'manuel'")
    now = live.now_iso()
    out = {"regle": 0, "points": 0, "manuel": len(manual), "sans": 0}
    for c in conn.execute("SELECT * FROM sub_calls").fetchall():
        if c["id"] in manual:
            continue
        found = rule_dossiers(c)
        if found:
            conn.executemany("INSERT INTO sub_call_dossiers(call_id,dossier,methode,score,at) VALUES(?,?,?,?,?)",
                             [(c["id"], slug, "regle", sc, now) for slug, sc in found.items()])
            out["regle"] += 1
            continue
        p = points_dossier(conn, c["id"])
        if p:
            conn.execute("INSERT INTO sub_call_dossiers(call_id,dossier,methode,score,at) VALUES(?,?,'points',?,?)",
                         (c["id"], p[0], p[1], now))
            out["points"] += 1
        else:
            out["sans"] += 1
    conn.commit()
    return out


NO_DOSSIER = "aucun"  # rattachement manuel « cet appel ne relève d'aucun dossier » (bloque les règles)


def set_manual(conn, call_id: int, dossiers: list[str]) -> list[str]:
    """Rattachement à la main (remplace tout rattachement de l'appel). Liste vide : ligne NO_DOSSIER, qui empêche
    les règles de le rattacher et n'apparaît dans aucun dossier."""
    ensure_schema(conn)
    slugs = [live.dossier_from_alias(d) or d for d in dossiers]
    unknown = [d for d in slugs if not live.dossier(d)]
    if unknown:
        raise ValueError(f"dossier(s) inconnu(s) : {', '.join(unknown)}")
    conn.execute("DELETE FROM sub_call_dossiers WHERE call_id=?", (call_id,))
    conn.executemany("INSERT INTO sub_call_dossiers(call_id,dossier,methode,score,at) VALUES(?,?,'manuel',1,?)",
                     [(call_id, d, live.now_iso()) for d in slugs or [NO_DOSSIER]])
    conn.commit()
    return slugs


def dossier_call_ids(conn, slug: str) -> list[int]:
    ensure_schema(conn)
    return [r[0] for r in conn.execute("SELECT call_id FROM sub_call_dossiers WHERE dossier=?", (slug,))]


def _alignment(conn, call_id: int) -> list[dict[str, Any]]:
    """Comparaisons DÉJÀ en cache entre soumissions de l'appel (aucune nouvelle n'est lancée)."""
    docs = {r[0] for r in conn.execute("SELECT doc_id FROM sub_items WHERE call_id=? AND doc_id IS NOT NULL", (call_id,))}
    out, seen = [], set()
    for r in conn.execute("SELECT a_doc, b_doc, verdict, counts, key FROM sub_comparisons ORDER BY at DESC"):
        if (r["a_doc"], r["b_doc"]) in seen:
            continue  # plus récente seulement (une ré-analyse laisse d'anciennes comparaisons en cache)
        if r["a_doc"] in docs and r["b_doc"] in docs and r["key"].split("|")[2] == PROMPT_VERSION:
            seen.add((r["a_doc"], r["b_doc"]))
            out.append({"a": _who(conn, r["a_doc"]), "b": _who(conn, r["b_doc"]), "indication": r["verdict"],
                        "indication_libelle": VERDICTS[r["verdict"]][0], "indication_en": VERDICTS[r["verdict"]][1],
                        "regle": json.loads(r["counts"]).get("regle")})
    return out


def dossier_section(conn, slug: str, *, limit: int = 10, recent_days: int = 365) -> dict[str, Any]:
    """Rubrique « soumissions » d'un dossier : appels liés (compacts), auteurs pour les appels récents ou de la session,
    alignements déjà calculés."""
    ensure_schema(conn)
    rows = conn.execute(
        f"""SELECT c.*, m.methode, m.score, COUNT(i.id) n, SUM(i.kind='party') np, MAX(i.submitted) last
            FROM sub_call_dossiers m JOIN sub_calls c ON c.id=m.call_id LEFT JOIN sub_items i ON i.call_id=c.id
            WHERE m.dossier=? GROUP BY c.id
            ORDER BY c.upcoming DESC, COALESCE(MAX(i.submitted), c.modified) DESC LIMIT ?""", (slug, limit)).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM sub_call_dossiers WHERE dossier=?", (slug,)).fetchone()[0]
    cutoff = (datetime.now(timezone.utc).date().toordinal() - recent_days)
    calls = []
    for r in rows:
        item = {**_call_public(r), "nb_soumissions": r["n"], "nb_parties": r["np"] or 0, "derniere": r["last"],
                "rattachement": r["methode"]}
        recent = bool(r["upcoming"]) or bool(r["last"] and datetime.strptime(r["last"], "%Y-%m-%d").date().toordinal() >= cutoff)
        if recent:
            authors: dict[str, None] = {}
            for it in conn.execute("SELECT * FROM sub_items WHERE call_id=? AND kind='party' ORDER BY submitted DESC",
                                   (r["id"],)):
                authors[speaks_for(it)] = None  # nom canonique ; le groupe quand la Partie soumet en son nom
            item["auteurs_parties"] = list(authors)
            item["observateurs"] = conn.execute("SELECT COUNT(*) FROM sub_items WHERE call_id=? AND kind<>'party'",
                                                (r["id"],)).fetchone()[0]
            al = _alignment(conn, r["id"])
            if al:
                item["alignements_connus"] = al[:12]
        calls.append({k: v for k, v in item.items() if v not in (None, [], "")})
    return {"appels": calls, "nb_appels_lies": total,
            "note": "Appels rattachés au dossier par règles (titre, thèmes) ou par les points analysés de leurs "
                    "soumissions ; auteurs listés pour les appels de la session ou des 12 derniers mois ; "
                    "alignements : seulement les comparaisons déjà calculées (comparer_soumissions pour en lancer)."}


def alignment_matrix(conn, slug: str, actors: list[str]) -> dict[str, Any]:
    """Matrice d'alignement LUE DANS LE CACHE : pour chaque paire d'acteurs, la comparaison la plus récente sur un
    appel du dossier, ou « à calculer » (aucun appel IA ici)."""
    from .positions import normalize_actor

    ensure_schema(conn)
    calls = set(dossier_call_ids(conn, slug))
    names = list(dict.fromkeys(normalize_actor(a) for a in actors))
    docs: dict[str, list[tuple[str, int, int]]] = {}  # acteur → [(date, call, doc)]
    for a in names:
        docs[a] = [(r["submitted"] or "", r["call_id"], r["doc_id"]) for r in conn.execute(
            """SELECT DISTINCT i.submitted, i.call_id, i.doc_id FROM sub_actors x JOIN sub_items i ON i.id=x.sub_id
               WHERE x.actor=? AND i.doc_id IS NOT NULL""", (a,)) if r["call_id"] in calls]
    pairs, missing, no_common = [], [], []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            common = sorted({c for _, c, _ in docs[a]} & {c for _, c, _ in docs[b]}, reverse=True)
            if not common:
                if docs[a] and docs[b]:
                    no_common.append({"a": a, "b": b})
                continue
            found = None
            for call in common:
                da = max(x for x in docs[a] if x[1] == call)[2]
                db_ = max(x for x in docs[b] if x[1] == call)[2]
                lo, hi = sorted((da, db_))
                row = conn.execute("SELECT verdict, counts FROM sub_comparisons WHERE a_doc=? AND b_doc=? AND key LIKE ? "
                                   "ORDER BY at DESC LIMIT 1", (lo, hi, f"{lo}|{hi}|{PROMPT_VERSION}|%")).fetchone()
                if row:
                    found = {"a": a, "b": b, "appel": call, "indication": row["verdict"],
                             "indication_libelle": VERDICTS[row["verdict"]][0], "regle": json.loads(row["counts"]).get("regle")}
                    break
            if found:
                pairs.append(found)
            else:
                missing.append({"a": a, "b": b, "appel": common[0]})
    return {"dossier": slug, "acteurs": names, "comparees": pairs, "a_calculer": missing,
            "sans_appel_commun": no_common or None,
            "absents_du_dossier": [a for a in names if not docs[a]] or None,
            "note": "Lecture du cache seulement ; lancer comparer_soumissions(a, b, dossier=…) pour une paire « à calculer »."}


# --- groupes de négociation : position d'un groupe à partir des soumissions de ses membres ----------------------------

def group_member_items(conn, gid: str, call_ids: set[int] | None = None) -> tuple[list, int]:
    """Soumissions de Parties MEMBRES d'un groupe (liste sourcée, voir groupes.py), et nombre de membres listés."""
    from . import groupes

    isos = groupes.member_isos(conn, gid)
    if not isos:
        return [], 0
    rows = conn.execute(
        f"""SELECT * FROM sub_items WHERE kind='party' AND doc_id IS NOT NULL
            AND iso3 IN ({','.join('?' * len(isos))}) ORDER BY submitted DESC""", sorted(isos)).fetchall()
    return [r for r in rows if call_ids is None or r["call_id"] in call_ids], len(isos)


def _side(conn, ref: str, call_ids: set[int] | None, force_members: bool = False) -> dict[str, Any]:
    """Un côté d'une comparaison : soumission(s) propres de l'acteur, sinon soumissions des membres du groupe."""
    from . import groupes

    if str(ref).strip().isdigit():
        it = item_for_doc(conn, int(ref))
        if not it:
            raise ValueError(f"le document {ref} n'est pas une soumission CCNUCC connue")
        return {"acteur": _who(conn, int(ref)), "mode": "propre", "items": [it]}
    try:
        items = [] if force_members else [x for x in _actor_items(conn, ref, indexed_only=True)
                                          if call_ids is None or x["call_id"] in call_ids]
        if items:
            return {"acteur": speaks_for(items[0]), "mode": "propre", "items": items}
    except UnknownActor:
        pass
    gid = groupes.group_id(ref)
    if not gid:
        raise UnknownActor(f"aucune soumission pour « {ref} »" + (" dans ce périmètre" if call_ids else ""))
    items, n = group_member_items(conn, gid, call_ids)
    if not n:
        raise UnknownActor(f"« {gid} » n'a pas soumis en son nom dans ce périmètre et sa liste de membres n'est pas "
                           "vérifiée (voir groupes) : impossible de reconstituer sa position")
    if not items:
        raise UnknownActor(f"ni « {gid} » ni aucun de ses {n} membres listés n'a soumis dans ce périmètre")
    return {"acteur": gid, "mode": "membres", "items": items, "membres_listes": n}


def compare_actors(conn, a: str, b: str, *, appel: int | None = None, dossier: str | None = None,
                   max_new: int = MAX_NEW_COMPARISONS, max_total: int = 12) -> dict[str, Any]:
    """Comparaison qui accepte des groupes sans soumission propre : on compare alors avec les soumissions de leurs
    MEMBRES sur l'appel commun le plus récent (présentées comme telles, avec la couverture), en cache d'abord."""
    call_ids: set[int] | None = None
    if appel:
        call_ids = {appel}
    elif dossier:
        call_ids = set(dossier_call_ids(conn, live.dossier_from_alias(dossier) or dossier))
    from . import groupes

    sa, sb = _side(conn, a, call_ids), _side(conn, b, call_ids)
    note = None

    def calls(side):
        return {x["call_id"] for x in side["items"]}

    if not calls(sa) & calls(sb):
        # Un groupe a soumis en son nom, mais pas sur un appel commun : repli sur les soumissions de ses membres.
        for ref, side_name in ((b, "sb"), (a, "sa")):
            side = sb if side_name == "sb" else sa
            if side["mode"] == "propre" and groupes.group_id(ref):
                try:
                    alt = _side(conn, ref, call_ids, force_members=True)
                except UnknownActor:
                    continue
                other = sa if side_name == "sb" else sb
                if calls(alt) & calls(other):
                    if side_name == "sb":
                        sb = alt
                    else:
                        sa = alt
                    note = (f"« {alt['acteur']} » n'a pas soumis en son nom sur un appel commun : comparaison avec les "
                            "soumissions de ses membres.")
                    break
    common = sorted({x["call_id"] for x in sa["items"]} & {x["call_id"] for x in sb["items"]},
                    key=lambda c: max(x["submitted"] or "" for x in sa["items"] + sb["items"] if x["call_id"] == c),
                    reverse=True)
    if not common:
        raise UnknownActor(f"pas d'appel auquel « {sa['acteur']} » et « {sb['acteur']} » ont tous deux répondu"
                           + (" dans ce périmètre" if call_ids else ""))
    call = common[0]

    def docs(side):
        ds = list(dict.fromkeys(x["doc_id"] for x in side["items"] if x["call_id"] == call))
        return ds[:1] if side["mode"] == "propre" else ds

    pairs = [(x, y) for x in docs(sa) for y in docs(sb) if x != y]
    pairs.sort(key=lambda p: not _cached_pair(conn, *p))
    results, skipped, new = [], [], 0
    for x, y in pairs:
        cached = _cached_pair(conn, x, y)
        if len(results) >= max_total or (not cached and new >= max_new):
            skipped.append(f"{_who(conn, x)} / {_who(conn, y)}")
            continue
        new += int(not cached)
        c = compare(conn, x, y)
        results.append({"a": c["a"], "b": c["b"], "indication": c["indication"],
                        "indication_libelle": c["indication_libelle"], "regle": c["decompte"].get("regle"),
                        "conflits": [p["point"] for p in c["points"] if p["relation"] == "conflit"][:5]})
    counts: dict[str, int] = {}
    for r in results:
        counts[r["indication_libelle"]] = counts.get(r["indication_libelle"], 0) + 1
    row = conn.execute("SELECT * FROM sub_calls WHERE id=?", (call,)).fetchone()

    def side_out(side):
        o = {"acteur": side["acteur"], "mode": side["mode"]}
        if side["mode"] == "membres":
            members = sorted({x["entity"] for x in side["items"] if x["call_id"] == call})
            o.update({"membres_ayant_soumis": members,
                      "couverture": f"{len(members)} membre(s) sur {side['membres_listes']} listés"})
        return o

    out = {"appel": _call_public(row) if row else call, "a": side_out(sa), "b": side_out(sb), "repli": note,
           "comparaisons": results, "repartition": counts, "non_comparees": skipped or None,
           "suite": "Relancer pour comparer les suivantes (cache)." if skipped else None,
           "methode": "Chaque comparaison : comparer_soumissions(doc_a, doc_b) pour le détail point par point."}
    if "membres" in (sa["mode"], sb["mode"]):
        out["avertissement"] = ("Soumissions de MEMBRES du groupe, pas une position de groupe : le groupe n'a pas soumis "
                                "en son nom sur cet appel. Ne pas présenter la répartition comme l'avis du groupe.")
    return {k: v for k, v in out.items() if v is not None}
