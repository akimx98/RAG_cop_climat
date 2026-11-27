"""Trajectoire d'une formulation au fil des versions d'un projet de texte : « 1.5 °C », « transition away from
fossil fuels »… sont-ils présents, entre crochets, dans une option, ou absents, version après version ? Où (unité,
page, lien), et à quel moment ils basculent.

Sans IA : notions définies par des motifs (variantes onusiennes en anglais), unités lues par `structure`, crochets
et options repérés dans le texte. Une notion libre (« human rights defenders ») est cherchée telle quelle.
"""

from __future__ import annotations

import re
from typing import Any

from . import auth, structure
from .structure import Unit

# Notion → libellés et motifs (expressions régulières, insensibles à la casse, sur le texte sans crochets).
CONCEPTS: dict[str, dict[str, Any]] = {
    "1.5": {"fr": "1,5 °C", "en": "1.5 °C", "alias": ["1,5", "1.5c", "1.5°c", "1,5°c", "1.5 degrees", "overshoot"],
            "motifs": [r"1[.,]5\s*°?\s*º?\s*C\b", r"1[.,]5\s*degrees?", r"\bovershoot\b"]},
    "fossiles": {"fr": "Énergies fossiles (sortie, réduction)", "en": "Fossil fuels (transition away, phase-out)",
                 "alias": ["fossil fuels", "fossil", "combustibles fossiles", "sortie des fossiles", "taff",
                           "transition away", "phase out", "phase-out"],
                 "motifs": [r"transition(?:ing)?\s+away\s+from\s+fossil", r"fossil[- ]fuels?", r"phas(?:e|ing)[- ]?(?:out|down)",
                            r"\bunabated\b"]},
    "charbon": {"fr": "Charbon", "en": "Coal", "alias": ["coal"], "motifs": [r"\bcoal\b"]},
    "renouvelables": {"fr": "Tripler les renouvelables", "en": "Tripling renewables",
                      "alias": ["renewables", "renewable energy", "tripling", "énergies renouvelables"],
                      "motifs": [r"tripl(?:e|ing)\b[^.;]{0,80}renewable", r"renewable energy capacity"]},
    "efficacite": {"fr": "Doubler l'efficacité énergétique", "en": "Doubling energy efficiency",
                   "alias": ["energy efficiency", "efficacité énergétique"],
                   "motifs": [r"doubl(?:e|ing)\b[^.;]{0,80}energy efficiency", r"energy efficiency improvements"]},
    "methane": {"fr": "Méthane", "en": "Methane", "alias": ["methane", "méthane"], "motifs": [r"\bmethane\b"]},
    "deforestation": {"fr": "Déforestation", "en": "Deforestation", "alias": ["deforestation", "forests", "forêts"],
                      "motifs": [r"\bdeforestation\b", r"forest degradation"]},
    "neutralite": {"fr": "Neutralité carbone / zéro net", "en": "Net zero", "alias": ["net zero", "net-zero",
                                                                                    "neutralité carbone"],
                   "motifs": [r"net[- ]zero", r"carbon neutrality", r"climate neutrality"]},
    "cbdr": {"fr": "Responsabilités communes mais différenciées", "en": "CBDR-RC", "alias": ["cbdr", "cbdr-rc",
                                                                                           "equity", "équité"],
             "motifs": [r"common but differentiated", r"\bCBDR", r"respective capabilities"]},
    "finance": {"fr": "Objectif de financement (NCQG, 1,3 billion)", "en": "Finance goal (NCQG, USD 1.3 trillion)",
                "alias": ["ncqg", "finance", "1.3 trillion", "300 billion", "baku to belém", "financement"],
                "motifs": [r"1[.,]3\s*trillion", r"300\s*billion", r"new collective quantified goal", r"\bNCQG\b",
                           r"Baku\s+to\s+Bel[ée]m"]},
    "dons": {"fr": "Dons, financement concessionnel", "en": "Grants, concessional finance",
             "alias": ["grants", "grant-based", "concessional", "dons"],
             "motifs": [r"grant[- ](?:based|equivalent)", r"\bgrants?\b", r"\bconcessional\b"]},
    "mesures_unilaterales": {"fr": "Mesures commerciales unilatérales", "en": "Unilateral trade measures",
                             "alias": ["unilateral measures", "trade", "cbam", "macf", "mesures unilatérales"],
                             "motifs": [r"unilateral\s+(?:trade[- ]related\s+)?measures", r"trade[- ]restrictive",
                                        r"carbon border"]},
    "droits_humains": {"fr": "Droits humains", "en": "Human rights", "alias": ["human rights", "droits humains",
                                                                              "droits de l'homme", "defenders"],
                       "motifs": [r"human rights", r"rights of Indigenous Peoples", r"rights[- ]based"]},
    "genre": {"fr": "Genre", "en": "Gender", "alias": ["gender", "women", "femmes"],
              "motifs": [r"\bgender", r"\bwomen\b"]},
    "transition_juste": {"fr": "Transition juste", "en": "Just transition", "alias": ["just transition",
                                                                                     "transition juste"],
                         "motifs": [r"just transition"]},
    "pertes_prejudices": {"fr": "Pertes et préjudices", "en": "Loss and damage", "alias": ["loss and damage",
                                                                                          "pertes et préjudices"],
                          "motifs": [r"loss and damage"]},
    "science": {"fr": "Science (GIEC)", "en": "Science (IPCC)", "alias": ["ipcc", "giec", "best available science"],
                "motifs": [r"\bIPCC\b", r"Intergovernmental Panel on Climate Change", r"best available science"]},
}

