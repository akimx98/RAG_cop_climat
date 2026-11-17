"""Serveur MCP du corpus COP31.

Le serveur est volontairement « bête et traçable » : il renvoie des passages
exacts, leurs références et leur niveau de fiabilité. Le raisonnement
(vérification d'une affirmation, brief) est fait par le client, guidé par les
instructions ci-dessous et par les skills du plugin.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import yaml
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import alerts, auth, config, data, db, diff, ingest, search

INSTRUCTIONS = """Corpus vérifiable pour suivre la COP31 (Antalya, 9–20 nov. 2026). Outil indépendant et gratuit, sans lien
avec la CCNUCC ni avec une délégation.

RÈGLES
1. Rien de factuel sans un passage renvoyé par un outil. Rien trouvé → « non trouvé dans le corpus » ; jamais de mémoire.
2. Citer le texte exact entre guillemets + référence (cote, décision et §, page) + lien (lien_source, sinon lien_passage).
3. Donner le NIVEAU : 1 adopté ; 2 officiel non adopté (projet, NDC, soumission d'une Partie) ; 3 science, données ;
4 compte rendu ; 5 acteur (jamais une preuve, ne dément pas 1–3) ; R interne (ne pas recopier hors contributeurs).
4. Projet de texte : toujours sa version, sa date et « non adopté ».
5. Distinguer ce que dit le texte (1–2) de ce qu'en disent les acteurs (4–5). Brief ou analyse IA : le dire.
6. shall = obligation ; should = recommandation ; urges/encourages/invites = incitation ; notes = constat.
7. Requêtes en anglais onusien, 2–3 formulations si c'est maigre.
8. Court, l'essentiel d'abord, dans la langue de l'utilisateur (citer dans la langue d'origine).

