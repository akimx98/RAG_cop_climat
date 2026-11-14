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
    for mod in ("embed", "auth", "search", "ingest", "alerts", "diff", "telegram", "mailin"):
        importlib.reload(importlib.import_module(f"cop_corpus.{mod}"))
    return db.get()


DRAFT_V1 = """Draft decision -/CMA.8
Mitigation work programme
The Conference of the Parties serving as the meeting of the Parties to the Paris Agreement,
1. Welcomes the synthesis report on nationally determined contributions;
2. Urges Parties to [accelerate][consider] the transition away from fossil fuels in energy systems;
3. Requests the secretariat to prepare a report;
4. Decides to continue consideration of this matter at its ninth session;
5. Notes the importance of finance;
6. Invites Parties to submit views.
"""

DRAFT_V2 = DRAFT_V1.replace("[accelerate][consider]", "accelerate").replace(
    "5. Notes the importance of finance;", "5. Recognizes the importance of finance and technology;"
) + "7. Calls on Parties to triple renewable energy capacity globally by 2030;\n"


def test_chunk_numbered_paragraphs():
    from cop_corpus import extract

    chunks = extract.chunk([extract.Page(1, DRAFT_V1)])
    labels = [c.para for c in chunks]
    assert "§2" in labels and "§6" in labels


def test_decision_labels():
    from cop_corpus import extract

    text = "Decision 1/CMA.5\nOutcome\n" + "\n".join(f"{i}. Paragraph number {i} of text." for i in range(1, 7))
    chunks = extract.chunk([extract.Page(3, text)])
    assert any(c.para == "1/CMA.5 §4" for c in chunks)


def test_sub_to_fts():
    from cop_corpus import alerts

    assert alerts.sub_to_fts("fossil fuels, NCQG") == '"fossil fuels"* OR "NCQG"'
    assert alerts.sub_to_fts("*") is None


def test_versions_alerts_and_diff(env):
    from cop_corpus import alerts, auth, diff, ingest

    conn = env
    uid, _ = auth.create_user(conn, "a@b.c", "A")
    alerts.add_subscription(conn, uid, "fossiles", "fossil fuels, combustibles fossiles")
    alerts.add_subscription(conn, uid, "tous projets", "*", doc_types=["projet_texte"])
    alerts.run(conn)  # initialise le curseur
    meta = {"title": "Draft text on mitigation work programme version 1", "doc_type": "projet_texte", "level": "2"}
    d1, new1, st1 = ingest.ingest_bytes(conn, DRAFT_V1.encode(), url="https://x/v1.txt", meta=meta, filename="v1.txt")
    meta2 = dict(meta, title="Draft text on mitigation work programme version 2")
    d2, new2, st2 = ingest.ingest_bytes(conn, DRAFT_V2.encode(), url="https://x/v2.txt", meta=meta2, filename="v2.txt")
    assert st1 == st2 == "indexed"
    v = conn.execute("SELECT lineage_key, version FROM documents WHERE id=?", (d2,)).fetchone()
    assert v["version"] == 2
    assert alerts.run(conn) == 4  # 2 documents × 2 abonnements
    res = diff.compare(conn, d1, d2)
    assert res["evolution"]["crochets"] == -2
    kinds = {c["paragraphe"]: c["changement"] for c in res["changements"]}
    assert kinds["§7"] == "ajouté" and kinds["§5"] == "modifié"


def test_restricted_hidden_from_public_user(env):
    from cop_corpus import auth, ingest, search

    conn = env
    ingest.ingest_bytes(conn, ("Note interne sur la position de négociation " * 20).encode(), url=None,
                        access="restreint", filename="note.txt")
    _, _ = auth.create_user(conn, "pub@b.c", "P", restricted=False)
    pub = auth.by_email(conn, "pub@b.c")
    assert search.search(conn, "position de négociation", {}, pub, semantic=False) == []
    assert search.search(conn, "position de négociation", {}, {"can_restricted": 1, "active": 1}, semantic=False)


def test_telegram_flow(env, monkeypatch):
    from cop_corpus import auth, telegram

    conn = env
    sent: list[tuple] = []
    monkeypatch.setattr(telegram, "send", lambda chat, text: sent.append((chat, text)) or True)
    monkeypatch.setattr(telegram, "download", lambda fid: ("Note interne sur la ligne rouge finance " * 30).encode())
    auth.create_user(conn, "a@b.c", "Alix")
    code = conn.execute("SELECT telegram_code FROM users").fetchone()[0]

    def msg(text=None, **extra):
        telegram.handle(conn, {"message": {"chat": {"id": 42}, **({"text": text} if text else {}), **extra}})
        return sent[-1][1]

    assert "non lié" in msg("/cherche finance")
    assert "Compte lié" in msg(f"/start {code}")
    assert "suivi" in msg("/suivre ligne rouge")
    out = msg(caption="Note de coordination UE", document={"file_id": "x", "file_name": "note.txt", "mime_type": "text/plain"})
    assert "restreint" in out
    out = msg("/cherche ligne rouge finance")
    assert "Document interne" in out and "Note de coordination" not in out and "ligne rouge" not in out.split("\n", 1)[0]
    assert "Document interne" in msg("/nouveau")


