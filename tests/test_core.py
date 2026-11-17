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


PAV_PAGE = r'''<html><script>self.__next_f.push([1,"7c:T34,<div class=\"payload-richtext\"><p>Long text</p></div>"])</script>
<script>self.__next_f.push([1,"[{\"id\":1,\"title\":\"Forests and finance\",\"organizer\":null,\"date\":\"2026-11-14\",\"startTime\":\"10:00\",\"endTime\":\"11:00\",\"place\":\"Area B2 Delegation Pavilions › Brazil Pavilion\",\"hub\":\"Area B2 Delegation Pavilions\",\"typeLabel\":\"Panel\",\"themes\":[],\"descriptionHtml\":\"<div><p>Tropical rainforest protection and REDD+.</p><p>Moderator: Jane Doe, WRI</p><p>Speakers:</p><ul><li>H.E. John Smith, Minister of Environment</li><li>Ana Lima, Amazon Fund</li></ul><p>Agenda</p><p>10:00 opening</p></div>\",\"zone\":\"blue\",\"nitelikIdleri\":[\"9\"],\"iptal\":false},{\"id\":2,\"title\":\"Steel\",\"organizer\":\"X\",\"date\":\"2026-11-15\",\"startTime\":\"12:00\",\"endTime\":\"13:00\",\"place\":\"Resilient Cities Hub › Resilient Cities Programming Area 2\",\"hub\":\"Resilient Cities\",\"typeLabel\":null,\"themes\":[],\"descriptionHtml\":\"$7c\",\"zone\":\"green\",\"nitelikIdleri\":[],\"iptal\":false}],\"suzgecGruplari\":[{\"id\":\"15\",\"key\":\"keyword\",\"label\":\"Keyword\",\"secenekler\":[{\"id\":\"9\",\"key\":\"forests\",\"label\":\"Forests\"}]}]"])</script></html>'''


def test_pavillons_parse_speakers_and_search(env):
    from cop_corpus import pavillons

    importlib.reload(pavillons)
    evs = pavillons.parse_cop31tr(PAV_PAGE, "blue")
    by_id = {e["key"]: e for e in evs}
    e = by_id["cop31tr:blue:1"]
    assert e["pavilion"] == "Brazil Pavilion" and e["keywords"] == ["Forests"]
    assert {"role": "moderator", "name": "Jane Doe, WRI"} in e["speakers"]
    assert [s["name"] for s in e["speakers"] if s["role"] == "speaker"] == [
        "H.E. John Smith, Minister of Environment", "Ana Lima, Amazon Fund"]          # « Agenda » arrête la liste
    assert by_id["cop31tr:blue:2"]["description"] == "Long text"                       # référence RSC résolue
    pavillons.ensure_schema(env)
    pavillons._store(env, evs, ["cop31.tr"], "2026-11-17T00:00:00Z")
    pavillons._reindex(env)
    from cop_corpus import db
    db.meta_set(env, "pav_synced_at", "2026-11-17T00:00:00Z")
    out = pavillons.query(env, sujet="rainforest, deforestation", mode="mots")
    assert out["pavillons_concernes"] == 1 and out["par_pavillon"][0]["pavillon"] == "Brazil Pavilion"
    assert pavillons.query(env, pavillon="Brazil")["evenements_trouves"] == 1


def _pt(rel, a=3, b=3):
    return {"point": "p", "a_position": "x", "b_position": "y", "a_force": a, "b_force": b, "relation": rel,
            "explication": ""}


def test_soumissions_verdict_rules():
    from cop_corpus.soumissions import verdict

    assert verdict([])[0] == "sans_objet"
    # Points d'un seul côté ou en langage faible : pas de point central.
    assert verdict([_pt("un_seul", 4, 0), _pt("accord", 2, 4)])[0] == "sans_objet"
    assert verdict([_pt("accord"), _pt("accord"), _pt("partiel")])[0] == "aligne"   # 1/3 partiel < 50 %
    assert verdict([_pt("accord"), _pt("partiel")])[0] == "partiel"                # 1/2 partiel = 50 %
    assert verdict([_pt("accord"), _pt("conflit", 1, 4)])[0] == "partiel"          # conflit secondaire
    assert verdict([_pt("conflit"), _pt("conflit"), _pt("accord"), _pt("accord")])[0] == "non_aligne"
    # 1 conflit central : seuil de 40 % d'accord sur les points centraux.
    two_of_five = [_pt("conflit"), _pt("accord"), _pt("accord"), _pt("partiel"), _pt("partiel")]
    assert verdict(two_of_five)[0] == "non_aligne"                                  # 40 % ≤ 40 %
    three_of_five = [_pt("conflit"), _pt("accord"), _pt("accord"), _pt("accord"), _pt("partiel")]
    v, counts = verdict(three_of_five)                                              # 60 % > 40 %
    assert v == "partiel" and counts["points_centraux"] == 5 and "40 %" in counts["regle"]
    # Les points d'un seul côté n'entrent jamais dans le verdict.
    assert verdict([_pt("accord"), _pt("un_seul", 4, 0), _pt("un_seul", 0, 4)])[0] == "aligne"


