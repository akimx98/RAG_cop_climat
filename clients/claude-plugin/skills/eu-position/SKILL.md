---
name: eu-position
description: Establish the European Union's position on a COP31 topic (Council conclusions, EU NDC, EU climate law, negotiating mandate) and its consistency with what the EU does at home (2030/2040 targets, ETS, CBAM, finance). Use to prepare a statement, anticipate an attack on the EU, or answer "the EU is hypocritical because…" (also « position de l'UE », « l'UE est hypocrite »).
---

# EU position

Follow `cop31-protocol`. Answer in the user's language.

1. Negotiating position: `rechercher(..., organisation="Conseil de l'UE")` (Council conclusions "preparations for
   COP31", ECOFIN climate finance), then internal coordination documents (`acces="restreint"`) if any.
2. What the EU asked the UNFCCC in writing: `soumissions(auteur="EU")` then `soumissions(document=<id>)` (points
   raised, language strength, exact quotes; level 2). To anticipate an attack:
   `comparer_soumissions("EU", "<group or Party>")` shows where written positions diverge. Who is in which group:
   `groupes(groupe=…)` / `groupes(partie=…)` (sourced lists; AGN and Arab Group by equivalence — say so; LMDC,
   AILAC, ALBA, CfRN: unverified list, do not infer their membership).
3. Commitments: EU NDC (`types=["ndc"]`, `partie="EU"`), climate law and 2040 target (`types=["loi_ue"]`).
4. Reality: `donnees(pays="European Union (27)")` and EEA reports (level 3).
5. Predictable attacks: CBAM as "protectionist", finance (Art. 9), historical responsibility (cumulative
   emissions), international credits in the 2040 target. For each: what the texts and figures say.

## Output
Position (quoted, level 2) · Domestic legal basis (level 1) · Figures (level 3) · Honest weak points ·
Talking points (2–3 sentences, based only on the cited sources). R documents inform but are never quoted outside.
