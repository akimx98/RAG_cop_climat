# 2026-11-27 — Revue d'architecture : consolider après la COP31

*Taille : L (programme de chantiers ; chaque phase a sa propre note M avant le code) · Statut : validé (contrat
des rôles du 27/11 : la technique relève de Claude ; décisions produit §7 à Akim)*
*Fichiers concernés : `worker.py`, `live_worker.py`, `alerts.py`, `watchdog.py`, `monitor.py`, `ingest.py`,
`live.py`, `web_live.py`, `usage.py`, `db.py`, `briefs.py`, `soumissions.py`, `catalog/sources/*.yaml`,
`.github/workflows/`*

## Problème

Depuis le 22/11, chaque correctif en révèle d'autres : 30 issues `suite` créées en 3 jours, 11 fermées. Les
incidents des 25–27/11 (lenteur, base coincée, mémoire, courses à la connexion, fausses alertes) ont été corrigés
un par un, par des **protections** (fils, verrous, sémaphores, replis), sans toucher aux trois contraintes qui les
produisent. Akim n'est pas développeur et servait de relais entre sessions, alertes et déploiements.

Cette note prend du recul : quelles contraintes, quelles simplifications, dans quel ordre. Elle s'appuie sur trois
relevés du 27/11 (mesures en production en lecture seule, carte du code, causes racines des incidents) et sur le
récapitulatif de la session « Messages d'erreur Telegram ».

## Faits mesurés

| Fait | Valeur | Comment mesuré |
|---|---|---|
| Comptes / actifs sur 7 et 30 jours | 19 / 4 / 4 (canal : MCP 4, web 2, Telegram 1) | `usage_events`, 27/11 13 h UTC |
| Appels d'outils MCP depuis le 18/11 | 467 ; `rechercher` 139, `lire_passage` 97, `comparer_versions` 59 ; `soumissions`, `comparer_soumissions`, `groupes` : 0 | `usage_events` |
| Abonnements actifs | 22 (2 comptes) ; Telegram : 6 commandes, 2 comptes | `subscriptions`, `usage_events` |
| Questions sur le site (/try, /ask) | 38 réponses en tout, 12 conversations | `demo_answers`, `conversations` |
| Trafic web | 99,8 % des événements journalisés = pages `/doc` anonymes (robots : 30 000 à 138 000 par jour) | `usage_events` (uid nul), Caddy |
| Nouveaux documents par jour depuis le 20/11 | 41, 30, 28, 50, 11, 23, 25 | session « Messages d'erreur Telegram » |
| VPS | 2 cœurs, charge 0,6 ; RAM 3,8 Go dont 2,0 disponibles ; swap 4 Go (1,5 utilisé, surtout par rag-embed) | `free`, `uptime`, 27/11 |
| Mémoire anonyme / plafond | site 425 / 700 Mo ; worker 90 / 600 ; courrier 39 / 150 | cgroup `memory.stat` |
| Base | 965 Mo + WAL 64 Mo ; passages 345 Mo, vecteurs 283 Mo, FTS 148 Mo, `usage_events` 30 Mo (253 572 lignes) | `dbstat`, base ouverte en lecture seule |
| Plantages | 130 SIGSEGV du 19 au 25/11 (extraction PDF avant isolement), aucun depuis | `journalctl -u docker` |
| Lenteur restante | `/database` p90 7,3 s, max 26,5 s (73 requêtes en 4 h) ; autres pages p90 < 0,7 s | Caddy, 4 h avant 13 h UTC |
| Préchauffage des pages dossier | ≈ 30 s d'un cœur dans le processus du site à chaque nouveau passage indexé | session « Messages d'erreur Telegram » |

Constats de la carte du code (fichier:ligne dans le relevé, vérifiés par grep) :

- `alerts.run` (curseur `alerts_last_doc_id`) est appelé par **4 chemins dans 2 processus** : boucle du worker,
  fil `mail`, outil MCP `deposer_url`, page admin. `alerts.send_pending` lit, envoie, puis marque : **double envoi
  Telegram possible**.
- `watchdog.check` réécrit `watchdog_state` depuis le worker **et** le site.
- `live.tidy_texts` écrit `documents.version` / `lineage_key` hors de `ingest._write_lock`, pendant que
  `_index_document` calcule `version = prev + 1` dans 4 autres fils.
- Schéma créé et migré par 10 modules, au démarrage de chaque processus, sans numéro de version.
- `usage.record` fait un `INSERT` + `commit` par page vue, robots compris : environ 100 000 écritures par jour
  pour rien.
- Code porté pour des fonctions coupées : briefs IA (`COP_BRIEFS=0` depuis le 17/11), analyse des soumissions
  par le serveur (`COP_SUB_AUTO_ANALYSE=0`), notes de terrain (`live.add_note`), `COP_DEMO_BOOST_UNTIL` échu.
