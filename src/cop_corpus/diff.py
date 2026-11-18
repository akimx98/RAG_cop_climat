"""Comparaison de deux versions d'un texte, paragraphe par paragraphe.

Ce qui compte pour suivre une négociation : quels paragraphes apparaissent ou
disparaissent, lesquels changent, et combien de crochets (texte non agréé) et
d'options restent ouverts.

Alignement par CONTENU, pas par numéro : quand un paragraphe est inséré, l'ancien §5 devient le §6 ; le comparer au
nouveau §5 donnerait un faux « modifié » à 20 % de similarité. Les paragraphes identiques sont appariés d'abord (où
qu'ils soient), puis les plus semblables (≥ 50 % de mots en commun, dans l'ordre). Un paragraphe dont seuls les
nombres changent (notes de bas de page « -/CP.317 » → « -/CP.316 », renvois « paragraph 5 above » → « 6 ») n'est
pas un changement de fond : il est compté à part (`renumerotes`), sans diff.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

BRACKET = re.compile(r"\[")
OPTION = re.compile(r"\b(Option|Alt(?:ernative)?)\s*\d+", re.I)
_LEAD = re.compile(r"^\s*(?:\(?[0-9]{1,3}[.)]|\([a-z]{1,4}\)|[a-z]\))\s+", re.I)  # « 5. », « (a) », « b) »
_NUM = re.compile(r"\d+")
_WORD = re.compile(r"\w+")
MATCH_MIN = 0.5


def _paras(conn, doc_id: int) -> list[tuple[str, str]]:
    """(paragraphe, texte) dans l'ordre du document. Un même numéro qui revient plus loin (annexe numérotée à
    nouveau depuis 1) reçoit un suffixe : « §1 (2) », pour ne jamais fusionner deux paragraphes distincts."""
    rows = conn.execute("SELECT para, text FROM passages WHERE doc_id=? ORDER BY seq", (doc_id,)).fetchall()
    merged: dict[str, list[str]] = {}
    order: list[str] = []
    runs: dict[str, int] = {}
    last = None
    for i, r in enumerate(rows):
        base = r["para"] or f"bloc {i + 1}"
        if base != last:
            runs[base] = runs.get(base, 0) + 1
        last = base
        key = base if runs[base] == 1 else f"{base} ({runs[base]})"
        if key not in merged:
            merged[key] = []
            order.append(key)
        merged[key].append(r["text"])
    return [(k, " ".join(" ".join(merged[k]).split())) for k in order]


def _body(text: str) -> str:
    return _LEAD.sub("", text).strip()


def fully_bracketed(text: str) -> bool:
    """Paragraphe entièrement entre crochets (« [5. Decides to …] », « 5. [Decides to …] ») : tout le paragraphe est
    contesté. Compte plus parlant que le nombre de « [ » quand une annexe entière est entre crochets."""
    b = _body(text).rstrip(" .;,")
    if not b.startswith("["):
        return False
    depth = 0
    for i, ch in enumerate(b):
        if ch == "[":
            depth += 1
        elif ch == "]" and depth:
            depth -= 1
            if depth == 0:
                return i >= len(b) - 1  # le premier crochet ouvert en tête se ferme à la fin
    return True  # ouvert en tête et pas refermé dans ce paragraphe


def _counts(paras: list[tuple[str, str]]) -> dict[str, int]:
    text = " ".join(t for _, t in paras)
    return {"paragraphes": len(paras), "crochets": len(BRACKET.findall(text)), "options": len(OPTION.findall(text)),
            "paragraphes_entre_crochets": sum(1 for _, t in paras if fully_bracketed(t)), "caracteres": len(text)}


def _inline(a: str, b: str, limit: int = 900) -> str:
    """Diff mot à mot lisible : [-supprimé-] {+ajouté+}."""
    aw, bw = a.split(), b.split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=aw, b=bw, autojunk=False).get_opcodes():
        if op == "equal":
            seg = aw[i1:i2]
            out.append(" ".join(seg) if len(seg) <= 12 else " ".join(seg[:5]) + " … " + " ".join(seg[-5:]))
        if op in ("delete", "replace"):
            out.append("[-" + " ".join(aw[i1:i2]) + "-]")
        if op in ("insert", "replace"):
            out.append("{+" + " ".join(bw[j1:j2]) + "+}")
    s = " ".join(out)
    return s if len(s) <= limit else s[:limit] + " …"


def _align(ta: list[str], tb: list[str]) -> tuple[dict[int, int], dict[int, float]]:
    """Appariement ancien → nouveau par contenu. Renvoie ({j nouveau: i ancien}, {j: similarité})."""
    pair: dict[int, int] = {}
    sim: dict[int, float] = {}
    used: set[int] = set()
    # 1. Textes identiques (hors numérotation de tête), puis identiques aux nombres près.
    for norm in (lambda t: " ".join(_body(t).lower().split()), lambda t: _NUM.sub("#", " ".join(_body(t).lower().split()))):
        index: dict[str, list[int]] = {}
        for i, t in enumerate(ta):
            if i not in used:
                index.setdefault(norm(t), []).append(i)
        for j, t in enumerate(tb):
            if j in pair:
                continue
            cands = [i for i in index.get(norm(t), []) if i not in used]
            if cands:
                i = min(cands, key=lambda i: abs(i - j))
                pair[j], sim[j] = i, 1.0
                used.add(i)
    # 2. Paragraphes semblables : mots communs (Jaccard) pour filtrer, puis similarité mot à mot.
    wa = [_WORD.findall(_body(t).lower()) for t in ta]
    wb = [_WORD.findall(_body(t).lower()) for t in tb]
    sa, sb = [set(w) for w in wa], [set(w) for w in wb]
    cands = []
    for j in range(len(tb)):
        if j in pair or not sb[j]:
            continue
        for i in range(len(ta)):
            if i in used or not sa[i]:
                continue
            inter = len(sa[i] & sb[j])
            if inter / len(sa[i] | sb[j]) < 0.3:
                continue
            r = difflib.SequenceMatcher(a=wa[i], b=wb[j], autojunk=False).ratio()
            if r >= MATCH_MIN:
                rel = abs(i / max(len(ta), 1) - j / max(len(tb), 1))
                cands.append((r - 0.1 * rel, r, i, j))
    for _score, r, i, j in sorted(cands, reverse=True):
        if j in pair or i in used:
            continue
        pair[j], sim[j] = i, r
        used.add(i)
    return pair, sim


def compare(conn, old_id: int, new_id: int, max_items: int = 40) -> dict[str, Any]:
    a, b = _paras(conn, old_id), _paras(conn, new_id)
    ta, tb = [t for _, t in a], [t for _, t in b]
    pair, sim = _align(ta, tb)
    matched_old = set(pair.values())
    changes: list[dict[str, Any]] = []
    renumbered: list[str] = []
    moved = 0
    emitted: set[int] = set()

    def deleted_before(limit: int) -> None:
        for i in range(limit):
            if i not in matched_old and i not in emitted:
                emitted.add(i)
                changes.append({"paragraphe": a[i][0], "changement": "supprimé", "texte": ta[i][:900]})

    for j, (key, text) in enumerate(b):
        if j not in pair:
            changes.append({"paragraphe": key, "changement": "ajouté", "texte": text[:900]})
            continue
        i = pair[j]
        deleted_before(i)
        old_key, old_text = a[i]
        if " ".join(_body(old_text).split()) == " ".join(_body(text).split()):
            moved += old_key != key
            continue
        if _NUM.sub("#", _body(old_text).lower()) == _NUM.sub("#", _body(text).lower()):
            renumbered.append(key)  # seuls des nombres changent : notes, renvois, numérotation
            continue
        item = {"paragraphe": key, "changement": "modifié", "similarite": round(sim[j], 2),
                "diff": _inline(_body(old_text), _body(text))}
        if old_key != key:
            item["ancien_paragraphe"] = old_key
        changes.append(item)
    deleted_before(len(a))
    ca, cb = _counts(a), _counts(b)
    out = {
        "ancienne_version": old_id,
        "nouvelle_version": new_id,
        "avant": ca,
        "apres": cb,
        "evolution": {k: cb[k] - ca[k] for k in ca},
        "nb_changements": len(changes),
        "changements": changes[:max_items],
        "tronque": len(changes) > max_items,
        "legende": ("[-texte supprimé-] {+texte ajouté+} ; crochets = texte non agréé ; paragraphes appariés par "
                    "contenu (ancien_paragraphe : numéro dans l'ancienne version quand il a changé)"),
    }
    if renumbered:
        out["renumerotes"] = {"nb": len(renumbered), "paragraphes": renumbered[:30],
                              "note": "seuls des nombres changent (notes de bas de page, renvois) : non comptés"}
    if moved:
        out["deplaces_sans_changement"] = moved
    return out


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
    return compare(conn, prev, doc_id, max_items=max_items) if prev else None


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
        # moins de crochets/options = convergence
        tag = (", resolved" if en else ", levés") if delta < 0 else (", new" if en else ", nouveaux")
        return f"{label} {old}→{new} ({delta:+d}{tag})"

    bits = [f"{d['nb_changements']} {'§ changed' if en else '§ modifiés'}"]
    for b in (part("brackets" if en else "crochets", av["crochets"], ap["crochets"]),
              part("options", av["options"], ap["options"])):
        if b:
            bits.append(b)
    return " · ".join(bits)
