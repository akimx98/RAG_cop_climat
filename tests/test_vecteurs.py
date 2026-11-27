"""Chargement des vecteurs par blocs fixes (note docs/notes/2026-11-27-memoire-vecteurs.md) : pic mémoire proche de
la taille utile, chargements successifs identiques à un chargement unique, aucune copie de la matrice, lecture
bornée aux lignes remplies, lignes arrivées pendant la lecture."""

import tracemalloc

import numpy as np
import pytest

from test_core import env  # noqa: F401  (fixture)


def _fill(conn, start, count, dim, seed):
    rng = np.random.default_rng(seed)
    vecs = rng.standard_normal((count, dim)).astype(np.float16)
    conn.executemany("INSERT INTO vectors(passage_id, vec) VALUES(?, ?)",
                     [(start + i, vecs[i].tobytes()) for i in range(count)])
    conn.commit()
    return vecs


@pytest.fixture()
def emb(env):  # noqa: F811
    from cop_corpus import config, embed

    env.execute("PRAGMA foreign_keys=OFF")   # vecteurs sans passages : seul le chargement est testé
    return embed, env, config.EMBED_DIM


def _all(embed):
    blocks = embed._snapshot()
    return np.vstack([m for m, _ in blocks]), np.concatenate([i for _, i in blocks])


def test_pic_memoire_du_premier_chargement(emb):
    embed, conn, dim = emb
    n = 40_000
    _fill(conn, 1, n, dim, 0)
    size = n * dim * 2
    tracemalloc.start()
    embed._refresh(conn)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert embed._cache["n"] == n
    # surcoût constant (un bloc lu : lignes Python + join + un bloc alloué d'avance), indépendant du nombre de
    # vecteurs ; avant : 2,5 × la matrice
    assert peak - size < 32 * 2**20, f"pic {peak / size:.2f} × la matrice"


def test_chargements_successifs_identiques(emb):
    embed, conn, dim = emb
    a = _fill(conn, 1, 5000, dim, 1)
    embed._refresh(conn)
    b = _fill(conn, 10_000, embed.BLOCK + 17, dim, 2)        # chevauche deux blocs
    embed._refresh(conn)
    c = _fill(conn, 30_000, 3, dim, 3)
    embed._refresh(conn)
    mat, ids = _all(embed)
    assert np.array_equal(mat, np.vstack([a, b, c]))
    assert list(ids[:2]) == [1, 2] and ids[5000] == 10_000 and list(ids[-3:]) == [30_000, 30_001, 30_002]


def test_aucune_copie_des_blocs_existants(emb):
    embed, conn, dim = emb
    _fill(conn, 1, 1000, dim, 4)
    embed._refresh(conn)
    first = embed._cache["blocks"][0][0]
    for i in range(100):                                      # 100 vecteurs ajoutés un par un
        _fill(conn, 5000 + i, 1, dim, 10 + i)
        embed._refresh(conn)
    assert embed._cache["blocks"][0][0] is first and len(embed._cache["blocks"]) == 1
    assert embed._cache["n"] == 1100


def test_lignes_arrivees_pendant_la_lecture(emb, monkeypatch):
    """Constat 1 de la revue : des lignes écrites par le worker pendant le chargement ne corrompent rien."""
    embed, conn, dim = emb
    monkeypatch.setattr(embed, "BLOCK", 10)
    a = _fill(conn, 1, 30, dim, 6)
    embed._refresh(conn)
    b = _fill(conn, 31, 25, dim, 7)
    real = conn.execute

    class Conn:   # 25 lignes de plus juste avant la lecture
        def execute(self, sql, params=()):
            if sql.startswith("SELECT passage_id, vec"):
                extra.append(_fill(conn, 56, 25, dim, 8))
            return real(sql, params)

    extra = []
    embed._refresh(Conn())
    mat, ids = _all(embed)
    assert list(ids) == list(range(1, 81))
    assert np.array_equal(mat, np.vstack([a, b, extra[0]]))


def test_recherche_bornee_aux_lignes_remplies(emb, monkeypatch):
    embed, conn, dim = emb
    vecs = _fill(conn, 1, 50, dim, 5)
    embed._refresh(conn)
    embed._cache["blocks"][0][0][50:] = np.float16(60000)   # lignes non remplies : scores énormes si lues
    monkeypatch.setattr(embed, "enabled", lambda: True)
    monkeypatch.setattr(embed, "encode", lambda texts: vecs[7:8].astype(np.float32))
    got = embed.search(conn, "x", k=10)
    assert len(got) == 10 and got[0][0] == 8 and all(pid <= 50 for pid, _ in got)


def test_erreur_en_cours_de_chargement(emb, monkeypatch):
    """Une erreur au milieu : les blocs déjà publiés restent justes, la reprise complète sans doublon."""
    embed, conn, dim = emb
    monkeypatch.setattr(embed, "BLOCK", 10)
    a = _fill(conn, 1, 35, dim, 9)
    real = np.frombuffer
    calls = []

    def flaky(*args, **kw):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("panne")
        return real(*args, **kw)

    monkeypatch.setattr(embed.np, "frombuffer", flaky)
    with pytest.raises(RuntimeError):
        embed._refresh(conn)
    assert embed._cache["n"] == 20
    monkeypatch.setattr(embed.np, "frombuffer", real)
    embed._refresh(conn)
    mat, ids = _all(embed)
    assert list(ids) == list(range(1, 36)) and np.array_equal(mat, a)
