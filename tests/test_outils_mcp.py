"""Outils MCP après retour d'expérience (novembre 2026) : dossier ACE et ambiguïtés, replis de comparer_soumissions
(sous-groupes, soumission antérieure, écart de dates), positions section par section et ENB, divisions internes,
seuil de pertinence de rechercher, crochets par thème, vérification de deposer_url."""

import importlib
import json

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
    from cop_corpus import live

    live._ready.clear()
    return db.get()


def _emissions(conn, pairs):
    conn.execute("CREATE TABLE IF NOT EXISTS emissions(iso3 TEXT, country TEXT, year INT, indicator TEXT, value REAL, "
                 "unit TEXT, source TEXT)")
    conn.executemany("INSERT INTO emissions(iso3,country) VALUES(?,?)", pairs)
    conn.commit()


# --- dossiers -------------------------------------------------------------------------------------------------------

def test_ace_dossier_and_ambiguity(env):
    from cop_corpus import live, mcp_server, soumissions as sm

    assert live.dossier_from_alias("ACE") == "ace"
    assert live.dossier_from_alias("Action for Climate Empowerment") == "ace"
    assert live.dossier_from_alias("ctcn") == "technologie-capacites"
    assert live.dossier_for_cb_topic("Action for climate empowerment") == "ace"
    # Appels : l'ACE (art. 6 de la Convention) n'est jamais rattachée à la technologie.
    assert sm.rule_dossiers({"title": "Views on Action for Climate Empowerment under Article 6 of the Convention",
                             "issue": "", "themes": "[]"}) == {"ace": 1.0}
    assert sm.rule_dossiers({"title": "Technology implementation programme (BTIP)", "issue": "", "themes": "[]"}) == {
        "technologie-capacites": 1.0}
    # Mot ambigu : candidats proposés, aucun choix silencieux.
    slug, cands = live.resolve_dossier("programme")
    assert slug is None and len(cands) > 1
    out = mcp_server.dossier("programme")
    assert "ambigu" in out["erreur"] and len(out["candidats"]) > 1
    assert "candidats" in mcp_server.soumissions(dossier="programme")
    assert "candidats" in mcp_server.comparer_soumissions("EU", "G77", dossier="programme")
    assert mcp_server.dossier("ACE")["dossier"]["slug"] == "ace"


