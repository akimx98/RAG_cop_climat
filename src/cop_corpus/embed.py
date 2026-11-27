"""Embeddings par API (pas de modèle local : le VPS n'a pas la RAM).

Les vecteurs sont stockés en float16 dans SQLite et chargés en mémoire pour une
recherche par produit scalaire. Les documents restreints ne sont pas envoyés à
l'API, sauf si COP_EMBED_RESTRICTED=1.
"""

from __future__ import annotations

import functools
import os
import socket
import threading
import time

import numpy as np

from . import config, db

_lock = threading.Lock()
# Vecteurs en blocs fixes de BLOCK lignes, jamais recopiés : chaque bloc = (matrice float16, identifiants).
# Seules les n premières lignes (tous blocs confondus) sont remplies et lues.
_cache: dict[str, object] = {"blocks": [], "n": 0, "max": 0}


def enabled() -> bool:
    return config.EMBED_PROVIDER == "openai"


def _client():
    from openai import OpenAI

    return OpenAI()


def encode(texts: list[str]) -> np.ndarray:
    client = _client()
    for attempt in range(5):
        try:
            resp = client.embeddings.create(model=config.EMBED_MODEL, input=texts, dimensions=config.EMBED_DIM)
            vecs = np.array([d.embedding for d in resp.data], dtype=np.float32)
            vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
            return vecs
        except Exception:
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


LOCK_KEY = "embed_lock"
LOCK_TTL = 300  # s : un calcul interrompu libère le verrou au bout de 5 min


def _db_lock(conn, owner: str) -> bool:
    """Un seul calcul d'embeddings à la fois (sinon deux processus paient les mêmes passages)."""
    cur = db.meta_get(conn, LOCK_KEY) or ""
    if cur:
        other, _, ts = cur.rpartition("|")
        try:
            fresh = time.time() - float(ts) < LOCK_TTL
        except ValueError:
            fresh = False
        if other != owner and fresh:
            return False
    db.meta_set(conn, LOCK_KEY, f"{owner}|{time.time()}")
    return True


def embed_pending(conn, batch: int = 128, limit: int | None = None, progress=None) -> int:
    if not enabled():
        return 0
    owner = f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"
    if not _db_lock(conn, owner):
        return 0
    try:
        return _embed_pending(conn, owner, batch, limit, progress)
    finally:
        if (db.meta_get(conn, LOCK_KEY) or "").startswith(owner + "|"):
            db.meta_set(conn, LOCK_KEY, "")


def _embed_pending(conn, owner: str, batch: int, limit: int | None, progress) -> int:
    access_clause = "" if config.EMBED_RESTRICTED else "AND d.access='public'"
    # Soumissions CCNUCC : seules les récentes sont vectorisées (le stock entier ajouterait ~190 000 vecteurs en
    # mémoire dans chaque processus) ; les plus anciennes restent cherchables par mots-clés.
    since = config.SUB_EMBED_SINCE
    access_clause += (f" AND NOT (COALESCE(d.source_id,'')='unfccc-submissions' "
                      f"AND COALESCE(d.published,'') < '{since}')")
    done = 0
    while True:
        rows = conn.execute(
            f"""SELECT p.id, p.text, d.title FROM passages p JOIN documents d ON d.id=p.doc_id
                LEFT JOIN vectors v ON v.passage_id=p.id
                WHERE v.passage_id IS NULL {access_clause}
                ORDER BY p.id LIMIT ?""",
            (batch,),
        ).fetchall()
        if not rows:
            break
        texts = [f"{(r['title'] or '')[:150]}\n{r['text'][:6000]}" for r in rows]
        vecs = encode(texts).astype(np.float16)
        conn.executemany(
            "INSERT OR REPLACE INTO vectors(passage_id, vec) VALUES(?,?)",
            [(r["id"], v.tobytes()) for r, v in zip(rows, vecs)],
        )
        conn.commit()
        if not _db_lock(conn, owner):  # verrou repris entre-temps (expiration) : on s'arrête
            break
        done += len(rows)
        if progress and done % (batch * 10) == 0:
            progress(f"embeddings : {done}")
        if limit and done >= limit:
            break
    return done


