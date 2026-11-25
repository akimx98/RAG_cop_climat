# Notes de conception — index

À lire **avant une tâche M/L** (CLAUDE.md) : repérer les notes qui touchent les mêmes fichiers, et les issues
`suite` ouvertes (`gh issue list -l suite`). Modèle : [_modele.md](_modele.md). Méthode : résumée dans [CLAUDE.md](../../CLAUDE.md) (référence complète dans le dépôt privé voisin `mcp-edge`).

| Note | Statut | Fichiers concernés | Suites |
|---|---|---|---|
| [2026-11-22 — Plantage du worker et pannes silencieuses](2026-11-22-plantage-worker-et-surveillance.md) | livré (9e81030, déployé le 25/11) | `extract.py`, `ingest.py`, `worker.py`, `watchdog.py`, `monitor.py`, `mailin.py`, `server.py`, `mcp_server.py` (`deposer_url`), `deploy/docker-compose.yml` | #1 à #7 |
| [2026-11-25 — Passage au domaine opencop.org](2026-11-25-domaine-opencop.md) | phase A livrée (19c31ed, 25/11) ; phase B à faire | `deploy/edge/sites/cop31.caddy.template`, `scripts/install-edge-site.sh`, `scripts/ship.sh`, `deploy/.env.example`, `deploy/.env` (VPS) | #9 à #12 |