def test_soumissions_normalisation():
    from cop_corpus import soumissions as sm

    assert sm.parse_date("2026-11-17T10:29:13Z") == "2026-11-17"
    assert sm.parse_date("11/3/2026 2:28:00 PM") == "2026-11-03"
    assert sm.parse_date("") is None
    assert sm.norm_session("COP 31") == "COP31" and sm.norm_session("N/A") is None
    assert sm.co_submitters("Republic of Korea", "Australia") == ["Republic of Korea"]
    assert sm.co_submitters("Australia", "Australia") == []
    assert sm.co_submitters("Uruguay on behalf of G77 and China \nSubmission on the 2026 Review", "Uruguay") == ["G77 and China"]
    assert sm.co_submitters("United Republic of Tanzania - Just Transition Review, Mechanism", "United Republic of Tanzania") == []
    assert sm.co_submitters("on behalf of the Least Developed Countries (LDCs) Group", "Timor-Leste") == [
        "Least Developed Countries (LDCs) Group"]
    assert sm.speaks_for({"entity": "Saudi Arabia", "co_submitters": '["LMDC"]'}) == "LMDC"
    assert sm.speaks_for({"entity": "Australia", "co_submitters": '["Republic of Korea"]'}) == "Australia"
    assert sm.entity_kind("pARTY", "X", set()) == "party"
    assert sm.entity_kind("", "Jordan", {"jordan"}) == "party"
    assert sm.entity_kind("NGO", "CAN", set()) == "observer"
    assert sm.is_document("a.PDF") and not sm.is_document("53_29_131218799232961536-") and not sm.is_document("x.jpg")
    assert " " not in sm.download_url("/submissions/Archive/a b.pdf").split("?", 1)[1]


def test_soumissions_harvest_analyse_compare(env, monkeypatch):
    """Index de l'API → documents niveau 2/5 ; analyse : citation inventée rejetée, positions alimentées ;
    comparaison par noms de Parties dans leur appel commun, verdict calculé par les règles."""
    import json

    from cop_corpus import fetch, live, llm, soumissions as sm

    conn = env
    live._ready.clear()
    calls = [{"Id": 10, "Title": "Views on GGA indicators", "Issue": "GGA", "SessionName": "SB 64", "Mandate": "N/A",
              "Deadline": None, "Themes": ["Adaptation"], "Bodies": ["SBSTA"], "TargetAudience": ["All Parties"],
              "CallType": "CallForSubmission", "OpenCall": False, "IsUpcomingSession": False,
              "ModifiedDate": "2026-03-01T00:00:00Z"}]
    eu_text = ("The European Union urges Parties to adopt a limited set of headline indicators. " * 3 +
               "The EU supports means of implementation indicators that are qualitative. " * 3)
    lmdc_text = ("The LMDC demand quantified finance indicators for developed countries. " * 3 +
                 "The LMDC cannot accept headline indicators that burden developing countries. " * 3)
    subs = [
        {"Id": 1, "CallForSubmissionId": 10, "Title": "EU submission GGA", "FileName": "EU GGA.txt",
         "ServerRelativeURL": "/submissions/Archive/EU GGA.txt", "Language": "English",
         "SubmissionDate": "2026-04-02T10:00:00Z", "Description": "", "Entity": "European Union", "EntityType": "Party"},
        {"Id": 2, "CallForSubmissionId": 10, "Title": "LMDC GGA", "FileName": "LMDC.txt",
         "ServerRelativeURL": "/submissions/Archive/LMDC.txt", "Language": "English",
         "SubmissionDate": "4/1/2026 9:00:00 AM", "Description": "on behalf of the LMDC", "Entity": "Bolivia",
         "EntityType": ""},
        {"Id": 3, "CallForSubmissionId": 10, "Title": "Bolivia earlier", "FileName": "b.pdf",
         "ServerRelativeURL": "/submissions/Archive/b.pdf", "Language": "Spanish", "SubmissionDate": "2025-01-01",
         "Description": "", "Entity": "Bolivia", "EntityType": "Party"},
        {"Id": 4, "CallForSubmissionId": 10, "Title": "img", "FileName": "photo.jpg",
         "ServerRelativeURL": "/submissions/Archive/photo.jpg", "Language": "English",
         "SubmissionDate": "2026-04-03T00:00:00Z", "Description": "", "Entity": "WWF", "EntityType": "NGO"},
    ]
    files = {"EU GGA.txt": eu_text, "LMDC.txt": lmdc_text}

    class FakeClient:
        def get(self, url, *_, **__):
            if url.endswith("/CallForSubmissions"):
                return fetch.FetchResult(url=url, status=200, content=json.dumps(calls).encode(), mime="application/json")
            if url.endswith("/Submissions"):
                return fetch.FetchResult(url=url, status=200, content=json.dumps(subs).encode(), mime="application/json")
            name = next((k for k in files if k.replace(" ", "%20") in url), None)
            if not name:
                return fetch.FetchResult(url=url, status=404)
            return fetch.FetchResult(url=url, status=200, content=files[name].encode(), mime="application/octet-stream")

    assert sm.sync_index(conn, FakeClient())["nouvelles"] == 4
    assert conn.execute("SELECT status FROM sub_items WHERE id=4").fetchone()[0] == "skipped"
    assert conn.execute("SELECT kind FROM sub_items WHERE id=2").fetchone()[0] == "party"  # type vide, nom connu
    got = sm.download_pending(conn, FakeClient(), limit=10, since="2026-01-01")
    assert got.get("indexed") == 2
    eu = conn.execute("SELECT * FROM documents WHERE org='European Union'").fetchone()
    assert eu["level"] == "2" and eu["doc_type"] == "soumission" and eu["session"] == "SB64"
    assert eu["published"] == "2026-04-02" and "European Union" in eu["title"] and eu["symbol"] == "UNFCCC-SUB/1"

    def fake_llm(conn_, **k):
        if k["purpose"] == "soumission-analyse":
            eu_doc = "European Union" in k["user"]
            return {"summary": "r", "summary_en": "s", "matters": [
                {"matter": "headline indicators", "position": "p", "strength": 4, "verb": "urges",
                 "stance": "soutient", "dossier": "adaptation",
                 "quote": "urges Parties to adopt a limited set of headline indicators" if eu_doc
                 else "cannot accept headline indicators that burden developing countries"},
                {"matter": "invented", "position": "p", "strength": 3, "verb": "x", "stance": "soutient",
                 "dossier": "adaptation", "quote": "this sentence is not in the document at all"}]}
        return {"points": [
            {"point": "headline indicators", "a_position": "a", "b_position": "b", "a_force": 4, "b_force": 4,
             "relation": "conflit", "explication": "e"},
            {"point": "MoI indicators", "a_position": "a", "b_position": "b", "a_force": 3, "b_force": 4,
             "relation": "partiel", "explication": "e"},
            {"point": "finance", "a_position": "", "b_position": "b", "a_force": 0, "b_force": 4,
             "relation": "un_seul", "explication": "e"}]}

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(llm, "json_call", fake_llm)
    a = sm.analyze(conn, eu["id"])
    assert len(a["points"]) == 1 and a["points_rejetes"] == 1 and a["points"][0]["force_libelle"] == "exige"
    pos = conn.execute("SELECT * FROM positions WHERE source_kind='soumission'").fetchall()
    assert len(pos) == 1 and pos[0]["actor"] == "EU" and pos[0]["said_on"] == "2026-04-02"

    da, db_, common = sm.resolve_pair(conn, "European Union", "LMDC")  # co-auteur trouvé via Description
    assert common == 10 and da == eu["id"]
    out = sm.compare(conn, da, db_)
    # 2 points centraux : 1 conflit, 0 % d'accord → non alignées ; le point d'un seul côté ne compte pas.
    assert out["indication"] == "non_aligne" and out["decompte"]["un_seul_cote"] == 1
    assert out["points"][0]["relation"] == "conflit"
    # Ordre inversé : même verdict (cache), positions échangées.
    rev = sm.compare(conn, db_, da)
    assert rev["indication"] == "non_aligne" and rev["a"]["doc_id"] == db_
    assert conn.execute("SELECT COUNT(*) FROM sub_comparisons").fetchone()[0] == 1
    st = sm.stats(conn)
    assert st["indexees"] == 2 and st["analysees"] == 2 and st["comparaisons"] == 1


