"""Relevé SEORS : requête ordinaire (arrêt sur l'anti-bot), bouton depuis le navigateur, alerte de relevé ancien."""

import importlib

import pytest


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
    for mod in ("auth", "search", "ingest", "agenda", "web"):
        importlib.reload(importlib.import_module(f"cop_corpus.{mod}"))
    return db.get()


def _page(n=60, day="18", room_of=lambda i: i % 9 + 1):
    rows = "".join(
        f"<tr><td>Wed, {day} Nov 2026</td><td>11:30—13:00<br>Side Event Room {room_of(i)} - X</td><td>org</td>"
        f"<td><strong>Event number {i} on adaptation</strong><br>details</td></tr>" for i in range(n))
    return ("<html><body><select name='keywords'><option value=''>All</option><option value='7'>Water</option></select>"
            f"<table>{rows}</table></body></html>")


def test_parse_seors_html():
    from cop_corpus import agenda

    rows = agenda.parse_seors_html(_page(3))
    assert rows[0] == ["18", "11:30", "13:00", "1", "Event number 0 on adaptation"]


def test_sync_seors_stops_on_antibot_and_imports_when_allowed(env):
    from cop_corpus import agenda, db
    from cop_corpus.fetch import FetchResult

    asked = []

    class Blocked:
        def get(self, url, **kw):
            asked.append(url)
            return FetchResult(url=url, status=200, mime="text/html",
                               content=b"<html><script src='/_Incapsula_Resource?x=1'></script></html>")

    out = agenda.sync_seors(env, Blocked())
    assert "bloqué" in out["seors"] and len(asked) == 1                 # une seule requête, aucun contournement
    assert "bloqué" in db.meta_get(env, "agenda_seors_auto")

    class Allowed:
        def get(self, url, **kw):
            html = _page(5) if "keywords=7" in url else _page(60)
            return FetchResult(url=url, status=200, mime="text/html", content=html.encode())

    assert agenda.sync_seors(env, Allowed())["seors"] == 60
    ev = env.execute("SELECT topics FROM agenda_events WHERE source='seors' AND title='Event number 0 on adaptation'").fetchone()
    assert "Water" in ev["topics"]


def test_programme_warns_when_listing_is_old(env):
    from cop_corpus import agenda, db

    agenda.ensure_schema(env)
    db.meta_set(env, "agenda_planner_built", "2026-11-09")
    db.meta_set(env, "agenda_seors_at", "2026-11-13T10:49:25Z")
    out = agenda.query(env, jour="2026-11-18")
    assert "périmé" in out["avertissement"] and "2026-11-13" in out["avertissement"]


def test_bookmarklet_import_endpoint(env):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from cop_corpus import agenda, auth, web

    uid, token = auth.create_user(env, "admin@x.org", "A", admin=True)
    gid, gtoken = auth.create_user(env, "g@x.org", "G", restricted=False)
    itok = auth.import_token(auth.by_id(env, uid))
    link = agenda.seors_bookmarklet("https://cop31.example/admin/seors/import", itok)
    assert token not in link                                   # jamais l'adresse MCP dans le bouton
    assert link.startswith("javascript:") and "Bearer" in link and "keywords" in link
    client = TestClient(Starlette(routes=web.routes), base_url="https://testserver")
    pre = client.options("/admin/seors/import", headers={"Origin": "https://seors.unfccc.int"})
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "https://seors.unfccc.int"
    tsv = "#releve\t2026-11-18T09:00:00Z\n#cats\tWater\n" + "\n".join(
        f"18|11:30|13:00|{i % 9 + 1}|Event {i}|0" for i in range(60))
    gtok = auth.import_token(auth.by_id(env, gid))
    assert client.post("/admin/seors/import", content=tsv, headers={"Authorization": f"Bearer {gtok}"}).status_code == 403
    assert client.post("/admin/seors/import", content=tsv, headers={"Authorization": f"Bearer {token}"}).status_code == 403
    assert client.post("/admin/seors/import", content=tsv, headers={"Authorization": f"Bearer {uid}.forged"}).status_code == 403
    r = client.post("/admin/seors/import", content=tsv, headers={"Authorization": f"Bearer {itok}"})
    assert r.status_code == 200 and r.json()["importes"] == 60
    assert agenda.seors_status(env)["releve_le"] == "2026-11-18T09:00:00Z"
    assert client.get("/admin/programme", headers={"Authorization": f"Bearer {token}"}).status_code == 403   # pas de jeton sur les pages
    client.cookies.set(web.COOKIE, auth.make_cookie(uid, days=1))
    page = client.get("/admin/programme")
    assert page.status_code == 200 and "Relevé SEORS → COP31" in page.text and "javascript:" in page.text