# Ordre des états : plus haut = plus près d'un texte agréé.
STATES = ("absente", "entre crochets", "dans une option", "sans crochets")
STATES_EN = {"absente": "absent", "entre crochets": "in brackets", "dans une option": "in an option",
             "sans crochets": "without brackets"}

_CACHE: dict[tuple[int, int], structure.Document] = {}
_DIFFS: dict[tuple[int, int], dict[str, Any]] = {}


def _pair_diff(conn, old_id: int, new_id: int) -> dict[str, Any]:
    from . import diff

    key = (old_id, new_id)
    if key not in _DIFFS:
        if len(_DIFFS) > 100:
            _DIFFS.clear()
        _DIFFS[key] = diff.compare(conn, old_id, new_id)
    return _DIFFS[key]


def _window(text: str, patterns: list[re.Pattern], words: int = 70) -> str:
    """Extrait centré sur la notion (texte avec marqueurs de diff), « … » aux coupures."""
    plain = re.sub(r"\[-|-\]|\{\+|\+\}|[\[\]]", "", text)
    m = next((m for p in patterns for m in [p.search(plain)] if m), None)
    if len(text) <= 1500:
        return text
    toks = text.split(" ")
    if not m:
        return " ".join(toks[: 2 * words]) + " …"
    # Position approximative du mot de la notion dans le texte marqué.
    k = len(plain[: m.start()].split(" "))
    a, b = max(0, k - words), min(len(toks), k + words)
    seg = " ".join(toks[a:b])
    # Ne pas laisser un marqueur ouvert.
    for o, c in (("[-", "-]"), ("{+", "+}")):
        if seg.count(o) > seg.count(c):
            seg += c
        if seg.count(c) > seg.count(o):
            seg = o + seg
    return ("… " if a else "") + seg + (" …" if b < len(toks) else "")


def switch_paragraphs(conn, old_id: int, new_id: int, old_units: set[str], new_units: set[str],
                      patterns: list[re.Pattern] | None = None, limit: int = 3) -> list[dict[str, Any]]:
    """Les paragraphes qui montrent une bascule : unités de l'ancienne version où figurait la notion, ou de la
    nouvelle où elle figure, telles que le diff les voit (texte barré / ajouté)."""
    out = []
    for c in _pair_diff(conn, old_id, new_id)["changements"]:
        old = (c.get("ancien") or {}).get("unite")
        new = (c.get("nouveau") or {}).get("unite")
        if old in old_units or new in new_units or c["unite"] in new_units:
            text = c.get("diff") or c.get("texte_ancien") or c.get("texte_nouveau") or ""
            plain = re.sub(r"\[-|-\]|\{\+|\+\}|[\[\]]", "", text)
            if not plain.strip() or (patterns and not any(p.search(plain) for p in patterns)):
                continue   # paragraphe où la notion ne figure pas : pas une preuve de la bascule
            text = _window(text, patterns or []) if len(text) > 1500 else text
            out.append({"unite": c["unite"], "ancien": old, "statut": c["statut"], "texte": text,
                        "genre": "diff" if c.get("diff") else ("supprime" if c.get("texte_ancien") else "ajoute"),
                        "lien": c.get("lien_diff")})
    order = {"supprimé_sans_reprise": 0, "ajouté": 1, "modifié": 2, "repris_de": 3, "repris_dans": 3}
    return sorted(out, key=lambda x: order.get(x["statut"], 4))[:limit]