def test_actor_resolver_aliases():
    from cop_corpus.positions import normalize_actor as n

    assert n("European Union") == n("UE") == n("EU") == "EU"
    assert n("the Least Developed Countries (LDCs) Group") == n("PMA") == "LDC"
    assert n("Landlocked Developing Countries (LLDCs)") == "LLDC" != n("LDC")
    assert n("African Group of Negotiators (AGN)") == n("Groupe africain") == "AGN"
    assert n("Group of 77 and China") == "G77 and China" and n("LMDC (group)") == "LMDC"
    assert n("Congo (Democratic Republic of the)") != n("Congo")
    assert n("Mali") == "Mali" and n("Somalia") == "Somalia"
    assert n("Korea (Republic of)") == "Korea (Republic of)" and n("Moldova (Republic of)") == "Moldova (Republic of)"
    assert n("Climate Action Network (CAN)") == "Climate Action Network"


def test_soumissions_exact_names_search_alerts_reanalysis(env, monkeypatch):
    """B1 : « Mali » ne trouve jamais la Somalie, « Niger » jamais le Nigeria ; B3 : soumissions exclues de rechercher
    par défaut ; alertes : pas d'alerte sur le stock ancien ; I3 : ré-analyse sans doublon dans positions."""
    import json

    from cop_corpus import alerts, auth, fetch, live, llm, search, soumissions as sm

    conn = env
    live._ready.clear()
    calls = [{"Id": 1, "Title": "Views on adaptation finance", "Issue": "", "SessionName": "SB 65", "Mandate": "N/A",
              "Deadline": None, "Themes": [], "Bodies": [], "TargetAudience": [], "CallType": "CallForSubmission",
              "OpenCall": True, "IsUpcomingSession": True, "ModifiedDate": "2026-09-01T00:00:00Z"}]
    names = {"Somalia": "2026-09-01T00:00:00Z", "Nigeria": "2026-09-02T00:00:00Z", "Mali": "2019-05-01T00:00:00Z"}
    subs = [{"Id": i + 1, "CallForSubmissionId": 1, "Title": f"{p} views", "FileName": f"{p}.txt",
             "ServerRelativeURL": f"/submissions/Archive/{p}.txt", "Language": "English", "SubmissionDate": d,
             "Description": "", "Entity": p, "EntityType": "Party"} for i, (p, d) in enumerate(names.items())]
    text = "{p} urges developed countries to triple adaptation finance by 2030 through grants. " * 6

    class FakeClient:
        def get(self, url, *_, **__):
            if url.endswith("/CallForSubmissions"):
                return fetch.FetchResult(url=url, status=200, content=json.dumps(calls).encode(), mime="application/json")
            if url.endswith("/Submissions"):
                return fetch.FetchResult(url=url, status=200, content=json.dumps(subs).encode(), mime="application/json")
            p = url.rsplit("/", 1)[1].split(".")[0]
            return fetch.FetchResult(url=url, status=200, content=text.format(p=p).encode(), mime="text/plain")

    uid, _ = auth.create_user(conn, "a@x.fr", "A")
    alerts.add_subscription(conn, uid, "finance", "adaptation finance")
    alerts.run(conn, notify=False)  # curseur initial
    sm.sync_index(conn, FakeClient())
    sm.download_pending(conn, FakeClient(), limit=10)
    # Noms exacts : la Somalie et le Nigeria ont soumis, pas le Mali (2019) ni le Niger.
    assert [r["auteur"] for r in sm.by_entity(conn, "Somalia")] == ["Somalia"]
    assert [r["auteur"] for r in sm.by_entity(conn, "Mali")] == ["Mali"]
    with pytest.raises(sm.UnknownActor) as err:
        sm.resolve_pair(conn, "Niger", "Somalia")
    assert "Nigeria" in str(err.value)  # proposé comme nom proche, jamais choisi en silence
    # Stock historique exclu par défaut ; dépôt récent visible partout (nouveautés, recherche).
    assert not search.search(conn, "adaptation finance", {}, None, limit=10)
    assert not search.latest(conn, None)
    conn.execute("UPDATE documents SET published=date('now','-2 days') WHERE org='Somalia'")
    assert [d["organisation"] for d in search.latest(conn, None)] == ["Somalia"]
    assert len({h["doc_id"] for h in search.search(conn, "adaptation finance", {}, None, limit=10)}) == 1
    conn.execute("UPDATE documents SET published='2026-09-01' WHERE org='Somalia'")
    assert search.search(conn, "adaptation finance", {"doc_types": ["soumission"]}, None, limit=10)
    assert search.search(conn, "adaptation finance", {"party": "Somalia"}, None, limit=10)
    # Alertes : seulement les dépôts récents (ici aucun : dates de 2019 et septembre 2026, avant les 14 derniers jours).
    alerts.run(conn, notify=False)
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
    # Ré-analyse : pas de doublon dans positions ; changement de version du prompt → cache invalidé.
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    n_calls = []

    def fake_llm(conn_, **k):
        n_calls.append(k["purpose"])
        return {"summary": "r", "summary_en": "s", "matters": [
            {"matter": "adaptation finance", "position": "p", "strength": 4, "verb": "urges", "stance": "priorité",
             "dossier": "adaptation", "quote": "urges developed countries to triple adaptation finance"}]}

    monkeypatch.setattr(llm, "json_call", fake_llm)
    som = conn.execute("SELECT doc_id FROM sub_items WHERE entity='Somalia'").fetchone()[0]
    mali = conn.execute("SELECT doc_id FROM sub_items WHERE entity='Mali'").fetchone()[0]
    sm.analyze(conn, som)
    sm.analyze(conn, som, force=True)
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE doc_id=?", (som,)).fetchone()[0] == 1
    sm.analyze(conn, mali)  # 2019, appel de la session en cours : versé quand même (appel « upcoming »)
    assert sm.analyze(conn, som) and len(n_calls) == 3  # en cache
    monkeypatch.setattr(sm, "PROMPT_VERSION", "2")
    sm.analyze(conn, som)
    assert len(n_calls) == 4  # nouvelle version : recalcul


