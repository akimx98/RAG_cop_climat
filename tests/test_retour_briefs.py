"""Retour d'expérience de trois briefs (18 novembre 2026) : lignée des textes de session quand la cote change
d'organe, diff aligné par contenu, renumérotations écartées, pertinence partielle, expressions « Article 6,
paragraph 8 », doublons de l'index, positions en double, sujets qui se recoupent."""

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
    from cop_corpus import live

    live._ready.clear()
    return db.get()


V1 = """Draft decision -/CP.31
Host of the Climate Technology Centre
1. Welcomes the report of the evaluation panel;
2. Decides that the United Nations Environment Programme shall host the Centre for five years;7
3. Requests the secretariat to prepare the memorandum of understanding referred to in paragraph 5 below;
4. [Invites Parties to provide resources;]
5. Decides to consider the memorandum of understanding at its thirty-second session.
"""
# Un paragraphe inséré en tête : tout se décale d'un cran ; note 7 → 6 ; renvoi « paragraph 5 » → « 6 ».
V2 = """Draft decision -/CP.31
Host of the Climate Technology Centre
1. Welcomes the report of the evaluation panel;
2. Notes with appreciation the proposals received from candidate host organizations;
3. Decides that the United Nations Environment Programme shall host the Centre for five years;6
4. Requests the secretariat to prepare the memorandum of understanding referred to in paragraph 6 below;
5. Invites Parties to provide resources;
6. Decides to consider the memorandum of understanding at its thirty-second session.
"""


def _ingest(conn, text, name, title):
    from cop_corpus import ingest

    d, _, st = ingest.ingest_bytes(conn, text.encode(), url=f"https://docs.test/{name}.txt", filename=f"{name}.txt",
                                   meta={"title": title, "level": "2"}, source_id="live-texts")
    assert st == "indexed"
    return d


def test_diff_aligns_by_content_and_ignores_renumbering(env):
    from cop_corpus import diff

    conn = env
    a, b = _ingest(conn, V1, "v1", "Host v1"), _ingest(conn, V2, "v2", "Host v2")
    res = diff.compare(conn, a, b)
    by_para = {c["paragraphe"]: c for c in res["changements"]}
    assert by_para["§2"]["changement"] == "ajouté"
    # §3 (ancien §2) et §4 (ancien §3) : seuls des nombres changent → pas un changement de fond
    assert "§3" not in by_para and "§4" not in by_para and set(res["renumerotes"]["paragraphes"]) == {"§3", "§4"}
    # Crochets levés sur l'ancien §4, devenu §5 : comparé à son ancien texte, pas au nouveau §4
    assert by_para["§5"]["changement"] == "modifié" and by_para["§5"]["ancien_paragraphe"] == "§4"
    assert by_para["§5"]["similarite"] > 0.8
    assert res["avant"]["paragraphes_entre_crochets"] == 1 and res["apres"]["paragraphes_entre_crochets"] == 0
    assert res["deplaces_sans_changement"] == 1   # ancien §5 → §6, identique


