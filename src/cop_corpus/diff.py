"""Comparaison de deux versions d'un texte, paragraphe par paragraphe.

Ce qui compte pour suivre une négociation : quels paragraphes apparaissent ou
disparaissent, lesquels changent, et combien de crochets (texte non agréé) et
d'options restent ouverts.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

BRACKET = re.compile(r"\[")
OPTION = re.compile(r"\b(Option|Alt(?:ernative)?)\s*\d+", re.I)


def _paras(conn, doc_id: int) -> list[tuple[str, str]]:
    rows = conn.execute("SELECT para, text FROM passages WHERE doc_id=? ORDER BY seq", (doc_id,)).fetchall()
    merged: dict[str, list[str]] = {}
    order: list[str] = []
    for i, r in enumerate(rows):
        key = r["para"] or f"bloc {i + 1}"
        if key not in merged:
            merged[key] = []
            order.append(key)
        merged[key].append(r["text"])
    return [(k, " ".join(" ".join(merged[k]).split())) for k in order]


def _counts(paras: list[tuple[str, str]]) -> dict[str, int]:
    text = " ".join(t for _, t in paras)
    return {"paragraphes": len(paras), "crochets": len(BRACKET.findall(text)), "options": len(OPTION.findall(text)),
            "caracteres": len(text)}


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


def compare(conn, old_id: int, new_id: int, max_items: int = 40) -> dict[str, Any]:
    a, b = _paras(conn, old_id), _paras(conn, new_id)
    da, db_ = dict(a), dict(b)
    labelled = sum(1 for k, _ in a + b if k.startswith(("§",)) or "§" in k)
    changes: list[dict[str, Any]] = []
    if labelled >= max(4, (len(a) + len(b)) // 3):
        # Alignement par numéro de paragraphe.
        for key, text in b:
            if key not in da:
                changes.append({"paragraphe": key, "changement": "ajouté", "texte": text[:900]})
            elif da[key] != text:
                ratio = difflib.SequenceMatcher(a=da[key], b=text, autojunk=False).ratio()
                changes.append({"paragraphe": key, "changement": "modifié", "similarite": round(ratio, 2),
                                "diff": _inline(da[key], text)})
        for key, text in a:
            if key not in db_:
                changes.append({"paragraphe": key, "changement": "supprimé", "texte": text[:900]})
    else:
        # Pas de numérotation fiable : diff de séquence sur les blocs.
        ta, tb = [t for _, t in a], [t for _, t in b]
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=ta, b=tb, autojunk=False).get_opcodes():
            if op == "equal":
                continue
            if op == "replace" and i2 - i1 == j2 - j1:
                for x, y in zip(ta[i1:i2], tb[j1:j2]):
                    changes.append({"changement": "modifié", "diff": _inline(x, y)})
            else:
                for x in ta[i1:i2]:
                    changes.append({"changement": "supprimé", "texte": x[:900]})
                for y in tb[j1:j2]:
                    changes.append({"changement": "ajouté", "texte": y[:900]})
    ca, cb = _counts(a), _counts(b)
    return {
        "ancienne_version": old_id,
        "nouvelle_version": new_id,
        "avant": ca,
        "apres": cb,
        "evolution": {k: cb[k] - ca[k] for k in ca},
        "nb_changements": len(changes),
        "changements": changes[:max_items],
        "tronque": len(changes) > max_items,
        "legende": "[-texte supprimé-] {+texte ajouté+} ; crochets = texte non agréé",
    }


def previous_in_lineage(conn, doc_id: int) -> int | None:
    """Version précédente indexée du même texte (même lignée), s'il y en a une."""
    doc = conn.execute("SELECT lineage_key FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc or not doc["lineage_key"]:
        return None
    row = conn.execute(
        "SELECT id FROM documents WHERE lineage_key=? AND id<? AND status='indexed' "
        "ORDER BY version DESC, id DESC LIMIT 1",
        (doc["lineage_key"], doc_id),
    ).fetchone()
    return row["id"] if row else None


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
