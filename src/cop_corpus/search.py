"""Recherche hybride (FTS5 + vecteurs, fusion RRF), lecture de passages,
documents, versions et nouveautés. Toutes les fonctions respectent l'accès
(un utilisateur sans droit `restreint` ne voit que le public)."""

from __future__ import annotations

import re
import sqlite3
from typing import Any
from urllib.parse import quote

from . import auth, config, db, embed

LEVEL_LABELS = {
    "1": "texte adopté (force juridique)",
    "2": "document officiel non adopté",
    "3": "science / données de référence",
    "4": "compte rendu de référence",
    "5": "position / communiqué d'un acteur",
    "G": "généré par l'outil",
    "R": "document interne (accès restreint)",
}

STOP = set(
    """a au aux avec ce ces dans de des du elle en et eux il je la le les leur lui ma mais me même mes moi mon ne nos
    notre nous on ou par pas pour qu que qui sa se ses son sur ta te tes toi ton tu un une vos votre vous est sont
    quoi quel quelle quels quelles comment pourquoi the of and to in on for is are was were be by with as at from
    that this these those it its an or not what which who how why does do did has have had about into than then
    there their they them can could should would will""".split()
)
FTS_OPS = re.compile(r'"|\bOR\b|\bAND\b|\bNOT\b|\bNEAR\(|\*')
WORD = re.compile(r"[\w][\w'’\-]*", re.UNICODE)


# Synonymes pour la recherche par mots (sigles FR/EN, noms de pays) ; les embeddings couvrent le reste.
_SYN_PAIRS = [
    ("cdn", "ndc"), ("ue", "eu"), ("giec", "ipcc"), ("pnue", "unep"), ("ccnucc", "unfccc"), ("macf", "cbam"),
    ("seqe", "ets"), ("aie", "iea"), ("pma", "ldc"), ("pied", "sids"), ("aosis", "aosis"), ("ogm", "mwp"),
    ("türkiye", "turkey"), ("turquie", "turkey"), ("türkiye", "turquie"),
]
# Sessions : nom usuel ↔ cotes des décisions ↔ ville (les décisions ne disent jamais « COP28 »).
_SESSIONS = {
    "cop21": ["cp 21", "paris"], "cop26": ["cma 3", "cp 26", "glasgow"], "cop27": ["cma 4", "cp 27", "sharm"],
    "cop28": ["cma 5", "cp 28", "dubai"], "cop29": ["cma 6", "cp 29", "baku", "bakou"],
    "cop30": ["cma 7", "cp 30", "belém", "belem"], "cop31": ["cma 8", "cp 31", "antalya"],
}


def _build_synonyms() -> dict[str, set[str]]:
    from .data import ALIASES

    syn: dict[str, set[str]] = {}

    def link(a: str, b: str) -> None:
        if a != b:
            syn.setdefault(a, set()).add(b)
            syn.setdefault(b, set()).add(a)

    for a, b in _SYN_PAIRS:
        link(a, b)
    for cop, alts in _SESSIONS.items():
        syn.setdefault(cop, set()).update(alts)
    for fr, en in ALIASES.items():
        if " " not in fr and "-" not in fr and len(fr) > 3 and "(" not in en:
            link(fr, en.lower())
    return syn


SYNONYMS = _build_synonyms()


def _quote(term: str) -> str:
    return '"' + term.replace('"', "") + '"'


def _term_groups(query: str) -> list[list[str]]:
    groups: list[list[str]] = []
    seen: set[str] = set()
    for w in WORD.findall(query):
        w = w.strip("-'’").lower()
        if len(w) < 2 or w in STOP:
            continue
        for part in re.split(r"[-'’]", w):
            if len(part) >= 2 and part not in STOP and part not in seen:
                seen.add(part)
                groups.append([_quote(part)] + [_quote(x) for x in sorted(SYNONYMS.get(part, ()))])
    return groups


def to_fts(query: str) -> str | None:
    query = query.strip()
    if not query:
        return None
    if FTS_OPS.search(query):
        # Syntaxe FTS5 explicite : on vérifie seulement l'équilibre des guillemets.
        if query.count('"') % 2:
            query = query.replace('"', "")
        return query
    groups = _term_groups(query)
    if not groups:
        return None
    return " OR ".join(dict.fromkeys(t for g in groups for t in g))


LEVEL_PRIOR = {"1": 1.3, "2": 1.1, "R": 1.1, "3": 1.05}


