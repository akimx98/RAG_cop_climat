---
name: daily-brief
description: Produce the COP31 daily brief (or since a given time) — new draft texts and decisions, what changed, internal documents received, yesterday's reports, what's new on my topics, today's programme. Use for "morning brief", "what's new since last night", "sum up the day" (also « brief du matin », « quoi de neuf depuis hier soir », « résume la journée »).
---

# Daily brief

Follow `cop31-protocol`. Answer in the user's language.

0. `dossiers()`: state of each negotiation item; for active ones (new texts, positions), `dossier(slug)` gives
   the texts and recorded positions. `positions(depuis="18h")`: who said what since last night.
1. `nouveautes(depuis="18h")` (or the requested period), then by type: draft texts and L documents
   (levels 1–2), internal documents (R), reports (4), press releases (5).
2. For each draft text with a previous version: `comparer_versions` (1 line from its `resume`: options settled,
   units deleted with no trace, brackets x → y). Always "version of …, not adopted".
3. My topics: `nouveautes(depuis="18h", mes_sujets=true)` — what's new on followed items and keyword topics,
   grouped per topic, with new draft texts and their diffs. If nothing is followed, `mes_sujets` and offer
   `suivre`.
4. `programme(jour="today")` for the day, or `programme(mes_sujets=true, jour="today")` — see `plan-my-day`.

## Output (1 screen)
**Key points** (3 bullets) · **Texts** (item — version — what moved) · **Internal** · **What observers say**
(ENB/Carbon Brief, level 4) · **Today** (programme) · **My topics**. Links everywhere.
