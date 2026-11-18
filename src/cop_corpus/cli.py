"""Ligne de commande `cop`."""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from pathlib import Path

from . import auth, catalog, config, db, embed, ingest, search


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_fetch(args) -> None:
    sources = catalog.load()
    if args.source:
        wanted = set(args.source)
        sources = [s for s in sources if s.id in wanted or any(s.id.startswith(w) or s.file.startswith(w) for w in wanted)]
    if args.watched:
        sources = [s for s in sources if s.watch_minutes > 0]
    if args.type:
        sources = [s for s in sources if s.type == args.type]
    print(f"{len(sources)} sources", file=sys.stderr)
    res = ingest.run_many(sources, workers=args.workers, recheck=args.recheck,
                          progress=lambda m: print(m, file=sys.stderr, flush=True))
    total: dict[str, int] = {}
    for counts in res.values():
        for k, v in counts.items():
            if isinstance(v, int):
                total[k] = total.get(k, 0) + v
    _print({"total": total})


def cmd_build(args) -> None:
    conn = db.get()
    _print(ingest.reindex_pending(conn))
    if args.all:
        ids = [r["id"] for r in conn.execute("SELECT id FROM documents WHERE status IN ('indexed','empty','error')")]
        counts: dict[str, int] = {}
        for i in ids:
            st = ingest.index_document(conn, i)
            counts[st] = counts.get(st, 0) + 1
        _print(counts)


def cmd_embed(args) -> None:
    conn = db.get()
    n = embed.embed_pending(conn, limit=args.limit, progress=lambda m: print(m, file=sys.stderr, flush=True))
    print(f"{n} passages encodés")


def cmd_search(args) -> None:
    conn = db.get()
    filters = {}
    if args.level:
        filters["levels"] = args.level
    admin = {"can_restricted": 1, "active": 1}
    for hit in search.search(conn, args.query, filters, admin, limit=args.limit, semantic=not args.lexical):
        print(f"[{hit.get('niveau')}] {hit.get('titre')} — {hit.get('cote', '')} {hit.get('paragraphe') or ''} p.{hit.get('page')}")
        print(f"    {hit['extrait'][:300]}")
        print(f"    {hit.get('url_source') or hit['lien']}  (passage {hit['passage_id']})")


def cmd_stats(args) -> None:
    _print(search.stats(db.get()))


def cmd_log(args) -> None:
    conn = db.get()
    rows = conn.execute(
        "SELECT ts, source_id, outcome, url, detail FROM fetch_log WHERE outcome LIKE ? ORDER BY id DESC LIMIT ?",
        (args.outcome or "%", args.limit),
    ).fetchall()
    for r in rows:
        print(f"{r['ts']} {r['outcome']:7} {r['source_id']} {r['url']} {r['detail']}")


def cmd_sources(args) -> None:
    conn = db.get()
    state = {r["source_id"]: r for r in conn.execute("SELECT * FROM source_state")}
    for s in catalog.load():
        st = state.get(s.id)
        print(f"{s.id:45} {s.type:5} watch={s.watch_minutes:<3} {st['last_run'] if st else '-':20} {st['last_result'] if st else ''}")


def cmd_user_add(args) -> None:
    conn = db.get()
    uid, token = auth.create_user(conn, args.email, args.name or "", admin=args.admin, restricted=not args.public_only,
                                 verified=True)
    user = auth.by_id(conn, uid)
    _print({
        "id": uid,
        "lien_premiere_connexion": auth.login_link(user),
        "connecteur_mcp": f"{config.PUBLIC_BASE}/u/{token}/mcp",
        "code_telegram": user["telegram_code"],
        "attention": "Lien valable 24 h pour choisir un mot de passe. Transmettre par un canal sûr.",
    })


def cmd_user_reset(args) -> None:
    conn = db.get()
    user = auth.by_email(conn, args.email)
    if not user:
        sys.exit("utilisateur inconnu")
    token = auth.reset_token(conn, user["id"])
    _print({"lien_connexion": f"{config.PUBLIC_BASE}/login/{token}", "connecteur_mcp": f"{config.PUBLIC_BASE}/u/{token}/mcp"})


