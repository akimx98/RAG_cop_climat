---
name: fact-check
description: Fact-check (debunk) a claim heard in a negotiation, a meeting or the press about the climate, the Paris Agreement, a country's commitments, EU policy, emissions or climate finance. E.g. "China emits more than the EU and the US combined", "the Paris Agreement requires phasing out fossil fuels", "the EU is not meeting its finance commitments" (also « vérifie cette affirmation », « c'est vrai que… », « débunke »). Returns a sourced verdict and a reply usable in a meeting.
---

# Fact-check a claim

Follow `cop31-protocol` first. Answer in the user's language.

## Steps
1. **Restate** the claim as one or more checkable propositions (who, what, when, which figure, which text). Spot
   the usual slips: annual vs cumulative vs per capita; CO2 vs GHG; with or without LULUCF; "commitment"
   (level 2) vs "obligation" (level 1); COP decision vs treaty.
2. **Search** in this order: adopted texts (level 1) → official documents (2) → data (3, `donnees` tool)
   → reports (4). Level 5 only shows who claims what; it never refutes levels 1–3.
   Claims about what a country or group "demands" or "refuses": its official submissions are authoritative for
   its written position (`soumissions(auteur="<actor>")`, then `soumissions(document=<id>)`: exact quotes, verb
   strength); an observer's submission remains level 5.
3. **Read** the decisive passages (`lire_passage`); check the verb (shall/should/urges).
4. **Verdict**, one of: Accurate · Accurate but misleading · Partly false · False · Not verifiable in the corpus.

## Output
```
Verdict: <verdict>
In one sentence: <what is true>
Evidence:
- "exact quote" — <reference>, level <n> (<link>)
- <figure> — <source, year, indicator>
What the claim leaves out: <decisive nuance>
Reply (2 sentences, calm tone, no jargon): "…"
Limits: <what the corpus cannot settle>
```
The reply brings the discussion back to **substance** (what the text actually says, what the figures show)
rather than procedural detail. Never make up a figure: if `donnees` does not have it, say so.