def resolve(notion: str) -> tuple[str, str, list[re.Pattern]]:
    """(clé, libellé, motifs) d'une notion connue (clé, nom ou alias, FR/EN), sinon de mots libres séparés par des
    virgules (cherchés tels quels)."""
    t = (notion or "").strip().lower()
    for key, c in CONCEPTS.items():
        if t in (key, c["fr"].lower(), c["en"].lower(), *[a.lower() for a in c["alias"]]):
            return key, c["en"], [re.compile(m, re.I) for m in c["motifs"]]
    words = [w.strip() for w in re.split(r"[,;]| OR ", notion or "") if w.strip()]
    return "libre", notion.strip(), [re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, w.split())) + r"(?!\w)", re.I)
                                     for w in words]


def _doc(conn, doc_id: int) -> structure.Document:
    n = conn.execute("SELECT COUNT(*) FROM passages WHERE doc_id=?", (doc_id,)).fetchone()[0]
    key = (doc_id, n)
    doc = _CACHE.get(key)   # une seule lecture : un autre fil peut vider le cache entre un test et une lecture
    if doc is None:
        if len(_CACHE) > 200:
            _CACHE.clear()
        doc = _CACHE[key] = structure.load(conn, doc_id)
    return doc


def _plain(text: str) -> tuple[str, list[int] | None]:
    """Texte sans crochets, et pour chaque caractère sa profondeur de crochets dans le texte d'origine (None : aucun
    crochet, profondeur nulle partout ; évite une liste d'un entier par caractère dans le cache des documents)."""
    if "[" not in text and "]" not in text:
        return text, None
    out, depth, depths = [], 0, []
    for ch in text:
        if ch == "[":
            depth += 1
            continue
        if ch == "]":
            depth = max(0, depth - 1)
            continue
        out.append(ch)
        depths.append(depth)
    return "".join(out), depths


def _in_option(doc: structure.Document, u: Unit) -> Unit | None:
    """L'option (avec au moins une autre option en balance) qui contient l'unité, s'il y en a une."""
    p: Unit | None = u
    while p is not None:
        if p.kind == "option" and p.parent:
            parent = doc.get(p.parent)
            if parent and sum(1 for c in parent.children if (doc.get(c) and doc.get(c).kind == "option")) > 1:
                return p
        p = doc.get(p.parent) if p.parent else None
    return None


def _excerpt(text: str, start: int, end: int, words: int = 18) -> str:
    before = text[:start].split()
    after = text[end:].split()
    left = " ".join(before[-words:])
    right = " ".join(after[:words])
    return (("… " if len(before) > words else "") + left + (" " if left else "") + text[start:end]
            + (" " if right else "") + right + (" …" if len(after) > words else "")).strip()


