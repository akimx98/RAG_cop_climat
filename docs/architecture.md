# COP Tracker — documentation de référence

Corpus vérifiable, veille en temps réel et alertes pour suivre les négociations de la COP31 (Antalya,
9–20 novembre 2026). Outil indépendant, gratuit et non commercial, sans lien avec la CCNUCC ni avec une délégation.
Ce document décrit l'état actuel ; l'historique de construction est dans [historique.md](historique.md),
l'exploitation (commandes, déploiement, courrier) dans le [README](../README.md). Réglages : §7.

## 1. Vue d'ensemble

```
sources publiques (ODS/ONU, portail des soumissions CCNUCC, EUR-Lex, Conseil UE, GIEC, PNUE, ENB, Carbon Brief,
cop31.tr, OWID…) + dépôts des contributeurs (tableau de bord, Telegram, courrier)
   │  worker : veille documentaire, veille en direct (textes, positions, briefs), soumissions, groupes, pavillons
   ▼
data/cop.sqlite3 (une seule base SQLite : FTS5 + vecteurs en mémoire)
   │
   ├── tableau de bord web   /                 dossiers, textes, recherche, alertes, dépôt, admin (FR/EN)
   ├── MCP                   /u/<jeton>/mcp    Claude (plugin clients/claude-plugin) et ChatGPT
   └── bot Telegram          alertes, dossiers, recherche, notes de terrain, dépôt de PDF
```

