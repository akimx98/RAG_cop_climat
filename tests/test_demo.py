"""Démo publique « Essayer » : lecture seule, documents publics seulement, citations vérifiées, HTML sûr, quotas."""

import importlib

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COP_EMBED_PROVIDER", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("COP_LOCAL_ADMIN", "1")   # même si le MCP local est administrateur, la démo ne l'est pas
    from cop_corpus import config

    importlib.reload(config)
    from cop_corpus import db

    importlib.reload(db)
    for mod in ("embed", "auth", "search", "ingest", "alerts", "diff", "llm", "demo"):
        importlib.reload(importlib.import_module(f"cop_corpus.{mod}"))
    return db.get()


def test_demo_tools_read_only_and_public_only(env):
    from cop_corpus import demo, ingest

    conn = env
    ingest.ingest_bytes(conn, ("Confidential negotiating mandate on adaptation finance " * 20).encode(), url=None,
                        access="restreint", filename="note.txt")
    ingest.ingest_bytes(conn, ("Parties shall pursue domestic mitigation measures on adaptation finance " * 20).encode(),
                        url=None, filename="public.txt")
    names = {s["function"]["name"] for s in demo.tool_specs()}
    assert {"suivre", "deposer_url", "mes_sujets", "comparer_soumissions"}.isdisjoint(names)
    assert "rechercher" in names
    for spec in demo.tool_specs():
        assert "mes_sujets" not in spec["function"]["parameters"].get("properties", {})
    assert demo.call_tool("suivre", {"mots_cles": "x"})["erreur"].startswith("unknown tool")
    res = demo.call_tool("rechercher", {"requete": "adaptation finance", "limite": 10})
    texts = " ".join(str(h) for h in res["resultats"] + res.get("faible_pertinence", []))
    assert "Confidential negotiating mandate" not in texts
    assert "domestic mitigation" in texts


def test_quote_check_and_safe_html(env):
    from cop_corpus import demo

    refs = demo.Refs()
    refs.sources["S1"] = {"ref": "S1", "passage_id": None, "text": "Parties shall pursue domestic mitigation "
                          "measures, with the aim of achieving the objectives of such contributions."}
    out = demo.render('It says "shall pursue domestic mitigation measures" [S1] but not "shall achieve its NDC '
                      'target" [S1]. <script>alert(1)</script> [S9]\n- **bold** *it*', refs, env)
    assert [c["ok"] for c in out["checks"]] == [True, False]
    assert "<script>" not in out["html"] and "&lt;script&gt;" in out["html"]
    assert 'class="q ok"' in out["html"] and 'class="q unverified"' in out["html"]
    assert "S9" not in out["html"]                 # référence inventée : ignorée
    assert "<b>bold</b>" in out["html"] and "<i>it</i>" in out["html"]
    assert out["order"] == ["S1"]
    assert demo.quote_found("pursue domestic mitigation … of such contributions", refs.sources["S1"]["text"])


def test_quota_per_session_and_budget(env, monkeypatch):
    from cop_corpus import demo

    conn = env
    session = "s" * 20
    assert demo.quota(conn, session, "1.1.1.1", None)["remaining"] == demo.PER_SESSION
    for _ in range(demo.PER_SESSION):
        demo.save(conn, question="q?", lang="en", session=session, ip="1.1.1.1", user=None, payload={"cost": 0})
    q = demo.quota(conn, session, "1.1.1.1", None)
    assert q["remaining"] == 0 and q["reason"] == "session"
    # Erreur API : ne consomme pas de question.
    demo.save(conn, question="q?", lang="en", session="t" * 20, ip="2.2.2.2", user=None, payload=None, error="x")
    assert demo.quota(conn, "t" * 20, "2.2.2.2", None)["remaining"] == demo.PER_SESSION
    monkeypatch.setattr(demo, "DAILY_USD", 0.0)
    assert demo.quota(conn, "u" * 20, "3.3.3.3", None)["reason"] == "budget"
    row = conn.execute("SELECT ip_hash FROM demo_answers LIMIT 1").fetchone()
    assert "1.1.1.1" not in row["ip_hash"]


def test_every_demo_tool_hides_restricted(env):
    """Chaque outil exposé à la démo, appelé avec la cote, l'id ou un passage d'un document interne : rien ne sort."""
    import json

    from cop_corpus import demo, ingest

    conn = env
    secret = "Zanzibar confidential fallback QXSECRETTOKEN position on loss and damage "
    doc_id = ingest.ingest_bytes(conn, (secret * 30).encode(), url=None, access="restreint", filename="secret.txt")
    doc_id = doc_id[0] if isinstance(doc_id, (list, tuple)) else doc_id
    pid = conn.execute("SELECT id FROM passages WHERE doc_id=? LIMIT 1", (doc_id,)).fetchone()[0]
    probes = {"rechercher": {"requete": "Zanzibar confidential fallback"}, "lire_passage": {"passage_id": pid},
              "document": {"reference": str(doc_id)}, "comparer_versions": {"document_recent": str(doc_id)},
              "crochets": {"theme": "finance", "texte": str(doc_id)}, "nouveautes": {"depuis": "30j"},
              "dossiers": {"textes": True}, "positions": {}, "programme": {"sujet": "Zanzibar"},
              "pavillons": {"sujet": "Zanzibar"}, "soumissions": {"requete": "Zanzibar"}, "groupes": {},
              "glossaire": {"terme": "Zanzibar"}, "donnees": {"pays": "FRA"}}
    names = {s["function"]["name"] for s in demo.tool_specs()}
    assert "etat_corpus" not in names
    for name in names:
        out = json.dumps(demo.call_tool(name, probes.get(name, {})), ensure_ascii=False, default=str)
        assert "QXSECRETTOKEN" not in out, name
        assert "secret.txt" not in out, name


