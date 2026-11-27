# OpenCOP — documentation de référence

*(Ancien nom : COP Tracker. Nom affiché, adresse, accroche : §10.)*

Corpus vérifiable, veille en temps réel et alertes pour suivre les négociations de la COP31 (Antalya,
9–20 novembre 2026). Outil indépendant, gratuit et non commercial, sans lien avec la CCNUCC ni avec une délégation.
Ce document décrit l'état actuel ; l'historique de construction est dans [historique.md](historique.md),
l'exploitation (commandes, déploiement, courrier) dans le [README](../README.md). Réglages : §7.
Indexation et présentation publique (ce qu'il faut mettre à jour quand on ajoute une page) : §10.

## 1. Vue d'ensemble

```
sources publiques (ODS/ONU, portail des soumissions CCNUCC, EUR-Lex, Conseil UE, GIEC, PNUE, ENB, Carbon Brief,
cop31.tr, OWID…) + lettres d'information reçues sur veille@
   │  worker : veille documentaire, veille en direct (textes, positions, briefs), soumissions, groupes, pavillons
   ▼
data/cop.sqlite3 (une seule base SQLite : FTS5 + vecteurs en mémoire)
   │
   ├── site web              /                 accueil, connecter un LLM, planifier ma journée, mes sujets,
   │                                           à propos (comment ça marche, base de données, contact), compte, admin
   ├── MCP                   /u/<jeton>/mcp    Claude (plugin clients/claude-plugin), ChatGPT, Le Chat
   └── bot Telegram          alertes sur les sujets, récapitulatif sans IA à 7 h et 19 h, recherche rapide
```

Trois conteneurs sur le VPS : `app` (web + MCP), `worker` (toutes les tâches planifiées, bot Telegram), `mail`
(courrier entrant, port 25).

Site (`app`) : un seul processus uvicorn, une boucle d'événements. Les pages sans `await` sont des `def`, exécutées
par Starlette dans un pool de 12 fils (`server.THREADS`, une connexion SQLite par fil ; toute connexion vient de
`db.connect`, qui annule sa transaction quand une écriture échoue sur verrou) ; une page `async def` sans
`await` est refusée par un test ; une page à formulaire est `def` sous `web.body_first` (le corps est lu dans la boucle, la page tourne dans un fil). Les outils MCP synchrones tournent aussi dans un fil (`mcp_server._tool`). Caches en mémoire : parties lourdes des pages dossier (« nouveautés », frises ;
1 h, invalidé par la signature du corpus : passages, accès et nombre de documents), statistiques et nombre de sources
(10 min, échecs jamais gardés). Produit vectoriel par blocs de 8 192 lignes, deux à la fois au plus. Mesuré le
26/11 après livraison : p90 des requêtes 0,18 s (7,5 s avant), page dossier en cache 0,08 s, premier calcul
≈ 19 s sans bloquer les autres pages ; mémoire anonyme 342 Mo sur 700 Mio (vecteurs en blocs fixes de 8 192, chargés par `fetchmany` : pic du chargement ≈ matrice + 30 Mo ; 27/11).

## 2. Niveaux de fiabilité

Chaque document porte un niveau ; chaque réponse dit de quel niveau vient chaque affirmation.

| Niv. | Nature | Exemples |
|---|---|---|
| 1 | Texte adopté | Convention, Accord de Paris, décisions x/CP.y, x/CMA.y, droit de l'UE au JO |
| 2 | Officiel non adopté | projets de texte (avec version), NDC, BTR, conclusions du Conseil, **soumission d'une Partie** |
| 3 | Science et données | GIEC, PNUE, AEE, Our World in Data, Climate Watch |
| 4 | Compte rendu de référence | ENB, Carbon Brief, lettres d'information reçues |
| 5 | Position d'un acteur | communiqués, presse, **soumission d'un observateur** — jamais une preuve |
| R | Document interne | dépôts restreints des contributeurs |

Règles (consignes du serveur MCP, skills, prompts) : rien sans passage cité ; un projet de texte porte sa version,
sa date et « non adopté » ; un niveau 5 ne dément pas un niveau 1–3 ; rien trouvé → le dire.

## 3. Données, par domaine

| Domaine | Module | Tables |
|---|---|---|
| Corpus | `ingest.py`, `extract.py`, `search.py`, `embed.py` | `documents` (reçu SHA-256, niveau, lignée de versions, accès), `passages` + `passages_fts`, `vectors`, `url_state`, `fetch_log`, `source_state` |
| Données chiffrées | `data.py` | `emissions` (Our World in Data ; sert aussi de table pays → ISO3) |
| Utilisateurs et alertes | `auth.py`, `alerts.py`, `telegram.py` | `users`, `login_codes`, `subscriptions`, `notifications`, `meta` |
| Négociations en direct | `live.py`, `positions.py`, `briefs.py` | `live_texts` (index Carbon Brief), `dossier_status`, `positions`, `extraction_log`, `social_posts`, `field_notes` (notes de terrain, saisie retirée), `briefs`, `dossier_follows`, `llm_usage` |
| Soumissions officielles | `soumissions.py` | `sub_calls`, `sub_items` (dont ISO3 de l'auteur), `sub_actors`, `sub_call_dossiers`, `sub_analysis`, `sub_comparisons` |
| Groupes de négociation | `groupes.py` + `catalog/groupes.yaml` | `grp_groups`, `grp_members` |
| Programme et pavillons | `agenda.py`, `pavillons.py`, `pavillons_ext.py` | `agenda_events`, `agenda_overview`, `pav_events` + `pav_fts` |
| Courrier entrant | `mailin.py` | `mail_inbox` |

Catalogues versionnés : `catalog/sources/*.yaml` (sources de la veille), `catalog/dossiers.yaml` (18 dossiers de
négociation et leurs alias), `catalog/groupes.yaml`, `catalog/glossaire.yaml`, `catalog/bluesky.yaml`.

### Corpus

Collecte polie (délai par hôte, requêtes conditionnelles, anti-bot jamais contourné : unfccc.int passe par l'ODS
de l'ONU ou par un dépôt manuel), extraction PDF/HTML/Word, découpage par paragraphe numéroté (« 1/CMA.5 §28 »),
lignées de versions (textes de session : par dossier et titre, car la cote change d'un organe à l'autre, `live.tidy_texts`) et diff par contenu, recherche hybride (paliers lexicaux + sens, priorité au texte adopté à pertinence
égale), liens directs vers chaque passage (`lien_source`, `lien_page`, `lien_passage`).

Extraction (`extract.py`) : tableaux tracés lus ligne par ligne, cellule par cellule (« a | b | c » : notes informelles en tableau) ; PDF à colonnes lus colonne par colonne (lignes classées gauche/droite, les lignes pleine
largeur coupent la page en bandes ; les tableaux, aux cellules courtes, restent lus par ordonnée) ; réparation des
artefacts (UTF-8 lu en Windows-1252 « TÃ¼rkiye », lettres espacées « S u b m i s s i o n ») ; pages HTML sans
jeu de caractères déclaré décodées en Windows-1252 (TWN) ; texte brut de la page quand trafilatura n'en garde qu'un
fragment (page sans `<article>`). Pages servies par une application JavaScript : texte par l'API publique du site
(`ingest.resolve` : discours et déclarations de la Commission sur presscorner, dont ceux de la COP).

Doublons de contenu (`doublons.py`) : empreinte de chaque texte (64 hachages de suites de 8 mots) ; deux documents qui
en partagent 90 % sont le même texte (déclaration du G77 sur g77.org et sur le portail CCNUCC, texte sans cote en
double de sa version renseignée). L'exemplaire de référence est le plus officiel (cote, vrai titre, niveau le plus
fort) ; l'autre (`documents.duplicate_of`) sort des recherches, nouveautés et alertes. Jamais entre deux versions
d'une lignée, deux cotes différentes, ni entre un document interne et un public.

**Comptes rendus et lettres** (le cœur de la veille, niveau 4 sauf ECO, niveau 5) :

| Source | Voie | Texte complet |
|---|---|---|
| ENB, comptes rendus quotidiens | `enb-daily-pdf` (`enb.py`) : PDF `sites/default/files/<aaaa-mm>/enb12<nnn><e\|f>.pdf`, numéros essayés à la suite, toutes les heures (pages HTML : Cloudflare, jamais contourné) | oui ; numéro, parution et journée couverte lus dans l'en-tête (n° 909 = « COP 31 #8 », paru le 18/11, journée du 17/11) |
| ENB, lettre quotidienne | veille@ | chapeaux + liens ; le « daily report » d'une journée = PDF du lendemain ; side events : HTML seulement, bloqués (`cop enb couverture`) |
| Carbon Brief, Daily Briefing | veille@ | oui (partie HTML : la partie texte n'est qu'un « can't display HTML ») ; pas d'archive web publique |
| Carbon Brief, articles | flux RSS, page COP31 | oui |
| Climate Home, « COP31 Bulletin Day N » | flux RSS | oui |
| ECO (CAN) | flux de la catégorie → page de chaque numéro | oui |
| TWN, Antalya Climate News Update | crawl de twn.my | oui (pages en Windows-1252) |

Chaque lettre reçue est un document à part, daté de son envoi (fuseau de la COP), titre « expéditeur — objet
(date) » : jamais une « version » de la lettre de la veille (avant le 18/11, les lettres de l'ENB en français et en
espagnol, au même objet, s'empilaient en versions d'un seul document). Le texte retenu est la partie HTML dès qu'elle
est plus riche que la partie texte, suivi des liens de contenu (sans pistage ni réseaux sociaux). Une cote citée dans
une lettre n'en fait pas une version du texte cité.

### Négociations en direct

Index public de Carbon Brief (textes de session, positions de référence « Who wants what », cité) ; en secours, fiches
numérotées de docs.unfccc.int (`docs_unfccc.py` : numéros suivants essayés toutes les 5 min, pas d'anti-bot), qui
annoncent un texte sans attendre l'index ; récupération
des textes par leur adresse puis par l'ODS, sinon « à récupérer » (dépôt manuel) ; positions extraites des sources
publiques récentes — comptes rendus de négociation (TWN, Climate Home, Carbon Brief, ENB, ECO) et déclarations
officielles (G77, AOSIS, présidence COP31, DG CLIMA), pas la presse généraliste (`positions.POSITION_SOURCES`) —,
gardées seulement si la citation figure mot pour mot dans la source ; extraction section par section (lots de
9 000 caractères) ; documents de l'ENB lus quelle que soit leur voie d'entrée (`positions.POSITION_ORGS`) : l'ENB
attribue chaque intervention (« the EU, supported by NORWAY, opposed by SAUDI ARABIA ») et c'est la seule source
publique des positions de l'UE par point de l'ordre du jour, qui ne publie pas ces interventions ; briefs IA par dossier à
7 h et 19 h, désactivés par défaut (`COP_BRIEFS`). Étape de chaque texte, sans IA (`live.text_stage`) : projet,
proposé pour adoption (document L), adopté (titre ou cote de décision) ; elle figure dans les alertes.

### Mes sujets, alertes et récapitulatif

Un sujet = un dossier suivi (`dossier_follows`) ou une requête par mots-clés (`subscriptions`). `topics.whats_new`
(sans IA, recherche par mots) donne pour chaque sujet ce qui a bougé depuis une date : textes de session (étape,
crochets, changements), comptes rendus et lettres qui en parlent, documents signalés. Elle sert à la page « My topics »,
à l'outil `nouveautes(mes_sujets=true)` et au récapitulatif Telegram de 7 h et 19 h (`digest.py`, rien de neuf → pas
de message, question toute prête pour le LLM de l'utilisateur en fin de message). Le dépôt de documents par les
contributeurs (site, Telegram, `depot-<code>@`) et les notes de terrain sont retirés.

### Calendrier

`events.py` réunit `pav_events` (pavillons, side events, hors site) et les réunions officielles d'`agenda_events` pour
la page « Plan my day » : filtres jour, mots (frontières de mots), type, zone, pavillon, « mes sujets » (mots des
sujets et alias des dossiers suivis). Sans IA.

### Soumissions officielles

API JSON publique du portail `submissions.unfccc.int` (≈670 appels, ≈8 500 dépôts depuis 2015). Chaque fichier
devient un document (`soumission` ou `declaration`, cote `UNFCCC-SUB/<id>`, Partie → niveau 2, observateur → 5).
Le stock historique est tenu à l'écart : exclu par défaut de `rechercher`, des nouveautés, des briefs et des
alertes s'il a été déposé il y a plus de 14 jours ; seules les soumissions de 2026 sont vectorisées.

- **Analyse et comparaison par le LLM du client** (coût porté par l'abonnement de l'utilisateur, aucun appel IA du
  serveur) : `soumissions(document=…)` renvoie les passages et une consigne (résumé, points soulevés, force du
  langage 1 constate / 2 suggère / 3 demande / 4 exige, citations mot pour mot) ; `comparer_soumissions(a, b)`
  renvoie les deux textes et la consigne de comparaison, puis `comparer_soumissions(a=<doc>, b=<doc>,
  qualifications=[…])` reçoit la qualification de chaque point (accord / partiel / conflit / un seul côté, forces,
  citations). Le serveur vérifie chaque citation dans le document (force ramenée à 2 si elle n'y figure pas mot pour
  mot) et calcule l'indication d'ensemble par règles fixes (`soumissions.verdict`) : points centraux = langage fort
  des deux côtés ; ≥ 2 conflits centraux, ou 1 avec ≤ 40 % d'accord → non alignées ; aucun point central → pas de
  position commune ; ≥ 50 % de différences partielles, conflit secondaire, ou 1 conflit avec > 40 % d'accord →
  partiellement ; sinon alignées. Le résultat est gardé en cache (`model='client'`) : alignements des dossiers et
  matrice. « Contre tout l'appel » et le mode « membres d'un groupe » ne calculent rien : ils renvoient les
  comparaisons en cache et les paires à comparer.
- **Analyse par le serveur** (`soumissions.analyze` / `compare`, modèle `COP_LLM_EXTRACT_MODEL`) : seulement en ligne
  de commande (`cop soumissions analyser|comparer`) ou en tâche de fond si `COP_SUB_AUTO_ANALYSE=1` ; ses points
  les plus forts (6 par soumission récente de Partie) alimentent `positions` (rubrique « positions écrites »).
- **Acteurs** : un seul résolveur, `positions.normalize_actor` (alias FR/EN, sigles), égalité stricte (« Mali »
  ≠ « Somalia ») ; une Partie qui soumet au nom d'un groupe est attribuée au groupe.
- **Lien dossier ↔ appels** (sans IA) : règles `CALL_RULES` sur le point de l'ordre du jour puis le titre, thèmes
  du portail en dernier recours ; à défaut, dossier majoritaire des points analysés ; rattachement manuel
  prioritaire (`cop soumissions rattacher`).

### Groupes de négociation

Une liste de membres n'existe que si elle est sourcée et datée : g77.org (G77, 134), europa.eu (UE, 27), ONU
(PMA, PDSL, PEID), aosis.org ; EIG, Umbrella Group, BASIC : relevé daté de la page CCNUCC, phrase source citée
(page derrière un anti-bot, non relue automatiquement) ; AGN et Groupe arabe **par équivalence** (groupe régional
africain de l'ONU, Ligue des États arabes, mêmes nombres que ceux de la CCNUCC) ; LMDC, AILAC, ALBA, CfRN : non
vérifiés, aucun membre. Contrôle du nombre de membres (écart signalé), ISO3 de chaque membre (une parenthèse
onusienne n'est jamais simplement retirée). Un groupe qui n'a pas soumis en son nom est comparé via les soumissions
de ses membres, présentées comme telles avec la couverture.
Sous-groupes (`soumissions.SUBGROUPS`) : un groupe sans soumission propre est comparé via ses sous-groupes (G77 → AGN,
PMA, AOSIS, Groupe arabe, LMDC, AILAC…) puis ses membres, puis sa dernière soumission sur le dossier.

### Programme et pavillons

Programme : planner public (side events, réunions officielles, événements hors site), relevés SEORS importés à la
main, lien SEORS de vérification par side event. Pavillons : programmes de cop31.tr (Blue et Green Zones), side
events, pavillons publiés ailleurs (`pavillons_ext.py`) ; recherche par mots et par sens, comptages par pavillon.

## 4. Outils MCP

Consignes du serveur : moins de 1 800 caractères (les clients tronquent au-delà), règles d'abord, puis une ligne
par famille d'outils ; le détail est dans la description de chaque outil. Un test compare ce tableau au registre.

| Outil | Rôle |
|---|---|
| `rechercher` | recherche hybride dans le corpus (filtres niveau, type, session, partie, groupe, dates) ; trois degrés de pertinence : `resultats`, `pertinence_partielle` (la plupart des termes, pondérés par leur rareté ; `termes_manquants`), `faible_pertinence` ; `verdict` nuancé |
| `lire_passage` | texte exact d'un passage et de ses voisins |
| `document` | métadonnées et versions d'un document |
| `comparer_versions` | ce qui a changé entre deux versions d'un texte, unité par unité (`structure.py` : §, options, puces, alinéas, lignes de tableau ; notes de bas de page retirées et comparées à part) : modifié / repris (scission, fusion) / ajouté / supprimé sans reprise, issue des options, devenir de chaque crochet et alternative, glissements de verbe et de nuance, références citables (passage, page, PDF) des deux versions ; pagination sans coupe (`page`), `filtre` ; même calcul pour la page `/diff` |
| `historique_formulation` | trajectoire d'une notion ou d'une formulation (1,5 °C, sortie des fossiles, dons, mesures unilatérales… ou mots libres) dans toutes les versions d'un texte : présente sans crochets, entre crochets, dans une option ou absente ; unités, extraits, pages et liens ; bascules d'une version à l'autre avec le lien vers le diff (`formulations.py`, lexique `CONCEPTS`). Même calcul pour la frise « Au fil des versions » (pages dossier et `/diff` : crochets, options, état de chaque notion, case cliquable vers le texte barré) et sous les réponses `/ask` |
| `crochets` | formulations entre crochets d'un projet de texte sur un thème (genre, droits humains, désinformation…) et depuis quelle version |
| `nouveautes` | documents entrés récemment ; `mes_sujets=true` : ce qui a bougé sur les sujets de l'utilisateur |
| `donnees` | chiffres d'émissions |
| `glossaire` | sigles et notions |
| `dossiers` | vue d'ensemble des dossiers ; `textes=true` : derniers textes annoncés |
| `dossier` | un dossier : état, textes, brief, positions, positions écrites, soumissions liées, notes |
| `positions` | qui veut quoi (acteur, dossier, période) ; `divisions=true` : clivages internes d'un groupe (sous-groupes, membres) et alliés hors groupe ; `compact`, `limite` + `decalage` (pagination) |
| `soumissions` | appels, qui a soumis quoi, par dossier ou par groupe ; `document=` : analyse |
| `comparer_soumissions` | comparaison de deux acteurs (repli : sous-groupes et membres, puis dernière soumission ; écart de dates signalé ; appels candidats listés), ou contre tout un appel ; `acteurs=` : matrice (cache) |
| `groupes` | membres sourcés d'un groupe, groupes d'une Partie |
| `programme` | où et quand aller (jour, sujets, sujets suivis) |
| `pavillons` | contenu des programmes des pavillons, comptages, couverture |
| `mes_sujets` | alertes et dossiers suivis |
| `suivre` | créer une alerte (mots-clés ou dossier) ou l'arrêter |
| `deposer_url` | ajouter une page publique au corpus (date et titre vérifiés dans le contenu) |
| `etat_corpus` | statistiques du corpus |

Méthodes (prompts MCP, FR/EN, étapes définies une fois dans `mcp_server.METHODES`) : `point_negociation`,
`position_pays`, `preparer_rencontre`, `plaidoyer`, `decouvrir_sujet`, `quoi_de_neuf`. Outils retirés
(`mcp_server.RETIRED_TOOLS`) : un test vérifie qu'aucun n'est plus cité.

## 5. Skills du plugin Claude

`clients/claude-plugin/skills/` (en anglais, réponse dans la langue de l'utilisateur) : `cop31-protocol` (règles de
base), `fact-check`, `negotiation-update`, `eu-position`, `beginner-brief`, `decode-text`, `daily-brief`,
`find-your-way`, `plan-my-day`. Un test vérifie que chaque outil appelé par un skill existe. ChatGPT :
`clients/chatgpt-instructions.md` ; Le Chat : `clients/mistral-instructions.md`. Le plugin et les skills se
téléchargent depuis la page « Connect an LLM » du site ; Claude Code peut aussi les installer depuis le dépôt GitHub,
qui sert de marketplace (`.claude-plugin/marketplace.json` → `clients/claude-plugin` ;
`/plugin marketplace add akimx98/RAG_cop_climat` puis `/plugin install cop31@cop31`). Règles communes : une seule source, `clients/rules.md`. Le serveur MCP les envoie dans ses instructions (avec la
version courante des copies) ; `python scripts/sync_clients.py` les recopie dans le skill `cop31-protocol` et les
consignes à coller (ChatGPT, Le Chat, autres), et passe la version suivante (`plugin.json`, `marketplace.json`)
quand une copie a changé. Le LLM signale à l'utilisateur des skills ou consignes plus anciens ; `etat_corpus` donne
la version. Un test échoue si une copie n'est pas à jour. Les skills décrivent des démarches, sans noms de champs
de sortie (ceux-ci sont dans les descriptions d'outils, toujours à jour).

## 6. Tâches planifiées

| Tâche | Fil (conteneur `worker`) | Fréquence |
|---|---|---|
| Veille documentaire (sources du catalogue selon `watch_minutes`, indexation, embeddings, alertes) | `worker.watch_once` | boucle de 60 s ; un passage complet ≈ 20 min pendant la COP |
| Comptes rendus quotidiens de l'ENB en PDF (`enb-daily-pdf`, numéros suivants) | `worker.watch_once` | 1 h |
| Soumissions : index du portail + rattachement aux dossiers | fil dédié `worker.submissions_forever` | 30 min |
| Soumissions : téléchargement (récentes d'abord, puis le stock) | idem | 60 fichiers par passage, un passage par minute, dès le démarrage |
| Récapitulatif de mes sujets (Telegram, sans IA) | `worker.digest` → `digest.run` | 7 h et 19 h, heure d'Antalya |
| Textes de session (index Carbon Brief, fiches suivantes de docs.unfccc.int en secours, récupération, alertes) | `live_worker` | 5 min |
| Bluesky, extraction des positions | `live_worker` | 10 min |
| Analyse des soumissions de Parties récentes (si `COP_SUB_AUTO_ANALYSE=1`) | `live_worker` | 10 min (5 par passage) |
| État des dossiers, positions de référence Carbon Brief | `live_worker` | 1 h |
| Programme (planner), pavillons | `live_worker` | 6 h |
| Relevé SEORS par requête ordinaire (`agenda.sync_seors` : s'arrête sur l'anti-bot tant que l'adresse du serveur n'est pas autorisée par la CCNUCC) | `live_worker` | 1 h |
| Groupes de négociation | `live_worker` | 24 h |
| Briefs par dossier (si `COP_BRIEFS=1`) | `live_worker` | 7 h et 19 h |
| Bot Telegram, courrier entrant | fils dédiés | en continu |
| Chien de garde (`watchdog`) : battements de chaque boucle, arrêt anormal et arrêts anormaux en boucle (worker et site), boucle d’événements du site figée, documents bloqués en attente, curseur des alertes, récapitulatif manqué, sources sans succès depuis 24 h, mémoire et disque | fil du site **et** boucle du worker (chacun surveille l'autre) | 5 min ; bloquant → Telegram admin (à défaut e-mail), rappel toutes les 30 min, puis « rétabli » ; dégradé → 1 message par jour |
| Indexation des documents `pending` : PDF déposés sur le site (qui ne lit jamais de PDF : `extract.DEFER_PDF`), indexations interrompues (quarantaine après 2 tentatives) ; tout PDF est lu dans un sous-processus (`extract.extract_isolated`, un à la fois, repli sans tableaux, sinon `error`) | fil dédié `worker.indexing_forever` | 15 s |

Sonde externe : `GET /healthz/deep` répond 503 (noms des problèmes bloquants, sans détail) quand le worker est arrêté
ou bloqué ; `GET /healthz` reste la sonde de vie du conteneur du site. Un service d'uptime externe sur `/healthz/deep`
couvre le seul cas que le chien de garde interne ne voit pas : le site et le worker arrêtés en même temps (VPS).

## 7. Réglages

Tous lus dans `config.py` (variables d'environnement, fichier `deploy/.env` en production). Un test vérifie que ce
tableau couvre toutes les variables lues.

| Variable | Défaut | Effet |
|---|---|---|
| `COP_DATA_DIR`, `COP_DB`, `COP_CATALOG` | `data/`, `data/cop.sqlite3`, `catalog/` | emplacements des données, de la base, des catalogues |
| `COP_PUBLIC_BASE` | `http://localhost:8000` | adresse publique (liens des alertes et des outils) |
| `COP_SECRET` | (à définir) | signature des cookies et des liens ; obligatoire en production |
| `COP_SOURCE_URL` | dépôt GitHub du projet | dépôt public du code (licence MIT), affiché sur le site |
| `COP_CONTACT_EMAIL` | vide | destinataire du formulaire de contact (jamais affiché) ; vide → formulaire inactif |
| `COP_USER_AGENT`, `COP_HOST_DELAY`, `COP_MAX_DOWNLOAD_MB` | agent du projet, 1 s, 60 Mo | collecte polie |
| `OPENAI_API_KEY` | vide | embeddings et modèle de langage ; vide → recherche lexicale seule, pas d'IA |
| `COP_EMBED_PROVIDER`, `COP_EMBED_MODEL`, `COP_EMBED_DIM` | `openai` si clé, `text-embedding-3-small`, 512 | embeddings |
| `COP_EMBED_RESTRICTED` | 0 | 1 = envoyer aussi les documents internes à l'API d'embeddings |
| `COP_LLM` | 1 | 0 = aucun appel au modèle de langage |
| `COP_USAGE_TEXT_DAYS`, `COP_USAGE_KEEP_DAYS` | 30, 395 | statistiques d'usage : jours de conservation du texte des recherches sans résultat (comptes invités seulement), puis des événements |
| `COP_DEMO_KEEP_DAYS` | 90 | jours de conservation des questions de la démo « Essayer » |
| `COP_DEMO` | 1 | 0 = démo publique « Essayer » (/try) désactivée ; active seulement si une clé OpenAI est posée |
| `COP_DEMO_MODEL`, `COP_DEMO_EFFORT` | `gpt-6.1-terra`, `low` | modèle des questions sur le site, /try et /ask (API Responses, outils MCP en lecture seule, documents publics seulement) |
| `COP_DEMO_PER_SESSION`, `COP_DEMO_PER_IP_DAY`, `COP_DEMO_PER_ACCOUNT_DAY` | 3, 20, 3 | invités : questions par visiteur et par adresse IP et par jour ; comptes : questions par jour (jour d'Antalya), hors période renforcée |
| `COP_DEMO_BOOST_UNTIL`, `COP_DEMO_BOOST_PER_ACCOUNT_DAY` | `2026-11-21T23:59:59+03:00`, 40 | fin de COP : questions par compte et par jour jusqu'à cette date (sans fuseau : heure d'Antalya), puis retour à `COP_DEMO_PER_ACCOUNT_DAY` — **échu** depuis le 22/11/2026 (sans effet) |
| `COP_DEMO_DAILY_USD`, `COP_DEMO_ACCOUNT_DAILY_USD`, `COP_DEMO_BUDGET_USD` | 10, 40, 150 | plafonds de dépense : par jour pour les invités (`purpose='demo'`), par jour pour les comptes (`'demo-account'`), au total pour les deux ; distincts de `COP_LLM_BUDGET_USD` qu'ils n'entament pas |
| `COP_LLM_EXTRACT_MODEL`, `COP_LLM_WRITE_MODEL` | `gpt-5.4-mini`, `gpt-5.6-luna` | extraction et analyses ; briefs |
| `COP_LLM_BUDGET_USD` | 30 | plafond de dépense cumulée |
| `COP_LIVE` | 1 | 0 = pas de veille en direct (textes, positions, briefs, programme, pavillons, groupes, analyses) |
| `COP_LIVE_SINCE`, `COP_LIVE_UNDATED_SINCE` | 2026-11-01, 2026-11-07T18:00Z | sources prises pour l'extraction des positions |
| `COP_BRIEFS` | 0 | 1 = briefs IA par dossier (Telegram, page dossier, outil `dossier`) ; 0 = désactivés et masqués |
| `COP_WATCHDOG` | 1 | 0 = pas de fil de surveillance dans le site (battements, blocages, `/healthz/deep`) ; le worker vérifie toujours |
| `COP_BRIEF_HOURS` | 7,19 | heures des briefs, s'ils sont activés (fuseau `COP_TZ`) |
| `COP_SUB` | 1 | 0 = ni collecte ni analyse des soumissions |
| `COP_SUB_AUTO_ANALYSE` | 0 | 1 = analyser en tâche de fond les soumissions de Parties récentes (sinon à la demande seulement) |
| `COP_SUB_ANALYSE_SINCE` | 2026-01-01 | analyse automatique (si activée) des soumissions de Parties déposées depuis |
| `COP_SUB_PER_TICK` | 60 | fichiers de soumissions téléchargés par passage |
| `COP_SUB_EMBED_SINCE` | 2026-01-01 | soumissions vectorisées seulement depuis cette date |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_NAME` | vide | bot Telegram |
| `COP_TELEGRAM_RESTRICTED_SNIPPETS` | 0 | 1 = titres et extraits des documents internes dans Telegram |
| `COP_TZ`, `COP_DIGEST_HOURS` | `Europe/Istanbul`, 7,19 | fuseau de la COP, heures du récapitulatif Telegram |
| `COP_MAIL_DOMAIN`, `COP_MAIL_CERT`, `COP_MAIL_KEY`, `COP_MAIL_MAX_MB` | domaine public, certificat de Caddy, 40 Mo | courrier entrant (`COP_MAIL_DOMAIN` figé à `cop31.akimx.fr` en production, §10) |
| `COP_MAIL_PROVIDER`, `COP_MAIL_API_KEY`, `COP_MAIL_FROM` | vide | envoi des codes de connexion et du formulaire de contact (resend, brevo) |
| `MCP_ALLOWED_HOSTS` | vide | hôtes admis par le MCP (protection DNS rebinding) |
| `COP_LOCAL_ADMIN` | 1 | MCP en stdio : utilisateur administrateur local |

## 8. Confidentialité

- Documents internes (`acces='restreint'`) : visibles des seuls contributeurs ; jamais envoyés à l'API
  d'embeddings (sauf `COP_EMBED_RESTRICTED=1`), ni à un modèle de langage (analyse, positions, briefs) ;
  titres et extraits masqués dans Telegram par défaut.
- Notes de terrain : saisie retirée ; les notes existantes restent restreintes, jamais envoyées à un modèle.
- Personne ne dépose plus de document par le site, Telegram ou le courrier ; un administrateur peut déposer le PDF
  d'un texte de session bloqué par un anti-bot (onglet « Session texts » de la base de données).
- Aucune coordonnée personnelle n'est conservée dans les programmes (intervenants : noms tels que publiés).

## 9. Coûts

- Modèle de langage : plafond global `COP_LLM_BUDGET_USD` (30 $ par défaut), suivi dans `llm_usage`
  (`llm.usage_summary`). Côté serveur, le modèle sert aux tâches de fond qui tournent sans utilisateur : briefs
  par dossier (`gpt-5.6-luna`, ≈ 3 $ sur la première semaine ; désactivés depuis le 17/11, `COP_BRIEFS=0`) et extraction des positions dans les comptes rendus
  (`gpt-5.4-mini`, ≈ 2 $). `COP_LLM=0` les coupe : la clé ne sert plus qu'aux embeddings. Les analyses et
  comparaisons de soumissions demandées par MCP sont faites par le LLM du client (aucun coût serveur).
- Embeddings : `text-embedding-3-small` (512 dimensions), documents publics seulement ; vecteurs gardés en mémoire
  dans chaque processus (≈ 200 000 vecteurs aujourd'hui).
- Hébergement : VPS partagé (3,8 Go de RAM), conteneurs plafonnés (app 700 Mo, worker 600 Mo, mail 150 Mo).

## 10. Indexation et présentation publique

**Domaines.** `https://opencop.org` est l'adresse publique (`COP_PUBLIC_BASE`) ; `www.opencop.org` et l'ancienne
`cop31.akimx.fr` servent le même site (connecteurs MCP existants, QR codes, courrier entrant sur
`veille@cop31.akimx.fr`). Les trois noms sont dans la route Caddy et dans `deploy/edge/.env` sur le VPS : toujours
réinstaller la route avec les trois (PITFALLS « Domaines, edge »). Note :
[2026-11-25-domaine-opencop](notes/2026-11-25-domaine-opencop.md) ; redirection de l'ancien nom : issue #9.

**Ce que voient les moteurs et les LLM** (note [2026-11-25-indexation-langues](notes/2026-11-25-indexation-langues.md)) :

| Élément | Où | Règle |
|---|---|---|
| Adresse de référence, `og:url` | `base.html` | toujours sur `COP_PUBLIC_BASE` ; `?lang=fr` seulement s'il est dans l'adresse |
| Versions linguistiques | `base.html` (`hreflang`), `?lang=fr` | pages bilingues : en, fr, x-default ; documents (`/doc/`) : une seule version |
| Description par défaut | `base.html`, bloc `description` | **= accroche de `about.html`** (test `test_accroche_unique`) |
| Description d'une page | bloc `{% block description %}` du gabarit | ex. `doc.html` ; sinon la description par défaut |
| Image d'aperçu | `static/og-opencop.svg` → `.png` | `rsvg-convert og-opencop.svg -o og-opencop.png` après toute modification du SVG |
| Sitemap | `web.SITEMAP_PAGES`, `SITEMAP_GENERATED`, `sitemap_urls` | documents publics indexés non doublons seulement |
| Pages exclues | `web.NOT_INDEXED` (avec la raison), `web.ROBOTS` | pages personnelles, actions, secrets dans l'adresse, alias |
| Présentation pour LLM | `web.llms_text` (`/llms.txt`) | reprend l'accroche de `/about` ; liens vers des pages du sitemap ; aucun chiffre qui bouge |

**Garde-fous automatiques** (`tests/test_indexation.py`, donc bloquants pour `scripts/ship.sh`) :
toute route GET est classée (sitemap ou `NOT_INDEXED`) ; chaque page fixe du sitemap répond 200 en anglais et en
français ; jamais de document non public dans le sitemap ; liens de `llms.txt` vers des pages du sitemap ;
une seule accroche (`/about`, description par défaut, `llms.txt`).

**Quand on change…**

- *une page publique (ajout)* : route, puis `SITEMAP_PAGES` (ou `SITEMAP_GENERATED` + `sitemap_urls`) **ou**
  `NOT_INDEXED` avec la raison ; si elle est privée ou coûteuse, aussi `ROBOTS` ; un bloc `description` si la
  description par défaut ne lui convient pas ; un lien dans `llms_text` si elle aide à utiliser l'outil.
- *l'accroche ou la présentation* (`about.html`) : même texte dans la description par défaut de `base.html` et
  dans le résumé de `llms_text`, dans les deux langues ; l'image d'aperçu si le slogan change.
- *le nom affiché* : gabarits, `llms_text`, image d'aperçu, `MAIL_FROM` par défaut (`config.py`) et
  `COP_MAIL_FROM` en production, aide du bot (`telegram.py`), démo (`demo.py`) ; restent à renommer : serveur MCP,
  plugin, instructions clients (issue #11).
- *le domaine* : note du 25/11 (route aux noms complets, `COP_PUBLIC_BASE`, `MCP_ALLOWED_HOSTS`,
  `COP_MAIL_DOMAIN` figé, `ship.sh`) ; le reste suit `COP_PUBLIC_BASE`.
- *la COP en cours* (généralisation à toutes les COP) : ligne « Sur ce site » de `about.html`, `llms_text`
  (« The current edition covers… »), sous-titre de l'en-tête (`base.html`), titre par défaut ; contenu du corpus
  et programme : catalogue.

