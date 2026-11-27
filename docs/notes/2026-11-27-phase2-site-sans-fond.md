# 2026-11-27 — Phase 2 : le site ne calcule plus en fond

*Taille : M (boucle et fils du site, mémoire, base) · Statut : livré (27/11, fcb3037) · Programme :
[revue d'architecture](2026-11-27-revue-architecture.md)*
*Fichiers concernés : `web_live.py` (préchauffage, `_heavy`, `_frises`, `dossier_page`), `server.py` (lancement,
`lifespan`), `worker.py` (fil `frises`), nouveau `frises.py`, `db.py` (table, `META_OWNERS`), `extract.py`
(`_slots`), `search.py` (journal des durées), tests `test_dossiers.py`, `test_lenteur.py`, `test_un_ecrivain.py`*

## Problème

Le fil `prechauffage` du site recalcule toutes les pages dossier (recherche « nouveautés » et frises) à chaque
nouveau passage indexé ou toutes les heures : ≈ 30 s d'un cœur dans le processus qui répond aux visiteurs
(même GIL), et ≈ 80 Mo de documents découpés gardés en mémoire (site à 436 Mo / 700). Le cache est aveugle aux
titres et aux doublons (#18). La première recherche après un redémarrage coûte 2 à 8,6 s (vecteurs à froid).

## Faits mesurés (27/11, 14 h 45–15 h 10 UTC, lecture seule)

| Fait | Valeur | Comment |
|---|---|---|
| Frises publiques, 18 dossiers | 26,4 s à froid, 7,2 s documents en cache ; JSON 230 Ko en tout | `web_live._frises`, base `ro` |
| Palier lexical « nouveautés » (filtre 7 jours), 18 dossiers | 0,22 à 0,66 s chacun | SQL seul |
| `/database/negotiations/*` (4 h) | p50 0,12 s, p90 0,29 s | Caddy |
| `/database?q=` (24 h) | p50 1,2 s ; premier appel après redémarrage 2,2 à 8,6 s (vecteurs 3,6 s + connexion OpenAI) | Caddy |
| Palier FTS « un » | médiane 0,46 s, max 1,2 s ; aucune variante à résultat identique plus rapide | SQL seul |
| Mémoire anonyme | site 436 Mo / 700 ; worker 93 Mo / 600 | cgroup |
| Vecteurs dans le worker | impossible sans risque : 222 Mo + 250 Mo d'un sous-processus PDF + base > 600 Mo | estimation |

## Conception

Option complète écartée : faire calculer aussi les « nouveautés » par le worker demanderait un balayage des vecteurs
en flux, un paramètre de candidats dans `search.search` et un filtre de sécurité : beaucoup de mécanique pour
4 utilisateurs actifs. **On ne déplace que la partie lourde (les frises)**, qui n'a pas besoin des vecteurs :

- **Worker** : nouveau module `frises.py` (seul écrivain de la table `dossier_frises`, `compute_all` dans
  `WORKER_ONLY`), fil `frises` (60 s, battement `hb:frises`, attendu à 90 min). Il recalcule les frises d'un
  dossier quand la signature de ses textes change (empreinte des documents de ses lignées : id, version, titre,
  accès, `duplicate_of`, statut ; et de `live_texts`) ou quand le format change ; il tient `extract._slots`
  pendant un tour (jamais en même temps qu'une lecture de PDF). Écriture par dossier, transaction courte.
  Variante `public` seulement tant qu'aucun document n'est restreint (0 aujourd'hui) ; sinon aussi `restreint`.
- **Site** : `dossier_page` lit les frises dans la table (≈ 2 ms) ; ne garde à l'affichage que les versions encore
  accessibles à l'utilisateur. « Nouveautés » : calculées à la demande (≈ 0,5–1 s), cache en mémoire par dossier
  avec une signature étendue aux titres et aux doublons (#18) et un TTL d'1 h ; un échec n'est jamais gardé.
  Plus de fil `prechauffage` ; plus de frises calculées par le site (sauf `/diff`, à la demande, inchangé).
  Frises absentes (premier déploiement, nouveau dossier) : le bloc « Au fil des versions » est masqué, sans
  calcul sur place, jusqu'au tour suivant du worker.
- **Démarrage du site** : vecteurs chargés de façon synchrone dans le `lifespan` (≈ 4 s, `ship.sh` attend jusqu'à
  60 s) : plus de premier appel à froid.
- **Mesure** : une ligne de journal par recherche avec les durées des parties (jamais la requête).
- `/database?q=` : pas de raccourci du palier « un » (changerait le classement) ; seuil réaliste 1,5 s.

## Modes de panne

| Composant | Panne | Après |
|---|---|---|
| Fil `frises` mort | frises qui vieillissent | `hb:frises` (blocage à 90 min) ; versions inaccessibles filtrées à la lecture |
| Base verrouillée | ligne non écrite | transaction courte, retentée au tour suivant |
| Arrêt du worker en plein tour | une partie des dossiers à jour | chaque ligne est cohérente ; le reste au tour suivant |
| Mémoire du worker | frises + PDF | `extract._slots` tenu pendant le tour ; documents découpés libérés à la fin du tour |
| Document devenu restreint | montré dans une frise ? | non : filtre à la lecture |

## Critères d'acceptation

1. Le site ne démarre plus aucun fil durable hormis le chien de garde et le battement de la boucle (liste fermée des
   fils liés à une requête : démo, `monitor._spawn`, pool sémantique) ; test AST ; `start_warming`,
   `warm_forever`, `_heavy_cache` n'existent plus.
2. `dossier_frises` n'est écrite que par `frises.py` ; `frises.compute_all` dans `WORKER_ONLY` (test).
3. Les frises stockées sont identiques à celles que calculait `web_live._frises` pour les mêmes textes (test ; et
   script de comparaison en production, base `ro`, sur les 18 dossiers).
4. Recalcul quand un texte du dossier change de version, de titre, d'accès, de `duplicate_of`, ou quand
   `live_texts` change ; pas de recalcul sinon (tests).
5. « Nouveautés » : un titre modifié ou un doublon apparu se voit au prochain affichage (#18, test) ; un échec
   n'est pas mis en cache (test).
6. Une version devenue inaccessible disparaît des frises d'un visiteur sans recalcul (test).
7. Frises absentes : page 200 sans le bloc, aucun calcul de frise dans le site (test).
8. Vecteurs chargés avant la première requête (test sur le `lifespan`).
9. Le fil `frises` tient `extract._slots` pendant un tour ; un dossier en échec n'empêche pas les autres et passe
   par `monitor.report` (tests).
10. En production : mémoire anonyme du site < 400 Mo une heure après ; worker < 360 Mo pendant un tour ; sonde
    scriptée : 18 pages dossier < 1 s chacune ; `/database?q=` p90 < 1,5 s hors premier appel ; CPU du site ≈ 0
    pendant un tour du worker.
11. Suite complète verte.

## Hors périmètre

Classement de la recherche ; `/diff` et `/how-it-works` à froid ; vecteurs arrivés dans le désordre
(`embed.py:128`, suite) ; schéma unique (phase 3).

## Déploiement

1. `scripts/ship.sh` (worker et site) avec tout le changement ; le fil `frises` remplit la table au premier tour
   (≈ 30 s d'un cœur) ; pendant ces secondes, le bloc frises peut être masqué (accepté).
2. Vérifications : `SELECT slug, length(value), duration_ms FROM dossier_frises` (base `ro`) ; mémoire du worker
   pendant le tour ; script d'égalité (critère 3) ; sonde (critère 10).
3. Retour arrière : `ship.sh --rollback` ; table orpheline inoffensive (< 1 Mo).

## Revue

Tour 1 (`reviewer`, 27/11) : **non bloquant**, critères 1–9 et 11 remplis ; aucune fuite d'une version restreinte
vers un visiteur. Corrigés avant livraison : `frises.read` ne lève jamais (ligne illisible ou d'un autre format =
absente, signalée) ; un dossier en échec n'est réessayé qu'après 30 min ou un changement de signature (il tenait
`extract._slots` chaque minute). Accepté : une bascule entre deux versions encadrant une version devenue
restreinte disparaît jusqu'au recalcul (transitoire, sans fuite). Écart à la note accepté : signature élargie à la
cote, aux dates, `n_passages`, `lineage_key` (affichés dans la frise).

## Vérifications en production (27/11, 15 h 20 UTC)

- Comparaison des frises stockées à l'ancien calcul (`scripts/compare_frises.py`, conteneur jetable, base `ro`) :
  **18 dossiers identiques, 0 différence**.
- Mémoire anonyme : site **309 Mo** (436 avant) ; worker 142 Mo pendant les tours.
- Pages dossier : **0,20–0,27 s** en cache ; premier affichage après redémarrage ou changement du corpus
  **1,8 à 7,9 s** (nouveautés calculées à la demande) → suite #32.
- `/database?q=` (6 requêtes de référence) : 1,07 à 2,6 s ; journal `[recherche]` : lexical 0,07–1,55 s,
  sémantique 0,2–1,4 s, total ≈ 1–1,6 s.

## Suites

- #32 : premier affichage d'une page dossier après un changement du corpus (1 à 8 s).
- #18 fermée.

## Promotion (à la livraison)

- [x] PITFALLS (« le site lit, le worker écrit » : calculs lourds dans le worker, table lue par le site)
- [x] architecture.md §1 et §6 ; statut ici, dans l'index et dans « Avancement » ; fermer #18
