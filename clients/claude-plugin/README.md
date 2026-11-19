# Claude plugin "COP31 Tracker"

Independent, free, open-source (MIT) assistant to follow COP31 (Antalya, 9–20 November 2026). Skills answer in the
user's language (English or French).

Skills: `cop31-protocol` (base rules: exact citations, reliability levels, no guessing), `fact-check`,
`negotiation-update`, `beginner-brief`, `decode-text`, `eu-position`, `daily-brief`, `find-your-way`, `plan-my-day`.

Requirement: the COP31 MCP connector (personal address `https://<domain>/u/<token>/mcp`, "Claude / ChatGPT" page
of the dashboard).

Installation:
- Claude Code (anyone, with updates): `/plugin marketplace add akimx98/RAG_cop_climat`, then
  `/plugin install cop31@cop31`; later `/plugin marketplace update cop31` to get new versions.
- claude.ai / Claude app: download the skills from the site's "Connect an LLM" page (one zip per skill, or the full
  plugin) and upload them (Settings → Capabilities → Skills). An organisation admin can publish the plugin zip to the
  whole organisation; re-upload after each new version.
When skills change, bump `version` in `.claude-plugin/plugin.json` AND in the repository's
`.claude-plugin/marketplace.json` (a test checks they match).
For ChatGPT and Le Chat (Mistral), paste `clients/chatgpt-instructions.md` or `clients/mistral-instructions.md`.