def occurrences(conn, doc_id: int, patterns: list[re.Pattern]) -> list[dict[str, Any]]:
    """Chaque unité du texte où la notion figure, avec son état (crochets, option) et un extrait."""
    doc = _doc(conn, doc_id)
    # Texte sans crochets de chaque unité, calculé une fois par document (et non une fois par notion : la frise
    # interroge une dizaine de notions sur chaque version ; 13 s mesurées pour un dossier, note 2026-11-27-dossiers).
    cached = doc.__dict__.get("_plains")
    if cached is None:   # (unité, corps, texte sans crochets, profondeurs) ; l'objet unité vit tant que le document est en cache
        plains = [(u, u.body, *_plain(u.body)) for u in doc.units if u.text]
        # publié d'un seul coup : document partagé entre fils (pages, préchauffage, outils MCP)
        cached = doc.__dict__["_plains"] = (plains, "\n".join(p for _u, _b, p, _d in plains))
    plains, alltext = cached
    if not any(p.search(alltext) for p in patterns):
        return []   # notion absente de tout le texte (cas le plus fréquent) : inutile de parcourir les unités
    out = []
    for u, body, plain, depths in plains:
        hits = [m for p in patterns for m in p.finditer(plain)]
        if not hits:
            continue
        bracketed = depths is not None and all(any(depths[i] for i in range(m.start(), m.end())) for m in hits)
        opt = _in_option(doc, u)
        state = "entre crochets" if bracketed else ("dans une option" if opt else "sans crochets")
        m = min(hits, key=lambda m: m.start())
        # Extrait dans le texte d'origine (crochets visibles) : on retrouve la position par le texte d'avant.
        raw_start = _raw_pos(body, m.start())
        raw_end = _raw_pos(body, m.end())
        out.append({"unite": u.uid, "etat": state, "option": opt.uid if opt else None,
                    "extrait": _excerpt(body, raw_start, raw_end), "passage_id": u.passages[0] if u.passages else None,
                    "page": u.pages[0] if u.pages else None, "n": len(hits)})
    return out


def _raw_pos(text: str, plain_pos: int) -> int:
    if "[" not in text and "]" not in text:   # sans crochets, les positions coïncident
        return min(plain_pos, len(text))
    k = 0
    for i, ch in enumerate(text):
        if ch in "[]":
            continue
        if k == plain_pos:
            return i
        k += 1
    return len(text)


def status(occ: list[dict[str, Any]]) -> str:
    return max((o["etat"] for o in occ), key=STATES.index, default="absente")


def lineage(conn, doc_row, user: dict | None) -> list[Any]:
    """Versions accessibles de la lignée du texte, de la plus ancienne à la plus récente."""
    if not doc_row["lineage_key"]:
        return [doc_row]
    access = "" if auth.can_restricted(user) else "AND access='public'"
    rows = conn.execute(f"SELECT * FROM documents WHERE lineage_key=? AND status='indexed' {access} "
                        "ORDER BY COALESCE(version,0), id", (doc_row["lineage_key"],)).fetchall()
    return rows or [doc_row]


def _event(prev: dict[str, Any], cur: dict[str, Any], lang: str) -> str | None:
    T = (lambda fr, en: en if lang == "en" else fr)
    a, b = prev["etat"], cur["etat"]
    ua, ub = {o["unite"] for o in prev["occurrences"]}, {o["unite"] for o in cur["occurrences"]}
    if a == "absente" and b == "absente":
        return None
    if a == "absente":
        return T(f"apparaît ({b}) : {', '.join(sorted(ub))}", f"appears ({STATES_EN[b]}): {', '.join(sorted(ub))}")
    if b == "absente":
        return T(f"disparaît (était {a} : {', '.join(sorted(ua))})",
                 f"disappears (was {STATES_EN[a]}: {', '.join(sorted(ua))})")
    if a != b:
        what = {("entre crochets", "sans crochets"): T("crochets levés", "brackets lifted"),
                ("dans une option", "sans crochets"): T("option retenue", "option retained"),
                ("sans crochets", "entre crochets"): T("mise entre crochets", "put in brackets"),
                ("sans crochets", "dans une option"): T("renvoyée dans une option", "moved into an option")}.get(
            (a, b), T(f"{a} → {b}", f"{STATES_EN[a]} → {STATES_EN[b]}"))
        return what
    if len(ub) < len(ua):
        return T(f"moins d'occurrences ({len(ua)} → {len(ub)} unités)", f"fewer occurrences ({len(ua)} → {len(ub)} units)")
    if len(ub) > len(ua):
        return T(f"plus d'occurrences ({len(ua)} → {len(ub)} unités)", f"more occurrences ({len(ua)} → {len(ub)} units)")
    return None


