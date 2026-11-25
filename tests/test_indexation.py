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


def test_versions_linguistiques(env):
    en = _client().get("/about").text
    assert '<link rel="alternate" hreflang="en" href="https://opencop.example/about">' in en
    assert '<link rel="alternate" hreflang="fr" href="https://opencop.example/about?lang=fr">' in en
    assert '<link rel="alternate" hreflang="x-default" href="https://opencop.example/about">' in en
    fr = _client().get("/about?lang=fr").text
    assert '<link rel="canonical" href="https://opencop.example/about?lang=fr">' in fr      # propre à la langue
    assert '<meta property="og:url" content="https://opencop.example/about?lang=fr">' in fr
    assert '<meta property="og:locale" content="fr_FR">' in fr and 'og:locale:alternate" content="en_GB"' in fr
    assert '<link rel="alternate" hreflang="fr" href="https://opencop.example/about?lang=fr">' in fr
    # cookie français sans ?lang= : l'adresse de référence reste celle de la version sans paramètre
    c = _client()
    c.cookies.set("lang", "fr")
    assert '<link rel="canonical" href="https://opencop.example/about">' in c.get("/about").text
    # autres paramètres ignorés dans l'adresse de référence
    assert '<link rel="canonical" href="https://opencop.example/about?lang=fr">' in _client().get("/about?lang=fr&x=1").text


def test_sitemap_bilingue_et_robots(env):
    xml = _client().get("/sitemap.xml").text
    assert 'xmlns:xhtml="http://www.w3.org/1999/xhtml"' in xml
    assert "<loc>https://opencop.example/about?lang=fr</loc>" in xml
    block = re.search(r"<url><loc>https://opencop.example/about\?lang=fr</loc>.*?</url>", xml).group(0)
    assert 'hreflang="en" href="https://opencop.example/about"' in block and 'hreflang="x-default"' in block
    from xml.dom import minidom
    minidom.parseString(xml.encode())                                     # XML bien formé
    robots = _client().get("/robots.txt").text
    assert "Allow: /database?lang=fr$" in robots and "Disallow: /database?" in robots
    # le $ limite l'autorisation à la page exacte : une recherche en français reste exclue (règle la plus longue)
    from urllib import robotparser  # noqa: F401  (ne gère pas $ ; vérification sur le texte)
    assert "Allow: /database?lang=fr\n" not in robots


def test_llms_txt_et_image(env):
    r = _client().get("/llms.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    t = r.text
    assert t.startswith("# OpenCOP") and "https://opencop.example/connect" in t and "not affiliated" in t
    assert "Akim Viennet" in t and "Alix de Thoisy" in t
    assert not re.search(r"https?://(?!opencop\.example|github\.com)", t)          # liens sur le domaine public
    html = _client().get("/").text
    assert '<meta property="og:image" content="https://opencop.example/static/og-opencop.png">' in html
    assert '<meta name="twitter:card" content="summary_large_image">' in html
    assert "/privacy" not in t and "https://opencop.example/confidentialite" in t   # une seule adresse
    img = _client().get("/static/og-opencop.png")
    assert img.status_code == 200 and img.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_documents_une_seule_version(env):
    conn = env
    ok = _doc(conn, "Public climate finance text. ", "ok")
    secret = _doc(conn, "Internal note on positions. ", "secret")
    conn.execute("UPDATE documents SET status='indexed' WHERE id IN (?,?)", (ok, secret))
    conn.execute("UPDATE documents SET access='restreint' WHERE id=?", (secret,))
    conn.commit()
    html = _client().get(f"/doc/{ok}?lang=fr").text
    assert f'<link rel="canonical" href="https://opencop.example/doc/{ok}">' in html   # pas de version française
    assert '<link rel="alternate" hreflang' not in html
    xml = _client().get("/sitemap.xml").text
    assert f"/doc/{ok}?lang=fr" not in xml and f"/doc/{secret}<" not in xml and f"/doc/{ok}</loc>" in xml
