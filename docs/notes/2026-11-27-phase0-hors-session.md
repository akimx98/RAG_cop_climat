# 2026-11-27 — Phase 0 : rythme hors session, sonde externe, visites des robots

*Taille : M (collecte, sorties Telegram) · Statut : validé · Programme : [revue d'architecture](2026-11-27-revue-architecture.md)*
*Fichiers concernés : `config.py`, `live_worker.py`, `worker.py` (`_due`, récapitulatif), `soumissions.py` (index),
`usage.py` (`purge`), `web.py` (page document), `.github/workflows/sonde.yml`, `docs/architecture.md` §6 §7*

## Problème

La COP31 est close depuis le 20/11, mais la collecte tourne au rythme de la conférence : textes en direct toutes les
5 min, SEORS toutes les heures (bloqué par l'anti-bot, ne rapporte rien), sources ODS « live » toutes les 15 min,
positions et Bluesky toutes les 10 min. Coût : CPU, écritures dans la base, risque de blocage par l'ONU.
Aucune sonde externe : une panne du VPS entier est invisible (#1). Chaque page `/doc` vue par un robot écrit une
ligne dans `usage_events` avec un `commit` : 252 272 lignes depuis le 18/11 (99,8 % de la table), jusqu'à
138 000 écritures par jour, sans usage.

## Faits mesurés

| Fait | Valeur | Comment mesuré |
|---|---|---|
| Nouveaux documents par jour depuis le 20/11 | 11 à 50 | session « Messages d'erreur Telegram », 27/11 |
| Sources qui apportent encore du neuf | ODS projets/décisions COP31 et CMA8, textes en direct, courrier, presse | idem |
| Collecte sur 24 h | 141 requêtes, 18 sources | idem |
| `usage_events` anonymes `page/document` | 252 272 sur 253 572 lignes | base en lecture seule, 27/11 |
| Rythmes actuels | `live_worker.EVERY` ; 11 sources à 15 min dont 9 dans `unfccc_cop31_live.yaml` ; index des soumissions 30 min | code |
| Récapitulatif Telegram | 7 h et 19 h (`COP_DIGEST_HOURS`) | `config.py:60` |

## Modes de panne

| Composant | Panne possible | Aujourd'hui | Après |
|---|---|---|---|
| Collecte ralentie | une décision officielle publiée arrive plus tard | 15 min | ≤ 6 h (acceptable hors session) |
| Réglage oublié à la prochaine COP | collecte lente pendant une session | — | `COP_SESSION=1` rétablit tout ; noté dans architecture §7 |
| Sonde GitHub | fausse alerte sur un redéploiement (≈ 1 min) | — | deux essais espacés de 2 min avant d'échouer |
| Sonde GitHub | GitHub coupe les workflows planifiés après 60 jours sans commit | — | noté ; la surveillance quotidienne vérifie la dernière exécution |
| Purge des lignes anonymes | verrou long sur la base | — | lots de 5 000 lignes, une transaction courte par lot, dans la purge horaire du worker (seul écrivain) |

## Critères d'acceptation

1. `config.SESSION` (variable `COP_SESSION`, défaut 0) existe, est listée dans architecture.md §7 (test existant).
2. Hors session (`SESSION=0`) : `live_worker` applique des périodes hors session — textes, référence, agenda,
   pavillons : 24 h ; social, positions : 6 h ; groupes : 7 jours ; SEORS : jamais. En session : les périodes
   actuelles, inchangées. Testé (table des périodes selon le réglage).
3. Hors session : toute source du catalogue dont `watch_minutes` est inférieur à 360 est relevée au plus toutes les
   360 min (`worker._due`), sans modifier les fichiers du catalogue. En session : inchangé. Testé.
4. Hors session : l'index du portail des soumissions est relu au plus une fois par 24 h (au lieu de 30 min) ; le
   téléchargement des fichiers manquants est inchangé. Testé.
5. Hors session : le récapitulatif Telegram part une fois par jour, à la première heure de `DIGEST_HOURS`, si
   `COP_DIGEST_HOURS` n'est pas fixé explicitement. Testé.
6. Une page `/doc` vue sans compte n'écrit plus rien dans `usage_events` ; vue avec un compte, inchangé. Testé.
7. La purge horaire du worker (`usage.purge`) supprime les lignes `channel='web', kind='page', name='document',
   uid IS NULL` par lots de 5 000 au plus par passage, en transactions courtes. Testé. Les statistiques de
   `/admin/usage` ne plantent pas sans ces lignes.
8. `.github/workflows/sonde.yml` : toutes les 15 min, `GET /healthz/deep` sur `https://opencop.org` et
   `GET /healthz` sur `https://cop31.akimx.fr` ; échec seulement après deux essais à 2 min d'intervalle ; un
   déclenchement manuel (`workflow_dispatch`) est possible. Aucun secret.
9. Suite de tests complète verte ; rien de visible pour l'utilisateur hormis le récapitulatif quotidien unique
   (décision §7 de la revue).

## Conception

Un seul réglage `COP_SESSION` plutôt que de réécrire le catalogue : la prochaine COP rétablit tout d'un coup.
Plancher de 360 min appliqué dans `_due` (un seul endroit). La purge passe par le worker, déjà propriétaire de
`usage.purge` : le site ne supprime rien. Sonde par GitHub Actions : gratuite, sans compte tiers, e-mail de GitHub
au propriétaire du dépôt en cas d'échec.

Hors périmètre : affichage « COP31 terminée » (phase 5), toute autre modification de l'alerte.

## Déploiement

`scripts/ship.sh` (worker et site). Premier démarrage : la purge commence au premier passage horaire (≈ 51 passages
pour tout purger ; aucune fausse alerte attendue). Retour arrière : `ship.sh --rollback`, ou `COP_SESSION=1` dans
`deploy/.env`.
Vérifications : `source_state.last_run` des sources ODS « live » espacé de ≥ 6 h après 12 h ; nombre de lignes
anonymes en baisse ; aucune nouvelle ligne anonyme `page/document` ; exécution verte de la sonde dans l'onglet
Actions.

## Revue

(à venir)

## Suites

(à la livraison)

## Promotion (à la livraison)

- [ ] architecture.md §6 (rythmes hors session) et §7 (`COP_SESSION`)
- [ ] statut « livré » ici, dans l'index et dans le tableau « Avancement » de la revue
