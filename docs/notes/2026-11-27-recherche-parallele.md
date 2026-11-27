# 2026-11-27 — Recherche : partie sémantique en parallèle, encodage des requêtes en cache

*Taille : M (recherche, coûts LLM, fils) · Statut : brouillon*
*Fichiers concernés : `search.py` (`search`, `_semantic`), `embed.py` (`_encode_query`, `search`),
`tests/test_recherche_parallele.py`*
*Issue : #14*

## Problème

La recherche (`search.search`) sert la recherche libre du site, l'outil MCP `rechercher`, les pages dossier et la
démo. Elle enchaîne la partie lexicale (paliers FTS) puis la partie sémantique (encodage de la requête chez OpenAI,
puis produit scalaire sur 217 000 vecteurs). Les deux sont indépendantes jusqu'à la fusion des rangs, et toutes
deux libèrent le GIL (SQLite pendant l'exécution, numpy pendant la conversion et le produit, réseau pendant
l'encodage). Les pages dossier et les outils relancent souvent les mêmes requêtes : chaque fois, un appel OpenAI.

## Faits mesurés (production, conteneur jetable `run --rm`, lecture seule, 27/11)

| Fait | Valeur |
|---|---|
| Recherche complète, requête longue (8 mots) | 1,39 s à 2,47 s |
| dont requêtes SQL (palier « un des mots » surtout, 176 000 passages trouvés) | 0,62 s (cProfile) ; palier seul 0,23 à 1,59 s selon la requête |
| dont conversion float16 → float32 des vecteurs | 0,32 s |
| dont encodage OpenAI | 0,18 à 0,34 s (1,27 s pour le premier appel : connexion) |
| Recherche libre de bout en bout (`/database?q=`, 6 requêtes) | 1,0 à 3,2 s |

## Modes de panne

| Composant | Panne possible | Aujourd'hui | Après |
|---|---|---|---|
| Durée | somme lexical + sémantique | ~1,4–2,5 s | ≈ max des deux |
| Partie sémantique en échec | — | lexical gardé, message au journal | idem (exception reprise du fil) |
| Fil sémantique | connexion propre au fil d'un autre test ou d'une autre base | — | connexion ouverte et fermée à chaque appel |
| Cache d'encodage | mémoire, requête sensible gardée | — | 512 requêtes × 2 Ko ≈ 1 Mo ; uniquement en mémoire du processus (pas de disque, pas de journal) |
| Concurrence | trop de produits vectoriels | sémaphore de 2 | inchangé (`embed._search_slots`) |

## Critères d'acceptation

1. Résultats identiques (mêmes passages, même ordre, mêmes `signals`) entre l'ancien enchaînement et le nouveau,
   sur un corpus de test (test qui compare avec `semantic` exécuté en ligne).
2. Durée ≈ max(lexical, sémantique) : test avec une partie sémantique lente simulée (0,5 s) et une partie lexicale
   lente simulée (0,5 s) → total < 0,8 s.
3. Une erreur de la partie sémantique laisse les résultats lexicaux (test).
4. Une même requête n'est encodée qu'une fois tant qu'elle est en cache (test : compteur d'appels).
5. Suite complète verte ; après déploiement : durées de `/database?q=` mesurées sur les 6 requêtes de référence.

## Conception

- `search.search` lance `_semantic(query)` dans un pool de 4 fils dès l'entrée, fait le lexical, puis attend le
  résultat. `_semantic` ouvre sa propre connexion (`db.connect`) et la ferme. La fusion des rangs reste après le
  lexical, inchangée.
- `embed._encode_query(query)` : `functools.lru_cache(512)` autour de `encode([query])[0]`.
- Écarté : sauter le palier « un des mots » quand les paliers précédents suffisent (changerait le classement :
  à décider sur des mesures de pertinence, hors périmètre) ; stocker les vecteurs en float32 (double la mémoire).

## Déploiement

`scripts/ship.sh`. Retour arrière `--rollback`. Vérification : critère 5.

## Revue

Tour 1 (`reviewer`) : **non bloquant**, critères 1 à 4 conformes (fusion des rangs inchangée, seule la source des
candidats change), 241 tests verts. Constat 1 accepté : marge du test de durée relevée (0,95 s ; en série > 1,0 s).
Reportés : 2. si le lexical lève avant d'attendre, la tâche sémantique va à son terme (un appel OpenAI inutile ;
connexion bien fermée) ; 4. `pending.result()` sans délai propre si l'API d'embeddings reste bloquée (comme avant,
`encode` a son délai réseau). Pas de tour 2 : seule la marge d'un test a changé.

## Suites