def test_lineage_follows_item_and_title_across_bodies(env):
    from cop_corpus import diff, live, mcp_server

    conn = env
    live.ensure_schema(conn)
    d1, d2 = _ingest(conn, V1, "ctc1", "x"), _ingest(conn, V2, "ctc2", "y")
    rows = [("DT.SBI65.i13b.2", "SBI 65", "2026-11-14T12:00", d1, "https://docs.test/ctc1.txt"),
            ("DT.COP31.i9b_CMA8.i12b.1", "CMA 8", "2026-11-17T22:30", d2, "https://docs.test/ctc2.txt")]
    for sym, body, pub, doc, pdf in rows:
        conn.execute("""INSERT INTO live_texts(key,symbol,title,cb_type,body,dossier,published,doc_id,pdf_url,
                        first_seen,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                     (sym, sym, "Host of the Climate Technology Centre", "draft text", body, "technologie-capacites",
                      pub, doc, pdf, live.now_iso(), live.now_iso()))
    # Ligne de l'index sans cote ni titre pour le même PDF : doublon vide
    conn.execute("INSERT INTO live_texts(key,pdf_url,title,dossier,doc_id,first_seen,updated_at) "
                 "VALUES(?,?,'', 'technologie-capacites',?,?,?)",
                 ("https://docs.test/ctc1.txt", "https://docs.test/ctc1.txt", d1, live.now_iso(), live.now_iso()))
    conn.commit()
    out = live.tidy_texts(conn)
    assert out["doublons"] == 1 and out["lignees"] == 2
    doc2 = conn.execute("SELECT * FROM documents WHERE id=?", (d2,)).fetchone()
    assert doc2["symbol"] == "DT.COP31.i9b_CMA8.i12b.1" and doc2["doc_type"] == "projet_texte" and doc2["version"] == 2
    assert diff.previous_in_lineage(conn, d2) == d1
    res = mcp_server.comparer_versions(str(d2))
    assert res["ancienne_version"] == d1 and res["nouvelle_version"] == d2
    assert live.tidy_texts(conn) == {"doublons": 0, "metadonnees": 0, "lignees": 0}   # idempotent
    # Réindexation : la lignée par dossier et titre est conservée
    from cop_corpus import ingest

    ingest.index_document(conn, d2)
    assert conn.execute("SELECT lineage_key FROM documents WHERE id=?", (d2,)).fetchone()[0].startswith("neg:")


def test_article_paragraph_is_one_phrase():
    from cop_corpus import alerts

    q = "non-market approaches, Article 6, paragraph 8, art. 6.8"
    assert alerts.fragments(q) == ["non-market approaches", "Article 6, paragraph 8", "art. 6.8"]
    fts = alerts.sub_to_fts(q)
    assert '"Article 6 paragraph 8"' in fts and '"paragraph 8"' not in fts.replace('"Article 6 paragraph 8"', "")
    assert alerts.generic_fragments("paragraph 8, fossil fuels, 11(b), LCIPP, YOUNGO") == ["paragraph 8", "11(b)"]


def test_overlapping_topics():
    from cop_corpus import alerts

    subs = [{"id": 9, "libelle": "Peuples autochtones", "requete": '"indigenous peoples", LCIPP'},
            {"id": 11, "libelle": "Peuples autochtones", "requete": "Indigenous Peoples, LCIPP, land rights"},
            {"id": 5, "libelle": "Tout", "requete": "*"},
            {"id": 12, "libelle": "Eau", "requete": "freshwater, water security"},
            {"id": 8, "libelle": "Amazonie", "requete": 'Amazon, Amazonia, "Amazon rainforest", "Amazon basin", ACTO, '
                                                         'TFFF, Amazonie, "forêt amazonienne", "Tropical Forests Forever"'},
            {"id": 10, "libelle": "Amazonie et orpaillage", "requete": "Amazon, Amazonia, Amazonie, gold mining, "
                                                                       "mercury, orpaillage, Tropical Forests Forever"}]
    over = alerts.overlaps(subs)
    assert [o["ids"] for o in over] == [[9, 11], [8, 10]]


def test_partial_relevance_is_not_nothing(env):
    from cop_corpus import ingest, mcp_server

    conn = env
    ingest.ingest_bytes(conn, ("65. Calls on Parties to contribute to transitioning away from fossil fuels in energy "
                               "systems, including through a roadmap. " * 4).encode(), url="https://x/cover",
                        filename="cover.txt", meta={"level": "2", "doc_type": "projet_texte"})
    for i in range(6):  # « electrification » rare : son absence pèse, mais pas assez pour dire « rien »
        ingest.ingest_bytes(conn, f"Note {i} on energy systems and fossil fuels markets in region {i}.".encode() * 3,
                            url=f"https://x/n{i}", filename=f"n{i}.txt", meta={"level": "4"})
    out = mcp_server.rechercher("transitioning away from fossil fuels roadmap electrification")
    assert out["nb"] == 0 and out["pertinence_partielle"]
    assert out["pertinence_partielle"][0]["termes_manquants"] == ["electrification"]
    assert out["verdict"].startswith("partial relevance")


def test_positions_dedupe_and_actor_names(env):
    from cop_corpus import live, positions

    conn = env
    live.ensure_schema(conn)
    src = "The Arab Group rejected the cover text over fossil fuels and the electrification goal entirely."
    item = {"actor": "Arab Group Statement", "dossier": "couverture", "issue": "fossiles", "stance": "s'oppose",
            "summary": "s", "quote": "rejected the cover text over fossil fuels", "said_on": ""}
    assert positions._store(conn, [item], src, kind="extraction", label="CH", url="u1", doc_id=1,
                            default_date="2026-11-17") == 1
    assert positions._store(conn, [item], src, kind="extraction", label="CH", url="u2", doc_id=2,
                            default_date="2026-11-17") == 0   # même article, autre flux
    assert conn.execute("SELECT actor FROM positions").fetchone()[0] == "Arab Group"
    assert positions.normalize_actor("Presidence COP31") == "Présidence COP31"
    assert positions.normalize_actor("G-77/China") == "G77 and China"
    assert positions._past("2099-11-22") == "2098-11-22" and positions._past("2026-11-10") == "2026-11-10"


def test_test_party_is_not_an_author():
    from cop_corpus import soumissions

    assert all(soumissions._test_author(x) for x in ("test-Party", "testtt", "Test submission please ignore"))
    assert not any(soumissions._test_author(x) for x in ("Testbiotech", "Tanzania", "European Union"))


def test_local_time():
    from cop_corpus import live

    assert live.local_time("2026-11-17T22:30") == "2026-11-17T22:30+03:00"
    assert live.local_time("2026-11-17T21:25:35Z") == "2026-11-17T21:25:35Z"
    assert live.local_time(None) is None
