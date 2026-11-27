# 2026-11-27 — Mémoire du site : chargement des vecteurs par blocs

*Taille : M (mémoire du site) · Statut : livré (49de307, déployé le 27/11)*
*Fichiers concernés : `embed.py` (`_refresh`, `search`, `_cache`), `tests/test_core.py` ou `tests/test_vecteurs.py`*

## Problème

À la première recherche après un démarrage, le site charge tous les vecteurs d'un coup : `fetchall()` (une ligne
Python et un objet `bytes` par vecteur), puis `b"".join` (une copie), puis `np.vstack` (une seconde copie). Le pic
atteint 2,5 fois la taille utile : le conteneur touche son plafond (700 Mo), le noyau récupère de la mémoire en
continu et envoie une partie du site en swap. Chaque nouveau lot de vecteurs refait un `vstack` de toute la matrice
(copie complète). Le corpus grandit : le jour où le pic dépasse le plafond, le site est tué à la première recherche
qui suit chaque redémarrage.

## Faits mesurés

| Fait | Valeur | Comment mesuré |
|---|---|---|
| Vecteurs | 216 917 × 512 en float16 = **222 Mo** utiles | `SELECT count(*) FROM vectors`, 27/11 |
| Site avant la première recherche | anon 21 Mo, swap 66 Mo | `memory.stat`, 27/11 10:05 UTC |
| Pendant la première recherche | anon **560 Mo**, `memory.current` 592 Mo, swap 195 Mo, `memory.peak` = **700 Mo** (le plafond) | échantillons toutes les 0,5 s, 27/11 |
| Plafond atteint (`memory.events` max) | 1 231 fois après le déploiement (smoke), 1 519 après la recherche | `memory.events` |
| Après chargement | anon 510 Mo stable | idem, recherches suivantes |
| Durée | première recherche 8,7 s ; suivantes 1,1–1,3 s | `curl -w %{time_total}` sur `/database?q=` |
| Worker, courrier | 110–176 Mo / 600 ; 50 / 150 : marge | `memory.peak`, 27/11 |
| VM | 3,8 Go de RAM, swap 4 Go (1,3 Go utilisés, surtout `rag-embed`) ; aucun `oom_kill` | `free -m`, `/proc/vmstat` |

L'issue #17 (« VPS sans swap ») est inexacte à ce jour : le swap existe.

Hypothèse non vérifiée : les 510 Mo stables (contre 222 Mo utiles) viennent surtout de la mémoire libérée par les
lignes Python mais gardée par l'allocateur (fragmentation), plus les caches de pages. À mesurer après le changement.

## Modes de panne

| Composant | Panne possible | Aujourd'hui | Après |
|---|---|---|---|
| Site, première recherche | pic > plafond → arrêt brutal (OOM) | pic au plafond, swap | pic ≈ matrice + un bloc |
| Site, nouveaux vecteurs | copie complète à chaque lot | `vstack` à chaque recherche qui voit de nouveaux vecteurs | croissance par paliers (copie rare) |
| Recherche en cours pendant un rechargement | lecture d'un tableau en cours de remplissage | non concernée (nouveau tableau) | la recherche lit une vue `[:n]` prise sous verrou ; les lignes au-delà de `n` ne sont jamais lues |
| Ligne supprimée entre le décompte et la lecture | décompte faux | — | on garde le nombre réellement lu ; tableau agrandi si plus de lignes que prévu |

## Critères d'acceptation

1. `_refresh` lit les vecteurs par blocs (`fetchmany`), sans `fetchall` ni `b"".join` de toute la table, et les
   écrit en place dans des blocs fixes préalloués.
2. Test : sur N vecteurs, le pic mesuré par `tracemalloc` pendant le premier chargement reste sous
   **taille de la matrice + 32 Mo** (surcoût constant d'un bloc, indépendant de N ; aujourd'hui ≈ 2,5 × la
   matrice).
3. Test : chargements successifs (N puis M nouveaux) → mêmes identifiants et mêmes vecteurs, dans le même ordre,
   qu'un chargement unique ; `search` renvoie les mêmes résultats qu'avant le changement (test existant
   `test_core` vert).
4. Aucune copie de la matrice : ajouter des vecteurs ne recopie jamais les blocs existants (un bloc neuf est
   alloué quand le dernier est plein). Des lignes arrivées entre deux lectures, ou une erreur au milieu d'un
   chargement, ne corrompent ni les identifiants ni les vecteurs (tests).
