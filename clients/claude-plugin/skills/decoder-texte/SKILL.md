---
name: decoder-texte
description: Décoder un paragraphe ou un projet de décision onusien (COP/CMA) — force juridique de chaque verbe, renvois aux décisions antérieures, ce qui change concrètement, ce qui est entre crochets. À utiliser quand on colle un extrait de texte, une cote (FCCC/…/L.x) ou « que veut dire ce paragraphe ».
---

# Décoder un texte

Suivre `protocole-cop31`.

1. Si on donne une cote ou un id : `document` puis `lire_passage`. Si on colle un texte : chercher sa source
   (`rechercher` avec une phrase exacte entre guillemets).
2. Pour chaque paragraphe : verbe introductif et force (shall / should / urges / encourages / invites /
   requests [secrétariat] / decides / notes) ; destinataire (Parties, pays développés, secrétariat…) ;
   échéance ; renvois (« decision 1/CMA.5, paragraph 28 » → aller lire le renvoi).
3. Crochets et options : les lister, dire ce que chaque option changerait.
4. Comparer avec le langage déjà agréé (`rechercher` la même formule dans les décisions de niveau 1) : un
   « recul » ou une « avancée » se juge par rapport au langage agréé.

## Rendu
Tableau court : § · ce que ça dit en clair · force · nouveau ou déjà agréé (avec référence). Puis
« En résumé » (2 lignes) et « Points d'attention » pour l'utilisateur.
