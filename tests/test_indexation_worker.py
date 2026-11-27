"""Phase 1, lot C (note 2026-11-27-phase1-un-ecrivain) : le site n'indexe plus rien, le fil `indexation` du worker
indexe tout (dépôts et textes de session d'abord, toutes les 5 s) ; lignée `neg:` et version de live.tidy_texts
jamais écrasées par une indexation concurrente."""

import ast
import asyncio
import importlib
import inspect
import threading
import time
from email.message import EmailMessage
from pathlib import Path

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
    for mod in ("embed", "auth", "search", "ingest", "alerts", "diff", "telegram", "mailin"):
        importlib.reload(importlib.import_module(f"cop_corpus.{mod}"))
    from cop_corpus import live

    live._ready.clear()
    return db.get()


HTML = ("<html><head><title>Statement by the Presidency</title></head><body>"
        + "<p>Parties discussed the cover decision and adaptation finance in informal consultations.</p>" * 6
        + "</body></html>").encode()


def _passages(conn, doc_id):
    return conn.execute("SELECT COUNT(*) FROM passages WHERE doc_id=?", (doc_id,)).fetchone()[0]


def _status(conn, doc_id):
    return conn.execute("SELECT status FROM documents WHERE id=?", (doc_id,)).fetchone()[0]


# --- critère 9 : le site enregistre 'pending', tous formats ; le worker indexe ------------------------------------------

def test_site_n_indexe_aucun_format(env, monkeypatch):
    """Processus du site (ingest.DEFER, posé par server.run) : HTML et texte restent 'pending', sans passage ;
    le fil d'indexation du worker les indexe ensuite."""
    from cop_corpus import ingest

    monkeypatch.setattr(ingest, "DEFER", True)
    h, new, st = ingest.ingest_bytes(env, HTML, url="https://x.test/a.html", mime="text/html", source_id="depot-mcp")
    t, _, st2 = ingest.ingest_bytes(env, ("Note " * 80).encode(), url=None, mime="text/plain", filename="n.txt",
                                    source_id="veille-mail")
    assert new and st == st2 == "pending"
    assert _passages(env, h) == _passages(env, t) == 0 and _status(env, h) == _status(env, t) == "pending"
    assert ingest.index_document(env, h) == "pending" and _passages(env, h) == 0
    monkeypatch.setattr(ingest, "DEFER", False)   # le worker
    assert ingest.reindex_pending(env) == {"indexed": 2}
    assert _passages(env, h) > 0 and _passages(env, t) > 0


def test_register_bytes_n_indexe_pas(env):
    """Chemins du site (dépôt MCP, dépôt d'un texte de session, page Courrier) : enregistrement seul, même sans
    DEFER ; un doublon rend le statut du document existant."""
    from cop_corpus import ingest, mailin

    d, new, st = ingest.register_bytes(env, HTML, url="https://x.test/b.html", mime="text/html", source_id="depot-mcp")
    assert new and st == "pending" and _passages(env, d) == 0
    m = EmailMessage()
    m["From"], m["Date"] = "Earth Negotiations Bulletin <enb@iisd.org>", "Mon, 16 Nov 2026 20:00:00 +0000"
    m["Subject"] = "Daily report"
    m.set_content("Parties discussed adaptation finance and the cover decision. " * 12)
    ids = mailin.ingest_newsletter(env, m, "x.eml", index=False)
    assert ids and all(_status(env, i) == "pending" and _passages(env, i) == 0 for i in ids)
    assert ingest.reindex_pending(env) == {"indexed": 2}
    assert ingest.register_bytes(env, HTML, url="https://x.test/b.html", mime="text/html") == (d, False, "indexed")


