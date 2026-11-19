"""Structure d'un projet de texte onusien, relue depuis les passages indexés : paragraphes numérotés, options,
puces et alinéas, notes de bas de page, crochets.

Pourquoi ici et pas à l'indexation : les passages sont découpés pour la recherche (taille, pages), pas pour la
comparaison. Un « 4. Option 1 » collé à la fin du §3 dans le PDF reste dans le passage étiqueté §3 ; une note de bas
de page « 1 FCCC/PA/CMA/2026/3. » glisse au milieu d'une puce. Ce module relit le texte complet d'un document et en
tire des unités stables (« §4.opt2.b3 » = 3e puce de l'option 2 du §4), chacune rattachée à son passage et sa page.

Sans IA : expressions régulières et heuristiques de mise en page des documents CCNUCC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --- notes de bas de page ---------------------------------------------------------------------------------------------

# Début d'une définition de note : « 1 FCCC/PA/CMA/2026/3. », « 4 Decision 3/CMA.7, para. 14. », « 2 See … ».
# Formules sûres ; une ligne « 3 Pursuant to … » (majuscule) n'est retenue que si l'appel « …3 » figure dans le texte.
_FN_WEAK = re.compile(r"^\s*(\d{1,2})\s+(?!(?:Alt|bis|ter|Option)\b)(?=[A-Z][\w’'-]*[\s.,:]|[A-Z]{2,}/)")
_FN_START = re.compile(
    r"(?:^|(?<=\s))(\d{1,2})\s+(?=FCCC/|UNFCCC/|A/RES/|A/\d|[Dd]ecisions?\s+\d|See\b|Available\b|As contained\b|"
    r"Contained in\b|https?://|Para(?:graph)?s?\.?\s+\d|Art(?:icle)?s?\.?\s+\d|IPCC\b|UNEP\b|Document\b|In accordance\b|"
    r"Voir\b|Disponible\b|Décisions?\s+\d)")
_FN_SYMBOL = re.compile(r"(?:FCCC|UNFCCC|A/RES|A)/[\w/.\-]*?[\w)](?:\s+and\s+(?:Add|Corr)\.\d)?\.(?=\s|$)")
_GRAMMAR = {"paragraph", "paragraphs", "para", "para.", "paras", "article", "articles", "art.", "decision",
            "decisions", "section", "sections", "chapter", "annex", "option", "options", "table", "figure", "and",
            "or", "to", "of", "than", "page", "pages", "item", "items", "goal", "target", "box", "version", "alt",
            "-", "–", "cma", "cop", "cmp"}


def _strip_footnote_defs(line: str, called: set[int] | None = None) -> tuple[str, list[tuple[int, str]]]:
    """Retire d'une ligne les définitions de notes (au début, ou collées au texte qui suit) ; renvoie (reste, notes)."""
    notes: list[tuple[int, str]] = []
    weak = _FN_WEAK.match(line)
    if weak and called and int(weak.group(1)) in called and not _FN_START.match(line.lstrip()):
        return "", [(int(weak.group(1)), " ".join(line[weak.end():].split()))]
    out = line
    while True:
        m = _FN_START.search(out)
        if not m:
            break
        # Seulement en tête de ligne, ou après une autre définition, ou si c'est une cote (« … measures 1 FCCC/… »).
        head = out[: m.start()].strip()
        rest = out[m.end():]
        sym = _FN_SYMBOL.match(rest)
        if head and not sym:
            break
        if sym:
            text, after = rest[: sym.end()], rest[sym.end():]
            # Plusieurs cotes dans la même note : « FCCC/… and FCCC/… . »
        else:
            text, after = rest, ""
            nxt = _FN_START.search(rest[1:])
            if nxt:
                text, after = rest[: nxt.start() + 1], rest[nxt.start() + 1:]
        notes.append((int(m.group(1)), " ".join(text.split())))
        out = (out[: m.start()] + " " + after) if head else after
    return out, notes


