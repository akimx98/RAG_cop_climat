"""Lenteur du site (docs/notes/2026-11-25-lenteur-du-site.md) : aucune page synchrone dans la boucle d'événements,
cache borné et invalidé des parties lourdes de la page dossier, produit vectoriel par blocs."""

import ast
import importlib
import threading
import time
from pathlib import Path

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "cop_corpus"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COP_EMBED_PROVIDER", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    from cop_corpus import config

    importlib.reload(config)
    from cop_corpus import db

    importlib.reload(db)
    db._local.__dict__.clear()
    for mod in ("embed", "auth", "search", "ingest", "web", "web_live"):
        importlib.reload(importlib.import_module(f"cop_corpus.{mod}"))
    from cop_corpus import live, usage

    live._ready.clear()
    usage._ready.clear()
    return db.get()


def _app():
    from starlette.applications import Starlette
    from cop_corpus import web, web_live

    return Starlette(routes=[*web.routes, *web_live.routes])


def _doc(conn, text: str, name: str) -> int:
    from cop_corpus import ingest

    doc_id, _, _ = ingest.ingest_bytes(conn, (text * 20).encode(), url=f"https://x/{name}.txt", filename=f"{name}.txt")
    conn.execute("UPDATE documents SET status='indexed' WHERE id=?", (doc_id,))
    conn.commit()
    return doc_id


# --- critères 1 et 2 : pas de page synchrone dans la boucle ---------------------------------------------------------

def test_page_async_sans_await_interdite():
    """Une page `async def` sans `await` s'exécute dans la boucle : ses requêtes SQLite figent tout le site."""
    bad = []
    for name in ("web.py", "web_live.py"):
        for node in ast.walk(ast.parse((SRC / name).read_text(encoding="utf-8"))):
            if isinstance(node, ast.AsyncFunctionDef) and node.name != "health":   # /healthz : reflète la boucle
                if not any(isinstance(x, ast.Await) for x in ast.walk(node)):
                    bad.append(f"{name}:{node.lineno} {node.name}")
    assert not bad, f"à écrire `def` (Starlette les exécute dans un fil) : {bad}"


def test_decorateurs_gardent_la_page_synchrone(env):
    import inspect

    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from cop_corpus import web

    seen = {}

    def page(request):
        seen["page"] = threading.get_ident()
        return PlainTextResponse("ok")

    async def boucle(request):
        seen["boucle"] = threading.get_ident()
        return PlainTextResponse("ok")

    for deco in (web.public_page, web.login_required, web.admin_required):
        assert not inspect.iscoroutinefunction(deco(page))
    async def apage(request):
        return PlainTextResponse("ok")
    assert inspect.iscoroutinefunction(web.public_page(apage))

    app = Starlette(routes=[Route("/p", web.public_page(page)), Route("/b", boucle)])
    with TestClient(app) as c:
        assert c.get("/p").text == "ok" and c.get("/b").text == "ok"
    assert seen["page"] != seen["boucle"]   # la page a tourné dans un fil du pool, pas dans la boucle


def test_refus_des_decorateurs(env):
    from starlette.testclient import TestClient

    c = TestClient(_app())
    r = c.get("/topics", follow_redirects=False)        # login_required
    assert r.status_code == 303 and "signin=1" in r.headers["location"]
    assert c.get("/admin").status_code == 403          # admin_required


# --- critère 3 : la boucle reste libre pendant un calcul de page dossier ---------------------------------------------

def test_healthz_repond_pendant_un_dossier_lent(env, monkeypatch):
    from starlette.testclient import TestClient
    from cop_corpus import web_live

    started, release = threading.Event(), threading.Event()

    def slow(*a, **k):
        started.set()
        release.wait(10)
        return []

    monkeypatch.setattr(web_live.search, "search", slow)
    with TestClient(_app()) as c:
        res = {}
        t = threading.Thread(target=lambda: res.setdefault("dossier", c.get("/database/negotiations/finance")))
        t.start()
        assert started.wait(5)
        a = time.perf_counter()
        assert c.get("/healthz").status_code == 200
        assert c.get("/connect").status_code == 200
        assert time.perf_counter() - a < 2      # sans attendre la fin du calcul (bloqué jusqu'à `release`)
        release.set()
        t.join(10)
    assert res["dossier"].status_code == 200


# --- critères 4 à 7 : cache des parties lourdes de la page dossier -------------------------------------------------

def test_second_affichage_sans_recalcul(env, monkeypatch):
    from starlette.testclient import TestClient
    from cop_corpus import web_live

    calls = []
    real = web_live.search.search
    monkeypatch.setattr(web_live.search, "search", lambda *a, **k: calls.append(1) or real(*a, **k))
    c = TestClient(_app())
    assert c.get("/database/negotiations/finance").status_code == 200
    assert c.get("/database/negotiations/finance?lang=fr").status_code == 200
    assert len(calls) == 1
    assert c.get("/database/negotiations/adaptation").status_code == 200
    assert len(calls) == 2


def test_document_devenu_restreint_disparait(env):
    from starlette.testclient import TestClient

    conn = env
    doc = _doc(conn, "The new collective quantified goal on climate finance, NCQG, Baku to Belem roadmap. ", "ncqg")
    c = TestClient(_app())
    assert f"/doc/{doc}" in c.get("/database/negotiations/finance").text
    conn.execute("UPDATE documents SET access='restreint' WHERE id=?", (doc,))
    conn.commit()
    assert f"/doc/{doc}" not in c.get("/database/negotiations/finance").text
    conn.execute("UPDATE documents SET access='public' WHERE id=?", (doc,))
    conn.commit()
    assert f"/doc/{doc}" in c.get("/database/negotiations/finance").text
    conn.execute("DELETE FROM passages WHERE doc_id=?", (doc,))
    conn.execute("DELETE FROM documents WHERE id=?", (doc,))
    conn.commit()
    assert f"/doc/{doc}" not in c.get("/database/negotiations/finance").text