def _fts_tiers(query: str) -> list[tuple[str, str]]:
    """Requêtes FTS5 successives, de la plus stricte à la plus large."""
    base = to_fts(query)
    if not base:
        return []
    if FTS_OPS.search(query.strip()):
        return [("explicite", base)]
    tiers = []
    words = [w.lower() for w in re.findall(r"\w+", query)]
    if 2 <= len(words) <= 14:
        tiers.append(("phrase", '"' + " ".join(words) + '"'))
    groups = _term_groups(query)
    if len(groups) > 1:
        tiers.append(("tous", " AND ".join("(" + " OR ".join(g) + ")" if len(g) > 1 else g[0] for g in groups)))
    tiers.append(("un", base))
    return tiers


def _filters_sql(f: dict[str, Any], user: dict | None) -> tuple[str, list[Any]]:
    clauses, params = [], []
    if not auth.can_restricted(user):
        clauses.append("d.access='public'")
    elif f.get("access") in ("public", "restreint"):
        clauses.append("d.access=?")
        params.append(f["access"])
    if f.get("levels"):
        levels = [str(x) for x in f["levels"]]
        clauses.append(f"d.level IN ({','.join('?' * len(levels))})")
        params += levels
    if f.get("doc_types"):
        clauses.append(f"d.doc_type IN ({','.join('?' * len(f['doc_types']))})")
        params += list(f["doc_types"])
    for key, col in (("session", "d.session"), ("org", "d.org"), ("party", "d.parties"), ("source", "d.source_id")):
        if f.get(key):
            clauses.append(f"{col} LIKE ?")
            params.append(f"%{f[key]}%")
    if f.get("lang"):
        clauses.append("d.lang=?")
        params.append(f["lang"])
    if f.get("since"):
        clauses.append("COALESCE(d.published, d.fetched_at) >= ?")
        params.append(f["since"])
    if f.get("until"):
        clauses.append("COALESCE(d.published, d.fetched_at) <= ?")
        params.append(f["until"])
    if f.get("added_since"):
        clauses.append("d.fetched_at >= ?")
        params.append(f["added_since"])
    if f.get("doc_id"):
        clauses.append("d.id=?")
        params.append(int(f["doc_id"]))
    if f.get("min_doc_id"):
        clauses.append("d.id>=?")
        params.append(int(f["min_doc_id"]))
    return (" AND ".join(clauses) or "1=1"), params


