# 2026-11-27 — Phase 3 : un seul point de migration du schéma, code mort retiré

*Taille : M (base, déploiement) · Statut : validé (à coder après la phase 2) · Programme :
[revue d'architecture](2026-11-27-revue-architecture.md)*
*Fichiers concernés : `db.py`, `cli.py`, `scripts/ship.sh`, tous les modules à `ensure_schema` (live, usage,
soumissions, agenda, pavillons, groupes, demo, doublons, monitor, positions), `briefs.py` (retiré),
`live_worker.py`, `mcp_server.py`, `demo.py`, `config.py`, `llm.py`, `auth.py`, gabarits (`OFFER.boost`), tests*

## Problème

Le schéma est créé et migré par 10 modules (≈ 94 appels à `ensure_schema`), au démarrage de chacun des trois
conteneurs, sans numéro de version ; `auth.hash_stored_networks` réécrit `login_codes` à chaque démarrage de
chaque conteneur (contraire à « un écrivain par donnée »). Du code de fonctions coupées reste porté : briefs IA
(décision d'Akim : retirer), analyse des soumissions par le serveur, notes de terrain, majoration de fin de COP.

## Faits (relevé du 27/11 sur 9c632a3, fichier:ligne dans le relevé)

- Inventaire du schéma : `db.SCHEMA` + `_COLUMNS`/`_migrate` ; `doublons`, `live` (9 tables, `migrate_ace`),
  `usage`, `soumissions` (6 tables), `agenda`, `pavillons` (FTS5), `groupes`, `demo`, `monitor._ensure` ;
  migration de données `positions.requeue_for_sections` à chaque passage d'extraction.
- **Corrections à la revue d'architecture** : la commande Telegram `/brief` envoie le récapitulatif **sans IA**
  (à garder) ; `pavillons.embed_pending` calcule les vecteurs des pavillons (pas un doublon : à garder) ;
  `COP_SUB_ANALYSE_SINCE` fixe aussi la priorité des téléchargements (remplacer par `SUB_EMBED_SINCE`, même défaut).
- **Défaut trouvé** : `compare_in_call` et `compare_actors` appellent `compare()` (analyse par le serveur) pour une
  paire déjà en cache → appel IA caché, ou erreur sans clé. À remplacer par la lecture du cache.
- Estimation : ≈ 650 lignes nettes retirées de `src/cop_corpus` (≈ 250 de tests en plus).

À mesurer avant le code (lecture seule) : valeurs `meta` `auth_backfill_v1`, `migration_ace`,
`positions_par_section_v1` ; volumes `briefs`, `field_notes`, `sub_analysis`, `sub_comparisons` par modèle,
positions `source_kind='soumission'` ; écart entre le schéma de production et une base neuve ; **noms** (pas les
valeurs) des variables retirées présentes dans `deploy/.env`.

## Conception

`db.migrate(conn)` : clé `meta` `schema_version` (propriétaire `db`), liste ordonnée `STEPS`, une transaction
`BEGIN IMMEDIATE` par étape (version relue dedans ; jamais `executescript`). Étape 1 « socle » : tous les
`CREATE … IF NOT EXISTS` et ajouts de colonnes conditionnels actuels. Étape 2 « données » : `auth_backfill_v1`,
`migration_ace`, `positions_par_section_v1`, `mail_code`, `hash_stored_networks` (plus à chaque démarrage).
Appel : `ship.sh` construit, lance `cop migrate` dans un conteneur jetable (`run --rm --no-deps`), puis `up -d
--no-build` ; `db.connect` ne fait que lire la version et migre en repli (poste de dev, CLI, tests) en le signalant ;
base plus récente que le code : une ligne de journal, aucune écriture. Écarté : le site attend le worker.
Règles : étapes **additives** (jamais de DROP/RENAME d'un objet lu par la version précédente), légères, rejouables.

Retraits : `briefs.py` et branchements (table `briefs` gardée, orpheline) ; analyse/comparaison par le serveur
(`analyze`, `compare`, `analyse_tick`, CLI, réglages) en gardant la lecture du cache et les outils MCP ;
`live.add_note`, `live.notes`, `nb_notes` (table `field_notes` gardée : données d'utilisateurs, décision d'Akim) ;
`COP_DEMO_BOOST_*` et `OFFER.boost` ; `auth.read_cookie`, `llm.usage_summary`, `demo.quote_found`, constantes
mortes. Réglages retirés : `COP_BRIEFS`, `COP_BRIEF_HOURS`, `COP_LLM_WRITE_MODEL`, `COP_SUB_AUTO_ANALYSE`,
`COP_SUB_ANALYSE_SINCE`, `COP_DEMO_BOOST_UNTIL`, `COP_DEMO_BOOST_PER_ACCOUNT_DAY`.

## Modes de panne

| Cas | Effet | Parade |
|---|---|---|
| Étape en échec en production | version inchangée | `cop migrate` sort en erreur, `ship.sh` s'arrête, anciens conteneurs intacts |
| Verrou tenu par l'ancien worker | attente 30 s | étape courte ; relancer le déploiement |
| Site démarré hors `ship.sh` avant migration | — | repli de `connect` sous verrou, signalé |
| `ship.sh --rollback` | ancien code sur base v2 | tout est additif, aucune table supprimée ; l'ancien code ignore `schema_version` |
| Arrêt brutal pendant une étape | transaction annulée | étape rejouée |

## Critères d'acceptation

1. Aucune instruction DDL (`CREATE`/`ALTER`/`DROP`) dans `src/cop_corpus` hors de `db.py` (test) ;
   `ensure_schema`, `_ready`, `monitor._ensure` n'existent plus.
2. Une base neuve migrée a exactement le schéma de référence relevé **avant** le changement (test).
3. `migrate` idempotente ; version de base plus récente : ni écriture ni exception ; étape en échec : version et
   schéma intacts, `cop migrate` sort avec un code non nul (tests).
4. `ship.sh` lance `cop migrate` après la construction et avant `up` ; un échec arrête le déploiement sans toucher
   aux conteneurs en service.
5. En production : `cop migrate --check` répond « à jour (2) » ; aucun signalement « migration au démarrage ».
6. `META_OWNERS` : `schema_version`, `migration_ace`, `positions_par_section_v1` → `db` ; plus de `brief_slot`.
7. Plus aucun appel IA du serveur hors extraction des positions et démo (grep des `purpose` de `llm.json_call`).
8. `comparer_soumissions` sur une paire déjà qualifiée par un client renvoie le cache, sans appel IA ni erreur.
9. Réglages retirés de `config.py` et de architecture.md §7 ensemble ; architecture.md à jour (§1, §3, §4, §6–§9,
   paragraphe « Schéma »).
10. Rien de visible ne change pour l'utilisateur, hors la description de l'outil MCP `dossier` et le critère 8.
11. Au moins 600 lignes de moins dans `src/cop_corpus`. *(Le critère 3 du programme — 1 000 lignes — se mesure
    cumulé sur les phases 2, 3 et 5.)*

## Hors périmètre

Tout `DROP TABLE`/`DROP COLUMN` ; `web_live.py`, `search.py` (phase 2) ; retrait de SEORS ; extraction des positions
par IA ; skills du plugin ; nettoyage du `.env` du VPS (variables devenues sans effet, à faire après).

## Déploiement

Deux livraisons : (a) schéma (`db.migrate`, `cop migrate`, `ship.sh`, retrait des `ensure_schema`) → critères 4,
5 ; (b) code mort, seulement si (a) est sain → critères 7, 8, 10. Retour arrière : `ship.sh --rollback`, jamais
de migration descendante.

## Revue

(à venir)

## Suites

(à la livraison)

## Promotion (à la livraison)

- [ ] PITFALLS : `executescript` valide tout seul ; migrations additives, légères, rejouables ; DDL seulement dans `db.py`
- [ ] architecture.md ; statut ici, dans l'index et dans « Avancement » de la revue
