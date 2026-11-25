# 2026-11-22 — Plantage du worker en boucle et pannes silencieuses

*Taille : incident → L · Statut : validé, revu (tour 1 : déployable)*

## Problème

Depuis le 19/11 à 23:00 UTC, le worker plante en boucle (50 fois en trois jours) sur le document 10873, un PDF
d'Ouzbékistan venu des soumissions. Depuis, plus aucune alerte, plus aucun récapitulatif Telegram, et la purge
de rétention ne tourne plus. Personne n'a été prévenu.

Preuves :
- `journalctl -u docker` : `exitCode=139`, `restartCount=38` depuis le déploiement du 20/11.
- Reproduction : segfault dans `pymupdf` `find_tables` → `extract._pdf`, obtenu dans un processus à part.
- `meta.alerts_last_doc_id` est resté à 10872, avec 98 documents en attente derrière.
- La dernière ligne de `notifications` et le dernier `digest_slot` datent du 19/11.
- `error_events` est vide.

Exigence : aucune erreur ne doit pouvoir bloquer quelque chose sans qu'un administrateur le sache vite.

## Faits mesurés (22/11)

| Fait | Valeur | Comment mesuré |
|---|---|---|
| Mémoire anonyme du site | ≈ 490 Mo / 700 | `memory.stat` (`anon`) dans `cop31-app-1` |
| Mémoire anonyme du worker | ≈ 130 Mo / 600 | idem dans `cop31-worker-1` |
| Lecture d'un PDF dans un sous-processus | ≈ 245 Mo au pic, 2 à 3 s | `docker compose run --rm --no-deps worker`, 3 PDF de 5 à 12 pages, `ru_maxrss` |
| Plantage | segfault dans `find_tables`, quelle que soit la page | `python -X faulthandler`, conteneur du worker |
| Outils MCP en `def` | appelés dans la boucle d'événements | `mcp/server/fastmcp/utilities/func_metadata.py` (`fn_is_async`) |
| PID 1 du worker | `cop worker` (forme exec) | `deploy/docker-compose.yml` |
| Curseur des alertes | s'arrête au premier document `pending` | `alerts.py` (`MIN(id) … status='pending'`) |
| Sources sans succès | 404 ou 500 depuis des jours | `fetch_log` |

Hypothèse non vérifiée : sur le VPS, la sortie d'uvicorn en 5 s après SIGTERM, avec des flux MCP ouverts, se fait
bien dans le délai d'arrêt laissé par Docker. À vérifier lors du déploiement (voir plus bas).

## Modes de panne