def cmd_user_link(args) -> None:
    """Mot de passe oublié : lien de connexion 24 h, sans révoquer le connecteur MCP."""
    conn = db.get()
    user = auth.by_email(conn, args.email)
    if not user:
        sys.exit("utilisateur inconnu")
    _print({"lien_connexion_24h": auth.login_link(user)})


def cmd_user_list(args) -> None:
    conn = db.get()
    for r in conn.execute("SELECT id,email,name,is_admin,can_restricted,telegram_chat_id IS NOT NULL tg,active FROM users"):
        print(dict(r))


def cmd_ingest_file(args) -> None:
    conn = db.get()
    for p in args.paths:
        path = Path(p)
        mime = mimetypes.guess_type(path.name)[0] or ""
        meta = {"title": args.title or path.stem, "level": args.level, "doc_type": args.doc_type, "org": args.org}
        doc_id, new, status = ingest.ingest_bytes(
            conn, path.read_bytes(), url=None, mime=mime, meta={k: v for k, v in meta.items() if v},
            source_id="depot-manuel", access="restreint" if args.restricted else "public", filename=path.name,
            note=args.note,
        )
        print(f"{path.name}: doc {doc_id} {'nouveau' if new else 'déjà présent'} {status}")
    from . import alerts
    alerts.run(conn)


def cmd_load_data(args) -> None:
    from . import data

    _print(data.load_all(db.get()))


def cmd_serve(args) -> None:
    from . import server

    server.run(host=args.host, port=args.port, with_worker=args.with_worker)


def cmd_mail_server(args) -> None:
    from . import mailin

    mailin.serve(host=args.host, port=args.port)


def cmd_mail_allow(args) -> None:
    from . import mailin

    conn = db.get()
    for s in args.senders:
        mailin.allow_sender(conn, s)
    print("\n".join(mailin.allowed_senders(conn)))


def cmd_worker(args) -> None:
    from . import worker

    worker.run_forever(bot=not args.no_bot, watch=not args.no_watch)


def cmd_programme_sync(args) -> None:
    from . import agenda, fetch

    _print(agenda.sync_planner(db.get(), fetch.Client()))


def cmd_programme_seors(args) -> None:
    from . import agenda

    text = Path(args.path).read_text(encoding="utf-8")
    payload = json.loads(text) if text.lstrip().startswith("{") else agenda.parse_seors_tsv(text)
    _print(agenda.import_seors(db.get(), payload))


def cmd_programme_unfccc(args) -> None:
    from . import agenda

    data = json.loads(Path(args.path).read_text(encoding="utf-8"))
    conn = db.get()
    _print(agenda.import_platform(conn, data) if "sessions" in data else agenda.import_unfccc_schedule(conn, data))


def cmd_programme(args) -> None:
    from . import agenda

    _print(agenda.query(db.get(), jour=args.jour, sujet=args.sujet, types=args.types, limit=args.limit))


def cmd_pavillons_sync(args) -> None:
    from . import fetch, pavillons

    _print(pavillons.sync(db.get(), fetch.Client()))


def cmd_pavillons(args) -> None:
    from . import pavillons

    conn = db.get()
    if args.couverture:
        _print(pavillons.coverage(conn))
    elif args.export:
        print(f"{pavillons.export(conn, Path(args.export))} événements → {args.export}")
    else:
        _print(pavillons.query(conn, sujet=args.sujet, pavillon=args.pavillon, jour=args.jour, zone=args.zone,
                               mode=args.mode, limite=args.limit))


