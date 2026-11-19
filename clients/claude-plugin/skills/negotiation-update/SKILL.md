---
name: negotiation-update
description: Give the state of play on a COP31 negotiation item (mitigation, adaptation/GGA, finance/NCQG, just transition, Article 6, loss and damage, global stocktake, transparency…) — latest text version, what changed since the previous one, remaining options and brackets, known positions, gap with the EU position. Use for "where are we on…", "what moved", "new version of the text" (also « où en est-on sur… », « qu'est-ce qui a bougé », « nouvelle version du texte »).
---

# Negotiation update

Follow `cop31-protocol`. Answer in the user's language.

1. **`dossier(<alias>)`** first (e.g. `dossier("GGA")`, `dossier("finance")`; list: `dossiers()`): state of the
   item, successive texts with brackets and options, positions recorded during the COP (exact quote + source),
   reference positions (Carbon Brief).
2. Latest text version: the most recent text in `dossier(...)`. If it is "to be retrieved" (`à récupérer`), say so
   and give its unfccc.int link. Otherwise `comparer_versions(<link or symbol>)` → what changed, unit by unit
   (`§4.opt2.b3` = 3rd bullet of Option 2 of §4). Read `resume` and `options` first (which option was retained,
   in full or in part); `supprimé_sans_reprise` = nothing of that unit survives (often the most telling);
   `repris_de` / `repris_dans` = split, moved or merged, with the source unit; each bracket's fate (lifted,
   deleted, kept; for `[A][B]`, which wording won). Quote old wording with the `ancien` reference and new
   wording with the `nouveau` reference (page + PDF link), never with the `[-…-]` `{+…+}` markers. One paragraph
   only: `filtre="§12"`; a theme: `filtre="finance, grant"`; long text: `page=2`… `crochets(theme=…, dossier=…)` for what is still bracketed on a theme, and since
   which version.
3. Who is moving: `positions(dossier=..., depuis="48h")`; compare with pre-COP reference positions.
   WRITTEN positions (official submissions, level 2 for a Party): the `soumissions` and `positions_ecrites`
   sections of `dossier(...)` (calls linked to the item, who submitted, alignments already computed); detail of a
   call: `soumissions(appel=<id>)`; agreements and conflicts between two actors:
   `comparer_soumissions("EU", "<group>", dossier="<alias>")` (rule-based indication, present it as an aid to
   analysis); overview: `soumissions(dossier="<alias>", acteurs=[…])` (pairs already compared).
   A group that did not submit in its own name: the comparison uses its members' submissions — say so
   ("members' submissions, not a group position", with the coverage). A submission says what an actor asked for in
   writing before or during the session; `positions` says what was recorded in the room and in the press.
4. Complete with `rechercher` (UN English, `session="CMA8"`…) and `nouveautes(depuis="48h")` if needed.
5. EU position: Council conclusions (level 2) — see `eu-position`. Internal notes (R): use them, never copy them
   outside.
6. Cite "according to Carbon Brief's tracker" for the text index and reference positions.

## Output
```
<Item> — version of <date time>, <symbol or title> (not adopted)
Where we are: <2–3 lines>
What moved since <previous version>: <options settled; units deleted with no trace; key rewordings quoted
before/after with page links; brackets x → y (lifted / deleted)>
Options still open: <short list, quoted>
Who pushes what: <groups/countries, source and level>
Written positions: <linked calls, who submitted; known alignments (aid to analysis)>
Gap with the EU position: <…>
To watch: <next step, meeting>
```
Offer to follow the item (`suivre(dossier="<alias>")`, `/suivi <alias>` in Telegram, or the "Follow" button on the
item page) to be alerted to each new text.