| Composant | Panne | Avant | Après |
|---|---|---|---|
| Extraction de PDF (worker) | segfault, blocage, mémoire | le processus meurt, Docker le relance, boucle silencieuse | sous-processus ; repli sans tableaux ; sinon `error` et alerte |
| Extraction de PDF (site) | idem, plus OOM (490 + 245 > 700) | le site meurt | le site ne lit jamais de PDF : document `pending`, lu par le worker |
| Document coincé en `pending` | arrêt pendant son indexation | alertes bloquées pour toujours | quarantaine après 2 tentatives interrompues ; alerte au-delà de 30 min |
| Processus worker ou site | segfault, exception, OOM | invisible (redémarrage Docker) | alerte « arrêt anormal » au redémarrage ; alerte « en boucle » à partir de 3 en 2 h |
| Boucle de fond (veille, direct, soumissions, courrier, bot, indexation, site, boucle d'événements) | bloquée ou arrêtée | invisible | battement de cœur en retard : alerte depuis l'autre processus |
| Récapitulatif, curseur des alertes | en retard | invisible | alerte |
| Source | en échec continu | invisible | un message par jour (anti-bot compté à part) |
| Lettre entrante | arrêt pendant son traitement | perdue en silence | reprise une fois, puis erreur et alerte |
| Telegram | jeton révoqué, admin non relié | alerte perdue | e-mail aux admins, sinon `ALERTE NON TRANSMISE` dans les journaux |
| Site et worker tous deux arrêtés (VPS) | — | invisible | **toujours invisible de l'intérieur** : sonde externe (voir Suites) |

## Critères d'acceptation

1. **Extraction sûre.** Un PDF qui fait planter ou bloque l'extraction ne fait tomber ni le worker ni le site.
   Le document finit `indexed`, après un repli sans tableaux, ou `error`. L'admin reçoit une alerte.
2. **Mémoire du site.** Le processus du site ne lance jamais d'extraction de PDF. Ses dépôts (`deposer_url`,
   dépôt d'un texte de session) sont indexés par le worker en moins d'une minute. `deposer_url` ne bloque pas la
   boucle d'événements.
3. **Rien ne reste `pending`.** Un document interrompu deux fois par un arrêt anormal passe en `error`, avec une
   alerte. Un arrêt demandé ne compte pas comme une tentative. Le fil d'indexation ne reprend pas un document
   qu'un autre fil du même processus est en train d'indexer.
4. **Arrêts anormaux.** Un arrêt non demandé du worker ou du site produit une alerte au redémarrage. Trois arrêts
   anormaux en 2 h produisent une alerte « en boucle ». Un déploiement (SIGTERM) n'en produit aucune.
5. **Boucles arrêtées.** Une boucle déclarée qui dépasse son délai maximal (en minutes : veille 90, direct 90,
   soumissions 60, courrier 15, Telegram 10, indexation 30, site 15, boucle d'événements 10) déclenche une alerte
   dans les 5 min. L'autre processus la détecte aussi. Quand le problème disparaît, un message « rétabli » part.
6. **Blocages des alertes.** Des documents indexés depuis plus de 120 min sans passage des alertes, ou un
   récapitulatif non parti une heure après son créneau, déclenchent une alerte « bloquant ». Un document `pending`
   observé depuis plus de 30 min aussi.
7. **Dégradations.** Les sources sans aucun succès depuis 24 h font l'objet d'au plus un message par jour. Les
   sources bloquées par un anti-bot sont comptées, sans être listées. La mémoire du conteneur au-dessus de 92 % et
   le disque presque plein suivent la même règle.
8. **Livraison des alertes.** Telegram d'abord, sinon e-mail, sinon une ligne « ALERTE NON TRANSMISE » dans les
   journaux. Une alerte ne contient jamais de titre ni de texte de document, ni de question d'utilisateur :
   seulement des identifiants, des noms de sources et des durées.
9. **Sonde.** `/healthz/deep` répond 503 si un problème bloquant est en cours, 200 sinon. La réponse contient
   seulement des noms de problèmes et reste en cache 60 s.
10. **Courrier.** Une lettre interrompue en cours de traitement est retraitée une fois, puis enregistrée en
    `erreur` avec une alerte. Elle n'est jamais perdue en silence.
11. **Premier déploiement.** Il ne produit pas de fausse alerte « bloquant ». Le document 10873 est traité (lu ou
    mis en `error`), les alertes reprennent leur cours et l'arriéré est envoyé aux abonnés : salve attendue
    d'environ 98 documents, pour les 2 comptes abonnés.
12. Tous les tests passent (`pytest`), y compris un test par critère quand c'est faisable.

## Conception

- **Extraction.** `extract.extract` lit les PDF dans un sous-processus (`python -m cop_corpus.extract`), un seul à
  la fois par processus, en deux essais : 180 s avec tableaux, puis 60 s sans. Le site active `DEFER_PDF` : le
  document reste `pending` et le fil d'indexation du worker (`indexing_forever`, toutes les 15 s) le lit.
  `verify_deposit` relit le texte dans les passages indexés.
  - *Écarté :* tout lire sur place, ce qui laisse le risque de segfault.
  - *Écarté :* lire dans le site, à cause de la mémoire.
  - *Écarté :* un conteneur « indexeur » séparé, trop lourd pendant la COP ; c'est une piste pour les Suites.
- **Quarantaine.** Un compteur de tentatives par document, dans des fichiers `data/run/reindex/` : il est écrit
  avant l'indexation et effacé après, ou par un arrêt demandé.
- **Arrêts anormaux.** Un fichier `data/run/{worker,app}.json` porte `clean=false` tant que le processus tourne.
  Seuls le gestionnaire de SIGTERM du worker et le retour d'uvicorn le passent à `true`. Ni `atexit` ni SQLite
  n'interviennent (voir PITFALLS).
- **Chien de garde** (`watchdog.py`).
  - Battements de cœur dans `meta`, écrits au plus une fois par minute.
  - Chaque processus déclare ses boucles et leur délai maximal. Le battement `app-boucle` est écrit depuis la
    boucle d'événements du site.
  - Contrôles toutes les 5 min, depuis le site et depuis le worker, chacun surveillant l'autre.
  - Un problème bloquant passe par `monitor.report`, avec un rappel toutes les 30 min. Un problème dégradé donne
    au plus un message par jour. La fin d'un problème donne un message « rétabli ».
- **Sources.** Une colonne `source_state.failing_since` marque le début de la série d'échecs ; elle repasse à
  NULL au premier succès.
- **Hors périmètre** : la dette voisine est listée dans les Suites.

## Déploiement

- Par `scripts/ship.sh`, sur tous les services. `stop_grace_period` vaut 45 s pour le worker et 30 s pour le site.
- Au premier démarrage :
  - aucun fichier `run/*.json` : pas d'alerte d'arrêt anormal ;
  - `failing_since` est NULL : pas d'alerte de sources avant 24 h ;
  - le fil d'indexation reprend le document 10873 (isolé, puis lu sans tableaux ou mis en `error`) ;
  - le curseur des alertes repart et l'arriéré part aux abonnés ;
  - **une alerte « bloquant » vraie mais passagère est attendue** : `alerts_lag` (et `digest` si on déploie plus
    d'une heure après un créneau), suivie d'un « rétabli » une fois l'arriéré traité.
- Vérifications après la mise en production :
  1. `journalctl -u docker` : aucun `exitCode` après le déploiement.
  2. Statut du document 10873 : `indexed` ou `error`.
  3. `alerts_last_doc_id` égal au plus grand identifiant.
  4. Les fichiers `run/worker.json` et `run/app.json` existent.
  5. `/healthz/deep` répond 200.
  6. Les clés `hb:*` sont fraîches.
  7. Un SIGTERM de test (`docker compose restart worker`, puis `restart app`) ne déclenche aucune alerte
     « arrêt anormal », ce qui vérifie l'hypothèse ci-dessus (uvicorn relance SIGTERM après son arrêt propre ;
     sans effet tant que le site est PID 1).
- Retour arrière : `scripts/ship.sh --rollback`. Les colonnes et fichiers ajoutés sont sans effet sur l'ancienne
  version.

## Revue

- Tours 1 à 3, avant la mise en place de la méthode : mêmes constats que dans PITFALLS (méthode).
- Tour 3 :
  - « important » acceptés :
    - le fil d'indexation entrait en concurrence avec les autres fils ;
    - le marqueur de courrier était écrit après le déplacement de la lettre ;
    - `deposer_url` rendait la main encore `pending` sans message.
  - « mineur » acceptés : test du battement de la boucle d'événements.
  - Refusé : rejouer les effets d'une lettre reprise (accusé de réception). C'est rare, et les documents sont
    dédoublonnés par empreinte.
- Revue contre cette note (méthode CLAUDE.md §4), tour 1 : **déployable**. Les critères sont conformes ; le 4 est
  non vérifié côté site (vérification 7).
  - Acceptés et corrigés :
    - A : le fil d'indexation relit le statut juste avant d'indexer ; un document indexé entre-temps n'est pas
      refait.
    - B : une exception Python n'est pas comptée comme une tentative interrompue (`finally`).
    - E : un seul écrivain de l'état du chien de garde, le worker. Le site n'alerte que si le worker ne vérifie
      plus depuis 15 min.
  - Reportés dans la note :
    - C : alerte passagère au premier déploiement (section Déploiement) ;
    - D : redémarrage de test du site (vérification 7).
  - En suivi : F et G (Suites). Pas de tour 2 : aucun constat bloquant.

## Suites

- **Sonde externe** sur `/healthz/deep` (service d'uptime) : seule défense quand le site et le worker sont tous
  deux arrêtés. À créer par l'utilisateur, qui seul peut ouvrir le compte.
- Sources en 404 ou 500 depuis des jours : URL à corriger (`unfccc-cop31-auv-draft-texts`, `-outcomes`,
  `-presidency-consultations`, `-daily-programme`, `-session-pages`).
- `login_codes.sent_at` garde l'adresse IP en clair.
- Les autres outils MCP synchrones lents (`rechercher`, jusqu'à 7 s) figent la boucle d'événements pendant leur
  exécution.
- File d'indexation unique, voire un conteneur indexeur : les fils de la veille, des soumissions et du courrier
  indexent encore eux-mêmes, en sous-processus.
- Un arrêt demandé n'efface pas les marqueurs de courrier : deux déploiements pendant la même lettre la passent en
  `erreur`.
- Extraction sérialisée dans le worker, un sous-processus par PDF : lors d'une salve de textes, les délais des
  boucles `indexation` (30 min) et `courrier` (15 min) pourraient être dépassés, avec une alerte « boucle
  arrêtée ». À mesurer en production.
- Un document HTML déposé sur le site peut être indexé en même temps par le site et par le fil d'indexation du
  worker (la protection `_indexing` ne vaut que dans un processus). Sans conséquence sur les données.
