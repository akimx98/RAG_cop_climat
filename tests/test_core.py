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
    from cop_corpus import auth, ingest, telegram

    conn = env
    sent: list[tuple] = []
    monkeypatch.setattr(telegram, "send", lambda chat, text: sent.append((chat, text)) or True)
    auth.create_user(conn, "a@b.c", "Alix")
    code = conn.execute("SELECT telegram_code FROM users").fetchone()[0]

    def msg(text=None, **extra):
        telegram.handle(conn, {"message": {"chat": {"id": 42}, **({"text": text} if text else {}), **extra}})
        return sent[-1][1]

    assert "not linked" in msg("/cherche finance")
    out = msg(f"/start {code}")
    assert "Alix" in out and "/brief" in out and "/connect" in out          # aide en anglais par défaut
    assert "Following topic" in msg("/follow ligne rouge")
    # le dépôt de documents par le bot est retiré
    assert "does not take documents" in msg(caption="Note", document={"file_id": "x", "file_name": "note.txt"})
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    # un document interne déjà au corpus reste masqué dans Telegram
    ingest.ingest_bytes(conn, ("Note interne sur la ligne rouge finance " * 30).encode(), url=None,
                        access="restreint", filename="note.txt", meta={"title": "Note de coordination UE"})
    out = msg("/search ligne rouge finance")
    assert "Internal document" in out and "Note de coordination" not in out
    assert "Internal document" in msg("/new")
    assert "Français" in msg("/lang fr") or "français" in sent[-1][1].lower()
    assert "/suivi" in msg("/aide")


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
    assert 'method="post"' in client.get(f"/entrer/{link}").text        # GET : un bouton, rien n'est consommé
    r = client.post(f"/entrer/{link}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account"
    assert "At least" in client.post("/account", data={"password": "court", "password2": "court"}).text
    r = client.post("/account", data={"password": "un-bon-mot-de-passe", "password2": "un-bon-mot-de-passe"})
    assert "Password saved" in r.text
    assert client.get(f"/entrer/{link}?lang=fr").text.count("Lien expiré") == 1   # caduc après changement
    assert token in client.get("/connect").text                           # adresse toujours affichée

    # connexion par mot de passe depuis un autre navigateur (page /signin, puis fenêtre en JSON)
    other = TestClient(Starlette(routes=web.routes), base_url="https://testserver")
    assert "wrong password" in other.post("/signin", data={"email": "alix@example.org", "password": "faux"}).text.lower()
    j = other.post("/signin", data={"email": "alix@example.org", "password": "faux"},
                   headers={"X-Requested-With": "fetch"}).json()
    assert j["step"] == "password" and "wrong password" in j["error"].lower()
    assert other.post("/signin", data={"email": "alix@example.org"}, headers={"X-Requested-With": "fetch"}).json()["step"] == "password"
    r = other.post("/signin", data={"email": "ALIX@example.org", "password": "un-bon-mot-de-passe",
                                    "next": "/connect"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/connect"
    assert token in other.get("/connect").text
    r = other.post("/connexion", data={"email": "alix@example.org", "password": "un-bon-mot-de-passe",
                                       "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    j = other.post("/signin", data={"email": "alix@example.org", "password": "un-bon-mot-de-passe", "next": "/topics"},
                   headers={"X-Requested-With": "fetch"}).json()
    assert j == {"redirect": "/topics"}

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
    assert mailin.route(conn, addr) is None                               # dépôt par mail retiré
    assert mailin.route(conn, "root@" + config.MAIL_DOMAIN) is None
    assert mailin.route(conn, "veille@" + config.MAIL_DOMAIN)[0] == "veille"

    def spool(msg, rcpt):
        folder = config.MAIL_DIR / mailin.SPOOL
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{len(list(folder.iterdir()))}.eml").write_bytes(f"X-Cop-Rcpt: {rcpt}\r\n".encode() + msg.as_bytes())

    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "Alix <alix@example.org>", addr, "TR: Note de coordination UE finance"
    m.set_content("Compte rendu de la coordination UE du matin sur le NCQG. " * 8)
    spool(m, addr)
    mailin.process_spool(conn)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0

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
    assert sent and sent[0][0] == "7" and "brackets 2→0 (-2, resolved)" in sent[0][1]   # anglais par défaut
    assert live.notify_retrieved(conn, lambda *a: sent.append(a)) == 0  # ne ré-alerte pas
    assert "brackets 2→0 (-2, resolved)" in diff.delta_line(d, "en")


def test_text_stage():
    from cop_corpus import live

    assert live.text_stage({"symbol": "FCCC/SBI/2026/L.13", "cb_type": "Draft conclusions"}) == "for_adoption"
    assert live.text_stage({"title": "Draft text on GGA", "cb_type": "draft text"}) == "draft"
    assert live.text_stage({"title": "Informal note by the co-facilitators", "cb_type": "informal note"}) == "draft"
    assert live.text_stage({"title": "CMA 8 Agenda as adopted", "cb_type": "Agenda"}) == "adopted"
    # projets : le « -/CMA.8 » d'un projet de décision et les renvois à des décisions passées ne sont pas des adoptions
    assert live.text_stage({"title": "Draft decision -/CMA.8 Mitigation work programme", "symbol": "FCCC/PA/CMA/2026/L.4"}) == "for_adoption"
    assert live.text_stage({"title": "Draft decision -/CMA.8 Mitigation work programme", "cb_type": "draft text"}) == "draft"
    assert live.text_stage({"title": "UAE dialogue referred to in decision 1/CMA.5", "symbol": "FCCC/SB/2026/L.2"}) == "for_adoption"
    assert live.text_stage({"title": "Report on the dialogue referred to in decision 1/CMA.5", "cb_type": "Summary reports"}) == "other"
    assert live.text_stage({"title": "Decision 3/CMA.8 Global goal on adaptation", "cb_type": ""}) == "adopted"
    assert live.text_stage({"title": "Global goal on adaptation. Advance unedited version", "cb_type": ""}) == "adopted"
    assert live.text_stage({"title": "Synthesis report", "cb_type": "Synthesis reports"}) == "other"
    assert live.stage_label("for_adoption", "en").startswith("🔨 forwarded")


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
    assert out["jour"] == "2026-11-13" and "Negotiations" in out["negociations_du_jour"]["resume"]
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
    assert r.headers["location"] == f"/join/{inv['code']}"
    page = client.get(f"/join/{inv['code']}").text                         # l'accueil, fenêtre d'inscription ouverte
    assert 'data-open="signup"' in page and f'action="/join/{inv["code"]}"' in page and "su-pw" not in page
    form = {"code": inv["code"], "name": "Sam Invité", "organisation": "ONG X", "email": "Sam@ong.org",
            "password": "mot-de-passe-long", "password2": "mot-de-passe-long"}
    j = client.post(f"/rejoindre/{inv['code']}", data=form, headers={"X-Requested-With": "fetch"}).json()
    assert j == {"redirect": "/?welcome=1"}
    guest = auth.by_email(conn, "sam@ong.org")
    assert guest["source"] == "invitation" and not guest["can_restricted"] and not guest["is_admin"]
    assert auth.is_guest(guest) and not auth.can_contribute(guest)

    home = client.get("/?welcome=1").text
    assert "Welcome" in home and "Get started" in home and 'href="/connect"' in home and "Create a free account" not in home
    assert f"/u/{auth.mcp_token(guest)}/mcp" in client.get("/connect").text
    client.post("/topics", data={"action": "dossiers", "dossiers": ["finance", "technologie-capacites"]})
    client.post("/topics", data={"action": "add", "query": "Action for Climate Empowerment"})
    assert live.followed(conn, guest["id"]) == {"finance", "technologie-capacites"}
    topics_page = client.get("/topics").text
    assert "Action for Climate Empowerment" in topics_page and "Alerts on Telegram" in topics_page
    assert guest["lang"] == "en" and 'lang="en"' in home
    assert "<svg" in client.get("/inviter").text                       # QR code d'invitation
    client.get("/lang/fr")
    assert "Mes sujets" in client.get("/").text and auth.by_id(conn, guest["id"])["lang"] == "fr"
    client.get("/lang/en")

    # aucun ajout au corpus, aucune administration
    assert client.get("/deposer", follow_redirects=False).status_code == 301
    assert client.post("/textes/1/deposer").status_code == 403
    assert client.get("/admin").status_code == 403

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
    telegram.handle(conn, {"message": {"chat": {"id": 7}, "text": f"/start {guest['telegram_code']}"}})
    assert "Sam" in sent[-1][1] or "sam@ong.org" in sent[-1][1]
    assert "PDF" not in sent[-1][1] and "/search" in sent[-1][1] and "/note" not in sent[-1][1]
    n_docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    for extra in ({"document": {"file_id": "x", "file_name": "a.txt", "mime_type": "text/plain"}},
                  {"text": "https://example.org/page"}, {"text": "/note GGA blocage au §12"}):
        telegram.handle(conn, {"message": {"chat": {"id": 7}, **extra}})
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
    assert not hasattr(mcp_server, "noter")                               # notes de terrain retirées
    assert mcp_server.suivre(dossier="ace")["dossier"] == "ace"
    assert "erreur" in mcp_server.suivre(mots_cles="x", dossier="ace")
    assert [d["slug"] for d in mcp_server.mes_sujets()["dossiers_suivis"]] == ["ace"]
    assert "notes_terrain_internes" not in mcp_server.dossier("finance")
    news = mcp_server.nouveautes(depuis="24h", mes_sujets=True)
    assert [s["slug"] for s in news["sujets"]] == ["ace"] and news["sans_nouveaute"]
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
    assert 'id="su-pw"' not in phone.get(f"/join/{code}").text
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
    assert r.status_code == 303 and laptop.get("/topics", follow_redirects=False).status_code == 200
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
    assert "could not be sent" in TestClient(app, base_url="https://testserver").post("/connexion", data={"email": "z@ngo.org"}).text


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
    assert 'id="pane-signup"' in home and 'name="password"' in home and 'data-auth="signin"' in home   # fenêtre sur l'accueil
    form = {"email": "Lea@media.org", "organisation": "Media", "password": "mot-de-passe-long", "password2": "mot-de-passe-long"}
    assert "do not match" in visitor.post("/join", data={**form, "password2": "autre-chose-long"}).text
    assert "at least 8" in visitor.post("/join", data={**form, "password": "court", "password2": "court"}).text
    assert "could not be completed" in visitor.post("/join", data={**form, "hp_x7": "http://spam"}).text   # champ piège
    j = visitor.post("/join", data={**form, "password2": "autre-chose-long"}, headers={"X-Requested-With": "fetch"}).json()
    assert "do not match" in j["error"]                                       # l'erreur reste dans la fenêtre
    r = visitor.post("/join", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?welcome=1"
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
    assert 'id="su-pw"' not in page and 'name="organisation"' in page
    u, _, err = auth.signup(conn, code, "inv@ngo.org")
    assert not err and u["source"] == "invitation" and not u["password_hash"]

    # fermeture par l'admin
    auth.set_open_signup(conn, open_=False)
    third = TestClient(app, base_url="https://testserver")
    assert 'id="su-pw"' not in third.get("/").text
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
    assert r.headers["location"].startswith("/account?forgot=1")
    assert "new password" in c.get("/account?forgot=1").text
    c.post("/account", data={"password": "nouveau-mot-de-passe", "password2": "nouveau-mot-de-passe"})
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
    assert sm.co_submitters("African Group of Negotiators as an input to the JTWP review",
                            "African Group of Negotiators (AGN)") == ["African Group of Negotiators"]
    assert sm.co_submitters("on behalf of the Coalition for Rainforest Nations", "Panama") == [
        "Coalition for Rainforest Nations"]
    assert sm.co_submitters("on behalf of ALBA, the Bolivarian Alliance for the Peoples of Our America", "Cuba") == [
        "ALBA", "Bolivarian Alliance for the Peoples of Our America"]
    assert sm.co_submitters("Group of Experts in Climate Finance", "X") == ["Group of Experts in Climate Finance"]
    assert sm.co_submitters("ALBA at the joint meeting of the SBI and SBSTA at their opening", "Cuba") == ["ALBA"]
    assert sm.co_submitters("European Union and its Member States. This submission is supported by Albania",
                            "Austria") == ["European Union and its Member States"]
    assert sm.co_submitters("on behalf Coalition for Rainforest Nations", "Panama") == ["Coalition for Rainforest Nations"]
    from cop_corpus.positions import normalize_actor
    assert normalize_actor("Coalition of Rainforest Nations (CfRN)") == "CfRN" == normalize_actor("CfRN")
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
    assert out["positions"] == [] and "Somalia" not in out["message"].split("similar names")[0]
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
                             "issue": "", "themes": "[]"}) == {"ace": 1.0}
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
    m = mcp_server.comparer_soumissions(acteurs=["UE", "LMDC", "AOSIS"], dossier="gga")
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
    assert out["b"]["membres_ayant_soumis"] == ["Nepal", "Tuvalu"]
    assert out["comparaisons"] == [] and len(out["a_comparer"]) == 2  # rien calculé côté serveur
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


def test_mcp_instructions_fit_client_limit():
    """Les clients MCP ne gardent qu'environ 2 000 caractères des consignes : règles d'abord, carte des outils
    entière, chaque outil cité existe."""
    import re as _re

    from cop_corpus import mcp_server

    text = mcp_server.INSTRUCTIONS
    assert len(text) < 1800
    head = text[:1000]
    assert "Quote the exact text" in head and "LEVEL" in head and "not adopted" in head
    tools = set(mcp_server.mcp._tool_manager._tools)
    outils = text.split("TOOLS", 1)[1].split("Methods", 1)[0]
    cited = {w for w in _re.findall(r"\b[a-z_]{5,}\b", outils) if "_" in w or w in tools}
    assert cited and cited <= tools, cited - tools
    assert {"soumissions", "comparer_soumissions", "groupes", "pavillons", "dossier"} <= cited


def test_prompts_and_skills_cite_existing_tools():
    """Chaque outil appelé (`nom(`) par une méthode MCP ou un skill du plugin existe ; les méthodes FR et EN ont les
    mêmes étapes et citent les positions écrites là où elles servent."""
    import re as _re
    from pathlib import Path

    from cop_corpus import mcp_server

    tools = set(mcp_server.mcp._tool_manager._tools)
    sources = {f"methode {k}/{lg}": "\n".join(v[lg]) for k, v in mcp_server.METHODES.items() for lg in ("fr", "en")}
    for f in Path(__file__).resolve().parents[1].glob("clients/claude-plugin/skills/*/SKILL.md"):
        sources[f.parent.name] = f.read_text(encoding="utf-8")
    called = {}
    for name, text in sources.items():
        for tool in _re.findall(r"`([a-z_]+)\(", text):
            called.setdefault(tool, set()).add(name)
        for tool in _re.findall(r"`([a-z_]+)`", text):
            if tool in tools or "_" in tool and tool.split("_")[0] in {"comparer", "lire", "mes", "suivre", "arreter"}:
                called.setdefault(tool, set()).add(name)
    missing = {t: sorted(w) for t, w in called.items() if t not in tools}
    assert not missing, missing
    for k, v in mcp_server.METHODES.items():
        assert len(v["fr"]) == len(v["en"]), k
    assert "soumissions" in " ".join(mcp_server.METHODES["position_pays"]["fr"])
    assert "groupes" in " ".join(mcp_server.METHODES["preparer_rencontre"]["en"])


def test_retired_tools_not_cited_and_and_split():
    """P3 : aucun outil retiré n'est cité (mot entier) ; « and » ne sépare que des acteurs connus."""
    import re as _re
    from pathlib import Path

    from cop_corpus import mcp_server, soumissions as sm

    root = Path(__file__).resolve().parents[1]
    texts = {"INSTRUCTIONS": mcp_server.INSTRUCTIONS,
             "METHODES": str(mcp_server.METHODES),
             "docstrings": "\n".join((t.description or "") for t in mcp_server.mcp._tool_manager._tools.values())}
    for f in root.glob("clients/claude-plugin/skills/*/SKILL.md"):
        texts[f.parent.name] = f.read_text(encoding="utf-8")
    for name, text in texts.items():
        for tool in mcp_server.RETIRED_TOOLS:
            # Nom avec « _ » : interdit comme mot entier ; mot courant (« engagements ») : interdit sous forme d'outil.
            pattern = rf"\b{tool}\b" if "_" in tool else rf"`{tool}[`(]|\b{tool}\("
            assert not _re.search(pattern, text), (tool, name)
    assert not set(mcp_server.RETIRED_TOOLS) & set(mcp_server.mcp._tool_manager._tools)
    known = {"brazil", "south africa", "india", "china", "basic", "arab group", "alba", "antigua"}
    assert sm.co_submitters("on behalf of Brazil, South Africa, India and China (BASIC)", "X", known) == [
        "Brazil", "South Africa", "India", "China", "BASIC"]
    assert sm.co_submitters("Arab Group and ALBA", "X", known) == ["Arab Group", "ALBA"]
    assert sm.co_submitters("Antigua and Barbuda", "X", known) == ["Antigua and Barbuda"]
    assert sm.co_submitters("Arab Group and ALBA", "X") == ["Arab Group and ALBA"]  # sans liste : pas de découpe


def test_docs_match_tool_registry():
    """P4 : le tableau des outils de la documentation de référence = le registre MCP ; aucun outil retiré cité
    dans la documentation (hors journal historique), le README et les instructions ChatGPT."""
    import re as _re
    from pathlib import Path

    from cop_corpus import mcp_server

    root = Path(__file__).resolve().parents[1]
    tools = set(mcp_server.mcp._tool_manager._tools)
    arch = (root / "docs/architecture.md").read_text(encoding="utf-8")
    section = arch.split("## 4. Outils MCP", 1)[1].split("\n## ", 1)[0]
    table = set(_re.findall(r"^\| `([a-z_]+)` \|", section, _re.M))
    assert table == tools, {"absents de la doc": tools - table, "en trop": table - tools}
    files = [p for p in (root / "docs").glob("*.md") if p.name != "historique.md"]
    files += [root / "README.md", root / "clients/chatgpt-instructions.md"]
    for f in files:
        text = f.read_text(encoding="utf-8")
        for tool in mcp_server.RETIRED_TOOLS:
            pattern = rf"\b{tool}\b" if "_" in tool else rf"`{tool}[`(]|\b{tool}\("
            assert not _re.search(pattern, text), (tool, f.name)


def test_settings_centralized_and_documented():
    """P5 : variables d'environnement lues seulement dans config.py, toutes listées dans la doc de référence."""
    import re as _re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    src = root / "src" / "cop_corpus"
    for f in src.glob("*.py"):
        if f.name != "config.py":
            assert "os.environ" not in f.read_text(encoding="utf-8"), f.name
    cfg = (src / "config.py").read_text(encoding="utf-8")
    names = set(_re.findall(r'(?:os\.environ\.get|_path)\(\s*"([A-Z_]+)"', cfg))
    assert len(names) > 30
    doc = (root / "docs/architecture.md").read_text(encoding="utf-8").split("## 7. Réglages", 1)[1].split("\n## ", 1)[0]
    documented = set(_re.findall(r"`([A-Z][A-Z_]+)`", doc))
    assert names <= documented, names - documented


def test_submissions_harvest_has_its_own_thread(monkeypatch):
    """La collecte des soumissions ne passe plus par la boucle de veille documentaire (fil dédié)."""
    import inspect

    from cop_corpus import worker

    assert "harvest_tick" not in inspect.getsource(worker.watch_once)
    calls = []
    monkeypatch.setattr("cop_corpus.soumissions.harvest_tick", lambda conn, client: calls.append(1) or {})
    stop = __import__("threading").Event()
    monkeypatch.setattr(worker.db, "connect", lambda: None)
    monkeypatch.setattr(worker, "submissions_once", lambda conn, client: (calls.append(1), stop.set()))
    worker.submissions_forever(stop, interval=0)
    assert calls == [1]


def test_client_side_analysis_and_scoring(env, monkeypatch):
    """Analyse et comparaison par le LLM du client : le serveur ne fait aucun appel IA, fournit le texte et la
    consigne, vérifie les citations et calcule l'indication par règles fixes ; résultat en cache."""
    import json

    from cop_corpus import fetch, live, llm, mcp_server, soumissions as sm

    conn = env
    live._ready.clear()
    monkeypatch.setattr(llm, "json_call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("appel IA serveur")))
    calls = [{"Id": 1, "Title": "Views on GGA indicators", "Issue": "", "SessionName": "SB 65", "Mandate": "N/A",
              "Deadline": None, "Themes": [], "Bodies": [], "TargetAudience": [], "CallType": "CallForSubmission",
              "OpenCall": True, "IsUpcomingSession": True, "ModifiedDate": "2026-09-01T00:00:00Z"}]
    texts = {"EU": "The European Union urges Parties to adopt a limited set of headline indicators. " * 4,
             "LMDC": "The LMDC cannot accept headline indicators that burden developing countries. " * 4}
    subs = [{"Id": i + 1, "CallForSubmissionId": 1, "Title": e, "FileName": f"{k}.txt",
             "ServerRelativeURL": f"/submissions/Archive/{k}.txt", "Language": "English",
             "SubmissionDate": f"2026-09-0{i + 1}T00:00:00Z", "Description": d, "Entity": e, "EntityType": "Party"}
            for i, (k, e, d) in enumerate([("EU", "European Union", ""), ("LMDC", "Saudi Arabia", "on behalf of the LMDC")])]

    class FakeClient:
        def get(self, url, *_, **__):
            if url.endswith("/CallForSubmissions"):
                return fetch.FetchResult(url=url, status=200, content=json.dumps(calls).encode(), mime="application/json")
            if url.endswith("/Submissions"):
                return fetch.FetchResult(url=url, status=200, content=json.dumps(subs).encode(), mime="application/json")
            k = url.rsplit("/", 1)[1].split(".")[0]
            return fetch.FetchResult(url=url, status=200, content=texts[k].encode(), mime="text/plain")

    sm.sync_index(conn, FakeClient())
    sm.download_pending(conn, FakeClient(), limit=10)
    eu = conn.execute("SELECT doc_id FROM sub_items WHERE entity='European Union'").fetchone()[0]
    lm = conn.execute("SELECT doc_id FROM sub_items WHERE entity='Saudi Arabia'").fetchone()[0]
    a = mcp_server.soumissions(document=str(eu))
    assert a["passages"] and "MOT POUR MOT" in a["consigne"] and "analyse_existante" not in a
    step1 = mcp_server.comparer_soumissions("EU", "LMDC")
    assert step1["a"]["doc_id"] == eu and step1["b"]["passages"] and "qualifications" in step1["consigne"]
    quals = [{"point": "headline indicators", "a_position": "pour", "b_position": "contre", "a_force": 4, "b_force": 4,
              "relation": "conflit", "a_citation": "urges Parties to adopt a limited set of headline indicators",
              "b_citation": "cannot accept headline indicators that burden developing countries"},
             {"point": "finance", "a_position": "", "b_position": "x", "a_force": 3, "b_force": 4, "relation": "conflit",
              "a_citation": "invented sentence not in the text", "b_citation": "cannot accept headline indicators"}]
    out = mcp_server.comparer_soumissions(a=str(eu), b=str(lm), qualifications=quals)
    assert out["decompte"]["citations_non_retrouvees"] == 1
    finance = next(p for p in out["points"] if p["point"] == "finance")
    assert finance["a_force"] == 2 and finance["a_citation_verifiee"] is False  # citation inventée : plus « forte »
    # 1 conflit central, 0 % d'accord → non alignées ; le conflit sur « finance » devient secondaire.
    assert out["indication"] == "non_aligne" and out["qualifie_par"].startswith("le LLM")
    again = mcp_server.comparer_soumissions("EU", "LMDC")
    assert again["indication"] == "non_aligne"  # en cache, plus de texte à relire
    assert mcp_server.comparer_soumissions(acteurs=["EU", "LMDC"], dossier="gga")["comparees"]


def test_briefs_off_by_default(env):
    """Briefs désactivés par défaut : pas de génération, aucun ancien brief montré, ni commande /brief."""
    from cop_corpus import briefs, config, live, live_bot

    conn = env
    live._ready.clear()
    live.ensure_schema(conn)
    assert config.BRIEFS is False
    conn.execute("INSERT INTO briefs(dossier, created_at, period_start, body, sources) VALUES('adaptation', "
                 "'2026-11-17T07:00:00Z', '2026-11-16T19:00:00Z', '{}', '[]')")
    conn.commit()
    assert briefs.latest(conn, "adaptation") is None and briefs.run_slot(conn, lambda *a: None) == 0
    assert not hasattr(live_bot, "COMMANDS")          # /brief existe toujours, mais c'est le récapitulatif sans IA


def test_position_extraction_sources_filtered(env):
    """Extraction des positions : comptes rendus de négociation et déclarations officielles seulement ; les positions
    déjà tirées d'une source exclue (presse généraliste) sont retirées."""
    from cop_corpus import ingest, live, positions

    conn = env
    live._ready.clear()
    live.ensure_schema(conn)
    ids = {}
    for sid in ("twn-climate-info", "guardian-cop31-rss", "g77-statements-2026-crawl"):
        ids[sid], _, _ = ingest.ingest_bytes(conn, f"{sid} report on negotiations and positions of Parties. ".encode() * 20,
                                             url=f"https://x.test/{sid}", filename=f"{sid}.txt", source_id=sid,
                                             meta={"doc_type": "analyse", "level": "4", "published": "2026-11-15"})
    picked = {d["source_id"] for d in positions.pending_docs(conn, limit=10)}
    assert picked == {"twn-climate-info", "g77-statements-2026-crawl"}
    for sid, did in ids.items():
        conn.execute("INSERT INTO positions(key,actor,issue,stance,summary,quote,source_kind,doc_id,created_at,access) "
                     "VALUES(?,?,?,?,?,?,'extraction',?,'2026-11-15','public')", (sid, "UK", "i", "soutient", "s", "q", did))
    conn.commit()
    assert positions.prune_unlisted(conn) == 1
    assert {r[0] for r in conn.execute("SELECT key FROM positions")} == {"twn-climate-info", "g77-statements-2026-crawl"}


def test_digest_and_whats_new_on_my_topics(env):
    """Récapitulatif sans IA : centré sur les sujets, étape des textes, rien de neuf → pas de message."""
    from datetime import datetime, timezone

    from cop_corpus import auth, config, digest, live, topics

    conn = env
    live._ready.clear()
    live.ensure_schema(conn)
    uid, _ = auth.create_user(conn, "d@x.org", "D", restricted=False)
    user = auth.by_id(conn, uid)
    since = "2026-11-17T00:00:00Z"
    assert topics.whats_new(conn, user, since)["aucun_sujet"]
    assert digest.render(conn, user, since) is None
    live.follow(conn, uid, "adaptation", True)
    assert digest.render(conn, user, since) is None                      # sujet suivi, mais rien de neuf
    conn.execute("INSERT INTO live_texts(key,symbol,title,cb_type,dossier,published,first_seen,updated_at,brackets) "
                 "VALUES('k1','FCCC/PA/CMA/2026/L.9','Global goal on adaptation. Draft conclusions','Draft conclusions',"
                 "'adaptation','2026-11-17T20:00','2026-11-17T20:01:00Z','2026-11-17T20:01:00Z',3)")
    conn.commit()
    news = topics.whats_new(conn, user, since)
    item = news["sujets"][0]
    assert item["textes"][0]["etape"] == "for_adoption" and "/database/negotiations/adaptation" in item["lien"]
    text = digest.render(conn, user, since, datetime(2026, 11, 18, 4, 0, tzinfo=timezone.utc))
    assert "Morning digest" in text and "forwarded for adoption" in text and "/connect" in text
    # créneaux : 7 h et 19 h à Antalya, une fois chacun
    sent = []
    conn.execute("UPDATE users SET telegram_chat_id='9' WHERE id=?", (uid,))
    conn.commit()
    config.TELEGRAM_TOKEN, old = "x", config.TELEGRAM_TOKEN
    try:
        now = datetime(2026, 11, 18, 4, 30, tzinfo=timezone.utc)          # 7 h 30 à Antalya
        assert digest.run(conn, lambda c, t: sent.append(t) or True, now) == 1
        assert digest.run(conn, lambda c, t: sent.append(t) or True, now) == 0
        assert digest.due_slot(conn, datetime(2026, 11, 18, 16, 5, tzinfo=timezone.utc)) == "2026-11-18T19"
    finally:
        config.TELEGRAM_TOKEN = old


def test_plan_calendar_and_contact(env, monkeypatch):
    import json as _json

    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from cop_corpus import agenda, alerts, auth, config, db, events, mailout, pavillons, web, web_live
    importlib.reload(web)
    importlib.reload(web_live)
    conn = db.get()
    pavillons.ensure_schema(conn)
    agenda.ensure_schema(conn)
    conn.execute("INSERT INTO pav_events(key,source,zone,date,start,end,pavilion,title,description,speakers,themes,"
                 "keywords,cancelled) VALUES('p1','cop31.tr','blue','2026-11-18','10:00','11:00','Fiji Pavilion',"
                 "'Ocean finance for SIDS','Blue bonds.',?, '[]','[]',0)", (_json.dumps([{"name": "Ana Lima", "role": "speaker"}]),))
    conn.execute("INSERT INTO pav_events(key,source,zone,date,start,end,pavilion,title,description,speakers,themes,"
                 "keywords,cancelled) VALUES('p2','cop31.tr','green','2026-11-18','12:00','13:00','Youth Hub',"
                 "'Space and climate','Satellites.','[]','[]','[]',0)")
    conn.execute("INSERT INTO agenda_events(key,kind,source,days,start,end,room,title,organisers,topics) "
                 "VALUES('r1','reunion','planner',',18,','15:00','17:00','Plenary','GGA informal consultations','[]','[]')")
    conn.commit()
    res = events.query(conn, 18)
    assert [e["kind"] for e in res["events"]] == ["pavilion", "pavilion", "official"]
    assert [e["key"] for e in events.query(conn, 18, q="ocean")["events"]] == ["p1"]
    assert events.query(conn, 18, q="ace")["total"] == 0                  # frontière de mot : « space » ne compte pas
    assert [e["key"] for e in events.query(conn, 18, kinds=["official"])["events"]] == ["agenda:r1"]
    uid, _ = auth.create_user(conn, "p@x.org", "P", restricted=False)
    alerts.add_subscription(conn, uid, "Ocean", "ocean, blue bonds")
    terms = events.topic_terms(conn, auth.by_id(conn, uid))
    assert [e["key"] for e in events.query(conn, 18, terms=terms)["events"]] == ["p1"]

    client = TestClient(Starlette(routes=[*web.routes, *web_live.routes]), base_url="https://testserver")
    page = client.get("/plan?day=18&q=ocean").text
    assert "Ocean finance for SIDS" in page and "Ana Lima" in page and "Space and climate" not in page

    # contact : adresse jamais affichée, champ piège, envoi par e-mail
    outbox = []
    monkeypatch.setattr(mailout, "enabled", lambda: True)
    monkeypatch.setattr(mailout, "send", lambda to, subject, text, **kw: outbox.append((to, kw.get("reply_to"))) or True)
    monkeypatch.setattr(config, "CONTACT_EMAIL", "owner@example.org")
    assert "owner@example.org" not in client.get("/contact").text
    assert "too short" in client.post("/contact", data={"email": "a@b.org", "message": "hi"}).text
    client.post("/contact", data={"email": "a@b.org", "message": "x" * 20, "website": "spam"})
    assert not outbox
    assert "Message sent" in client.post("/contact", data={"email": "a@b.org", "message": "A source to add, please."}).text
    assert outbox == [("owner@example.org", "a@b.org")]



def test_review_fixes(env, monkeypatch):
    """Revue de la refonte : redirections sûres, suppression de compte sans réemploi d'identifiant, rapports internes
    jamais dans le récapitulatif Telegram, code Telegram renouvelé, liens tiers filtrés."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from cop_corpus import auth, db, digest, ingest, live, web, web_live
    importlib.reload(web)
    importlib.reload(web_live)
    conn = db.get()
    for bad in ("//evil.example", "/\\evil.example", "/\tevil", "https://evil.example", "/x\ny"):
        assert web._safe_next(bad) == "/", bad
    assert web._safe_next("/topics?x=1") == "/topics?x=1"
    assert web._safe_url("javascript:alert(1)") == "" and web._safe_url("https://a.b/c") == "https://a.b/c"

    # compte supprimé : l'identifiant n'est jamais réattribué, l'ancien cookie n'ouvre rien
    code = auth.invite_settings(conn)["code"]
    g, tok, _ = auth.signup(conn, code, "old@x.org")
    phone = TestClient(Starlette(routes=[*web.routes, *web_live.routes]), base_url="https://testserver")
    phone.cookies.set(web.COOKIE, auth.make_cookie(g["id"], days=1))
    assert phone.get("/account").status_code == 200
    phone.post("/account", data={"action": "delete", "confirm": "delete"})
    n, _, _ = auth.signup(conn, code, "new@x.org")
    assert n["id"] != g["id"] and auth.by_token(conn, tok) is None
    stale = TestClient(Starlette(routes=[*web.routes, *web_live.routes]), base_url="https://testserver")
    stale.cookies.set(web.COOKIE, auth.make_cookie(g["id"], days=1))
    assert stale.get("/account", follow_redirects=False).status_code == 303
    assert auth.invite_settings(conn)["used"] == 1                        # la place du compte supprimé est libérée

    # rapport interne : jamais dans le récapitulatif, même pour un compte autorisé
    live._ready.clear()
    live.ensure_schema(conn)
    uid, _ = auth.create_user(conn, "c@x.org", "C")                       # contributeur (accès restreint)
    live.follow(conn, uid, "attenuation", True)
    ingest.ingest_bytes(conn, ("SECRET delegation debrief on the mitigation work programme MWP. " * 20).encode(),
                        url=None, access="restreint", filename="d.txt",
                        meta={"title": "SECRET debrief", "doc_type": "compte_rendu"})
    text = digest.render(conn, auth.by_id(conn, uid), "2000-01-01T00:00:00Z") or ""
    assert "SECRET" not in text

    # délier Telegram renouvelle le code
    c = TestClient(Starlette(routes=[*web.routes, *web_live.routes]), base_url="https://testserver")
    c.cookies.set(web.COOKIE, auth.make_cookie(uid, days=1))
    before = auth.by_id(conn, uid)["telegram_code"]
    c.post("/account", data={"action": "telegram_unlink"})
    assert auth.by_id(conn, uid)["telegram_code"] != before

    # pages réservées : retour après connexion, jamais vers une route POST
    anon = TestClient(Starlette(routes=[*web.routes, *web_live.routes]), base_url="https://testserver")
    assert anon.get("/topics?x=1", follow_redirects=False).headers["location"] == "/?signin=1&next=/topics%3Fx%3D1"
    assert anon.post("/topics/1/delete", follow_redirects=False).headers["location"] == "/?signin=1&next=/"
    assert anon.get("/telegram?a=1", follow_redirects=False).headers["location"] == "/topics?a=1#telegram"
