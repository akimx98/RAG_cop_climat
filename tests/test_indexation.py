"""Indexation par les moteurs : sitemap (jamais un document non public, en attente ou doublon), robots.txt qui
l'annonce, adresse de référence et description sur le domaine public."""

import importlib
import re

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COP_EMBED_PROVIDER", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("COP_PUBLIC_BASE", "https://opencop.example")
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


def _client():
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from cop_corpus import web, web_live

    # autre nom d'hôte que le domaine public : l'adresse de référence doit quand même pointer vers lui
    return TestClient(Starlette(routes=[*web.routes, *web_live.routes]), base_url="https://ancien.example")


def _doc(conn, text: str, name: str) -> int:
    from cop_corpus import ingest

    doc_id, _, _ = ingest.ingest_bytes(conn, (text * 20).encode(), url=f"https://x/{name}.txt", filename=f"{name}.txt")
    return doc_id


def test_sitemap_documents_publics_seulement(env):
    conn = env
    ok = _doc(conn, "Public climate finance text. ", "ok")
    secret = _doc(conn, "Internal note on positions. ", "secret")
    attente = _doc(conn, "Pending draft decision text. ", "attente")
    double = _doc(conn, "Duplicate of the public text. ", "double")
    conn.execute("UPDATE documents SET status='indexed' WHERE id IN (?,?,?,?)", (ok, secret, attente, double))
    conn.execute("UPDATE documents SET access='restreint' WHERE id=?", (secret,))
    conn.execute("UPDATE documents SET status='pending' WHERE id=?", (attente,))
    conn.execute("UPDATE documents SET duplicate_of=? WHERE id=?", (ok, double))
    conn.commit()

    r = _client().get("/sitemap.xml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    locs = re.findall(r"<loc>([^<]+)</loc>", r.text)
    assert "https://opencop.example/" in locs and "https://opencop.example/about" in locs
    assert f"https://opencop.example/doc/{ok}" in locs
    for bad in (secret, attente, double):
        assert f"https://opencop.example/doc/{bad}" not in locs
    assert all(u.startswith("https://opencop.example/") for u in locs)
    assert any("/database/negotiations/" in u for u in locs)


def test_robots_annonce_le_sitemap(env):
    r = _client().get("/robots.txt")
    assert "Sitemap: https://opencop.example/sitemap.xml" in r.text
    assert "Disallow: /u/" in r.text and "Disallow: /admin" in r.text


def test_reference_et_description(env):
    html = _client().get("/about").text
    assert '<link rel="canonical" href="https://opencop.example/about">' in html
    assert '<meta property="og:url" content="https://opencop.example/about">' in html
    desc = re.search(r'<meta name="description" content="([^"]*)">', html).group(1)
    assert "free and open-source" in desc
    assert re.search(r'<meta property="og:title" content="What is OpenCOP\?">', html)
    fr = _client().get("/about?lang=fr").text
    assert "libre et gratuit" in re.search(r'<meta name="description" content="([^"]*)">', fr).group(1)