def _strip_calls(text: str, defined: set[int]) -> tuple[str, list[int]]:
    """Retire les appels de note (« thereon1 », « [first,2 second3] », « stocktake4 ») dont la note est connue."""
    calls: list[int] = []
    if not defined:
        return text, calls

    def glued(m: re.Match) -> str:
        n = int(m.group(2))
        if n in defined:
            calls.append(n)
            return m.group(1)
        return m.group(0)

    # Collé à un mot en minuscules ou à une ponctuation (« thereon1 », « first,2 ») ; jamais « CMA.5 », « CO2 ».
    text = re.sub(r"([a-zà-ÿ,;\])])(\d{1,2})(?=[\s,.;:\])]|$)", glued, text)

    def spaced(m: re.Match) -> str:
        n = int(m.group(2))
        prev = (m.group(1) or "").lower()
        if n in defined and prev not in _GRAMMAR:
            calls.append(n)
            return m.group(1) + m.group(3)
        return m.group(0)

    # Isolé : « first, 2 second » (après une virgule) ou « second 3] » (avant un crochet fermant).
    text = re.sub(r"(\S+,)\s(\d{1,2})(\s)(?=[a-z])", spaced, text)
    text = re.sub(r"(\w+)\s(\d{1,2})(\s*)(?=\])", spaced, text)
    return text, calls


# --- crochets ---------------------------------------------------------------------------------------------------------

@dataclass
class Bracket:
    content: str             # contenu sans les crochets extérieurs (crochets imbriqués conservés)
    start: int               # position du « [ » dans le texte de l'unité
    end: int                 # position après le « ] »
    depth: int = 0
    closed: bool = True


def brackets(text: str) -> list[Bracket]:
    """Crochets de premier niveau, dans l'ordre. Un crochet non refermé dans l'unité va jusqu'à la fin."""
    out: list[Bracket] = []
    depth, start = 0, 0
    for i, ch in enumerate(text):
        if ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]" and depth:
            depth -= 1
            if depth == 0:
                out.append(Bracket(text[start + 1: i].strip(), start, i + 1))
    if depth:
        out.append(Bracket(text[start + 1:].strip(), start, len(text), closed=False))
    return [b for b in out if b.content and not re.fullmatch(r"[\d\s,.*–-]*|x+|\.\.\.|…", b.content)]


def bracket_groups(text: str) -> list[list[Bracket]]:
    """Crochets regroupés en alternatives : « [A][B] » ou « [A] [B] » (même ligne) = choix entre A et B."""
    groups: list[list[Bracket]] = []
    for b in brackets(text):
        gap = text[groups[-1][-1].end: b.start] if groups else None
        # « ][ » : alternative. « ] [ » : souvent un retour à la ligne du PDF ; alternative seulement si les deux
        # formulations sont de longueur comparable (« [the need for] [the importance of] »).
        if gap == "" or (gap == " " and _comparable_len(groups[-1][-1].content, b.content)):
            groups[-1].append(b)
        else:
            groups.append([b])
    return groups


def _comparable_len(a: str, b: str) -> bool:
    la, lb = len(a.split()), len(b.split())
    return max(la, lb) <= 3 * max(1, min(la, lb))


def is_placeholder(content: str) -> bool:
    return bool(re.match(r"\s*(placeholder|espace réservé)", content, re.I))


# --- unités -----------------------------------------------------------------------------------------------------------

_PARA = re.compile(r"^\s*\[?\s*(\d{1,3})(?:\s?(bis|ter|quater)\.|\s?(alt\.?\s?\d*)\.?|\.)(?:\s+(?=\S)|\s*$)", re.I)
_OPTION = re.compile(r"^\s*\[?\s*(?:Option|Alt(?:ernative)?)\s+(\d{1,2}[A-Za-z]?|[A-Z])\s*[:.\-–]?\s*", re.I)
_BULLET = re.compile(r"^\s*[•▪●◦‣]\s*|^\s*[-–]\s+(?=\S)")
_SUB = re.compile(r"^\s*\[?\s*\(([a-z]{1,2}|[ivxl]{1,6})\)\s+(?=\S)")
_INTRO = re.compile(r"^\s*(The\s+(Conference|Subsidiary|Meeting)|La\s+Conférence|L'Organe|Le\s+Comité)\b")
_TABLE = re.compile(r"^\s*Table\s+(\d+)\s*[:.]?\s*$", re.I)
_ROW_CODE = re.compile(r"^\s*([A-Z]{1,2}\.\d+(?:\.\d+)*)\b")
_HEADING = re.compile(r"^\s*(Annex(?:e)?\b.*|Appendix\b.*|[IVX]{1,5}\.\s+\S.*|[A-H]\.\s+[A-Z].*)$")
_LEAD = re.compile(r"^\s*\[?\s*(?:\d{1,3}(?:\s?(?:bis|ter|quater)\.|\s?alt\.?\s?\d*\.?|\.)|\([a-z]{1,2}\)|\([ivxl]{1,6}\)|[•▪●◦‣]|[-–])\s*")
_OPT_LEAD = re.compile(r"^\s*\[?\s*(?:Option|Alt(?:ernative)?)\s+(?:\d{1,2}[A-Za-z]?|[A-Z])\s*[:.\-–]?\s*", re.I)
NO_TEXT = re.compile(r"^\W*(no text|pas de texte|no decision|no draft decision)\W*$", re.I)
_ROMAN = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii", "xiii", "xiv", "xv"]