BLOCK = 8192                # lignes par bloc (8 192 × 512 × 2 octets = 8 Mo) : lecture, stockage et produit


def _refresh(conn) -> None:
    """Ajoute les nouveaux vecteurs, lus par blocs (fetchmany) et écrits en place dans des blocs fixes : le pic reste
    la taille utile plus un bloc, sans copie de la matrice (un fetchall + join + vstack de toute la table montait à
    2,5 fois ; note 2026-11-27-memoire-vecteurs)."""
    with _lock:
        blocks: list = _cache["blocks"]  # type: ignore[assignment]
        n, last = int(_cache["n"]), int(_cache["max"])  # type: ignore[arg-type]
        cur = conn.execute("SELECT passage_id, vec FROM vectors WHERE passage_id>? ORDER BY passage_id", (last,))
        while rows := cur.fetchmany(BLOCK):
            vecs = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float16).reshape(len(rows), config.EMBED_DIM)
            ids = np.fromiter((r["passage_id"] for r in rows), dtype=np.int64, count=len(rows))
            done = 0
            while done < len(rows):
                b, off = divmod(n, BLOCK)
                if b == len(blocks):
                    blocks.append((np.empty((BLOCK, config.EMBED_DIM), dtype=np.float16),
                                   np.empty(BLOCK, dtype=np.int64)))
                take = min(BLOCK - off, len(rows) - done)
                blocks[b][0][off:off + take] = vecs[done:done + take]
                blocks[b][1][off:off + take] = ids[done:done + take]
                done += take
                n += take
            # publié bloc après bloc : les lecteurs ne voient que [:n] (écrit), une erreur plus loin ne perd rien
            _cache["n"], _cache["max"] = n, int(ids[-1])


def _snapshot() -> list[tuple[np.ndarray, np.ndarray]]:
    """Vues des lignes remplies de chaque bloc (sans copie) : un rechargement concurrent écrit au-delà de n."""
    with _lock:
        n = int(_cache["n"])  # type: ignore[arg-type]
        out = []
        for mat, ids in _cache["blocks"]:  # type: ignore[attr-defined]
            k = min(BLOCK, n)
            if k <= 0:
                break
            out.append((mat[:k], ids[:k]))
            n -= k
        return out


SEARCH_CONCURRENCY = 2      # produits vectoriels simultanés dans un processus
_search_slots = threading.BoundedSemaphore(SEARCH_CONCURRENCY)


@functools.lru_cache(maxsize=512)
def _encode_query(query: str) -> np.ndarray:
    """Vecteur d'une requête, gardé en mémoire : pages dossier et outils relancent souvent les mêmes requêtes (un appel
    OpenAI de moins à chaque fois). 512 × 2 Ko ; jamais écrit ailleurs qu'en mémoire du processus."""
    v = encode([query])[0].astype(np.float32)
    v.flags.writeable = False
    return v


def search(conn, query: str, k: int = 100) -> list[tuple[int, float]]:
    if not enabled():
        return []
    _refresh(conn)
    blocks = _snapshot()
    if not blocks:
        return []
    q = _encode_query(query)                    # appel réseau (ou cache) : hors du sémaphore
    # numpy n'a pas de produit matriciel float16 rapide : conversion float32 bloc par bloc (16 Mo). Les pages
    # tournent dans des fils : au plus SEARCH_CONCURRENCY produits à la fois, pour borner la mémoire du site.
    scores = np.empty(sum(len(m) for m, _ in blocks), dtype=np.float32)
    with _search_slots:
        i = 0
        for mat, _ in blocks:
            scores[i:i + len(mat)] = mat.astype(np.float32) @ q
            i += len(mat)
    ids = np.concatenate([b for _, b in blocks])
    k = min(k, len(scores))
    top = np.argpartition(-scores, k - 1)[:k]
    top = top[np.argsort(-scores[top])]
    return [(int(ids[i]), float(scores[i])) for i in top]  # type: ignore[index]
