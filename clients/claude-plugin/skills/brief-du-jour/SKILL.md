---
name: brief-du-jour
description: Produire le brief quotidien de la COP31 (ou depuis une heure donnée) — nouveaux projets de texte et décisions, ce qui a changé, documents internes reçus, comptes rendus de la veille, alertes sur mes sujets, programme du jour. À utiliser pour « brief du matin », « quoi de neuf depuis hier soir », « résume la journée ».
---

# Brief du jour

Suivre `protocole-cop31`.

0. `dossiers()` : état de chaque dossier ; pour les dossiers actifs (nouveaux textes, positions), `dossier(slug)`
   donne le dernier brief (rédigé par IA, sources numérotées), les textes et les positions relevées.
   `positions(depuis="18h")` : qui a dit quoi depuis hier soir.
1. `nouveautes(depuis="18h")` (ou la période demandée), puis par type : projets de texte et documents L
   (niveaux 1–2), documents internes (R), comptes rendus (4), communiqués (5).
2. Pour chaque projet de texte ayant une version précédente : `comparer_versions` (1 ligne : ce qui a bougé).
3. `mes_sujets` → alertes récentes.
4. `programme()` pour la journée.

## Rendu (1 écran)
**À retenir** (3 puces) · **Textes** (sujet — version — ce qui a bougé) · **Interne** · **Ce que disent les
observateurs** (ENB/Carbon Brief, niveau 4) · **Aujourd'hui** (programme) · **Mes alertes**. Liens partout.
