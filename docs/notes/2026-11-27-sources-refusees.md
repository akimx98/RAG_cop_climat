# 2026-11-27 — Sources refusées par le site : anti-bot et articles payants

*Taille : M (collecte) · Statut : brouillon*
*Fichiers concernés : `ingest.py` (`fetch_one`, `run_source.log`, `REFUSED`), `tests/test_sources_refus.py`*
*Issue : #2*

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
| Un 403 qui serait une vraie panne (droits changés) | masqué | — | toujours visible dans `fetch_log` et compté (« + N bloquée(s) ») |

## Critères d'acceptation

1. `fetch_one` : 403 → « antibot », 402 → « payant », journalisés comme tels ; les autres échecs restent « error ».
2. Pages de liste et flux (crawl, rss) : `page 403` / `rss 403` → « antibot », `402` → « payant ».
3. Une source dont tous les articles sont payants n'est pas « en échec » ; une source refusée (403) l'est, mais
   sous la seule rubrique anti-bot (`watchdog._antibot_only`).
4. Aucune tentative de contournement (pas de nouvel en-tête, pas de nouvel essai).
5. Suite complète verte.

## Conception

Table `REFUSED = {403: "antibot", 402: "payant"}` utilisée par `fetch_one` et par le journal des listes/flux de
`run_source`. « payant » n'est pas dans `FAIL_KEYS`. Écarté : désactiver ces sources (les flux de presse servent
encore quand ils répondent ; le choix d'arrêter les sources propres à la COP31 relève de l'utilisateur).

Hors périmètre : `fr-mte-presse-crawl` (panne DNS passagère) ; retrait des sources COP31 closes.

## Revue

## Suites