@dataclass
class Unit:
    uid: str                         # identifiant hiérarchique stable : « §4.opt2.b3 », « PP2 », « §3.a.ii »
    kind: str                        # entete | pp | para | option | puce | alinea | titre | bloc
    parent: str | None
    lines: list[tuple[int, str]] = field(default_factory=list)   # (position dans le texte du document, ligne)
    text: str = ""                   # texte propre de l'unité (sans ses sous-unités), notes retirées
    notes: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    passages: list[int] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)

    @property
    def body(self) -> str:
        """Texte sans le numéro, la puce ni l'intitulé d'option en tête."""
        t = _LEAD.sub("", self.text, count=1)
        return _OPT_LEAD.sub("", t, count=1).strip() if self.kind == "option" else t.strip()

    @property
    def no_text(self) -> bool:
        return self.kind == "option" and bool(NO_TEXT.match(self.body))


def _split_glued(line: str, expect: int) -> list[str]:
    """Coupe une ligne là où le PDF a collé un nouveau paragraphe, une option, une puce ou un alinéa."""
    parts = [line]
    # « … processes; 4. Option 1: no text » : numéro attendu (n+1) après une fin de phrase.
    pat = re.compile(rf"(?<=[;:.\]])\s+(?=\[?\s*(?:{expect}|{expect + 1})\.\s+(?:Option\b|\[|[A-Z]))")
    out: list[str] = []
    for p in parts:
        out.extend(x for x in pat.split(p) if x is not None)
    parts, out = out, []
    for p in parts:
        out.extend(re.split(r"(?<=\S)\s+(?=\[?\s*Option\s+\d{1,2}[A-Za-z]?\s*:)", p))
    parts, out = out, []
    for p in parts:
        out.extend(re.split(r"(?<=\S)\s+(?=[•▪●◦‣]\s)", p))
    parts, out = out, []
    for p in parts:
        out.extend(re.split(r"(?<=[;:])\s+(?=\[?\s*\((?:[a-z]|[ivx]{1,4})\)\s+[A-Za-z\[])", p))
    parts, out = out, []
    for p in parts:
        out.extend(re.split(r"(?<=\S)\s+(?=Table\s+\d+\s*$)", p))
    return [p for p in out if p.strip()]


def _clean(text: str) -> str:
    text = " ".join(text.split())
    text = re.sub(r"(\w)- (?!and\b|or\b|et\b|ou\b)(?=[a-z])", r"\1-", text)   # « long- term » → « long-term »
    text = re.sub(r"\[\s+", "[", text)
    text = re.sub(r"\s+\]", "]", text)
    return text


@dataclass
class Document:
    doc_id: int
    units: list[Unit]
    footnotes: dict[int, str]
    header: str

    def get(self, uid: str) -> Unit | None:
        return self._index.get(uid)

    def __post_init__(self) -> None:
        self._index = {u.uid: u for u in self.units}

    def descendants(self, uid: str) -> list[Unit]:
        out, stack = [], list(reversed(self._index[uid].children))
        while stack:
            u = self._index[stack.pop()]
            out.append(u)
            stack.extend(reversed(u.children))
        return out

    def full_text(self, uid: str) -> str:
        return " ".join(u.text for u in [self._index[uid], *self.descendants(uid)] if u.text)

    def top(self, uid: str) -> Unit:
        u = self._index[uid]
        while u.parent:
            u = self._index[u.parent]
        return u