OUTILS
- Corpus : rechercher → lire_passage ; document ; comparer_versions ; nouveautes ; donnees ; glossaire.
- Négociations : dossiers (textes annoncés) → dossier (état, textes, brief, positions, soumissions) ; positions ; noter.
- Positions écrites : soumissions (appels, auteurs, analyse) ; comparer_soumissions (et matrice) ; groupes.
- Événements : programme (où et quand) ; pavillons (contenu des événements, comptages).
- Alertes et ajout : mes_sujets ; suivre (mots-clés, dossier, arrêt) ; deposer_url ; etat_corpus.
Méthodes : point_negociation, position_pays, preparer_rencontre, plaidoyer, decouvrir_sujet, quoi_de_neuf."""

# Outils retirés ou renommés : ne doivent plus apparaître nulle part (test). textes_en_cours → dossiers(textes=…) ;
# suivre_dossier / arreter_suivi → suivre(dossier=… | arreter=…) ; analyser_soumission → soumissions(document=…) ;
# engagements / deposer : noms qui n'ont jamais existé (ancienne documentation).
RETIRED_TOOLS = ("textes_en_cours", "suivre_dossier", "arreter_suivi", "analyser_soumission", "engagements", "deposer")

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
        raise ValueError("Utilisateur non identifié : utiliser l'adresse personnelle du connecteur (/u/<jeton>/mcp).")
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
    """Recherche hybride (mots-clés + sens, multilingue) dans tout le corpus : droit onusien (Convention, Accord de
    Paris, décisions), documents de session et projets de texte, NDC, droit et positions de l'UE, science, comptes
    rendus, données ; documents internes réservés aux contributeurs (invités : pas d'accès).
    Formuler les termes onusiens en anglais (« global stocktake », « transitioning away from fossil fuels »),
    guillemets pour une expression exacte, 2–3 formulations si le premier résultat est maigre. Soumissions
    officielles : exclues par défaut (stock historique), voir types/partie/groupe ou l'outil `soumissions`.

    - requete : question ou mots-clés ; guillemets pour une expression exacte ; OR possible.
    - niveaux : ex. ["1","2"] pour ne garder que le droit et les documents officiels ; "R" n'existe pas
      comme filtre : utiliser acces="restreint" pour les documents internes.
    - types : traite, decision, conclusions_sb, ordre_du_jour, projet_texte, ndc, btr, rapport_synthese,
      loi_ue, proposition_ue, conclusions_conseil, strategie_nationale, rapport_scientifique, donnees,
      compte_rendu, communique, analyse, programme, soumission (soumission officielle à la CCNUCC),
      declaration (déclaration en plénière déposée sur le portail).
    - session : COP29, CMA6, COP31… ; organisation : UNFCCC, Council of the EU… ; partie : EU, FRA, CHN…
    - groupe : LDC, AOSIS, G77, EU, Umbrella… : documents (NDC, soumissions) de ses membres (liste sourcée, voir groupes)
    - depuis / jusqu_a : date ISO (AAAA-MM-JJ) sur la date de publication (ou de récupération).
    Renvoie des passages avec niveau, cote, paragraphe, page, extrait et liens. Utiliser lire_passage pour le texte complet.
    """
    user = _user(ctx)
    filters = {"levels": niveaux, "doc_types": types, "session": session, "org": organisation, "party": partie,
               "group": groupe,
               "since": depuis, "until": jusqu_a, "lang": langue, "access": acces}
    hits = search.search(db.get(), requete, {k: v for k, v in filters.items() if v}, user, limit=min(limite, 30))
    return {"requete": requete, "nb": len(hits), "resultats": hits,
            "rappel": "Citer le texte exact, la référence et le niveau. Rien de pertinent → le dire."}


@mcp.tool(annotations=READ)
def lire_passage(passage_id: int, avant: int = 1, apres: int = 2, ctx: Context | None = None) -> dict[str, Any]:
    """Texte exact d'un passage et de ses voisins (contexte), avec la référence complète et le lien vers l'original à la bonne page."""
    out = search.passage(db.get(), passage_id, _user(ctx), before=min(avant, 5), after=min(apres, 8))
    return out or {"erreur": "passage introuvable ou non autorisé"}


@mcp.tool(annotations=READ)
def document(reference: str, ctx: Context | None = None) -> dict[str, Any]:
    """Métadonnées d'un document et liste de ses versions. `reference` = identifiant numérique, cote
    officielle (ex. FCCC/PA/CMA/2023/16/Add.1, FCCC/CP/2026/L.3) ou URL."""
    conn = db.get()
    user = _user(ctx)
    row = search.find_doc(conn, reference, user)
    if not row:
        return {"erreur": f"document introuvable : {reference}"}
    out = search.document(conn, row["id"], user) or {}
    first = conn.execute("SELECT id, page, para, substr(text,1,400) AS t FROM passages WHERE doc_id=? ORDER BY seq LIMIT 3",
                         (row["id"],)).fetchall()
    out["debut"] = [{"passage_id": r["id"], "page": r["page"], "paragraphe": r["para"], "texte": r["t"]} for r in first]
    return out


@mcp.tool(annotations=READ)
def comparer_versions(document_recent: str, document_ancien: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Ce qui a changé entre deux versions d'un texte (projet de décision, conclusions…) : paragraphes
    ajoutés, supprimés, modifiés (diff mot à mot), évolution du nombre de crochets et d'options.
    Sans `document_ancien`, compare avec la version précédente de la même lignée."""
    conn = db.get()
    user = _user(ctx)
    new = search.find_doc(conn, document_recent, user)
    if not new:
        return {"erreur": f"document introuvable : {document_recent}"}
    if document_ancien:
        old = search.find_doc(conn, document_ancien, user)
    else:
        old = conn.execute(
            "SELECT * FROM documents WHERE lineage_key=? AND id<? ORDER BY id DESC LIMIT 1",
            (new["lineage_key"], new["id"]),
        ).fetchone()
        if old and old["access"] != "public" and not auth.can_restricted(user):
            old = None
    if not old:
        return {"erreur": "pas de version antérieure connue", "versions": search.versions(conn, new["lineage_key"], user)}
    out = diff.compare(conn, old["id"], new["id"])
    out["ancien"] = search.doc_public(old)
    out["nouveau"] = search.doc_public(new)
    return out


@mcp.tool(annotations=READ)
def nouveautes(depuis: str = "24h", niveaux: list[str] | None = None, types: list[str] | None = None,
               limite: int = 30, ctx: Context | None = None) -> dict[str, Any]:
    """Documents entrés dans le corpus récemment (veille en direct). `depuis` : « 6h », « 2j », « 30min »
    ou date ISO. Filtrable par niveaux et types (ex. types=["projet_texte","decision"])."""
    filters = {"added_since": _since(depuis), "levels": niveaux, "doc_types": types}
    docs = search.latest(db.get(), _user(ctx), {k: v for k, v in filters.items() if v}, limit=min(limite, 100))
    return {"depuis": filters["added_since"], "nb": len(docs), "documents": docs}


@mcp.tool(annotations=READ)
def donnees(pays: str, indicateur: str | None = None, debut: int | None = None, fin: int | None = None) -> dict[str, Any]:
    """Chiffres d'émissions pour vérifier une affirmation. `pays` : code ISO3 (FRA, CHN, USA, IND…) ou nom
    anglais (« European Union (27) », « World »). Indicateurs : co2, co2_including_luc, co2_per_capita,
    share_global_co2, cumulative_co2, share_global_cumulative_co2, total_ghg, total_ghg_excluding_lucf,
    ghg_per_capita, coal_co2, oil_co2, gas_co2, methane, population, gdp. Sans indicateur : sélection
    usuelle. Sans bornes : 1990, 2005, 2015 et les dernières années. Source : Our World in Data (niveau 3)."""
    return data.query(db.get(), pays, indicateur, debut, fin)


_GLOSSARY: list[dict[str, Any]] | None = None


@mcp.tool(annotations=READ)
def glossaire(terme: str | None = None) -> dict[str, Any]:
    """Définition d'un sigle ou d'une notion des négociations climat (NCQG, GGA, MWP, article 6, BTR…),
    avec le texte de référence. Sans terme : liste des entrées."""
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
    return {"resultats": found} if found else {"erreur": f"absent du glossaire : {terme}", "conseil": "utiliser rechercher"}


@mcp.tool(annotations=READ)
def programme(sujet: str | None = None, jour: str | None = None, types: list[str] | None = None,
              limite: int = 40, mes_sujets: bool = False, pavillons: bool = True,
              ctx: Context | None = None) -> dict[str, Any]:
    """Programme de la COP31 : OÙ et QUAND aller (un jour donné, des sujets donnés). Pour chercher dans le contenu
    des événements, compter (« combien de pavillons ont parlé de… ») ou voir la couverture : outil `pavillons`. Side events officiels (salle, horaire, organisateurs, thèmes),
    réunions officielles du calendrier CCNUCC (dialogues et ateliers prévus par les décisions, coordinations de
    groupes, événements spéciaux), résumé des négociations du jour, documents de programme déposés (Daily
    Programme, ordres du jour) ET événements des pavillons et hubs (Blue Zone et Green Zone, ≈5 000 sessions
    avec résumé et intervenants : clé `pavillons`).
    `jour` : « aujourd'hui », « demain », « 2026-11-14 » ou « 14 ». `sujet` : mots-clés séparés par des virgules,
    de préférence en anglais (« indigenous, LCIPP », « water », « youth, ACE, education »).
    `mes_sujets=true` : utilise les sujets suivis par l'utilisateur (alertes) et ses dossiers suivis, un bloc par
    sujet — pour « aujourd'hui, quels événements pour mes sujets ? ». `pavillons=false` : sans les pavillons.
    `types` : side_event, reunion, hors_site (événements hors site) ; par défaut side_event + reunion.
    Pavillons : correspondance « mots » (terme présent) ou « sens » (proximité sémantique : vérifier le résumé).
    Chaque side event porte son lien de vérification (SEORS, filtré sur le jour) : salles et horaires changent,
    toujours donner ce lien. Pour les questions transversales (comptages, par pavillon) : outil `pavillons`."""
    from . import agenda
    from . import pavillons as pav

    conn = db.get()
    user = _user(ctx)
    lim = max(1, min(limite, 100))
    if mes_sujets:
        if not user or not user.get("id"):
            return {"erreur": "mes_sujets demande d'être connecté ; sinon préciser `sujet`"}
        subjects = [(s["libelle"], s["requete"]) for s in alerts.list_subscriptions(conn, user["id"])]
        live, _, _ = _live()
        for slug in sorted(live.followed(conn, user["id"])):
            d = live.dossier(slug) or {}
            subjects.append((d.get("nom") or slug, d.get("requete") or d.get("nom_en") or slug))
        if not subjects:
            return {"erreur": "aucun sujet suivi : en ajouter avec `suivre`, ou préciser `sujet`"}
        day = agenda.resolve_day(jour)
        blocs = []
        for label, q in subjects[:8]:
            terms = ", ".join(t for t in re.split(r"[,;]| OR | AND ", q.replace('"', "")) if t.strip())
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
        out["note"] = "Aucun événement trouvé pour ce jour et ce sujet dans le programme relevé."
    return out


@mcp.tool(annotations=READ)
def pavillons(sujet: str | None = None, pavillon: str | None = None, jour: str | None = None,
              zone: str | None = None, mode: str = "hybride", limite: int = 80, evenement: str | None = None,
              couverture: bool = False) -> dict[str, Any]:
    """Recherche dans le CONTENU des programmes (comptages, couverture, qui a parlé de quoi). Pour « où et quand
    aller aujourd'hui » : outil `programme`. Programmes de TOUS les pavillons et side events de la COP31 (≈5 000 événements : titre, résumé,
    intervenants, thèmes, mots-clés), pour des questions transversales : « combien de pavillons ont parlé des
    forêts tropicales ? », « quels pavillons ont critiqué les solutions techniques (CCS, géo-ingénierie…) ? »,
    « que fait le pavillon de l'Indonésie sur le méthane ? », « qui a parlé de minerais critiques ? ».
    Résultat groupé par pavillon, avec le nombre d'événements par pavillon.
    `sujet` : termes séparés par des virgules (OU), en anglais de préférence, avec synonymes
    (« tropical forest, rainforest, Amazon, Congo Basin, REDD+, deforestation »). Mot entre guillemets = expression
    exacte ; `mot*` = préfixe. `mode` : hybride (mots + sens, défaut), mots (comptage strict), sens (idées
    proches : « critique du techno-solutionnisme »). `pavillon` : nom partiel (« Brazil », « WWF ») ; sans `sujet`,
    renvoie tout son programme. `jour` : « 14 », « 2026-11-14 », « aujourd'hui ». `zone` : blue, green, autre.
    `evenement` : identifiant renvoyé → résumé complet et fiche. `couverture=true` : liste des pavillons couverts
    (événements, résumés, intervenants connus).
    MÉTHODE pour compter : lancer 2–3 formulations, relire les résumés des correspondances « sens » avant de les
    compter, annoncer le nombre de pavillons ET d'événements, et rappeler les limites (programmes déclaratifs,
    résumés parfois absents, pavillons absents de la source). Citer le titre exact et le pavillon ; source de
    niveau 5 (programme publié par les organisateurs)."""
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
    """Soumissions officielles à la CCNUCC (portail submissions.unfccc.int, depuis 2015) : appels, qui a soumis
    quoi, et analyse d'une soumission. UN mode par appel, dans cet ordre de priorité : document, appel, groupe,
    dossier, auteur, sinon liste des appels (requete / theme / session / ouverts).
    - sans argument (ou requete / theme / session / ouverts) : liste des appels, avec nombre de soumissions ;
      requete = mots du titre, tous requis (« just transition », « adaptation indicators », « Article 6.8 ») ;
      session = COP31, SB64… ; ouverts=true : appels ouverts ou de la session en cours.
    - appel=<id> : détail de l'appel et toutes ses soumissions (auteur, Partie ou observateur, date, résumé IA s'il
      existe, doc_id).
    - auteur=« EU » / « UE » / « European Union », « LDC », « Brazil »… : soumissions de cet acteur, auteur ou
      co-auteur (nom exact après normalisation ; un nom inconnu renvoie les noms proches, jamais un autre pays).
    - dossier=<slug ou alias : GGA, finance, JTWP, art6…> : appels rattachés au dossier (les plus récents), qui a
      soumis (acteurs canoniques ; groupe quand une Partie soumet en son nom), alignements déjà calculés.
    - groupe=« LDC », « AOSIS », « G77 »… : soumissions du groupe en son nom ET de ses membres (liste sourcée),
      avec dossier= pour se limiter à un dossier.
    - document=<doc_id> : ANALYSE de la soumission (calculée une fois puis en cache) : résumé, points soulevés avec
      la position de l'auteur, la force du langage (1 constate, 2 suggère, 3 demande, 4 exige ; fort ≥ 3), le verbe
      et une citation vérifiée mot pour mot. Analyse par IA : le dire et citer les extraits.
    Niveau : soumission d'une Partie = 2 (position officielle écrite, non adoptée), observateur = 5.
    Pour « qui veut quoi » pendant la COP, voir aussi `positions` ; pour comparer deux acteurs : comparer_soumissions."""
    from . import soumissions as sm

    conn = db.get()
    limite = max(1, min(limite, 200))
    try:
        if document:
            row = search.find_doc(conn, document, _user(ctx))
            if not row:
                return {"erreur": f"document introuvable : {document}"}
            cached = sm.analysis(conn, row["id"])
            if cached and not cached.get("erreur"):
                return {"document": search.doc_public(row), **cached}
            _need_user(ctx)
            return {"document": search.doc_public(row), **sm.analyze(conn, row["id"])}
        if appel:
            return sm.call_detail(conn, appel, parties_only=parties_seulement) or {"erreur": f"appel {appel} inconnu"}
        if groupe:
            from . import groupes as grp

            gid = grp.group_id(groupe)
            if not gid:
                return {"erreur": f"groupe inconnu : {groupe}"}
            calls = set(sm.dossier_call_ids(conn, dossier)) if dossier else None
            own = []
            try:
                own = [x for x in sm._actor_items(conn, gid) if calls is None or x["call_id"] in calls]
            except sm.UnknownActor:
                pass
            mem, n = sm.group_member_items(conn, gid, calls)
            return {"groupe": grp.group_info(conn, gid),
                    "soumissions_du_groupe": [sm._item_public(conn, x, with_analysis=True) for x in own[:limite]],
                    "soumissions_des_membres": [{**sm._item_public(conn, x), "appel": x["call_id"]} for x in mem[:limite]],
                    "note": "Soumissions des membres : positions de chaque Partie, pas du groupe."}
        if dossier:
            from . import live

            slug = live.dossier_from_alias(dossier) or dossier
            if not live.dossier(slug):
                return {"erreur": f"dossier inconnu : {dossier}", "dossiers": [d["slug"] for d in live.dossiers()]}
            return {"dossier": slug, **sm.dossier_section(conn, slug, limit=min(limite, 25))}
        if auteur:
            return {"auteur": auteur, "soumissions": sm.by_entity(conn, auteur, limite)}
        return {"appels": sm.list_calls(conn, requete=requete, theme=theme, session=session, ouverts=ouverts,
                                        limite=limite), "etat": sm.stats(conn)}
    except Exception as exc:
        return {"erreur": str(exc)}


@mcp.tool(annotations=READ)
def comparer_soumissions(a: str | None = None, b: str | None = None, appel: int | None = None,
                         dossier: str | None = None, acteurs: list[str] | None = None, max_comparaisons: int = 6,
                         ctx: Context | None = None) -> dict[str, Any]:
    """Compare les positions écrites de deux acteurs (soumissions officielles), point par point : chaque point est
    qualifié accord / partiel / conflit / un seul côté, avec ce que dit chacun et la force de son langage ;
    l'indication d'ensemble (alignées, partiellement, non alignées, pas de position commune) est calculée par
    règles fixes à partir de ces qualifications (decompte.regle).
    - a, b : nom d'acteur (« EU », « LMDC », « Saudi Arabia », « AOSIS » ; un groupe qui a soumis en son nom
      compte ; un groupe qui n'a pas soumis : comparaison avec les soumissions de ses MEMBRES, présentée comme
      telle, avec la couverture) ou doc_id. On prend l'appel le plus récent auquel les deux ont répondu (ou `appel`, ou le plus récent
      des appels du `dossier` : GGA, finance, JTWP…) ; sans appel commun, on compare leurs dernières soumissions et
      on le signale.
    - b absent : compare la soumission de `a` avec les autres Parties du même appel (comparaisons en cache
      d'abord, quelques nouvelles par requête ; relancer pour la suite).
    - acteurs=["EU","LMDC","AOSIS",…] + dossier=… : matrice d'alignement LUE DANS LE CACHE (paires déjà comparées,
      paires « à calculer » avec leur appel commun) ; aucune comparaison lancée.
    Analyses et comparaisons sont calculées une fois puis gardées en cache (coût plafonné).
    Présenter comme une aide à l'analyse (pas un jugement), avec les citations et liens des deux documents."""
    from . import soumissions as sm

    conn = db.get()
    try:
        if acteurs:
            from . import live

            slug = live.dossier_from_alias(dossier or "") or dossier
            if not slug or not live.dossier(slug):
                return {"erreur": "acteurs=[…] demande dossier=… (slug ou alias)"}
            return sm.alignment_matrix(conn, slug, acteurs)
        if not a:
            return {"erreur": "préciser a (et b), ou acteurs=[…] avec dossier=…"}
        _need_user(ctx)
        if not b:
            return sm.compare_in_call(conn, sm.latest_doc(conn, a, appel), max_n=max(1, min(max_comparaisons, 10)))
        try:
            da, dbb, common = sm.resolve_pair(conn, a, b, appel, dossier=dossier)
        except sm.UnknownActor:
            from . import groupes as grp

            if not (grp.group_id(a) or grp.group_id(b)):
                raise
            # Groupe sans soumission propre : comparaison avec les soumissions de ses membres.
            return sm.compare_actors(conn, a, b, appel=appel, dossier=dossier)
        out = sm.compare(conn, da, dbb)
        call = conn.execute("SELECT * FROM sub_calls WHERE id=?", (common,)).fetchone() if common else None
        out["appel_commun"] = sm._call_public(call) if call else None
        if not common:
            out["attention"] = ("Pas d'appel commun : les deux soumissions répondent à des questions différentes ; "
                                "la comparaison est indicative.")
        return out
    except Exception as exc:
        return {"erreur": str(exc)}


@mcp.tool(annotations=READ)
def groupes(groupe: str | None = None, partie: str | None = None) -> dict[str, Any]:
    """Groupes de négociation : qui est membre de quoi, avec la SOURCE et la date de chaque liste.
    - groupe=« LDC » / « PMA », « AOSIS », « G77 », « EU », « Umbrella », « BASIC », « EIG », « LLDC », « SIDS » :
      membres (nom, ISO3, année d'adhésion si la source la donne).
    - partie=« China », « Brazil », « Tuvalu »… : groupes dont elle est membre.
    - sans argument : tous les groupes et l'état de leur liste.
    Statut : verifie (liste sourcée, nombre conforme) ; verifie_par_equivalence (AGN = groupe régional africain de
    l'ONU, Groupe arabe = Ligue des États arabes : le dire avec la phrase `equivalence`) ; ecart (à vérifier) ;
    non_verifie (LMDC, AILAC, ALBA, CfRN : pas de liste officielle — ne JAMAIS en déduire la composition, citer
    seulement le nombre annoncé par la source)."""
    from . import groupes as grp

    return grp.query(db.get(), groupe=groupe, partie=partie)


@mcp.tool(annotations=READ)
def mes_sujets(ctx: Context | None = None) -> dict[str, Any]:
    """Sujets suivis par l'utilisateur (alertes Telegram et tableau de bord) et dernières alertes."""
    user = _need_user(ctx)
    conn = db.get()
    rows = conn.execute(
        """SELECT n.created_at, n.doc_id, s.label FROM notifications n LEFT JOIN subscriptions s ON s.id=n.subscription_id
           WHERE n.user_id=? ORDER BY n.id DESC LIMIT 20""", (user["id"],)
    ).fetchall()
    live, _, _ = _live()
    follows = sorted(live.followed(conn, user["id"])) if user.get("id") else []
    return {"sujets": alerts.list_subscriptions(conn, user["id"]),
            "dossiers_suivis": [{"slug": s, "nom": (live.dossier(s) or {}).get("nom")} for s in follows],
            "dernieres_alertes": [dict(r) for r in rows]}


@mcp.tool(annotations=WRITE)
def suivre(mots_cles: str | None = None, dossier: str | None = None, arreter: int | None = None,
           ne_plus_suivre: bool = False, libelle: str | None = None, niveaux: list[str] | None = None,
           types: list[str] | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Alertes (Telegram + tableau de bord). Un seul des trois modes :
    - mots_cles=… : alerte sur chaque nouveau document correspondant ; expressions séparées par des virgules, en
      anglais ET en français (« fossil fuels, combustibles fossiles, transition away ») ; « * » + types = tout
      nouveau document de ces types (ex. types=["projet_texte"]).
    - dossier=… (slug ou alias : GGA, finance, ACE, art6…) : alerte à chaque nouveau texte du dossier et brief de
      7 h et 19 h ; ne_plus_suivre=true pour arrêter.
    - arreter=<id> : supprimer une alerte par mots-clés (identifiant donné par mes_sujets)."""
    user = _need_user(ctx)
    conn = db.get()
    modes = [x is not None and x != "" for x in (mots_cles, dossier, arreter)]
    if sum(modes) != 1:
        return {"erreur": "préciser exactement un de : mots_cles, dossier, arreter"}
    if arreter is not None:
        return {"ok": alerts.remove_subscription(conn, user["id"], arreter), "alerte_supprimee": arreter}
    if dossier:
        live, _, _ = _live()
        slug = live.dossier_from_alias(dossier) or (dossier if live.dossier(dossier) else None)
        if not slug:
            return {"erreur": "dossier inconnu : voir l'outil `dossiers` pour la liste et les alias"}
        live.follow(conn, user["id"], slug, not ne_plus_suivre)
        return {"ok": True, "dossier": slug, "suivi": not ne_plus_suivre}
    sid = alerts.add_subscription(conn, user["id"], libelle or mots_cles[:60], mots_cles, niveaux, types)
    return {"ok": True, "id": sid, "apercu_7_jours": alerts.preview(conn, mots_cles, niveaux, types, user, days=7, limit=5)}


@mcp.tool(annotations=WRITE)
def deposer_url(url: str, titre: str | None = None, niveau: str | None = None, type: str | None = None,
                organisation: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Ajouter au corpus (public) une page ou un PDF accessible en ligne : communiqué, déclaration,
    analyse… Préciser le niveau (2 officiel, 4 compte rendu, 5 position d'un acteur)."""
    from . import fetch

    user = _need_user(ctx)
    if not auth.can_contribute(user):
        return {"erreur": "reserved for contributors: guest accounts can search the corpus but not add documents / réservé aux contributeurs : les comptes invités consultent le corpus sans y ajouter de documents"}
    res = fetch.Client().get(url)
    if not res.ok or fetch.is_antibot(res):
        return {"erreur": f"inaccessible ({res.status}{', anti-bot' if res.ok else ''}) : déposer le fichier via le tableau de bord ou Telegram"}
    meta = {k: v for k, v in {"title": titre, "level": niveau, "doc_type": type, "org": organisation}.items() if v}
    conn = db.get()
    doc_id, new, status = ingest.ingest_bytes(conn, res.content, url=res.url, mime=res.mime, meta=meta,
                                              source_id="depot-mcp", uploaded_by=user.get("id") or None)
    alerts.run(conn)
    return {"doc_id": doc_id, "nouveau": new, "statut": status, "lien": f"{config.PUBLIC_BASE}/doc/{doc_id}"}


@mcp.tool(annotations=READ)
def etat_corpus() -> dict[str, Any]:
    """Volume du corpus par niveau, type et statut ; date du dernier ajout ; sources surveillées."""
    conn = db.get()
    out = search.stats(conn)
    out["sources_surveillees"] = [dict(r) for r in conn.execute(
        "SELECT source_id, last_run, last_result FROM source_state ORDER BY last_run DESC LIMIT 25")]
    return out


# --- veille en direct : dossiers, textes en cours, positions, notes ------------------------

def _live():
    from . import briefs, live, positions

    return live, positions, briefs


@mcp.tool(annotations=READ)
def dossiers(textes: bool = False, a_recuperer: bool = False, limite: int = 30,
             ctx: Context | None = None) -> dict[str, Any]:
    """Les dossiers de négociation de la COP31 (atténuation, GGA, finance, art. 6, transition juste…) avec leur
    état (selon l'index de Carbon Brief), le nombre de textes, de positions et de notes. Point d'entrée pour
    « où en est-on ? ». Utiliser ensuite `dossier(slug)`.
    textes=true : en plus, les derniers textes de négociation annoncés, tous dossiers (cote, titre, heure,
    crochets, options, récupéré ou non) ; a_recuperer=true : seulement ceux pas encore récupérés dans le corpus."""
    live, _, _ = _live()
    conn = db.get()
    rows = live.overview(conn, _user(ctx))
    out = {"dossiers": [{k: d[k] for k in ("slug", "nom", "alias", "enjeu", "etat", "nb_textes", "a_recuperer",
                                           "nb_positions", "nb_notes", "dernier")} for d in rows],
           "source_index": f"{live.CB_CREDIT} — {live.CB_TEXTS_PAGE} (à citer)"}
    if textes or a_recuperer:
        ts = live.texts(conn, None, pending_only=a_recuperer, limit=min(limite, 100))
        out["textes"] = [{**{k: t[k] for k in ("dossier", "symbol", "title", "body", "agenda_item", "published",
                                               "brackets", "options", "pdf_url")},
                          "lien": f"{config.PUBLIC_BASE}/doc/{t['doc_id']}" if t["doc_id"] else None} for t in ts]
    return out


@mcp.tool(annotations=READ)
def dossier(slug: str, ctx: Context | None = None) -> dict[str, Any]:
    """Tout un dossier : état, textes de négociation successifs (crochets, options, lien vers le document et
    le diff), dernier brief (sources numérotées), positions relevées pendant la COP (citation exacte et source),
    positions écrites et appels à soumissions liés (qui a soumis, alignements déjà calculés),
    positions de référence (Carbon Brief) et, pour les contributeurs, notes de terrain. `slug` ou alias (ex. GGA, finance, MWP).
    Index des textes et positions de référence : citer « selon le suivi de Carbon Brief ». Le brief est rédigé par IA :
    le présenter comme tel et s'appuyer sur ses sources numérotées."""
    live, positions, briefs = _live()
    conn = db.get()
    user = _user(ctx)
    slug = live.dossier_from_alias(slug) or slug
    d = live.dossier(slug)
    if not d:
        return {"erreur": "dossier inconnu", "dossiers": [x["slug"] for x in live.dossiers()]}
    pos = positions.for_dossier(conn, slug, limit=300)
    out = {
        "dossier": {k: d[k] for k in ("slug", "nom", "enjeu", "alias")},
        "etat": [x["status"] for x in live.status_by_dossier(conn).get(slug, [])],
        "textes": [{**{k: t[k] for k in ("symbol", "title", "body", "agenda_item", "published", "pages", "brackets",
                                         "options", "pdf_url", "note")},
                    "statut": "récupéré" if t["doc_id"] else "à récupérer",
                    "lien": f"{config.PUBLIC_BASE}/doc/{t['doc_id']}" if t["doc_id"] else None,
                    "diff": f"{config.PUBLIC_BASE}/diff/{t['doc_id']}" if t["doc_id"] else None}
                   for t in live.texts(conn, slug, limit=40)],
        "brief": briefs.latest(conn, slug),
        "positions_cop": [{k: p[k] for k in ("actor", "stance", "issue", "summary", "quote", "said_on",
                                             "source_label", "source_url")}
                          for p in pos if p["source_kind"] not in ("carbonbrief", "soumission")][:80],
        "positions_ecrites": [{k: p[k] for k in ("actor", "stance", "issue", "summary", "quote", "said_on", "source_url")}
                              for p in pos if p["source_kind"] == "soumission"][:60],
        "soumissions": _sub_section(conn, slug),
        "positions_reference": [{k: p[k] for k in ("actor", "stance", "issue", "source_url")}
                                for p in pos if p["source_kind"] == "carbonbrief"],
        "credit": f"Index et positions de référence : {live.CB_CREDIT} (citer). Positions COP : extraites de sources "
                  "publiques, citation vérifiée mot pour mot. Positions écrites : points des soumissions officielles "
                  "récentes des Parties (niveau 2, citation vérifiée), datées du dépôt. Brief : rédigé par IA, à vérifier sur les sources.",
        "page": f"{config.PUBLIC_BASE}/dossiers/{slug}",
    }
    if auth.can_restricted(user):
        out["notes_terrain_internes"] = live.notes(conn, slug, limit=30)
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
              limite: int = 80, ctx: Context | None = None) -> dict[str, Any]:
    """Qui veut quoi : positions d'un acteur (EU, G77 and China, LMDC, AOSIS, LDC, AGN, Arab Group, China,
    Saudi Arabia…) et/ou sur un dossier, avec attitude, enjeu, citation exacte, date et source. `depuis` :
    « 24h », « 3j » ou date ISO (positions relevées depuis)."""
    live, pos, _ = _live()
    conn = db.get()
    live.ensure_schema(conn)
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
                    "message": f"aucune position relevée pour « {acteur} » (nom canonique « {exact} »)"
                               + (f" ; noms proches : {', '.join(near)}" if near else "")}
        where.append("actor=?")
        params.append(exact)
    if dossier:
        where.append("dossier=?")
        params.append(live.dossier_from_alias(dossier) or dossier)
    if depuis:
        where.append("created_at>=?")
        params.append(_since(depuis))
    rows = conn.execute(
        f"""SELECT dossier, actor, stance, issue, summary, quote, said_on, source_kind, source_label, source_url
            FROM positions WHERE {' AND '.join(where)} ORDER BY COALESCE(said_on, created_at) DESC LIMIT ?""",
        (*params, min(limite, 300))).fetchall()
    return {"positions": [dict(r) for r in rows],
            "note": "source_kind=carbonbrief : positions de référence d'avant la COP (Carbon Brief, à citer) ; "
                    "extraction/bluesky : relevées pendant la COP, citation vérifiée dans la source ; "
                    "soumission : position écrite, point d'une soumission officielle récente à la CCNUCC (niveau 2), "
                    "datée du dépôt (said_on), citation vérifiée (détail : soumissions(document=…))."}


