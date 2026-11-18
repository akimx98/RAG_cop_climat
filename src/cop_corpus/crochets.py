"""Crochets par thème dans un projet de texte : quelles formulations sur un thème (genre, défenseurs des droits,
désinformation…) restent entre crochets, et depuis quelle version de la lignée.

Sans IA : crochets lus dans le texte indexé (passages), versions de la même lignée (ingest.lineage_key), thèmes
traduits en termes onusiens anglais (table THEMES, ou mots fournis par l'utilisateur).
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from . import auth, config, diff, search

# Thème → termes cherchés DANS le crochet (anglais onusien, minuscules ; préfixe accepté).
THEMES: dict[str, dict[str, Any]] = {
    "genre": {"alias": ["gender", "femmes", "women", "égalité des sexes"],
              "termes": ["gender", "women", "girls", "gender-responsive", "gender equality", "gender-just",
                         "lgbt", "sexual orientation", "intersectional"]},
    "droits humains": {"alias": ["droits", "human rights", "droits de l'homme", "défenseurs", "defenders",
                                 "human rights defenders", "défenseurs des droits"],
                       "termes": ["human rights", "human rights defenders", "environmental defenders",
                                  "environmental human rights defenders", "defenders", "civic space",
                                  "right to a clean, healthy and sustainable environment",
                                  "rights of indigenous peoples", "rights-based"]},
    "désinformation": {"alias": ["desinformation", "disinformation", "misinformation", "information integrity",
                                 "intégrité de l'information"],
                       "termes": ["disinformation", "misinformation", "information integrity", "integrity of information",
                                  "climate information", "access to information"]},
    "peuples autochtones": {"alias": ["autochtones", "indigenous", "indigenous peoples", "lcipp"],
                            "termes": ["indigenous peoples", "local communities", "traditional knowledge", "lcipp"]},
    "jeunesse": {"alias": ["jeunes", "youth", "children", "enfants", "intergenerational"],
                 "termes": ["youth", "children", "young people", "intergenerational", "future generations"]},
    "transition juste": {"alias": ["just transition", "travailleurs", "workers"],
                         "termes": ["just transition", "workers", "decent work", "labour", "quality jobs"]},
    "fossiles": {"alias": ["fossil fuels", "combustibles fossiles", "transition away", "sortie des fossiles"],
                 "termes": ["fossil fuel", "transitioning away", "transition away", "coal", "oil and gas",
                            "phase-out", "phase out", "phase-down", "phase down", "unabated"]},
    "finance": {"alias": ["financement", "climate finance"],
                "termes": ["finance", "financial resources", "means of implementation", "article 9", "public funds",
                           "grant", "developed country parties shall"]},
    "mesures unilatérales": {"alias": ["commerce", "trade", "cbam", "macf", "unilateral measures"],
                             "termes": ["unilateral", "trade measures", "trade-restrictive", "carbon border", "cbam"]},
}


def theme_terms(theme: str) -> tuple[str, list[str]]:
    """(libellé, termes) : thème connu (nom ou alias, FR/EN), sinon les mots donnés, séparés par des virgules."""
    t = (theme or "").strip().lower()
    for name, spec in THEMES.items():
        if t == name or t in [a.lower() for a in spec["alias"]]:
            return name, spec["termes"]
    terms = [x.strip().lower() for x in re.split(r"[,;]| OR ", theme or "") if x.strip()]
    return theme, terms


def bracket_spans(text: str) -> list[str]:
    """Contenu des crochets de premier niveau (crochets imbriqués inclus dans le contenu ; crochet non fermé
    ignoré). Les renvois de notes « [1] » et les blancs « [ ] » ne comptent pas."""
    out, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch == "[":
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == "]" and depth:
            depth -= 1
            if depth == 0:
                inner = text[start:i].strip()
                if inner and not re.fullmatch(r"[\d\s,.*–-]*|x+|\.\.\.|…", inner):
                    out.append(inner)
    return out


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[\[\]]", " ", s.lower()).split()).strip(" .,;:")


def _state(span: str, version: dict[str, Any]) -> str:
    """État d'une formulation dans une version : « entre crochets », « sans crochets » ou « absent »."""
    n = _norm(span)
    if n in version["spans"] or any(difflib.SequenceMatcher(a=n, b=s, autojunk=False).ratio() >= 0.85
                                    for s in version["spans"] if abs(len(s) - len(n)) < max(30, len(n) // 4)):
        return "entre crochets"
    return "sans crochets" if n in version["text"] else "absent"


def _version(conn, row) -> dict[str, Any]:
    paras = diff._paras(conn, row["id"])
    text = " ".join(t for _, t in paras)
    return {"doc_id": row["id"], "date": row["published"] or (row["fetched_at"] or "")[:10], "titre": row["title"],
            "paras": paras, "spans": {_norm(s) for s in bracket_spans(text)}, "text": _norm(text)}


def by_theme(conn, doc_row, theme: str, user: dict | None, limit: int = 40) -> dict[str, Any]:
    label, terms = theme_terms(theme)
    if not terms:
        return {"erreur": "préciser un thème (ex. genre, droits humains, désinformation) ou des mots séparés par des virgules"}
    access = "" if auth.can_restricted(user) else "AND access='public'"
    rows = conn.execute(f"SELECT * FROM documents WHERE lineage_key=? {access} ORDER BY COALESCE(version,0), id",
                        (doc_row["lineage_key"],)).fetchall() if doc_row["lineage_key"] else [doc_row]
    cur_rank = (doc_row["version"] or 0, doc_row["id"])
    rows = [r for r in rows if (r["version"] or 0, r["id"]) <= cur_rank] or [doc_row]
    versions = [_version(conn, r) for r in rows]
    cur = versions[-1]
    found = []
    for para, text in cur["paras"]:
        for span in bracket_spans(text):
            low = span.lower()
            hit = next((t for t in terms if re.search(rf"(?<![\w-]){re.escape(t)}", low)), None)
            if not hit:
                continue
            states = [_state(span, v) for v in versions]
            since = len(states) - 1
            while since > 0 and states[since - 1] == "entre crochets":
                since -= 1
            first = next(i for i, st in enumerate(states) if st != "absent") if any(st != "absent" for st in states) \
                else len(states) - 1
            pos = text.find(span)
            ctx = text[max(0, pos - 200): pos + len(span) + 200] if pos >= 0 else text[:500]
            found.append({
                "paragraphe": para, "crochet": span[:600], "terme": hit,
                "contexte": ("…" if pos > 200 else "") + ctx.strip() + "…",
                "entre_crochets_depuis": {"version": since + 1, "doc_id": versions[since]["doc_id"],
                                          "date": versions[since]["date"]},
                "apparu_en_version": first + 1,
                "historique": [{"version": i + 1, "etat": st} for i, st in enumerate(states)] if len(states) > 1 else None,
                "lien": f"{config.PUBLIC_BASE}/doc/{cur['doc_id']}",
            })
            if len(found) >= limit:
                break
    from . import live

    paras = cur["paras"]
    out = {"document": search.doc_public(doc_row), "theme": label, "termes": terms,
           "versions_connues": [{"version": i + 1, "doc_id": v["doc_id"], "date": v["date"]} for i, v in enumerate(versions)],
           "texte_entier": {"paires_de_crochets": sum(len(bracket_spans(t)) for _, t in paras),
                            "paragraphes": len(paras),
                            "paragraphes_entierement_entre_crochets": sum(1 for _, t in paras if diff.fully_bracketed(t))},
           "nb": len(found), "crochets": found}
    hint = live.compilation_hint(conn, doc_row["id"])
    if hint:
        out["compilation"] = hint
    return {**out,
            "note": ("Projet de texte NON ADOPTÉ : donner sa version et sa date. Crochets = formulation non agréée. "
                     "« entre_crochets_depuis » : première version d'une suite ininterrompue de versions où la formulation "
                     "est entre crochets (rapprochement à 85 % de similarité près). Rien trouvé ≠ thème absent : le "
                     "texte peut l'aborder sans crochets (voir rechercher)." if found else
                     "Aucune formulation entre crochets sur ce thème dans cette version (le thème peut figurer sans "
                     "crochets, ou avec d'autres mots : essayer des termes précis séparés par des virgules)."),
            "definition": "crochets = formulation non agréée ; paires_de_crochets = crochets de premier niveau (un "
                          "crochet imbriqué compte dans son parent) ; paragraphes_entierement_entre_crochets = "
                          "paragraphes contestés en bloc (une annexe entière entre crochets gonfle le nombre de paires)"}
