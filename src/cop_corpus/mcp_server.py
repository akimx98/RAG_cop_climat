"""Serveur MCP du corpus COP31.

Le serveur est volontairement « bête et traçable » : il renvoie des passages
exacts, leurs références et leur niveau de fiabilité. Le raisonnement
(vérification d'une affirmation, brief) est fait par le client, guidé par les
instructions ci-dessous et par les skills du plugin.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import yaml
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import alerts, auth, config, data, db, diff, ingest, search

# Règles : une seule source, clients/rules.md, recopiée dans le skill cop31-protocol et les consignes à coller par
# scripts/sync_clients.py (un test vérifie que les copies sont à jour). Version des copies : plugin.json.
CLIENTS = config.ROOT / "clients"
RULES = (CLIENTS / "rules.md").read_text(encoding="utf-8").strip()
CLIENT_VERSION = json.loads((CLIENTS / "claude-plugin" / ".claude-plugin" / "plugin.json").read_text())["version"]

INSTRUCTIONS = f"""Verifiable corpus to follow COP31 (Antalya, 9–20 Nov 2026). Independent, free, open-source tool, not
affiliated with the UNFCCC or any delegation.

RULES
{RULES}

TOOLS
- Corpus: rechercher → lire_passage; document; comparer_versions; crochets; historique_formulation; nouveautes;
  donnees; glossaire.