def snippet(text: str, query: str, width: int = 500) -> str:
    """Extrait centré sur l'expression exacte si elle figure, sinon sur la zone la plus dense en mots cherchés."""
    flat = " ".join(text.split())
    low = flat.lower()
    phrase = " ".join(w.lower() for w in WORD.findall(query))
    pos = low.find(phrase) if len(phrase) > 8 else -1
    if pos < 0:
        words = list(dict.fromkeys(w.lower() for w in WORD.findall(query) if len(w) > 3 and w.lower() not in STOP))
        occ = sorted((m.start(), w) for w in words for m in re.finditer(re.escape(w), low))
        best, pos = 0, 0
        span = int(width * 0.6)
        for i, (p0, _) in enumerate(occ):
            n = len({w for p1, w in occ[i:] if p1 - p0 <= span})
            if n > best:
                best, pos = n, p0
    start = max(0, pos - width // 4)
    if start:
        cut = flat.rfind(" ", 0, start)
        start = cut + 1 if cut > start - 40 else start
    out = flat[start : start + width]
    return ("…" if start else "") + out + ("…" if start + width < len(flat) else "")


def doc_public(d: sqlite3.Row | dict) -> dict[str, Any]:
    level = d["level"] or ("R" if d["access"] == "restreint" else None)
    out = {
        "doc_id": d["id"],
        "titre": d["title"],
        "niveau": level,
        "niveau_sens": LEVEL_LABELS.get(str(level), "non classé"),
        "organisation": d["org"],
        "type": d["doc_type"],
        "session": d["session"],
        "cote": d["symbol"],
        "langue": d["lang"],
        "publie_le": d["published"],
        "recupere_le": d["fetched_at"],
        "version": d["version"],
        "acces": d["access"],
        "url_source": d["url"] if d["access"] == "public" else None,
        "lien": f"{config.PUBLIC_BASE}/doc/{d['id']}",
    }
    return {k: v for k, v in out.items() if v is not None}


def _fragment(text: str) -> str:
    """Début du passage en « fragment de texte » (#:~:text=…) : Chrome, Edge et Safari surlignent le paragraphe."""
    words = " ".join(text.split()).split(" ")
    while words and re.fullmatch(r"\(?[0-9A-Za-z]{1,4}[.)]|[•–-]", words[0]):  # « 28. », « (a) », puces
        words = words[1:]
    start = " ".join(words[:7])
    return quote(start, safe="").replace("-", "%2D")


def deep_links(d, page: int | None, text: str, passage_id: int) -> dict[str, str]:
    """Liens directs vers un passage : dans le tableau de bord, dans la copie archivée, dans l'original."""
    mime = (d["mime"] or "").lower()
    raw = (d["raw_path"] or "").lower()
    is_pdf = "pdf" in mime or raw.endswith(".pdf")
    out = {"lien_passage": f"{config.PUBLIC_BASE}/doc/{d['id']}?passage={passage_id}#p{passage_id}"}
    if is_pdf:
        out["lien_page"] = f"{config.PUBLIC_BASE}/fichier/{d['id']}#page={page or 1}"
    url = (d["url"] or "").split("#")[0]
    if d["access"] == "public" and url.startswith("http") and "/api/" not in url and not url.endswith(".json"):
        if is_pdf:
            out["lien_source"] = f"{url}#page={page or 1}"
        elif "html" in mime and text.strip():
            out["lien_source"] = f"{url}#:~:text={_fragment(text)}"
    return out


def _hits(conn, ids: list[int], query: str) -> list[dict[str, Any]]:
    if not ids:
        return []
    rows = conn.execute(
        f"""SELECT p.id AS pid, p.page, p.para, p.text, d.* FROM passages p JOIN documents d ON d.id=p.doc_id
            WHERE p.id IN ({','.join('?' * len(ids))})""",
        ids,
    ).fetchall()
    by_id = {r["pid"]: r for r in rows}
    out = []
    for pid in ids:
        r = by_id.get(pid)
        if not r:
            continue
        hit = doc_public(r)
        hit.update({
            "passage_id": pid,
            "page": r["page"],
            "paragraphe": r["para"],
            "extrait": snippet(r["text"], query),
        })
        hit.update(deep_links(r, r["page"], r["text"], pid))
        out.append(hit)
    return out


def search(conn, query: str, filters: dict[str, Any] | None = None, user: dict | None = None,
           limit: int = 10, per_doc: int = 3, semantic: bool = True) -> list[dict[str, Any]]:
    f = filters or {}
    where, params = _filters_sql(f, user)
    ranked: dict[int, float] = {}
    # Recherche lexicale par paliers : expression exacte, puis tous les mots, puis n'importe lequel.
    # Sans paliers, un OU pur favorise les longs textes qui répètent un mot fréquent.
    seen: set[int] = set()
    rank = 0
    n_groups = len(_term_groups(query))
    for tier, fts in _fts_tiers(query):
        # Expression exacte : le texte qui fait foi d'abord (une citation se vérifie à la source).
        order = ("CASE d.level WHEN '1' THEN 0 WHEN '2' THEN 1 ELSE 2 END, bm25(passages_fts, 1.0, 2.0)"
                 if tier == "phrase" else "bm25(passages_fts, 1.0, 2.0)")
        try:
            rows = conn.execute(
                f"""SELECT p.id FROM passages_fts JOIN passages p ON p.id=passages_fts.rowid
                    JOIN documents d ON d.id=p.doc_id
                    WHERE passages_fts MATCH ? AND {where}
                    ORDER BY {order} LIMIT 200""",
                [fts, *params],
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for r in rows:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            # L'expression exacte compte double : c'est le signal le plus sûr pour vérifier une citation.
            # « N'importe quel mot » d'une question longue est surtout du bruit : poids réduit.
            bonus = 2.0 if tier == "phrase" else 0.5 if (tier == "un" and n_groups > 3) else 1.0
            ranked[r["id"]] = ranked.get(r["id"], 0) + bonus / (60 + rank)
            rank += 1
        if rank >= 200:
            break
    if semantic and embed.enabled():
        try:
            cands = embed.search(conn, query, k=400)
        except Exception as exc:  # recherche sémantique indisponible : on garde le lexical, mais on le dit
            import sys
            print(f"[recherche sémantique] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            cands = []
        if cands:
            cand_ids = [pid for pid, _ in cands]
            ok = {
                r["id"]
                for r in conn.execute(
                    f"""SELECT p.id FROM passages p JOIN documents d ON d.id=p.doc_id
                        WHERE p.id IN ({','.join('?' * len(cand_ids))}) AND {where}""",
                    [*cand_ids, *params],
                )
            }
            rank = 0
            for pid, _score in cands:
                if pid in ok:
                    ranked[pid] = ranked.get(pid, 0) + 1 / (60 + rank)
                    rank += 1
    # À pertinence comparable, priorité au texte qui fait foi (adopté > officiel > reste).
    if ranked:
        ids = list(ranked)
        for i in range(0, len(ids), 900):
            part = ids[i:i + 900]
            for r in conn.execute(
                f"""SELECT p.id, d.level FROM passages p JOIN documents d ON d.id=p.doc_id
                    WHERE p.id IN ({','.join('?' * len(part))})""", part):
                ranked[r["id"]] *= LEVEL_PRIOR.get(str(r["level"]), 1.0)
    order = sorted(ranked, key=ranked.get, reverse=True)
    # Diversité : pas plus de `per_doc` passages par document.
    doc_of = {}
    if order:
        for r in conn.execute(
            f"SELECT id, doc_id FROM passages WHERE id IN ({','.join('?' * len(order[:600]))})", order[:600]
        ):
            doc_of[r["id"]] = r["doc_id"]
    picked, per = [], {}
    for pid in order:
        d = doc_of.get(pid)
        if d is None:
            continue
        if per.get(d, 0) >= per_doc:
            continue
        per[d] = per.get(d, 0) + 1
        picked.append(pid)
        if len(picked) >= limit:
            break
    return _hits(conn, picked, query)


def passage(conn, passage_id: int, user: dict | None, before: int = 1, after: int = 1) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT p.*, d.access FROM passages p JOIN documents d ON d.id=p.doc_id WHERE p.id=?", (passage_id,)
    ).fetchone()
    if not row or (row["access"] != "public" and not auth.can_restricted(user)):
        return None
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (row["doc_id"],)).fetchone()
    ctx = conn.execute(
        "SELECT id, seq, page, para, text FROM passages WHERE doc_id=? AND seq BETWEEN ? AND ? ORDER BY seq",
        (row["doc_id"], row["seq"] - before, row["seq"] + after),
    ).fetchall()
    out = doc_public(doc)
    out["passages"] = [
        {"passage_id": c["id"], "page": c["page"], "paragraphe": c["para"], "texte": c["text"],
         "cible": c["id"] == passage_id}
        for c in ctx
    ]
    out.update(deep_links(doc, row["page"], row["text"], passage_id))
    return out


def document(conn, doc_id: int, user: dict | None) -> dict[str, Any] | None:
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc or (doc["access"] != "public" and not auth.can_restricted(user)):
        return None
    out = doc_public(doc)
    out["nb_pages"] = doc["n_pages"]
    out["nb_passages"] = doc["n_passages"]
    out["note"] = doc["note"]
    out["versions"] = versions(conn, doc["lineage_key"], user) if doc["lineage_key"] else []
    return out


def versions(conn, lineage_key: str, user: dict | None) -> list[dict[str, Any]]:
    access = "" if auth.can_restricted(user) else "AND access='public'"
    rows = conn.execute(
        f"SELECT * FROM documents WHERE lineage_key=? {access} ORDER BY id", (lineage_key,)
    ).fetchall()
    return [
        {"doc_id": r["id"], "version": i + 1, "titre": r["title"], "recupere_le": r["fetched_at"], "url_source": r["url"]}
        for i, r in enumerate(rows)
    ]


def latest(conn, user: dict | None, filters: dict[str, Any] | None = None, limit: int = 30) -> list[dict[str, Any]]:
    where, params = _filters_sql(filters or {}, user)
    rows = conn.execute(
        f"""SELECT d.* FROM documents d WHERE {where} AND d.status IN ('indexed','data','empty')
            ORDER BY d.id DESC LIMIT ?""",
        [*params, limit],
    ).fetchall()
    return [doc_public(r) for r in rows]


def find_doc(conn, ref: str, user: dict | None) -> sqlite3.Row | None:
    """Retrouve un document par id, cote (symbole) ou URL."""
    ref = ref.strip()
    access = "" if auth.can_restricted(user) else "AND access='public'"
    if ref.isdigit():
        return conn.execute(f"SELECT * FROM documents WHERE id=? {access}", (int(ref),)).fetchone()
    return conn.execute(
        f"""SELECT * FROM documents WHERE (symbol=? OR url=? OR symbol LIKE ?) {access}
            ORDER BY id DESC LIMIT 1""",
        (ref, ref, f"%{ref}%"),
    ).fetchone()


def stats(conn) -> dict[str, Any]:
    q = lambda sql: [dict(r) for r in conn.execute(sql)]  # noqa: E731
    return {
        "documents": conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
        "passages": conn.execute("SELECT COUNT(*) FROM passages").fetchone()[0],
        "vecteurs": conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0],
        "par_statut": q("SELECT status, COUNT(*) n FROM documents GROUP BY status"),
        "par_niveau": q("SELECT level, access, COUNT(*) n FROM documents GROUP BY level, access"),
        "par_type": q("SELECT doc_type, COUNT(*) n FROM documents GROUP BY doc_type ORDER BY n DESC"),
        "dernier_ajout": conn.execute("SELECT MAX(fetched_at) FROM documents").fetchone()[0],
    }
