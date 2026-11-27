# 2026-11-27 — Codes de connexion : réseaux stockés en empreinte, jamais en clair

*Taille : M (authentification, confidentialité, base) · Statut : brouillon*
*Fichiers concernés : `auth.py` (`_net`, `_log`, `hash_stored_networks`, `issue_login_code`, `code_locked`,
`check_login_code`), `db.py` (`_migrate`), `tests/test_auth_parcours.py`*
*Issue : #3*

## Problème

`login_codes.sent_at` et `login_codes.fail_log` gardent, pour les limites d'envoi et d'essais, des couples
`[horodatage, réseau]` où le réseau est l'adresse IP (IPv4) ou le préfixe /64 (IPv6) **en clair**. La règle du projet
(CLAUDE.md) : « Pas d'adresse IP en clair ». Les limites ne font que comparer des réseaux entre eux : une empreinte
suffit (comme `demo.ip_hash`).

## Faits mesurés (production, lecture seule, 27/11)

| Fait | Valeur |
|---|---|
| Lignes de `login_codes` | 2 |
| Entrées `[t, réseau]` | 2, toutes deux avec une adresse réseau en clair |
| Rétention | 24 h par entrée, mais seulement à la réécriture de la ligne (une ligne jamais réécrite garde ses entrées) |

## Modes de panne

| Composant | Panne possible | Aujourd'hui | Après |
|---|---|---|---|
| Limites par réseau | adresse en clair en base | oui | empreinte HMAC-SHA256 salée par `COP_SECRET` (préfixe `h:`) |
| Anciennes entrées | restent en clair | jusqu'à la réécriture | réécrites (empreinte) et purgées au-delà de 24 h à chaque démarrage (`_migrate`) |
| Transition | compteurs par réseau remis à zéro | — | non : les anciennes entrées sont converties avec la même empreinte |
| Changement de `COP_SECRET` | compteurs par réseau perdus | — | accepté (≤ 24 h ; les plafonds par compte restent) |

## Critères d'acceptation

1. Après un envoi de code et un essai faux, `sent_at` et `fail_log` ne contiennent aucune adresse en clair ; chaque
   réseau commence par `h:` (test).
2. Les limites par réseau et par compte se comportent comme avant (test existant
   `test_code_caps_per_day_and_per_account`, adapté au format stocké).
3. `hash_stored_networks` convertit les entrées en clair, retire celles de plus de 24 h, est idempotent (test), et
   tourne au démarrage (`_migrate`).
4. Aucune adresse, ni empreinte, dans les journaux ou Telegram.
5. Suite complète verte ; après déploiement : 0 entrée en clair dans `login_codes`.

## Conception

`_net(ip)` = `h:` + 20 caractères hexadécimaux de HMAC-SHA256(`COP_SECRET`, `net:<clé réseau>`), la clé réseau étant
déjà celle d'`ip_key` (seul appelant : `web.login_form`), idempotent. Appliqué à
l'entrée des trois fonctions qui écrivent ou comparent ces réseaux. Les compteurs en mémoire (`auth._failures`) ne
sont pas concernés (jamais stockés). Écarté : réutiliser `demo.ip_hash` (autre format, autre module).

## Déploiement

`scripts/ship.sh` ; la conversion se fait au premier démarrage. Retour arrière : `--rollback` (l'ancien code
comparerait des adresses en clair aux empreintes : compteurs par réseau remis à zéro, plafonds par compte intacts).

## Revue

Tour 1 (`reviewer`) : **non bloquant**, critères 1 à 4 conformes, 247 tests verts ; pas d'import circulaire
(import local dans `_migrate`), table et colonne créées avant la conversion. Constats corrigés : (1) un ancien
processus encore en marche pendant un déploiement (ou après un retour arrière) peut réécrire du clair, que la
conversion au seul démarrage laissait jusqu'au redémarrage suivant → `_log` convertit à la lecture (comptage juste,
réécriture hachée) ; (2) un journal illisible aurait fait échouer `_migrate`, donc toute connexion → ligne vidée,
démarrage jamais bloqué ; test pour les deux. Note précisée : `_net` hache la clé déjà passée par `ip_key`
(préfixe /64 en IPv6), seul appelant : `web.login_form`.

## Suites
