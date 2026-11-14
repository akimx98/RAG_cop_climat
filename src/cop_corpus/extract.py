"""Extraction du texte (par page) et découpage en passages citables.

Les décisions et projets de texte onusiens se citent au paragraphe numéroté
(« 1/CMA.5 §28 ») : le découpage suit cette numérotation quand elle existe,
et retombe sinon sur des blocs de taille bornée.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path

SYMBOL_RE = re.compile(
    r"\b(FCCC/(?:[A-Z]+/)*(?:\d{4}/)?(?:[A-Z]+\.)?\d+(?:/Add\.\d+)?(?:/Rev\.\d+)?(?:/Corr\.\d+)?)"
)
DECISION_RE = re.compile(r"\b(?:[Dd]ecision|[Dd]écision)\s+(\d+/(?:CP|CMA|CMP)\.\d+)")


@dataclass
class Page:
    number: int
    text: str


@dataclass
class Chunk:
    seq: int
    page: int
    para: str | None
    text: str


def extract(path: Path, mime: str = "") -> tuple[str | None, list[Page]]:
    """Renvoie (titre détecté, pages)."""
    suffix = path.suffix.lower()
    data = path.read_bytes()
    if suffix == ".pdf" or data[:5] == b"%PDF-":
        return _pdf(data)
    if suffix == ".docx":
        return _docx(data)
    if suffix in (".html", ".htm", ".xml") or "html" in mime:
        return _html(data)
    if suffix in (".txt", ".md"):
        return None, [Page(1, data.decode("utf-8", "replace"))]
    return None, []


def _pdf(data: bytes) -> tuple[str | None, list[Page]]:
    import pymupdf

    pages = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        title = (doc.metadata or {}).get("title") or None
        for i, page in enumerate(doc, start=1):
            pages.append(Page(i, page.get_text("text", sort=True)))
    return title, pages


def _docx(data: bytes) -> tuple[str | None, list[Page]]:
    import docx

    d = docx.Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs]
    for table in d.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    title = d.core_properties.title or None
    return title, [Page(1, "\n".join(parts))]


def _html(data: bytes) -> tuple[str | None, list[Page]]:
    import trafilatura
    from bs4 import BeautifulSoup

    html = data.decode("utf-8", "replace")
    title = None
    try:
        soup = BeautifulSoup(html, "lxml")
        if soup.title and soup.title.string:
            title = " ".join(soup.title.string.split())
    except Exception:
        soup = None
    text = trafilatura.extract(html, include_tables=True, include_comments=False, favor_recall=True) or ""
    if len(text) < 200 and soup is not None:
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = soup.get_text("\n", strip=True)
    return title, [Page(1, text)]


# --- découpage ---------------------------------------------------------------

DECISION_HEAD = re.compile(r"^\s*(?:Decision|Décision|Decisión)\s+(\d+/(?:CP|CMA|CMP)\.\d+)\s*$")
PARA_START = re.compile(r"^\s{0,6}(\d{1,3})\.\s+(?=[A-Z\[“\"(‘'ÀÉÈ]|[a-z]{3,})")
TARGET = 1200
MAX = 2200


def _clean(text: str) -> str:
    text = text.replace("­", "").replace("\xa0", " ")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text


def _split_long(text: str) -> list[str]:
    if len(text) <= MAX:
        return [text]
    sentences = re.split(r"(?<=[.;:!?])\s+", text)
    out, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) > TARGET:
            out.append(cur.strip())
            cur = ""
        cur += s + " "
        while len(cur) > MAX:
            out.append(cur[:MAX])
            cur = cur[MAX:]
    if cur.strip():
        out.append(cur.strip())
    return out


def chunk(pages: list[Page]) -> list[Chunk]:
    lines: list[tuple[int, str]] = []
    for page in pages:
        for line in _clean(page.text).split("\n"):
            lines.append((page.number, line.rstrip()))

    numbered = sum(1 for _, l in lines if PARA_START.match(l))
    chunks: list[Chunk] = []
    seq = 0

    def emit(page: int, para: str | None, text: str) -> None:
        nonlocal seq
        text = re.sub(r"\n{2,}", "\n", text).strip()
        if len(text) < 30:
            return
        for part in _split_long(text):
            seq += 1
            chunks.append(Chunk(seq, page, para, part))

    if numbered >= 5:
        # Document à paragraphes numérotés (décisions, projets, conclusions).
        cur_page, cur_para, buf = lines[0][0] if lines else 1, None, []
        last_num = 0
        decision = None
        for page, line in lines:
            dm = DECISION_HEAD.match(line)
            if dm:
                emit(cur_page, cur_para, "\n".join(buf))
                decision, last_num = dm.group(1), 0
                cur_page, cur_para, buf = page, decision, [line]
                continue
            m = PARA_START.match(line)
            if m:
                num = int(m.group(1))
                # Évite les faux positifs (années, listes de notes) : numérotation plausible.
                if num == 1 or num == last_num + 1 or (last_num and 0 < num - last_num <= 3):
                    emit(cur_page, cur_para, "\n".join(buf))
                    cur_page, cur_para, buf = page, (f"{decision} §{num}" if decision else f"§{num}"), [line]
                    last_num = num
                    continue
            buf.append(line)
            if sum(len(b) for b in buf) > MAX * 3:
                emit(cur_page, cur_para, "\n".join(buf))
                cur_page, buf = page, []
        emit(cur_page, cur_para, "\n".join(buf))
        return chunks

    # Sinon : blocs de paragraphes regroupés jusqu'à ~TARGET caractères.
    cur_page, buf, size = None, [], 0
    for page in pages:
        blocks = re.split(r"\n\s*\n", _clean(page.text))
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            if cur_page is None:
                cur_page = page.number
            if size + len(block) > TARGET and buf:
                emit(cur_page, None, "\n".join(buf))
                buf, size, cur_page = [], 0, page.number
            buf.append(block)
            size += len(block)
    if buf:
        emit(cur_page or 1, None, "\n".join(buf))
    return chunks


def detect_symbol(url: str, pages: list[Page]) -> str | None:
    head = "\n".join(p.text for p in pages[:2])[:6000]
    m = SYMBOL_RE.search(head)
    if m:
        return m.group(1).rstrip(".")
    return None


def detect_lang(text: str) -> str | None:
    sample = f" {text[:4000].lower()} "
    scores = {
        "en": sum(sample.count(w) for w in (" the ", " and ", " of ", " to ")),
        "fr": sum(sample.count(w) for w in (" le ", " les ", " des ", " et ", " la ")),
        "es": sum(sample.count(w) for w in (" el ", " los ", " las ", " y ", " del ")),
        "de": sum(sample.count(w) for w in (" der ", " die ", " und ", " das ")),
        "pt": sum(sample.count(w) for w in (" os ", " das ", " não ", " com ", " para ")),
    }
    best = max(scores, key=scores.get)
    return best if scores[best] >= 5 else None


BOILER = re.compile(
    r"^(united nations|nations unies|naciones unidas|distr\.|original|ge\.\d|advance unedited|"
    r"framework convention|convention-cadre|conference of the parties|conférence des parties|"
    r".*\bsession\b\s*$|.*\bsession\b.*\d{4}|antalya|bonn|baku|bakou|belém|belem|dubai|dubaï|"
    r"serving as the meeting|agissant comme réunion|note by|note du|note de|addendum|corrigendum|"
    r"part (one|two)|deuxième partie|première partie|contents|table des matières|"
    r"trente|thirty|twenty|vingt|sixty|soixante|"
    r"subsidiary body|organe subsidiaire|agenda item|point \d|item \d|english|french|"
    r"page \d|\d+ (january|february|march|april|may|june|july|august|september|october|november|december)|"
    r"\d+ (janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)|fccc/)",
    re.I,
)
FILE_ID = re.compile(r"^[a-z]?\d{6,8}$|^microsoft word|\.docx?$", re.I)


def guess_title(pages: list[Page], meta_title: str | None) -> str | None:
    if meta_title and len(meta_title) > 8 and not FILE_ID.search(meta_title.strip()):
        return meta_title.strip()
    if not pages:
        return None
    lines = [l.strip() for l in pages[0].text.split("\n") if l.strip()][:80]
    for i, line in enumerate(lines):
        if len(line) < 12 or BOILER.search(line) or not re.search(r"[A-Za-zÀ-ÿ]{4}", line):
            continue
        if re.match(r"^(note|report|draft|provisional|decision|proposal|summary|synthesis|rapport|projet|"
                    r"ordre du jour|outcome|guidance|matters|annotated|work programme)", line, re.I) or line[:1].isupper():
            title = line
            j = i + 1
            while j < len(lines) and len(title) < 160 and lines[j][:1].islower():
                title += " " + lines[j]
                j += 1
            return title[:200]
    return None
