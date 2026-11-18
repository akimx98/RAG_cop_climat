---
name: find-your-way
description: Find your way at COP31 — today's programme, where and when an item is discussed, which meeting to attend, negotiating bodies and groups, acronyms. Use for "where is adaptation discussed today", "what is this meeting", "which body deals with…" (also « où se discute… aujourd'hui », « c'est quoi cette réunion », « quel organe traite de… »). For a full personal schedule, use `plan-my-day`.
---

# Find your way

Follow `cop31-protocol`. Answer in the user's language.

0. `dossiers()`: the map of negotiation items, their state and aliases (GGA, MWP, art6…).
1. `programme(jour="today", sujet="water, indigenous")`: official side events (room, time, organisers, themes),
   official UNFCCC calendar meetings (dialogues, workshops, group coordinations), summary of the day's
   negotiations, and uploaded Daily Programme if any. Keywords in English, comma-separated;
   `types=["hors_site"]` for off-site events. The `pavillons` key gives the day's pavilion and hub sessions (Blue
   and Green Zone) with summary and speakers. For "what's on today for my topics?":
   `programme(jour="today", mes_sujets=true)` (one block per followed topic). Cross-cutting questions ("which
   pavilions talked about…", counts, a pavilion's full programme): `pavillons` tool.
   - Always give, for each side event, the SEORS link (`lien`, official programme filtered on the day): rooms and
     times change. State the source and its date (`source`, `releve_le`, `a_verifier`).
   - Informal consultations per agenda item are in none of these lists: only the Daily Programme gives them.
2. Agendas (`types=["ordre_du_jour"]`, session COP31/CMA8/CMP21, SB64) to know which body handles which item.
3. `glossaire` for bodies and groups.

Answer briefly: what, where, when, which body, and the reference text. Never invent a room or a time: with no
programme in the corpus, say so.
