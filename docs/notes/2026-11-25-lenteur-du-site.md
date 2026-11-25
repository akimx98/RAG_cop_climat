# 2026-11-25 — Lenteur du site : pages lourdes dans la boucle d'événements

*Taille : M (boucle d'événements, fils, mémoire du site) · Statut : validé (revue tour 2)*
*Fichiers concernés : `web.py` (décorateurs, pages sans `await`, `_landing`, `how_page`), `web_live.py`
(`dossier_page`, cache), `search.py` (`stats`), `embed.py` (`search`), `server.py` (taille du pool de fils),
`tests/test_lenteur.py`*

## Problème

Le site paraît souvent lent (constat de l'utilisateur, confirmé le 25/11 par une autre session : `/healthz` passe
de 0,38 s à 5,5 s pendant le calcul de `/database/negotiations/finance`). Toutes les pages, même les plus légères,
attendent parfois 7 à 10 s, et certaines requêtes sont abandonnées par le client au bout de 25 s.

Cause : un seul processus uvicorn, une seule boucle d'événements. Les pages sont des `async def` qui interrogent
SQLite et calculent de façon synchrone (PITFALLS, « Boucle d'événements du site »). Pendant qu'une page lourde
calcule, **aucune autre requête du site n'est servie**. Les pages lourdes sont les 18 pages dossier, entrées dans le
sitemap le 25/11 (9326e27) : les moteurs vont les explorer.

## Faits mesurés

| Fait | Valeur | Comment mesuré |
|---|---|---|
| Premier octet de `/`, `/connect`, `/sitemap.xml` vu du dehors | 7 à 10 s (une fois 0,4 s) | `curl -w` depuis le Mac, 25/11 ~19 h 57 |
| Durée des requêtes Caddy sur 30 min (opencop.org) | médiane 0,17 s, 90e centile 7,5 s, max 24,8 s | journal Caddy, champ `duration`, 2 763 requêtes |
| Même pages au calme | `/` 0,22 s, `/connect` 0,01 s, `/doc/…` 0,01 s | `TestClient` sur la base de prod (dans le worker, sans écriture ni vecteurs) |
| Page dossier, détail (adaptation / finance) | `search.search` 12,4 / 6,5 s ; `_frises` 2,6 / 6,7 s ; reste < 0,1 s | chronométrage des fonctions dans le worker |
| `search.search` du dossier adaptation | palier FTS « un » 5,7 s ; palier « tous » 0,07 s ; `embed.encode` (API) 2,4 s ; produit vectoriel 0,8 s ; `_refresh` à froid 3,6 s | idem |
| `search.stats` (accueil, `/database`, `/how-it-works`) | 0,2 à 0,3 s (dont `COUNT(*) FROM vectors` 0,17 s) | idem |
| `catalog.load` (`/how-it-works`) | 0,8 s ; la page entière 1,25 s | idem |
| Robot de Meta (`meta-externalagent`) | 16 716 requêtes en 6 h (≈ 0,8/s), 79 % du trafic sur 30 min ; ignore `Disallow: /lang/` | journal Caddy |
| Coût d'une page `/doc/<id>?passage=…` (ce que visite le robot) | 0,01 s | `TestClient` |
| Mémoire anonyme du site | 561 Mo / 700 Mo (marge ≈ 170 Mo) | `memory.stat` (`anon`) dans `cop31-app-1`, 25/11 ~20 h 15 |
| Vecteurs en mémoire | 216 400 × 512 en float16 | `COUNT(*) FROM vectors` |
| Temporaire du produit vectoriel par appel | bloc de 32 768 × 512 en float32 = 64 Mo | lecture de `embed.search` |
| Connexion SQLite | une par fil (`db.get()`, `threading.local`), `check_same_thread=False`, cache par défaut | lecture de `db.py` |
| Pool de fils d'anyio | 40 fils par défaut | documentation anyio (non mesuré en prod) |

Hypothèses non vérifiées :
- La mémoire d'une connexion SQLite supplémentaire (cache de pages par défaut, ≈ 2 Mo) ; d'où un pool de fils borné.
- Le swap du VPS (1,2 Go utilisés) n'a pas été relié à la lenteur.

## Modes de panne

| Composant | Panne possible | Ce qu'on en voit aujourd'hui | Après le changement |
|---|---|---|---|
| Boucle d'événements | une page lourde fige tout le site | 7–25 s d'attente pour tout le monde | les pages sans `await` tournent dans un fil ; la boucle reste libre (`/healthz` aussi) |
| Pool de fils | 12 fils pris par des pages lentes (ou en attente du même dossier) | — | les pages suivantes attendent un fil ; la boucle, `/healthz` et les flux MCP continuent |
| Mémoire | N recherches simultanées × 64 Mo de temporaire | une seule recherche à la fois (boucle) | bloc réduit à 16 Mo, au plus 2 produits vectoriels simultanés : ≤ 32 Mo |
| Cache des dossiers | résultat périmé | — | au plus 1 h, ou jusqu'au prochain passage indexé ou changement d'accès |
| Cache des dossiers | **document passé en restreint encore montré aux visiteurs** | — | la signature du corpus inclut les documents publics : tout changement d'accès invalide le cache |
| Cache des dossiers | 18 dossiers demandés en même temps, à froid | — | un verrou par clé : un seul calcul par dossier, les autres attendent son résultat |
| Fils | état de module partagé (`_COUNTS`, `usage._ready`, `auth._failures`) | une seule boucle, pas de concurrence | courses bénignes (recalcul ou création de table idempotente) ; caches nouveaux protégés par verrou |
| SQLite | écritures concurrentes depuis plusieurs fils (`usage.record`, `set_lang`) | sérialisées par la boucle | transactions courtes, `busy_timeout` 30 s ; aucune transaction ouverte pendant un appel réseau (inchangé) |
| Chien de garde | le battement `app-boucle` passe par le pool (`run_in_threadpool`) : retardé si le pool est plein | — | seuil d'alerte 10 min ; une page prend au plus ~15 s : pas de fausse alerte attendue |
| Cache des dossiers | verrou par clé tenu pendant l'appel à l'API d'embeddings (2,4 s) | — | seuls les visiteurs du même dossier attendent ; aucune transaction SQLite ouverte (lectures en autocommit) |

Auto-contrôle PITFALLS (liste « à passer sur un diff ») : plantage natif — inchangé ; mémoire — mesurée (561 Mo /
700), bornée par le bloc, le sémaphore et le pool ; code synchrone dans la boucle — c'est l'objet, reste les pages
`async` avec `await` (hors périmètre) et les outils MCP (#4) ; transaction pendant un appel réseau — non ; deux
écrivains d'une même clé `meta` — non ; confidentialité — clé de cache par accès aux restreints, signature qui
suit l'accès, rien vers les journaux ; réglages — `THREADS`, `DOSSIER_TTL`, `SEARCH_STEP`, `SEARCH_CONCURRENCY`
sont des constantes, pas des variables d'environnement ; pages publiques — aucune ajoutée.

## Critères d'acceptation

1. Toute route de `web.py` et `web_live.py` écrite `async def` contient au moins un `await` ; seule exception :
   `/healthz` (`health`), qui doit refléter l'état de la boucle. Test par lecture du code (AST).
2. Les décorateurs `public_page`, `login_required`, `admin_required` gardent une page `def` synchrone : Starlette
   l'exécute dans un fil, pas dans la boucle. Test : la page s'exécute hors du fil de la boucle.
3. Pendant qu'une page dossier calcule (calcul artificiellement lent dans le test), `/healthz` et une page légère
   répondent sans attendre la fin de ce calcul. Test.
4. Page dossier : les résultats « nouveautés » et les frises sont mis en cache par (dossier, accès aux documents
   restreints) ; un second affichage ne relance ni `search.search` ni `_frises`. Test.
5. Le cache est invalidé : au bout de 1 h ; à l'arrivée d'un nouveau passage indexé ; à tout changement d'accès
   d'un document (public ↔ restreint) ou suppression. Test : un document passé en restreint disparaît de la page
   dossier d'un visiteur dès l'affichage suivant.
6. Le cache est borné : clés limitées aux dossiers connus (`live.dossier(slug)` vérifié avant) × 2 variantes
   d'accès ; aucune entrée pour un dossier inconnu.
7. Calcul à froid concurrent d'un même dossier : un seul calcul (verrou par clé). Test.
8. `search.stats` mis en cache 10 min (comme `_corpus_counts`) ; nombre de sources de `/how-it-works` aussi.
9. `embed.search` : produit vectoriel par blocs de 8 192 lignes, au plus 2 produits simultanés (sémaphore) ;
   résultats identiques à l'ancien calcul. Test sur une petite matrice.
10. Pool de fils d'anyio limité à 12 au démarrage du site (`server.py`, `THREADS`).
11. Tous les tests existants passent, en particulier `tests/test_indexation.py` (pages du sitemap en 200, toute
    route GET classée).
12. En production après déploiement : pendant le calcul à froid d'une page dossier, `/healthz` répond en moins
    de 0,5 s ; un second affichage de la page dossier en moins de 0,5 s ; mémoire anonyme du site relevée
    (marge > 100 Mo).
13. (revue, tour 1) Un lien de connexion à usage unique ne s'utilise qu'une fois, même envoyé deux fois en même
    temps depuis deux fils. Test.
14. (revue, tour 1) Un échec du calcul d'une valeur en cache (`_ttl_cached`) n'est pas mis en cache : la requête
    suivante recalcule. Test.

## Conception

**Options pour libérer la boucle.**
- A. Plusieurs processus uvicorn (`--workers 2`) : chaque processus charge ≈ 410 Mo de vecteurs ; impossible dans
  700 Mo. Écarté.
- B. Envelopper chaque appel lourd dans `run_in_threadpool` à la main : beaucoup de points de modification,
  facile à oublier. Écarté.
- C. **Pages sans `await` écrites `def`** : Starlette les exécute d'office dans son pool de fils. Les décorateurs
  produisent un enveloppeur synchrone pour une page synchrone. Une règle simple, vérifiée par test (critère 1).
  Retenu. `_landing` devient synchrone : `home` aussi ; les pages `async` qui l'appellent après `await
  request.form()` passent par `run_in_threadpool`.

**Options pour la page dossier.**
- Cache des résultats calculés (hits et frises), pas du HTML : la langue et l'utilisateur (suivi, messages)
  restent rendus à chaque fois. La clé ne dépend de l'utilisateur que par `auth.can_restricted(user)`, seule
  dépendance de `search._filters_sql` et de `formulations.lineage` à l'utilisateur.
- Signature du corpus, relue à chaque affichage (≈ 1 ms) : `MAX(passages.id)`, et `COUNT(*)`, `TOTAL(id)` des
  documents publics. Un changement d'accès ou une suppression change le nombre ou la somme des identifiants.
- Durée de vie 1 h : la fenêtre « 7 derniers jours » glisse lentement.

**Mémoire.** Bloc de 8 192 lignes (16 Mo en float32) au lieu de 32 768 ; sémaphore de 2 autour du produit
vectoriel ; l'appel à l'API d'embeddings reste hors sémaphore. Pool de 12 fils (connexions SQLite : une par fil).

**Hors périmètre** (à suivre en issues) :
- outils MCP synchrones dans la boucle (issue #4, même mécanisme) ;
- palier FTS « n'importe quel mot » lent (5,7 s) pour les recherches libres de `/database` ;
- robot de Meta qui ignore robots.txt : ses pages coûtent 0,01 s, il ne gêne plus une fois la boucle libérée ;
- pages `async` avec `await` (formulaires, admin) qui font aussi du travail synchrone après l'`await` ;
- swap du VPS.

## Déploiement

`scripts/ship.sh --app` (le worker n'est pas touché). Au premier démarrage, le cache est vide : le premier
affichage de chaque dossier coûte comme avant, mais dans un fil. Retour arrière : `scripts/ship.sh --rollback`.

Vérifications après mise en production :

```bash
# boucle libre pendant un calcul à froid de page dossier
( curl -s -o /dev/null -w "dossier %{time_total}\n" https://opencop.org/database/negotiations/finance & ) ; sleep 1
curl -s -o /dev/null -w "healthz %{time_total}\n" https://opencop.org/healthz
curl -s -o /dev/null -w "dossier (cache) %{time_total}\n" https://opencop.org/database/negotiations/finance
ssh … 'sudo docker exec cop31-app-1 grep ^anon /sys/fs/cgroup/memory.stat'
```

## Revue

Tour 1 (agent `reviewer`, Sonnet) : non bloquant sous réserve d'un correctif ; critères 1 à 11 tenus.
- Accepté, **ajouté aux critères (13)** : `login_link` passée dans un fil, `auth.by_login_link` faisait
  « lire `meta` puis écrire » : deux POST simultanés du même lien ouvraient deux sessions (la boucle les
  sérialisait sans le dire). → Consommation atomique (`INSERT OR IGNORE`, `rowcount`). Autres « tester puis
  écrire » des pages passées en `def` : aucun (UPDATE idempotents), vérifié par le relecteur et par moi.
- Accepté, **ajouté aux critères (14)** : `_ttl_cached` gardait un échec 10 min (`/database` en 500 pendant
  10 min après un « database is locked »). → Un échec n'est jamais mis en cache.
- Accepté : test de pic de concurrence du produit vectoriel (critère 9) ; commentaire du pool corrigé (seuls
  `deposer_url` et le journal MCP y passent, les autres outils restent dans la boucle : #4) ; mise en forme.
- Noté : la signature du cache ne suit pas `duplicate_of` ni un titre modifié (au plus 1 h de retard, sans effet
  sur la confidentialité) ; critère 10 vérifié à la main (pool à 12 au démarrage de l'application complète).

Tour 2 (relecteur neuf) : non bloquant, critères 1 à 11, 13 et 14 conformes, 204 tests verts. Remarques gardées
pour les suites : `auth._failures` sans verrou (appelé seulement depuis des pages `async`, à protéger si une page
`def` l'utilise) ; jusqu'à 12 calculs à froid de dossiers différents peuvent occuper le pool (à mesurer, critère
12) ; lien brûlé si `mark_verified` échoue après sa consommation (échec fermé, rare).

## Suites

À créer à la livraison.

## Promotion (à la livraison)

- [ ] pièges → PITFALLS ; faits → architecture, commentaires dans le code
- [ ] suites → issues `suite`
- [ ] statut « livré » ici et dans l'index
