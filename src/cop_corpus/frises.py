"""Frises « Au fil des versions » des pages dossier : calculées par le worker (fil `frises`), lues par le site.

Note docs/notes/2026-11-27-phase2-site-sans-fond.md. Le site ne calcule plus aucune frise (sauf `/diff`, à la
demande, par `formulations.grid`) : il lit la table `dossier_frises`, dont ce module est le seul écrivain
(tests/test_un_ecrivain.py). Calcul : 26 s d'un cœur à froid pour les 18 dossiers (mesuré le 27/11), jamais en même
temps qu'une lecture de PDF (`extract._slots` tenu pendant un tour) ; documents découpés libérés à la fin du tour.

Une ligne par (dossier, variante) : `public` (visiteurs, invités), et `restreint` seulement si un document des
lignées du dossier est restreint (sinon les deux variantes seraient identiques). À la lecture, les versions devenues
inaccessibles à l'utilisateur sont retirées sans recalcul (`read`).
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from typing import Any

from . import auth, db, live

FORMAT = 1   # à augmenter quand la forme des frises change : tout est recalculé au tour suivant
MAX_FRISES = 4
_READER = {"id": 0, "can_restricted": 1, "active": 1}   # lecteur fictif de la variante `restreint`


def compute(conn, texts: list[dict], user: dict | None) -> list[dict]:
    """Une frise par texte du dossier qui a au moins deux versions accessibles (crochets, options, notions).
    Reprise telle quelle de l'ancien `web_live._frises` (critère 3 : résultats identiques)."""
    from . import formulations

    out, seen = [], set()
    for t in texts:
        if not t.get("doc_id"):
            continue
        row = conn.execute("SELECT * FROM documents WHERE id=?", (t["doc_id"],)).fetchone()
        if not row or not row["lineage_key"] or row["lineage_key"] in seen:
            continue
        seen.add(row["lineage_key"])
        if row["access"] != "public" and not auth.can_restricted(user):
            continue
        rows = formulations.lineage(conn, row, user)
        if len(rows) >= 2:
            out.append({"titre": rows[-1]["title"], "cote": rows[-1]["symbol"], "grille": formulations.grid(conn, rows)})
    return out[:MAX_FRISES]


_DOC_COLS = ("id, lineage_key, version, title, symbol, access, duplicate_of, status, published, fetched_at, "
             "n_passages")


def state(conn, slug: str) -> tuple[str, bool, list[dict]]:
    """(signature, un document restreint ?, textes du dossier). Signature : empreinte des textes du dossier
    (`live_texts` : ordre et document rattaché) et de tous les documents de leurs lignées (id, version, titre, cote,
    accès, `duplicate_of`, statut, dates, nombre de passages), plus `FORMAT`. Quelques ms."""
    texts = live.texts(conn, slug)
    ids = sorted({t["doc_id"] for t in texts if t.get("doc_id")})
    docs: list[Any] = []
    if ids:
        marks = ",".join("?" * len(ids))
        keys = sorted({r[0] for r in conn.execute(
            f"SELECT lineage_key FROM documents WHERE id IN ({marks}) AND lineage_key IS NOT NULL", ids)})
        lk = ",".join("?" * len(keys))
        docs = conn.execute(f"SELECT {_DOC_COLS} FROM documents WHERE id IN ({marks})"
                            + (f" OR lineage_key IN ({lk})" if keys else "") + " ORDER BY id", [*ids, *keys]).fetchall()
    payload = [FORMAT, [[t["id"], t.get("doc_id"), t.get("published"), t.get("first_seen")] for t in texts],
               [list(r) for r in docs]]
    sig = hashlib.sha256(json.dumps(payload, ensure_ascii=False, default=str).encode()).hexdigest()
    return sig, any(r["access"] != "public" for r in docs), texts


RETRY_AFTER = 1800   # s : un dossier en échec n'est réessayé qu'après 30 min, ou dès que sa signature change
_failed: dict[str, tuple[str | None, float]] = {}   # dossier → (signature, instant) du dernier échec ; mémoire du worker


def _waiting(slug: str, sig: str | None) -> bool:
    hit = _failed.get(slug)
    return bool(hit) and hit[0] == sig and time.monotonic() - hit[1] < RETRY_AFTER


def compute_all(conn, dossiers: list[dict] | None = None, force: bool = False) -> dict[str, int]:
    """Un tour du fil `frises` (worker seulement : WORKER_ONLY). Recalcule les dossiers dont la signature a changé ;
    un dossier en échec n'empêche pas les suivants (signalé une fois ; réessayé après 30 min ou dès que sa signature
    change). Écriture par dossier, en une
    transaction courte. Tient `extract._slots` tout le tour (jamais en même temps qu'une lecture de PDF)."""
    from . import extract, formulations, monitor

    out = {"recalcules": 0, "inchanges": 0, "echecs": 0, "differes": 0}
    with extract._slots:
        try:
            for d in (dossiers if dossiers is not None else live.dossiers()):
                slug = d["slug"]
                sig = None
                try:
                    sig, restricted, texts = state(conn, slug)
                    old = conn.execute("SELECT signature FROM dossier_frises WHERE slug=? AND variant='public'",
                                       (slug,)).fetchone()
                    if old and old[0] == sig and not force:
                        out["inchanges"] += 1
                        continue
                    if _waiting(slug, sig) and not force:   # même échec il y a moins de 30 min : pas de nouvel essai
                        out["differes"] += 1
                        continue
                    t0 = time.perf_counter()
                    values = {"public": compute(conn, texts, None)}
                    if restricted:
                        values["restreint"] = compute(conn, texts, _READER)
                    ms = int((time.perf_counter() - t0) * 1000)
                    now = db.now()
                    with conn:   # transaction courte : rien de lent dedans
                        conn.execute("DELETE FROM dossier_frises WHERE slug=?", (slug,))
                        conn.executemany(
                            "INSERT INTO dossier_frises(slug, variant, value, signature, computed_at, duration_ms) "
                            "VALUES(?,?,?,?,?,?)",
                            [(slug, v, json.dumps(f, ensure_ascii=False), sig, now, ms) for v, f in values.items()])
                    out["recalcules"] += 1
                    _failed.pop(slug, None)
                except Exception as exc:  # noqa: BLE001  (document corrompu, base verrouillée…)
                    if conn.in_transaction:
                        try:
                            conn.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                    out["echecs"] += 1
                    if not _waiting(slug, sig):   # signalé une fois par (dossier, signature) et par 30 min
                        monitor.report(f"worker : frises {slug}", exc, conn=conn)
                        _failed[slug] = (sig, time.monotonic())
        finally:
            # Documents découpés et diffs gardés par formulations (≈ 80 Mo) : libérés hors du tour.
            formulations._CACHE.clear()
            formulations._DIFFS.clear()
    if out["recalcules"] or out["echecs"]:   # (« differes » : silencieux, déjà signalé)
        print(f"[frises] {out}", file=sys.stderr, flush=True)
    return out