def test_positions_tool_exact_actor(env):
    """N2 : positions(acteur=) n'élargit jamais à un autre acteur ; il propose des noms proches."""
    from cop_corpus import live, mcp_server

    conn = env
    live._ready.clear()
    live.ensure_schema(conn)
    conn.execute("INSERT INTO positions(key,actor,issue,stance,summary,quote,source_kind,created_at,access) "
                 "VALUES('k1','Somalia','finance','soutient','s','q','extraction','2026-11-10','public')")
    conn.commit()
    out = mcp_server.positions(acteur="Mali")
    assert out["positions"] == [] and "Somalia" not in out["message"].split("noms proches")[0]
    assert len(mcp_server.positions(acteur="Somalia")["positions"]) == 1
    assert "Somalia" in mcp_server.positions(acteur="Somali")["message"]


def test_soumissions_dossier_link(env, monkeypatch):
    """Rattachement appel → dossier : règle, points analysés (seuil), manuel prioritaire ; rubrique compacte ;
    filtre dossier= ; comparer avec dossier= ; matrice lue dans le cache."""
    import json

    from cop_corpus import fetch, live, llm, mcp_server, soumissions as sm

    conn = env
    live._ready.clear()
    sm.ensure_schema(conn)
    rule = {"title": "Views on indicators for the global goal on adaptation", "issue": "", "themes": "[]"}
    assert sm.rule_dossiers(rule) == {"adaptation": 1.0}
    # Règle spécifique d'un dossier : efface les correspondances larges (« adaptation » dans un appel pertes et préjudices).
    assert sm.rule_dossiers({"title": "Loss and damage: adaptation limits", "issue": "", "themes": "[]"}) == {
        "pertes-prejudices": 1.0}
    assert sm.rule_dossiers({"title": "Review of the just transition work programme and finance", "issue": "",
                             "themes": "[]"}) == {"transition-juste": 1.0}
    assert sm.rule_dossiers({"title": "Voluntary submissions", "issue": "Unsolicited", "themes": "[]"}) == {}
    # R1 : article 6 de la Convention = ACE, pas les marchés carbone.
    assert sm.rule_dossiers({"title": "Views on Action for Climate Empowerment under Article 6 of the Convention",
                             "issue": "", "themes": "[]"}) == {"technologie-capacites": 1.0}
    assert sm.rule_dossiers({"title": "Views on Article 6, paragraph 4 mechanism", "issue": "", "themes": "[]"}) == {
        "article-6": 1.0}
    # R2 : les thèmes du portail ne s'ajoutent jamais à un rattachement spécifique ; dernier recours à 0.6.
    assert sm.rule_dossiers({"title": "Just transition work programme", "issue": "",
                             "themes": '["Technology", "Transparency"]'}) == {"transition-juste": 1.0}
    assert sm.rule_dossiers({"title": "Call for inputs", "issue": "", "themes": '["Gender and climate change"]'}) == {
        "genre": 0.6}
    # R3 : « reporting » et « agenda » sont larges ; adaptation communication est spécifique.
    assert sm.rule_dossiers({"title": "Adaptation communications: reporting guidance", "issue": "", "themes": "[]"}) == {
        "adaptation": 1.0}
    assert sm.rule_dossiers({"title": "Views on the provisional agenda item on the GGA", "issue": "", "themes": "[]"}) == {
        "adaptation": 1.0}
    # Le point de l'ordre du jour prime sur un titre descriptif ; les invités (« Financial Mechanism ») ne comptent pas.
    assert sm.rule_dossiers({"title": "Submissions on gender-responsive finance and available capacity-building",
                             "issue": "Gender and climate change", "themes": "[]"}) == {"genre": 1.0}
    assert sm.rule_dossiers({"title": "Parties, constituted bodies and the operating entities of the Financial Mechanism "
                                      "are invited", "issue": "Second review of the PCCB", "themes": "[]"}) == {
        "technologie-capacites": 1.0}

    calls = [
        {"Id": 1, "Title": "Views on GGA indicators", "Issue": "", "SessionName": "SB 65", "Mandate": "N/A",
         "Deadline": None, "Themes": [], "Bodies": [], "TargetAudience": [], "CallType": "CallForSubmission",
         "OpenCall": True, "IsUpcomingSession": True, "ModifiedDate": "2026-09-01T00:00:00Z"},
        {"Id": 2, "Title": "Voluntary submissions SB 65", "Issue": "Unsolicited", "SessionName": "SB 65",
         "Mandate": "N/A", "Deadline": None, "Themes": [], "Bodies": [], "TargetAudience": [],
         "CallType": "CallForSubmission", "OpenCall": True, "IsUpcomingSession": True,
         "ModifiedDate": "2026-09-01T00:00:00Z"}]
    subs = []
    for i, (call, ent, desc) in enumerate([(1, "European Union", ""), (1, "Saudi Arabia", "on behalf of the LMDC"),
                                           (2, "European Union", ""), (2, "Palau", "on behalf of AOSIS")]):
        subs.append({"Id": i + 1, "CallForSubmissionId": call, "Title": f"{ent} {call}", "FileName": f"f{i}.txt",
                     "ServerRelativeURL": f"/submissions/Archive/f{i}.txt", "Language": "English",
                     "SubmissionDate": f"2026-09-0{i + 1}T00:00:00Z", "Description": desc, "Entity": ent,
                     "EntityType": "Party"})
    text = "We call for robust indicators on means of implementation for adaptation finance. " * 6

    class FakeClient:
        def get(self, url, *_, **__):
            if url.endswith("/CallForSubmissions"):
                return fetch.FetchResult(url=url, status=200, content=json.dumps(calls).encode(), mime="application/json")
            if url.endswith("/Submissions"):
                return fetch.FetchResult(url=url, status=200, content=json.dumps(subs).encode(), mime="application/json")
            return fetch.FetchResult(url=url, status=200, content=(text + url).encode(), mime="text/plain")

    sm.sync_index(conn, FakeClient())
    sm.download_pending(conn, FakeClient(), limit=10)
    assert len({r[0] for r in conn.execute("SELECT doc_id FROM sub_items")}) == 4
    assert sm.dossier_call_ids(conn, "adaptation") == [1]  # appel 2 : aucune règle, pas encore de points
    # Points analysés : 5 points « adaptation » sur 6 (≈ 83 % ≥ 40 %) → appel 2 rattaché par les points.
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    def fake_llm(conn_, **k):
        if k["purpose"] == "soumission-comparaison":
            return {"points": [{"point": "MoI indicators", "a_position": "a", "b_position": "b", "a_force": 3,
                                "b_force": 4, "relation": "accord", "explication": "e"}]}
        dossiers = ["adaptation", "adaptation", "finance"] if "Palau" in k["user"] else ["adaptation"] * 3
        return {"summary": "r", "summary_en": "s", "matters": [
            {"matter": f"m{j}", "position": "p", "strength": 3, "verb": "call for", "stance": "soutient",
             "dossier": d, "quote": "We call for robust indicators on means of implementation"} for j, d in enumerate(dossiers)]}

    monkeypatch.setattr(llm, "json_call", fake_llm)
    for d in [r[0] for r in conn.execute("SELECT doc_id FROM sub_items WHERE call_id=2")]:
        sm.analyze(conn, d)
    sm.map_calls(conn)
    row = conn.execute("SELECT methode, score FROM sub_call_dossiers WHERE call_id=2").fetchone()
    assert row["methode"] == "points" and row["score"] == 0.83
    # Manuel : remplace et survit au recalcul.
    sm.set_manual(conn, 2, ["finance"])
    sm.map_calls(conn)
    assert [tuple(r) for r in conn.execute("SELECT dossier, methode FROM sub_call_dossiers WHERE call_id=2")] == [
        ("finance", "manuel")]
    with pytest.raises(ValueError):
        sm.set_manual(conn, 2, ["dossier-inexistant"])
    # Rubrique du dossier : compacte, auteurs canoniques (groupe pour une Partie qui soumet en son nom).
    sec = sm.dossier_section(conn, "adaptation")
    assert sec["nb_appels_lies"] == 1 and sec["appels"][0]["auteurs_parties"] == ["LMDC", "EU"]
    assert "alignements_connus" not in sec["appels"][0]
    # Filtre dossier= (alias accepté) et comparaison restreinte au dossier.
    assert [c["appel"] for c in mcp_server.soumissions(dossier="GGA")["appels"]] == [1]
    assert [c["appel"] for c in sm.list_calls(conn, dossier="gga")] == [1]
    da, db_, common = sm.resolve_pair(conn, "EU", "LMDC", dossier="gga")
    assert common == 1
    with pytest.raises(sm.UnknownActor):
        sm.resolve_pair(conn, "EU", "AOSIS", dossier="gga")  # l'AOSIS n'a soumis que hors dossier
    # Matrice : lecture du cache ; une paire comparée apparaît ensuite dans la rubrique.
    m = sm.alignment_matrix(conn, "adaptation", ["UE", "LMDC", "AOSIS"])
    assert m["comparees"] == [] and m["a_calculer"] == [{"a": "EU", "b": "LMDC", "appel": 1}]
    assert m["absents_du_dossier"] == ["AOSIS"] and m["sans_appel_commun"] is None
    sm.compare(conn, da, db_)
    m = sm.alignment_matrix(conn, "adaptation", ["EU", "LMDC"])
    assert m["comparees"][0]["indication"] == "aligne" and not m["a_calculer"]
    assert sm.dossier_section(conn, "adaptation")["appels"][0]["alignements_connus"][0]["indication"] == "aligne"
    assert mcp_server.dossier("gga")["soumissions"]["nb_appels_lies"] == 1


