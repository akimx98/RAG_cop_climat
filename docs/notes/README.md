# Notes de conception — index

À lire **avant une tâche M/L** (CLAUDE.md) : repérer les notes qui touchent les mêmes fichiers, et les issues
`suite` ouvertes (`gh issue list -l suite`). Modèle : [_modele.md](_modele.md). Méthode : résumée dans [CLAUDE.md](../../CLAUDE.md) (référence complète dans le dépôt privé voisin `mcp-edge`).

| Note | Statut | Fichiers concernés | Suites |
|---|---|---|---|
| [2026-11-22 — Plantage du worker et pannes silencieuses](2026-11-22-plantage-worker-et-surveillance.md) | livré (9e81030, déployé le 25/11) | `extract.py`, `ingest.py`, `worker.py`, `watchdog.py`, `monitor.py`, `mailin.py`, `server.py`, `mcp_server.py` (`deposer_url`), `deploy/docker-compose.yml` | #1 à #7 |
| [2026-11-25 — Passage au domaine opencop.org](2026-11-25-domaine-opencop.md) | phase A livrée (19c31ed, 25/11) ; phase B à faire | `deploy/edge/sites/cop31.caddy.template`, `scripts/install-edge-site.sh`, `scripts/ship.sh`, `deploy/.env.example`, `deploy/.env` (VPS) | #9 à #12 |
| [2026-11-25 — Indexation : version française, llms.txt, image d'aperçu](2026-11-25-indexation-langues.md) | livré (9326e27, 25/11) | `templates/base.html`, `web.py` (`ROBOTS`, `sitemap`, `llms`, `NOT_INDEXED`), `static/og-opencop.*`, `tests/test_indexation.py` | #13 |
| [2026-11-25 — Lenteur du site : pages lourdes dans la boucle d'événements](2026-11-25-lenteur-du-site.md) | livré (92cfbd2, 26/11) | `web.py` (décorateurs, pages `def`), `web_live.py` (cache des dossiers), `search.py` (`stats`), `embed.py` (`search`), `server.py` | #14–#19 |
| [2026-11-26 — Connexion SQLite coincée après un « database is locked »](2026-11-26-connexion-sqlite-coincee.md) | livré (ef77254, 27/11) | `db.py` (`connect`) ; relus : `worker.py`, `live_worker.py`, `watchdog.py`, `monitor.py`, `ingest.py`, `mailin.py` | #20–#24 |
| [2026-11-27 — Mémoire du site : chargement des vecteurs par blocs](2026-11-27-memoire-vecteurs.md) | livré (49de307, 27/11) | `embed.py` (`_refresh`, `_snapshot`, `search`), `tests/test_vecteurs.py` | — (#17 close) |
| [2026-11-27 — Outils MCP et pages à formulaire hors de la boucle](2026-11-27-hors-de-la-boucle.md) | livré (5ffc0cd, 27/11) | `mcp_server.py` (`_tool`), `demo.py`, `web.py` (`body_first`), `web_live.py`, `auth.py` (`_login_lock`, `_failures_lock`) | #25, #26, #27 |
| [2026-11-27 — Surveillance robuste : alertes sous verrou, WAL, « Rétabli » à tort, lien de connexion](2026-11-27-surveillance-robuste.md) | livré (8cae8cb, 27/11) | `monitor.py` (`_fallback`), `watchdog.py` (contrôle `wal`, ordre état/Rétabli), `db.py` (`journal_size_limit`), `auth.py` (`by_login_link`) | #28, #29 |
| [2026-11-27 — Sources refusées : anti-bot et articles payants ; code mort du dépôt par courrier](2026-11-27-sources-refusees.md) | livré (1daa3e3, 27/11) | `ingest.py` (`REFUSED`, `fetch_one`, boucle ODS), `worker.py` (`_due`), `mailin.py`, `db.py` (`_migrate`) | #30 |
| [2026-11-27 — Recherche : partie sémantique en parallèle, encodage en cache](2026-11-27-recherche-parallele.md) | livré (043d6f9, 27/11) | `search.py` (`_SEM_POOL`, `_semantic`), `embed.py` (`_encode_query`) | — |
| [2026-11-27 — Pages dossier : frises plus rapides et préchauffage](2026-11-27-dossiers.md) | livré (537fc53, 27/11) | `formulations.py` (`occurrences`, `_plain`, `_raw_pos`, `_doc`), `structure.py` (`parse`), `web_live.py` (`warm_forever`), `server.py` | — |
| [2026-11-27 — Codes de connexion : réseaux en empreinte](2026-11-27-reseaux-en-empreinte.md) | livré (cc0fbbe, 27/11) | `auth.py` (`_net`, `_log`, `hash_stored_networks`), `db.py` (`_migrate`) | — |
| [2026-11-27 — Revue d'architecture : consolider après la COP31](2026-11-27-revue-architecture.md) | validé ; phases 0 à 5 à faire | `worker.py`, `live_worker.py`, `alerts.py`, `watchdog.py`, `ingest.py`, `live.py`, `web_live.py`, `usage.py`, `db.py`, catalogues | une issue par phase |
