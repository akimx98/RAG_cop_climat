"""Comparaison de deux versions d'un projet de texte, unité par unité.

Ce qui compte pour suivre une négociation, et ce que ce module rend citable :
- quelles unités (paragraphe, option, puce, alinéa) sont modifiées, déplacées, reprises ailleurs, ajoutées, et
  surtout SUPPRIMÉES SANS REPRISE (rien de leur contenu ne subsiste) ;
- quelles options sont tranchées (laquelle est retenue, en tout ou en partie ; « no text » retenu de fait) ;
- le devenir de chaque crochet : levé (texte gardé), supprimé, maintenu ; pour les alternatives [A][B], laquelle
  l'emporte ;
- les glissements de force juridique : verbe de tête (Urges → Encourages), shall/should, ajout de « as appropriate ».

Unités lues par `structure` (découpage sûr même quand le PDF colle un « 4. Option 1 » au §3 ; notes de bas de page
retirées du texte et comparées à part). Appariement par CONTENU, pas par numéro : identiques d'abord, puis semblables
(≥ 50 % de mots en commun dans l'ordre), puis reprises partielles (une puce scindée en deux paragraphes, un
paragraphe réécrit) par couverture : part des mots de l'une retrouvés en blocs dans l'autre. Chaque changement porte
ses références (passage, page, lien vers le PDF à la page) pour l'ancienne et la nouvelle version. Aucun texte n'est
coupé : la liste se pagine (`page`) quand elle dépasse le budget.
"""

from __future__ import annotations

import difflib
import json
import re
from typing import Any

from . import structure
from .structure import Unit

BRACKET = re.compile(r"\[")
OPTION = re.compile(r"\b(Option|Alt(?:ernative)?)\s*\d+", re.I)
_LEAD = re.compile(r"^\s*(?:\(?[0-9]{1,3}[.)]|\([a-z]{1,4}\)|[a-z]\))\s+", re.I)  # « 5. », « (a) », « b) »
_NUM = re.compile(r"\d+")
_WORD = re.compile(r"\w+")
_TOKEN = re.compile(r"\[|\]|\w(?:[\w'’/.\-]*\w)?|[^\s\w\[\]]")
MATCH_MIN = 0.5        # similarité mot à mot pour « modifié »
COVER_MIN = 0.5        # part des mots retrouvés en blocs pour « repris »
BLOCK_MIN = 3          # taille minimale d'un bloc de mots communs (évite « the », « of »)

# Force du verbe de tête (projets de décision et de conclusions).
VERB_RANK = {"decides": 5, "agrees": 5, "adopts": 5, "requests": 4, "shall": 4, "urges": 3, "calls": 3,
             "encourages": 2, "invites": 2, "recommends": 2, "welcomes": 1, "notes": 1, "takes": 1,
             "acknowledges": 1, "recognizes": 1, "recognises": 1, "expresses": 1, "recalls": 1, "reaffirms": 1,
             "emphasizes": 1, "emphasises": 1, "underlines": 1, "underscores": 1, "stresses": 1, "highlights": 1,
             "appreciates": 1, "affirms": 1, "reiterates": 1, "considers": 1}
MODALS = ("shall", "should", "must", "may", "will")
# Formules qui nuancent ou affaiblissent un engagement (leur ajout ou leur retrait se suit à la loupe).
QUALIFIERS = ("as appropriate", "where appropriate", "as applicable", "in accordance with national circumstances",
              "taking into account national circumstances", "in the light of different national circumstances",
              "nationally determined", "on a voluntary basis", "voluntary", "to the extent possible", "inter alia",
              "as soon as possible", "urgently", "with a view to", "common but differentiated responsibilities",
              "respective capabilities", "in line with", "consistent with", "including through")

STATUS_FR = {"modifié": "modifiés", "ajouté": "ajoutés", "supprimé_sans_reprise": "supprimés sans reprise",
             "repris_de": "repris d'une autre unité", "repris_dans": "fusionnés dans une autre unité",
             "notes_modifiees": "notes modifiées", "attente_supprimee": "espaces réservés retirés"}
STATUS_EN = {"notes_modifiees": "footnotes changed", "attente_supprimee": "placeholders removed", "modifié": "modified", "ajouté": "added", "supprimé_sans_reprise": "deleted (no trace)",
             "repris_de": "taken from", "repris_dans": "merged into", "renuméroté": "renumbered",
             "déplacé": "moved", "inchangé": "unchanged"}


# --- compatibilité : paragraphes bruts (utilisés par `crochets`) -------------------------------------------------------

def _paras(conn, doc_id: int) -> list[tuple[str, str]]:
    """(paragraphe, texte) dans l'ordre du document, à partir des unités structurées : paragraphe avec ses options,
    puces et alinéas. Notes de bas de page retirées."""
    doc = structure.load(conn, doc_id)
    out = []
    for u in doc.units:
        if u.parent is None:
            out.append((u.uid, doc.full_text(u.uid)))
    return out


def _body(text: str) -> str:
    return _LEAD.sub("", text).strip()


def fully_bracketed(text: str) -> bool:
    """Texte entièrement entre crochets (« [5. Decides to …] », « 5. [Decides to …] ») : tout est contesté."""
    b = _body(text).rstrip(" .;,")
    b = re.sub(r"^\s*(?:Option\s+\w+\s*:\s*|[•▪●◦‣]\s*)", "", b)
    if not b.startswith("["):
        return False
    depth = 0
    for i, ch in enumerate(b):
        if ch == "[":
            depth += 1
        elif ch == "]" and depth:
            depth -= 1
            if depth == 0:
                return i >= len(b) - 1
    return True


# --- mots, similarité, diff mot à mot ----------------------------------------------------------------------------------

def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _ratio(a: list[str], b: list[str]) -> float:
    return difflib.SequenceMatcher(a=a, b=b, autojunk=False).ratio() if a and b else 0.0


def _blocks(a: list[str], b: list[str], size: int = BLOCK_MIN) -> list[tuple[int, int, int]]:
    return [(m.a, m.b, m.size) for m in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_matching_blocks()
            if m.size >= size]


