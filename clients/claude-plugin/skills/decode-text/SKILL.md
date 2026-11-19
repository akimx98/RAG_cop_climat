---
name: decode-text
description: Decode a UN paragraph or draft decision (COP/CMA) — legal force of each verb, references to earlier decisions, what concretely changes, what is in brackets. Use when the user pastes a text excerpt, a document symbol (FCCC/…/L.x) or asks "what does this paragraph mean" (also « que veut dire ce paragraphe », « décode ce texte »).
---

# Decode a text

Follow `cop31-protocol`. Answer in the user's language; keep quotes in the original English.

1. Given a symbol or id: `document` then `lire_passage`. Given pasted text: find its source (`rechercher` with an
   exact sentence in quotation marks). State the version, date and "not adopted" for a draft.
2. For each paragraph: operative verb and its force (shall / should / urges / encourages / invites /
   requests [secretariat] / decides / notes); addressee (Parties, developed countries, secretariat…); deadline;
   references ("decision 1/CMA.5, paragraph 28" → go and read the reference).
3. Brackets and options: list them, say what each option would change (`crochets` for brackets on a theme and
   since which version; `comparer_versions(<symbol>, filtre="§N")` for how that paragraph changed since the
   previous version).
4. Compare with already agreed language (`rechercher` the same wording in level 1 decisions): "backsliding" or
   "progress" is judged against agreed language.

## Output
Short table: § · what it says in plain words · force · new or already agreed (with reference). Then
"In short" (2 lines) and "Points of attention" for the user.