def test_groupes_sources_parsers_and_membership(env, monkeypatch):
    """Groupes : listes sourcées et datées, contrôle du nombre de membres (écart signalé), groupes sans liste
    vérifiée jamais complétés ; une Partie dans plusieurs groupes ; filtre groupe= exact (Niger ≠ Nigeria)."""
    from cop_corpus import fetch, groupes, ingest, search

    conn = env
    conn.execute("CREATE TABLE IF NOT EXISTS emissions(iso3 TEXT, country TEXT, year INT, indicator TEXT, value REAL, "
                 "unit TEXT, source TEXT)")
    conn.executemany("INSERT INTO emissions(iso3,country) VALUES(?,?)", [
        ("CHN", "China"), ("BRA", "Brazil"), ("IND", "India"), ("ZAF", "South Africa"), ("NER", "Niger"),
        ("NGA", "Nigeria"), ("TUV", "Tuvalu"), ("NRU", "Nauru"), ("LCA", "Saint Lucia"), ("FRA", "France")])
    # Parseurs sur des extraits de la forme des pages réelles.
    numbered = "<div>1. Afghanistan * 3. Niger * 2. Plurinational State of Bolivia Also Least Developed Country</div>"
    assert [n for n, _ in groupes.parse_ohrlls_numbered(numbered)] == ["Afghanistan", "Plurinational State of Bolivia", "Niger"]
    eu = "<p>France EU Member State since 1958, Euro area member since 1999 Croatia EU Member State since 2013, and</p>"
    assert groupes.parse_eu(eu) == [("France", "1958"), ("Croatia", "2013")]
    aosis = "<p>Pacific 3 🇹🇻 Tuvalu 🇳🇷 Naoero Caribbean 1 🇱🇨 St Lucia PALAU AOSIS Chair</p>"
    assert [n for n, _ in groupes.parse_aosis(aosis)] == ["Tuvalu", "Naoero", "St Lucia"]
    assert groupes.to_iso3("Naoero", groupes.iso_index(conn)) == "NRU"
    cat = [
        {"id": "LDC", "nom": "PMA", "type": "onu", "source": "https://un.test/ldc", "parser": "ohrlls_numbered",
         "attendu": 3, "alias": ["PMA"]},
        {"id": "AOSIS", "nom": "AOSIS", "type": "negociation", "source": "https://aosis.test", "parser": "aosis",
         "attendu": 39},
        {"id": "BASIC", "nom": "BASIC", "type": "coalition", "source": "https://unfccc.test", "releve": "2026-11-17",
         "membres": ["Brazil", "South Africa", "India", "China"], "attendu": 4, "citation": "…Brazil, South Africa…"},
        {"id": "LMDC", "nom": "LMDC", "type": "coalition", "source": "https://unfccc.test", "attendu": 24}]
    monkeypatch.setattr(groupes, "catalog", lambda: cat)
    pages = {"https://un.test/ldc": numbered, "https://aosis.test": aosis}

    class FakeClient:
        def get(self, url, *_, **__):
            return fetch.FetchResult(url=url, status=200, content=pages[url].encode(), mime="text/html")

    out = groupes.sync(conn, FakeClient())
    assert out["LDC"].startswith("verifie 3") and out["AOSIS"].startswith("ecart 3") and out["LMDC"] == "non_verifie"
    assert "Afghanistan" in out["LDC"]  # nom non résolu (absent de la table pays du test) : signalé, pas écarté
    info = groupes.query(conn, groupe="LMDC")
    assert info["membres"] == [] and "avertissement" in info and info["membres_annonces"] == 24
    assert groupes.query(conn, groupe="AOSIS")["statut"] == "ecart"
    assert [g["groupe"] for g in groupes.query(conn, partie="China")["groupes"]] == ["BASIC"]
    assert groupes.query(conn, groupe="PMA")["groupe"] == "LDC"
    m = {x["nom"]: x for x in groupes.members(conn, "BASIC")}
    assert m["China"]["iso3"] == "CHN" and m["China"]["recupere_le"] == "2026-11-17"
    # Source en échec : la dernière liste connue est gardée, statut « erreur ».
    pages.pop("https://un.test/ldc")

    class Broken(FakeClient):
        def get(self, url, *_, **__):
            if url not in pages:
                return fetch.FetchResult(url=url, status=503)
            return super().get(url)

    groupes.sync(conn, Broken())
    assert groupes.query(conn, groupe="LDC")["statut"] == "erreur" and len(groupes.members(conn, "LDC")) == 3
    # Filtre groupe= : élément exact de la liste des Parties (Niger membre des PMA, pas le Nigeria).
    for country in ("NER", "NGA"):
        ingest.ingest_bytes(conn, f"NDC {country} adaptation finance targets for 2035. ".encode() * 20,
                            url=f"https://ndc.test/{country}", filename=f"{country}.txt",
                            meta={"doc_type": "ndc", "parties": [country], "level": "2"})
    hits = search.search(conn, "adaptation finance", {"group": "LDC"}, None, limit=10)
    assert hits
    docs = {conn.execute("SELECT parties FROM documents WHERE id=?", (h["doc_id"],)).fetchone()[0] for h in hits}
    assert docs == {'["NER"]'}


