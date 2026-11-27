# PITFALLS.md — pièges déjà payés

Chaque entrée : ce qui s'est passé ou ce qui trompe, la règle, comment vérifier. On ajoute une entrée après chaque
incident ou chaque constat bloquant de revue ; on n'en retire que si le code rend le piège impossible (et testé).

## Liste à passer sur un diff avant revue

- [ ] Un plantage natif, une exception non rattrapée, un arrêt brutal (SIGKILL, mémoire) pendant ce code : qui le
      voit, en combien de temps ? Que reste-t-il à moitié fait (fichier déplacé, statut intermédiaire) ?
- [ ] Un traitement qui peut échouer sur une entrée précise : est-il réessayé sans fin ? Bloque-t-il la suite ?
- [ ] Mémoire : quel conteneur, quelle marge **mesurée** (mémoire anonyme, pas `docker stats`) ?
- [ ] Code synchrone appelé depuis la boucle d'événements du site (outil MCP `def`, route `async def`) ?
- [ ] Transaction SQLite ouverte pendant un appel réseau, une attente ou un sous-processus ?
- [ ] Deux processus (site, worker) ou deux fils écrivent-ils la même clé `meta`, la même ligne, le même fichier ?
- [ ] Ce qui part vers Telegram, l'e-mail, les journaux : aucun titre ou extrait restreint, aucune question
      d'utilisateur, aucun secret, aucune IP en clair ?
- [ ] Premier déploiement : fausses alertes (données anciennes, colonnes NULL), rattrapage d'un arriéré
      (salve de notifications) ?
- [ ] Arrêt pendant un déploiement (SIGTERM, 10 s par défaut) : l'état est-il cohérent, est-ce compté comme panne ?
- [ ] Réglage nouveau : dans `config.py` et dans architecture.md §7 ? Outil MCP : dans le tableau de la doc ?
- [ ] Page publique ajoutée ou texte de présentation modifié : architecture.md §10 (sitemap ou `NOT_INDEXED`,
      description, `llms.txt`, même accroche partout) ? Les tests le rattrapent en partie, pas le sens du texte.

## Processus, Docker, signaux

- **Un plantage natif (segfault) ne passe par aucun `except`.** PyMuPDF (`find_tables`, modèle de mise en page) a
  planté le worker sur un PDF, 50 fois en 3 jours (19–22/11/2026). → Tout code natif sur une entrée externe (PDF)
  s'exécute dans un sous-processus ; l'appelant ne voit qu'une exception.
- **`restart: unless-stopped` masque les boucles de plantage.** `docker inspect` montre l'état du conteneur
  *relancé* (ExitCode 0, OOMKilled false). → Les vrais codes : `sudo journalctl -u docker | grep exitCode`
  (139 = segfault, 137 = SIGKILL/mémoire). Un redémarrage non demandé doit être détecté par l'application.
- **Python en PID 1 ignore SIGTERM sans gestionnaire** : Docker attend 10 s puis SIGKILL. → Gestionnaire explicite,
  `stop_grace_period` suffisant.
