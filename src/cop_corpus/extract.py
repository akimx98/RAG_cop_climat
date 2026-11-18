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
        return None, [Page(1, fix_text(decode_html(data)))]
    return None, []


def _pdf(data: bytes) -> tuple[str | None, list[Page]]:
    import pymupdf

    pages = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        title = (doc.metadata or {}).get("title") or None
        tables = doc.page_count <= TABLE_MAX_PAGES  # notes et projets courts ; jamais les gros rapports à graphiques
        for i, page in enumerate(doc, start=1):
            text = _table_text(page) if tables else None
            if text is None:
                text = _columns_text(page)
            pages.append(Page(i, fix_text(text if text is not None else page.get_text("text", sort=True))))
    return title, pages


TABLE_MAX_PAGES = 80


def _table_text(page) -> str | None:
    """Page portant un tableau tracé (note informelle en tableau : activité | entité | calendrier | livrable…) :
    le tableau est lu ligne par ligne, cellule par cellule (« cellule | cellule | … », une ligne du tableau par
    paragraphe), le reste de la page par ordonnée. Sans cela, le tri par ordonnée entrelace les cellules voisines et
    colle un en-tête de colonne au milieu d'un crochet. None si la page n'a pas de tableau tracé."""
    import pymupdf

    try:
        n = len(page.get_drawings())
        if n < 8 or n > 800:  # pas de filets : pas de tableau ; des milliers de tracés : un graphique
            return None
        tables = [t for t in page.find_tables(strategy="lines").tables if t.col_count >= 3 and t.row_count >= 2]
    except Exception:
        return None
    if not tables:
        return None
    boxes = [pymupdf.Rect(t.bbox) for t in tables]
    items: list[tuple[float, float, str]] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", [])).rstrip()
            if not text.strip():
                continue
            x0, y0, x1, y1 = line["bbox"]
            centre = pymupdf.Point((x0 + x1) / 2, (y0 + y1) / 2)
            if not any(b.contains(centre) for b in boxes):
                items.append((y0, x0, text))
    for t in tables:
        for row in t.rows:
            cells = []
            for c in row.cells:
                cell = page.get_text("text", clip=pymupdf.Rect(c)) if c else ""
                cells.append(" ".join(cell.split()))
            if any(cells):
                items.append((row.bbox[1], -1.0, "\n" + " | ".join(cells) + "\n"))
    items.sort(key=lambda it: (round(it[0] / 3), it[1]))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(it[2] for it in items)).strip()