5. `search` ne lit jamais au-delà des lignes remplies, même pendant un rechargement concurrent.
6. Après déploiement : première recherche, `memory.peak` remis à zéro puis mesuré **< 450 Mo** ; plus aucune
   hausse du compteur `max` de `memory.events` pendant cette recherche.

## Conception

- Retenu : **blocs fixes** de `BLOCK` = 8 192 vecteurs (8 Mo en float16), chacun avec ses identifiants, alloués à
  la demande et jamais recopiés. `_refresh` lit par `fetchmany(BLOCK)` et écrit en place ; `n` (lignes remplies)
  et `max` sont publiés après chaque bloc lu, sous `_lock`. `search` prend sous le verrou des vues `[:k]` des lignes
  remplies de chaque bloc (`_snapshot`), puis calcule bloc par bloc (conversion float32 de 16 Mo, comme avant).
- Première version (revue tour 1) : un tableau unique agrandi par paliers de 25 %. Écartée : `_grow` recopiait
  avec le `n` publié et perdait les blocs écrits pendant le même chargement (constat bloquant), et chaque
  agrandissement recopiait toute la matrice (pic ancien + nouveau ≈ 500 Mo).
- Écarté aussi : `np.memmap` d'un fichier de vecteurs à part (deuxième source de vérité) ; float32 en mémoire
  (double la taille).

Hors périmètre : vecteurs de passages supprimés gardés en mémoire jusqu'au redémarrage (comportement actuel,
quelques Mo) ; décision sur le plafond du site (on remesure d'abord).

## Déploiement

`scripts/ship.sh` (site et worker). Retour arrière : `scripts/ship.sh --rollback`. Vérification : critère 6.

## Revue

Tour 1 (`reviewer`) : **bloquant**. Constat 1 accepté : `_grow` recopiait `[:n]` avec le `n` publié au lieu du `n`
local ; si le tableau grandissait au milieu d'un chargement (lignes arrivées après le décompte), des lignes non
initialisées devenaient visibles (mauvais identifiants). Retour à la conception plutôt que rustine : blocs fixes,
plus de recopie du tout, plus de décompte préalable. Tests ajoutés : lignes arrivées pendant la lecture, erreur au
milieu d'un chargement. Constat 2 (test de `_grow` hors verrou) : sans objet, `_grow` n'existe plus.

Tour 2 (`reviewer`, nouveau) : **non bloquant**, critères 1 à 5 conformes, 217 tests verts. Constats mineurs :
critère 5 testé sans fils concurrents (l'invariant tient par construction : écriture au-delà de `n`, vues `[:k]`
avec `k ≤ n`) ; le premier chargement garde `_lock` et la lecture SQLite ouverte pendant toute sa durée, comme
avant avec `fetchall` (les autres recherches attendent) : à mesurer (critère 6).

## Mesures après déploiement (27/11)

| Fait | Avant | Après |
|---|---|---|
| Pic du chargement (processus seul, `ru_maxrss`, conteneur jetable `run --rm`) | ≈ 560 Mo anon dans le site | **270 Mo** (27 au départ + 212 de matrice) ; 1,3 s |
| Mémoire anonyme du site après chargement | 510 Mo | **342 Mo** |
| Compteur `max` pendant deux recherches | +288 | **+0** |
| Recherche libre (`/database?q=`) | 8,7 s (1re), 1,1–1,3 s | 4,1 s (1re, encodage compris), 0,8 s |

Le site reste à `memory.current` ≈ 696 Mo : c'est le cache de pages de la base (fichier, récupérable), pas de la
mémoire anonyme. Le compteur `max` monte au déploiement parce que `cop smoke` (lancé par `ship.sh` dans le
conteneur du site) charge aussi les vecteurs dans un second processus. Écrire dans `memory.peak` ne le remet à
zéro que pour le descripteur qui écrit (noyau 6.12) : mesurer par `ru_maxrss` ou par échantillons de `anon`.

Décision sur les plafonds : **aucun changement**. Site 342 Mo anon sur 700, worker ≤ 176 sur 600, courrier 50 sur
150 ; la VM a 4 Go de swap (l'issue #17 disait le contraire, à tort). Pas de VM plus grosse nécessaire.

## Suites