- **`atexit` tourne aussi après une exception non rattrapée** : ne s'en servir pour noter un « arrêt propre ».
  → Seul le gestionnaire de SIGTERM (ou le retour normal d'uvicorn) note un arrêt voulu ; dans un fichier, pas
  dans SQLite (un gestionnaire de signal ne doit pas attendre un verrou).
- **uvicorn attend sans fin la fermeture des connexions** (flux MCP ouverts) à l'arrêt. → `timeout_graceful_shutdown`.
- **`docker compose up --build worker` tue les `docker compose exec` en cours dans ce conteneur** (calcul
  d'embeddings, réindexation). → Expériences risquées dans un conteneur jetable : `docker compose run --rm --no-deps`.

## Mémoire

- **Mesurer la mémoire anonyme** (`/sys/fs/cgroup/memory.stat` : `anon`), pas `docker stats` (inclut le cache).
  Mesures du 22/11/2026 : site ≈ 490 Mo / 700, worker ≈ 130 Mo / 600.
- **Les vecteurs sont chargés dans chaque processus qui cherche** (≈ 200 000 × 512 × 4 octets ≈ 410 Mo) : c'est
  l'essentiel de la mémoire du site.
- **Un sous-processus PyMuPDF pèse ≈ 245 Mo** (modèle de mise en page). Il compte dans la limite du conteneur ;
  le tueur OOM du cgroup tue le plus gros processus, souvent le parent. → Jamais d'extraction de PDF dans le site ;
  une seule à la fois dans le worker.

## Boucle d'événements du site

- **Le SDK MCP appelle les outils `def` directement dans la boucle d'événements** : un outil lent fige tout le
  site (pages, MCP, /healthz). → Outil lent ou bloquant : `async def` + `anyio.to_thread.run_sync`.
- Une route Starlette `async def` qui appelle du code bloquant (base, extraction, réseau) fige aussi la boucle.
- Un battement de cœur écrit par un fil séparé ne prouve pas que la boucle tourne : il doit partir de la boucle.
- **Une page `async def` sans `await` s'exécute dans la boucle** : ses requêtes SQLite figeaient tout le site
  (p90 7,5 s, 25/11/2026, pages dossier de 8 à 15 s). → Page sans `await` : `def` (Starlette la passe au pool de
  fils) ; testé par `test_page_async_sans_await_interdite`. Une page `async` qui fait du synchrone après son
  `await` : `run_in_threadpool` (#15).
- **Le SDK MCP exécute un outil `def` dans la boucle d'événements** (`rechercher` : 3,4 s de site figé). → Outils
  déclarés par `mcp_server._tool` (enveloppe async + fil) ; testé (`all(t.is_async …)`).
- **Une page `async def` fait tout son travail dans la boucle après son `await`** (indexation d'une lettre, scrypt,
  nouveautés). → Page à formulaire : `def` sous `web.body_first` ; liste fermée des `async` permises
  (`tests/test_lenteur.py`, `ASYNC_PERMIS`).
- **Passer du code de la boucle dans des fils révèle des courses que la boucle masquait.** « Lire `meta` puis
  écrire » laissait un lien de connexion à usage unique ouvrir deux sessions (revue, 25/11). → Avant de passer une
  page en `def`, chercher les « tester puis écrire » ; écriture atomique (`INSERT OR IGNORE` + `rowcount`).
- **Un cache ne garde jamais un échec** : un « database is locked » gardé 10 min = une page en 500 pendant
  10 min. → L'exception remonte, rien n'est mis en cache (`web._ttl_cached`, testé).

## SQLite partagée (site + worker + fils)

- **En WAL, la date de la base principale ne change qu'aux checkpoints, et le fichier WAL ne rétrécit pas**
  (321 Mo gardés après l'incident du 26/11). Ni l'une ni l'autre ne mesure un lecteur coincé. → `journal_size_limit`
  (64 Mo, `db.connect`) ; mesure directe par `PRAGMA wal_checkpoint(PASSIVE)` (`log − checkpointed`), jamais dans
  une transaction (`watchdog.issues`).
- **Annoncer avant d'enregistrer** : une connexion qui lit une version figée croit le problème réglé, envoie
  « Rétabli », puis ne peut pas écrire son état (26/11). → État écrit d'abord, annonce ensuite (`watchdog.check`).

- **Charger une table entière par `fetchall` puis la convertir** coûte 2 à 3 fois sa taille (lignes Python +
  `join` + `vstack`) : les vecteurs (212 Mo) mettaient le site à son plafond de 700 Mo (27/11/2026). → Lecture par
  `fetchmany` dans des blocs préalloués jamais recopiés (`embed._refresh`, `tests/test_vecteurs.py`).

- **« database is locked »** déjà vu (collecte, 20/11). Une base, plusieurs conteneurs, `busy_timeout` 30 s. →
  Transactions courtes ; jamais d'appel réseau (Telegram, LLM, HTTP) ni de sous-processus transaction ouverte.
- **Une écriture en échec sur verrou laisse la transaction ouverte** (module `sqlite3`) : la connexion lit ensuite
  une version figée et toute écriture suivante échoue aussitôt, sans fin ; le WAL ne se reporte plus dans la base.
  Gel du VPS de 4 min le 26/11/2026 → boucle d'indexation coincée 3 h, WAL à 303 Mo, jusqu'au redémarrage. →
  `db.connect` annule la transaction sur `SQLITE_BUSY`/`SQLITE_LOCKED` (`db._Connexion`, `tests/test_verrou.py`) ;
  toute connexion passe par `db.connect`, jamais `sqlite3.connect` directement.
- **La table `meta` sert de coordination** (curseurs, créneaux, état) : lecture-modification-écriture non
  atomique entre processus. → Une clé = un seul écrivain, ou un écrasement sans conséquence.
- **Horodatages** : texte UTC `YYYY-MM-DDTHH:MM:SSZ` (`db.now()`), comparés comme chaînes ; créneaux et affichage
  à l'heure de la COP (`config.TIMEZONE`).

## Collecte, indexation, alertes

- **Le curseur des alertes s'arrête au premier document `pending`** (`alerts.run`) : un seul document coincé
  arrête toutes les alertes, en silence. → Un document ne doit pas pouvoir rester `pending` (quarantaine).
- **Un document `pending` peut être en cours d'indexation par un autre fil** (enregistrement puis indexation).
- **`fetch_log` ne journalise que `new`, `error`, `crash`** (pas `unchanged`) : on ne peut pas y lire le dernier
  succès d'une source.
- Plusieurs URL de sources officielles renvoient 404 ou 500 depuis des jours sans que rien ne le signale
  (22/11 : `unfccc-cop31-auv-draft-texts`, `-outcomes`, `-presidency-consultations`, `-daily-programme`).
- Le courrier entrant est archivé **avant** traitement : un arrêt pendant le traitement perd la lettre en silence.

## Surveillance

- **`monitor.report` ne voit que les exceptions Python** : ni segfault, ni arrêt mémoire, ni boucle arrêtée, ni
  blocage. Table `error_events` vide ≠ tout va bien.
- Une alerte envoyée seulement à Telegram se perd si le jeton est révoqué ou l'admin non relié.
- Le site et le worker peuvent se surveiller l'un l'autre, mais pas une panne des deux (VPS) : sonde externe.

## Domaines, edge

- **La route Caddy sert trois noms** (`opencop.org, www.opencop.org, cop31.akimx.fr`), mémorisés dans
  `deploy/edge/.env` sur le VPS. Réinstaller la route avec un seul nom retire les autres : plus servis, plus
  renouvelés ; sans `cop31.akimx.fr`, le courrier entrant perd son certificat. → Toujours la liste complète.
- **`MAIL_DOMAIN` est déduit de `COP_PUBLIC_BASE` s'il n'est pas fixé** : changer l'adresse publique déplacerait la
  réception du courrier. → `COP_MAIL_DOMAIN` fixé explicitement dans `.env`.
- **Domaine neuf : réponse négative en cache** chez le résolveur du poste (jusqu'à 1 h) même une fois la délégation
  publiée. → Vérifier avec `dig @1.1.1.1` et `curl --resolve`, pas avec le résolveur local.

## Confidentialité, sécurité

- Telegram n'est pas chiffré de bout en bout : jamais de titre ni d'extrait de document restreint (alertes,
  /cherche, /nouveau, récapitulatif, alertes d'erreur).
- `login_codes.sent_at` garde l'adresse IP en clair (constaté le 22/11/2026), contre la règle : issue #3.
- Chemins à secret (`/u/<jeton>`, `/entrer/`, `/join/`…) masqués dans les journaux d'accès (`logs.py`, Caddy).

## Tests

- La fixture `env` recharge `config`, `db` et plusieurs modules : un module importé ailleurs garde l'ancien état
  (recharger aussi celui qu'on teste).
- Pas de test dépendant de l'heure réelle : passer `now=`.
- Tester l'échec, pas seulement le succès : un faux interpréteur qui se tue (SIGSEGV) reproduit un plantage natif
  (`tests/test_extraction_isolee.py`).

## Méthode (agents)

- **Coder avant de mesurer** a coûté trois tours de revue le 22/11 : mémoire du site, outils MCP synchrones,
  `atexit`, arrêt d'uvicorn, tous vérifiables en cinq minutes. → Faits mesurés dans la note, avant le code.
- **Corriger un constat de revue localement** en crée d'autres (isoler partout → risque mémoire sur le site). →
  Reporter d'abord le constat dans la note, revérifier tous les critères.
- **Un relecteur repris d'un tour à l'autre s'ancre** sur ses constats ; « trouve des problèmes » ne rend jamais
  rien de vide. → Relecteur neuf, jugement contre les critères d'acceptation, deux tours au plus.
- **Laisser la prod bloquée pendant qu'on conçoit** : la mitigation manuelle (un `UPDATE`) passe d'abord.