@mcp.tool(annotations=WRITE)
def noter(texte: str, dossier: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Enregistrer une note de terrain (interne, restreinte), rattachée à un dossier :
    ce qui a été dit en salle, par qui, sur quel paragraphe."""
    live, _, _ = _live()
    user = _need_user(ctx)
    if not auth.can_restricted(user):
        return {"erreur": "reserved for contributors / réservé aux contributeurs"}
    slug = live.dossier_from_alias(dossier) if dossier else None
    nid, slug = live.add_note(db.get(), user, texte, dossier_slug=slug, via="mcp")
    return {"ok": True, "note": nid, "dossier": slug, "page": f"{config.PUBLIC_BASE}/dossiers/{slug or ''}"}


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
        "fr": ["`dossier` (slug ou alias) : état, textes successifs, crochets et options, dernier brief, rubrique "
               "« soumissions » (appels liés, qui a soumis, alignements connus).",
               "`comparer_versions` sur la dernière version : paragraphes ajoutés, supprimés, options tranchées, crochets "
               "levés — citer les paragraphes avant/après.",
               "`positions(dossier=…, depuis=…)` : qui pousse quoi, qui bloque ; positions écrites : "
               "`comparer_soumissions(<acteur>, <acteur>, dossier=…)` (aide à l'analyse).",
               "`nouveautes(depuis=…)` et `rechercher` (termes onusiens en anglais) pour les comptes rendus (ENB, Carbon Brief)."],
        "en": ["`dossier` (slug or alias): status, successive texts, brackets and options, latest brief, “soumissions” "
               "section (linked calls, who submitted, known alignment).",
               "`comparer_versions` on the latest version: paragraphs added or deleted, options settled, brackets removed "
               "— quote paragraphs before/after.",
               "`positions(dossier=…, depuis=…)`: who pushes what, who blocks; written positions: "
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