def cmd_soumissions(args) -> None:
    from . import fetch, soumissions as sm

    conn = db.get()
    if args.action == "index":
        _print(sm.sync_index(conn, fetch.Client()))
    elif args.action == "telecharger":
        _print(sm.download_pending(conn, fetch.Client(), limit=args.limit, since=args.depuis,
                                   parties_only=args.parties, call_id=args.appel))
        if args.embed:
            from . import embed

            print(f"{embed.embed_pending(conn)} passages vectorisés")
    elif args.action == "analyser":
        _print(sm.analyze(conn, args.doc, force=args.force) if args.doc else {"analysees": sm.analyse_tick(conn, args.limit)})
    elif args.action == "comparer":
        a, b, call = sm.resolve_pair(conn, args.a, args.b, args.appel, dossier=(args.dossier or [None])[0])
        _print({"appel_commun": call, **sm.compare(conn, a, b, force=args.force)})
    elif args.action == "appels":
        _print(sm.list_calls(conn, requete=args.requete, session=args.session, ouverts=args.ouverts, limite=args.limit))
    elif args.action == "appel":
        _print(sm.call_detail(conn, args.appel))
    elif args.action == "rattacher":
        if not args.appel:
            raise SystemExit("--appel requis ; --dossier slug (répétable), aucun = « aucun dossier »")
        _print({"appel": args.appel, "dossiers": sm.set_manual(conn, args.appel, args.dossier or [])})
    elif args.action == "dossiers":
        if args.dossier:
            _print(sm.dossier_section(conn, args.dossier[0], limit=args.limit))
        else:
            _print(sm.map_calls(conn))
    else:
        _print(sm.stats(conn))


def cmd_groupes(args) -> None:
    from . import fetch, groupes

    conn = db.get()
    if args.sync:
        _print(groupes.sync(conn, fetch.Client()))
    else:
        _print(groupes.query(conn, groupe=args.groupe, partie=args.partie))


def cmd_lettres_relire(args) -> None:
    from . import mailin

    _print(mailin.reread_newsletters(db.get(), since=args.depuis, dry_run=args.essai))


def cmd_enb(args) -> None:
    from . import enb

    conn = db.get()
    if args.action == "annoter":
        _print({"annotes": enb.annotate_all(conn)})
    else:
        _print(enb.coverage(conn, since=args.depuis or "2026-11-01"))


def cmd_reextraire(args) -> None:
    """Réextrait des documents déjà au corpus (même identifiant) : colonnes des PDF, encodage, lettres espacées."""
    conn = db.get()
    where, params = ["status IN ('indexed','empty','error')"], []
    if args.source:
        where.append("source_id LIKE ?")
        params.append(args.source)
    if args.org:
        where.append("org LIKE ?")
        params.append(f"%{args.org}%")
    if args.url:
        where.append("url LIKE ?")
        params.append(f"%{args.url}%")
    if args.doc:
        where.append(f"id IN ({','.join('?' * len(args.doc))})")
        params += args.doc
    if len(where) == 1:
        sys.exit("préciser --source, --org, --url ou --doc")
    ids = [r["id"] for r in conn.execute(f"SELECT id FROM documents WHERE {' AND '.join(where)} ORDER BY id", params)]
    if args.essai:
        _print({"documents": len(ids), "ids": ids[:50]})
        return
    counts: dict[str, int] = {}
    has_log = conn.execute("SELECT 1 FROM sqlite_master WHERE name='extraction_log'").fetchone()
    for i in ids:
        st = ingest.index_document(conn, i)
        counts[st] = counts.get(st, 0) + 1
        if has_log:
            conn.execute("DELETE FROM extraction_log WHERE doc_id=?", (i,))  # positions réextraites au passage suivant
    conn.commit()
    from . import enb

    counts["enb_annotes"] = enb.annotate_all(conn)
    _print(counts)


def cmd_recuperer(args) -> None:
    """Retélécharge des documents entrés vides ou tronqués (même identifiant)."""
    from . import fetch

    conn = db.get()
    where, params = [], []
    if args.url:
        where.append("url LIKE ?")
        params.append(f"%{args.url}%")
    if args.doc:
        where.append(f"id IN ({','.join('?' * len(args.doc))})")
        params += args.doc
    if not where:
        sys.exit("préciser --url ou --doc")
    if args.vides:
        where.append("(n_passages IS NULL OR n_passages<=2)")
    ids = [r["id"] for r in conn.execute(f"SELECT id FROM documents WHERE {' AND '.join(where)} ORDER BY id", params)]
    client = fetch.Client()
    counts: dict[str, int] = {}
    for i in ids:
        st = ingest.refetch_in_place(conn, client, i)
        counts[st] = counts.get(st, 0) + 1
    _print(counts)


