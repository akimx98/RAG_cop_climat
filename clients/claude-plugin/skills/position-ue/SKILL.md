---
name: position-ue
description: Établir la position de l'Union européenne sur un sujet de la COP31 (conclusions du Conseil, NDC de l'UE, droit climatique européen, mandat de négociation) et sa cohérence avec ce que l'UE fait chez elle (objectifs 2030/2040, ETS, MACF/CBAM, financement). À utiliser pour préparer une prise de parole, anticiper une attaque contre l'UE, ou répondre à « l'UE est hypocrite parce que… ».
---

# Position de l'UE

Suivre `protocole-cop31`.

1. Position de négociation : `rechercher(..., organisation="Conseil de l'UE")` (conclusions du Conseil
   « préparation de la COP31 », finance climat ECOFIN), puis documents internes de coordination
   (`acces="restreint"`) s'ils existent.
2. Ce que l'UE a demandé par écrit à la CCNUCC : `soumissions(auteur="EU")` puis `soumissions(document=<id>)`
   (points soulevés, force du langage, citations exactes ; niveau 2). Pour anticiper une attaque :
   `comparer_soumissions("EU", "<groupe ou Partie>")` montre où les positions écrites divergent. Qui est dans quel
   groupe : `groupes(groupe=…)` / `groupes(partie=…)` (listes sourcées ; AGN et Groupe arabe par équivalence, à dire ;
   LMDC, AILAC, ALBA, CfRN : liste non vérifiée, ne pas déduire leur composition).
3. Engagements : NDC de l'UE (`types=["ndc"]`, `partie="EU"`), loi climat et objectif 2040 (`types=["loi_ue"]`).
4. Réalité : `donnees(pays="European Union (27)")` et rapports de l'AEE (niveau 3).
5. Attaques prévisibles : MACF/CBAM « protectionniste », finance (art. 9), responsabilité historique (émissions
   cumulées), crédits internationaux dans l'objectif 2040. Pour chacune : ce que disent les textes et les chiffres.

## Rendu
Position (citée, niveau 2) · Base juridique interne (niveau 1) · Chiffres (niveau 3) · Points faibles honnêtes ·
Éléments de langage (2–3 phrases, uniquement fondés sur les sources citées). Les documents R éclairent mais ne
se citent pas à l'extérieur.
