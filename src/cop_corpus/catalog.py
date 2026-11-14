"""Lecture du catalogue de sources (catalog/sources/*.yaml)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import config

META_KEYS = ("level", "org", "doc_type", "session", "symbol", "parties", "topics", "lang", "published", "title")


@dataclass
class Source:
    id: str
    type: str
    raw: dict[str, Any]
    file: str
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def watch_minutes(self) -> int:
        try:
            return int(self.raw.get("watch_minutes") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def urls(self) -> list[str]:
        if self.type == "crawl":
            start = self.raw.get("start") or []
            return [start] if isinstance(start, str) else list(start)
        return [self.raw["url"]] if self.raw.get("url") else []


def load(catalog_dir: Path | None = None) -> list[Source]:
    base = (catalog_dir or config.CATALOG_DIR) / "sources"
    sources: list[Source] = []
    seen: set[str] = set()
    for path in sorted(base.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if not isinstance(data, list):
            continue
        for entry in data:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            sid = str(entry["id"])
            if sid in seen:
                sid = f"{path.stem}:{sid}"
            seen.add(sid)
            kind = entry.get("type", "doc")
            meta = {k: entry[k] for k in META_KEYS if entry.get(k) is not None}
            if "level" in meta:
                meta["level"] = str(meta["level"])
            sources.append(Source(id=sid, type=kind, raw=entry, file=path.name, meta=meta))
    return sources


def by_id(sources: list[Source]) -> dict[str, Source]:
    return {s.id: s for s in sources}
