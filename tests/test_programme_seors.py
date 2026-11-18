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


def test_official_schedule_replaces_planner_meetings(env):
    from cop_corpus import agenda, db

    agenda.ensure_schema(env)
    db.meta_set(env, "agenda_planner_built", "2026-11-09")
    agenda._upsert(env, [dict(key="planner:cal:1", kind="reunion", days=[18], start="10:00", end="11:00", room="X",
                              title="Old planner meeting", organisers=[], summary=None, speakers=None, topics=[],
                              type=None, access=None, link=None, note=None)], "planner", "2026-11-09")
    ev = lambda i, title, typ="Negotiations", day=18: {
        "id": str(i), "title": title, "date": [{"value": f"2026-11-{day}T09:00:00", "end_value": f"2026-11-{day}T10:00:00"}],
        "room": [{"value": "Meeting Room 19"}], "typeofevent": [typ], "access": [{"value": "0"}],
        "body": ["Conference of the Parties (COP)"], "keywords": [], "topic": [], "maintextbody": [],
        "haswebcast": [{"value": "0"}], "url": f"https://unfccc.int/event/{i}"}
    events = [ev(i, f"CMA 4 Global stocktake - Informal consultation {i}") for i in range(90)] + \
        [ev(100 + i, f"Side event {i}", "Side events") for i in range(30)] + [ev(200 + i, f"G77 coordination {i}", "Coordination meetings") for i in range(20)]
    out = agenda.import_unfccc_schedule(env, {"releve": "2026-11-18T07:41:03Z", "events": events})
    assert out["importes"] == 110 and out["consultations_informelles"] == 90           # side events laissés à SEORS
    q = agenda.query(env, jour="18", sujet="informal", types=["reunion"])
    assert q["total"] == 90 and q["evenements"][0]["source"].startswith("calendrier officiel")
    assert not any(e["titre"] == "Old planner meeting" for e in agenda.query(env, jour="18", types=["reunion"], limit=500)["evenements"])
    assert agenda._norm("Side Event Room 4 - Aşık Veysel") == agenda._norm("Side Event Room 4 - AŞIK VEYSEL")


def test_platform_overlays_real_times(env):
    from cop_corpus import agenda

    agenda.ensure_schema(env)
    agenda._upsert(env, [dict(key="unfccc:1", kind="reunion", days=[18], start="10:00", end="11:00", room="Meeting Room 19",
                              title="COP 3(b) / CMA 3(b) Matters relating to Action for Climate Empowerment - Informal consultation",
                              organisers=[], summary=None, speakers=None, topics=[], type="Negotiations", access=None,
                              link=None, note=None)], "unfccc", "2026-11-18T07:41:03Z")
    out = agenda.import_platform(env, {"releve": "2026-11-18T07:44:47Z", "sessions": [
        {"id": "a", "title": "COP 3(b) / CMA 3(b) Matters relating to Action for Climate Empowerment - Informal consultation",
         "dateTimeRange": {"start": "2026-11-18T07:00:00Z", "end": "2026-11-18T09:00:00Z"},
         "actualDateTimeRange": {"start": "2026-11-18T07:00:00Z", "end": "2026-11-18T08:00:00Z"}, "roomName": "Meeting Room 19"},
        {"id": "b", "title": "Heads of Delegation consultations on CMA 11(f)", "sessionType": "Heads of delegation",
         "dateTimeRange": {"start": "2026-11-18T12:30:00Z", "end": "2026-11-18T14:00:00Z"},
         "actualDateTimeRange": {"start": "2026-11-18T12:30:00Z", "end": "2026-11-18T14:00:00Z"}, "roomName": "Meeting Room 25"}]})
    assert out["horaires_mis_a_jour"] == 1 and out["ajoutees"] == 1
    ace = env.execute("SELECT start, end, note FROM agenda_events WHERE key='unfccc:1'").fetchone()
    assert (ace["start"], ace["end"]) == ("10:00", "12:00") and "fin prévue 11:00" in ace["note"]
    hod = env.execute("SELECT start, end, source FROM agenda_events WHERE key='plateforme:b'").fetchone()
    assert (hod["start"], hod["end"], hod["source"]) == ("15:30", "17:00", "unfccc")