def _columns_text(page) -> str | None:
    """Texte d'une page en deux (ou trois) colonnes, colonne par colonne.

    ``get_text(sort=True)`` range les lignes par ordonnée : sur une page à colonnes (ENB, communiqués,
    soumissions mises en page), il entrelace les deux colonnes et colle une phrase du G77 sur les mesures de
    riposte juste après un passage sur l'ACE. On lit donc les blocs : les blocs pleine largeur (titres,
    en-têtes) coupent la page en bandes ; dans chaque bande, la colonne de gauche puis celle de droite.
    Renvoie None si la page n'a pas de colonnes (le tri par ordonnée convient alors)."""
    width = page.rect.width
    # Lignes (et non blocs : PyMuPDF fusionne parfois dans un même bloc deux lignes alignées de part et d'autre).
    lines = []
    for bi, block in enumerate(page.get_text("dict").get("blocks", [])):
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if text.strip():
                x0, y0, x1, y1 = line["bbox"]
                lines.append((x0, y0, x1, y1, text.rstrip(), bi))
    if len(lines) < 8:
        return None
    mid, tol = width / 2, width * 0.04
    left = [l for l in lines if l[2] <= mid + tol and (l[2] - l[0]) < width * 0.55]
    right = [l for l in lines if l[0] >= mid - tol and (l[2] - l[0]) < width * 0.55]
    # Des colonnes : assez de lignes de part et d'autre, qui portent l'essentiel du texte.
    if len(left) < 4 or len(right) < 4:
        return None
    # Un tableau aligne aussi du texte à gauche et à droite, mais en cellules courtes : on exige des lignes de texte
    # courant (largeur médiane d'au moins 30 % de la page) des deux côtés.
    def median_width(items) -> float:
        widths = sorted(l[2] - l[0] for l in items)
        return widths[len(widths) // 2]

    if median_width(left) < width * 0.3 or median_width(right) < width * 0.3:
        return None
    chars = sum(len(l[4]) for l in lines)
    if sum(len(l[4]) for l in left + right) < chars * 0.6:
        return None
    side = {id(l): 0 for l in left}
    side.update({id(l): 1 for l in right})
    full = [l for l in lines if id(l) not in side]
    # Ligne de colonne alignée sur une ligne pleine largeur (date et numéro de part et d'autre d'un titre courant) :
    # elle fait partie de cette rangée d'en-tête, pas du texte de la colonne.
    for l in [l for l in lines if id(l) in side]:
        if any(min(l[3], f[3]) - max(l[1], f[1]) > 0.5 * (l[3] - l[1]) for f in full):
            del side[id(l)]
            full.append(l)
    full.sort(key=lambda l: (round(l[1] / 4), l[0]))
    out: list[str] = []
    top = -1.0

    def column(items) -> None:
        prev = None
        for l in sorted(items, key=lambda l: (l[1], l[0])):
            if prev is not None and (l[5] != prev[5] or l[1] - prev[3] > (prev[3] - prev[1]) * 0.9):
                out.append("")  # nouveau paragraphe
            out.append(l[4])
            prev = l
        if items:
            out.append("")

    for cut in full + [None]:
        bottom = cut[1] if cut else float("inf")
        band = [l for l in lines if id(l) in side and top <= (l[1] + l[3]) / 2 < bottom]
        # (une ligne de colonne à la hauteur d'une ligne pleine largeur, comme la date dans un en-tête, passe
        # dans la bande suivante : les bornes sont les hauts des lignes pleine largeur)
        column([l for l in band if side[id(l)] == 0])
        column([l for l in band if side[id(l)] == 1])
        if cut:
            out.append(cut[4])
            top = cut[1]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# --- nettoyage des artefacts d'extraction ---------------------------------------------

_SPACED_RUN = re.compile(r"(?<!\S)((?:[^\W\d_] ){5,}[^\W\d_])(?!\S)")
_SPACED_SEG = re.compile(r"^(?:[^\W_] )+[^\W_]$")
_MOJIBAKE = re.compile(r"(?:[\u00c2-\u00f4][\u0080-\u00bf\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02c6\u02dc\u2013\u2014\u2018-\u201e\u2020-\u2022\u2026\u2030\u2039\u203a\u20ac\u2122]{1,3})+")


def _despace_line(line: str) -> str:
    """« S u b m i s s i o n  b y  t h e » → « Submission by the » (lettres espacées de certains PDF) :
    lettres séparées d'une espace, mots de deux espaces ou plus ; à défaut, suites d'au moins 6 lettres."""
    if re.search(r"\S {2,}\S", line):
        parts = re.split(r" {2,}", line)
        if sum(bool(_SPACED_SEG.match(p.strip())) for p in parts) >= 2:
            return " ".join(p.strip().replace(" ", "") if _SPACED_SEG.match(p.strip())
                            else _SPACED_RUN.sub(lambda m: m.group(1).replace(" ", ""), p) for p in parts)
    return _SPACED_RUN.sub(lambda m: m.group(1).replace(" ", ""), line)


def _demojibake(m: re.Match) -> str:
    s = m.group(0)
    for enc in ("cp1252", "latin-1"):
        try:
            return s.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return s


def fix_text(text: str) -> str:
    """Répare les artefacts courants : UTF-8 lu comme Windows-1252 (« TÃ¼rkiye » → « Türkiye »), lettres
    espacées (« S u b m i s s i o n »)."""
    if not text:
        return text
    if "Ã" in text or "Â" in text or "â€" in text:
        text = _MOJIBAKE.sub(_demojibake, text)
    if re.search(r"(?:(?<!\S)\w ){4,}", text):
        text = "\n".join(_despace_line(l) for l in text.split("\n"))
    return text


def decode_html(data: bytes) -> str:
    """Décode une page : UTF-8 si valide, sinon le jeu déclaré, sinon Windows-1252 (pages de TWN sans
    déclaration : « � » avec un décodage UTF-8 forcé)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    m = re.search(rb"""charset=["']?([A-Za-z0-9_\-]+)""", data[:4000])
    for enc in ([m.group(1).decode("ascii", "ignore")] if m else []) + ["cp1252"]:
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


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

    html = decode_html(data)
    title = None
    try:
        soup = BeautifulSoup(html, "lxml")
        if soup.title and soup.title.string:
            title = " ".join(soup.title.string.split())
    except Exception:
        soup = None
    text = trafilatura.extract(html, include_tables=True, include_comments=False, favor_recall=True) or ""
    if soup is not None:
        for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
            tag.decompose()
        full = soup.get_text("\n", strip=True)
        # trafilatura cherche « l'article » : sur une page sans article (lettre en tableaux, note de TWN), il
        # ne garde parfois qu'un fragment. Texte brut de la page si l'extrait en perd plus de la moitié.
        if len(text) < 200 or (soup.find("article") is None and len(text) < 0.5 * len(full) < 100_000):
            text = full
    return (fix_text(title) if title else title), [Page(1, fix_text(text))]


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


# --- dates ---------------------------------------------------------------------

MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}
MONTHS.update({m: i for i, m in enumerate(
    "janvier février mars avril mai juin juillet août septembre octobre novembre décembre".split(), 1)})
MONTHS.update({"fevrier": 2, "aout": 8, "decembre": 12})
_DATE_DMY = re.compile(r"\b(\d{1,2})(?:er|st|nd|rd|th)?\s+([A-Za-zéûè]{3,9})\.?,?\s+(20\d\d)\b")
_DATE_MDY = re.compile(r"\b([A-Z][a-z]{2,8})\.?\s+(\d{1,2}),\s+(20\d\d)\b")


def parse_date(text: str) -> str | None:
    """Première date en toutes lettres du texte (« 18 November 2026 », « November 18, 2026 », « 18 novembre
    2026 ») au format ISO, ou None."""
    best: tuple[int, str] | None = None
    for rx, order in ((_DATE_DMY, "dmy"), (_DATE_MDY, "mdy")):
        m = rx.search(text)
        if not m:
            continue
        if order == "dmy":
            day, month, year = m.group(1), m.group(2), m.group(3)
        else:
            month, day, year = m.group(1), m.group(2), m.group(3)
        num = MONTHS.get(month.lower()) or next((v for k, v in MONTHS.items() if len(month) >= 3 and k.startswith(month.lower())), None)
        if not num or not 1 <= int(day) <= 31:
            continue
        iso = f"{year}-{num:02d}-{int(day):02d}"
        if best is None or m.start() < best[0]:
            best = (m.start(), iso)
    return best[1] if best else None


def detect_date(pages: list[Page], head_chars: int = 1500) -> str | None:
    """Date de publication lue dans l'en-tête du document (début de la première page)."""
    if not pages:
        return None
    return parse_date(pages[0].text[:head_chars])