def _cover(src: list[str], dst: list[str]) -> tuple[float, tuple[int, int] | None]:
    """Part des mots de `dst` retrouvés en blocs dans `src`, et l'étendue correspondante dans `src`."""
    if not src or not dst:
        return 0.0, None
    bl = _blocks(src, dst)
    if not bl:
        return 0.0, None
    return sum(s for _, _, s in bl) / len(dst), (bl[0][0], bl[-1][0] + bl[-1][2])


def _tokens(text: str) -> list[tuple[str, bool]]:
    """Mots et crochets, avec « précédé d'une espace » pour restituer le texte tel quel."""
    return [(m.group(), m.start() > 0 and text[m.start() - 1].isspace()) for m in _TOKEN.finditer(text)]


def _join(toks: list[tuple[str, bool]], first: bool) -> str:
    out = []
    for k, (t, sp) in enumerate(toks):
        out.append((" " if sp and (k or first) else "") + t)
    return "".join(out)


def render_diff(a: str, b: str) -> str:
    """Texte COMPLET de la nouvelle version, avec [-supprimé-] et {+ajouté+} ; rien n'est abrégé."""
    ta, tb = _tokens(a), _tokens(b)
    out: list[str] = []
    sm = difflib.SequenceMatcher(a=[t for t, _ in ta], b=[t for t, _ in tb], autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        lead = bool(out)
        if op == "equal":
            out.append(_join(tb[j1:j2], lead))
            continue
        if op in ("delete", "replace"):
            seg = _join(ta[i1:i2], False)
            out.append((" " if lead and ta[i1][1] else "") + "[-" + seg + "-]")
        if op in ("insert", "replace"):
            seg = _join(tb[j1:j2], False)
            out.append((" " if op == "replace" or (lead and tb[j1][1]) else "") + "{+" + seg + "+}")
    return "".join(out).strip()


def _extract(text: str, span: tuple[int, int] | None) -> str:
    """Portion d'un texte correspondant à une étendue de mots (pour comparer une reprise à sa source)."""
    if not span:
        return text
    ms = list(_WORD.finditer(text))
    if not ms:
        return text
    s, e = ms[span[0]].start(), ms[min(span[1], len(ms)) - 1].end()
    # Étendre aux crochets et à la ponctuation qui entourent.
    while s > 0 and text[s - 1] in "[(":
        s -= 1
    while e < len(text) and text[e] in "]);,.:":
        e += 1
    return text[s:e]


# --- crochets ----------------------------------------------------------------------------------------------------------

def _clip(text: str, n: int = 40) -> str:
    """Contenu de crochet abrégé dans les listes de crochets (le texte intégral est dans le diff de l'unité)."""
    w = text.split()
    return text if len(w) <= n else " ".join(w[: n - 8]) + " […] " + " ".join(w[-6:])


def _contains(hay: list[str], needle: list[str]) -> float:
    if not needle:
        return 0.0
    if len(needle) <= 6:   # formulation courte : présente telle quelle ou pas du tout
        n = len(needle)
        return 1.0 if any(hay[k:k + n] == needle for k in range(len(hay) - n + 1)) else 0.0
    bl = _blocks(hay, needle, 2)
    return sum(s for _, _, s in bl) / len(needle)


def _context_words(text: str, br: structure.Bracket) -> list[str]:
    """Contenu d'un crochet court avec le mot d'avant et d'après (« report[s] on » → « reports on »)."""
    before, after = text[: br.start], text[br.end:]
    glued_l = bool(before) and not before[-1].isspace()
    glued_r = bool(after) and not after[0].isspace()
    pre = re.search(r"(\S+)\s*$", before)
    post = re.match(r"\s*(\S+)", after)
    s = ((pre.group(1) if pre else "") + ("" if glued_l else " ") + br.content + ("" if glued_r else " ")
         + (post.group(1) if post else ""))
    return _words(re.sub(r"[\[\]]", "", s))


def _additive(group: list[structure.Bracket]) -> bool:
    """« [A][, and B] » : crochets successifs (ajouts cumulables), pas une alternative entre A et B."""
    return any(re.match(r"\s*(?:[,;]|and\b|or\b|including\b|as well as\b|et\b|y compris\b)", b.content, re.I)
               for b in group[1:])


def _bracket_fates(old_text: str, targets: list[Unit], lang: str, primary: str | None = None,
                   nested: bool = True) -> list[dict[str, Any]]:
    """Devenir de chaque crochet (ou alternative) d'une unité dans les unités qui la reprennent. Un niveau
    d'imbrication est suivi (« [noting [the need for][the importance of] …] »)."""
    T = (lambda fr, en: en if lang == "en" else fr)
    hays = [(u.uid, _words(re.sub(r"[\[\]]", " ", u.body))) for u in targets]
    kept_spans = []
    for u in targets:
        for b in structure.brackets(u.body):
            kept_spans.append(_words(b.content))
            kept_spans += [_words(b2.content) for b2 in structure.brackets(b.content)]

    def fate(text: str, br: structure.Bracket) -> str:
        w = _words(br.content)
        if not targets:
            return T("supprimé (avec l'unité)", "deleted (with the unit)")
        if any(w == k or (len(w) > 3 and _ratio(w, k) >= 0.85) for k in kept_spans):
            return T("maintenu", "still bracketed")
        probe = _context_words(text, br) if len(w) <= 2 else w
        best_uid, best = None, 0.0
        for uid, hay in hays:
            c = _contains(hay, probe)
            if c > best:
                best_uid, best = uid, c
        where = T(f" (en {best_uid})", f" (in {best_uid})") if best_uid and primary and best_uid != primary else ""
        if best >= 0.85:
            return T("levé", "lifted") + where
        if best >= (0.4 if len(probe) > 12 else 0.5):
            return T("levé, reformulé", "lifted, reworded") + where
        if best >= 0.3 and len(probe) > 12:
            return T(f"en partie repris ({best:.0%})", f"partly kept ({best:.0%})") + where
        return T("supprimé", "deleted")

    def simple(text: str, b: structure.Bracket, level: int) -> dict[str, Any]:
        item: dict[str, Any] = {"contenu": _clip(b.content), "type": "attente" if structure.is_placeholder(b.content)
                                else "simple", "devenir": fate(text, b)}
        if level > 1:
            item["niveau"] = level
        return item

    def walk(text: str, level: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for group in structure.bracket_groups(text):
            if len(group) == 1 or _additive(group):
                out.extend(simple(text, b, level) for b in group)
            else:
                fates = [fate(text, b) for b in group]
                lifted = [b.content for b, f in zip(group, fates) if f.startswith(T("levé", "lifted"))]
                if any(f == T("maintenu", "still bracketed") for f in fates):
                    issue = T("alternative maintenue", "alternative still open")
                elif not targets:
                    issue = T("supprimée avec l'unité", "deleted with the unit")
                elif len(lifted) == 1:
                    issue = T(f"« {_clip(lifted[0], 20)} » retenu", f"“{_clip(lifted[0], 20)}” retained")
                elif lifted:
                    issue = T("plusieurs choix gardés (fusion)", "several choices kept (merged)")
                elif any(f.startswith(T("en partie", "partly")) for f in fates):
                    part = next(b.content for b, f in zip(group, fates) if f.startswith(T("en partie", "partly")))
                    issue = T(f"« {_short(part, 8)} » en partie repris", f"“{_short(part, 8)}” partly kept")
                else:
                    issue = T("toutes les variantes supprimées", "all variants deleted")
                item = {"type": "alternative", "choix": [_clip(b.content) for b in group], "devenir": issue}
                if targets:
                    item["detail"] = fates
                if level > 1:
                    item["niveau"] = level
                out.append(item)
            if nested and level < 3 and targets:
                for b in group:
                    inner = walk(b.content, level + 1)
                    out.extend(inner)
        return out

    return walk(old_text, 1)


def _new_brackets(new_text: str, sources: list[Unit]) -> list[str]:
    old = [_words(b.content) for u in sources for b in structure.brackets(u.body)]
    old += [_words(b2.content) for u in sources for b in structure.brackets(u.body)
            for b2 in structure.brackets(b.content)]
    out = []
    for b in structure.brackets(new_text):
        w = _words(b.content)
        if not any(w == s or (len(w) > 3 and _ratio(w, s) >= 0.85) for s in old):
            out.append(b.content)
    return out


# --- force juridique ---------------------------------------------------------------------------------------------------

def _verb(text: str) -> str | None:
    m = re.match(r"^\W*((?:Also|Further|Strongly|Again|Once again)\s+)?([A-Z][a-z]+)(\s+(?:upon|on|note|with "
                 r"appreciation|with concern|with serious concern))?", text)
    if not m or m.group(2).lower() not in VERB_RANK:
        return None
    return ((m.group(1) or "") + m.group(2) + (m.group(3) or "")).strip()


def _force(old: str, new: str, lang: str) -> dict[str, Any]:
    T = (lambda fr, en: en if lang == "en" else fr)
    out: dict[str, Any] = {}
    va, vb = _verb(old), _verb(new)
    if va and vb and va.lower() != vb.lower():
        def rank(v: str) -> int:
            w = [x for x in v.split() if x not in ("Also", "Further", "Strongly", "Again", "Once", "again")]
            return VERB_RANK.get(w[0].lower(), 0) if w else 0

        ra, rb = rank(va), rank(vb)
        sens = T("renforcé", "stronger") if rb > ra else T("affaibli", "weaker") if rb < ra else T("équivalent", "similar")
        out["verbe"] = {"avant": va, "apres": vb, "sens": sens}
    ca, cb = " ".join(_words(old)), " ".join(_words(new))
    mod = {m: (len(re.findall(rf"\b{m}\b", ca)), len(re.findall(rf"\b{m}\b", cb))) for m in MODALS}
    mod = {m: f"{a}→{b}" for m, (a, b) in mod.items() if a != b}
    if mod:
        out["modaux"] = mod
    added = [q for q in QUALIFIERS if f" {q} " in f" {cb} " and f" {q} " not in f" {ca} "]
    removed = [q for q in QUALIFIERS if f" {q} " in f" {ca} " and f" {q} " not in f" {cb} "]
    if added or removed:
        out["nuances"] = {k: v for k, v in (("ajoutees", added), ("retirees", removed)) if v}
    return out


# --- appariement -------------------------------------------------------------------------------------------------------

def _placeholder(u: Unit) -> bool:
    """Unité réduite à un espace réservé « [Placeholder for …] »."""
    br = structure.brackets(u.body)
    return len(br) == 1 and structure.is_placeholder(br[0].content) and len(_words(u.body)) == len(_words(br[0].content))


def _comparable(u: Unit) -> bool:
    return bool(_words(u.body)) and not u.no_text


def _align(A: list[Unit], B: list[Unit]) -> tuple[dict[int, int], dict[int, float]]:
    """Appariement ancien → nouveau par contenu. Renvoie ({j nouveau: i ancien}, {j: similarité})."""
    pair: dict[int, int] = {}
    sim: dict[int, float] = {}
    used: set[int] = set()
    wa = [_words(u.body) for u in A]
    wb = [_words(u.body) for u in B]
    for norm in (lambda w: " ".join(w), lambda w: _NUM.sub("#", " ".join(w))):
        index: dict[str, list[int]] = {}
        for i, w in enumerate(wa):
            if i not in used and w:
                index.setdefault(norm(w), []).append(i)
        for j, w in enumerate(wb):
            if j in pair or not w:
                continue
            cands = [i for i in index.get(norm(w), []) if i not in used]
            if cands:
                i = min(cands, key=lambda i: abs(i / max(len(A), 1) - j / max(len(B), 1)))
                pair[j], sim[j] = i, 1.0
                used.add(i)
    sa, sb = [set(w) for w in wa], [set(w) for w in wb]
    cands = []
    for j in range(len(B)):
        if j in pair or not sb[j]:
            continue
        for i in range(len(A)):
            if i in used or not sa[i]:
                continue
            if len(sa[i] & sb[j]) / len(sa[i] | sb[j]) < 0.25:
                continue
            r = _ratio(wa[i], wb[j])
            if r >= MATCH_MIN:
                rel = abs(i / max(len(A), 1) - j / max(len(B), 1))
                cands.append((r - 0.1 * rel, r, i, j))
    for _score, r, i, j in sorted(cands, reverse=True):
        if j in pair or i in used:
            continue
        pair[j], sim[j] = i, r
        used.add(i)
    # Lignes de tableau : le code d'activité (« A.1 ») est un identifiant stable, même quand la ligne est réécrite.
    by_uid = {u.uid: i for i, u in enumerate(A) if u.kind == "ligne" and i not in used}
    for j, v in enumerate(B):
        i = by_uid.get(v.uid)
        if v.kind == "ligne" and j not in pair and i is not None and i not in used:
            r = _ratio(wa[i], wb[j])
            if r >= 0.2:
                pair[j], sim[j] = i, r
                used.add(i)
    return pair, sim


# --- comparaison -------------------------------------------------------------------------------------------------------

def _counts(doc: structure.Document) -> dict[str, int]:
    units = [u for u in doc.units if _comparable(u) or u.no_text]
    text = " ".join(u.text for u in doc.units)
    groups = [g for u in units for g in structure.bracket_groups(u.body)]
    return {"paragraphes": sum(1 for u in doc.units if u.kind == "para"),
            "unites": len(units),
            "crochets": len(BRACKET.findall(text)),
            "paires_de_crochets": sum(len(g) for g in groups),
            "alternatives": sum(1 for g in groups if len(g) > 1),
            "options": sum(1 for u in doc.units if u.kind == "option"),
            "unites_avec_crochets": sum(1 for u in units if "[" in u.body),
            "unites_entre_crochets": sum(1 for u in units if fully_bracketed(u.text)),
            "paragraphes_entre_crochets": sum(1 for u in doc.units if u.kind == "para"
                                              and fully_bracketed(doc.full_text(u.uid))),
            "notes": len(doc.footnotes),
            "caracteres": len(text)}


def _ref(drow, u: Unit, quote: str | None = None) -> dict[str, Any]:
    from . import search

    pid = u.passages[0] if u.passages else None
    page = u.pages[0] if u.pages else None
    out: dict[str, Any] = {"unite": u.uid, "doc_id": drow["id"], "cote": drow["symbol"], "passage_id": pid,
                           "page": page}
    if len(u.passages) > 1:
        out["passages"] = u.passages
    if pid:
        links = search.deep_links(drow, page, quote or u.body, pid)
        out["lien_source"] = links.get("lien_source") or links.get("lien_page")
        out["lien_passage"] = links["lien_passage"]
    return {k: v for k, v in out.items() if v is not None}


def _context(doc: structure.Document, u: Unit, lang: str) -> str | None:
    """« §4, Option 2 (en balance avec Option 1 : no text) » pour une unité dans une option."""
    T = (lambda fr, en: en if lang == "en" else fr)
    chain, p = [], u
    while p.parent:
        p = doc.get(p.parent)
        if p is None:
            break
        if p.kind == "option":
            parent = doc.get(p.parent) if p.parent else None
            others = [doc.get(c) for c in (parent.children if parent else []) if c != p.uid]
            others = [o for o in others if o and o.kind == "option"]
            label = re.match(r"\W*((?:Option|Alt(?:ernative)?)\s+\w+)", p.text)
            desc = (label.group(1) if label else p.uid)
            if others:
                alt = "; ".join((re.match(r"\W*((?:Option|Alt\w*)\s+\w+)", o.text) or [o.uid])[0]
                                + " : " + " ".join(o.body.split()[:8]) + ("…" if len(o.body.split()) > 8 else "")
                                for o in others)
                desc += T(f" (en balance avec {alt})", f" (against {alt})")
            chain.append(desc)
        else:
            chain.append(p.uid)
    return ", ".join(reversed(chain)) or None


def _option_fates(A: structure.Document, B: structure.Document, status: dict[str, str],
                  new_of: dict[str, list[str]], lang: str) -> list[dict[str, Any]]:
    T = (lambda fr, en: en if lang == "en" else fr)
    alive = {"modifié", "inchangé", "déplacé", "renuméroté", "repris"}
    out = []
    for para in A.units:
        opts = [A.get(c) for c in para.children if A.get(c) and A.get(c).kind == "option"]
        if len(opts) < 2:
            continue
        rows = []
        for o in opts:
            desc = [x for x in [o, *A.descendants(o.uid)] if _comparable(x) and not _placeholder(x)]
            surv = [x for x in desc if status.get(x.uid) in alive]
            label = (re.match(r"\W*((?:Option|Alt\w*)\s+\w+)", o.text) or [o.uid])[0]
            rows.append({"option": label, "unite": o.uid, "texte": " ".join(o.body.split()[:14])
                         + ("…" if len(o.body.split()) > 14 else ""), "pas_de_texte": o.no_text,
                         "_n": len(desc), "_surv": surv})
        # Les options existent-elles encore dans la nouvelle version, au même endroit ?
        targets = {t for x in [para, *A.descendants(para.uid)] for t in new_of.get(x.uid, [])}
        still = any(B.get(t) and (B.get(t).kind == "option" or any(
            B.get(c) and B.get(c).kind == "option" for c in B.get(t).children)) for t in targets)
        living = [r for r in rows if r["_surv"]]
        if still:
            issue = T("options toujours ouvertes", "options still open")
        elif len(living) == 1:
            r = living[0]
            partial = len(r["_surv"]) < r["_n"]
            kept = sorted({t for x in r["_surv"] for t in new_of.get(x.uid, [])},
                          key=lambda t: [u.uid for u in B.units].index(t) if B.get(t) else 0)
            dropped = [x.uid for x in [A.get(r["unite"]), *A.descendants(r["unite"])]
                       if _comparable(x) and not _placeholder(x) and x not in r["_surv"]]
            issue = T(f"{r['option']} retenue", f"{r['option']} retained")
            if partial:
                issue += T(" en partie", " in part")
            if kept:
                issue += T(f" (reprise en {', '.join(kept)})", f" (now {', '.join(kept)})")
            if partial and dropped:
                issue += T(f" ; supprimé : {', '.join(dropped)}", f"; deleted: {', '.join(dropped)}")
        elif not living:
            nt = next((r for r in rows if r["pas_de_texte"]), None)
            issue = (T(f"{nt['option']} (pas de texte) retenue de fait : le paragraphe disparaît",
                       f"{nt['option']} (no text) retained in effect: the paragraph is gone") if nt
                     else T("paragraphe supprimé avec ses options", "paragraph deleted with its options"))
        else:
            issue = T("contenu de plusieurs options repris (fusion)", "content of several options kept (merged)")
        for r in rows:
            n, s = r.pop("_n"), r.pop("_surv")
            if still:
                continue
            if r["pas_de_texte"]:
                r["devenir"] = (T("retenue de fait", "retained in effect") if not living
                                else T("écartée", "set aside"))
            else:
                r["devenir"] = (T("écartée", "set aside") if not s else T("retenue", "retained") if len(s) == n
                                else T(f"retenue en partie ({len(s)}/{n} unités)", f"retained in part ({len(s)}/{n} units)"))
        out.append({"paragraphe": para.uid, "issue": issue, "options": rows})
    return out


def compare(conn, old_id: int, new_id: int, max_items: int | None = None, *, budget: int | None = None,
            page: int = 1, filtre: str | None = None, lang: str = "fr") -> dict[str, Any]:
    """Diff structuré. `budget` (caractères JSON de la liste des changements par page) : None = tout."""
    T = (lambda fr, en: en if lang == "en" else fr)
    A, B = structure.load(conn, old_id), structure.load(conn, new_id)
    ra = conn.execute("SELECT * FROM documents WHERE id=?", (old_id,)).fetchone()
    rb = conn.execute("SELECT * FROM documents WHERE id=?", (new_id,)).fetchone()
    ua = [u for u in A.units if _comparable(u)]
    ub = [u for u in B.units if _comparable(u)]
    wa = [_words(u.body) for u in ua]
    wb = [_words(u.body) for u in ub]
    pair, sim = _align(ua, ub)
    matched_old = set(pair.values())

    # Reprises partielles : nouvelle unité non appariée tirée d'une ancienne (scission, réécriture).
    derived: dict[int, tuple[int, float, tuple[int, int] | None]] = {}
    for j in range(len(ub)):
        if j in pair or len(wb[j]) < 4:
            continue
        sj = set(wb[j])
        cands = [i for i in range(len(ua)) if len(sj & set(wa[i])) >= COVER_MIN * len(sj)]   # préfiltre
        best = max(((i, *_cover(wa[i], wb[j])) for i in cands), key=lambda x: x[1], default=None)
        if best and best[1] >= COVER_MIN:
            derived[j] = best
    derived_from = {i for i, *_ in derived.values()}
    # Ancienne unité non appariée dont le contenu est passé dans une nouvelle (fusion).
    merged: dict[int, tuple[int, float, tuple[int, int] | None]] = {}
    for i in range(len(ua)):
        if i in matched_old or i in derived_from or len(wa[i]) < 4:
            continue
        si = set(wa[i])
        cands = [j for j in range(len(ub)) if len(si & set(wb[j])) >= COVER_MIN * len(si)]   # préfiltre
        best = max(((j, *_cover(wb[j], wa[i])) for j in cands), key=lambda x: x[1], default=None)
        if best and best[1] >= COVER_MIN:
            merged[i] = best

    status: dict[str, str] = {}
    new_of: dict[str, list[str]] = {}
    for j, i in pair.items():
        new_of.setdefault(ua[i].uid, []).append(ub[j].uid)
    for j, (i, _c, _s) in derived.items():
        new_of.setdefault(ua[i].uid, []).append(ub[j].uid)
    for i, (j, _c, _s) in merged.items():
        new_of.setdefault(ua[i].uid, []).append(ub[j].uid)

    changes: list[dict[str, Any]] = []
    unchanged: list[str] = []
    moved: list[dict[str, str]] = []
    renumbered: list[str] = []
    emitted: set[int] = set()

    def targets_of(i: int) -> list[Unit]:
        return [B.get(t) for t in new_of.get(ua[i].uid, []) if B.get(t)]

    def old_item(i: int) -> None:
        u = ua[i]
        if i in merged:
            j, cov, span = merged[i]
            status[u.uid] = "repris"
            item = {"unite": u.uid, "statut": "repris_dans", "repris_dans": ub[j].uid, "couverture": round(cov, 2),
                    "ancien": _ref(ra, u), "nouveau": _ref(rb, ub[j]),
                    "diff": render_diff(u.body, _extract(ub[j].body, span))}
        else:
            status[u.uid] = "supprimé"
            placeholder = _placeholder(u)
            item = {"unite": u.uid, "statut": "attente_supprimee" if placeholder else "supprimé_sans_reprise",
                    "texte_ancien": u.body, "ancien": _ref(ra, u), "nouveau": None}
        ctx = _context(A, u, lang)
        if ctx:
            item["contexte"] = ctx
        cr = _bracket_fates(u.body, targets_of(i), lang, nested=False)
        if item["statut"] != "repris_dans":
            # Unité disparue : seules les alternatives disent quelque chose de plus que le texte cité.
            cr = [c for c in cr if c.get("type") == "alternative"]
        if cr:
            item["crochets"] = cr
        if u.notes:
            item["notes"] = {"supprimees": u.notes}
        changes.append(item)

    def flush(limit: int) -> None:
        for i in range(limit):
            if i not in matched_old and i not in derived_from and i not in emitted:
                emitted.add(i)
                old_item(i)

    for j, v in enumerate(ub):
        if j in pair:
            i = pair[j]
            flush(i)
            u = ua[i]
            if wa[i] == wb[j] and u.body == v.body:
                if u.uid == v.uid:
                    unchanged.append(v.uid)
                    status[u.uid] = "inchangé"
                else:
                    moved.append({"de": u.uid, "vers": v.uid})
                    status[u.uid] = "déplacé"
                if set(u.notes) != set(v.notes):
                    changes.append({"unite": v.uid, "statut": "notes_modifiees", "ancien": _ref(ra, u),
                                    "nouveau": _ref(rb, v), "notes": _notes(u, v)})
                continue
            if _NUM.sub("#", " ".join(wa[i])) == _NUM.sub("#", " ".join(wb[j])) and "[" not in u.body + v.body:
                renumbered.append(v.uid)
                status[u.uid] = "renuméroté"
                continue
            status[u.uid] = "modifié"
            src_only = False
            item = {"unite": v.uid, "statut": "modifié", "similarite": round(sim[j], 2),
                    "ancien": _ref(ra, u), "nouveau": _ref(rb, v), "diff": render_diff(u.body, v.body)}
            others = [t for t in new_of.get(u.uid, []) if t != v.uid]
            if others:
                item["repris_aussi_dans"] = others
        elif j in derived:
            i, cov, span = derived[j]
            u = ua[i]
            src_only = i not in matched_old and i not in emitted
            if src_only:
                flush(i)
                emitted.add(i)
                status[u.uid] = "repris"
            item = {"unite": v.uid, "statut": "repris_de", "repris_de": u.uid, "couverture": round(cov, 2),
                    "ancien": _ref(ra, u, _extract(u.body, span)), "nouveau": _ref(rb, v),
                    "diff": render_diff(_extract(u.body, span), v.body)}
        else:
            item = {"unite": v.uid, "statut": "ajouté", "texte_nouveau": v.body, "ancien": None, "nouveau": _ref(rb, v)}
            ctx = _context(B, v, lang)
            if ctx:
                item["contexte"] = ctx
            nb = _new_brackets(v.body, [])
            if nb:
                item["nouveaux_crochets"] = nb
            changes.append(item)
            continue
        ctx = _context(A, u, lang)
        if ctx:
            item["contexte_ancien"] = ctx
        if item["statut"] == "modifié" or src_only:
            cr = _bracket_fates(u.body, targets_of(i), lang, primary=v.uid)
            if cr:
                item["crochets"] = cr
        nb = _new_brackets(v.body, [u])
        if nb:
            item["nouveaux_crochets"] = nb
        if set(u.notes) != set(v.notes):
            item["notes"] = _notes(u, v)
        if item["statut"] == "modifié":
            force = _force(u.body, v.body, lang)
            combined = _force(u.body, " ".join(t.body for t in targets_of(i)), lang)
            force.pop("nuances", None)
            force.pop("modaux", None)
            force.update({k: combined[k] for k in ("nuances", "modaux") if k in combined})
        else:
            force = _force(_extract(u.body, derived[j][2]), v.body, lang)
        item.update(force)
        changes.append(item)
    flush(len(ua))

    # « No text » : options vides, jugées dans le devenir des options, pas comme des suppressions.
    options = _option_fates(A, B, status, new_of, lang)
    ca, cb = _counts(A), _counts(B)
    tally = _tally(changes)
    out: dict[str, Any] = {
        "ancienne_version": old_id,
        "nouvelle_version": new_id,
        "versions_lues": {"ancienne": structure.version_label(A.header), "nouvelle": structure.version_label(B.header)},
        "resume": _summary(changes, options, ca, cb, tally, lang),
        "avant": ca,
        "apres": cb,
        "evolution": {k: cb[k] - ca[k] for k in ca if cb[k] != ca[k]},
        "par_statut": tally,
        "options": options,
        "crochets": {k: v for k, v in _bracket_tally(changes, lang).items() if k != "alternatives"},
        "nb_changements": len(changes),
    }
    if filtre:
        f = filtre.strip().lower()
        if f.startswith("§") or re.match(r"(pp|bloc|annex|table)", f):
            def under(uid: str) -> bool:   # « §4 » : §4, §4.opt1, §4 (2)… mais pas §40
                uid = (uid or "").lower()
                return uid == f or uid.startswith(f + ".") or uid.startswith(f + " ")

            changes = [c for c in changes if under(c["unite"]) or any(
                under((c.get(k) or {}).get("unite", "")) for k in ("ancien", "nouveau"))]
        else:
            terms = [t.strip() for t in re.split(r"[,;]", f) if t.strip()]

            def text_of(c: dict[str, Any]) -> str:
                return " ".join([c.get("diff") or "", c.get("texte_ancien") or "", c.get("texte_nouveau") or "",
                                 json.dumps(c.get("crochets") or [], ensure_ascii=False)]).lower()

            changes = [c for c in changes if any(t in text_of(c) for t in terms)]
        out["filtre"] = {"filtre": filtre, "nb": len(changes)}
    first = rest = budget
    out["inchanges"] = unchanged
    if moved:
        out["deplaces_sans_changement"] = len(moved)
        out["deplaces"] = moved if not budget or len(moved) <= 20 else moved[:20] + [{"de": "…", "vers": "…"}]
    if renumbered:
        out["renumerotes"] = {"nb": len(renumbered), "paragraphes": renumbered,
                              "note": T("seuls des nombres changent (renvois, numérotation) : non comptés",
                                        "only numbers change (cross-references, numbering): not counted")}
    out["legende"] = T(
        "diff : texte complet de la nouvelle version, [-supprimé-] {+ajouté+} ; unités : §4.opt2.b3 = 3e puce de "
        "l'option 2 du §4 ; statut : modifié | repris_de (scission/réécriture, avec la source) | repris_dans (fusion) "
        "| ajouté | supprimé_sans_reprise (aucun contenu ne subsiste) ; notes de bas de page retirées du texte, "
        "comparées à part ; ancien/nouveau : passage, page et lien PDF à citer",
        "diff: full text of the new version, [-deleted-] {+added+}; units: §4.opt2.b3 = 3rd bullet of option 2 of "
        "§4; status: modifié (modified) | repris_de (split/rewritten, with source) | repris_dans (merged) | ajouté "
        "(added) | supprimé_sans_reprise (deleted, no trace left); footnotes removed from the text and compared "
        "separately; ancien/nouveau: passage, page and PDF link to cite")
    if budget and (len(options) > 6 or len(json.dumps(options, ensure_ascii=False)) > 2500):
        # Page de /ask : l'issue de chaque paragraphe à options, sans le détail option par option.
        out["options"] = [{"paragraphe": o["paragraphe"], "issue": o["issue"]} for o in options[:8]] + (
            [{"paragraphe": "…", "issue": T(f"+{len(options) - 8} paragraphes à options : filtre='§N'",
                                            f"+{len(options) - 8} paragraphs with options: filtre='§N'")}]
            if len(options) > 8 else [])
    if budget:
        # Le budget couvre toute la réponse : ce qui précède la liste (résumé, comptes) en est déduit.
        rest = max(3000, budget - len(json.dumps(out, ensure_ascii=False)) - 2500)
        first = rest
        if len(json.dumps(changes, ensure_ascii=False)) > rest:
            # Plusieurs pages : en page 1, un sommaire d'une ligne par changement, pour viser ensuite avec `filtre`.
            room = max(1500, budget - len(json.dumps(out, ensure_ascii=False)) - 4000)
            summary: list[str] = []
            for k, c in enumerate(changes):
                line = _index_line(c)
                if sum(len(x) + 4 for x in summary) + len(line) > room:
                    summary.append(T(f"… (+{len(changes) - k} : page suivante ou filtre)",
                                     f"… (+{len(changes) - k}: next page or filtre)"))
                    break
                summary.append(line)
            first = max(2000, rest - len(json.dumps(summary, ensure_ascii=False)))
            if page <= 1:
                out["sommaire"] = summary
    if rest:
        changes = [_fit(c, rest, lang) for c in changes]
    pages = _paginate(changes, first, max_items, rest)
    page = max(1, min(page, len(pages)))
    if page > 1 and budget:
        # Pages suivantes : le résumé, les comptes et les options sont en page 1.
        for k in ("resume", "avant", "apres", "evolution", "options", "crochets", "versions_lues"):
            out.pop(k, None)
    out["changements"] = pages[page - 1] if pages else []
    out["page"], out["pages"] = page, len(pages) or 1
    out["tronque"] = page < len(pages)
    if out["tronque"]:
        out["suite"] = T(f"changements suivants : même appel avec page={page + 1} (sur {len(pages)})",
                         f"more changes: same call with page={page + 1} (of {len(pages)})")
    return out


def _notes(u: Unit, v: Unit) -> dict[str, list[str]]:
    a, b = set(u.notes), set(v.notes)
    return {k: x for k, x in (("supprimees", [n for n in u.notes if n not in b]),
                              ("ajoutees", [n for n in v.notes if n not in a])) if x}


def _focus(diff_text: str, ctx: int = 30) -> str:
    """Diff réduit aux changements et à `ctx` mots de part et d'autre (« […] » entre deux zones)."""
    parts = re.split(r"(\[-.*?-\]|\{\+.*?\+\})", diff_text, flags=re.S)
    out = []
    for k, seg in enumerate(parts):
        if k % 2:
            out.append(seg)
            continue
        w = seg.split(" ")
        if len(w) <= 2 * ctx + 2:
            out.append(seg)
            continue
        head = " ".join(w[:ctx]) if k else ""
        tail = " ".join(w[-ctx:]) if k < len(parts) - 1 else ""
        out.append((head + " […] " + tail).strip() if head or tail else "")
    return "".join(out).strip()


def _fit(c: dict[str, Any], cap: int, lang: str) -> dict[str, Any]:
    """Unité trop longue pour une page (/ask) : abrégée AUTOUR des changements, et signalée comme telle."""
    if len(json.dumps(c, ensure_ascii=False)) <= cap:
        return c
    T = (lambda fr, en: en if lang == "en" else fr)
    c = dict(c)
    if c.get("diff"):
        c["diff"] = _focus(c["diff"])
    for k in ("texte_ancien", "texte_nouveau"):
        if c.get(k):
            w = c[k].split()
            c[k] = " ".join(w[:120]) + (" […]" if len(w) > 120 else "")
    if c.get("crochets") and len(c["crochets"]) > 12:
        c["crochets"] = c["crochets"][:12] + [{"type": "…", "devenir": T(f"+{len(c['crochets']) - 12} crochets",
                                                                         f"+{len(c['crochets']) - 12} brackets")}]
    if c.get("nouveaux_crochets"):
        nb = c["nouveaux_crochets"]
        c["nouveaux_crochets"] = [_clip(x, 20) for x in nb[:8]] + (["…"] if len(nb) > 8 else [])
    if c.get("diff") and len(json.dumps(c, ensure_ascii=False)) > cap:
        room = max(800, len(c["diff"]) - (len(json.dumps(c, ensure_ascii=False)) - cap))
        c["diff"] = c["diff"][:room].rsplit(" ", 1)[0] + " […]"
    c["abrege"] = T("unité très longue, abrégée autour des changements : texte intégral avec lire_passage "
                    "(passage_id) ou sur la page diff_en_ligne",
                    "very long unit, shortened around the changes: full text with lire_passage (passage_id) or "
                    "on the diff_en_ligne page")
    return c


def _index_line(c: dict[str, Any]) -> str:
    old = (c.get("ancien") or {}).get("unite")
    head = c["unite"] + (f" ← {old}" if old and old != c["unite"] else "")
    return f"{head} : {c['statut']}" + (f" ({c['similarite']})" if c.get("similarite") else "")


def _tally(changes: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in changes:
        out[c["statut"]] = out.get(c["statut"], 0) + 1
    return out


def _bracket_tally(changes: list[dict[str, Any]], lang: str) -> dict[str, Any]:
    T = (lambda fr, en: en if lang == "en" else fr)
    t = {"leves": 0, "supprimes": 0, "maintenus": 0, "nouveaux": 0}
    alternatives = []
    for c in changes:
        for b in c.get("crochets") or []:
            if b.get("type") == "alternative":
                alternatives.append({"unite": c["unite"], "choix": b["choix"], "devenir": b["devenir"]})
                continue
            d = b.get("devenir", "")
            key = ("leves" if d.startswith(T("levé", "lifted")) else "maintenus" if d.startswith(T("maintenu", "still"))
                   else "supprimes")
            t[key] += 1
        t["nouveaux"] += len(c.get("nouveaux_crochets") or [])
    return {**t, "alternatives": alternatives}


def _short(text: str, n: int = 12) -> str:
    w = text.split()
    return " ".join(w[:n]) + ("…" if len(w) > n else "")


def _summary(changes, options, ca, cb, tally, lang: str) -> list[str]:
    T = (lambda fr, en: en if lang == "en" else fr)
    lines = []
    parts = [f"{n} {T(STATUS_FR.get(k, k), STATUS_EN.get(k, k))}" for k, n in tally.items()]
    lines.append(T(f"{len(changes)} changements : ", f"{len(changes)} changes: ") + ", ".join(parts))
    for o in options[:8]:
        lines.append(f"{o['paragraphe']} : {o['issue']}")
    if len(options) > 8:
        lines.append(T(f"… (+{len(options) - 8} paragraphes à options : voir `options`)",
                       f"… (+{len(options) - 8} paragraphs with options: see `options`)"))
    bt = _bracket_tally(changes, lang)
    lines.append(T(f"crochets {ca['crochets']}→{cb['crochets']} : {bt['leves']} levés, {bt['supprimes']} supprimés, "
                   f"{bt['maintenus']} maintenus, {bt['nouveaux']} nouveaux ; alternatives tranchées : "
                   f"{sum(1 for a in bt['alternatives'] if 'maintenue' not in a['devenir'])}",
                   f"brackets {ca['crochets']}→{cb['crochets']}: {bt['leves']} lifted, {bt['supprimes']} deleted, "
                   f"{bt['maintenus']} kept, {bt['nouveaux']} new; alternatives settled: "
                   f"{sum(1 for a in bt['alternatives'] if 'open' not in a['devenir'])}"))
    for a in bt["alternatives"][:6]:
        lines.append(f"{a['unite']} : [" + "][".join(_short(x, 8) for x in a["choix"]) + f"] → {a['devenir']}")
    gone = [c for c in changes if c["statut"] == "supprimé_sans_reprise"]
    if gone:
        lines.append(T("supprimé sans reprise : ", "deleted with no trace: ")
                     + " ; ".join(f"{c['unite']} « {_short(c['texte_ancien'], 10)} »" for c in gone[:8])
                     + (" …" if len(gone) > 8 else ""))
    extra: list[str] = []
    for c in changes:
        if c.get("verbe"):
            v = c["verbe"]
            extra.append(f"{c['unite']} : {v['avant']} → {v['apres']} ({v['sens']})")
        if c.get("nuances"):
            n = c["nuances"]
            bits = ([T("ajout de ", "added ") + ", ".join(f"« {x} »" for x in n["ajoutees"])] if n.get("ajoutees") else []) + \
                   ([T("retrait de ", "removed ") + ", ".join(f"« {x} »" for x in n["retirees"])] if n.get("retirees") else [])
            extra.append(f"{c['unite']} : " + " ; ".join(bits))
        if c.get("modaux"):
            extra.append(f"{c['unite']} : " + ", ".join(f"{m} {d}" for m, d in c["modaux"].items()))
    lines += extra[:12] + ([T(f"… (+{len(extra) - 12} glissements de verbe ou de nuance)",
                              f"… (+{len(extra) - 12} verb or qualifier shifts)")] if len(extra) > 12 else [])
    added = [c for c in changes if c["statut"] == "ajouté"]
    if added:
        lines.append(T("ajouté : ", "added: ") + " ; ".join(f"{c['unite']} « {_short(c['texte_nouveau'], 10)} »"
                                                          for c in added[:6]) + (" …" if len(added) > 6 else ""))
    return lines


def _paginate(changes: list[dict[str, Any]], budget: int | None, max_items: int | None,
              rest: int | None = None) -> list[list[dict[str, Any]]]:
    """Pages de changements entiers : jamais de texte coupé, au plus `budget` caractères JSON en page 1, `rest`
    ensuite."""
    if not changes:
        return []
    if not budget and not max_items:
        return [changes]
    pages: list[list[dict[str, Any]]] = [[]]
    size = 0
    for c in changes:
        n = len(json.dumps(c, ensure_ascii=False))
        cap = budget if len(pages) == 1 else (rest or budget)
        full = (cap and pages[-1] and size + n > cap) or (max_items and len(pages[-1]) >= max_items)
        if full:
            pages.append([])
            size = 0
        pages[-1].append(c)
        size += n
    return pages


def previous_in_lineage(conn, doc_id: int) -> int | None:
    """Version précédente indexée du même texte (même lignée, ordre des versions), s'il y en a une.
    Un document L (proposé pour adoption) se compare au dernier projet, pas à un document L parallèle (COP / CMA)."""
    doc = conn.execute("SELECT lineage_key, version, symbol FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc or not doc["lineage_key"]:
        return None
    rows = conn.execute(
        "SELECT id, symbol FROM documents WHERE lineage_key=? AND id<>? AND status='indexed' "
        "AND (COALESCE(version,0) < ? OR (COALESCE(version,0) = ? AND id < ?)) ORDER BY version DESC, id DESC",
        (doc["lineage_key"], doc_id, doc["version"] or 0, doc["version"] or 0, doc_id),
    ).fetchall()
    is_l = "/L." in (doc["symbol"] or "")
    for r in rows:
        if is_l and "/L." in (r["symbol"] or ""):
            continue
        return r["id"]
    return None


def since_previous(conn, doc_id: int, max_items: int = 40) -> dict[str, Any] | None:
    """Diff d'un document avec la version précédente de sa lignée, ou None s'il est seul."""
    prev = previous_in_lineage(conn, doc_id)
    return compare(conn, prev, doc_id) if prev else None


def delta_line(d: dict[str, Any], lang: str = "fr") -> str:
    """Résumé d'une ligne : « 3 § modifiés · crochets 40→22 (-18, levés) · options 5→3 (-2) »."""
    av, ap = d["avant"], d["apres"]
    en = lang == "en"

    def part(label: str, old: int, new: int) -> str | None:
        if not old and not new:
            return None
        if old == new:
            return f"{label} {new} ({'unchanged' if en else 'inchangé'})"
        delta = new - old
        tag = (", resolved" if en else ", levés") if delta < 0 else (", new" if en else ", nouveaux")
        return f"{label} {old}→{new} ({delta:+d}{tag})"

    bits = [f"{d['nb_changements']} {'changes' if en else 'changements'}"]
    gone = (d.get("par_statut") or {}).get("supprimé_sans_reprise")
    if gone:
        bits.append(f"{gone} {'deleted' if en else 'supprimés sans reprise'}")
    for b in (part("brackets" if en else "crochets", av["crochets"], ap["crochets"]),
              part("options", av["options"], ap["options"])):
        if b:
            bits.append(b)
    return " · ".join(bits)
