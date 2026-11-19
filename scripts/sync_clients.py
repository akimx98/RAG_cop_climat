"""Recopie les règles communes (clients/rules.md) dans le skill cop31-protocol et dans les consignes à coller
(ChatGPT, Le Chat, autres assistants), et tient la version des copies.

Les règles ne se modifient QUE dans clients/rules.md (le serveur MCP les envoie telles quelles) ; ce script les
recopie entre les marqueurs `<!-- rules -->` et `<!-- /rules -->`. Quand le contenu des copies a changé depuis la
dernière fois, il passe la version au numéro suivant (plugin.json et .claude-plugin/marketplace.json) : le serveur
annonce cette version, et le LLM signale à l'utilisateur des skills ou des consignes plus anciens.

    python scripts/sync_clients.py           # met à jour
    python scripts/sync_clients.py --check   # code 1 si quelque chose n'est pas à jour (test)
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENTS = ROOT / "clients"
PLUGIN = CLIENTS / "claude-plugin"
MANIFEST = PLUGIN / ".claude-plugin" / "plugin.json"
MARKET = ROOT / ".claude-plugin" / "marketplace.json"
STAMP = CLIENTS / ".contenu.sha256"
TARGETS = [PLUGIN / "skills" / "cop31-protocol" / "SKILL.md", CLIENTS / "chatgpt-instructions.md",
           CLIENTS / "mistral-instructions.md", CLIENTS / "assistant-instructions.md"]
BLOCK = re.compile(r"(<!-- rules -->\n).*?(<!-- /rules -->)", re.S)
VERSION_LINE = re.compile(r"\(COP31 instructions v[\d.]+\)")


def _files() -> list[Path]:
    return sorted({*PLUGIN.rglob("*"), *TARGETS}) if PLUGIN.exists() else TARGETS


def digest() -> str:
    """Empreinte du contenu des copies, hors numéro de version (qui en dépend)."""
    h = hashlib.sha256()
    for p in _files():
        if p.is_file() and p not in (MANIFEST, STAMP):
            h.update(str(p.relative_to(ROOT)).encode())
            h.update(VERSION_LINE.sub("", p.read_text(encoding="utf-8")).encode())
    return h.hexdigest()


def render(text: str, rules: str, version: str) -> str:
    if not BLOCK.search(text):
        raise SystemExit("marqueurs <!-- rules --> … <!-- /rules --> absents")
    body = f"{rules}\n(COP31 instructions v{version})\n"
    return BLOCK.sub(lambda m: m.group(1) + body + m.group(2), text)


def main(check: bool = False) -> int:
    rules = (CLIENTS / "rules.md").read_text(encoding="utf-8").strip()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    market = json.loads(MARKET.read_text(encoding="utf-8"))
    version = manifest["version"]
    changed = []
    for p in TARGETS:
        old = p.read_text(encoding="utf-8")
        new = render(old, rules, version)
        if new != old:
            changed.append(p)
            if not check:
                p.write_text(new, encoding="utf-8")
    d = digest()
    previous = STAMP.read_text().strip() if STAMP.exists() else None
    bump = previous is not None and previous != d
    if check:
        stale = changed or previous != d or market["plugins"][0]["version"] != version
        for p in changed:
            print(f"pas à jour : {p.relative_to(ROOT)}")
        if previous != d:
            print("contenu modifié sans nouvelle version : lancer python scripts/sync_clients.py")
        return 1 if stale else 0
    if bump:
        major, minor, patch = (int(x) for x in version.split("."))
        version = f"{major}.{minor}.{patch + 1}"
        manifest["version"] = version
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for p in TARGETS:
            p.write_text(render(p.read_text(encoding="utf-8"), rules, version), encoding="utf-8")
    market["plugins"][0]["version"] = version
    MARKET.write_text(json.dumps(market, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    STAMP.write_text(digest() + "\n")
    print(f"COP31 instructions v{version} ({'nouvelle version' if bump else 'version inchangée'})")
    return 0


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv))