def test_compare_group_through_members(env, monkeypatch):
    """Groupe sans soumission propre : comparaison avec les soumissions de ses MEMBRES, présentée comme telle."""
    import json

    from cop_corpus import fetch, groupes, live, llm, mcp_server, soumissions as sm

    conn = env
    live._ready.clear()
    conn.execute("CREATE TABLE IF NOT EXISTS emissions(iso3 TEXT, country TEXT, year INT, indicator TEXT, value REAL, "
                 "unit TEXT, source TEXT)")
    conn.executemany("INSERT INTO emissions(iso3,country) VALUES(?,?)", [("TUV", "Tuvalu"), ("NPL", "Nepal"), ("MLI", "Mali")])
    monkeypatch.setattr(groupes, "catalog", lambda: [
        {"id": "LDC", "nom": "PMA", "type": "onu", "source": "https://un.test", "releve": "2026-11-17",
         "membres": ["Tuvalu", "Nepal", "Mali"], "attendu": 3, "alias": ["PMA"]}])
    groupes.sync(conn, None)
    calls = [{"Id": 1, "Title": "Views on GGA indicators", "Issue": "", "SessionName": "SB 65", "Mandate": "N/A",
              "Deadline": None, "Themes": [], "Bodies": [], "TargetAudience": [], "CallType": "CallForSubmission",
              "OpenCall": True, "IsUpcomingSession": True, "ModifiedDate": "2026-09-01T00:00:00Z"}]
    subs = [{"Id": i + 1, "CallForSubmissionId": 1, "Title": e, "FileName": f"{e}.txt",
             "ServerRelativeURL": f"/submissions/Archive/{e}.txt", "Language": "English",
             "SubmissionDate": f"2026-09-0{i + 1}T00:00:00Z", "Description": "", "Entity": e, "EntityType": "Party"}
            for i, e in enumerate(["European Union", "Tuvalu", "Nepal"])]

    class FakeClient:
        def get(self, url, *_, **__):
            if url.endswith("/CallForSubmissions"):
                return fetch.FetchResult(url=url, status=200, content=json.dumps(calls).encode(), mime="application/json")
            if url.endswith("/Submissions"):
                return fetch.FetchResult(url=url, status=200, content=json.dumps(subs).encode(), mime="application/json")
            return fetch.FetchResult(url=url, status=200, mime="text/plain",
                                     content=(("We call for robust adaptation indicators. " * 8) + url).encode())

    sm.sync_index(conn, FakeClient())
    sm.download_pending(conn, FakeClient(), limit=10)
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    def fake_llm(conn_, **k):
        if k["purpose"] == "soumission-comparaison":
            return {"points": [{"point": "indicators", "a_position": "a", "b_position": "b", "a_force": 3, "b_force": 3,
                                "relation": "accord", "explication": "e"}]}
        return {"summary": "r", "summary_en": "s", "matters": []}

    monkeypatch.setattr(llm, "json_call", fake_llm)
    out = mcp_server.comparer_soumissions("EU", "PMA")
    assert out["b"]["mode"] == "membres" and out["b"]["couverture"] == "2 membre(s) sur 3 listés"
    assert out["b"]["membres_ayant_soumis"] == ["Nepal", "Tuvalu"] and len(out["comparaisons"]) == 2
    assert "pas une position de groupe" in out["avertissement"]
    g = mcp_server.soumissions(groupe="LDC")
    assert g["soumissions_du_groupe"] == [] and len(g["soumissions_des_membres"]) == 2