def test_semantic_search_path(env, monkeypatch):
    """Le chemin sémantique doit fonctionner de bout en bout (embeddings simulés)."""
    import numpy as np
    from cop_corpus import config, embed, ingest, search

    monkeypatch.setattr(config, "EMBED_PROVIDER", "openai")

    def fake_encode(texts):
        v = np.array([[1.0 if "finance" in t.lower() else 0.0, 1.0] + [0.0] * (config.EMBED_DIM - 2) for t in texts],
                     dtype=np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    monkeypatch.setattr(embed, "encode", fake_encode)
    conn = env
    ingest.ingest_bytes(conn, ("Climate finance goal of 300 billion per year. " * 10).encode(), url="https://x/a.txt",
                        filename="a.txt")
    assert embed.embed_pending(conn) > 0
    assert embed.search(conn, "finance", k=3)
    assert search.search(conn, "montant versé", {}, {"can_restricted": 1, "active": 1})


def test_password_login_and_links(env):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from cop_corpus import auth, db, web
    importlib.reload(web)
    conn = db.get()
    uid, token = auth.create_user(conn, "alix@example.org", "Alix")
    user = auth.by_id(conn, uid)
    assert auth.mcp_token(user) == token                      # adresse MCP réaffichable
    client = TestClient(Starlette(routes=web.routes), base_url="https://testserver")

    # première connexion par lien 24 h -> page Compte -> mot de passe
    link = auth.login_link(user).split("/entrer/")[1]
    r = client.get(f"/entrer/{link}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/compte"
    assert "Au moins" in client.post("/compte", data={"password": "court", "password2": "court"}).text
    r = client.post("/compte", data={"password": "un-bon-mot-de-passe", "password2": "un-bon-mot-de-passe"})
    assert "Mot de passe enregistré" in r.text
    assert client.get(f"/entrer/{link}?lang=fr").text.count("Lien expiré") == 1   # caduc après changement
    assert token in client.get("/connecteur").text                        # connecteur toujours affiché

    # connexion par mot de passe depuis un autre navigateur
    other = TestClient(Starlette(routes=web.routes), base_url="https://testserver")
    assert "incorrect" in other.post("/connexion", data={"email": "alix@example.org", "password": "faux"}).text.lower()
    r = other.post("/connexion", data={"email": "ALIX@example.org", "password": "un-bon-mot-de-passe",
                                       "next": "/connecteur"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/connecteur"
    assert token in other.get("/connecteur").text
    r = other.post("/connexion", data={"email": "alix@example.org", "password": "un-bon-mot-de-passe",
                                       "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"

    # limitation des essais
    for _ in range(auth.MAX_FAILURES):
        auth.check_password(conn, "alix@example.org", "faux", "1.2.3.4")
    assert auth.check_password(conn, "alix@example.org", "un-bon-mot-de-passe", "1.2.3.4") is None
    assert auth.check_password(conn, "alix@example.org", "un-bon-mot-de-passe", "5.6.7.8")


def test_mail_deposit_and_newsletter(env, monkeypatch):
    from email.message import EmailMessage
    from cop_corpus import auth, config, mailin, telegram
    sent = []
    monkeypatch.setattr(telegram, "send", lambda chat, text: sent.append((chat, text)) or True)
    conn = env
    uid, _ = auth.create_user(conn, "alix@example.org", "Alix", admin=True)
    conn.execute("UPDATE users SET telegram_chat_id='42' WHERE id=?", (uid,))
    conn.commit()
    user = auth.by_id(conn, uid)
    addr = mailin.deposit_address(conn, user)
    assert mailin.route(conn, addr)[1]["id"] == uid
    assert mailin.route(conn, "depot-000000@" + config.MAIL_DOMAIN) is None
    assert mailin.route(conn, "root@" + config.MAIL_DOMAIN) is None
    assert mailin.route(conn, "veille@" + config.MAIL_DOMAIN)[0] == "veille"

    def spool(msg, rcpt):
        folder = config.MAIL_DIR / mailin.SPOOL
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{len(list(folder.iterdir()))}.eml").write_bytes(f"X-Cop-Rcpt: {rcpt}\r\n".encode() + msg.as_bytes())

    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "Alix <alix@example.org>", addr, "TR: Note de coordination UE finance"
    m.set_content("Compte rendu de la coordination UE du matin sur le NCQG. " * 8)
    m.add_attachment(b"Draft text on the new collective quantified goal. " * 20, maintype="text",
                     subtype="plain", filename="draft-ncqg.txt")
    spool(m, addr)
    assert mailin.process_spool(conn) == 1
    docs = conn.execute("SELECT title, access, uploaded_by FROM documents ORDER BY id").fetchall()
    assert len(docs) == 2 and all(d["access"] == "restreint" and d["uploaded_by"] == uid for d in docs)
    assert sent and "2 document(s)" in sent[-1][1] and "coordination" not in sent[-1][1]   # accusé sans titre

    n = EmailMessage()
    n["From"], n["Subject"] = "ENB <enb@iisd.org>", "Earth Negotiations Bulletin: COP31 highlights, Monday"
    n.set_content("<html><body><p>" + "Parties discussed adaptation indicators and finance. " * 10 +
                  '</p><a href="https://enb.iisd.org/cop31">Read</a></body></html>', subtype="html")
    spool(n, mailin.veille_address())
    mailin.process_spool(conn)
    row = conn.execute("SELECT * FROM mail_inbox WHERE mailbox='veille'").fetchone()
    assert row["status"] == "attente" and "ENB" in sent[-1][1]
    mailin.allow_sender(conn, "iisd.org")
    ids = mailin.ingest_newsletter(conn, mailin.load(row["path"]), row["path"], inbox_id=row["id"])
    d = conn.execute("SELECT level, access, org, title FROM documents WHERE id=?", (ids[0],)).fetchone()
    assert (d["level"], d["access"]) == ("4", "public") and "ENB" in d["title"]
    spool(n, mailin.veille_address())                                     # même lettre, expéditeur autorisé
    mailin.process_spool(conn)
    assert conn.execute("SELECT status FROM mail_inbox ORDER BY id DESC").fetchone()["status"] == "ingere"
    assert ("Read", "https://enb.iisd.org/cop31") in mailin.links(mailin.load(row["path"]))


def test_live_texts_notes_and_positions(env, monkeypatch):
    """Index Carbon Brief → textes annoncés ; dépôt manuel rattaché ; note interne ; citation inventée rejetée."""
    import csv
    import io

    from cop_corpus import auth, ingest, live, llm, positions

    conn = env
    live._ready.clear()
    rows = [["Body", "Agenda item", "Symbol", "Title", "Document type", "Date", "Time", "PDF link", "Topic", "Pages",
             "Brackets", "Options", "Type", "Note", "Tweet"],
            ["CMA 8", "4", "", "Draft text on GGA indicators version 2", "Informal notes", "11/12/2026", "09:30",
             "https://unfccc.int/sites/default/files/resource/cma8_gga_v2.pdf", "Global goal on adaptation", "6", "41",
             "12", "", "", ""]]
    monkeypatch.setattr(live, "_sheet", lambda client, sid, sheet: rows)
    new = live.sync_texts(conn, None)
    t = conn.execute("SELECT * FROM live_texts WHERE id=?", (new[0],)).fetchone()
    assert t["dossier"] == "adaptation" and t["published"] == "2026-11-12T09:30" and t["brackets"] == 41
    assert live.sync_texts(conn, None) == []  # pas de doublon
    # Dépôt manuel par un collègue (fichier téléchargé dans son navigateur) : rattaché par le nom de fichier.
    ingest.ingest_bytes(conn, ("Draft text [option 1] [option 2] " * 40).encode(), url=None, filename="cma8_gga_v2.txt")
    assert live.link_existing(conn) == 1
    # Note de terrain : restreinte, rattachée par l'alias, jamais envoyée au modèle.
    uid, _ = auth.create_user(conn, "n@x.fr", "N")
    user = auth.by_id(conn, uid)
    calls = []
    monkeypatch.setattr(llm, "json_call", lambda *a, **k: calls.append(k) or {"positions": []})
    _nid, slug = live.add_note(conn, user, "GGA l'AGN demande des indicateurs de moyens de mise en œuvre")
    assert slug == "adaptation" and not calls
    doc = conn.execute("SELECT access FROM documents WHERE source_id='note-terrain'").fetchone()
    assert doc["access"] == "restreint"
    # Citation absente de la source : position rejetée ; citation exacte : gardée.
    src = "The LMDC rejected any new mitigation targets, calling them a red line for developing countries."
    kept = positions._store(conn, [
        {"actor": "LMDC (group)", "dossier": "attenuation", "issue": "objectifs", "stance": "ligne rouge",
         "summary": "s", "quote": "rejected any new mitigation targets", "said_on": ""},
        {"actor": "EU", "dossier": "attenuation", "issue": "x", "stance": "soutient", "summary": "s",
         "quote": "the EU called for a fossil fuel roadmap", "said_on": ""}],
        src, kind="extraction", label="ENB", url=None, doc_id=None, default_date="2026-11-12")
    assert kept == 1
    assert conn.execute("SELECT actor FROM positions").fetchone()["actor"] == "LMDC"


def test_bracket_delta_alert(env):
    """Un texte récupéré déclenche une alerte Telegram avec l'analyse des crochets vs la version précédente."""
    from cop_corpus import auth, diff, ingest, live

    conn = env
    live._ready.clear()
    meta = {"title": "Draft text on adaptation version 1", "doc_type": "projet_texte", "level": "2"}
    d1, *_ = ingest.ingest_bytes(conn, DRAFT_V1.encode(), url="https://x/a1.txt", meta=meta, filename="a1.txt")
    meta2 = dict(meta, title="Draft text on adaptation version 2")
    d2, *_ = ingest.ingest_bytes(conn, DRAFT_V2.encode(), url="https://x/a2.txt", meta=meta2, filename="a2.txt")

    # Les deux versions partagent la même lignée ; le diff voit les crochets levés ([accelerate][consider] → accelerate).
    assert diff.previous_in_lineage(conn, d2) == d1
    d = diff.since_previous(conn, d2)
    assert d["evolution"]["crochets"] == -2
    assert "crochets 2→0 (-2, levés)" in diff.delta_line(d)

    # Abonné au dossier « adaptation » sur Telegram.
    uid, _ = auth.create_user(conn, "f@x.fr", "F")
    conn.execute("UPDATE users SET telegram_chat_id='7' WHERE id=?", (uid,))
    live.follow(conn, uid, "adaptation", True)
    # Texte annoncé (notified) puis récupéré (doc_id) mais delta pas encore signalé.
    conn.execute(
        "INSERT INTO live_texts(key,symbol,title,dossier,published,doc_id,status,notified,diff_notified,"
        "first_seen,updated_at) VALUES('t-gga','FCCC/PA/CMA/2026/L.9','GGA',?,?,?,'recupere',1,0,?,?)",
        ("adaptation", "2026-11-13T10:00", d2, live.now_iso(), live.now_iso()))
    conn.commit()

    sent = []
    assert live.notify_retrieved(conn, lambda chat, text: sent.append((chat, text))) == 1
    assert sent and sent[0][0] == "7" and "crochets 2→0 (-2, levés)" in sent[0][1]
    assert live.notify_retrieved(conn, lambda *a: sent.append(a)) == 0  # ne ré-alerte pas


PLANNER_PAGE ="""<html><script>var SLOTS=[['11:30','13:00'],['13:15','14:45'],['15:00','16:30'],['16:45','18:15'],['18:30','20:00']];
var ROOMS={1:'Gordiyon',2:'Dalyan',3:'Zerzevan',4:'Aşık Veysel',5:'Hattuşa'};
var TOPICS={indigenous:'Indigenous peoples & communities',water:'Ocean & water'};
var OFFCAT={pav:'Pavilions & hubs'};
var BUILT='2026-11-09';
var OFF=[{"days": [14], "when": "14 Nov, 10:00–11:00", "t": "Pavillon France : eau", "k": ["pav"], "loc": "Blue Zone", "addr": "", "acc": "Open", "acct": "pub", "src": "x", "note": "", "hl": false, "st": "Verified", "link": "https://example.org/pf", "z": "blue", "id": 2000}];
</script>
<script type="application/json" id="data">[
 {"id": 1, "d": 13, "s": 1, "r": 5, "t": "Youth Perspectives on a Just Transition", "o": ["ONG CEUS Chile"], "on": 1, "x": "Youth.", "sp": "", "g": ["indigenous"], "f": [], "ot": []},
 {"id": 2, "d": 14, "s": 3, "r": 5, "t": "Protecting Rivers and Indigenous Territories", "o": ["Planète Amazone"], "on": 1, "x": "Rivers, mining.", "sp": "", "g": ["indigenous", "water"], "f": [], "ot": []}
]</script>
<script type="application/json" id="caldata">[
 {"id": 1000, "d": 13, "s": "13:00", "e": "15:00", "r": "Meeting Room 02", "open": true, "ty": "mandated", "wc": false, "t": "LCIPP Annual Intergenerational Dialogue"}
]</script>
<script>var NEG=[
     ['Fri 13 Nov','Negotiations (COP/CMP/CMA/SBSTA/SBI in the morning).'],
     ['Wed 11 and Thu 12 Nov','Leaders Summit.']
    ];</script></html>"""


def test_programme_planner_and_seors(env):
    from datetime import date

    from cop_corpus import agenda, db
    from cop_corpus.fetch import FetchResult

    conn = env
    parsed = agenda.parse_planner(PLANNER_PAGE)
    assert parsed["built"] == "2026-11-09" and set(parsed["overview"]) == {11, 12, 13}
    ev = {e["key"]: e for e in parsed["events"]}
    assert ev["planner:ev:1"]["room"] == "Side Event Room 5 - Hattuşa" and ev["planner:ev:1"]["start"] == "13:15"
    assert ev["planner:off:2000"]["start"] == "10:00" and ev["planner:cal:1000"]["kind"] == "reunion"

    class FakeClient:
        def get(self, url):
            if url == agenda.PLANNER_URL:
                return FetchResult(url=url, status=200, content=PLANNER_PAGE.encode())
            return FetchResult(url=url, status=404)

    assert "erreur" in agenda.sync_planner(conn, FakeClient())  # page trop courte : base intacte
    assert agenda.sync_planner(conn, FakeClient(), min_events=1)["construit_le"] == "2026-11-09"

    out = agenda.query(conn, jour="aujourd'hui", sujet="youth, LCIPP", today=date(2026, 11, 13))
    assert out["jour"] == "2026-11-13" and "Negotiations" in out["negociations_du_jour"]
    assert [e["titre"] for e in out["evenements"]][0] == "LCIPP Annual Intergenerational Dialogue"  # 13:00 < 13:15
    side = next(e for e in out["evenements"] if e["titre"].startswith("Youth"))
    assert side["lien"].endswith("scheduled_date=2026-11-13") and "seors.unfccc.int" in out["a_verifier"]
    off = agenda.query(conn, jour="demain", types=["hors_site"], today=date(2026, 11, 13))["evenements"]
    assert off[0]["lien"] == "https://example.org/pf"

    # Relevé SEORS : la salle a changé, il l'emporte sur le planner ; aucun numéro de téléphone conservé.
    rows = [{"date": "2026-11-13", "start": "13:15", "end": "14:45", "room": "Side Event Room 3 - Zerzevan",
             "title": "Youth Perspectives on a Just Transition", "orgs": ["ONG CEUS Chile", "+56 9 1234 5678"],
             "cats": ["Children and youth"]}]
    rows += [{"date": "2026-11-19", "start": "11:30", "end": "13:00", "room": "Side Event Room 1 - Gordiyon",
              "title": f"Filler {i}", "cats": []} for i in range(60)]
    res = agenda.import_seors(conn, {"releve": "2026-11-13T10:49:25Z", "events": rows})
    assert any("Hattuşa → Side Event Room 3 - Zerzevan" in c for c in res["salle_ou_horaire_change"])
    side = next(e for e in agenda.query(conn, jour="13", sujet="youth")["evenements"] if e["titre"].startswith("Youth"))
    assert side["lieu"] == "Side Event Room 3 - Zerzevan" and side["source"] == "SEORS (CCNUCC)"
    assert side["organisateurs"] == ["ONG CEUS Chile"] and side["resume"] == "Youth."
    assert db.meta_get(conn, "agenda_seors_at") == "2026-11-13T10:49:25Z"

    tsv = "#releve\t2026-11-13T10:00Z\n#cats\tWater;Indigenous peoples\n14|16:45|18:15|5|Protecting Rivers|0.1\n"
    p = agenda.parse_seors_tsv(tsv)
    assert p["events"][0]["room"] == "Side Event Room 5 - Hattuşa"
    assert p["events"][0]["cats"] == ["Water", "Indigenous peoples"]


def test_guest_signup_and_permissions(env, monkeypatch):
    """Invités : inscription par code, accueil, aucun ajout au corpus, rien d'interne."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from cop_corpus import auth, db, live, mailin, server, telegram, web, web_live
    importlib.reload(web)
    importlib.reload(web_live)
    conn = db.get()
    app = Starlette(routes=[*web.routes, *web_live.routes])
    client = TestClient(app, base_url="https://testserver")
    inv = auth.invite_settings(conn)

    assert "not (or no longer) valid" in client.get("/join/mauvais").text
    assert "pas (ou plus) valable" in client.get("/rejoindre/mauvais?lang=fr").text
    client.get("/join/mauvais?lang=en")                                    # choix de langue mémorisé
    r = client.post("/rejoindre", data={"code": inv["code"], "check_code": "1"}, follow_redirects=False)
    assert r.headers["location"] == f"/join/{inv['code']}#signup"
    form = {"code": inv["code"], "name": "Sam Invité", "organisation": "ONG X", "email": "Sam@ong.org",
            "password": "mot-de-passe-long", "password2": "mot-de-passe-long"}
    r = client.post(f"/rejoindre/{inv['code']}", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/bienvenue"
    guest = auth.by_email(conn, "sam@ong.org")
    assert guest["source"] == "invitation" and not guest["can_restricted"] and not guest["is_admin"]
    assert auth.is_guest(guest) and not auth.can_contribute(guest)

    page = client.get("/bienvenue").text
    assert f"/u/{auth.mcp_token(guest)}/mcp" in page
    client.post("/bienvenue", data={"dossiers": ["finance", "technologie-capacites"], "sujet": "Action for Climate Empowerment"})
    assert live.followed(conn, guest["id"]) == {"finance", "technologie-capacites"}
    home = client.get("/").text
    assert "Getting started" not in home and 'href="/deposer"' not in home
    assert guest["lang"] == "en" and 'lang="en"' in home
    assert "<svg" in client.get("/inviter").text                       # QR code d'invitation
    client.get("/lang/fr")
    assert "Fil" in client.get("/").text and auth.by_id(conn, guest["id"])["lang"] == "fr"
    client.get("/lang/en")

    # aucun ajout au corpus, aucun brief IA
    assert client.get("/deposer").status_code == 403
    assert client.post("/dossiers/finance/brief").status_code == 403
    assert client.post("/textes/1/deposer").status_code == 403

    # même e-mail, plafond, nouveau code
    other = TestClient(app, base_url="https://testserver")
    assert "already exists" in other.post(f"/rejoindre/{inv['code']}", data=form).text
    auth.set_invites(conn, maximum=1)
    assert "places for this invitation" in other.post(f"/rejoindre/{inv['code']}", data={**form, "email": "b@ong.org"}).text
    auth.set_invites(conn, maximum=60)
    auth.new_invite_code(conn)
    assert "no longer" in other.post(f"/rejoindre/{inv['code']}", data={**form, "email": "b@ong.org"}).text
    auth.set_invites(conn, open_=False)
    assert not auth.invite_valid(conn, auth.invite_settings(conn)["code"])

    # Telegram : ni dépôt, ni URL, ni note de terrain
    sent: list[tuple] = []
    monkeypatch.setattr(telegram, "send", lambda chat, text: sent.append((chat, text)) or True)
    monkeypatch.setattr(telegram, "download", lambda fid: b"x" * 500)
    telegram.handle(conn, {"message": {"chat": {"id": 7}, "text": f"/start {guest['telegram_code']}"}})
    assert "Compte lié" in sent[-1][1] and "PDF" not in sent[-1][1] and "/search" in sent[-1][1]
    n_docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    for extra in ({"document": {"file_id": "x", "file_name": "a.txt", "mime_type": "text/plain"}},
                  {"text": "https://example.org/page"}, {"text": "/note GGA blocage au §12"}):
        telegram.handle(conn, {"message": {"chat": {"id": 7}, **extra}})
        assert "contributors" in sent[-1][1]
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == n_docs

    # adresse de dépôt par e-mail inactive pour un invité
    addr = mailin.deposit_address(conn, auth.by_id(conn, guest["id"]))
    assert mailin.route(conn, addr) is None

    # limite de requêtes MCP pour les invités seulement
    assert not any(server.rate_limited(guest) for _ in range(server.GUEST_RATE))
    assert server.rate_limited(guest)
    assert not server.rate_limited({"id": 999, "can_restricted": 1, "active": 1})


def test_mcp_guest_tools_and_prompts(env, monkeypatch):
    from cop_corpus import auth, db, live, mcp_server

    conn = db.get()
    uid, _ = auth.create_user(conn, "g@x.org", "G", restricted=False)
    guest = auth.by_id(conn, uid)
    monkeypatch.setattr(mcp_server, "_user", lambda ctx: guest)
    assert "reserved for contributors" in mcp_server.deposer_url("https://example.org")["erreur"]
    assert "contributors" in mcp_server.noter("blocage")["erreur"]
    assert mcp_server.suivre_dossier("ace")["dossier"] == "technologie-capacites"
    assert [d["slug"] for d in mcp_server.mes_sujets()["dossiers_suivis"]] == ["technologie-capacites"]
    assert all(d["nb_notes"] == 0 for d in live.overview(conn, guest))
    assert "notes_terrain_internes" not in mcp_server.dossier("finance")
    for prompt in (mcp_server.point_negociation("ACE"), mcp_server.position_pays("Brésil", "fossiles"),
                   mcp_server.preparer_rencontre("ACE"), mcp_server.plaidoyer("adaptation"),
                   mcp_server.decouvrir_sujet("éducation au climat"), mcp_server.quoi_de_neuf()):
        assert "not adopted" in prompt                    # invité : anglais par défaut
    assert "non adopté" in mcp_server.point_negociation("ACE", langue="fr")


def test_admin_deletes_guest_accounts(env):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from cop_corpus import alerts, auth, db, live, web, web_live
    importlib.reload(web)
    importlib.reload(web_live)
    conn = db.get()
    code = auth.invite_settings(conn)["code"]
    guest, _, err = auth.signup(conn, code, "g@test.org")
    assert not err
    alerts.add_subscription(conn, guest["id"], "fossiles", "fossil fuels")
    live.follow(conn, guest["id"], "finance")
    colleague, _ = auth.create_user(conn, "c@test.org", "C")              # contributeur : jamais supprimable
    admin_id, _ = auth.create_user(conn, "a@test.org", "A", admin=True)
    auth.set_password(conn, admin_id, "mot-de-passe-admin")
    admin = TestClient(Starlette(routes=[*web.routes, *web_live.routes]), base_url="https://testserver")
    admin.post("/connexion", data={"email": "a@test.org", "password": "mot-de-passe-admin"})
    page = admin.get("/admin").text
    assert f"/admin/utilisateur/{guest['id']}/supprimer" in page and f"/admin/utilisateur/{colleague}/supprimer" not in page
    assert auth.invite_settings(conn)["used"] == 1

    admin.post(f"/admin/utilisateur/{guest['id']}/supprimer")
    assert auth.by_email(conn, "g@test.org") is None
    assert conn.execute("SELECT COUNT(*) FROM subscriptions WHERE user_id=?", (guest["id"],)).fetchone()[0] == 0
    assert live.followed(conn, guest["id"]) == set()
    assert auth.invite_settings(conn)["used"] == 0                       # place libérée
    admin.post(f"/admin/utilisateur/{colleague}/supprimer")
    assert auth.by_id(conn, colleague)
    guest_client = TestClient(Starlette(routes=web.routes), base_url="https://testserver")   # pas admin : refusé
    assert guest_client.post(f"/admin/utilisateur/{colleague}/supprimer").status_code == 403


def test_passwordless_signup_and_email_code_login(env, monkeypatch):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from cop_corpus import auth, db, mailout, web, web_live
    importlib.reload(web)
    importlib.reload(web_live)
    conn = db.get()
    app = Starlette(routes=[*web.routes, *web_live.routes])
    code = auth.invite_settings(conn)["code"]

    # inscription : nom + e-mail, pas de mot de passe ; session d'un an
    phone = TestClient(app, base_url="https://testserver")
    assert 'type="password"' not in phone.get(f"/join/{code}").text
    r = phone.post(f"/join/{code}", data={"code": code, "name": "Ana", "email": "ana@ngo.org"}, follow_redirects=False)
    assert r.status_code == 303 and "max-age=31536000" in r.headers["set-cookie"].lower()
    assert not auth.by_email(conn, "ana@ngo.org")["password_hash"]

    # autre appareil : e-mail → code envoyé par e-mail → connexion
    outbox: list[tuple] = []
    monkeypatch.setattr(mailout, "send", lambda to, subject, text, **kw: outbox.append((to, subject, text)) or True)
    laptop = TestClient(app, base_url="https://testserver")
    page = laptop.post("/connexion", data={"email": "ana@ngo.org"}).text
    assert "Code sent" in page and 'name="code"' in page and outbox[-1][0] == "ana@ngo.org"
    sent_code = "".join(c for c in outbox[-1][1] if c.isdigit())[-6:]
    assert "Wrong or expired" in laptop.post("/connexion", data={"email": "ana@ngo.org", "code": "000000" if sent_code != "000000" else "111111"}).text
    assert "wait a minute" in laptop.post("/connexion", data={"email": "ana@ngo.org"}).text    # pas de rafale de codes
    r = laptop.post("/connexion", data={"email": "ana@ngo.org", "code": sent_code}, follow_redirects=False)
    assert r.status_code == 303 and laptop.get("/bienvenue").status_code == 200
    assert not auth.check_login_code(conn, auth.by_email(conn, "ana@ngo.org"), sent_code)          # code consommé

    # compte avec mot de passe : on demande le mot de passe, pas de code
    uid, _ = auth.create_user(conn, "c@team.org", "C")
    auth.set_password(conn, uid, "mot-de-passe-long")
    n = len(outbox)
    assert 'type="password"' in TestClient(app, base_url="https://testserver").post("/connexion", data={"email": "c@team.org"}).text
    assert len(outbox) == n
    # inconnu, et service d'e-mail non configuré
    assert "No account" in TestClient(app, base_url="https://testserver").post("/connexion", data={"email": "x@y.org"}).text
    monkeypatch.setattr(mailout, "send", lambda *a, **kw: False)
    auth.create_user(conn, "z@ngo.org", "Z", restricted=False)
    assert "be sent yet" in TestClient(app, base_url="https://testserver").post("/connexion", data={"email": "z@ngo.org"}).text


def test_open_signup_with_password(env):
    """Page d'accueil : n'importe qui crée un compte invité avec un mot de passe ; l'admin peut fermer."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from cop_corpus import auth, db, web, web_live
    importlib.reload(web)
    importlib.reload(web_live)
    conn = db.get()
    app = Starlette(routes=[*web.routes, *web_live.routes])
    visitor = TestClient(app, base_url="https://testserver")
    home = visitor.get("/").text
    assert 'id="signup"' in home and 'name="password"' in home and "Sign in" in home     # accueil = page d'arrivée
    form = {"email": "Lea@media.org", "organisation": "Media", "password": "mot-de-passe-long", "password2": "mot-de-passe-long"}
    assert "do not match" in visitor.post("/join", data={**form, "password2": "autre-chose-long"}).text
    assert "at least 10" in visitor.post("/join", data={**form, "password": "court", "password2": "court"}).text
    assert "invalid" in visitor.post("/join", data={**form, "website": "http://spam"}).text.lower()   # champ piège
    r = visitor.post("/join", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/bienvenue"
    lea = auth.by_email(conn, "lea@media.org")
    assert lea["source"] == "inscription" and lea["password_hash"] and auth.is_guest(lea) and lea["organisation"] == "Media"
    assert auth.open_signup_settings(conn)["used"] == 1 and auth.invite_settings(conn)["used"] == 0
    other = TestClient(app, base_url="https://testserver")                     # se reconnecte avec le mot de passe
    assert 'type="password"' in other.post("/connexion", data={"email": "lea@media.org"}).text
    assert other.post("/connexion", data={"email": "lea@media.org", "password": "mot-de-passe-long"},
                      follow_redirects=False).status_code == 303

    # avec invitation : e-mail seulement, pas de mot de passe
    code = auth.invite_settings(conn)["code"]
    page = TestClient(app, base_url="https://testserver").get(f"/join/{code}").text
    assert 'name="password"' not in page and 'name="organisation"' in page
    u, _, err = auth.signup(conn, code, "inv@ngo.org")
    assert not err and u["source"] == "invitation" and not u["password_hash"]

    # fermeture par l'admin
    auth.set_open_signup(conn, open_=False)
    third = TestClient(app, base_url="https://testserver")
    assert 'name="password"' not in third.get("/").text
    assert "closed" in third.post("/join", data={**form, "email": "x@media.org"}).text
    assert not auth.signup(conn, code, "inv2@ngo.org")[2]                       # l'invitation marche toujours


def test_forgot_password_code(env, monkeypatch):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from cop_corpus import auth, db, mailout, web, web_live
    importlib.reload(web)
    importlib.reload(web_live)
    conn = db.get()
    uid, _ = auth.create_user(conn, "p@ngo.org", "", restricted=False)
    auth.set_password(conn, uid, "ancien-mot-de-passe")
    outbox: list[str] = []
    monkeypatch.setattr(mailout, "send", lambda to, subject, text, **kw: outbox.append(subject) or True)
    c = TestClient(Starlette(routes=[*web.routes, *web_live.routes]), base_url="https://testserver")
    page = c.post("/connexion", data={"email": "p@ngo.org"}).text
    assert 'value="forgot"' in page and not outbox                          # mot de passe demandé, pas de code
    page = c.post("/connexion", data={"email": "p@ngo.org", "action": "forgot"}).text
    assert 'name="code"' in page and len(outbox) == 1
    code = "".join(ch for ch in outbox[-1] if ch.isdigit())[-6:]
    r = c.post("/connexion", data={"email": "p@ngo.org", "code": code}, follow_redirects=False)
    assert r.headers["location"] == "/compte?oubli=1"
    assert "new password" in c.get("/compte?oubli=1").text
    c.post("/compte", data={"password": "nouveau-mot-de-passe", "password2": "nouveau-mot-de-passe"})
    assert auth.check_password(conn, "p@ngo.org", "nouveau-mot-de-passe") and not auth.check_password(conn, "p@ngo.org", "ancien-mot-de-passe")
