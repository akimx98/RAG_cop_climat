# 2026-11-27 — Sources refusées par le site : anti-bot et articles payants

*Taille : M (collecte) · Statut : brouillon*
*Fichiers concernés : `ingest.py` (`fetch_one`, `run_source.log`, `REFUSED`, boucle ODS), `worker.py` (`_due`),
`mailin.py` (code mort retiré, #8), `db.py` (`_migrate` : `mail_code` vidée), `tests/test_sources_refus.py`, `tests/test_core.py`*
*Issues : #2, #8*

## Problème

Le chien de garde envoie chaque jour un « Dégradé : N source(s) sans aucun succès » qui mélange de vraies pannes
et des refus des sites eux-mêmes, contre lesquels on ne peut rien (règle : anti-bot jamais contourné). Un refus
HTTP 403 ou 402 est compté comme une erreur, alors qu'une page de challenge reconnue (`is_antibot`) est comptée
« antibot » et signalée à part.

## Faits mesurés (production, 27/11 ~10:40 UTC, lecture seule)

| Source | Dernière erreur | Nature |
|---|---|---|
| `guardian-climate-crisis-rss`, `guardian-cop31-rss` | `rss 403` | robot refusé |
| `climatehome-rss`, `climatehome-cop31-tag-rss` | `rss 403` | robot refusé |
| `eu-council-press-rss` | article `403` | robot refusé |
| `unfccc-cop31-daily-programme`, `-outcomes`, `-presidency-consultations` | `page 403` | robot refusé (unfccc.int) |
| `unfccc-cop31-auv-draft-texts` | page de challenge | déjà classée « antibot » |
| `lemonde-planete-rss`, `lemonde-climat-rss` | articles `402` (14 sur le dernier passage) | articles payants : le flux, lui, répond |
| `fr-mte-presse-crawl` | `ConnectError: Temporary failure in name resolution` | vraie panne (DNS), passagère |

Requêtes : `source_state WHERE failing_since IS NOT NULL` et dernier `fetch_log` en erreur par source.

## Modes de panne

| Composant | Panne possible | Aujourd'hui | Après |
|---|---|---|---|
| Rapport « Dégradé » | noyé par les refus | 11 sources listées dont 10 refus | seules les vraies pannes listées ; les refus anti-bot comptés à part (« + N bloquée(s) par un anti-bot ») |
| Source à articles payants | marquée « en échec » | oui (Le Monde) | non : le flux répond, les articles payants sont comptés « payant » |
| Un 403 qui serait une vraie panne (droits changés) | masqué | — | visible dans `fetch_log` et sur la page admin (résultats « antibot ») ; compté dans le « Dégradé » quand d'autres sources sont en panne, sinon silencieux (choix : un refus anti-bot n'est pas actionnable) |
| Source payante ou refusée | relevée à chaque passage | espacée (×8, 240 min au moins) | espacée, « payant » compris (`worker._due`) |
| Série ODS refusée (403) | — | arrêt après `stop_after_misses` | idem, « antibot » et « payant » comptés comme manqués |

## Critères d'acceptation

1. `fetch_one` : 403 → « antibot », 402 → « payant », journalisés comme tels ; les autres échecs restent « error ».
2. Pages de liste et flux (crawl, rss) : `page 403` / `rss 403` → « antibot », `402` → « payant ».
3. Une source dont tous les articles sont payants n'est pas « en échec » ; une source refusée (403) l'est, mais
   sous la seule rubrique anti-bot (`watchdog._antibot_only`).
4. Aucune tentative de contournement (pas de nouvel en-tête, pas de nouvel essai).
5. Une source à résultat « payant » est espacée comme une source en erreur (`worker._due`) ; une série ODS refusée
   s'arrête après `stop_after_misses` (tests).
6. #8 : `mailin.deposit_address`, `new_code` et `_DEPOT` retirés ; les tests vérifient toujours qu'une ancienne
   adresse `depot-…@` n'est plus routée ; `_migrate` vide `users.mail_code` (1 valeur en production le 27/11), test.
7. Suite complète verte.

## Conception

Table `REFUSED = {403: "antibot", 402: "payant"}` utilisée par `fetch_one` et par le journal des listes/flux de
`run_source`. « payant » n'est pas dans `FAIL_KEYS`. Écarté : désactiver ces sources (les flux de presse servent
encore quand ils répondent ; le choix d'arrêter les sources propres à la COP31 relève de l'utilisateur).

Hors périmètre : `fr-mte-presse-crawl` (panne DNS passagère) ; retrait des sources COP31 closes.

## Revue

Tour 1 (`reviewer`) : **non bloquant**, critères 1 à 5 conformes, 231 tests verts. Constats acceptés et corrigés :
1. (important) « payant » n'était plus espacé par `worker._due` (avant : compté « error », donc espacé) → ajouté ;
2. (important) la boucle ODS ne comptait plus un 403 comme manqué (devenu « antibot ») → « antibot » et « payant »
   comptés ; tests `test_source_payante_espacee`, `test_serie_ods_refusee_s_arrete`.
Constat 3 (sources seulement refusées : aucun « Dégradé ») : accepté comme choix, note corrigée (page admin). 4
(« payant » invisible sur la page admin) : accepté, cohérent avec la conception.

Tour 2 (`reviewer`, nouveau) : **non bloquant**, critères 1 à 7 conformes, 234 tests verts. Constat reporté en
suite : `mailin._deposit` et la branche `kind == "depot"` sont désormais inatteignables (`route` ne renvoie plus
« depot ») ; à retirer avec leurs tests.

## Suites
