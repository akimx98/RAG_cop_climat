---
name: verifier-affirmation
description: Vérifier (fact-checker, débunker) une affirmation entendue en négociation, en réunion ou dans la presse sur le climat, l'Accord de Paris, les engagements d'un pays, la politique de l'UE, les émissions ou le financement climat. Ex. « la Chine émet plus que l'UE et les États-Unis réunis », « l'Accord de Paris oblige à sortir des fossiles », « l'UE ne tient pas ses engagements de financement ». Rend un verdict sourcé et une réplique utilisable en réunion.
---

# Vérifier une affirmation

Suivre d'abord le protocole `protocole-cop31`.

## Étapes
1. **Reformuler** l'affirmation en une ou plusieurs propositions vérifiables (qui, quoi, quand, quel chiffre,
   quel texte). Repérer les glissements typiques : annuel vs cumulé vs par habitant ; CO2 vs GES ; avec ou sans
   UTCATF ; « engagement » (niveau 2) vs « obligation » (niveau 1) ; décision COP vs traité.
2. **Chercher** dans l'ordre : textes adoptés (niveau 1) → documents officiels (2) → données (3, outil `donnees`)
   → comptes rendus (4). Le niveau 5 sert seulement à montrer qui affirme quoi.
3. **Lire** les passages décisifs (`lire_passage`) ; vérifier le verbe (shall/should/urges).
4. **Verdict** parmi : Exact · Exact mais trompeur · Partiellement faux · Faux · Invérifiable dans le corpus.

## Rendu
```
Verdict : <verdict>
En une phrase : <ce qui est vrai>
Preuves :
- « citation exacte » — <référence>, niveau <n> (<lien>)
- <chiffre> — <source, année, indicateur>
Ce que l'affirmation omet : <nuance décisive>
Réplique (2 phrases, ton posé, sans jargon) : « … »
Limites : <ce que le corpus ne permet pas de trancher>
```
La réplique ramène au **fond** (ce que le texte dit vraiment, ce que les chiffres montrent) plutôt qu'au
détail procédural. Ne jamais fabriquer de chiffre : si `donnees` ne l'a pas, le dire.
