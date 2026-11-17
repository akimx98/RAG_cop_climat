---
name: point-negociation
description: Faire le point sur l'état d'un point de négociation de la COP31 (atténuation, adaptation/GGA, finance/NCQG, transition juste, article 6, pertes et préjudices, bilan mondial, transparence…) : dernière version du texte, ce qui a changé depuis la version précédente, options et crochets restants, positions connues, écart avec la position de l'UE. À utiliser pour « où en est-on sur… », « qu'est-ce qui a bougé », « nouvelle version du texte ».
---

# Point de négociation

Suivre `protocole-cop31`.

1. **`dossier(<alias>)`** d'abord (ex. `dossier("GGA")`, `dossier("finance")`, liste : `dossiers()`) : état du
   dossier, textes successifs avec crochets et options, positions relevées pendant la COP
   (citation exacte + source), positions de référence (Carbon Brief), notes de terrain internes.
2. Dernière version du texte : le texte le plus récent de `dossier(...)`. S'il est « à récupérer », le dire et
   donner son lien unfccc.int (un contributeur l'ouvre et le transfère au bot). Sinon
   `comparer_versions(<lien ou cote>)` → ce qui a changé ; `lire_passage` pour les paragraphes modifiés.
3. Qui bouge : `positions(dossier=..., depuis="48h")` ; comparer aux positions de référence d'avant la COP.
   Positions ÉCRITES (soumissions officielles, niveau 2 pour une Partie) : rubriques `soumissions` (appels liés au
   dossier, qui a soumis, alignements déjà calculés) et `positions_ecrites` de `dossier(...)` ; détail d'un appel :
   `soumissions(appel=<id>)` ; accords et conflits entre deux acteurs :
   `comparer_soumissions("EU", "<groupe>", dossier="<alias>")` (indication calculée par règles, à présenter comme
   une aide à l'analyse) ; vue d'ensemble : `soumissions(dossier="<alias>", acteurs=[…])` (paires déjà comparées).
   Un groupe qui n'a pas soumis en son nom : la comparaison passe par les soumissions de ses membres — le dire
   (« soumissions de membres, pas une position de groupe », avec la couverture). Une soumission dit ce qu'un acteur a demandé par
   écrit avant ou pendant la session ; `positions` dit ce qui a été relevé en salle et dans la presse.
4. Compléter par `rechercher` (anglais onusien, `session="CMA8"`…) et `nouveautes(depuis="48h")` si besoin.
5. Position de l'UE : conclusions du Conseil (niveau 2) — voir `position-ue`. Notes internes (R) : à utiliser,
   jamais à recopier à l'extérieur.
6. Citer « selon le suivi de Carbon Brief » pour l'index des textes et les positions de référence.

## Rendu
```
<Sujet> — version du <date heure>, <cote ou titre> (non adopté)
Où on en est : <2–3 lignes>
Ce qui a bougé depuis <version précédente> : <paragraphes ajoutés/supprimés/modifiés ; crochets x → y>
Options encore ouvertes : <liste courte, citée>
Qui pousse quoi : <groupes/pays, source et niveau>
Positions écrites : <appels liés, qui a soumis ; alignements connus (aide à l'analyse)>
Écart avec la position UE : <…>
À surveiller : <prochaine étape, réunion>
```
Proposer de suivre le dossier (`/suivi <alias>` dans Telegram ou bouton « Suivre » de la page dossier) pour
recevoir chaque nouveau texte ; `noter(...)` pour consigner ce qui a été entendu en salle.
