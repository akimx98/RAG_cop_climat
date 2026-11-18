---
name: plan-my-day
description: Plan the user's day at COP31 from their topics of interest — side events, official meetings and pavilion sessions worth attending, without time clashes, with verification links and people worth meeting, plus what happens in the negotiations on their followed items. Use for "plan my day", "what should I attend tomorrow", "my schedule for Thursday" (also « organise ma journée », « que faire demain », « mon programme de jeudi »).
---

# Plan my day

Follow `cop31-protocol`. Answer in the user's language. All times are Antalya time (Europe/Istanbul).

1. **Topics**: `mes_sujets` (followed negotiation items and keyword topics). Add any keywords the user gives in the
   conversation. Nothing followed and no keywords → ask for 2–3 topics, and offer `suivre` to keep them.
2. **Programme**: `programme(mes_sujets=true, jour=…)` ("today", "tomorrow", "2026-11-14" or "14"); one block per
   topic with side events, official meetings and pavilion sessions, plus the day's negotiations
   (`negociations_du_jour`) and the sources. For extra keywords: `programme(sujet="…", jour=…)` (English,
   comma-separated, with synonyms) and `pavillons(sujet="…", jour=…)`; `pavillons(evenement=<id>)` for the full
   summary and speakers of a session.
3. **Negotiations on followed items**: from `negociations_du_jour` and `dossier(<alias>)` for each followed item:
   meetings, consultations or new texts expected that day (version, date, "not adopted"). Informal consultations are
   only in the Daily Programme: if it is not in the corpus, say so.
4. **Select**: cross the three sources, drop duplicates, keep 4–7 items, **no overlapping slots** (when two clash,
   keep the more relevant one and list the other as an alternative). Leave travel time between the Blue Zone and
   the Green Zone.

## Output
```
<Day, date> — Antalya time
HH:MM–HH:MM · <title> · <room / pavilion, zone>
  Why: <link to the user's topic>
  People: <speakers / organisers as published>
  Check: <SEORS link or source link>
…
Alternatives: <clashing or lower-priority events>
In the negotiations today: <item — meeting / text expected, source>
```
Rules: give the SEORS link for each side event when provided — rooms and times change; state the source and date
of the programme (`releve_le`, `a_verifier`). Never invent an event, room, time, speaker or contact detail: name
speakers and organisers only as published, without emails or phone numbers. If the programme has nothing for a
topic, say "not found in the corpus" for that topic.