- Negotiations: dossiers (aliases) → dossier; positions (divisions=true: dividing lines).
- Written positions: soumissions; comparer_soumissions; groupes.
- Plan the day: programme (where, when; mes_sujets=true); pavillons (contents).
- My topics: mes_sujets; nouveautes(mes_sujets=true) = what changed on them; suivre; deposer_url; etat_corpus.
Methods (MCP prompts): point_negociation, position_pays, preparer_rencontre, plaidoyer, decouvrir_sujet,
quoi_de_neuf.
COP31 skills or pasted instructions here older than v{CLIENT_VERSION} or unversioned: tell the user once to update
them at {config.PUBLIC_BASE}/connect."""

# Outils retirés ou renommés : ne doivent plus apparaître nulle part (test). textes_en_cours → dossiers(textes=…) ;
# suivre_dossier / arreter_suivi → suivre(dossier=… | arreter=…) ; analyser_soumission → soumissions(document=…) ;
# engagements / deposer : noms qui n'ont jamais existé (ancienne documentation) ; noter : notes de terrain retirées.
RETIRED_TOOLS = ("textes_en_cours", "suivre_dossier", "arreter_suivi", "analyser_soumission", "engagements", "deposer",
                 "noter")

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)


def _security() -> TransportSecuritySettings:
    hosts = config.MCP_ALLOWED_HOSTS
    if not hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                     allowed_origins=[f"https://{h}" for h in hosts])


mcp = FastMCP("cop31", instructions=INSTRUCTIONS, stateless_http=True, json_response=True,
              transport_security=_security())

LOCAL_ADMIN = {"id": 0, "email": "local", "name": "local", "is_admin": 1, "can_restricted": 1, "active": 1}


def _user(ctx: Context | None) -> dict | None:
    req = None
    try:
        req = ctx.request_context.request if ctx else None
    except Exception:
        req = None
    if req is not None:
        user = req.scope.get("cop_user")
        if user:
            return user
        try:
            from mcp.server.auth.middleware.auth_context import get_access_token

            tok = get_access_token()
            if tok and tok.claims and tok.claims.get("email"):
                return auth.by_email(db.get(), tok.claims["email"])
        except Exception:
            pass
        return None
    # stdio (usage local) : administrateur local.
    return LOCAL_ADMIN if config.LOCAL_ADMIN else None


def _need_user(ctx) -> dict:
    user = _user(ctx)
    if not user:
        raise ValueError("Unidentified user: use your personal connector address (/u/<token>/mcp).")
    return user


def _since(value: str | None) -> str | None:
    if not value:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*(h|j|d|min)\s*", value.lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(hours=n) if unit == "h" else timedelta(minutes=n) if unit == "min" else timedelta(days=n)
        return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    return value.strip()


def _resolve_dossier(word: str | None) -> tuple[str | None, dict[str, Any] | None]:
    """(slug, None) si le mot désigne un seul dossier ; sinon (None, réponse d'erreur avec les candidats)."""
    from . import live

    if not word:
        return None, None
    slug, cands = live.resolve_dossier(word)
    return (slug, None) if slug else (None, live.ambiguity(word, cands))


@mcp.tool(annotations=READ)
def rechercher(
    requete: str,
    niveaux: list[str] | None = None,
    types: list[str] | None = None,
    session: str | None = None,
    organisation: str | None = None,
    partie: str | None = None,
    groupe: str | None = None,
    depuis: str | None = None,
    jusqu_a: str | None = None,
    langue: str | None = None,
    acces: str | None = None,
    limite: int = 10,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Hybrid search (keywords + meaning, multilingual) across the whole corpus: UN law (Convention, Paris
    Agreement, decisions), session documents and draft texts, NDCs, EU law and positions, science, daily reports,
    data; internal documents are reserved for contributors (guests: no access).
    Phrase UN terms in English ("global stocktake", "transitioning away from fossil fuels"), quotes for an exact
    phrase, 2–3 phrasings if the first result is thin. Official submissions: excluded by default (historical stock),
    see types/partie/groupe or the `soumissions` tool.

    - requete: question or keywords; quotes for an exact phrase; OR allowed.
    - niveaux: e.g. ["1","2"] to keep only law and official documents; "R" is not a filter: use
      acces="restreint" for internal documents.
    - types: traite, decision, conclusions_sb, ordre_du_jour, projet_texte, ndc, btr, rapport_synthese,
      loi_ue, proposition_ue, conclusions_conseil, strategie_nationale, rapport_scientifique, donnees,
      compte_rendu, communique, analyse, programme, soumission (official UNFCCC submission),
      declaration (plenary statement posted on the portal).
    - session: COP29, CMA6, COP31…; organisation: UNFCCC, Council of the EU…; partie: EU, FRA, CHN…
    - groupe: LDC, AOSIS, G77, EU, Umbrella…: documents (NDCs, submissions) of its members (sourced list, see groupes)
    - depuis / jusqu_a: ISO date (YYYY-MM-DD) on the publication (or retrieval) date.
    Returns passages with level, symbol, paragraph, page, excerpt and links. Use lire_passage for the full text.
    Relevance: `resultats` = phrase, all terms or close in meaning; `pertinence_partielle` = most terms
    (`termes_manquants` listed: read them before saying "not found"); `faible_pertinence` = one term among
    several, far in meaning. `verdict` when nothing is fully relevant: follow it as is. Nothing found = "not found
    in the corpus", never an answer from memory.
    To answer: quote the exact text with its reference and link, give its level, and for a draft its version, date
    and "not adopted".
    """
    user = _user(ctx)
    filters = {"levels": niveaux, "doc_types": types, "session": session, "org": organisation, "party": partie,
               "group": groupe,
               "since": depuis, "until": jusqu_a, "lang": langue, "access": acces}
    signals: dict[int, dict[str, Any]] = {}
    hits = search.search(db.get(), requete, {k: v for k, v in filters.items() if v}, user, limit=min(limite, 30),
                         signals=signals)
    n = search.n_terms(requete)
    level = {h["passage_id"]: search.relevance(signals.get(h["passage_id"]), n) for h in hits}
    strong = [h for h in hits if level[h["passage_id"]] == "forte"]
    partial = [h for h in hits if level[h["passage_id"]] == "partielle"]
    weak = [h for h in hits if level[h["passage_id"]] == "faible"]
    out: dict[str, Any] = {"requete": requete, "nb": len(strong), "resultats": strong}
    if partial:
        out["pertinence_partielle"] = [{**h, "termes_manquants": signals[h["passage_id"]].get("termes_manquants")}
                                       for h in partial[:8]]
    if weak:
        out["faible_pertinence"] = [{**h, "pertinence": signals.get(h["passage_id"])} for h in weak[:5]]
    if not strong and partial:
        missing = sorted({t for h in partial for t in signals[h["passage_id"]].get("termes_manquants") or []})
        out["verdict"] = (f"partial relevance: passages contain most terms but not {', '.join(missing)}. Read them "
                          "(lire_passage) before concluding: if they answer the question, cite them and say which "
                          "term is absent; do not answer \"not found\" without reading them.")
    elif not strong:
        out["verdict"] = ("nothing relevant: no passage contains the phrase, all or most of the terms, or is "
                          "close in meaning. Say so (\"not found in the corpus\"); try 2–3 other phrasings or remove "
                          "filters; do not present `faible_pertinence` as an answer.")
    out["rappel"] = "Quote the exact text, the reference and the level. Nothing relevant → say so."
    return out


@mcp.tool(annotations=READ)
def lire_passage(passage_id: int, avant: int = 1, apres: int = 2, ctx: Context | None = None) -> dict[str, Any]:
    """Exact text of a passage and its neighbours (context), with the full reference and a link to the original at
    the right page. Read it before quoting: quote it exactly (original language), with reference, link and level;
    a draft always with its version, date and "not adopted"."""
    out = search.passage(db.get(), passage_id, _user(ctx), before=min(avant, 5), after=min(apres, 8))
    return out or {"erreur": "passage not found or not authorised"}


@mcp.tool(annotations=READ)
def document(reference: str, ctx: Context | None = None) -> dict[str, Any]:
    """Document metadata and list of its versions. `reference` = numeric id, official symbol
    (e.g. FCCC/PA/CMA/2023/16/Add.1, FCCC/CP/2026/L.3) or URL. A draft is always cited with its version, date and
    "not adopted"."""
    conn = db.get()
    user = _user(ctx)
    row = search.find_doc(conn, reference, user)
    if not row:
        return {"erreur": f"document not found: {reference}"}
    out = search.document(conn, row["id"], user) or {}
    first = conn.execute("SELECT id, page, para, substr(text,1,400) AS t FROM passages WHERE doc_id=? ORDER BY seq LIMIT 3",
                         (row["id"],)).fetchall()
    out["debut"] = [{"passage_id": r["id"], "page": r["page"], "paragraphe": r["para"], "texte": r["t"]} for r in first]
    return out


COMPARE_BUDGET = 40000   # caractères par page pour un client MCP ; /ask passe le sien (limite de 14 000 par appel)


def _scope(ctx: Context | None, key: str) -> Any:
    try:
        return ctx.request_context.request.scope.get(key) if ctx else None
    except Exception:
        return None


@mcp.tool(annotations=READ)
def comparer_versions(document_recent: str, document_ancien: str | None = None, page: int = 1,
                      filtre: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """What changed between two versions of a draft text, unit by unit (paragraph, option, bullet, sub-paragraph:
    ids like §4.opt2.b3 = 3rd bullet of Option 2 of §4). Without `document_ancien`, compares with the previous
    version in the same lineage.
    Returns: `resume` (headline lines: options settled, bracket outcomes, units deleted with no trace, verb and
    qualifier shifts), `options` (which option was retained, in full or in part), `changements` with a status per
    unit — modifié (full word diff), repris_de (split/rewritten, with its source), repris_dans (merged),
    ajouté, supprimé_sans_reprise (nothing of it survives: usually the most telling) — each with the fate of its
    brackets (levé = lifted, text kept; supprimé; maintenu; alternatives [A][B]: which one won), footnote changes,
    and citable references for BOTH versions (passage_id, page, lien_source = PDF at the page, lien_passage).
    Texts are never cut: if `tronque` is true, call again with page=N. `filtre`: a unit prefix ("§4") or words
    ("finance, grant") to keep only matching changes. Quote the diff's text, give versions, dates and "not adopted".
    Give the user the link `diff_en_ligne` (whole comparison) or a unit's `lien_diff`: that page shows the deleted
    and added wording struck through and highlighted, which says more than a summary."""
    conn = db.get()
    user = _user(ctx)
    new = search.find_doc(conn, document_recent, user)
    if not new:
        return {"erreur": f"document not found: {document_recent}"}
    if document_ancien:
        old = search.find_doc(conn, document_ancien, user)
    else:
        prev = diff.previous_in_lineage(conn, new["id"])
        old = conn.execute("SELECT * FROM documents WHERE id=?", (prev,)).fetchone() if prev else None
        if old and old["access"] != "public" and not auth.can_restricted(user):
            old = None
    if not old:
        return {"erreur": "no known earlier version",
                "versions": search.versions(conn, new["lineage_key"], user) if new["lineage_key"] else [],
                "conseil": "versions of a session text are linked by item and title (the symbol changes between "
                           "bodies); pass document_ancien=<symbol or doc_id> to compare any two texts"}
    head = {"ancien": search.doc_public(old), "nouveau": search.doc_public(new)}
    from . import live as lv

    for side, row in (("ancien", old), ("nouveau", new)):
        hint = lv.compilation_hint(conn, row["id"])
        if hint:
            head[side]["compilation"] = hint
    if new["lineage_key"]:
        vs = search.versions(conn, new["lineage_key"], user)
        if len(vs) > 2:
            head["autres_versions"] = [{k: v.get(k) for k in ("doc_id", "version", "cote", "publie_le")} for v in vs[-8:]]
    head["diff_en_ligne"] = f"{config.PUBLIC_BASE}/diff/{new['id']}?avec={old['id']}"
    budget = (_scope(ctx, "cop_budget") or COMPARE_BUDGET) - len(json.dumps(head, ensure_ascii=False))
    out = diff.compare(conn, old["id"], new["id"], budget=budget, page=page, filtre=filtre,
                       compact=bool(_scope(ctx, "cop_compact")))
    return {**head, **out}


@mcp.tool(annotations=READ)
def historique_formulation(notion: str, texte: str | None = None, dossier: str | None = None,
                           ctx: Context | None = None) -> dict[str, Any]:
    """Track a wording or notion through every version of a draft text: is "1.5 °C", "transition away from fossil
    fuels", "grant-based", "unilateral measures"… present without brackets, in brackets, inside an option, or
    absent — version by version, where (unit such as §4.opt2.b3, page, link, excerpt), and when it switched
    (`bascules`: appears, disappears, brackets lifted, put in brackets, option retained…).
    `notion`: 1.5, fossil fuels, coal, renewables, energy efficiency, methane, deforestation, net zero, CBDR,
    finance, grants, unilateral measures, human rights, gender, just transition, loss and damage, science (IPCC)
    — or English words separated by commas, searched as written. `texte`: symbol, doc_id or URL; otherwise
    `dossier` (slug or alias): its latest retrieved text. "Absent" means this wording is absent, not that the
    topic is: try other words. Quote the excerpt with version, date, page and link; drafts are not adopted; give
    the `lien_diff` of a switch so the user sees the struck-through wording."""
    from . import formulations

    conn = db.get()
    user = _user(ctx)
    if texte:
        row = search.find_doc(conn, texte, user)
        if not row:
            return {"erreur": f"document not found: {texte}"}
    elif dossier:
        slug, err = _resolve_dossier(dossier)
        if err:
            return err
        live, _, _ = _live()
        got = [t for t in live.texts(conn, slug, limit=40) if t["doc_id"]]
        row = search.find_doc(conn, str(got[0]["doc_id"]), user) if got else None
        if not row:
            return {"erreur": f"no text of item “{slug}” retrieved in the corpus", "conseil": "dossiers(a_recuperer=true)"}
    else:
        return {"erreur": "specify texte=<symbol or doc_id> or dossier=<slug or alias>"}
    return formulations.timeline(conn, row, notion, user)


@mcp.tool(annotations=READ)
def crochets(theme: str, texte: str | None = None, dossier: str | None = None, limite: int = 40,
             ctx: Context | None = None) -> dict[str, Any]:
    """Brackets by theme in a draft text: which wording on a theme is still IN BRACKETS (not agreed), and since
    which version. `theme`: gender, human rights (rights defenders), disinformation, indigenous peoples, youth,
    just transition, fossil fuels, finance, unilateral measures (English or French names) — or English words
    separated by commas ("civic space, human rights defenders"). `texte`: symbol (FCCC/…/L.x), doc_id or URL;
    otherwise `dossier` (slug or alias): latest retrieved text of the item (the others are listed). For each
    bracket: paragraph, wording, context, `entre_crochets_depuis` (version, date), version-by-version history.
    Always state the version, the date and "not adopted"."""
    from . import crochets as cr

    conn = db.get()
    user = _user(ctx)
    others = None
    if texte:
        row = search.find_doc(conn, texte, user)
        if not row:
            return {"erreur": f"document not found: {texte}"}
    elif dossier:
        slug, err = _resolve_dossier(dossier)
        if err:
            return err
        live, _, _ = _live()
        got = [t for t in live.texts(conn, slug, limit=40) if t["doc_id"]]
        if not got:
            return {"erreur": f"no text of item “{slug}” retrieved in the corpus", "conseil": "dossiers(a_recuperer=true)"}
        row = search.find_doc(conn, str(got[0]["doc_id"]), user)
        if not row:
            return {"erreur": "text not accessible"}
        others = [{"cote": t["symbol"], "titre": t["title"], "date": live.local_time(t["published"]), "doc_id": t["doc_id"]}
                  for t in got[1:8]] or None
    else:
        return {"erreur": "specify texte=<symbol or doc_id> or dossier=<slug or alias>"}
    out = cr.by_theme(conn, row, theme, user, limit=max(1, min(limite, 100)))
    if others:
        out["autres_textes_du_dossier"] = others
        out["choix_texte"] = "most recent text of the item used; specify texte=<symbol> for another"
    return out


@mcp.tool(annotations=READ)
def nouveautes(depuis: str = "24h", niveaux: list[str] | None = None, types: list[str] | None = None,
               limite: int = 30, mes_sujets: bool = False, ctx: Context | None = None) -> dict[str, Any]:
    """What entered the corpus recently. `depuis`: "6h", "2j" (days), "30min" or an ISO date.
    `mes_sujets=true`: what changed since `depuis` on the user's topics (followed negotiation items and keyword
    topics), one block per topic: new session texts with their stage (draft, forwarded for adoption = L document,
    adopted), brackets and what changed since the previous version (link to the diff; use comparer_versions for the
    detail), recent reports and newsletters that mention the item, documents flagged by keyword topics. Answers
    "how have the drafts on my topics changed since yesterday?". Otherwise: latest documents, filterable by levels
    and types (e.g. types=["projet_texte","decision"])."""
    since = _since(depuis)
    if mes_sujets:
        from . import i18n, topics

        user = _need_user(ctx)
        out = topics.whats_new(db.get(), user, since, i18n.default_for(user))
        if out["aucun_sujet"]:
            out["conseil"] = "no topics yet: suggest suivre(dossier=…) or suivre(mots_cles=…), or the My topics page"
        return out
    filters = {"added_since": since, "levels": niveaux, "doc_types": types}
    docs = search.latest(db.get(), _user(ctx), {k: v for k, v in filters.items() if v}, limit=min(limite, 100))
    return {"depuis": filters["added_since"], "nb": len(docs), "documents": docs}


@mcp.tool(annotations=READ)
def donnees(pays: str, indicateur: str | None = None, debut: int | None = None, fin: int | None = None) -> dict[str, Any]:
    """Emissions figures to check a claim. `pays`: ISO3 code (FRA, CHN, USA, IND…) or English name
    ("European Union (27)", "World"). Indicators: co2, co2_including_luc, co2_per_capita,
    share_global_co2, cumulative_co2, share_global_cumulative_co2, total_ghg, total_ghg_excluding_lucf,
    ghg_per_capita, coal_co2, oil_co2, gas_co2, methane, population, gdp. No indicator: usual selection.
    No bounds (debut/fin): 1990, 2005, 2015 and the latest years. Source: Our World in Data (level 3)."""
    return data.query(db.get(), pays, indicateur, debut, fin)


_GLOSSARY: list[dict[str, Any]] | None = None


@mcp.tool(annotations=READ)
def glossaire(terme: str | None = None) -> dict[str, Any]:
    """Definition of a climate-negotiation acronym or concept (NCQG, GGA, MWP, Article 6, BTR…),
    with the reference text. No term: list of entries."""
    global _GLOSSARY
    if _GLOSSARY is None:
        path = config.CATALOG_DIR / "glossaire.yaml"
        _GLOSSARY = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else []
    entries = _GLOSSARY or []
    if not terme:
        return {"entrees": [e["terme"] for e in entries]}
    t = terme.lower().strip()
    found = [e for e in entries if t == e["terme"].lower() or t in [str(a).lower() for a in e.get("alias", [])]]
    if not found:
        found = [e for e in entries if t in e["terme"].lower() or t in e.get("definition", "").lower()][:5]
    return {"resultats": found} if found else {"erreur": f"not in the glossary: {terme}", "conseil": "use rechercher"}


@mcp.tool(annotations=READ)
def programme(sujet: str | None = None, jour: str | None = None, types: list[str] | None = None,
              limite: int = 40, mes_sujets: bool = False, pavillons: bool = True,
              ctx: Context | None = None) -> dict[str, Any]:
    """COP31 programme: WHERE and WHEN to go (on a given day, on given topics). To search event content, count
    ("how many pavilions talked about…") or see coverage: `pavillons` tool. Official side events (room, time,
    organisers, themes), official UNFCCC calendar meetings (dialogues and workshops mandated by decisions, group
    coordinations, special events), summary of the day's negotiations, programme documents posted (Daily
    Programme, agendas) AND pavilion and hub events (Blue Zone and Green Zone, ≈5,000 sessions with summary and
    speakers: key `pavillons`).
    `jour`: "today", "tomorrow" (or "aujourd'hui", "demain"), "2026-11-14" or "14". `sujet`: comma-separated
    keywords, preferably in English ("indigenous, LCIPP", "water", "youth, ACE, education").
    `mes_sujets=true`: uses the user's followed topics (alerts) and followed items, one block per topic — for
    "today, which events for my topics?". `pavillons=false`: without pavilions.
    `types`: side_event, reunion, hors_site (off-site events); default side_event + reunion.
    Pavilions: match "mots" (term present) or "sens" (semantic proximity: check the summary).
    Each side event carries its verification link (SEORS, filtered on the day): rooms and times change, always
    give that link. For cross-cutting questions (counts, per pavilion): `pavillons` tool."""
    from . import agenda
    from . import pavillons as pav

    conn = db.get()
    user = _user(ctx)
    lim = max(1, min(limite, 100))
    if mes_sujets:
        if not user or not user.get("id"):
            return {"erreur": "mes_sujets requires being signed in; otherwise specify `sujet`"}
        # Un sujet joker (« * » : tout nouveau projet de texte) vaut pour les documents, pas pour le programme.
        subjects = [(s["libelle"], s["requete"]) for s in alerts.list_subscriptions(conn, user["id"])
                    if s["requete"].strip() not in ("*", "")]
        live, _, _ = _live()
        for slug in sorted(live.followed(conn, user["id"])):
            d = live.dossier(slug) or {}
            subjects.append((d.get("nom") or slug, d.get("requete") or d.get("nom_en") or slug))
        if not subjects:
            return {"erreur": "no topics followed: add some with `suivre`, or specify `sujet`"}
        day = agenda.resolve_day(jour)
        blocs = []
        for label, q in subjects[:8]:
            terms = ", ".join(t for t in re.split(r"[,;]| OR | AND ", q.replace('"', "")) if t.strip() and t.strip() != "*")
            if not terms:
                continue
            ag = agenda.query(conn, jour=jour, sujet=terms, types=types, limit=12)
            bloc: dict[str, Any] = {"sujet": label, "requete": terms, "side_events_et_reunions": ag["evenements"],
                                    "total_side_events_et_reunions": ag["total"]}
            if pavillons:
                bloc["pavillons"] = pav.for_programme(conn, terms, jour, limit=12)
            blocs.append(bloc)
        out = {"jour": agenda.iso_day(day) if day else None, "par_sujet": blocs}
        ref = agenda.query(conn, jour=jour, limit=0)
        out.update({k: ref[k] for k in ("negociations_du_jour", "a_verifier", "sources") if k in ref})
        return out
    out: dict[str, Any] = agenda.query(conn, jour=jour, sujet=sujet, types=types, limit=lim)
    if pavillons:
        out["pavillons"] = pav.for_programme(conn, sujet, jour, limit=min(lim, 30))
    docs = search.latest(conn, user, {"doc_types": ["programme", "ordre_du_jour"]}, limit=5)
    out["documents"] = docs
    if sujet:
        out["passages"] = search.search(conn, sujet, {"doc_types": ["programme", "ordre_du_jour"]}, user, limit=5)
    if not out["evenements"] and not out["total"] and not (out.get("pavillons") or {}).get("evenements"):
        out["note"] = "No event found for this day and topic in the collected programme."
    return out


@mcp.tool(annotations=READ)
def pavillons(sujet: str | None = None, pavillon: str | None = None, jour: str | None = None,
              zone: str | None = None, mode: str = "hybride", limite: int = 80, evenement: str | None = None,
              couverture: bool = False) -> dict[str, Any]:
    """Searches the CONTENT of all pavilion and side-event programmes (≈5,000 events: title, summary,
    speakers, themes) for cross-cutting questions: "how many pavilions talked about tropical forests?", "who
    talked about critical minerals?". Results grouped by pavilion. For where/when to go: `programme`.
    `sujet`: comma-separated terms (OR), English + synonyms; "quoted" = exact phrase, word* = prefix.
    `mode`: hybride (default), mots (strict count), sens (related ideas: reread summaries). `pavillon`: partial
    name; `jour`; `zone`: blue|green|autre; `evenement`: id → full record; `couverture=true`: pavilions covered.
    Counting: 2–3 phrasings, give pavilions AND events, state limits (self-declared programmes). Level 5."""
    from . import pavillons as pav

    conn = db.get()
    if evenement:
        return pav.event(conn, evenement)
    if couverture:
        return pav.coverage(conn)
    return pav.query(conn, sujet=sujet, pavillon=pavillon, jour=jour, zone=zone,
                     mode=mode if mode in ("hybride", "mots", "sens") else "hybride",
                     limite=max(1, min(limite, 300)))


@mcp.tool(annotations=READ)
def soumissions(appel: int | None = None, auteur: str | None = None, document: str | None = None,
                dossier: str | None = None, groupe: str | None = None,
                requete: str | None = None, theme: str | None = None, session: str | None = None,
                ouverts: bool = False, parties_seulement: bool = False, limite: int = 40,
                ctx: Context | None = None) -> dict[str, Any]:
    """Official UNFCCC submissions (submissions.unfccc.int, since 2015). ONE mode per call, by priority:
    - document=<doc_id>: submission text + analysis instructions (you analyse; cached analysis attached).
    - appel=<id>: a call and all its submissions.
    - groupe="LDC"|"AOSIS"|"G77"…: by the group, its sub-groups and members (+ dossier= to narrow).
    - dossier=<slug|alias>: calls linked to the item, who submitted, known alignments.
    - auteur="EU"|"Brazil"…: by this actor (exact name after normalisation; close names if unknown).
    - otherwise: list of calls (requete = title words, theme, session, ouverts=true for current ones).
    Level: Party = 2 (not adopted), observer = 5. Compare two actors: comparer_soumissions."""
    from . import soumissions as sm

    conn = db.get()
    limite = max(1, min(limite, 200))
    try:
        if document:
            row = search.find_doc(conn, document, _user(ctx))
            if not row:
                return {"erreur": f"document not found: {document}"}
            item = sm.item_for_doc(conn, row["id"])
            out = {"document": search.doc_public(row), **sm.client_text(conn, row["id"]),
                   "consigne": sm.CLIENT_ANALYSE_GUIDE}
            if item:
                out["auteur"] = sm.speaks_for(item)
                out["appel"] = item["call_id"]
            cached = sm.analysis(conn, row["id"])
            if cached and not cached.get("erreur"):
                out["analyse_existante"] = cached
            return out
        if appel:
            return sm.call_detail(conn, appel, parties_only=parties_seulement) or {"erreur": f"unknown call {appel}"}
        slug = None
        if dossier:
            slug, err = _resolve_dossier(dossier)
            if err:
                return err
        if groupe:
            from . import groupes as grp

            gid = grp.group_id(groupe)
            if not gid:
                return {"erreur": f"unknown group: {groupe}"}
            calls = set(sm.dossier_call_ids(conn, slug)) if slug else None
            own = []
            try:
                own = [x for x in sm._actor_items(conn, gid) if calls is None or x["call_id"] in calls]
            except sm.UnknownActor:
                pass
            mem, n = sm.group_member_items(conn, gid, calls)
            subs = sm.subgroup_items(conn, gid, calls)
            out = {"groupe": grp.group_info(conn, gid),
                   "soumissions_du_groupe": [sm._item_public(conn, x, with_analysis=True) for x in own[:limite]],
                   "soumissions_des_sous_groupes": [{**sm._item_public(conn, x), "sous_groupe": sg, "appel": x["call_id"]}
                                                    for sg, x in subs[:limite]] or None,
                   "soumissions_des_membres": [{**sm._item_public(conn, x), "appel": x["call_id"]} for x in mem[:limite]],
                   "note": "A group often submits through its sub-groups or members: those are then their positions, "
                           "not the group's."}
            return {k: v for k, v in out.items() if v is not None}
        if slug:
            return {"dossier": slug, **sm.dossier_section(conn, slug, limit=min(limite, 25))}
        if auteur:
            return {"auteur": auteur, "soumissions": sm.by_entity(conn, auteur, limite)}
        return {"appels": sm.list_calls(conn, requete=requete, theme=theme, session=session, ouverts=ouverts,
                                        limite=limite), "etat": sm.stats(conn)}
    except Exception as exc:
        return {"erreur": str(exc)}


@mcp.tool(annotations=READ)
def comparer_soumissions(a: str | None = None, b: str | None = None, appel: int | None = None,
                         dossier: str | None = None, acteurs: list[str] | None = None,
                         qualifications: list[dict] | None = None, max_comparaisons: int = 6,
                         ctx: Context | None = None) -> dict[str, Any]:
    """Compares two actors' written positions (official submissions), in two steps:
    1. (a, b): picks their latest common call (or `appel`, or the item's via `dossier`) and returns both texts +
       instructions; you qualify each point. Cached comparison → returned directly.
    2. (a=<doc_id>, b=<doc_id>, qualifications=[…] as instructed): the server checks the quotes and computes the
       overall verdict by fixed rules.
    a/b: actor ("EU", "LMDC", "AOSIS") or doc_id. A group without its own submission falls back to sub-groups and
    members, then latest submissions (`repli`, `ecart_dates`: relay as is). b missing: `a` vs the other Parties of
    the call. acteurs=[…] + dossier=: alignment matrix from the cache only. An analytical aid, not a judgement."""
    from . import soumissions as sm

    conn = db.get()
    try:
        slug = None
        if dossier:
            slug, err = _resolve_dossier(dossier)
            if err:
                return err
        if acteurs:
            if not slug:
                return {"erreur": "acteurs=[…] requires dossier=… (slug or alias)"}
            return sm.alignment_matrix(conn, slug, acteurs)
        if not a:
            return {"erreur": "specify a (and b), or acteurs=[…] with dossier=…"}
        if qualifications is not None:
            _need_user(ctx)
            if not (str(a).isdigit() and str(b or "").isdigit()):
                return {"erreur": "qualifications=… requires the doc_id of both submissions (a=…, b=…)"}
            return sm.score_client(conn, int(a), int(b), qualifications)
        if not b:
            return sm.compare_in_call(conn, sm.latest_doc(conn, a, appel), max_n=max(1, min(max_comparaisons, 10)))
        from . import groupes as grp

        is_group = bool(grp.group_id(a) or grp.group_id(b))
        try:
            info = sm.resolve_pair_info(conn, a, b, appel, dossier=slug)
        except sm.UnknownActor:
            if not is_group:
                raise
            # Groupe sans soumission propre : sous-groupes, membres, puis dernière soumission de chacun.
            return sm.compare_actors(conn, a, b, appel=appel, dossier=slug)
        if not info["appel"] and is_group:
            # Le groupe a soumis, mais pas sur un appel commun : ses sous-groupes ou ses membres ont peut-être répondu.
            return sm.compare_actors(conn, a, b, appel=appel, dossier=slug)
        da, dbb, common = info["doc_a"], info["doc_b"], info["appel"]
        out = sm.cached_comparison(conn, da, dbb) or {
            "a": {"doc_id": da, "auteur": sm._who(conn, da), **sm.client_text(conn, da, 20000)},
            "b": {"doc_id": dbb, "auteur": sm._who(conn, dbb), **sm.client_text(conn, dbb, 20000)},
            "consigne": sm.CLIENT_COMPARE_GUIDE}
        call = conn.execute("SELECT * FROM sub_calls WHERE id=?", (common,)).fetchone() if common else None
        out["appel_commun"] = sm._call_public(call) if call else None
        if not common:
            out["attention"] = ("No common call: the two submissions answer different questions; "
                                "the comparison is only indicative.")
        elif len(info["appels_communs"]) > 1 and not appel:
            out["autres_appels_communs"] = sm.calls_brief(conn, info["appels_communs"][1:6])
            out["choix_appel"] = "most recent common call used; specify appel=… for another"
        gap = sm.date_gap(conn, da, dbb)
        if gap:
            out["ecart_dates"] = gap
        return out
    except Exception as exc:
        return {"erreur": str(exc)}


@mcp.tool(annotations=READ)
def groupes(groupe: str | None = None, partie: str | None = None) -> dict[str, Any]:
    """Negotiating groups: who belongs to what, with the SOURCE and date of each list.
    - groupe="LDC" / "PMA", "AOSIS", "G77", "EU", "Umbrella", "BASIC", "EIG", "LLDC", "SIDS":
      members (name, ISO3, year joined if the source gives it).
    - partie="China", "Brazil", "Tuvalu"…: groups it belongs to.
    - no argument: all groups and the status of their list.
    Status: verifie (sourced list, count matches); verifie_par_equivalence (AGN = UN African regional group,
    Arab Group = League of Arab States: say so with the `equivalence` sentence); ecart (to check);
    non_verifie (LMDC, AILAC, ALBA, CfRN: no official list — NEVER infer the membership, only quote the
    number given by the source)."""
    from . import groupes as grp

    return grp.query(db.get(), groupe=groupe, partie=partie)


@mcp.tool(annotations=READ)
def mes_sujets(ctx: Context | None = None) -> dict[str, Any]:
    """Topics followed by the user (Telegram and dashboard alerts), followed items and latest alerts."""
    user = _need_user(ctx)
    conn = db.get()
    rows = conn.execute(
        """SELECT n.created_at, n.doc_id, s.label FROM notifications n LEFT JOIN subscriptions s ON s.id=n.subscription_id
           WHERE n.user_id=? ORDER BY n.id DESC LIMIT 20""", (user["id"],)
    ).fetchall()
    live, _, _ = _live()
    follows = sorted(live.followed(conn, user["id"])) if user.get("id") else []
    subs = alerts.list_subscriptions(conn, user["id"])
    out = {"sujets": subs,
           "dossiers_suivis": [{"slug": s, "nom": (live.dossier(s) or {}).get("nom")} for s in follows],
           "dernieres_alertes": [dict(r) for r in rows]}
    over = alerts.overlaps(subs)
    if over:
        out["chevauchements"] = over
    vague = {s["id"]: alerts.generic_fragments(s["requete"]) for s in subs}
    vague = {k: v for k, v in vague.items() if v}
    if vague:
        out["termes_trop_vagues"] = {"par_sujet": vague, "conseil": "these fragments match almost any text "
                                     "(e.g. “paragraph 8” alone): rewrite the topic with full phrases"}
    return out


@mcp.tool(annotations=WRITE)
def suivre(mots_cles: str | None = None, dossier: str | None = None, arreter: int | None = None,
           ne_plus_suivre: bool = False, libelle: str | None = None, niveaux: list[str] | None = None,
           types: list[str] | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Alerts (Telegram + dashboard). Exactly one of three modes:
    - mots_cles=…: alert on each new matching document; comma-separated phrases, in English AND French
      ("fossil fuels, combustibles fossiles, transition away"); "*" + types = every new document of those types
      (e.g. types=["projet_texte"]).
    - dossier=… (slug or alias: GGA, finance, ACE, art6…): alert on each new text of the item;
      ne_plus_suivre=true to stop.
    - arreter=<id>: delete a keyword alert (id given by mes_sujets)."""
    user = _need_user(ctx)
    conn = db.get()
    modes = [x is not None and x != "" for x in (mots_cles, dossier, arreter)]
    if sum(modes) != 1:
        return {"erreur": "specify exactly one of: mots_cles, dossier, arreter"}
    if arreter is not None:
        return {"ok": alerts.remove_subscription(conn, user["id"], arreter), "alerte_supprimee": arreter}
    if dossier:
        live, _, _ = _live()
        slug = live.dossier_from_alias(dossier) or (dossier if live.dossier(dossier) else None)
        if not slug:
            return {"erreur": "unknown item: see the `dossiers` tool for the list and aliases"}
        live.follow(conn, user["id"], slug, not ne_plus_suivre)
        return {"ok": True, "dossier": slug, "suivi": not ne_plus_suivre}
    existing = alerts.list_subscriptions(conn, user["id"])
    sid = alerts.add_subscription(conn, user["id"], libelle or mots_cles[:60], mots_cles, niveaux, types)
    out = {"ok": True, "id": sid, "apercu_7_jours": alerts.preview(conn, mots_cles, niveaux, types, user, days=7, limit=5)}
    over = alerts.overlaps(existing, mots_cles)
    if over:
        out["chevauchements"] = [{**o, "ids": [sid, *o["ids"]]} for o in over]
    vague = alerts.generic_fragments(mots_cles)
    if vague:
        out["termes_trop_vagues"] = vague
    return out


_DATE_IN_TEXT = re.compile(
    r"\b\d{1,2}(?:er|st|nd|rd|th)?\s+[A-Za-zéûè]{3,9}\.?,?\s+20\d\d\b|\b[A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+20\d\d\b|"
    r"\b20\d\d-\d{2}-\d{2}\b")


def _parse_date(text: str) -> str | None:
    from . import extract

    fn = getattr(extract, "parse_date", None)
    if fn:
        return fn(text or "")
    m = re.search(r"\b(20\d\d)-(\d{2})-(\d{2})\b", text or "")
    return m.group(0) if m else None


def verify_deposit(conn, doc_id: int, titre: str | None) -> dict[str, Any]:
    """Date et titre LUS DANS LE CONTENU d'un document déposé, comparés à ce que l'utilisateur a saisi. La date lue
    devient la date de publication ; une date du titre saisi qui contredit le document y est remplacée."""
    from . import extract

    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    out: dict[str, Any] = {"ecarts": []}
    # Texte relu dans les passages indexés (le site ne relit jamais un PDF lui-même : voir extract.DEFER_PDF).
    found_title = None
    pages = [extract.Page(r[0] or 1, r[1] or "") for r in conn.execute(
        "SELECT page, group_concat(text, char(10)) FROM (SELECT page, text FROM passages WHERE doc_id=? ORDER BY seq) "
        "GROUP BY page ORDER BY page", (doc_id,))]
    if not pages:
        return {"ecarts": ["content unreadable for verification: no indexed text"]}
    detect = getattr(extract, "detect_date", None)
    date_read = detect(pages) if detect else _parse_date(pages[0].text[:1500] if pages else "")
    title_read = extract.guess_title(pages, found_title)
    out["date_lue_dans_le_document"] = date_read
    out["titre_lu_dans_le_document"] = title_read
    if date_read:
        if doc["published"] and str(doc["published"])[:10] != date_read:
            out["ecarts"].append(f"publication date {doc['published']} replaced by the document's date {date_read}")
        conn.execute("UPDATE documents SET published=? WHERE id=?", (date_read, doc_id))
    else:
        out["ecarts"].append("no readable date in the document header: publication date left empty "
                             "(do not infer it from the issue number or the retrieval day)")
    title = doc["title"] or ""
    if titre:
        typed = _parse_date(titre)
        if typed and date_read and typed != date_read:
            fixed = " ".join(_DATE_IN_TEXT.sub(date_read, titre).split())
            out["ecarts"].append(f"the title entered carries the date {typed}, the document is dated {date_read}: title corrected")
            ingest._set_title(conn, doc_id, fixed[:250])
            title = fixed
        words = {w for w in re.findall(r"[a-zà-ÿ0-9]{4,}", titre.lower())}
        read = {w for w in re.findall(r"[a-zà-ÿ0-9]{4,}", (title_read or "").lower())}
        if title_read and words and read and not words & read:
            out["ecarts"].append(f"the title entered does not resemble the title read in the document (“{title_read[:120]}”): "
                                 "check that this is the right document")
    conn.commit()
    out["titre_retenu"] = title
    if not out["ecarts"]:
        out["ecarts"] = None
    return out


@mcp.tool(annotations=WRITE)
async def deposer_url(url: str, titre: str | None = None, niveau: str | None = None, type: str | None = None,
                      organisation: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Add to the (public) corpus a web page or PDF available online: press release, statement, analysis…
    Specify the level (2 official, 4 daily report, 5 actor's position). Publication date and title are READ FROM
    THE DOCUMENT and compared with what was entered: `verification.ecarts` says what was corrected (date in the
    title contradicted by the header, unrelated title). Blocked page (anti-bot): look for the PDF version of the
    same document, otherwise upload the file via the dashboard or Telegram. Contributors only."""
    import anyio

    user = _need_user(ctx)
    if not auth.can_contribute(user):
        return {"erreur": "reserved for contributors: guest accounts can search the corpus but not add documents"}
    # Hors de la boucle d'événements : téléchargement et attente de l'indexation ne figent pas le site.
    return await anyio.to_thread.run_sync(lambda: _deposer_url(url, titre, niveau, type, organisation, user))


DEPOSIT_WAIT_SECONDS = 90


def _deposer_url(url, titre, niveau, type, organisation, user) -> dict[str, Any]:
    import time

    from . import fetch

    res = fetch.Client().get(url)
    if not res.ok or fetch.is_antibot(res):
        return {"erreur": f"unreachable ({res.status}{', anti-bot' if res.ok else ''}): look for the PDF version of the "
                          "same document (e.g. ENB: /sites/default/files/YYYY-MM/enbNNNNNe.pdf), otherwise upload the "
                          "file via the dashboard or Telegram"}
    meta = {k: v for k, v in {"title": titre, "level": niveau, "doc_type": type, "org": organisation}.items() if v}
    conn = db.get()
    doc_id, new, status = ingest.ingest_bytes(conn, res.content, url=res.url, mime=res.mime, meta=meta,
                                              source_id="depot-mcp", uploaded_by=user.get("id") or None)
    deadline = time.monotonic() + DEPOSIT_WAIT_SECONDS
    while status == "pending" and time.monotonic() < deadline:   # PDF : lu par le worker (≈ 15 s), pas par le site
        time.sleep(3)
        status = conn.execute("SELECT status FROM documents WHERE id=?", (doc_id,)).fetchone()["status"]
    check = verify_deposit(conn, doc_id, titre) if status == "indexed" else None
    alerts.run(conn)
    out = {"doc_id": doc_id, "nouveau": new, "statut": status, "lien": f"{config.PUBLIC_BASE}/doc/{doc_id}"}
    if status == "pending":
        out["message"] = ("indexing in progress (the server reads PDFs in the background): text, date and title "
                          "checks will be available in a few minutes at the link; call `document` again later")
    if check:
        out["verification"] = check
    return out


@mcp.tool(annotations=READ)
def etat_corpus() -> dict[str, Any]:
    """Corpus volume by level, type and status; date of the latest addition; monitored sources; current version of
    the COP31 skills and pasted instructions (tell the user if theirs is older)."""
    conn = db.get()
    out = search.stats(conn)
    out["consignes_clients"] = {"version": CLIENT_VERSION, "mise_a_jour": f"{config.PUBLIC_BASE}/connect",
                                "claude_code": "/plugin marketplace add akimx98/RAG_cop_climat ; "
                                               "/plugin marketplace update cop31"}
    out["sources_surveillees"] = [dict(r) for r in conn.execute(
        "SELECT source_id, last_run, last_result FROM source_state ORDER BY last_run DESC LIMIT 25")]
    return out


# --- veille en direct : dossiers, textes en cours, positions, notes ------------------------

TIME_NOTE = ("published: local time in Antalya (UTC+3), as posted by the index; server timestamps (first_seen, "
             "recu_le, created_at) are UTC (Z)")


def _live():
    from . import briefs, live, positions

    return live, positions, briefs


@mcp.tool(annotations=READ)
def dossiers(textes: bool = False, a_recuperer: bool = False, limite: int = 30,
             ctx: Context | None = None) -> dict[str, Any]:
    """COP31 negotiation items (mitigation, GGA, finance, Art. 6, just transition…) with their status (per the
    Carbon Brief index), number of texts and positions, and aliases. Entry point for "where do things stand?".
    Then use `dossier(slug)`.
    textes=true: also the latest negotiation texts announced, all items (symbol, title, time, brackets, options,
    retrieved or not); a_recuperer=true: only those not yet retrieved in the corpus."""
    live, _, _ = _live()
    conn = db.get()
    rows = live.overview(conn, _user(ctx))
    out = {"dossiers": [{k: d[k] for k in ("slug", "nom", "alias", "enjeu", "etat", "nb_textes", "a_recuperer",
                                           "nb_positions", "dernier")} for d in rows],
           "source_index": f"{live.CB_CREDIT} — {live.CB_TEXTS_PAGE} (to cite)"}
    if textes or a_recuperer:
        ts = live.texts(conn, None, pending_only=a_recuperer, limit=min(limite, 100))
        out["textes"] = [{**{k: t[k] for k in ("dossier", "symbol", "title", "body", "agenda_item", "brackets",
                                               "options", "pdf_url")},
                          "published": live.local_time(t["published"]), "etape": live.text_stage(dict(t)),
                          "lien": f"{config.PUBLIC_BASE}/doc/{t['doc_id']}" if t["doc_id"] else None} for t in ts]
    out["fuseau"] = TIME_NOTE
    return out


@mcp.tool(annotations=READ)
def dossier(slug: str, ctx: Context | None = None) -> dict[str, Any]:
    """A whole negotiation item: status, successive negotiation texts (brackets, options, link to the document and
    the diff), AI brief if enabled, positions recorded during the COP (exact quote and source), written positions
    and linked calls for submissions (who submitted, alignments already computed), reference positions (Carbon
    Brief). Each text carries its `etape` (draft, for_adoption = L document forwarded to the plenary, adopted).
    `slug` or alias (e.g. GGA, finance, MWP, ACE; alias table: `dossiers` tool). Ambiguous word → `candidats`:
    ask the user, do not pick. Text index and reference positions: cite "according to Carbon Brief's tracker".
    Any brief is AI-written: present it as such and rely on its numbered sources. Quote texts and positions exactly,
    with their source and level (a position reported by the ENB is a report, level 4, not the Party's statement)."""
    live, positions, briefs = _live()
    conn = db.get()
    word = slug
    slug, err = _resolve_dossier(word)
    if err:
        return err
    d = live.dossier(slug)
    pos = positions.for_dossier(conn, slug, limit=300)
    texts = live.texts(conn, slug, limit=40)

    def text_row(t) -> dict[str, Any]:
        row = {**{k: t[k] for k in ("symbol", "title", "body", "agenda_item", "pages", "brackets", "options",
                                    "pdf_url", "note")},
               "published": live.local_time(t["published"]),
               "statut": "retrieved" if t["doc_id"] else "to retrieve",
               "etape": live.text_stage(dict(t)),
               "lien": f"{config.PUBLIC_BASE}/doc/{t['doc_id']}" if t["doc_id"] else None,
               "diff": f"{config.PUBLIC_BASE}/diff/{t['doc_id']}" if t["doc_id"] else None}
        hint = live.compilation_hint(conn, t["doc_id"]) if t["doc_id"] else None
        if hint:
            row["compilation"] = hint["consequence"]
        return {k: v for k, v in row.items() if v is not None}

    statuses = live.status_by_dossier(conn).get(slug, [])
    out = {
        "dossier": {k: d[k] for k in ("slug", "nom", "enjeu", "alias")},
        "etat": [f"{x['cb_topic']}: {x['status']}" for x in statuses],
        "textes": [text_row(t) for t in texts],
        "brief": briefs.latest(conn, slug),
        "positions_cop": [{k: p[k] for k in ("actor", "stance", "issue", "summary", "quote", "said_on",
                                             "source_label", "source_url")}
                          for p in pos if p["source_kind"] not in ("carbonbrief", "soumission")][:80],
        "positions_ecrites": [{k: p[k] for k in ("actor", "stance", "issue", "summary", "quote", "said_on", "source_url")}
                              for p in pos if p["source_kind"] == "soumission"][:60],
        "soumissions": _sub_section(conn, slug),
        "positions_reference": [{k: p[k] for k in ("actor", "stance", "issue", "source_url")}
                                for p in pos if p["source_kind"] == "carbonbrief"],
        "credit": f"Index and reference positions: {live.CB_CREDIT} (cite). COP positions: extracted from public "
                  "sources, quote checked word for word. Written positions: points from recent official Party "
                  "submissions (level 2, quote checked), dated by filing. Brief: AI-written, check against the sources.",
        "page": f"{config.PUBLIC_BASE}/database/negotiations/{slug}",
        "fuseau": TIME_NOTE,
        "lexique": "etape: draft (draft text, informal note), for_adoption (L document forwarded to the plenary), "
                   "adopted, other; brackets/options: counts from Carbon Brief's index (crochets tool for themes)",
    }
    stale = [x["cb_topic"] for x in statuses if re.search(r"\bno text\b", x["status"] or "", re.I)]
    if stale and texts:
        out["etat_avertissement"] = (
            f"Carbon Brief status “No text” for {', '.join(stale)} while {len(texts)} texts of this item are indexed "
            f"(latest {live.local_time(texts[0]['published']) or texts[0]['first_seen']}): the status is per "
            "sub-topic and may lag; rely on the texts list")
    return out


def _sub_section(conn, slug: str) -> dict[str, Any] | None:
    from . import soumissions as sm

    try:
        out = sm.dossier_section(conn, slug, limit=8)
        return out if out["appels"] else None
    except Exception:
        return None


@mcp.tool(annotations=READ)
def positions(acteur: str | None = None, dossier: str | None = None, depuis: str | None = None,
              limite: int = 80, divisions: bool = False, groupe: str | None = None, compact: bool | None = None,
              decalage: int = 0, ctx: Context | None = None) -> dict[str, Any]:
    """Who wants what: positions of an actor (EU, G77 and China, LMDC, AOSIS, LDC, AGN, Arab Group, China,
    Saudi Arabia…) and/or on an item, with stance, issue, exact quote, date and source. `depuis`: "24h", "3d"
    (or "3j") or ISO date (positions recorded since). Sources: negotiation reports (ENB, level 4, attributes
    interventions in the room: "X, supported by Y, opposed by Z"), official statements, submissions.
    divisions=true (+ groupe="G77" by default, "AOSIS", "AGN"…; dossier= optional): the group's INTERNAL
    DIVISIONS — issues where some sub-groups or members support while others oppose, with each camp's allies
    outside the group (e.g. AOSIS and LDCs with the EU, against the Arab Group and India).
    `compact=true` (default with divisions): actor, stance, issue, date, link — no quotes (fetch them with
    compact=false on a narrower query). `limite` + `decalage` paginate (`suite` gives the next offset)."""
    live, pos, _ = _live()
    conn = db.get()
    live.ensure_schema(conn)
    slug = None
    if dossier:
        slug, err = _resolve_dossier(dossier)
        if err:
            return err
    if divisions:
        from . import groupes as grp

        gid = grp.group_id(groupe or acteur or "G77")
        if not gid:
            return {"erreur": f"unknown group: {groupe or acteur} (see the `groupes` tool)"}
        out = pos.divisions(conn, gid, dossier=slug, since=_since(depuis), compact=compact is not False)
        lim = max(1, min(limite, 25))
        total = len(out["divisions"])
        out["divisions"] = out["divisions"][decalage:decalage + lim]
        out["nb_divisions"] = total
        if decalage + lim < total:
            out["suite"] = {"decalage": decalage + lim, "reste": total - decalage - lim}
        return out
    where, params = ["access='public'"], []
    if acteur:
        # Nom canonique exact (« UE » → « EU ») ; jamais de correspondance partielle (« Mali » ≠ « Somalia ») :
        # rien trouvé → noms proches proposés, aucun autre acteur substitué.
        exact = pos.normalize_actor(acteur)
        if not conn.execute("SELECT 1 FROM positions WHERE actor=? AND access='public' LIMIT 1", (exact,)).fetchone():
            words = [w for w in re.findall(r"\w+", acteur.lower()) if len(w) > 2] or [acteur.lower()]
            near = [r[0] for r in conn.execute(
                f"""SELECT actor, COUNT(*) n FROM positions WHERE access='public' AND
                    ({' OR '.join('lower(actor) LIKE ?' for _ in words)}) GROUP BY actor ORDER BY n DESC LIMIT 10""",
                [f"%{w}%" for w in words])]
            return {"positions": [], "acteur": exact,
                    "message": f"no position recorded for “{acteur}” (canonical name “{exact}”)"
                               + (f"; similar names: {', '.join(near)}" if near else "")}
        where.append("actor=?")
        params.append(exact)
    if slug:
        where.append("dossier=?")
        params.append(slug)
    if depuis:
        where.append("created_at>=?")
        params.append(_since(depuis))
    lim = max(1, min(limite, 300))
    total = conn.execute(f"SELECT COUNT(*) FROM positions WHERE {' AND '.join(where)}", params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT dossier, actor, stance, issue, summary, quote, said_on, source_kind, source_label, source_url
            FROM positions WHERE {' AND '.join(where)} ORDER BY COALESCE(said_on, created_at) DESC LIMIT ? OFFSET ?""",
        (*params, lim, max(0, decalage))).fetchall()
    keys = ("dossier", "actor", "stance", "issue", "said_on", "source_url") if compact else None
    out_rows = [{k: r[k] for k in keys} if keys else dict(r) for r in rows]
    extra: dict[str, Any] = {"nb_total": total}
    if decalage + lim < total:
        extra["suite"] = {"decalage": decalage + lim, "reste": total - decalage - lim}
    return {"positions": out_rows, **extra,
            "note": "source_kind=carbonbrief: pre-COP reference positions (Carbon Brief, to cite); "
                    "extraction/bluesky: recorded during the COP, quote checked in the source; "
                    "soumission: written position, a point from a recent official UNFCCC submission (level 2), "
                    "dated by filing (said_on), quote checked (detail: soumissions(document=…))."}


# --- méthodes toutes prêtes (prompts MCP : menu « + » de Claude, « / » ailleurs) -------------------------
# Chaque méthode existe en français et en anglais ; la langue suit le compte (anglais par défaut pour un invité),
# ou l'argument `langue` (« fr » / « en »).

_RIGUEUR = {
    "fr": ("Règles : chaque fait vient d'un passage renvoyé par un outil, cité entre guillemets avec sa référence, "
           "son niveau de fiabilité et son lien ; un projet de texte porte sa version, sa date et la mention "
           "« non adopté » ; rien trouvé → le dire. Distinguer ce que dit le texte de ce qu'en disent les acteurs."),
    "en": ("Rules: every fact comes from a passage returned by a tool, quoted with its reference, reliability level "
           "and link; a draft text always carries its version, date and the words “not adopted”; nothing found → say "
           "so. Keep what the text says apart from what actors say about it. Answer in English."),
}


def _lang(ctx, langue: str | None) -> str:
    from . import i18n

    if langue and langue.lower()[:2] in i18n.LANGS:
        return langue.lower()[:2]
    try:
        return i18n.default_for(_user(ctx))
    except Exception:
        return "en"


# Étapes de chaque méthode, définies UNE fois (FR et EN) ; les prompts ci-dessous les mettent en forme, les skills du
# plugin (clients/claude-plugin/skills) suivent les mêmes outils. Un test vérifie que chaque outil cité existe.
METHODES: dict[str, dict[str, list[str]]] = {
    "point_negociation": {
        "fr": ["`dossier` (slug ou alias) : état, textes successifs, crochets et options, rubrique "
               "« soumissions » (appels liés, qui a soumis, alignements connus).",
               "`comparer_versions` sur la dernière version : lire `resume` et `options` (option retenue, en tout ou "
               "en partie), unités supprimées sans reprise, reprises et fusions, devenir des crochets et des "
               "alternatives — citer avant/après avec les références `ancien`/`nouveau` (page, lien PDF), "
               "`filtre=\"§N\"` pour un paragraphe ; `historique_formulation(notion=…, dossier=…)` pour les notions clés "
               "(1,5 °C, fossiles, financement…) version par version ; `crochets(theme=…, texte=…)` : ce qui reste entre crochets "
               "sur un thème (genre, droits humains, désinformation…) et depuis quelle version.",
               "`positions(dossier=…, depuis=…)` : qui pousse quoi, qui bloque (l'ENB attribue les interventions) ; "
               "clivages internes d'un groupe : `positions(divisions=true, groupe=…)` ; positions écrites : "
               "`comparer_soumissions(<acteur>, <acteur>, dossier=…)` (aide à l'analyse).",
               "`nouveautes(depuis=…)` et `rechercher` (termes onusiens en anglais) pour les comptes rendus (ENB, Carbon Brief)."],
        "en": ["`dossier` (slug or alias): status, successive texts, brackets and options, “soumissions” "
               "section (linked calls, who submitted, known alignment).",
               "`comparer_versions` on the latest version: read `resume` and `options` (option retained, in full or "
               "in part), units deleted with no trace, splits and merges, fate of brackets and alternatives — quote "
               "before/after with the `ancien`/`nouveau` references (page, PDF link), `filtre=\"§N\"` for one "
               "paragraph; `historique_formulation(notion=…, dossier=…)` for key notions (1.5 °C, fossil fuels, "
               "finance…) version by version; `crochets(theme=…, texte=…)`: what stays in brackets on a theme "
               "(gender, human rights, disinformation…) and since which version.",
               "`positions(dossier=…, depuis=…)`: who pushes what, who blocks (ENB attributes interventions); "
               "a group's internal splits: `positions(divisions=true, groupe=…)`; written positions: "
               "`comparer_soumissions(<actor>, <actor>, dossier=…)` (an analytical aid).",
               "`nouveautes(depuis=…)` and `rechercher` (UN terms in English) for daily reports (ENB, Carbon Brief)."],
    },
    "position_pays": {
        "fr": ["A. Position OFFICIELLE ÉCRITE (niveaux 1–2) : `soumissions(auteur=…)` puis `soumissions(document=…)` "
               "(points soulevés, force du langage, citations) ; NDC : `rechercher(…, partie=…, types=[\"ndc\"])` ; "
               "groupes dont il est membre : `groupes(partie=…)`.",
               "B. Ce qu'il dit et fait PENDANT la COP : `positions(acteur=…)`, comptes rendus (niveaux 4–5).",
               "C. Les FAITS qui éclairent l'écart : `donnees` (émissions, production) et politiques nationales du corpus.",
               "Terminer par les écarts entre A, B et C, et ce qui reste non documenté."],
        "en": ["A. OFFICIAL WRITTEN position (levels 1–2): `soumissions(auteur=…)` then `soumissions(document=…)` "
               "(points raised, strength of language, quotes); NDC: `rechercher(…, partie=…, types=[\"ndc\"])`; groups "
               "it belongs to: `groupes(partie=…)`.",
               "B. What it says and does DURING the COP: `positions(acteur=…)`, daily reports (levels 4–5).",
               "C. The FACTS that shed light on the gap: `donnees` (emissions, production) and national policies in the corpus.",
               "End with the gaps between A, B and C, and what remains undocumented."],
    },
    "preparer_rencontre": {
        "fr": ["Où en est le texte (dernière version, ce qui a bougé : `dossier`, `comparer_versions`).",
               "Qui est l'interlocuteur : `groupes(partie=…)` (ses groupes, liste sourcée) ; sa position écrite : "
               "`soumissions(auteur=…)` ; écart avec l'UE ou d'autres blocs : `comparer_soumissions(…, dossier=…)` ; "
               "ce qu'il dit en salle : `positions(acteur=…)`.",
               "Les points de blocage identifiés et qui les porte.",
               "Six à huit QUESTIONS précises, rattachées chacune à un paragraphe ou à une option du texte, pour comprendre "
               "la position réelle, les lignes rouges et les marges de compromis (questions ouvertes, non accusatoires)."],
        "en": ["Where the text stands (latest version, what moved: `dossier`, `comparer_versions`).",
               "Who they are: `groupes(partie=…)` (their groups, sourced list); their written position: "
               "`soumissions(auteur=…)`; gap with the EU or other blocs: `comparer_soumissions(…, dossier=…)`; what they "
               "say in the room: `positions(acteur=…)`.",
               "The sticking points identified and who holds them.",
               "Six to eight precise QUESTIONS, each tied to a paragraph or option of the text, to understand the real "
               "position, red lines and room for compromise (open, non-accusatory questions)."],
    },
    "plaidoyer": {
        "fr": ["Appuyer chaque argument sur : le texte en négociation (`dossier`, paragraphe et option visés), le droit "
               "déjà adopté (`rechercher`, niveaux 1), la science (GIEC) ou des données (`donnees`).",
               "Alliés et opposants par écrit : `comparer_soumissions(…, dossier=…)` ; en salle : `positions(dossier=…)`.",
               "Proposer une formulation de paragraphe concrète à défendre ; anticiper les deux objections les plus "
               "probables avec leur réponse.",
               "Terminer par la liste des sources (référence, niveau, lien)."],
        "en": ["Base every argument on: the text under negotiation (`dossier`, paragraph and option targeted), law already "
               "adopted (`rechercher`, level 1), science (IPCC) or data (`donnees`).",
               "Allies and opponents in writing: `comparer_soumissions(…, dossier=…)`; in the room: `positions(dossier=…)`.",
               "Propose concrete paragraph wording to defend; anticipate the two most likely objections with their answer.",
               "End with the list of sources (reference, level, link)."],
    },
    "decouvrir_sujet": {
        "fr": ["L'enjeu en clair (10 lignes au plus), sigles expliqués avec `glossaire`.",
               "Ce qui est déjà décidé (décisions antérieures, niveau 1 : `rechercher`).",
               "Ce qui se négocie à la COP31 : `dossier` (dernière version du texte, appels à soumissions liés).",
               "Qui veut quoi : `positions(dossier=…)` ; les blocs : `groupes`.",
               "Trois questions à poser pour aller plus loin."],
        "en": ["The stakes in plain words (10 lines max), acronyms explained with `glossaire`.",
               "What is already decided (past decisions, level 1: `rechercher`).",
               "What is being negotiated at COP31: `dossier` (latest version of the text, linked calls for submissions).",
               "Who wants what: `positions(dossier=…)`; the blocs: `groupes`.",
               "Three questions to dig deeper."],
    },
    "quoi_de_neuf": {
        "fr": ["`mes_sujets` (sujets et dossiers suivis).",
               "Pour chaque dossier suivi : `dossier` ; puis `nouveautes(depuis=…)` (nouveaux textes, soumissions "
               "récentes, comptes rendus).",
               "Classer par importance, trois lignes par sujet au plus."],
        "en": ["`mes_sujets` (topics and negotiation items followed).",
               "For each item followed: `dossier`; then `nouveautes(depuis=…)` (new texts, recent submissions, daily reports).",
               "Rank by importance, three lines per topic at most."],
    },
}


def _steps(name: str, lg: str) -> str:
    items = METHODES[name][lg]
    if all(re.match(r"[A-C]\. ", x) for x in items[:3]):
        return "\n".join(items)  # étapes déjà lettrées (A, B, C)
    return "\n".join(f"{i}. {x}" for i, x in enumerate(items, 1))


@mcp.prompt(title="Negotiation update / Point de négociation")
def point_negociation(dossier: str, depuis: str = "24h", langue: str = "", ctx: Context | None = None) -> str:
    """Where a negotiation item stands and what moved (e.g. “ACE”, “GGA”, “finance”, “art6”)."""
    lg = _lang(ctx, langue)
    if lg == "en":
        return (f"Give me an update on the COP31 negotiation “{dossier}”, focusing on what changed since {depuis}.\n"
                f"{_steps('point_negociation', 'en')}\nOutput: the gist in 3 lines; what moved; sticking points and who "
                f"holds them; what is still in brackets; what to watch. {_RIGUEUR['en']}")
    return (f"Fais le point sur la négociation « {dossier} » à la COP31, en insistant sur ce qui a changé depuis {depuis}.\n"
            f"{_steps('point_negociation', 'fr')}\nRendu : l'essentiel en 3 lignes ; ce qui a bougé ; points de blocage et "
            f"qui les porte ; ce qui reste entre crochets ; à surveiller. {_RIGUEUR['fr']}")


@mcp.prompt(title="Country position: official vs. in practice / Position d'un pays")
def position_pays(acteur: str, sujet: str, langue: str = "", ctx: Context | None = None) -> str:
    """Official and actual position of a country or group (e.g. Brazil, fossil fuel phase-out)."""
    if _lang(ctx, langue) == "en":
        return (f"What is the position of “{acteur}” on “{sujet}”? Keep these clearly apart:\n"
                f"{_steps('position_pays', 'en')}\n{_RIGUEUR['en']}")
    return (f"Quelle est la position de « {acteur} » sur « {sujet} » ? Sépare nettement :\n"
            f"{_steps('position_pays', 'fr')}\n{_RIGUEUR['fr']}")


@mcp.prompt(title="Prepare a meeting with a delegate / Préparer une rencontre")
def preparer_rencontre(sujet: str, interlocuteur: str = "", langue: str = "", ctx: Context | None = None) -> str:
    """One-page brief and questions to ask a delegate (e.g. “ACE”, “a delegate from France”)."""
    if _lang(ctx, langue) == "en":
        return (f"I am about to talk to {interlocuteur or 'a delegate'} about “{sujet}”. Prepare a one-page brief:\n"
                f"{_steps('preparer_rencontre', 'en')}\n{_RIGUEUR['en']}")
    return (f"Je vais parler à {interlocuteur or 'un délégué'} sur « {sujet} ». Prépare-moi une fiche d'une page :\n"
            f"{_steps('preparer_rencontre', 'fr')}\n{_RIGUEUR['fr']}")


@mcp.prompt(title="Sourced advocacy note / Plaidoyer sourcé")
def plaidoyer(sujet: str, demande: str = "", public: str = "", longueur: str = "", langue: str = "",
              ctx: Context | None = None) -> str:
    """Advocacy note where every argument is sourced (text, science, data)."""
    if _lang(ctx, langue) == "en":
        return (f"Draft an advocacy note on “{sujet}”{f', asking for: {demande}' if demande else ''}, "
                f"for {public or 'negotiators'}, {longueur or 'one page'} long.\n{_steps('plaidoyer', 'en')}\n"
                f"{_RIGUEUR['en']}")
    return (f"Rédige un argumentaire de plaidoyer sur « {sujet} »{f', pour demander : {demande}' if demande else ''}, "
            f"destiné à des {public or 'négociateurs'}, en {longueur or 'une page'}.\n{_steps('plaidoyer', 'fr')}\n"
            f"{_RIGUEUR['fr']}")


@mcp.prompt(title="Discover a topic / Découvrir un sujet")
def decouvrir_sujet(sujet: str, langue: str = "", ctx: Context | None = None) -> str:
    """A negotiation topic explained to a newcomer (e.g. “climate education”)."""
    if _lang(ctx, langue) == "en":
        return (f"Explain “{sujet}” to someone new to climate negotiations, then the details:\n"
                f"{_steps('decouvrir_sujet', 'en')}\n{_RIGUEUR['en']}")
    return (f"Explique « {sujet} » à quelqu'un qui découvre les négociations climat, puis le détail :\n"
            f"{_steps('decouvrir_sujet', 'fr')}\n{_RIGUEUR['fr']}")


@mcp.prompt(title="What's new on my topics? / Quoi de neuf ?")
def quoi_de_neuf(depuis: str = "24h", langue: str = "", ctx: Context | None = None) -> str:
    """What happened since a given time on the topics and negotiation items I follow."""
    if _lang(ctx, langue) == "en":
        return f"What happened since {depuis} on my topics?\n{_steps('quoi_de_neuf', 'en')}\n{_RIGUEUR['en']}"
    return f"Que s'est-il passé depuis {depuis} sur mes sujets ?\n{_steps('quoi_de_neuf', 'fr')}\n{_RIGUEUR['fr']}"