Trois conteneurs sur le VPS : `app` (web + MCP), `worker` (toutes les tâches planifiées, bot Telegram), `mail`
(courrier entrant, port 25).

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
| Négociations en direct | `live.py`, `positions.py`, `briefs.py` | `live_texts` (index Carbon Brief), `dossier_status`, `positions`, `extraction_log`, `social_posts`, `field_notes` (notes de terrain, restreintes), `briefs`, `dossier_follows`, `llm_usage` |
| Soumissions officielles | `soumissions.py` | `sub_calls`, `sub_items` (dont ISO3 de l'auteur), `sub_actors`, `sub_call_dossiers`, `sub_analysis`, `sub_comparisons` |
| Groupes de négociation | `groupes.py` + `catalog/groupes.yaml` | `grp_groups`, `grp_members` |
| Programme et pavillons | `agenda.py`, `pavillons.py`, `pavillons_ext.py` | `agenda_events`, `agenda_overview`, `pav_events` + `pav_fts` |
| Courrier entrant | `mailin.py` | `mail_inbox` |

Catalogues versionnés : `catalog/sources/*.yaml` (sources de la veille), `catalog/dossiers.yaml` (17 dossiers de
négociation et leurs alias), `catalog/groupes.yaml`, `catalog/glossaire.yaml`, `catalog/bluesky.yaml`.

### Corpus

Collecte polie (délai par hôte, requêtes conditionnelles, anti-bot jamais contourné : unfccc.int passe par l'ODS
de l'ONU ou par un dépôt manuel), extraction PDF/HTML/Word, découpage par paragraphe numéroté (« 1/CMA.5 §28 »),
lignées de versions et diff, recherche hybride (paliers lexicaux + sens, priorité au texte adopté à pertinence
égale), liens directs vers chaque passage (`lien_source`, `lien_page`, `lien_passage`).

### Négociations en direct

Index public de Carbon Brief (textes de session, positions de référence « Who wants what », cité) ; récupération
des textes par leur adresse puis par l'ODS, sinon « à récupérer » (dépôt manuel) ; positions extraites des sources
publiques récentes — comptes rendus de négociation (TWN, Climate Home, Carbon Brief, ENB, ECO) et déclarations
officielles (G77, AOSIS, présidence COP31, DG CLIMA), pas la presse généraliste (`positions.POSITION_SOURCES`) —,
gardées seulement si la citation figure mot pour mot dans la source ; briefs IA par dossier à
7 h et 19 h, désactivés par défaut (`COP_BRIEFS`) ; notes de terrain restreintes, jamais
envoyées à un modèle.

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

### Programme et pavillons

Programme : planner public (side events, réunions officielles, événements hors site), relevés SEORS importés à la
main, lien SEORS de vérification par side event. Pavillons : programmes de cop31.tr (Blue et Green Zones), side
events, pavillons publiés ailleurs (`pavillons_ext.py`) ; recherche par mots et par sens, comptages par pavillon.

## 4. Outils MCP

Consignes du serveur : moins de 1 800 caractères (les clients tronquent au-delà), règles d'abord, puis une ligne
par famille d'outils ; le détail est dans la description de chaque outil. Un test compare ce tableau au registre.

| Outil | Rôle |
|---|---|
| `rechercher` | recherche hybride dans le corpus (filtres niveau, type, session, partie, groupe, dates) |
| `lire_passage` | texte exact d'un passage et de ses voisins |
| `document` | métadonnées et versions d'un document |
| `comparer_versions` | ce qui a changé entre deux versions d'un texte (diff, crochets, options) |
| `nouveautes` | documents entrés récemment |
| `donnees` | chiffres d'émissions |
| `glossaire` | sigles et notions |
| `dossiers` | vue d'ensemble des dossiers ; `textes=true` : derniers textes annoncés |
| `dossier` | un dossier : état, textes, brief, positions, positions écrites, soumissions liées, notes |
| `positions` | qui veut quoi (acteur, dossier, période) |
| `noter` | note de terrain (restreinte) |
| `soumissions` | appels, qui a soumis quoi, par dossier ou par groupe ; `document=` : analyse |
| `comparer_soumissions` | comparaison de deux acteurs, ou contre tout un appel ; `acteurs=` : matrice (cache) |
| `groupes` | membres sourcés d'un groupe, groupes d'une Partie |
| `programme` | où et quand aller (jour, sujets, sujets suivis) |
| `pavillons` | contenu des programmes des pavillons, comptages, couverture |
| `mes_sujets` | alertes et dossiers suivis |
| `suivre` | créer une alerte (mots-clés ou dossier) ou l'arrêter |
| `deposer_url` | ajouter une page publique au corpus |
| `etat_corpus` | statistiques du corpus |

Méthodes (prompts MCP, FR/EN, étapes définies une fois dans `mcp_server.METHODES`) : `point_negociation`,
`position_pays`, `preparer_rencontre`, `plaidoyer`, `decouvrir_sujet`, `quoi_de_neuf`. Outils retirés
(`mcp_server.RETIRED_TOOLS`) : un test vérifie qu'aucun n'est plus cité.

## 5. Skills du plugin Claude

`clients/claude-plugin/skills/` : `protocole-cop31` (règles de base), `verifier-affirmation`, `point-negociation`,
`position-ue`, `brief-novice`, `decoder-texte`, `brief-du-jour`, `se-reperer`. Un test vérifie que chaque outil
appelé par un skill existe. ChatGPT : `clients/chatgpt-instructions.md`. Le plugin et les skills se téléchargent
depuis la page « Claude / ChatGPT » du tableau de bord.

## 6. Tâches planifiées

| Tâche | Fil (conteneur `worker`) | Fréquence |
|---|---|---|
| Veille documentaire (sources du catalogue selon `watch_minutes`, indexation, embeddings, alertes) | `worker.watch_once` | boucle de 60 s ; un passage complet ≈ 20 min pendant la COP |
| Soumissions : index du portail + rattachement aux dossiers | fil dédié `worker.submissions_forever` | 30 min |
| Soumissions : téléchargement (récentes d'abord, puis le stock) | idem | 60 fichiers par passage, un passage par minute, dès le démarrage |
| Récapitulatif du matin (Telegram) | `worker.morning_digest` | 7 h, heure d'Antalya |
| Textes de session (index Carbon Brief, récupération, alertes) | `live_worker` | 5 min |
| Bluesky, extraction des positions | `live_worker` | 10 min |
| Analyse des soumissions de Parties récentes (si `COP_SUB_AUTO_ANALYSE=1`) | `live_worker` | 10 min (5 par passage) |
| État des dossiers, positions de référence Carbon Brief | `live_worker` | 1 h |
| Programme (planner), pavillons | `live_worker` | 6 h |
| Groupes de négociation | `live_worker` | 24 h |
| Briefs par dossier (si `COP_BRIEFS=1`) | `live_worker` | 7 h et 19 h |
| Bot Telegram, courrier entrant | fils dédiés | en continu |

## 7. Réglages

Tous lus dans `config.py` (variables d'environnement, fichier `deploy/.env` en production). Un test vérifie que ce
tableau couvre toutes les variables lues.

| Variable | Défaut | Effet |
|---|---|---|
| `COP_DATA_DIR`, `COP_DB`, `COP_CATALOG` | `data/`, `data/cop.sqlite3`, `catalog/` | emplacements des données, de la base, des catalogues |
| `COP_PUBLIC_BASE` | `http://localhost:8000` | adresse publique (liens des alertes et des outils) |
| `COP_SECRET` | (à définir) | signature des cookies et des liens ; obligatoire en production |
| `COP_SOURCE_URL` | vide | dépôt public du code, affiché s'il est renseigné |
| `COP_USER_AGENT`, `COP_HOST_DELAY`, `COP_MAX_DOWNLOAD_MB` | agent du projet, 1 s, 60 Mo | collecte polie |
| `OPENAI_API_KEY` | vide | embeddings et modèle de langage ; vide → recherche lexicale seule, pas d'IA |
| `COP_EMBED_PROVIDER`, `COP_EMBED_MODEL`, `COP_EMBED_DIM` | `openai` si clé, `text-embedding-3-small`, 512 | embeddings |
| `COP_EMBED_RESTRICTED` | 0 | 1 = envoyer aussi les documents internes à l'API d'embeddings |
| `COP_LLM` | 1 | 0 = aucun appel au modèle de langage |
| `COP_LLM_EXTRACT_MODEL`, `COP_LLM_WRITE_MODEL` | `gpt-5.4-mini`, `gpt-5.6-luna` | extraction et analyses ; briefs |
| `COP_LLM_BUDGET_USD` | 30 | plafond de dépense cumulée |
| `COP_LIVE` | 1 | 0 = pas de veille en direct (textes, positions, briefs, programme, pavillons, groupes, analyses) |
| `COP_LIVE_SINCE`, `COP_LIVE_UNDATED_SINCE` | 2026-11-01, 2026-11-07T18:00Z | sources prises pour l'extraction des positions |
| `COP_BRIEFS` | 0 | 1 = briefs IA par dossier (Telegram, page dossier, outil `dossier`) ; 0 = désactivés et masqués |
| `COP_BRIEF_HOURS` | 7,19 | heures des briefs, s'ils sont activés (fuseau `COP_TZ`) |
| `COP_SUB` | 1 | 0 = ni collecte ni analyse des soumissions |
| `COP_SUB_AUTO_ANALYSE` | 0 | 1 = analyser en tâche de fond les soumissions de Parties récentes (sinon à la demande seulement) |
| `COP_SUB_ANALYSE_SINCE` | 2026-01-01 | analyse automatique (si activée) des soumissions de Parties déposées depuis |
| `COP_SUB_PER_TICK` | 60 | fichiers de soumissions téléchargés par passage |
| `COP_SUB_EMBED_SINCE` | 2026-01-01 | soumissions vectorisées seulement depuis cette date |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_NAME` | vide | bot Telegram |
| `COP_TELEGRAM_RESTRICTED_SNIPPETS` | 0 | 1 = titres et extraits des documents internes dans Telegram |
| `COP_TELEGRAM_ALLOW_UPLOAD` | 1 | dépôt de documents par le bot |
| `COP_TZ`, `COP_DIGEST_HOUR` | `Europe/Istanbul`, 7 | fuseau de la COP, heure du récapitulatif |
| `COP_MAIL_DOMAIN`, `COP_MAIL_CERT`, `COP_MAIL_KEY`, `COP_MAIL_MAX_MB` | domaine public, certificat de Caddy, 40 Mo | courrier entrant |
| `COP_MAIL_PROVIDER`, `COP_MAIL_API_KEY`, `COP_MAIL_FROM` | vide | envoi des codes de connexion par e-mail (resend, brevo) |
| `MCP_ALLOWED_HOSTS` | vide | hôtes admis par le MCP (protection DNS rebinding) |
| `COP_LOCAL_ADMIN` | 1 | MCP en stdio : utilisateur administrateur local |

## 8. Confidentialité

- Documents internes (`acces='restreint'`) : visibles des seuls contributeurs ; jamais envoyés à l'API
  d'embeddings (sauf `COP_EMBED_RESTRICTED=1`), ni à un modèle de langage (analyse, positions, briefs) ;
  titres et extraits masqués dans Telegram par défaut.
- Notes de terrain : restreintes, jamais envoyées à un modèle.
- Les invités consultent le corpus public et ne peuvent pas déposer.
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