def test_nouveau_passage_et_expiration(env, monkeypatch):
    from starlette.testclient import TestClient
    from cop_corpus import web_live

    c = TestClient(_app())
    c.get("/database/negotiations/finance")   # cache rempli, corpus vide
    doc =_doc(env, "The new collective quantified goal on climate finance, NCQG, Baku to Belem roadmap. ", "neuf")
    assert f"/doc/{doc}" in c.get("/database/negotiations/finance").text   # nouveau passage : recalcul

    calls = []
    monkeypatch.setattr(web_live.search, "search", lambda *a, **k: calls.append(1) or [])
    c.get("/database/negotiations/finance")
    assert calls == []
    monkeypatch.setattr(web_live, "DOSSIER_TTL", 0)
    c.get("/database/negotiations/finance")
    assert calls == [1]


def test_cache_borne_aux_dossiers_connus(env):
    from starlette.testclient import TestClient
    from cop_corpus import web_live

    c = TestClient(_app())
    assert c.get("/database/negotiations/inconnu").status_code == 404
    assert not web_live._heavy_cache and not web_live._heavy_locks


def test_un_seul_calcul_a_froid(env, monkeypatch):
    from cop_corpus import db, live, web_live

    calls = []

    def slow(*a, **k):
        calls.append(1)
        time.sleep(0.3)
        return []

    monkeypatch.setattr(web_live.search, "search", slow)
    d = live.dossier("finance")

    def one():
        web_live._heavy(db.get(), "finance", d, [], None)

    threads = [threading.Thread(target=one) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert calls == [1]


# --- critères 8 et 9 : statistiques en cache, produit vectoriel par blocs --------------------------------------------

def test_stats_en_cache(env, monkeypatch):
    from cop_corpus import web

    calls = []
    monkeypatch.setattr(web.search, "stats", lambda conn: calls.append(1) or {"documents": 1})
    assert web._stats(env) == {"documents": 1}
    assert web._stats(env) == {"documents": 1}
    assert calls == [1]


def test_produit_vectoriel_par_blocs(env, monkeypatch):
    from cop_corpus import embed

    rng = np.random.default_rng(0)
    n = embed.BLOCK * 2 + 123
    mat = rng.standard_normal((n, 8)).astype(np.float16)
    q = rng.standard_normal(8).astype(np.float32)
    ids = np.arange(n, dtype=np.int64) + 1000
    monkeypatch.setattr(embed, "enabled", lambda: True)
    monkeypatch.setattr(embed, "_refresh", lambda conn: None)
    monkeypatch.setattr(embed, "encode", lambda texts: q[None, :])
    monkeypatch.setitem(embed._cache, "blocks", [(mat[i:i + embed.BLOCK], ids[i:i + embed.BLOCK])
                                                for i in range(0, n, embed.BLOCK)])
    monkeypatch.setitem(embed._cache, "n", n)
    got = embed.search(None, "x", k=20)
    ref = mat.astype(np.float32) @ q
    top = np.argsort(-ref)[:20]
    assert [pid for pid, _ in got] == [int(i) + 1000 for i in top]
    assert embed._search_slots._value == embed.SEARCH_CONCURRENCY   # sémaphore rendu


def test_produit_vectoriel_deux_a_la_fois_au_plus(env, monkeypatch):
    from cop_corpus import embed

    state = {"now": 0, "peak": 0}
    guard = threading.Lock()

    class SlowMat:   # chaque bloc converti « dure » 50 ms : on compte les produits simultanés
        def __len__(self):
            return embed.BLOCK

        def __getitem__(self, sl):   # vue des lignes remplies (_snapshot)
            return self

        def astype(self, dtype):
            with guard:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.05)
            with guard:
                state["now"] -= 1
            return np.ones((embed.BLOCK, 4), dtype=np.float32)

    monkeypatch.setattr(embed, "enabled", lambda: True)
    monkeypatch.setattr(embed, "_refresh", lambda conn: None)
    monkeypatch.setattr(embed, "encode", lambda texts: np.ones((1, 4), dtype=np.float32))
    monkeypatch.setitem(embed._cache, "blocks", [(SlowMat(), np.arange(embed.BLOCK, dtype=np.int64)) for _ in range(2)])
    monkeypatch.setitem(embed._cache, "n", embed.BLOCK * 2)
    threads = [threading.Thread(target=embed.search, args=(None, "x", 5)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert state["peak"] == embed.SEARCH_CONCURRENCY


# --- critères 13 et 14 (revue, tour 1) ------------------------------------------------------------------------------

def test_lien_de_connexion_une_seule_fois_meme_en_parallele(env):
    from cop_corpus import auth, db

    uid, _ = auth.create_user(env, "lien@example.org", "Lien")
    value = auth.login_link(auth.by_id(env, uid)).split("/entrer/")[1]
    start = threading.Barrier(6)
    ok = []

    def use():
        conn = db.get()   # une connexion par fil, comme les pages
        start.wait()
        if auth.by_login_link(conn, value):
            ok.append(1)

    threads = [threading.Thread(target=use) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert ok == [1]
    assert auth.by_login_link(env, value) is None


def test_echec_jamais_mis_en_cache(env):
    from cop_corpus import web

    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return 42

    with pytest.raises(RuntimeError):
        web._ttl_cached("essai", flaky)
    assert web._ttl_cached("essai", flaky) == 42
    assert web._ttl_cached("essai", flaky) == 42
    assert len(calls) == 2