def test_ace_migration_moves_positions_and_follows(env):
    from cop_corpus import auth, db, live

    conn = env
    live.ensure_schema(conn)
    db.meta_set(conn, "migration_ace", "0")
    uid, _ = auth.create_user(conn, "a@b.c", "A")
    conn.execute("INSERT INTO dossier_follows(user_id,dossier,created_at) VALUES(?,?,?)",
                 (uid, "technologie-capacites", "2026-11-10"))
    for key, issue in (("k1", "Action for Climate Empowerment action plan"), ("k2", "CTCN budget")):
        conn.execute("INSERT INTO positions(key,dossier,actor,issue,stance,summary,quote,source_kind,created_at,access) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (key, "technologie-capacites", "EU", issue, "soutient", "s", "q", "extraction", "2026-11-12", "public"))
    conn.commit()
    live.migrate_ace(conn)
    got = dict(conn.execute("SELECT key, dossier FROM positions").fetchall())
    assert got == {"k1": "ace", "k2": "technologie-capacites"}
    assert live.followed(conn, uid) == {"technologie-capacites", "ace"}


# --- comparer_soumissions : sous-groupes, soumission antérieure, écart de dates ----------------------------------

def _submissions_setup(conn, monkeypatch):
    from cop_corpus import fetch, groupes, soumissions as sm

    _emissions(conn, [("NPL", "Nepal"), ("TUV", "Tuvalu"), ("KEN", "Kenya"), ("PAK", "Pakistan")])
    monkeypatch.setattr(groupes, "catalog", lambda: [
        {"id": "G77 and China", "nom": "G77", "type": "onu", "source": "https://g77.test", "releve": "2026-11-17",
         "membres": ["Nepal", "Tuvalu"], "attendu": 2, "alias": ["G77", "G-77"]},
        {"id": "AGN", "nom": "Groupe africain", "type": "onu", "source": "https://un.test", "releve": "2026-11-17",
         "membres": ["Kenya"], "attendu": 1, "alias": ["African Group", "African Group of Negotiators"]}])
    groupes.sync(conn, None)
    calls = [
        {"Id": 1, "Title": "Views on Action for Climate Empowerment", "Issue": "", "SessionName": "SB 64",
         "Mandate": "N/A", "Deadline": None, "Themes": [], "Bodies": [], "TargetAudience": [],
         "CallType": "CallForSubmission", "OpenCall": True, "IsUpcomingSession": True,
         "ModifiedDate": "2026-03-01T00:00:00Z"},
        {"Id": 2, "Title": "Glasgow work programme on Action for Climate Empowerment: views", "Issue": "",
         "SessionName": "SB 60", "Mandate": "N/A", "Deadline": None, "Themes": [], "Bodies": [], "TargetAudience": [],
         "CallType": "CallForSubmission", "OpenCall": False, "IsUpcomingSession": False,
         "ModifiedDate": "2024-03-01T00:00:00Z"}]
    rows = [(1, "European Union", "", "2026-03-10"), (1, "Kenya", "on behalf of the African Group of Negotiators", "2026-03-12"),
            (2, "Pakistan", "on behalf of the Group of 77 and China", "2024-03-15"), (2, "Japan", "", "2024-03-20")]
    subs = [{"Id": i + 1, "CallForSubmissionId": c, "Title": f"{e} {c}", "FileName": f"f{i}.txt",
             "ServerRelativeURL": f"/submissions/Archive/f{i}.txt", "Language": "English",
             "SubmissionDate": f"{d}T00:00:00Z", "Description": desc, "Entity": e, "EntityType": "Party"}
            for i, (c, e, desc, d) in enumerate(rows)]
    text = "We call for a strong Action for Climate Empowerment action plan with climate education. " * 6

    class FakeClient:
        def get(self, url, *_, **__):
            if url.endswith("/CallForSubmissions"):
                return fetch.FetchResult(url=url, status=200, content=json.dumps(calls).encode(), mime="application/json")
            if url.endswith("/Submissions"):
                return fetch.FetchResult(url=url, status=200, content=json.dumps(subs).encode(), mime="application/json")
            return fetch.FetchResult(url=url, status=200, content=(text + url).encode(), mime="text/plain")

    sm.sync_index(conn, FakeClient())
    sm.download_pending(conn, FakeClient(), limit=10)
    return {r["entity"]: r["doc_id"] for r in conn.execute("SELECT entity, doc_id FROM sub_items")}


def test_compare_group_falls_back_to_subgroup_and_flags_dates(env, monkeypatch):
    from cop_corpus import mcp_server, soumissions as sm

    conn = env
    docs = _submissions_setup(conn, monkeypatch)
    assert set(sm.dossier_call_ids(conn, "ace")) == {1, 2}
    # Le G77 a soumis en 2024 (appel 2) ; sur l'appel 2026, seul le Groupe africain (sous-groupe) a répondu.
    out = mcp_server.comparer_soumissions("EU", "G77", dossier="ACE")
    assert out["appel"]["appel"] == 1
    assert out["b"]["mode"] == "membres" and out["b"]["sous_groupes_ayant_soumis"] == ["AGN"]
    assert out["b"]["derniere_soumission_propre"]["appel"] == 2
    assert "sous-groupes" in out["repli"] and "pas une position de groupe" in out["avertissement"]
    assert [(p["a"], p["b"]) for p in out["a_comparer"]] == [(docs["European Union"], docs["Kenya"])]
    # Écart de dates signalé (2026 / 2024).
    gap = sm.date_gap(conn, docs["European Union"], docs["Pakistan"])
    assert gap["ecart_jours"] > 365 and "mois" in gap["avertissement"]
    # Partie sans appel commun : dernières soumissions, avec l'écart de dates.
    out = mcp_server.comparer_soumissions("EU", "Japan", dossier="ACE")
    assert out["appel_commun"] is None and "attention" in out and "avertissement" in out["ecart_dates"]
    # Groupe : soumissions propres, des sous-groupes et des membres.
    g = mcp_server.soumissions(groupe="G77", dossier="ACE")
    assert [x["sous_groupe"] for x in g["soumissions_des_sous_groupes"]] == ["AGN"]
    assert len(g["soumissions_du_groupe"]) == 1


def test_compare_no_common_call_uses_latest_each(env, monkeypatch):
    from cop_corpus import soumissions as sm

    conn = env
    _submissions_setup(conn, monkeypatch)
    out = sm.compare_actors(conn, "Japan", "AGN", dossier="ace")
    assert out.get("appel") is None and set(out["appels"]) == {"a", "b"}
    assert "Aucun appel commun" in out["repli"]
    assert out["a_comparer"][0]["ecart_dates"]["avertissement"]


def test_co_submitters_known_names_not_overwritten(env, monkeypatch):
    from cop_corpus import db

    conn = env
    _submissions_setup(conn, monkeypatch)
    cos = db.jload(conn.execute("SELECT co_submitters FROM sub_items WHERE entity='Kenya'").fetchone()[0])
    assert cos == ["African Group of Negotiators"]


# --- positions -------------------------------------------------------------------------------------------------------

G77_STATEMENT = """Statement on behalf of the Group of 77 and China
Climate Technology Centre and Network
""" + "The Group calls for a host agreement that ensures predictable funding for the CTCN. " * 12 + """
Capacity-building
""" + "The Group urges developed countries to provide capacity-building support at scale. " * 12 + """
Action for Climate Empowerment
""" + "The Group insists that the ACE action plan must be supported by finance for developing countries. " * 12


def test_positions_sections_and_enb_sources(env, monkeypatch):
    from cop_corpus import ingest, live, llm, positions

    conn = env
    live.ensure_schema(conn)
    parts = positions.sections(G77_STATEMENT, max_chars=1400)
    assert len(parts) == 3 and parts[2].startswith("Action for Climate Empowerment")
    # Un document ENB entré par une autre voie (dépôt d'URL) est lu, grâce à son organisation.
    enb, _, _ = ingest.ingest_bytes(conn, ("The EU, supported by NORWAY, opposed by SAUDI ARABIA, called for "
                                           "language on disinformation. " * 20).encode(),
                                    url="https://enb.test/enb12909e.pdf", filename="enb.txt", source_id="depot-mcp",
                                    meta={"org": "Earth Negotiations Bulletin", "doc_type": "compte_rendu", "level": "4",
                                          "published": "2026-11-18"})
    g77, _, _ = ingest.ingest_bytes(conn, G77_STATEMENT.encode(), url="https://g77.test/s", filename="g77.txt",
                                    source_id="g77-statements-2026-crawl",
                                    meta={"doc_type": "communique", "level": "5", "published": "2026-11-10"})
    assert {d["id"] for d in positions.pending_docs(conn, limit=10)} == {enb, g77}
    monkeypatch.setattr(positions, "SECTION_CHARS", 1400)
    calls = []

    def fake(conn_, **k):
        calls.append(k["user"])
        body = k["user"].split("<<<\n", 1)[1]
        quote = next(l for l in body.split("\n") if len(l) > 60)[:120]
        return {"positions": [{"actor": "G77", "dossier": "ace" if "ACE" in quote else "technologie-capacites",
                               "issue": quote[:30], "stance": "soutient", "summary": "s", "quote": quote,
                               "said_on": ""}]}

    monkeypatch.setattr(llm, "json_call", fake)
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (g77,)).fetchone()
    assert positions.extract_doc(conn, doc) == 3 and len(calls) == 3
    assert {r[0] for r in conn.execute("SELECT dossier FROM positions")} == {"ace", "technologie-capacites"}
    # Relecture une seule fois des déclarations et comptes rendus déjà traités.
    assert positions.requeue_for_sections(conn) >= 1
    assert positions.requeue_for_sections(conn) == 0