def parse(rows: list[dict[str, Any]], doc_id: int = 0) -> Document:
    """rows : passages du document dans l'ordre (id, page, text). Renvoie les unités dans l'ordre du texte."""
    # Texte complet, avec la correspondance position → passage.
    spans: list[tuple[int, int, int, int]] = []
    lines: list[tuple[int, str]] = []
    pos = 0
    for r in rows:
        t = r["text"] or ""
        spans.append((pos, pos + len(t), r["id"], r["page"] or 1))
        off = pos
        for ln in t.split("\n"):
            lines.append((off, ln))
            off += len(ln) + 1
        pos += len(t) + 1

    # 1. Notes de bas de page : définitions retirées des lignes. Appels repérés d'abord (« thereon1 », « Parties,2 »).
    called = {int(n) for n in re.findall(r"[a-z\u00e0-\u00ff,;)\]](\d{1,2})(?=[\s,.;:\])]|$)",
                                          "\n".join(ln for _, ln in lines), flags=re.M)}
    footnotes: dict[int, str] = {}
    kept: list[tuple[int, str]] = []
    for off, ln in lines:
        rest, notes = _strip_footnote_defs(ln, called)
        for n, t in notes:
            footnotes.setdefault(n, t)
        if rest.strip():
            kept.append((off, rest))
    defined = set(footnotes)

    # 2. Repérage des unités ligne par ligne.
    numbered = sum(1 for _, ln in kept if _PARA.match(ln))
    structured = numbered >= 2
    units: list[Unit] = []
    by_id: dict[str, Unit] = {}
    seen_ids: dict[str, int] = {}
    state: dict[str, Any] = {"para": None, "opt": None, "bul": None, "sub": None, "last_num": 0, "section": "",
                             "pp": 0, "intro": False, "bullets": 0, "subs": [], "bloc": 0, "table": None, "rows": 0}

    def new(uid: str, kind: str, parent: Unit | None) -> Unit:
        base = (state["section"] + uid) if kind in ("para", "titre") else uid
        n = seen_ids.get(base, 0) + 1
        seen_ids[base] = n
        real = base if n == 1 else f"{base} ({n})"
        u = Unit(real, kind, parent.uid if parent else None)
        if parent:
            parent.children.append(real)
        units.append(u)
        by_id[real] = u
        return u

    cur: Unit | None = None
    if not structured:
        # Texte sans paragraphes numérotés : un bloc par passage.
        for r in rows:
            state["bloc"] += 1
            u = new(f"bloc {state['bloc']}", "bloc", None)
            off = next(s for s, _e, pid, _p in spans if pid == r["id"])
            u.lines.append((off, r["text"] or ""))
    else:
        cur = new("en-tête", "entete", None)
        expect = 1
        for off, raw in kept:
            for ln in _split_glued(raw, expect):
                # Tableaux (plans d'action en annexe) : une unité par ligne, repérée par son code (« A.1 »).
                t = _TABLE.match(ln)
                if t:
                    cur = new(f"Table {t.group(1)}", "titre", None)
                    state.update(para=None, opt=None, bul=None, sub=None, bullets=0, subs=[], table=cur.uid, rows=0)
                    cur.lines.append((off, ln))
                    continue
                if state["table"]:
                    pm = _PARA.match(ln)
                    if pm and " | " not in ln and int(pm.group(1)) == state["last_num"] + 1:
                        state["table"] = None
                    elif " | " in ln and (_ROW_CODE.match(ln) or state["rows"] == 0):
                        state["rows"] += 1
                        code = _ROW_CODE.match(ln)
                        cur = new(f"{state['table']}.{code.group(1) if code else 'en-tête'}", "ligne",
                                  by_id.get(state["table"]))
                        cur.lines.append((off, ln))
                        continue
                    else:
                        cur.lines.append((off, ln))
                        continue
                m = _PARA.match(ln)
                if m:
                    num = int(m.group(1))
                    last = state["last_num"]
                    variant = m.group(2) or m.group(3)
                    if num == 1 or num == last + 1 or (not last and num <= 10) or (last and 0 < num - last <= 3) or (
                            variant and num == last):
                        suffix = re.sub(r"[\s.]", "", variant or "").lower()
                        cur = new(f"§{num}{suffix}", "para", None)
                        state.update(para=cur, opt=None, bul=None, sub=None, last_num=num, bullets=0, subs=[])
                        expect = num + 1
                        cur.lines.append((off, ln))
                        continue
                m = _OPTION.match(ln)
                if m and state["para"] is not None or (m and state["last_num"] == 0 and m.group(1)):
                    parent = state["para"]
                    cur = new(f"{parent.uid}.opt{m.group(1)}" if parent else f"opt{m.group(1)}", "option", parent)
                    state.update(opt=cur, bul=None, sub=None, bullets=0, subs=[])
                    cur.lines.append((off, ln))
                    continue
                if _BULLET.match(ln) and (state["para"] is not None or state["opt"] is not None):
                    parent = state["opt"] or state["para"]
                    state["bullets"] += 1
                    cur = new(f"{parent.uid}.b{state['bullets']}", "puce", parent)
                    state.update(bul=cur, sub=None)
                    cur.lines.append((off, ln))
                    continue
                m = _SUB.match(ln)
                if m and state["para"] is not None:
                    label = m.group(1)
                    subs = state["subs"]
                    letters = [s for s in subs if s[0] == "l"]
                    is_roman = label in _ROMAN and not (
                        letters and len(label) == 1 and ord(label) == ord(letters[-1][1]) + 1)
                    if label == "i" and not letters:
                        is_roman = True
                    if is_roman and letters:
                        parent = by_id.get(letters[-1][2]) or state["para"]
                    else:
                        parent = state["bul"] or state["opt"] or state["para"]
                    cur = new(f"{parent.uid}.{label}", "alinea", parent)
                    if not is_roman:
                        subs.append(("l", label, cur.uid))
                    cur.lines.append((off, ln))
                    continue
                if state["para"] is None:
                    # Avant le §1 : en-tête, puis préambule (« The Conference of the Parties, », « Recalling … »).
                    if _INTRO.match(ln) and not state["intro"]:
                        state["intro"] = True
                        cur = new("préambule", "pp", None)
                        cur.lines.append((off, ln))
                        continue
                    if state["intro"] and cur is not None and cur.kind == "pp" and re.match(r"\s*[A-Z]", ln) and (
                            _clean(" ".join(x for _, x in cur.lines)).endswith(",")):
                        state["pp"] += 1
                        cur = new(f"PP{state['pp']}", "pp", None)
                        cur.lines.append((off, ln))
                        continue
                elif _HEADING.match(ln) and len(ln.strip()) < 120 and not ln.rstrip().endswith((",", ";")):
                    state["table"] = None
                    if re.match(r"\s*Annex", ln, re.I):
                        state["section"] = "Annexe "
                        state["last_num"] = 0
                    cur = new(_clean(ln)[:40], "titre", None)
                    state.update(para=None, opt=None, bul=None, sub=None, bullets=0, subs=[])
                    state["intro"] = True
                    cur.lines.append((off, ln))
                    continue
                if cur is not None:
                    cur.lines.append((off, ln))

    # 3. Texte propre, appels de notes, passages et pages.
    for u in units:
        raw = _clean(" ".join(ln for _, ln in u.lines))
        text, calls = _strip_calls(raw, defined)
        u.text = _clean(text)
        u.notes = [footnotes[n] for n in dict.fromkeys(calls)]
        for off, ln in u.lines:
            for s, e, pid, page in spans:
                if s <= off < e or (s <= off + len(ln) - 1 < e and ln.strip()):
                    if pid not in u.passages:
                        u.passages.append(pid)
                    if page not in u.pages:
                        u.pages.append(page)
    header = next((u.text for u in units if u.kind == "entete"), "")
    units = [u for u in units if u.kind != "entete"]
    return Document(doc_id, units, footnotes, header)


def load(conn, doc_id: int) -> Document:
    rows = conn.execute("SELECT id, page, text FROM passages WHERE doc_id=? ORDER BY seq", (doc_id,)).fetchall()
    return parse([dict(r) for r in rows], doc_id)


def version_label(header: str) -> str | None:
    """« Version 16/11/2026 14:00 » lu dans l'en-tête des projets de texte, s'il y figure."""
    m = re.search(r"Version\s+(\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{1,2}[:h]\d{2})?)", header or "")
    return m.group(1) if m else None