_bad_seen: set[tuple[str, str]] = set()   # (dossier, empreinte de la ligne) déjà signalés : un seul rapport par ligne


def read(conn, slug: str, user: dict | None) -> list[dict]:
    """Frises stockées d'un dossier pour cet utilisateur (≈ 2 ms), sans aucun calcul. Les versions qui ne lui sont
    plus accessibles (devenues restreintes, retirées, plus indexées) sont retirées de l'affichage ; liste vide si le
    worker n'a pas encore calculé ce dossier (bloc masqué). Ne lève jamais : une ligne illisible ou d'une autre forme
    (JSON invalide, clé manquante, autre FORMAT) est traitée comme absente et signalée une seule fois."""
    raw = None
    try:
        can = auth.can_restricted(user)
        rows = {r["variant"]: r["value"] for r in conn.execute(
            "SELECT variant, value FROM dossier_frises WHERE slug=?", (slug,))}
        raw = rows.get("restreint") if can and "restreint" in rows else rows.get("public")
        if not raw:
            return []
        return _read(conn, json.loads(raw), can)
    except Exception as exc:  # noqa: BLE001
        from . import monitor

        key = (slug, hashlib.sha256(str(raw).encode()).hexdigest())
        if key not in _bad_seen:
            _bad_seen.add(key)
            monitor.report(f"site : frises illisibles {slug}", exc)
        return []


def _check(stored) -> None:
    """Lève si la valeur n'a pas la forme lue par `_keep` et par le gabarit `_frise.html`."""
    if not isinstance(stored, list):
        raise TypeError("frises : liste attendue")
    for f in stored:
        f["titre"], f["cote"]
        g = f["grille"]
        n = len(g["versions"])
        for v in g["versions"]:
            int(v["doc_id"]), v["version"], v["cote"], v["date"][:10], v["crochets"], v["options"], v["prev"]
        for line in g["notions"]:
            line["cle"], line["fr"], line["en"]
            if len(line["cellules"]) != n:
                raise ValueError("frises : cellules et versions de longueurs différentes")
            for c in line["cellules"]:
                if c["etat"] not in ("absente", "entre crochets", "dans une option", "sans crochets"):
                    raise ValueError(f"frises : état inconnu {c['etat']!r}")
                list(c["unites"])
            for b in line["bascules"]:
                b["doc_id"], b["prev"], b["version"], b["date"], b["de"], b["a"], list(b["paragraphes"])


def _read(conn, stored, can: bool) -> list[dict]:
    _check(stored)
    ids = sorted({v["doc_id"] for f in stored for v in f["grille"]["versions"]})
    docs = {}
    if ids:
        docs = {r["id"]: r for r in conn.execute(
            f"SELECT id, access, status, title, symbol FROM documents WHERE id IN ({','.join('?' * len(ids))})", ids)}
    ok = {i for i, r in docs.items() if r["status"] == "indexed" and (r["access"] == "public" or can)}
    out = []
    for f in stored:
        f = _keep(f, ok, docs)
        if f:
            out.append(f)
    return out


def _keep(f: dict, ok: set[int], docs: dict) -> dict | None:
    """La frise réduite aux versions accessibles ; None s'il en reste moins de deux. Inchangée si tout est accessible.
    Réduite (état transitoire jusqu'au recalcul du worker) : versions renumérotées, lien « précédente » refait,
    bascules touchant une version retirée ôtées, titre et cote repris de la dernière version gardée."""
    g = f["grille"]
    keep = [i for i, v in enumerate(g["versions"]) if v["doc_id"] in ok]
    if len(keep) == len(g["versions"]):
        return f
    if len(keep) < 2:
        return None
    versions, prev = [], None
    for n, i in enumerate(keep):
        v = dict(g["versions"][i], version=n + 1, prev=prev)
        versions.append(v)
        prev = v["doc_id"]
    num = {v["doc_id"]: v["version"] for v in versions}
    notions = []
    for line in g["notions"]:
        cells = [line["cellules"][i] for i in keep]
        if not any(c["etat"] != "absente" for c in cells):
            continue
        switches = [dict(b, version=num[b["doc_id"]]) for b in line["bascules"]
                    if b["doc_id"] in num and b["prev"] in num]
        notions.append(dict(line, cellules=cells, bascules=switches))
    last = docs[versions[-1]["doc_id"]]
    return {"titre": last["title"], "cote": last["symbol"], "grille": {"versions": versions, "notions": notions}}
