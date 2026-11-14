# Assistant COP31 — architecture

*Rédigé le 6 novembre 2026. COP31 : Antalya, 9–20 novembre 2026 (présidence
turque, négociations présidées par l'Australie). Ouverture dans 3 jours.*

## 0. La contrainte qui commande tout : le calendrier

La COP ouvre lundi. Il n'y a pas le temps de construire « la bonne architecture »
puis de la remplir : il faut un **MVP utilisable dimanche soir**, puis
l'enrichir pendant la COP. Tout ce qui suit est donc ordonné par valeur livrée
par jour de travail, et **réutilise la pile déjà en production** pour
`RAG_programmes_Maths` (collecte avec reçus SHA-256, SQLite FTS5, MCP HTTP +
OAuth, edge Caddy partagé, plugin Claude). On ne réinvente rien de ce qui
marche ; on ajoute ce qui est propre à une COP : **le temps réel, les versions
successives des textes, et la hiérarchie de fiabilité**.

## 1. Vue d'ensemble

```
         SOURCES                         VPS (akimx.fr)                       CLIENTS
 ┌──────────────────────┐   ┌───────────────────────────────────────┐
 │ Socle (ingéré 1 fois) │   │  veilleur (cron 10–15 min, poli)      │
 │ CCNUCC, Paris, déci-  │──▶│   ETag/If-Modified-Since → reçu SHA   │
 │ sions COP/CMA, NDC,   │   │   → extraction → découpage par §      │
 │ droit UE, GIEC, UNEP  │   │   → rattachement point d'agenda +     │
 ├──────────────────────┤   │     version (lignée)                  │
 │ Flux live COP31       │──▶│   → embeddings (API) → index          │      ┌──────────────────┐
 │ projets de texte(AUV),│   │   → notification si point suivi ──────┼─────▶│ ntfy / Telegram  │
 │ docs de session, Daily│   │                                       │      │ (téléphone)      │
 │ Programme, ENB, Conseil│  │  cop.sqlite3                          │      └──────────────────┘
 │ UE, Commission, prési-│   │   documents · passages(FTS5) · vecteurs│
 │ dence, Carbon Brief   │   │   versions · programme · glossaire    │      ┌──────────────────┐
 ├──────────────────────┤   │  data.duckdb (émissions, cibles NDC)   │      │ Claude (Entre-   │
 │ Données chiffrées     │──▶│                                       │      │ prise) + plugin  │
 │ Climate Watch, EDGAR, │   │  serveur MCP  /mcp  (OAuth)  ─────────┼─────▶│ skills COP       │
 │ AEE, Eurostat, CAT    │   │  /sources/…  (liens vers originaux)   │      ├──────────────────┤
 ├──────────────────────┤   │  /tableau    (page statique générée)  │─────▶│ ChatGPT (MCP en  │
 │ Dépôts manuels        │──▶│  brief du jour (7h00, généré, étiqueté)│     │ mode dév.)       │
 │ (docs de couloir)     │   └───────────────────────────────────────┘      ├──────────────────┤
 └──────────────────────┘                                                   │ navigateur/tél.  │
          Mac (puissant) : construction du socle, OCR, ré-indexations lourdes  └──────────────────┘
```

Le Mac construit ; le VPS sert et veille. Pendant la COP le Mac sera loin de
toi : **tout ce qui doit tourner en continu tourne sur le VPS**, et ce qui est
lourd (OCR, ré-indexation complète) peut attendre ou se lancer à distance.

## 2. Le cœur : une hiérarchie de fiabilité explicite

C'est ce qui rend l'outil crédible face à un négociateur aguerri, et ce qui
permet de débunker. Chaque document porte un **niveau**, et chaque réponse dit
de quel niveau vient chaque affirmation.

| Niv. | Nature | Exemples | Ce que ça prouve |
|---|---|---|---|
| **1** | Texte **adopté**, force juridique | Convention, Accord de Paris, décisions `x/CP.y`, `x/CMA.y`, règlements et directives UE au JO | Ce qui **est** le droit / l'engagement |
| **2** | Document officiel **non adopté** | Projets de texte (avec horodatage de version), rapports de synthèse NDC, NDC soumises, conclusions du Conseil UE, propositions de la Commission | Ce qui est **proposé** ou **déclaré** officiellement |
| **3** | Science et données de référence | GIEC AR6 (SPM), UNEP Emissions Gap 2026, Global Carbon Budget 2026, AEE, Climate Watch, EDGAR | Les **faits** physiques et chiffrés |
| **4** | Compte rendu de référence | Earth Negotiations Bulletin (IISD), Carbon Brief | Ce qui **s'est dit** en salle |
| **5** | Position, communiqué, plaidoyer | Déclarations d'États, de groupes (G77, AOSIS, LMDC…), ONG, presse | Ce qu'un acteur **affirme** — jamais une preuve |
| **G** | Généré par l'outil | Brief du jour, résumés | Rien : un point d'entrée vers les sources |

Règles qui en découlent, imposées par le serveur (dans ses `instructions`) et
par les skills :

- un projet de texte se cite **toujours** avec sa version et son heure
  (« projet du 14/11 à 18h, non adopté ») ;
- une affirmation de niveau 5 ne peut pas en démentir une de niveau 1–3 ;
- pas de source → « non trouvé dans le corpus », jamais une réponse de mémoire ;
- tout passage cité renvoie à l'original, à la page ou au paragraphe.

## 3. Modèle de données

Une seule base SQLite (FTS5 + vecteurs), comme pour les maths, plus une base
analytique pour les chiffres.

**`documents`** — `doc_id`, `titre`, `symbole` (ex. `FCCC/PA/CMA/2026/L.4`),
`organisation`, `niveau` (1–5, G), `session` (COP31, CMA8, CMP21, SB64…),
`point_agenda`, `parties` (pays/groupes concernés), `langue`,
`publie_le`, `recupere_le`, `url`, `sha256`, `acces` (`public` | `restreint`),
`lignee_id`, `version`, `remplace`.

**`passages`** — découpés **par paragraphe numéroté** quand le texte en a (les
décisions et projets se citent au paragraphe : « 1/CMA.5 §28 »), sinon par
page/section. Index FTS5 (accents ignorés, multilingue) + embeddings.

**`lignees`** — un point d'agenda = une suite de versions de texte. C'est ce qui
permet de répondre à « qu'est-ce qui a bougé depuis hier soir sur l'adaptation ? ».

**`programme`** — Daily Programme parsé : heure, salle, organe, point, type
(plénière, informelles, consultations de la présidence…).

**`glossaire`** — sigles et notions (NCQG, GGA, MWP, JTWP, art. 6.2/6.4, ETF/BTR,
bilan mondial, « shall / should / urges / encourages »…), rédigé pour les
novices, chaque entrée renvoyant à un texte de niveau 1–2.

**`data.duckdb`** — émissions par pays/année/gaz/secteur, cibles NDC structurées
(année, type, %, année de référence, conditionnel ou non), engagements
financiers. Le débunk chiffré passe par une **requête**, pas par une recherche
vectorielle : « la Chine émet plus que l'UE et les USA réunis » se vérifie sur
une table, pas sur des paragraphes.

**Embeddings** : par API (Mistral `mistral-embed` ou OpenAI
`text-embedding-3-small`), pas de modèle local sur le VPS. Raison mesurée dans
`RAG_programmes_Maths/docs/conventions-vps.md` : 3,7 Go de RAM, deux MCP qui
chargent déjà ~1,1 Gio chacun. Un troisième modèle local, c'est l'OOM. Le corpus
est public, donc l'envoyer à une API d'embeddings ne pose pas de problème (sauf
documents `restreint` : voir §8). Coût : quelques euros pour toute la COP.

## 4. Sources

### Socle (avant lundi, priorisé Europe)

1. **Droit onusien** : Convention-cadre, Accord de Paris, protocole de Kyoto
   (pour mémoire) ; décisions-clés COP21 → COP30 : Paris rulebook (Katowice,
   Glasgow), bilan mondial (1/CMA.5), NCQG (Bakou), décisions de Belém ;
   ordre du jour provisoire annoté de COP31/CMA8/CMP21 et SB64.
2. **Engagements** : registre des NDC (NDC 3.0, horizon 2035), rapport de
   synthèse NDC 2026, BTR. Priorité : UE et ses États membres, puis grands
   émetteurs et pays dont on entendra parler (Chine, Inde, USA, Brésil,
   Turquie, Australie, Arabie saoudite, AOSIS).
3. **UE** : NDC de l'UE 2035, loi européenne sur le climat et l'objectif 2040,
   paquet Fit for 55 (ETS, ETS2, CBAM, règlement sur la répartition de
   l'effort, LULUCF), **conclusions du Conseil Environnement préparant COP31**,
   mandat de négociation.
4. **France** : SNBC, PNIEC, positions publiques du MTE/MEAE pour COP31.
5. **Science** : GIEC AR6 (SPM des trois groupes + synthèse), UNEP Emissions
   Gap 2026, WMO State of the Climate 2026, Global Carbon Budget 2026 (souvent
   publié pendant la COP : à surveiller).
6. **Données** : Climate Watch (WRI), EDGAR, AEE/Eurostat, Climate Action
   Tracker (notations, étiquetées niveau 3 mais méthodologie propre à CAT).

### Flux live (pendant la COP)

| Source | Fréquence de relève | Niveau |
|---|---|---|
| Projets de texte « unedited versions » (page AUV de COP31) | 10 min | 2 |
| Documents de session officiels (`unfccc.int/documents`, filtre COP31/CMA8) | 15 min | 1–2 |
| Daily Programme (PDF quotidien) | matin + midi | — |
| Communiqués présidence (Türkiye) et présidence des négociations (Australie) | 30 min | 5 |
| Conseil de l'UE / Commission (presse, déclarations) | 30 min | 2 / 5 |
| Earth Negotiations Bulletin (résumé quotidien) | soir | 4 |
| Carbon Brief (fil COP) | 1 h | 4 |
| Dépôts manuels (toi) | à la demande | selon le document |

*Les adresses exactes (page AUV, flux RSS) sont à confirmer le jour J : la
CCNUCC les publie à l'ouverture. Le site de la CCNUCC est parfois derrière un
filtre anti-bot ; la collecte par navigateur (Playwright) du projet maths est
réutilisable mais coûteuse en RAM sur le VPS : à n'activer que si nécessaire.*

**Droits** : textes officiels → intégral. Presse et ENB → indexés pour la
recherche, mais le serveur ne renvoie qu'un extrait court + le lien (même
politique que les manuels dans le projet maths).

## 5. Serveur MCP — outils

Peu d'outils, chacun vérifiable. L'intelligence (débunk, brief) est dans les
skills, pas dans le serveur : un serveur bête et traçable est plus fiable.

| Outil | Rôle |
|---|---|
| `rechercher` | Recherche hybride (FTS5 + sémantique), filtres : niveau, session, point d'agenda, partie, période, langue |
| `lire_passage` | Texte exact d'un passage + contexte + lien vers l'original à la bonne page |
| `document` | Métadonnées d'un document ou d'un symbole, et toutes ses versions |
| `nouveautes` | Ce qui est entré dans le corpus depuis T (filtrable par point d'agenda) |
| `comparer_versions` | Diff paragraphe par paragraphe entre deux versions d'un projet : ajouts, suppressions, **crochets et options** apparus ou levés |
| `engagements` | Cibles et engagements d'un pays/groupe (NDC, droit UE), structurés et sourcés |
| `donnees` | Série chiffrée (émissions, par habitant, cumulées, part mondiale…) avec source et millésime |
| `programme` | Ce qui se passe aujourd'hui / à telle heure / sur tel sujet, et où |
| `glossaire` | Définition d'un sigle ou d'une notion, avec renvoi au texte |
| `deposer` | Ajouter un document (URL ou fichier) avec niveau et accès — réservé à ton jeton |

Les `instructions` du serveur portent la discipline de citation et la
hiérarchie de fiabilité : ainsi **ChatGPT, qui n'a pas les skills, les reçoit
quand même**.

## 6. Plugin Claude — skills

Deux publics, donc deux registres, mais les mêmes sources.

- **`verifier-affirmation`** — décompose l'affirmation, va chercher niveaux 1–3,
  rend un verdict (exact / trompeur / faux / invérifiable dans le corpus), les
  citations, et **une réplique en deux phrases** utilisable en réunion.
- **`point-negociation`** — état d'un point d'agenda : dernière version, ce qui
  a bougé (via `comparer_versions`), options entre crochets, qui pousse quoi
  (ENB, niveau 4), écart avec la position de l'UE.
- **`brief-novice`** — l'enjeu en 5 minutes : de quoi on parle, pourquoi c'est
  dur, ce que veut l'UE, ce que veulent les autres, et **3 questions de fond à
  poser** pour sortir d'un débat technique opaque.
- **`decoder-texte`** — lire un paragraphe de décision : force normative
  (*shall* vs *should* vs *encourages*), renvois aux décisions antérieures,
  ce que ça change concrètement.
- **`position-ue`** — position de l'UE (conclusions du Conseil, NDC, droit
  applicable) sur un sujet, et sa cohérence avec ce que l'UE fait chez elle :
  c'est là qu'arrivent les attaques en salle.
- **`brief-du-jour`** — synthèse de la nuit : nouvelles versions, plénières du
  jour, points chauds.
- **`se-reperer`** — programme, salles, pavillons UE et France, side events.

Côté **ChatGPT Plus** : connecteur MCP via le mode développeur (à vérifier sur
ton compte), plus un GPT ou un projet qui reprend les instructions condensées
des skills. Côté **Claude Entreprise** : un admin de l'organisation doit
autoriser le connecteur personnalisé.

## 7. Temps réel : veille et alertes

- **Veilleur** sur le VPS (timer systemd ou conteneur dédié), requêtes
  conditionnelles, rythme poli, journal de chaque relève.
- **Rattachement automatique** d'un nouveau projet à sa lignée (symbole,
  titre, point d'agenda) ; en cas de doute, il crée une nouvelle lignée plutôt
  que de mal rattacher.
- **Alertes** sur les points que tu suis (« nouvelle version du texte sur
  l'adaptation, 3 paragraphes modifiés, crochets levés au §12 ») via **ntfy**
  ou un bot Telegram. Dans les couloirs, c'est ce qui sert le plus.
- **Brief de 7h00** (heure d'Antalya) généré, étiqueté `G`, indexé avec liens
  vers les sources.
- **Tableau** `/tableau` : page statique régénérée à chaque relève (nouveautés,
  versions par point d'agenda, programme du jour). Pas d'application à
  maintenir, lisible sur téléphone, en cache si le Wi-Fi du site sature.

## 8. Confidentialité : ce qui ne doit pas entrer

Des contributeurs peuvent avoir accès à des documents **non publics**. Avant
d'en déposer un seul dans cet outil — et donc de le faire lire par Claude ou
ChatGPT — **vérifier les règles de qui l'a fourni**. L'architecture
prévoit un accès `restreint` (stocké sur le VPS, jamais renvoyé sans ton jeton,
pas d'embeddings par API tiers), mais la question n'est pas technique.

Par défaut : **seuls les documents publics** entrent dans le corpus.

## 9. Application web

Pas avant la fin de la COP. Claude (application mobile incluse) + MCP couvre
les usages, et le tableau statique couvre le « se repérer ». Une vraie
application, avec modes négociateur/novice, n'a de sens que si l'outil doit
servir à d'autres personnes qui n'ont pas de compte Claude ou ChatGPT : à
décider après la COP, au vu de l'usage réel.

## 10. Plan

### Avant lundi (J-3 → J0) — MVP

1. Nouveau dépôt calqué sur `RAG_programmes_Maths` : collecte avec reçus,
   extraction, FTS5, serveur MCP + OAuth, conventions VPS (nom Compose,
   alias réseau, plafond mémoire, route dans `mcp-edge`). Domaine :
   `mcp-cop.akimx.fr`.
2. Socle : ~150 documents (droit onusien, décisions COP21–30, NDC UE et
   grands émetteurs, droit UE, conclusions du Conseil, SPM GIEC, Emissions Gap).
   Découpage par paragraphe numéroté.
3. Outils : `rechercher`, `lire_passage`, `document`, `nouveautes`, `glossaire`.
4. Plugin : `verifier-affirmation`, `brief-novice`, `point-negociation` (sans
   diff au début).
5. Test sur 20 questions réelles (« que dit Paris sur le 1,5 °C ? », « la NDC
   de l'UE est-elle alignée sur 1,5 ? », « qu'a-t-on décidé à Bakou sur la
   finance ? ») avant de partir.

### Semaine 1 de la COP

6. Veilleur sur les projets de texte et documents de session ; lignées de
   versions ; `comparer_versions`.
7. Daily Programme → `programme` ; alertes ntfy ; brief de 7h00.
8. ENB et Carbon Brief (niveau 4).

### Semaine 2

9. `data.duckdb` + outils `engagements` et `donnees` pour le débunk chiffré.
10. `deposer` pour les documents récupérés sur place.
11. Tableau statique.

### Après la COP

12. Bilan d'usage, puis décision sur l'application web et l'ouverture à
    d'autres contributeurs.

## 10 bis. État au 6 novembre 2026 (v0.1)

- Implémenté : collecte (doc, crawl, RSS, énumération de cotes ODS), extraction PDF/HTML/Word, découpage par
  paragraphe numéroté avec décision (« 1/CMA.5 §28 »), lignées de versions et diff, recherche hybride,
  données d'émissions (OWID), MCP (13 outils), tableau de bord multi-utilisateurs (sujets suivis, alertes,
  dépôt de documents internes, admin), bot Telegram (alertes, /suivre, /cherche, dépôt par transfert),
  récapitulatif du matin, plugin Claude (8 skills), instructions ChatGPT, déploiement Docker + edge Caddy.
- Écart à l'architecture : un seul processus web sert le tableau de bord ET le MCP (sur un même domaine), et
  l'accès MCP se fait par une adresse personnelle secrète (`/u/<jeton>/mcp`) plutôt que par OAuth — plus rapide
  à mettre en place pour des collègues avant lundi ; OAuth (Auth0, comme le corpus maths) reste possible.
- unfccc.int est derrière un anti-bot Imperva : non contourné. Les documents à cote passent par l'ODS de l'ONU ;
  les versions « unedited » et le Daily Programme se déposent à la main. Certains PDF de
  `unfccc.int/sites/default/files/resource/` passent toutefois sans challenge et sont collectés.
- **En production depuis le 6 novembre 2026** : https://cop31.akimx.fr (VPS partagé, conteneurs `cop31-app` et
  `cop31-worker`, TLS par l'edge Caddy), bot @AssistantCOP31Bot. Corpus initial : ~1 840 documents, ~163 000
  passages, embeddings OpenAI (documents publics seulement).
- Recherche, réglages après tests sur des questions réelles : paliers lexicaux (expression exacte → tous les
  mots → n'importe lequel), à pertinence égale priorité au texte adopté (niveau 1), synonymes FR/EN (CDN/NDC,
  UE/EU, GIEC/IPCC, MACF/CBAM, noms de pays), extrait centré sur l'expression cherchée, titres réparés
  (versions FR/ES sans titre, « Télécharger le rapport complet »).
- Liens directs vers chaque passage : `lien_source` (original ouvert à la page pour un PDF, paragraphe surligné
  par fragment de texte `#:~:text=` pour une page web), `lien_page` (copie archivée à la page), `lien_passage`
  (passage surligné dans le tableau de bord, seul lien pour un document interne). Exposés par le MCP, la
  recherche web et le bot.

## 10 ter. Veille en direct des négociations (7 novembre 2026)

Constat : unfccc.int (pages, RSS et désormais PDF `/resource/`) et l'ENB bloquent les clients automatisés ; une
bibliothèque ne suffit pas à suivre les textes et les positions heure par heure. Quatre briques :

1. **Textes** (`live.py`) : l'index public de Carbon Brief (feuille Google « Texts », lue toutes les 5 min, accord
   de Carbon Brief pour un usage interne cité) annonce chaque document de session (cote, point, heure, lien PDF,
   pages, crochets, options). Récupération polie : adresse du PDF, puis ODS par la cote ; sinon « à récupérer » :
   un contributeur l'ouvre dans son navigateur et le dépose (page Textes, bot, courrier). Rattachement
   automatique (cote, adresse, nom de fichier), versions, diff, alerte Telegram aux abonnés du dossier.
2. **Dossiers** (`catalog/dossiers.yaml`, 17 dossiers alignés sur les thèmes de Carbon Brief) : page par dossier
   (état, textes et courbe des crochets, brief, positions, notes, corpus récent, Bluesky), suivi par utilisateur.
3. **Positions** (`positions.py`) : 344 positions de référence (« Who wants what », Carbon Brief) + extraction par
   `gpt-5.4-mini` dans les sources publiques récentes (lettres reçues, ENB/TWN/ECO, Carbon Brief, Climate Home,
   Bluesky) ; une position n'est gardée que si sa citation figure mot pour mot dans la source. **Briefs**
   (`briefs.py`) par `gpt-5.6-luna` à 7 h et 19 h (heure d'Antalya), seulement s'il y a du nouveau, sources
   numérotées. Plafond de dépense (`COP_LLM_BUDGET_USD`, 30 $), suivi dans `llm_usage`.
4. **Terrain** : `/note GGA …` (bot, dictée au clavier), formulaire de la page dossier, outil MCP `noter` ; notes
   restreintes, jamais envoyées à un modèle.

Bot : `/dossiers`, `/dossier`, `/suivi`, `/textes`, `/note`, `/brief`. MCP : `dossiers`, `dossier`,
`textes_en_cours`, `positions`, `noter`. Comptes Bluesky suivis : `catalog/bluesky.yaml`.

## 11. Coûts

| Poste | Estimation |
|---|---|
| VPS existant | 0 € (éventuellement +RAM pour deux semaines si le veilleur navigateur est nécessaire : ~10 €) |
| Embeddings par API | < 5 € |
| Génération du brief quotidien (API Claude) | ~10–20 € sur la COP |
| ntfy (auto-hébergé) ou Telegram | 0 € |
| **Total** | **< 40 €** |