def test_groupes_review_fixes(env, monkeypatch):
    """G1 : parenthèses onusiennes jamais simplement retirées (RDC ≠ Congo, Corée du Nord ≠ du Sud) ;
    G2 : statut « par équivalence » ; G4 : nombre attendu variable ; G3 : repli sur les membres."""
    import json

    from cop_corpus import fetch, groupes, live, llm, soumissions as sm

    conn = env
    conn.execute("CREATE TABLE IF NOT EXISTS emissions(iso3 TEXT, country TEXT, year INT, indicator TEXT, value REAL, "
                 "unit TEXT, source TEXT)")
    conn.executemany("INSERT INTO emissions(iso3,country) VALUES(?,?)", [
        ("COG", "Congo"), ("COD", "Democratic Republic of Congo"), ("KOR", "South Korea"), ("PRK", "North Korea"),
        ("JOR", "Jordan"), ("OMN", "Oman"), ("EGY", "Egypt"), ("DZA", "Algeria"), ("ZWE", "Zimbabwe"),
        ("TUV", "Tuvalu"), ("NPL", "Nepal")])
    idx = groupes.iso_index(conn)
    t = groupes.to_iso3
    assert t("Congo (Democratic Republic of the)", idx) == "COD" and t("Congo", idx) == "COG"
    assert t("Korea (Democratic People's Republic of)", idx) == "PRK" and t("Korea (Republic of)", idx) == "KOR"
    assert t("Bahamas (the)", idx) is None or t("Bahamas (the)", idx) == idx.get("bahamas")
    assert t("Hashemite Kingdom of Jordan", idx) == "JOR" and t("Sultanate of Oman", idx) == "OMN"
    assert t("Arab Republic of Egypt", idx) == "EGY" and t("Unknown (Something)", idx) is None
    # G2 / G4
    las = '<a href="/en/aboutlas/Pages/CountryDataDetails.aspx?RID=5">Hashemite Kingdom of Jordan</a>' \
          '<a href="/en/aboutlas/Pages/CountryDataDetails.aspx?RID=12">Sultanate of Oman</a>'
    afr = "<table><tr><td>Algeria</td><td>Egypt</td><td>Zimbabwe</td></tr></table>"
    cat = [{"id": "Arab Group", "nom": "Groupe arabe", "type": "negociation", "source": "https://las.test",
            "parser": "las", "attendu": 2, "equivalence": "Ligue des États arabes"},
           {"id": "AGN", "nom": "AGN", "type": "negociation", "source": "https://un.test", "parser": "un_african_states",
            "attendu": [4, 3], "equivalence": "groupe régional africain de l'ONU"},
           {"id": "LDC", "nom": "PMA", "type": "onu", "source": "https://ldc.test", "releve": "2026-11-17",
            "membres": ["Tuvalu", "Nepal"], "attendu": 2, "alias": ["PMA"]}]
    monkeypatch.setattr(groupes, "catalog", lambda: cat)
    pages = {"https://las.test": las, "https://un.test": afr}

    class FakeClient:
        def get(self, url, *_, **__):
            if url in pages:
                return fetch.FetchResult(url=url, status=200, content=pages[url].encode(), mime="text/html")
            if url.endswith("/CallForSubmissions"):
                return fetch.FetchResult(url=url, status=200, content=json.dumps(calls).encode(), mime="application/json")
            if url.endswith("/Submissions"):
                return fetch.FetchResult(url=url, status=200, content=json.dumps(subs).encode(), mime="application/json")
            return fetch.FetchResult(url=url, status=200, mime="text/plain",
                                     content=(("We call for robust adaptation indicators. " * 8) + url).encode())

    out = groupes.sync(conn, FakeClient())
    assert out["Arab Group"].startswith("verifie_par_equivalence 2") and out["AGN"].startswith("verifie_par_equivalence 3")
    info = groupes.query(conn, groupe="AGN")
    assert "équivalence" not in info["statut"] and "groupe régional" in info["equivalence"]
    assert info["membres_annonces"] == "4 puis 3"
    # G3 : le groupe a soumis en son nom, mais pas sur l'appel où l'UE a répondu → repli sur les membres.
    live._ready.clear()
    calls = [{"Id": i, "Title": f"Views on GGA indicators {i}", "Issue": "", "SessionName": "SB 65", "Mandate": "N/A",
              "Deadline": None, "Themes": [], "Bodies": [], "TargetAudience": [], "CallType": "CallForSubmission",
              "OpenCall": True, "IsUpcomingSession": True, "ModifiedDate": "2026-09-01T00:00:00Z"} for i in (1, 2)]
    subs = [{"Id": i + 1, "CallForSubmissionId": c, "Title": e, "FileName": f"{e}{c}.txt",
             "ServerRelativeURL": f"/submissions/Archive/{e}{c}.txt", "Language": "English",
             "SubmissionDate": f"2026-09-0{i + 1}T00:00:00Z", "Description": d, "Entity": e, "EntityType": "Party"}
            for i, (c, e, d) in enumerate([(1, "European Union", ""), (1, "Nepal", ""),
                                           (2, "Tuvalu", "on behalf of the LDC Group")])]
    sm.sync_index(conn, FakeClient())
    assert conn.execute("SELECT iso3 FROM sub_items WHERE entity='Nepal'").fetchone()[0] == "NPL"  # G5
    sm.download_pending(conn, FakeClient(), limit=10)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(llm, "json_call", lambda conn_, **k: {"points": []} if k["purpose"] == "soumission-comparaison"
                        else {"summary": "r", "summary_en": "s", "matters": []})
    r = sm.compare_actors(conn, "EU", "LDC")
    assert r["b"]["mode"] == "membres" and r["b"]["membres_ayant_soumis"] == ["Nepal"] and "repli" in r