def timeline(conn, doc_row, notion: str, user: dict | None, lang: str = "fr") -> dict[str, Any]:
    """Trajectoire d'une notion dans toutes les versions accessibles du texte."""
    from . import config, search

    key, label, patterns = resolve(notion)
    if not patterns:
        return {"erreur": "préciser une notion (ex. 1.5, fossil fuels, finance) ou des mots en anglais"}
    versions = []
    rows = lineage(conn, doc_row, user)
    for i, r in enumerate(rows):
        occ = occurrences(conn, r["id"], patterns)
        for o in occ:
            if o["passage_id"]:
                links = search.deep_links(r, o["page"], o["extrait"], o["passage_id"])
                o["lien_source"] = links.get("lien_source") or links.get("lien_page")
                o["lien_passage"] = links["lien_passage"]
        v = {"version": i + 1, "doc_id": r["id"], "cote": r["symbol"], "date": r["published"] or r["fetched_at"][:10],
             "etat": status(occ), "occurrences": occ}
        if i:
            v["lien_diff"] = f"{config.PUBLIC_BASE}/diff/{r['id']}?avec={rows[i - 1]['id']}"
            ev = _event(versions[-1], v, lang)
            if ev:
                v["evenement"] = ev
                v["paragraphes"] = switch_paragraphs(
                    conn, rows[i - 1]["id"], r["id"], {o["unite"] for o in versions[-1]["occurrences"]},
                    {o["unite"] for o in occ}, patterns, limit=2)
        versions.append(v)
    last = versions[-1]
    fr = CONCEPTS[key]["fr"] if key in CONCEPTS else label
    return {"notion": label, "notion_fr": fr, "cle": key, "texte": search.doc_public(rows[-1]),
            "versions": versions,
            "etat_actuel": last["etat"] if lang != "en" else STATES_EN[last["etat"]],
            "bascules": [{"version": v["version"], "date": v["date"], "evenement": v["evenement"],
                          "lien_diff": v.get("lien_diff")} for v in versions if v.get("evenement")],
            "note": ("Projet de texte NON ADOPTÉ. État d'une version = le plus favorable de ses occurrences "
                     "(sans crochets > dans une option > entre crochets > absente) ; une occurrence n'est « entre "
                     "crochets » que si toutes ses mentions dans l'unité le sont. Motifs : variantes anglaises usuelles ; "
                     "« absente » ≠ « le sujet n'est pas traité » (autre formulation possible). Citer l'extrait avec "
                     "sa version, sa date, sa page et son lien.")}


def grid(conn, rows: list[Any], keys: list[str] | None = None) -> dict[str, Any]:
    """Tableau versions × notions (pour la frise) : seulement les notions présentes dans au moins une version."""
    keys = keys or list(CONCEPTS)
    pats = {k: [re.compile(m, re.I) for m in CONCEPTS[k]["motifs"]] for k in keys}
    cols = []
    for i, r in enumerate(rows):
        doc = _doc(conn, r["id"])
        text = " ".join(u.text for u in doc.units)
        cols.append({"version": i + 1, "doc_id": r["id"], "cote": r["symbol"],
                     "date": (r["published"] or r["fetched_at"] or "")[:10], "crochets": text.count("["),
                     "options": sum(1 for u in doc.units if u.kind == "option"),
                     "prev": rows[i - 1]["id"] if i else None})
    lines = []
    for k in keys:
        cells = []
        for r in rows:
            occ = occurrences(conn, r["id"], pats[k])
            cells.append({"etat": status(occ), "unites": [o["unite"] for o in occ]})
        if not any(c["etat"] != "absente" for c in cells):
            continue
        # Bascules : version où l'état change, avec les paragraphes qui le montrent (texte barré / ajouté).
        switches = []
        for i in range(1, len(rows)):
            if cells[i]["etat"] != cells[i - 1]["etat"]:
                switches.append({"version": i + 1, "date": cols[i]["date"], "de": cells[i - 1]["etat"],
                                 "a": cells[i]["etat"], "doc_id": rows[i]["id"], "prev": rows[i - 1]["id"],
                                 "paragraphes": switch_paragraphs(conn, rows[i - 1]["id"], rows[i]["id"],
                                                                  set(cells[i - 1]["unites"]),
                                                                  set(cells[i]["unites"]), pats[k])})
        lines.append({"cle": k, "fr": CONCEPTS[k]["fr"], "en": CONCEPTS[k]["en"], "cellules": cells,
                      "bascules": switches})
    return {"versions": cols, "notions": lines}