def _site_calls(func):
    """Appels `module.func(...)` dans les modules du site."""
    out = []
    for name in ("web", "web_live", "mcp_server", "server"):
        tree = ast.parse((SRC / f"{name}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == func:
                out.append((name, node))
    return out


def test_page_courrier_enregistre_sans_indexer():
    calls = _site_calls("ingest_newsletter")
    assert calls, "la page Courrier appelle mailin.ingest_newsletter"
    for name, node in calls:
        kw = {k.arg: k.value for k in node.keywords}
        assert isinstance(kw.get("index"), ast.Constant) and kw["index"].value is False, f"{name}.py:{node.lineno}"
    assert {n for n, _ in _site_calls("register_bytes")} == {"web_live", "mcp_server"}


def test_server_pose_defer():
    src = (SRC / "server.py").read_text(encoding="utf-8")
    assert "ingest.DEFER = True" in src


def test_fil_indexation_toutes_les_5_s():
    from cop_corpus import worker

    assert inspect.signature(worker.indexing_forever).parameters["interval"].default == 5


def test_depots_et_textes_de_session_d_abord(env, monkeypatch):
    """Ordre : dépôts (`depot-*`) et `live-texts`, puis par identifiant ; un dépôt arrivé pendant le passage passe
    devant l'arriéré."""
    from cop_corpus import ingest

    ids = {}
    for i, src in enumerate(("veille-a", "depot-mcp", "veille-b", "live-texts", "depot-mail")):
        ids[src], _ = ingest.register(env, f"doc {i}".encode() * 50, url=f"https://x.test/{i}.txt",
                                      mime="text/plain", source_id=src)
    order = []

    def fake_index(conn, doc_id):
        order.append(doc_id)
        if len(order) == 1:   # dépôt arrivé pendant l'indexation du premier document
            ids["depot-tard"], _ = ingest.register(env, b"tard" * 50, url="https://x.test/t.txt", mime="text/plain",
                                                   source_id="depot-telegram")
        conn.execute("UPDATE documents SET status='indexed' WHERE id=?", (doc_id,))
        conn.commit()
        return "indexed"

    monkeypatch.setattr(ingest, "index_document", fake_index)
    monkeypatch.setattr(ingest, "fix_titles", lambda conn: 0)
    assert ingest.reindex_pending(env) == {"indexed": 6}
    assert order == [ids["depot-mcp"], ids["live-texts"], ids["depot-mail"], ids["depot-tard"],
                     ids["veille-a"], ids["veille-b"]]


def test_document_reste_pending_pas_de_boucle(env, monkeypatch):
    """Un document qui reste 'pending' (PDF différé) n'est vu qu'une fois par passage."""
    from cop_corpus import ingest

    d, _ = ingest.register(env, b"x" * 300, url="https://x.test/p.txt", mime="text/plain", source_id="depot-mcp")
    seen = []
    monkeypatch.setattr(ingest, "index_document", lambda conn, i: seen.append(i) or "pending")
    assert ingest.reindex_pending(env) == {"pending": 1} and seen == [d]


def test_deposer_url_html_indexe_par_le_worker(env, monkeypatch):
    """Dépôt MCP d'une page HTML : le site l'enregistre 'pending' ; le fil du worker l'indexe pendant l'attente de
    l'outil, qui rend alors le statut 'indexed' et la vérification."""
    from cop_corpus import fetch, ingest, mcp_server

    class FakeClient:
        def get(self, url, *_, **__):
            return fetch.FetchResult(url=url, status=200, content=HTML, mime="text/html")

    monkeypatch.setattr(fetch, "Client", FakeClient)
    monkeypatch.setattr(ingest, "DEFER", True)   # processus du site
    monkeypatch.setattr(mcp_server, "DEPOSIT_WAIT_SECONDS", 20)
    seen = []

    def worker():
        from cop_corpus import db

        conn = db.connect()
        for _ in range(100):
            if conn.execute("SELECT 1 FROM documents WHERE status='pending'").fetchone():
                seen.append(conn.execute("SELECT COUNT(*) FROM passages").fetchone()[0])
                ingest.DEFER = False   # le worker, autre processus en production
                ingest.reindex_pending(conn)
                return
            time.sleep(0.05)

    t = threading.Thread(target=worker)
    t.start()
    out = asyncio.run(mcp_server.deposer_url("https://x.test/statement.html", niveau="5"))
    t.join()
    assert seen == [0] and out["statut"] == "indexed" and "verification" in out


# --- critère 10 : tidy_texts sous le verrou ; _index_document relit lignée et version sous le verrou -------------------

V1 = "Draft decision -/CP.31\nHost of the Climate Technology Centre\n1. Welcomes the report;\n2. Decides to host.\n"
V2 = V1 + "3. Requests the secretariat to prepare the memorandum.\n"


def _texts(conn):
    from cop_corpus import ingest, live

    live.ensure_schema(conn)
    docs = []
    for name, text in (("ctc1", V1), ("ctc2", V2)):
        d, _, st = ingest.ingest_bytes(conn, text.encode(), url=f"https://docs.test/{name}.txt", filename=f"{name}.txt",
                                       meta={"title": name, "level": "2"}, source_id="live-texts")
        assert st == "indexed"
        docs.append(d)
    for sym, pub, doc in (("DT.SBI65.i13b.2", "2026-11-14T12:00", docs[0]),
                          ("DT.COP31.i9b_CMA8.i12b.1", "2026-11-17T22:30", docs[1])):
        conn.execute("""INSERT INTO live_texts(key,symbol,title,cb_type,body,dossier,published,doc_id,pdf_url,
                        first_seen,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                     (sym, sym, "Host of the Climate Technology Centre", "draft text", "SBI", "technologie-capacites",
                      pub, doc, None, live.now_iso(), live.now_iso()))
    conn.commit()
    return docs


def test_course_tidy_pendant_l_indexation(env, monkeypatch):
    """Point d'arrêt simulé entre la lecture du document (avant le verrou) et l'écriture : live.tidy_texts pose la
    lignée `neg:` et la version 2 pendant l'extraction ; l'indexation les conserve (avant : `url:…`, version 1)."""
    from cop_corpus import db, extract, ingest, live

    d1, d2 = _texts(env)
    env.execute("UPDATE documents SET status='pending', lineage_key=NULL, version=NULL, symbol=NULL WHERE id=?", (d2,))
    env.commit()
    paused, resume = threading.Event(), threading.Event()
    real_chunk = extract.chunk

    def chunk(pages):   # appelé après la lecture du document et l'extraction, avant le verrou d'écriture
        out = real_chunk(pages)
        if threading.current_thread().name == "indexeur":
            paused.set()
            assert resume.wait(10)
        return out

    monkeypatch.setattr(extract, "chunk", chunk)
    res = {}

    def run():
        res["st"] = ingest.index_document(db.connect(), d2)

    t = threading.Thread(target=run, name="indexeur")
    t.start()
    assert paused.wait(10)
    out = live.tidy_texts(env)
    assert out["lignees"] >= 1
    resume.set()
    t.join(10)
    assert res["st"] == "indexed"
    row = env.execute("SELECT lineage_key, version, symbol FROM documents WHERE id=?", (d2,)).fetchone()
    assert row["lineage_key"].startswith("neg:technologie-capacites|") and row["version"] == 2
    assert row["symbol"] == "DT.COP31.i9b_CMA8.i12b.1"   # cote de l'index posée par tidy_texts, gardée elle aussi
    assert live.tidy_texts(env) == {"doublons": 0, "metadonnees": 0, "lignees": 0}


def test_tidy_texts_attend_le_verrou_d_ecriture(env):
    from cop_corpus import db, ingest, live

    _texts(env)
    done = threading.Event()
    with ingest._write_lock:
        t = threading.Thread(target=lambda: (live.tidy_texts(db.connect()), done.set()))
        t.start()
        assert not done.wait(0.3)   # bloqué tant que le verrou est tenu
    t.join(10)
    assert done.is_set()


def test_tidy_texts_ne_laisse_pas_de_transaction_ouverte(env, monkeypatch):
    from cop_corpus import live

    _texts(env)
    monkeypatch.setattr(live, "negotiated", lambda t: (_ for _ in ()).throw(RuntimeError("panne")))
    with pytest.raises(RuntimeError):
        live.tidy_texts(env)
    assert not env.in_transaction
