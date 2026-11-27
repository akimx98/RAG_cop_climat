# 2026-11-27 — Phase 1 : un écrivain par donnée

*Taille : M (base et `meta`, sorties Telegram, indexation) · Statut : validé · Programme :
[revue d'architecture](2026-11-27-revue-architecture.md) · Part de l'état laissé par la
[phase 0](2026-11-27-phase0-hors-session.md)*
*Fichiers concernés : `alerts.py`, `watchdog.py`, `monitor.py`, `worker.py`, `ingest.py`, `live.py`
(`tidy_texts`), `extract.py` (`DEFER_PDF`), `server.py`, `mcp_server.py` (`_deposer_url`), `web.py` (`mail_page`),
`mailin.py`, `cli.py`, `db.py` (registre), tests*

## Problème

Plusieurs fils et deux processus (site, worker) écrivent les mêmes données. Conséquences constatées ou possibles :
double envoi d'une alerte Telegram, transaction tenue pendant un appel réseau, état du chien de garde réécrit par
deux processus (#23, #28, #29), double indexation (#5), version et lignée d'un texte écrasées.

## Faits vérifiés (relevé du 27/11, `HEAD` c60db82)

- `alerts.run` est appelé par : worker principal (`worker.py:39`, fin de passage de veille ≈ 20 min pendant la COP),
  fil `mail` (`mailin.py:459`), **site** : `_deposer_url` (`mcp_server.py:841`) et page admin (`web.py:1162`),
  `cli.py:142`. Le site a le même jeton Telegram : il envoie vraiment des alertes.
- `send_pending` : SELECT, puis envoi, puis UPDATE ; le premier UPDATE ouvre une transaction implicite tenue
  pendant les envois suivants (jusqu'à 70 s × 3 essais, `telegram.py:21-38`) ; un échec est retenté sans fin.
- `watchdog_state` / `watchdog_pending` : écrits par le worker et par le site (relais si `watchdog_check:worker`
  a plus de 15 min, alors qu'un passage de veille de 20 min le vieillit sans panne). `mem:app` n'est jamais
  signalé tant que le worker vérifie. #29 : « Dégradé » envoyé avant l'écriture de l'état.
- Le site indexe lui-même le HTML, le .docx et le texte (`DEFER_PDF` ne diffère que les PDF) ; le worker peut
  indexer le même document en même temps (`_indexing` est propre au processus).
- `_index_document` lit la ligne avant le verrou et calcule `version = COUNT(*)` hors transaction ;
  `live.tidy_texts` (fil `direct`) écrit `lineage_key`/`version` hors de `_write_lock`, juste avant
  `notify_new_texts`.
- Clés à un seul écrivain déjà correctes : `digest_*`, `brief_slot`, `docs_unfccc_last`, `agenda_*`, `pav_*`,
  `soumissions_index`, `telegram_offset`, `hb:*`, `login_link_used:*`. Sans conséquence : `llm_usage`,
  réglages admin. `embed_lock` : un seul fil l'utilise.

## Modes de panne

| Composant | Panne | Après le changement |
|---|---|---|
| Fil `alertes` mort | plus d'alertes | battement `hb:alertes` + `alerts_lag` |
| Arrêt brutal entre réservation et envoi | un message Telegram perdu (la notification reste sur le site) | accepté (aujourd'hui : double envoi) |
| Worker mort | — | le site alerte (clé `worker`) en ≤ 15 min, puis « Rétabli » |
| Site mort | — | le worker alerte (`hb:app`) |
| Les deux | — | sonde externe (phase 0) |
| Fil `garde` figé | vu comme worker mort | accepté : c'est une panne de la surveillance |
| Dépôt MCP d'une page HTML | indexé ≈ 5–10 s plus tard (dans l'attente de 90 s de l'outil) | invisible ou presque |
| Premier déploiement | ancien `watchdog_state` abandonné | pas de « Rétabli » pour un problème ouvert pendant le déploiement (accepté) |

## Critères d'acceptation

**Lot A — alertes**
1. `alerts.run` et `alerts.send_pending` ne sont appelés que par un fil `alertes` du worker (période 60 s,
   battement `hb:alertes` déclaré dans `expect`) ; aucun appel dans `web.py`, `web_live.py`, `mcp_server.py`,
   `server.py`, `mailin.py`, `cli.py`, ni dans `watch_once`.
2. Réservation atomique : `UPDATE … SET telegram_sent_at=? WHERE id=? AND telegram_sent_at IS NULL`, commit, envoi
   seulement si `rowcount == 1` ; en cas d'échec, libération (`… SET NULL WHERE id=? AND telegram_sent_at=?`).
   Même principe pour le message « … et N autres ». Test : deux `send_pending` concurrents (deux connexions, envoi
   simulé lent) envoient chaque notification une seule fois.
3. Aucune transaction ouverte pendant un envoi : test où le faux `telegram.send` vérifie
   `conn.in_transaction is False` et où une autre connexion écrit pendant l'envoi.
4. Plafond d'âge de la phase 0 conservé (48 h) ; un envoi en échec est retenté au tour suivant, au plus jusqu'au
   plafond.

**Lot B — chien de garde**
5. Le worker vérifie dans un fil `garde` indépendant de la veille (`watchdog.run_forever("worker")`) et n'écrit que
   `watchdog_state:worker`, `watchdog_pending`, `watchdog_check:worker`. Le site n'écrit que
   `watchdog_state:app`, `watchdog_check:app` et ses battements ; ses seules clés de problème : `worker`
   (`watchdog_check:worker` plus vieux que 3 × 5 min, bloquant) et `mem:app` (dégradé). Clés disjointes, testé.
6. Test : `watchdog_check:worker` vieux de 16 min → alerte `worker` envoyée par le site, puis « Rétabli » envoyé par
   le site ; le worker n'envoie jamais `worker`, le site jamais `pending` ni `alerts_lag`.
7. #29 : état écrit avant toute annonce ; si l'écriture échoue, rien ne part ; un seul message après le retour de la
   base. #28 : une alerte envoyée par le repli n'est pas renvoyée au retour de la base ; `_mem_sent` purgé au-delà
   de `RESEND_MINUTES`. Testés.
8. L'ancienne clé `watchdog_state` est supprimée au premier passage du worker.

**Lot C — indexation**
9. Le site n'indexe plus rien : il enregistre le document `pending` (tous formats) ; le fil `indexation` du worker
   (intervalle 5 s) l'indexe, dépôts (`depot-*`) et textes de session (`live-texts`) d'abord. Test.
10. `live.tidy_texts` s'exécute sous `ingest._write_lock` ; `_index_document` relit `lineage_key` et `version` sous
    le verrou avant de décider. Test de course (point d'arrêt simulé) : la lignée `neg:` et la version de
    `tidy_texts` sont conservées.

**Lot D — registre**
11. `db.META_OWNERS` déclare le module propriétaire de chaque clé ou préfixe `meta` ; un test AST relève tous les
    `meta_set` / `INTO|UPDATE|DELETE FROM meta` de `src/cop_corpus` et échoue sur une clé non déclarée, non
    résolue ou écrite par un autre module. Une liste `WORKER_ONLY` (`alerts.run`, `alerts.send_pending`,
    `ingest.index_document`, `live.tidy_texts`…) est interdite d'appel depuis `web.py`, `web_live.py`,
    `mcp_server.py`, `server.py`. Limite assumée : le test contrôle le module, pas le fil.
12. Suite complète verte ; rien de visible pour l'utilisateur.
13. En production, sur 24 h : aucun « database is locked », aucune clé `watchdog_state` restante, `/healthz/deep`
    à 200, aucun doublon d'envoi dans `notifications`.

## Conception

Choix et raisons dans le relevé du 27/11 (résumé) : fil `alertes` dédié plutôt qu'en fin de passage de veille
(latence de 20 min) ; réservation sans état « en cours » (une colonne et une reprise à tester pour perdre au pire un
rappel Telegram) ; un état de chien de garde par processus, à clés disjointes, plutôt qu'un relais.
File unique pour tous les producteurs du worker (#5 complet) : **écartée pour l'instant** (un arriéré bloquerait le
curseur des alertes ; les PDF sont déjà sérialisés) ; #5 est réduit au lot C.

Hors périmètre : migrations (phase 3), préchauffage (phase 2), courrier #7, `embed_lock`, texte des messages.

## Déploiement

Un `ship.sh` par lot (A, B, C ; D avec A puis enrichi). Vérifications après chaque lot : journaux du worker
(fils démarrés, battements), `meta` (clés `watchdog_state:*`), `/healthz/deep`.

## Revue

Lots A + D, tour 1 (`reviewer`, 27/11) : **non bloquant**, critères 1–4 et 11 remplis. Constats acceptés et
corrigés avant livraison : (1) le test AST suit les alias d'import et échoue sur un SQL `meta` non littéral ;
(2) dans le fil `alertes`, envoi Telegram en un seul essai de 20 s au plus et 5 échecs au plus par tour (une panne
de Telegram ne fige plus le fil) ; (3) réveil du fil remis à zéro avant le traitement. Accepté sans changement :
`_send_reserved` valide une transaction ouverte de l'appelant (aucun appelant n'en laisse aujourd'hui).

Lot B, tour 1 (`reviewer`, 27/11) : **non bloquant**, critères 5–8 remplis. Corrigés avant livraison : fausse
alerte `worker` possible au premier démarrage (valeur ancienne de `watchdog_check:worker`) → le site ignore
`worker` pendant ses 10 premières minutes ; double littéral dans `check`. Acceptés : pas d'alerte `worker` si la
clé est absente (installation neuve seulement ; `process_started` couvre les plantages au démarrage) ; un
« Rétabli » peut suivre un blocage dont l'envoi a échoué sur Telegram et e-mail ; libellés « chien de garde
(app|worker) » ; fil `garde` sans battement propre (`watchdog_check:worker` en tient lieu, vu par le site).

## Suites

(à la livraison)

## Promotion (à la livraison)

- [ ] PITFALLS : « un écrivain par donnée » dans la liste du diff ; registre `META_OWNERS`
- [ ] architecture.md §1 et §6 (fils `alertes`, `garde` ; le site n'indexe plus)
- [ ] statut ici, dans l'index et dans « Avancement » de la revue ; fermer ou reporter #5, #28, #29