def test_quote_order_brackets_and_unsourced(env):
    from cop_corpus import demo

    src = "Parties shall pursue domestic mitigation measures [, including through markets,] with the aim of " \
          "achieving the objectives of such contributions. Parties urge the secretariat to report."
    assert demo.quote_status("Parties shall pursue domestic mitigation measures [, including through markets,]", src) == "ok"
    assert demo.quote_status("shall pursue domestic … the objectives of such", src) == "ok"
    assert demo.quote_status("the objectives of such … shall pursue domestic", src) == "no"      # ordre inversé
    assert demo.quote_status("the … report", src) == "no"                                        # morceaux trop courts
    assert demo.quote_status("domestic mitigation measures, including through markets, with", src) == "brackets"
    refs = demo.Refs()
    out = demo.render('As noted, "governments must phase out coal by 2030" without a source. \x00 x', refs, env)
    assert out["checks"] == [{"quote": "governments must phase out coal by 2030", "ref": None, "ok": False,
                              "status": "unsourced"}]
    assert "unverified" in out["html"]


def test_demo_spend_separate_from_global_budget(env):
    from types import SimpleNamespace

    from cop_corpus import live, llm

    conn = env
    live.ensure_schema(conn)
    llm._record(conn, "gpt-5.6-terra", "demo", SimpleNamespace(prompt_tokens=10_000_000, completion_tokens=0))
    assert llm.spent(conn) == 0
    llm._record(conn, "gpt-5.6-terra", "positions", SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=0))
    assert llm.spent(conn) == pytest.approx(2.0)


def test_signed_links_follow_access(env):
    from cop_corpus import demo, ingest

    conn = env
    doc_id = ingest.ingest_bytes(conn, ("Public text on adaptation " * 30).encode(), url=None, filename="a.txt")
    doc_id = doc_id[0] if isinstance(doc_id, (list, tuple)) else doc_id
    src = [{"ref": "S1", "doc_id": doc_id, "text": "Public text", "url": "https://example.org/a"}]
    assert not demo.sign_links(conn, src)[0].get("withdrawn")
    conn.execute("UPDATE documents SET access='restreint' WHERE id=?", (doc_id,))
    conn.commit()
    out = demo.sign_links(conn, src)[0]
    assert out["withdrawn"] and "text" not in out and "pdf" not in out


def test_three_questions_also_for_signed_in_users(env):
    """Connecté ou non : 3 questions ; un compte ne retrouve pas ses questions en changeant de navigateur."""
    from cop_corpus import demo

    conn = env
    user = {"id": 42}
    for i in range(3):
        assert demo.quota(conn, "a" * 20, "4.4.4.4", user)["remaining"] == 3 - i
        demo.save(conn, question="q?", lang="fr", session="a" * 20, ip="4.4.4.4", user=user, payload={"cost": 0})
    assert demo.quota(conn, "a" * 20, "4.4.4.4", user)["remaining"] == 0
    assert demo.quota(conn, "b" * 20, "5.5.5.5", user)["reason"] == "account"   # autre navigateur, autre réseau


def test_follow_up_only_from_own_session(env):
    from cop_corpus import demo

    conn = env
    refs = demo.Refs()
    refs.sources["S1"] = {"ref": "S1", "text": "x"}
    aid = demo.save(conn, question="q?", lang="fr", session="m" * 20, ip="1.1.1.1", user=None,
                    payload={"cost": 0, "response_id": "resp_1", "state": refs.state()})
    ctx = demo.follow_context(conn, aid, "m" * 20)
    assert ctx["response_id"] == "resp_1" and "S1" in demo.Refs(ctx["state"]).sources
    assert demo.follow_context(conn, aid, "n" * 20) is None        # lien partagé : pas de reprise par un autre
    nxt = demo.Refs(ctx["state"])
    assert nxt.add_source({"passage_id": 7, "texte": "y"}) == "S2"   # la numérotation continue


def test_admins_have_no_demo_limit_and_separate_spend(env, monkeypatch):
    """Administrateurs : démos sans plafond (questions, IP, budget) ; leur coût ne touche ni le budget de la démo
    publique ni le budget interne."""
    from types import SimpleNamespace

    from cop_corpus import demo, live, llm

    conn = env
    live.ensure_schema(conn)
    admin = {"id": 7, "is_admin": 1, "active": 1}
    for _ in range(demo.PER_SESSION + demo.PER_IP_DAY + 2):
        demo.save(conn, question="q?", lang="fr", session="a" * 20, ip="4.4.4.4", user=admin, payload={"cost": 0})
    monkeypatch.setattr(demo, "DAILY_USD", 0.0)
    q = demo.quota(conn, "a" * 20, "4.4.4.4", admin)
    assert q["unlimited"] and q["remaining"] > 0 and q["reason"] is None
    assert demo.quota(conn, "b" * 20, "4.4.4.4", {"id": 8})["reason"] == "budget"     # les autres restent bornés
    assert demo.purpose_for(admin) == "demo-admin" and demo.purpose_for({"id": 8}) == "demo"
    monkeypatch.setattr(demo, "DAILY_USD", 8.0)
    llm._record(conn, "gpt-5.6-terra", "demo-admin", SimpleNamespace(prompt_tokens=10_000_000, completion_tokens=0))
    assert llm.spent(conn) == 0 and demo._spent(conn) == 0
    assert not demo.unlimited({"id": 9, "is_admin": 1, "active": 0})
