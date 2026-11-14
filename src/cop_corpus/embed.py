"""Embeddings par API (pas de modèle local : le VPS n'a pas la RAM).

Les vecteurs sont stockés en float16 dans SQLite et chargés en mémoire pour une
recherche par produit scalaire. Les documents restreints ne sont pas envoyés à
l'API, sauf si COP_EMBED_RESTRICTED=1.
"""

from __future__ import annotations

import os
import socket
import threading
import time

import numpy as np

from . import config, db

_lock = threading.Lock()
_cache: dict[str, object] = {"ids": np.zeros(0, dtype=np.int64), "mat": np.zeros((0, config.EMBED_DIM), dtype=np.float16), "max": 0}


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


def _refresh(conn) -> None:
    with _lock:
        last = int(_cache["max"])  # type: ignore[arg-type]
        rows = conn.execute(
            "SELECT passage_id, vec FROM vectors WHERE passage_id>? ORDER BY passage_id", (last,)
        ).fetchall()
        if not rows:
            return
        ids = np.array([r["passage_id"] for r in rows], dtype=np.int64)
        mat = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float16).reshape(len(rows), config.EMBED_DIM)
        _cache["ids"] = np.concatenate([_cache["ids"], ids])  # type: ignore[list-item]
        _cache["mat"] = np.vstack([_cache["mat"], mat])  # type: ignore[list-item]
        _cache["max"] = int(ids[-1])


def search(conn, query: str, k: int = 100) -> list[tuple[int, float]]:
    if not enabled():
        return []
    _refresh(conn)
    mat = _cache["mat"]
    if not len(mat):  # type: ignore[arg-type]
        return []
    q = encode([query])[0].astype(np.float32)
    # numpy n'a pas de produit matriciel float16 rapide : conversion float32 par blocs.
    scores = np.empty(len(mat), dtype=np.float32)  # type: ignore[arg-type]
    step = 32768
    for i in range(0, len(mat), step):  # type: ignore[arg-type]
        scores[i:i + step] = mat[i:i + step].astype(np.float32) @ q  # type: ignore[index]
    k = min(k, len(scores))
    top = np.argpartition(-scores, k - 1)[:k]
    top = top[np.argsort(-scores[top])]
    ids = _cache["ids"]
    return [(int(ids[i]), float(scores[i])) for i in top]  # type: ignore[index]
