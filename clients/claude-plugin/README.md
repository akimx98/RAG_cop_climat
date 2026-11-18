# Claude plugin "COP31 Tracker"

Independent, free, open-source (MIT) assistant to follow COP31 (Antalya, 9–20 November 2026). Skills answer in the
user's language (English or French).

Skills: `cop31-protocol` (base rules: exact citations, reliability levels, no guessing), `fact-check`,
`negotiation-update`, `beginner-brief`, `decode-text`, `eu-position`, `daily-brief`, `find-your-way`, `plan-my-day`.

Requirement: the COP31 MCP connector (personal address `https://<domain>/u/<token>/mcp`, "Claude / ChatGPT" page
of the dashboard).

Installation: from this folder, or zip `clients/claude-plugin` and import it into Claude (Settings → Customize →
Plugins / Skills). A Claude Enterprise admin can distribute it to the whole organisation.
For ChatGPT and Le Chat (Mistral), paste `clients/chatgpt-instructions.md` or `clients/mistral-instructions.md`.
