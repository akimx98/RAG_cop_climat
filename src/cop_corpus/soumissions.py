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
    m = re.search(r"\bon behalf of\s+(?:the\s+)?(.+)$", d, re.I)
    if m:
        d = m.group(1)
    elif d.lower().startswith(ent):
        return []  # titre du document, pas des co-auteurs
    d = re.sub(r"^(this submission (was|is) )?(jointly )?(prepared|submitted) by\s+", "", d, flags=re.I)
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
    from .positions import normalize_actor

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
               ouverts: bool = False, limite: int = 40) -> list[dict[str, Any]]:
    ensure_schema(conn)
    where, params = ["1=1"], []
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


def resolve_pair(conn, a: str, b: str, appel: int | None = None) -> tuple[int, int, int | None]:
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
    calls_b = {x["call_id"]: x for x in db_}
    for x in da:  # du plus récent au plus ancien
        if x["call_id"] in calls_b:
            return x["doc_id"], calls_b[x["call_id"]]["doc_id"], x["call_id"]
    return da[0]["doc_id"], db_[0]["doc_id"], None


def latest_doc(conn, ref: str, appel: int | None = None) -> int:
    if str(ref).strip().isdigit():
        return int(ref)
    return _actor_items(conn, ref, appel, indexed_only=True)[0]["doc_id"]
