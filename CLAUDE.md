# CLAUDE.md — COP Tracker (COP31)

Corpus vérifiable et veille des négociations climat. Projet **indépendant**, open source, sans lien avec la CCNUCC
ni avec une délégation (ne jamais le présenter comme l'outil d'une délégation). En production pendant la COP :
chaque erreur touche de vrais utilisateurs.

À lire selon le besoin :
- [docs/architecture.md](docs/architecture.md) : référence (données, outils MCP, tâches, réglages, confidentialité, coûts).
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

### 1. Trier la demande

| Taille | Critère | Démarche |
|---|---|---|
| **S** | un module, < ~50 lignes, aucun des sujets sensibles ci-dessous | coder, tester, livrer |
| **M/L** | plusieurs modules, ou un sujet sensible | **note de conception d'abord** (§2), puis code, revue (§4) |
| **Incident** | la prod est dégradée | **§3 d'abord** : arrêter l'hémorragie, puis traiter comme M/L |

Sujets sensibles (toujours M/L, même pour trois lignes) : processus, fils, signaux, Docker/compose, mémoire ;
schéma de la base ou coordination par `meta` ; authentification, confidentialité, sorties vers l'extérieur
(Telegram, e-mail, MCP) ; boucle d'événements du site ; collecte et indexation ; coûts LLM ; déploiement.

### 2. Concevoir avant de coder (M/L)

Écrire `docs/notes/AAAA-MM-JJ-sujet.md` à partir de [docs/notes/_modele.md](docs/notes/_modele.md) :
1. **Faits mesurés, pas supposés.** Avant toute option : mesurer en prod ce dont dépend le design (mémoire anonyme
   par conteneur, volumes, durées, fréquences, comportement réel d'une bibliothèque). Une hypothèse non vérifiée
   s'écrit « hypothèse » et se vérifie avant de coder dessus.
2. **Modes de panne** : pour chaque composant touché, comment il échoue (exception, plantage natif, mémoire,
   blocage, arrêt pendant l'opération, concurrence, réseau) et ce que l'utilisateur ou l'admin en voit.
3. **Critères d'acceptation** vérifiables : ce qui doit être vrai après le changement. C'est la spécification de
   la revue.
4. Options envisagées, choix et pourquoi ; **hors périmètre** explicite (la dette voisine va dans « Suites »).
5. Déploiement, retour arrière, vérifications après mise en production.

Décision qui revient à l'utilisateur (compromis produit, coût, risque) : la poser avant de coder.

### 3. Incident en production

1. **Arrêter l'hémorragie d'abord** : proposer tout de suite la mitigation manuelle la plus simple et réversible
   (passer un document en erreur, couper un réglage, revenir à l'image précédente), avant toute ingénierie.
2. Établir la cause avec des preuves (journaux, `journalctl -u docker`, base), reproduire hors du service en
   marche (`docker compose run --rm --no-deps …`, base en lecture seule `?mode=ro`).
3. Puis note de conception : correctif **et** détection (comment l'aurait-on su en minutes ?).
4. Ajouter le piège à PITFALLS.md.

### 4. Revue adversariale (M/L)

- **Auto-contrôle d'abord** : passer la liste de PITFALLS.md sur le diff ; relire la note ; tests verts.
- **Relecteur** : l'agent `reviewer` (`.claude/agents/reviewer.md`, **Sonnet, effort moyen** : fixé dans sa définition,
  ne pas le surcharger), **neuf à chaque tour** (jamais repris), avec
  la note de conception et le diff, pas mes conclusions. Il juge chaque critère d'acceptation (conforme / non
  conforme) et classe chaque constat : **régression** introduite, **dette existante**, **hors périmètre** ;
  gravité **bloquant** seulement si un critère d'acceptation, la sécurité, la confidentialité ou les données sont
  en jeu.
- **Deux tours au plus.** Je traite les bloquants ; les autres vont dans « Suites » de la note. Si le 2e tour
  trouve encore un bloquant : **arrêter de rapiécer**, la conception est fausse → revenir à la note.
- **Pas de correction locale** : chaque constat accepté se reporte d'abord dans la note (critères, modes de panne),
  puis on vérifie qu'il ne casse pas un autre critère. Une correction qui touche un autre composant = repasser §2.

### 5. Vérifier, livrer

- Tests : `.venv/bin/python -m pytest -q` (CI à chaque push). Un test par critère d'acceptation quand c'est
  possible ; pas de test dépendant de l'horloge (passer `now=`).
- Livraison : commit (message en français, ce qui change pour l'utilisateur), puis `scripts/ship.sh` (ou
  `--app`), puis les vérifications listées dans la note (journaux, base, comportement) ; dire ce qui n'est pas
  vérifié.

## Production (repères)

VPS et chemins : voir `scripts/ship.sh`. Conteneurs `cop31-app-1` (site + MCP, 700 Mo), `cop31-worker-1`
(veille, alertes, bot, 600 Mo), `cop31-mail-1` (port 25, 150 Mo) ; une base SQLite partagée (`data/`, WAL).
Requête en lecture : `ssh … 'cd RAG_cop_climat && sudo docker compose -f deploy/docker-compose.yml exec -T app
python -'` avec un script sur l'entrée standard. Redémarrages et codes de sortie : `sudo journalctl -u docker |
grep exitCode` (pas `docker inspect`). Reconstruire le worker tue les `exec` en cours dans ce conteneur.

## Style

Réponses et documentation en français ; code commenté en français, noms courts ; pas de fonctionnalité au-delà de
la demande ; dire ce qui n'est pas fait ou pas vérifié.
