"""Collecte HTTP polie : délai par hôte, requêtes conditionnelles, reçus SHA-256,
exploration de pages (crawl) et flux RSS."""

from __future__ import annotations

import hashlib
import mimetypes
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from . import config

DOC_EXT = re.compile(r"\.(pdf|docx?|xlsx?|csv|json|txt|odt|rtf|pptx?)(\?|$)", re.I)


@dataclass
class FetchResult:
    url: str
    status: int
    content: bytes = b""
    mime: str = ""
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error


class Client:
    """Client HTTP partagé entre threads, avec un délai minimal par hôte."""

    def __init__(self, delay: float | None = None) -> None:
        self.delay = config.HOST_DELAY if delay is None else delay
        self._locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}
        self._guard = threading.Lock()
        self.http = httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, connect=20.0),
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept-Language": "en,fr;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/pdf,application/xml;q=0.9,*/*;q=0.8",
            },
        )

    def _wait(self, host: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.setdefault(host, threading.Lock())
        lock.acquire()
        elapsed = time.monotonic() - self._last.get(host, 0)
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        return lock

    def get(self, url: str, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        """GET avec 2 relances sur erreur réseau ou 5xx (sites instables)."""
        res = self._get(url, etag, last_modified)
        for attempt in range(2):
            if res.status == 0 or res.status >= 500:
                time.sleep(3 * (attempt + 1))
                res = self._get(url, etag, last_modified)
            else:
                break
        return res

    def _get(self, url: str, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        host = urlparse(url).netloc
        lock = self._wait(host)
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        limit = int(config.MAX_DOWNLOAD_MB * 1024 * 1024)
        try:
            with self.http.stream("GET", url, headers=headers) as resp:
                if resp.status_code != 200:
                    return FetchResult(url=str(resp.url), status=resp.status_code)
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > limit:
                        return FetchResult(url=str(resp.url), status=413, error=f"> {config.MAX_DOWNLOAD_MB} Mo")
                    chunks.append(chunk)
                return FetchResult(
                    url=str(resp.url),
                    status=200,
                    content=b"".join(chunks),
                    mime=resp.headers.get("content-type", "").split(";")[0].strip().lower(),
                    etag=resp.headers.get("etag"),
                    last_modified=resp.headers.get("last-modified"),
                )
        except Exception as exc:  # réseau, TLS, timeout
            return FetchResult(url=url, status=0, error=f"{type(exc).__name__}: {exc}")
        finally:
            self._last[host] = time.monotonic()
            lock.release()


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def guess_ext(url: str, mime: str, content: bytes) -> str:
    if content[:5] == b"%PDF-":
        return ".pdf"
    if content[:4] == b"PK\x03\x04":
        path = urlparse(url).path.lower()
        for ext in (".docx", ".xlsx", ".pptx", ".odt"):
            if path.endswith(ext):
                return ext
        if "wordprocessingml" in mime:
            return ".docx"
        return ".zip"
    m = DOC_EXT.search(urlparse(url).path)
    if m:
        return "." + m.group(1).lower()
    if "html" in mime:
        return ".html"
    if "xml" in mime or "rss" in mime:
        return ".xml"
    if "json" in mime:
        return ".json"
    if "csv" in mime:
        return ".csv"
    ext = mimetypes.guess_extension(mime or "") or ".bin"
    return ext


def store_raw(content: bytes, ext: str) -> tuple[str, Path]:
    digest = sha256(content)
    path = config.RAW_DIR / digest[:2] / f"{digest}{ext}"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(content)
        tmp.replace(path)
    return digest, path


# --- exploration -------------------------------------------------------------

@dataclass
class Link:
    url: str
    text: str
    found_on: str
    published: str | None = None
    summary: str | None = None


def _compile(patterns) -> list[re.Pattern]:
    if not patterns:
        return []
    if isinstance(patterns, str):
        patterns = [patterns]
    return [re.compile(p) for p in patterns]


def _matches(regexes: list[re.Pattern], url: str) -> bool:
    return any(r.search(url) for r in regexes)


def extract_links(html: bytes | str, base: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "javascript:", "tel:")):
            continue
        url, _ = urldefrag(urljoin(base, href))
        text = " ".join(a.get_text(" ", strip=True).split())
        if not text:
            text = (a.get("title") or a.get("aria-label") or "").strip()
        out.append((url, text))
    return out


def crawl(client: Client, raw: dict, log=None) -> Iterator[Link]:
    """Parcours en largeur : suit `follow`, émet ce qui matche `collect`."""
    starts = raw.get("start") or []
    if isinstance(starts, str):
        starts = [starts]
    follow = _compile(raw.get("follow"))
    collect = _compile(raw.get("collect"))
    exclude = _compile(raw.get("exclude"))
    max_depth = int(raw.get("max_depth", 1))
    max_pages = int(raw.get("max_pages", 300))
    max_docs = int(raw.get("max_docs", 500))
    queue = deque((u, 0) for u in starts)
    seen_pages: set[str] = set(starts)
    emitted: set[str] = set()
    pages = 0
    while queue and pages < max_pages and len(emitted) < max_docs:
        url, depth = queue.popleft()
        res = client.get(url)
        pages += 1
        if not res.ok:
            if log:
                log(url, f"page {res.status} {res.error or ''}")
            continue
        if is_antibot(res):
            if log:
                log(url, "antibot : page de challenge, non contournée")
            continue
        if "html" not in res.mime and not res.content.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
            # La page de départ est elle-même un document.
            if collect and _matches(collect, res.url) and res.url not in emitted:
                emitted.add(res.url)
                yield Link(url=res.url, text="", found_on=url)
            continue
        for link, text in extract_links(res.content, res.url):
            if exclude and _matches(exclude, link):
                continue
            if collect and _matches(collect, link):
                if link not in emitted:
                    emitted.add(link)
                    yield Link(url=link, text=text, found_on=res.url)
                    if len(emitted) >= max_docs:
                        break
                continue
            if depth < max_depth and follow and _matches(follow, link) and link not in seen_pages:
                seen_pages.add(link)
                queue.append((link, depth + 1))


ANTIBOT_MARKERS = (b"_Incapsula_Resource", b"cf-chl", b"challenge-platform", b"Just a moment...", b"captcha")


def is_antibot(res: FetchResult) -> bool:
    head = res.content[:4000]
    return ("html" in res.mime or head.lstrip()[:1] == b"<") and any(m in head for m in ANTIBOT_MARKERS)


ODS_URL = "https://documents.un.org/api/symbol/access?s={symbol}&l={lang}&t=pdf"


def ods_symbols(raw: dict) -> Iterator[tuple[str, list[str]]]:
    """Énumère les cotes d'une entrée `ods` : renvoie (gabarit_sans_k, [cotes ordonnées])."""
    templates = raw.get("symbols") or []
    if isinstance(templates, str):
        templates = [templates]
    y0, y1 = (raw.get("years") or [None, None])[:2] if raw.get("years") else (None, None)
    n0, n1 = (raw.get("range") or [1, 1])[:2]
    k0, k1 = (raw.get("add_range") or [1, 1])[:2]
    for tpl in templates:
        years = range(int(y0), int(y1) + 1) if "{year}" in tpl and y0 else [None]
        for year in years:
            base = tpl.replace("{year}", str(year)) if year else tpl
            if "{n}" in base:
                if "{k}" in base:
                    for n in range(int(n0), int(n1) + 1):
                        yield (f"{base}|{n}", [base.replace("{n}", str(n)).replace("{k}", str(k)) for k in range(int(k0), int(k1) + 1)])
                else:
                    yield (base, [base.replace("{n}", str(n)) for n in range(int(n0), int(n1) + 1)])
            elif "{k}" in base:
                yield (base, [base.replace("{k}", str(k)) for k in range(int(k0), int(k1) + 1)])
            else:
                yield (base, [base])


def rss(client: Client, raw: dict, log=None) -> Iterator[Link]:
    import feedparser

    res = client.get(raw["url"])
    if not res.ok:
        if log:
            log(raw["url"], f"rss {res.status} {res.error or ''}")
        return
    feed = feedparser.parse(res.content)
    keywords = [str(k).lower() for k in (raw.get("keywords") or [])]
    for entry in feed.entries[: int(raw.get("max_items", 100))]:
        link = entry.get("link")
        if not link:
            continue
        title = entry.get("title", "")
        summary = BeautifulSoup(entry.get("summary", "") or "", "lxml").get_text(" ", strip=True)
        if keywords:
            hay = f"{title} {summary} {' '.join(t.get('term', '') for t in entry.get('tags', []))}".lower()
            if not any(k in hay for k in keywords):
                continue
        published = None
        if entry.get("published_parsed"):
            t = entry.published_parsed
            published = f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"
        yield Link(url=link, text=title, found_on=raw["url"], published=published, summary=summary)