def cmd_doublons(args) -> None:
    from . import doublons

    conn = db.get()
    if args.calculer:
        _print(doublons.backfill(conn, limit=args.limit, progress=lambda m: print(m, file=sys.stderr, flush=True)))
    _print(doublons.listing(conn, limit=args.limit or 100))


def cmd_mcp_stdio(args) -> None:
    from . import mcp_server

    mcp_server.mcp.run()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="cop", description="Corpus COP31 : collecte, index, veille, alertes")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("fetch", help="collecter les sources du catalogue")
    s.add_argument("--source", nargs="*", help="ids ou préfixes de fichier (ex. unfccc_, eu_)")
    s.add_argument("--watched", action="store_true", help="seulement les sources surveillées")
    s.add_argument("--type", choices=["doc", "crawl", "rss"])
    s.add_argument("--workers", type=int, default=6)
    s.add_argument("--recheck", action="store_true", help="re-télécharger les URL déjà connues")
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("build", help="indexer les documents en attente")
    s.add_argument("--all", action="store_true", help="ré-indexer tout")
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("embed", help="calculer les embeddings manquants")
    s.add_argument("--limit", type=int)
    s.set_defaults(func=cmd_embed)

    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--level", nargs="*")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--lexical", action="store_true")
    s.set_defaults(func=cmd_search)

    sub.add_parser("stats").set_defaults(func=cmd_stats)
    sub.add_parser("sources").set_defaults(func=cmd_sources)
    s = sub.add_parser("log")
    s.add_argument("--outcome")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("user-add")
    s.add_argument("email")
    s.add_argument("--name")
    s.add_argument("--admin", action="store_true")
    s.add_argument("--public-only", action="store_true", help="pas d'accès aux documents restreints")
    s.set_defaults(func=cmd_user_add)
    s = sub.add_parser("user-reset")
    s.add_argument("email")
    s.set_defaults(func=cmd_user_reset)
    s = sub.add_parser("user-link", help="lien de connexion 24 h (mot de passe oublié)")
    s.add_argument("email")
    s.set_defaults(func=cmd_user_link)
    sub.add_parser("user-list").set_defaults(func=cmd_user_list)

    s = sub.add_parser("ingest-file", help="déposer des fichiers locaux")
    s.add_argument("paths", nargs="+")
    s.add_argument("--title")
    s.add_argument("--level")
    s.add_argument("--doc-type")
    s.add_argument("--org")
    s.add_argument("--note")
    s.add_argument("--public", dest="restricted", action="store_false")
    s.set_defaults(func=cmd_ingest_file, restricted=True)

    sub.add_parser("load-data", help="charger les jeux de données chiffrées (émissions)").set_defaults(func=cmd_load_data)

    s = sub.add_parser("serve", help="tableau de bord web + MCP HTTP")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--with-worker", action="store_true")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("mail-server", help="réception SMTP (veille@, depot-…@)")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=2525)
    s.set_defaults(func=cmd_mail_server)
    s = sub.add_parser("mail-allow", help="autoriser des expéditeurs (adresse ou domaine) sur veille@")
    s.add_argument("senders", nargs="*")
    s.set_defaults(func=cmd_mail_allow)
    s = sub.add_parser("worker", help="veilleur + alertes + bot Telegram")
    s.add_argument("--no-bot", action="store_true")
    s.add_argument("--no-watch", action="store_true")
    s.set_defaults(func=cmd_worker)

    sub.add_parser("programme-sync", help="relever le planner (side events, réunions officielles)").set_defaults(
        func=cmd_programme_sync)
    s = sub.add_parser("programme-seors", help="importer un relevé SEORS fait dans un navigateur (JSON)")
    s.add_argument("path")
    s.set_defaults(func=cmd_programme_seors)
    s = sub.add_parser("programme-unfccc", help="importer le calendrier officiel (JSON de unfccc.int/cop31/schedule) ou les séances de la plateforme COP31")
    s.add_argument("path")
    s.set_defaults(func=cmd_programme_unfccc)
    s = sub.add_parser("programme", help="programme d'un jour / d'un sujet")
    s.add_argument("--jour")
    s.add_argument("--sujet")
    s.add_argument("--types", nargs="*")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(func=cmd_programme)

    sub.add_parser("pavillons-sync", help="relever les programmes des pavillons (cop31.tr) et side events").set_defaults(
        func=cmd_pavillons_sync)
    s = sub.add_parser("pavillons", help="chercher dans les programmes des pavillons / exporter")
    s.add_argument("--sujet")
    s.add_argument("--pavillon")
    s.add_argument("--jour")
    s.add_argument("--zone", choices=["blue", "green", "autre"])
    s.add_argument("--mode", default="hybride", choices=["hybride", "mots", "sens"])
    s.add_argument("--limit", type=int, default=80)
    s.add_argument("--couverture", action="store_true", help="événements, résumés et intervenants par pavillon")
    s.add_argument("--export", help="fichier .csv ou .jsonl")
    s.set_defaults(func=cmd_pavillons)

    s = sub.add_parser("soumissions", help="soumissions officielles CCNUCC (submissions.unfccc.int)")
    s.add_argument("action", choices=["etat", "index", "telecharger", "analyser", "comparer", "appels", "appel",
                                      "rattacher", "dossiers"])
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--depuis", help="AAAA-MM-JJ (date de dépôt)")
    s.add_argument("--parties", action="store_true", help="seulement les soumissions de Parties")
    s.add_argument("--appel", type=int)
    s.add_argument("--doc", type=int)
    s.add_argument("--a")
    s.add_argument("--b")
    s.add_argument("--requete")
    s.add_argument("--session")
    s.add_argument("--ouverts", action="store_true")
    s.add_argument("--force", action="store_true")
    s.add_argument("--embed", action="store_true", help="vectoriser après téléchargement")
    s.add_argument("--dossier", action="append", help="slug ou alias de dossier (rattacher : répétable)")
    s.set_defaults(func=cmd_soumissions)

    s = sub.add_parser("groupes", help="groupes de négociation : membres sourcés")
    s.add_argument("--sync", action="store_true", help="relever les listes de membres")
    s.add_argument("--groupe")
    s.add_argument("--partie")
    s.set_defaults(func=cmd_groupes)

    s = sub.add_parser("lettres-relire", help="relire les lettres reçues sur veille@ (texte complet, date, un document par lettre)")
    s.add_argument("--depuis", help="AAAA-MM-JJ (date de réception)")
    s.add_argument("--essai", action="store_true", help="compter sans rien modifier")
    s.set_defaults(func=cmd_lettres_relire)
    s = sub.add_parser("enb", help="Earth Negotiations Bulletin : en-têtes des PDF, couverture des lettres")
    s.add_argument("action", choices=["annoter", "couverture"])
    s.add_argument("--depuis")
    s.set_defaults(func=cmd_enb)
    s = sub.add_parser("reextraire", help="réextraire des documents du corpus (même identifiant)")
    s.add_argument("--source", help="identifiant de source (motif SQL LIKE, ex. twn-%%)")
    s.add_argument("--org", help="organisation (contient)")
    s.add_argument("--url", help="adresse (contient)")
    s.add_argument("--doc", type=int, action="append")
    s.add_argument("--essai", action="store_true")
    s.set_defaults(func=cmd_reextraire)
    s = sub.add_parser("recuperer", help="retélécharger des documents entrés vides ou tronqués (même identifiant)")
    s.add_argument("--url", help="adresse (contient), ex. presscorner/detail")
    s.add_argument("--doc", type=int, action="append")
    s.add_argument("--vides", action="store_true", help="seulement les documents de 2 passages ou moins")
    s.set_defaults(func=cmd_recuperer)
    s = sub.add_parser("doublons", help="doublons de contenu (même texte, deux fichiers)")
    s.add_argument("--calculer", action="store_true", help="empreintes manquantes puis recherche des doublons")
    s.add_argument("--limit", type=int)
    s.set_defaults(func=cmd_doublons)

    sub.add_parser("mcp-stdio", help="serveur MCP en stdio (usage local)").set_defaults(func=cmd_mcp_stdio)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
