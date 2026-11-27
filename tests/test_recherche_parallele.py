"""Recherche : partie sémantique en parallèle du lexical, encodage des requêtes en cache
(note docs/notes/2026-11-27-recherche-parallele.md, #14)."""

import time

import numpy as np
import pytest

from test_core import env  # noqa: F401  (fixture)

TEXTS = [
    "Climate finance goal of 300 billion per year for developing countries. ",
    "Adaptation indicators under the global goal on adaptation and the UAE framework. ",
    "Loss and damage fund replenishment and funding arrangements. ",
    "Just transition work programme and decent work for all. ",
]


@pytest.fixture()
def corpus(env, monkeypatch):  # noqa: F811
    from cop_corpus import config, embed, ingest

    monkeypatch.setattr(config, "EMBED_PROVIDER", "openai")
    calls = []

    def fake_encode(texts):
        calls.append(list(texts))
        rows = []
        for t in texts:
            v = np.zeros(config.EMBED_DIM, dtype=np.float32)
            for i, w in enumerate(("finance", "adaptation", "loss", "transition")):
                v[i] = 1.0 if w in t.lower() else 0.0
            v[10] = 0.1
            rows.append(v / np.linalg.norm(v))
        return np.array(rows, dtype=np.float32)

    monkeypatch.setattr(embed, "encode", fake_encode)
    for i, t in enumerate(TEXTS):
        ingest.ingest_bytes(env, (t * 8).encode(), url=f"https://x/{i}.txt", filename=f"{i}.txt")
    embed.embed_pending(env)
    calls.clear()
    return env, calls


def _sequential(monkeypatch, search):
    """Ancien enchaînement : partie sémantique exécutée en ligne, après le lexical."""
    class Now:
        def __init__(self, fn, *a):
            self.fn, self.a = fn, a

        def result(self):
            return self.fn(*self.a)

    class Pool:
        def submit(self, fn, *a):
            return Now(fn, *a)

    monkeypatch.setattr(search, "_SEM_POOL", Pool())


@pytest.mark.parametrize("q", ["adaptation indicators", "finance for developing countries", "fund", "decent work"])
def test_resultats_identiques(corpus, monkeypatch, q):
    from cop_corpus import search

    conn, _ = corpus
    sig_a: dict = {}
    a = search.search(conn, q, {}, {"can_restricted": 1, "active": 1}, signals=sig_a)
    _sequential(monkeypatch, search)
    sig_b: dict = {}
    b = search.search(conn, q, {}, {"can_restricted": 1, "active": 1}, signals=sig_b)
    assert [h["passage_id"] for h in a] == [h["passage_id"] for h in b] and a
    assert sig_a == sig_b


def test_duree_proche_du_max(corpus, monkeypatch):
    from cop_corpus import embed, search

    conn, _ = corpus
    real_embed, real_tiers = embed.search, search._fts_tiers

    def slow_embed(*a, **k):
        time.sleep(0.5)
        return real_embed(*a, **k)

    def slow_tiers(q):
        time.sleep(0.5)
        return real_tiers(q)

    monkeypatch.setattr(embed, "search", slow_embed)
    monkeypatch.setattr(search, "_fts_tiers", slow_tiers)
    t = time.perf_counter()
    assert search.search(conn, "loss and damage", {}, {"can_restricted": 1, "active": 1})
    assert time.perf_counter() - t < 0.95   # en série : plus de 1,0 s


def test_erreur_semantique_garde_le_lexical(corpus, monkeypatch):
    from cop_corpus import embed, search

    conn, _ = corpus

    def broken(*a, **k):
        raise RuntimeError("API indisponible")

    monkeypatch.setattr(embed, "search", broken)
    hits = search.search(conn, "transition", {}, {"can_restricted": 1, "active": 1})
    assert hits and "transition" in hits[0]["extrait"].lower()


def test_encodage_en_cache(corpus):
    from cop_corpus import embed

    conn, calls = corpus
    embed.search(conn, "finance", k=3)
    embed.search(conn, "finance", k=3)
    embed.search(conn, "adaptation", k=3)
    assert calls == [["finance"], ["adaptation"]]
