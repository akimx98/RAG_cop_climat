# CLAUDE.md — OpenCOP (ex-COP Tracker ; édition COP31)

Corpus vérifiable et veille des négociations climat. Projet **indépendant**, open source, sans lien avec la CCNUCC
ni avec une délégation (ne jamais le présenter comme l'outil d'une délégation). En production
(https://opencop.org ; l'ancienne adresse cop31.akimx.fr reste servie) : chaque erreur touche de vrais utilisateurs. La COP31 s'est tenue du 9 au 20/11/2026.

À lire selon le besoin :
- [docs/architecture.md](docs/architecture.md) : référence (données, outils MCP, tâches, réglages, confidentialité, coûts ;
  §10 : ce qu'il faut mettre à jour quand on ajoute une page ou qu'on change la présentation, le nom, le domaine).
- [PITFALLS.md](PITFALLS.md) : pièges déjà payés. **À relire avant toute conception M/L et à passer sur le diff
  avant toute revue.**
- [README.md](README.md) : commandes, exploitation ; [scripts/ship.sh](scripts/ship.sh) : mise en production.
- [docs/notes/](docs/notes/) : notes de conception (une par changement M/L ou incident).

## Règles intangibles

- **Confidentialité.** Documents `restreint` : jamais vers l'API d'embeddings ni vers un modèle de langage ;
  jamais de titre ni d'extrait dans Telegram (non chiffré de bout en bout). Alertes d'erreur : jamais de question
  d'utilisateur ni de texte de document. Pas d'adresse IP en clair, pas de secret dans les journaux ni les URL.
- **Anti-bot jamais contourné** (pas d'en-tête de navigateur usurpé, pas de navigateur sans tête) : ODS, Cellar ou
  dépôt manuel.
- **La base du VPS fait foi** (comptes, abonnements, documents). Ne jamais l'écraser depuis le Mac ; ne jamais
  lancer `cop worker` en local avec le jeton Telegram de production (un seul lecteur par bot).
- **Réglages** : variables d'environnement lues seulement dans `config.py` et listées dans architecture.md §7
  (testé). Outils MCP : registre = tableau de la doc (testé).
- **Production** : lecture seule (requêtes, journaux) sans demander ; toute écriture (base, fichiers, conteneurs)
  et tout déploiement après accord explicite. Déployer uniquement par `scripts/ship.sh` (commit, tests, contrôles,
  retour arrière), jamais par rsync à la main.

## Méthode de travail

Méthode **canonique**, commune aux services du VPS : `../mcp-edge/DEVELOPING.md` (dépôt privé `mcp-edge`, à
côté de celui-ci sur le poste de développement). L'essentiel, suffisant sans ce dépôt :

- **Trier** : S (un module, < ~50 lignes, rien de sensible) → coder, tester ; **M/L** (plusieurs modules, ou un
  sujet sensible : processus, fils, signaux, Docker, mémoire ; base et `meta` ; authentification, confidentialité ;
  sorties Telegram, e-mail, MCP ; boucle d'événements ; collecte et indexation ; coûts LLM ; déploiement) → note de conception
  `docs/notes/AAAA-MM-JJ-sujet.md` d'abord (modèle [docs/notes/_modele.md](docs/notes/_modele.md) : faits
  **mesurés**, modes de panne, critères d'acceptation, hors périmètre) ; **incident** → mitigation manuelle
  réversible d'abord (passer un document en erreur, couper un réglage, `ship.sh --rollback`), puis note.
- **Revue** M/L : auto-contrôle avec [PITFALLS.md](PITFALLS.md) et les pièges communs
  (`../mcp-edge/PITFALLS-MCP-RAG.md`), puis agent `reviewer` (Sonnet, effort moyen ;
  [.claude/agents/reviewer.md](.claude/agents/reviewer.md)), neuf à chaque tour, contre la note ; deux tours au plus ; un bloquant
  au 2e tour = retour à la note, pas de rustine.
- **Avant de toucher** à Docker, au compose, à la mémoire ou au déploiement : règles du VPS partagé
  `../mcp-edge/CONVENTIONS.md`.
- **Avant une M/L** : lire [docs/notes/README.md](docs/notes/README.md) et les issues `suite` ouvertes
  (`gh issue list -l suite`) qui touchent les mêmes fichiers.
- **Livrer** : tests, commit en français, `scripts/ship.sh`, vérifications de la note ; dire ce qui n'est pas
  vérifié.
- **Après livraison** : promouvoir ce qui dure (pièges → PITFALLS, règles, faits → architecture), une issue `suite`
  par suite, note « livré » dans l'index (méthode canonique §6).

## Production (repères)

VPS et chemins : voir `scripts/ship.sh`. Conteneurs `cop31-app-1` (site + MCP), `cop31-worker-1` (veille,
alertes, bot), `cop31-mail-1` (port 25) ; plafonds mémoire dans `deploy/docker-compose.yml` ; une base SQLite
partagée (`data/`, WAL).
Requête en lecture : `ssh … 'cd RAG_cop_climat && sudo docker compose -f deploy/docker-compose.yml exec -T app
python -'` avec un script sur l'entrée standard. Redémarrages et codes de sortie : `sudo journalctl -u docker |
grep exitCode` (pas `docker inspect`). Reconstruire le worker tue les `exec` en cours dans ce conteneur.

## Style

Réponses et documentation en français ; code commenté en français, noms courts ; pas de fonctionnalité au-delà de
la demande ; dire ce qui n'est pas fait ou pas vérifié.