def test_positions_divisions_inside_group(env, monkeypatch):
    from cop_corpus import groupes, live, mcp_server

    conn = env
    live.ensure_schema(conn)
    _emissions(conn, [("IND", "India"), ("TUV", "Tuvalu")])
    monkeypatch.setattr(groupes, "catalog", lambda: [
        {"id": "G77 and China", "nom": "G77", "type": "onu", "source": "https://g77.test", "releve": "2026-11-17",
         "membres": ["India", "Tuvalu"], "attendu": 2, "alias": ["G77"]}])
    groupes.sync(conn, None)
    rows = [("AOSIS", "soutient"), ("LDC", "soutient"), ("EU", "soutient"), ("Arab Group", "s'oppose"),
            ("India", "s'oppose"), ("Russian Federation", "s'oppose")]
    for i, (actor, stance) in enumerate(rows):
        conn.execute("INSERT INTO positions(key,dossier,actor,issue,stance,summary,quote,source_kind,created_at,access) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (f"k{i}", "ace", actor, "Reference to disinformation in the ACE decision", stance, "s",
                      f"quote {i}", "extraction", "2026-11-17", "public"))
    conn.execute("INSERT INTO positions(key,dossier,actor,issue,stance,summary,quote,source_kind,created_at,access) "
                 "VALUES('z','finance','AOSIS','Article 9.1','soutient','s','q','extraction','2026-11-17','public')")
    conn.commit()
    out = mcp_server.positions(divisions=True, groupe="G77", dossier="ACE")
    assert len(out["divisions"]) == 1
    d = out["divisions"][0]
    assert {p["acteur"] for p in d["pour"]} == {"AOSIS", "LDC"}
    assert {p["acteur"] for p in d["contre"]} == {"Arab Group", "India"}
    assert [p["acteur"] for p in d["allies_hors_groupe_pour"]] == ["EU"]
    assert [p["acteur"] for p in d["allies_hors_groupe_contre"]] == ["Russian Federation"]


# --- rechercher : seuil de pertinence ---------------------------------------------------------------------------------

