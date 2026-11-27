# 2026-11-27 — Outils MCP et pages à formulaire hors de la boucle d'événements

*Taille : M (boucle d'événements, sorties MCP) · Statut : brouillon*
*Fichiers concernés : `mcp_server.py` (`_tool`), `demo.py` (`call_tool`), `web.py` (`body_first`, pages à
formulaire), `web_live.py` (`texte_upload`), `auth.py` (`_failures`), `tests/test_lenteur.py`*
*Issues : #4, #15*

## Problème

Le site tourne dans un seul processus, une boucle d'événements. Tout code synchrone exécuté dans cette boucle
fige toutes les requêtes (pages, MCP, `/healthz`) pendant sa durée.

1. **Outils MCP (#4).** Le SDK (`mcp` 1.30, `func_metadata.call_fn_with_arg_validation`) appelle un outil `def`
   directement dans la boucle. 20 des 21 outils sont `def` ; `rechercher` mesuré à 3,4 s en médiane, 7 s au
   maximum (note du 22/11).
2. **Pages `async def` (#15).** Dix pages gardent un `await` (lecture du formulaire) et font ensuite tout leur
   travail synchrone dans la boucle, y compris en GET. Le décorateur `_wrap` y appelle aussi `current_user` (une
   requête SQLite) dans la boucle. Inventaire (`ast`, 27/11) :

| Page | Travail synchrone dans la boucle |
|---|---|
| `login_form` (`/signin`, `/connexion`) | requêtes `auth`, **scrypt** (25 ms sur le Mac, n = 2¹⁴, 16 Mo) |
| `join` | `auth.signup` (scrypt), `invite_valid` |
| `contact_page` | `current_user` |
| `topics_page` (`/topics`) | **`topics.whats_new`** à chaque GET, `alerts.preview` |
| `account_page` | requêtes, `set_password` (scrypt), `usage.forget_user` |
| `admin_page` | **`search.stats`**, listes des utilisateurs et des sources |
| `mail_page` | **`mailin.ingest_newsletter`** (indexation) et **`alerts.run`** |
| `admin_programme`, `seors_import` | `agenda.import_seors` |
| `texte_upload` (`web_live`) | **`ingest.ingest_bytes`** (extraction d'un PDF) |
| `demo_ask` | prélude court (quota, conversation), puis travail dans un fil |

## Faits mesurés

| Fait | Valeur | Comment mesuré |
|---|---|---|
| Outils MCP `def` / `async` | 20 / 1 (`deposer_url`) | `grep`, 27/11 |
| Le SDK appelle un outil `def` dans la boucle | oui (`if fn_is_async: await fn(...) else: fn(...)`) | lecture de `mcp/server/fastmcp/utilities/func_metadata.py` |
| `anyio.to_thread.run_sync` copie les variables de contexte | oui (`copy_context()`, anyio 4.16) | lecture d'`anyio/_backends/_asyncio.py` |
| Schémas des 21 outils avant/après l'enveloppe | identiques octet pour octet | `mcp.list_tools()` sérialisé, `cmp` |
| scrypt | 25 ms (Mac) | `auth._scrypt`, chronométré |
| Fils de calcul du site | 12 (`server.THREADS`, limiteur par défaut d'anyio) | `server.py` |

## Modes de panne

| Composant | Panne possible | Aujourd'hui | Après |
|---|---|---|---|
| Outil MCP lent | site figé pendant l'outil | 3,4 s à 7 s par `rechercher` | l'outil occupe un fil sur 12, la boucle reste libre |
| Outil dans un fil | perte de l'utilisateur (variables de contexte) | — | contexte copié par anyio (test) |
| Démo du site | appelle `t.fn` en synchrone : recevrait une coroutine | — | appelle la fonction d'origine (`__wrapped__`) |
| Page à formulaire | site figé pendant l'indexation, les nouveautés, scrypt | oui | la page tourne dans un fil ; seule la lecture du corps reste dans la boucle |
| Compteurs d'échecs `auth._failures` | course entre fils (listes modifiées en parallèle) | sans objet (boucle seule) | protégés par un verrou |
| Pool de 12 fils saturé | requêtes en attente | pages `def` déjà dans le pool | idem, plus les outils MCP : à surveiller |

## Critères d'acceptation

1. Tous les outils MCP enregistrés sont asynchrones côté SDK (`is_async`), et un outil synchrone s'exécute hors
   du fil de la boucle (test qui enregistre un outil d'essai et relève le fil).
2. Les variables de contexte (utilisateur MCP, jeton) sont visibles dans l'outil exécuté dans un fil (test).
3. Les schémas (`inputSchema`, description, annotations) des 21 outils sont inchangés.
4. La démo appelle toujours les outils et obtient leurs résultats (tests existants `test_demo`, `test_parcours_question`).
5. Aucune page ni fonction de route de `web.py` et `web_live.py` n'est `async def`, sauf une liste explicite et
   justifiée dans le test (`health`, `health_deep`, `sitemap`, `demo_ask`, et l'enveloppe `body_first`) ; le test
   échoue si une nouvelle page `async` apparaît.
6. Une page à formulaire décorée par `body_first` lit le corps dans la boucle puis s'exécute dans un fil, y compris
   ses contrôles d'accès (`login_required`, `admin_required`) (test : fil relevé).
7. `auth._failures` est protégé par un verrou (test de concurrence : N fils × `hit` → N entrées). Les « lire puis
   écrire » de la connexion (`issue_login_code`, `check_login_code`, `cancel_login_code`, `signup`) sont
   sérialisés dans le processus : 12 essais parallèles d'un mauvais code comptent 5 essais au plus, un bon code
   envoyé 12 fois en parallèle n'ouvre qu'une session, une double inscription simultanée donne un compte et une
   erreur « exists » (pas de 500) (tests).
8. Suite de tests complète verte ; après déploiement : `cop smoke` OK, connexion, Mes sujets, Compte et admin
   répondent (requêtes HTTP, codes 200 ou 303 attendus).

## Conception

- Outils MCP : décorateur `_tool(annotations)` qui enregistre, pour une fonction `def`, une enveloppe `async`
  (`functools.wraps`) appelant `anyio.to_thread.run_sync(partial(fn, ...))`, et rend la fonction d'origine
  (appels directs des tests et de la CLI inchangés). Écarté : réécrire chaque outil en `async` (21 fonctions,
  risque d'oubli ; le décorateur centralise).
- Pages : décorateur `body_first` (le plus extérieur) : en POST, lit le formulaire (`await request.form()`) ou le
  corps (`await request.body()`) dans la boucle et le range dans `request.state`, puis exécute la page `def` dans
  un fil (`run_in_threadpool`). Les pages lisent `_form(request)` / `_body(request)`. Le fichier déposé se lit par
  `f.file.read()`. Écarté : `run_in_threadpool` page par page après l'`await` (dix découpages à la main, et les
  contrôles d'accès resteraient dans la boucle).
- `demo_ask` reste `async` : sa réponse est un flux (file d'attente asyncio) ; son prélude fait trois petites
  requêtes indexées. Hors périmètre.

Hors périmètre : #5 (file d'indexation unique), pool de fils dédié aux outils MCP.

## Déploiement

`scripts/ship.sh`. Aucun schéma, aucun réglage. Retour arrière : `scripts/ship.sh --rollback`. Vérifications :
critère 8.

## Revue

Tour 1 (`reviewer`) : **non bloquant**, critères 1 à 7 conformes, 220 tests verts. Constats acceptés et corrigés
avant livraison (régressions révélées par le passage en fils) :
- a. `check_login_code` : « tester puis écrire » sur `tries` → 12 essais par code au lieu de 5 en parallèle
  (reproduit par test : `tries` = 12), et deux sessions pour un même bon code ;
- b. `signup` : double clic → `IntegrityError` (500) au lieu de « exists » ;
- c. `issue_login_code` : deux codes à la même minute.
Correction : verrou `auth._login_lock` (décorateur `_serialized`) sur les quatre fonctions, seules appelantes : les
pages du site, un seul processus ; plus `IntegrityError` → « exists » pour une inscription venue d'un autre
processus. Critère 7 complété.
Reportés en suites : d. `_contact_hits` (plafond de 5 messages par heure dépassable de quelques unités en
parallèle, dictionnaire sans purge) ; e. `invite_settings` peut générer deux codes d'invitation en parallèle
(effet négligeable, existant).

Tour 2 (`reviewer`, nouveau) : **non bloquant**, 222 tests verts ; pas d'interblocage (`_login_lock` puis
`_failures_lock`, jamais l'inverse), pas d'appelant hors du site. Constat 1 accepté : la branche `IntegrityError`
n'était pas testée → `test_inscription_adresse_creee_ailleurs`. Reportés en suites : `_login_lock` tenu pendant
l'attente d'un verrou SQLite (30 s) — les pages de connexion attendent en file, les autres non ; `cancel_login_code`
peut annuler un code plus récent (fenêtre étroite) ; `throttled` puis `hit` non atomiques (quelques essais de plus) ;
`signup` pose le mot de passe après le `commit` (compte sans mot de passe si ce second temps échoue).

## Suites