- La collecte tourne encore au rythme de la COP : textes en direct toutes les 5 min, SEORS toutes les heures
  (bloqué par l'anti-bot), sources ODS « live » toutes les 15 min.

Hypothèses non vérifiées :
- Une seule tâche d'écriture de fond dans le worker suffit au volume actuel (≈ 30 documents par jour). À mesurer :
  durée d'un passage de veille hors COP.
- `/database` est lent sur le palier FTS « n'importe quel mot » (#14, en partie traité par 043d6f9).

## Diagnostic : trois contraintes, une cause

1. **Trop d'écrivains concurrents sur une base SQLite à écrivain unique.** Le site, le worker (7 fils) et le
   courrier écrivent la même base, parfois la même ligne ou la même clé `meta`. Chaque passage de code dans un
   fil a révélé une course que la boucle masquait. SQLite n'est pas en cause au volume actuel ; c'est le nombre
   d'écrivains.
2. **Le site fait du travail de fond.** Préchauffage, alertes déclenchées depuis une page ou un outil, chien de
   garde : le processus qui doit répondre vite partage le GIL et la base avec du calcul et des écritures.
3. **Une surface taillée pour la COP.** 52 modules et environ 20 000 lignes, pour 4 utilisateurs actifs après la
   COP, plusieurs fonctions coupées mais encore portées.

La cause commune est une construction rapide pendant la COP, où chaque fonction a ajouté son fil, son schéma et
ses écritures. **Le remède est de retirer de la concurrence et de la surface, pas d'ajouter des protections.**

## Principes retenus (pour toute session à venir)

- **Le site lit, le worker écrit.** Le site n'écrit que ce qui vient d'un utilisateur : compte, connexion,
  abonnements, conversations. Tout le reste (alertes, calculs lourds, surveillance) relève du worker.
- **Un écrivain par donnée.** Toute clé `meta`, tout curseur et tout état a un seul fil propriétaire. Les autres
  le *demandent*, par un drapeau ou une file, mais ne l'écrivent pas.
- **Retirer avant d'ajouter.** Une fonction coupée depuis plus de deux semaines est retirée avec son code, ses
  tables et ses tests (une fonction visible : avec l'accord d'Akim).
- **Rythme hors session.** Entre deux COP, la collecte tourne au rythme des publications réelles.

## Écartés (et pourquoi)

- **Agrandir le VPS ou les plafonds** : 2 Go disponibles, site à 61 % de son plafond. La mémoire n'est pas la
  contrainte.
- **Passer à PostgreSQL** : migration lourde et nouveau service à exploiter, pour un volume qui ne le demande
  pas. Le problème vient du nombre d'écrivains, que l'on réduit.
- **Un service de recherche séparé** : 200 Mo de vecteurs tiennent dans le site.
- **Réécrire** : non. On procède par phases, chacune livrable, mesurée et réversible.

## Avancement

Tenu à jour à chaque livraison (la tâche programmée « OpenCOP — reprise de la consolidation » le lit).

| Phase | Note | Statut |
|---|---|---|
| 0 — rythme hors session, sonde externe, usage des robots | [2026-11-27-phase0-hors-session](2026-11-27-phase0-hors-session.md) | livré (27/11) |
| 1 — un écrivain par donnée | [2026-11-27-phase1-un-ecrivain](2026-11-27-phase1-un-ecrivain.md) | livré (27/11) |
| 2 — le site ne calcule plus en fond | — | à faire |
| 3 — schéma unique et code mort | — | à faire |
| 5 — surface (décisions §7 acceptées par Akim le 27/11) | — | à faire |
| 4 — accès de surveillance bridé | — | à faire |

## Phases

Chaque phase a sa note M (faits, modes de panne, critères), sa revue par `reviewer`, puis `ship.sh`. Aucune ne
change ce que voit l'utilisateur, sauf la phase 5, soumise aux décisions d'Akim.

**Phase 0 — Rythme hors session et filets (S, invisible)**
- Fréquences de collecte : textes en direct, agenda et pavillons une fois par jour ; SEORS coupé ; sources ODS
  « live » à 6 h. On garde les décisions officielles, la presse, le courrier, les alertes et les récapitulatifs.
  Un réglage `COP_SESSION` (0 hors session, 1 pendant) bascule tout d'un coup.
- Sonde externe : workflow GitHub Actions toutes les 15 min sur `/healthz/deep` des deux adresses ; un échec
  envoie un e-mail de GitHub (ferme #1 sans compte tiers).
- Usage : ne plus journaliser les pages `/doc` vues sans compte, compter seulement un total par jour, et purger
  les 252 000 lignes anonymes existantes. *(Les lignes de robots ne sont pas des données d'utilisateurs ; les
  statistiques de /admin/usage restent justes.)*

**Phase 1 — Un écrivain par donnée (M)**
- Alertes : `alerts.run` et `send_pending` seulement depuis la boucle du worker. Les autres chemins posent un
  drapeau « à traiter ». Envoi marqué *avant* l'appel à Telegram (réservation), donc jamais de double envoi.
- Chien de garde : `watchdog_state` écrit par le worker seul ; le site ne fait que publier son battement et
  répondre à `/healthz/deep`. Traite #28 et #29.
- Indexation : une seule file dans le worker (#5). `tidy_texts` passe par le même verrou que `_index_document`.

**Phase 2 — Le site ne calcule plus en fond (M)**
- Parties lourdes des pages dossier (nouveautés, frises) calculées par le worker après chaque passage indexé,
  puis stockées en table. Le site les lit ; le fil `prechauffage` disparaît.
- `/database` : mesurer puis corriger le palier lent (#14).

**Phase 3 — Schéma et code mort (M)**
- Un point de migration unique (`db.migrate`, numéro de version dans `meta`), lancé une fois par `ship.sh` avant
  le démarrage des conteneurs ; les `ensure_schema` dispersés deviennent des étapes de migration.
- Retirer briefs IA, analyse des soumissions par le serveur, notes de terrain, `COP_DEMO_BOOST_*`, doublon
  `pavillons.embed_pending` ; mettre à jour architecture.md.

**Phase 4 — Accès de surveillance bridé (M, sécurité)**
- Clé SSH dédiée, limitée par `command=` à un script de rapport de santé en lecture seule. La surveillance
  quotidienne peut alors tourner dans le cloud, sans droit de déployer.

**Phase 5 — Surface (selon les décisions d'Akim, §7)** : « COP31 terminée » sur programme et pavillons ; textes du
site qui promettent des alertes Telegram, ajustés hors session ; retrait des briefs (avec la phase 3).

## Critères d'acceptation du programme

1. Après la phase 1 : aucun curseur, état ni clé `meta` n'a plus d'un fil écrivain. Un test énumère les écrivains
   de chaque clé connue, et le relecteur le vérifie par grep.
2. Après la phase 2 : le site ne lance plus aucun fil de fond hormis son battement ; p90 de toutes les familles de
   pages < 1 s sur 4 h de trafic (Caddy), `/database` comprise.
3. Après la phase 3 : une seule fonction crée ou modifie le schéma ; `src/cop_corpus` a perdu au moins
   1 000 lignes sans perte de fonction visible.
4. À chaque phase : aucune nouvelle issue `suite` de gravité « élevée » ; les suites ouvertes touchées par la phase
   sont fermées ou explicitement reportées.
5. Sur deux semaines après la phase 2 : aucune alerte « Blocage » qui ne soit pas une vraie panne.

## Hors périmètre

Nouvelles fonctions ; changement de base de données ; VPS ; textes et parcours du site ; redirection de l'ancien
domaine et courrier sur opencop.org (#9–#12 : à reprendre après la consolidation).

## 7. Décisions produit pour Akim

**Décidé par Akim le 27/11** : 1 garder les outils de soumissions et de groupes ; 2 **couper Telegram hors
session, sauf les alertes qui méritent de passer par lui** (plus rien sur la COP : ni alertes de sujets, ni
textes, ni récapitulatif, ni commandes du bot ; seules les alertes bloquantes du chien de garde) ; 3 garder /try ;
4 « COP31 terminée » sur programme et pavillons ; 5 retirer les briefs IA. Usage mesuré depuis le 18/11, fin de la COP comprise :

| Fonction | Usage mesuré | Proposition de Claude |
|---|---|---|
| Outils MCP `soumissions`, `comparer_soumissions`, `groupes` | 0 appel | garder (utiles en préparation de COP, coût nul hors collecte) mais collecte des soumissions à 1 fois par jour |
| Bot Telegram (commandes) et récapitulatif 7 h / 19 h | 6 commandes, 2 comptes | garder les alertes ; récapitulatif une fois par jour hors session |
| Questions sur le site (/try, /ask) | 38 réponses en tout | garder /ask pour les comptes ; décider si /try (démo publique, coût LLM) reste ouverte hors COP |
| Programme et pavillons (page « Plan my day », outils `programme`, `pavillons`) | 38 appels pendant la COP | afficher « COP31 terminée » hors session, données gardées |
| Briefs IA par dossier | coupés depuis le 17/11 | retirer (phase 3) |

## Revue

Note de programme : pas de diff. Chaque phase passe par `reviewer` contre sa propre note.

## Suites

Phases 0 à 4 : une issue chacune à l'ouverture de leur note.

## Promotion (à la livraison de chaque phase)

- [ ] principes « le site lit, le worker écrit » et « un écrivain par donnée » → PITFALLS (liste du diff) et architecture.md §1
- [ ] statut de chaque phase ici et dans l'index