def test_search_relevance_threshold(env):
    from cop_corpus import ingest, mcp_server

    conn = env
    ingest.ingest_bytes(conn, ("DG CLIMA press release: the Commission welcomes climate action by cities and "
                               "announces new funding for climate action. " * 8).encode(),
                        url="https://ec.test/pr", filename="pr.txt", meta={"org": "European Commission", "level": "5"})
    out = mcp_server.rechercher("Action for Climate Empowerment", organisation="European Commission")
    assert out["nb"] == 0 and out["resultats"] == [] and "nothing relevant" in out["verdict"]
    assert out["faible_pertinence"] and out["faible_pertinence"][0]["pertinence"]["lexical"] == "un"
    ingest.ingest_bytes(conn, ("The Glasgow work programme on Action for Climate Empowerment supports climate "
                               "education and public participation. " * 6).encode(),
                        url="https://unfccc.test/ace", filename="ace.txt", meta={"org": "UNFCCC", "level": "2"})
    out = mcp_server.rechercher("Action for Climate Empowerment")
    assert out["nb"] >= 1 and "verdict" not in out and "UNFCCC" in out["resultats"][0]["organisation"]


# --- crochets par thème ---------------------------------------------------------------------------------------------

V1 = """Draft decision -/CMA.8
Action for Climate Empowerment
1. Welcomes the action plan;
2. Encourages Parties to integrate [gender-responsive] approaches in climate education;
3. Requests the secretariat to prepare a report;
4. Invites Parties to submit views;
5. Decides to continue consideration of this matter.
"""
V2 = V1.replace("3. Requests the secretariat to prepare a report;",
                "3. Requests the secretariat to prepare a report [including on countering climate disinformation];")
V3 = V2.replace("[gender-responsive]", "gender-responsive")


def test_crochets_by_theme(env):
    from cop_corpus import ingest, mcp_server

    conn = env
    ids = []
    for i, text in enumerate((V1, V2, V3), 1):
        d, _, st = ingest.ingest_bytes(conn, text.encode(), url=f"https://x/ace-v{i}.txt", filename=f"v{i}.txt",
                                       meta={"title": f"Draft text on Action for Climate Empowerment version {i}",
                                             "doc_type": "projet_texte", "level": "2"})
        assert st == "indexed"
        ids.append(d)
    out = mcp_server.crochets(theme="désinformation", texte=str(ids[1]))
    assert out["nb"] == 1 and "disinformation" in out["crochets"][0]["crochet"]
    assert out["crochets"][0]["entre_crochets_depuis"]["version"] == 2
    assert [h["etat"] for h in out["crochets"][0]["historique"]] == ["absent", "entre crochets"]
    out = mcp_server.crochets(theme="gender", texte=str(ids[1]))
    assert out["crochets"][0]["entre_crochets_depuis"]["version"] == 1 and out["crochets"][0]["paragraphe"] == "§2"
    # Version 3 : le crochet sur le genre est levé, celui sur la désinformation reste (depuis la version 2).
    assert mcp_server.crochets(theme="genre", texte=str(ids[2]))["nb"] == 0
    out = mcp_server.crochets(theme="disinformation, misinformation", texte=str(ids[2]))
    assert out["crochets"][0]["entre_crochets_depuis"]["version"] == 2
    assert "erreur" in mcp_server.crochets(theme="genre")


# --- deposer_url : date et titre lus dans le contenu -------------------------------------------------------------------

def test_deposer_url_checks_date_and_title(env, monkeypatch):
    from cop_corpus import fetch, mcp_server

    conn = env
    page = ("<html><head><title>Earth Negotiations Bulletin COP 31 #8</title></head><body>"
            "<p>Earth Negotiations Bulletin — Vol. 12 No. 909 — Wednesday, 18 November 2026</p>"
            + "<p>Antalya Climate Change Conference: Tuesday, 17 November 2026. Parties discussed the cover decision "
              "and Action for Climate Empowerment in informal consultations.</p>" * 6 + "</body></html>")

    class FakeClient:
        def get(self, url, *_, **__):
            return fetch.FetchResult(url=url, status=200, content=page.encode(), mime="text/html")

    monkeypatch.setattr(fetch, "Client", FakeClient)
    out = mcp_server.deposer_url("https://enb.test/enb12909e.html", titre="ENB Daily report for 17 November 2026 (COP31)",
                                 niveau="4", type="compte_rendu", organisation="Earth Negotiations Bulletin")
    v = out["verification"]
    assert v["date_lue_dans_le_document"] == "2026-11-18"
    assert any("2026-11-17" in e for e in v["ecarts"])
    row = conn.execute("SELECT published, title FROM documents WHERE id=?", (out["doc_id"],)).fetchone()
    assert row["published"] == "2026-11-18" and "2026-11-18" in row["title"] and "17 November" not in row["title"]
