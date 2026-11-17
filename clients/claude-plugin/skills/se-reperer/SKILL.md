---
name: se-reperer
description: Se repérer pendant la COP31 — programme du jour, où et quand se discute un point, quelle réunion suivre, organes et groupes de négociation, sigles. À utiliser pour « où se discute l'adaptation aujourd'hui », « c'est quoi cette réunion », « quel organe traite de… ».
---

# Se repérer

0. `dossiers()` : la carte des sujets de négociation, leur état et leurs alias (GGA, MWP, art6…).
1. `programme(jour="aujourd'hui", sujet="water, indigenous")` : side events officiels (salle, horaire,
   organisateurs, thèmes), réunions officielles du calendrier CCNUCC (dialogues, ateliers, coordinations de
   groupes), résumé des négociations du jour, et Daily Programme déposé s'il y en a un. Mots-clés en anglais,
   séparés par des virgules ; `types=["hors_site"]` pour les événements hors site. La clé `pavillons` donne les
   sessions des pavillons et hubs du jour (Blue et Green Zone) avec résumé et intervenants. Pour « quoi
   aujourd'hui sur mes sujets ? » : `programme(jour="aujourd'hui", mes_sujets=true)` (un bloc par sujet suivi).
   Questions transversales (« quels pavillons ont parlé de… », comptages, tout le programme d'un pavillon) :
   outil `pavillons`.
   - Toujours donner, pour chaque side event, le lien SEORS (`lien`, programme officiel filtré sur le jour) :
     salles et horaires changent. Signaler la source et sa date (`source`, `releve_le`, `a_verifier`).
   - Les consultations informelles par point de l'ordre du jour ne sont dans aucune de ces listes : seul le
     Daily Programme les donne (à déposer en PDF via le bot Telegram ou le tableau de bord).
2. Ordres du jour (`types=["ordre_du_jour"]`, session COP31/CMA8/CMP21, SB64) pour savoir quel organe traite
   quel point.
3. `glossaire` pour les organes et groupes.

Répondre court : quoi, où, quand, quel organe, et le texte de référence. Ne jamais inventer une salle ou un
horaire : sans programme dans le corpus, le dire.
