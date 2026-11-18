"""Mesure d'usage : journaux sans secrets, événements MCP/Telegram/site, rétention, pseudonymes, opposition, page admin."""

import importlib
import json
import logging

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
    from cop_corpus import live, usage

    live._ready.clear()
    usage._ready.clear()
    return db.get()


def _events(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM usage_events ORDER BY id")]


# --- journaux techniques ----------------------------------------------------------------------------------------------

def test_access_log_hides_tokens_and_ips():
    from cop_corpus import logs

    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("203.0.113.57:5123", "POST", "/u/abcdefghijklmnopqrstuv/mcp", "1.1", 200), None)
    logs.RedactFilter().filter(rec)
    line = rec.getMessage()
    assert "abcdefghijklmnop" not in line and "/u/***/mcp" in line and "203.0.113.0" in line and ".57" not in line
    for path in ("/entrer/12.1799.sig", "/login/tok", "/join/CODE", "/rejoindre/CODE?x=1"):
        assert "***" in logs.redact(path) and path.split("/")[2].split("?")[0] not in logs.redact(path)
    assert logs.redact("/doc/12?passage=3") == "/doc/12?passage=3"
    assert logs.mask_ip("[2001:db8:1:2::5]:443") == "2001:db8:1::"
    assert logs.mask_ip("testclient") == "-"
    cfg = logs.uvicorn_log_config()
    assert "redact" in cfg["handlers"]["access"]["filters"]


# --- événements -------------------------------------------------------------------------------------------------------

def test_record_pseudonym_text_rules_and_dossiers(env):
    from cop_corpus import auth, usage

    conn = env
    code = auth.invite_settings(conn)["code"]
    guest, _, _ = auth.signup(conn, code, "g@test.org", profil="presse")
    assert guest["profil"] == "presse"
    colleague_id, _ = auth.create_user(conn, "c@test.org", "C")        # accès restreint (délégation)
    colleague = auth.by_id(conn, colleague_id)

    usage.record("mcp", "tool", "rechercher", guest, args={"requete": "global goal on adaptation indicators",
                                                           "niveaux": ["1"], "ctx": None}, n=0, outcome="vide")
    usage.record("mcp", "tool", "rechercher", colleague, args={"requete": "our fallback position on NCQG"}, n=0,
                 outcome="vide")
    usage.record("mcp", "tool", "dossier", guest, args={"slug": "ACE"})
    ev = _events(conn)
    assert ev[0]["uid"] == usage.uid(guest["id"]) and str(guest["id"]) != ev[0]["uid"] and "g@test" not in json.dumps(ev)
    assert json.loads(ev[0]["text"]) == {"requete": "global goal on adaptation indicators"}
    assert json.loads(ev[0]["params"]) == {"niveaux": ["1"]}
    assert "adaptation" in ev[0]["dossiers"]
    assert ev[1]["text"] is None and ev[1]["dossiers"] and "finance" in ev[1]["dossiers"]   # restreint : pas de texte
    assert ev[2]["dossiers"] == "ace"
    usage.record("mcp", "tool", "rechercher", guest, args={"requete": "loss and damage fund"}, n=5, outcome="ok")
    assert _events(conn)[-1]["text"] is None                  # recherche fructueuse : texte jamais gardé

    # Opposition : plus rien d'enregistré, l'historique est effacé.
    conn.execute("UPDATE users SET usage_optout=1 WHERE id=?", (guest["id"],))
    usage.forget_user(conn, guest["id"], demo=False)
    usage.record("mcp", "tool", "glossaire", auth.by_id(conn, guest["id"]))
    assert {e["uid"] for e in _events(conn)} == {usage.uid(colleague_id)}

    # Suppression du compte : ses événements partent avec lui.
    g2, _, _ = auth.signup(conn, code, "h@test.org")
    usage.record("telegram", "command", "/cherche", g2, args={"texte": "loss and damage fund"})
    assert auth.delete_guest(conn, g2["id"])
    assert not conn.execute("SELECT 1 FROM usage_events WHERE uid=?", (usage.uid(g2["id"]),)).fetchone()


def test_retention_purge(env):
    from cop_corpus import usage

    conn = env
    usage.ensure_schema(conn)
    rows = [("2020-01-01T00:00:00Z", '{"requete": "old"}'), ("2099-01-01T00:00:00Z", '{"requete": "new"}')]
    old_text = "2000-01-01T00:00:00Z"
    conn.execute("INSERT INTO usage_events(at,channel,kind,name,text) VALUES(?,?,?,?,?)",
                 (old_text, "mcp", "tool", "rechercher", '{"requete": "very old"}'))
    for at, text in rows:
        conn.execute("INSERT INTO usage_events(at,channel,kind,name,text) VALUES(?,?,?,?,?)",
                     (at, "mcp", "tool", "rechercher", text))
    from datetime import datetime, timedelta, timezone

    recent_but_text_expired = (datetime.now(timezone.utc) - timedelta(days=usage.TEXT_DAYS + 2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT INTO usage_events(at,channel,kind,name,text) VALUES(?,?,?,?,?)",
                 (recent_but_text_expired, "mcp", "tool", "rechercher", '{"requete": "mid"}'))
    conn.commit()
    out = usage.purge(conn)
    left = {r["at"]: r["text"] for r in conn.execute("SELECT at, text FROM usage_events")}
    assert old_text not in left and "2020-01-01T00:00:00Z" not in left       # au-delà de la conservation : supprimés
    assert left[recent_but_text_expired] is None                             # texte effacé, événement gardé
    assert left["2099-01-01T00:00:00Z"] == '{"requete": "new"}'
    assert out["evenements_supprimes"] == 2


def test_tool_outcomes():
    from cop_corpus import usage

    assert usage.outcome_of("rechercher", {"nb": 0, "pertinence_partielle": [{}]}) == (0, "partiel", None)
    assert usage.outcome_of("rechercher", {"nb": 0, "resultats": []}) == (0, "vide", None)
    assert usage.outcome_of("rechercher", {"nb": 4}) == (4, "ok", None)
    assert usage.outcome_of("document", {"erreur": "document not found: X"})[1] == "refus"
    assert usage.outcome_of("nouveautes", {"depuis": "x", "nb": 0, "documents": []}) == (0, "vide", None)
    assert usage.outcome_of("positions", {"positions": [1, 2]}) == (2, "ok", None)
    assert usage.outcome_of("dossier", {"dossier": {}, "etat": [], "textes": [1], "credit": "x"}) == (1, "ok", None)
    assert usage.outcome_of("donnees", {"pays": "FRA", "series": {}}) == (None, "ok", None)
    assert usage.client_label("openai-mcp/1.0.0") == "ChatGPT" and usage.client_label("claude-ai 0.1") == "Claude"


def test_mcp_calls_are_measured_end_to_end(env):
    """Requêtes JSON-RPC réelles via /u/<jeton>/mcp : connexion (client), appel d'outil, prompt, issue et durée."""
    from starlette.testclient import TestClient
    from cop_corpus import auth, server, usage

    conn = env
    code = auth.invite_settings(conn)["code"]
    guest, token, _ = auth.signup(conn, code, "g@test.org")
    server._hits.clear()   # limiteur en mémoire, partagé avec les autres tests (mêmes identifiants de compte)
    hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
           "User-Agent": "openai-mcp/1.0.0"}
    with TestClient(server.build_app(), base_url="http://localhost") as client:
        url = f"/u/{token}/mcp"
        r = client.post(url, headers=hdr, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude-ai", "version": "0.1"}}})
        assert r.status_code == 200
        r = client.post(url, headers={**hdr, "mcp-protocol-version": "2025-06-18"}, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "glossaire", "arguments": {"terme": "zzz-introuvable"}}})
        assert r.status_code == 200 and "result" in r.json()
        r = client.post(url, headers={**hdr, "mcp-protocol-version": "2025-06-18"}, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "glossaire", "arguments": {}}})
        assert r.status_code == 200
        client.post(url, headers={**hdr, "mcp-protocol-version": "2025-06-18"}, json={
            "jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}})          # bruit : non journalisé
    ev = _events(conn)
    assert [e["kind"] for e in ev] == ["connect", "tool", "tool"]
    assert ev[0]["client"] == "claude-ai 0.1" and ev[1]["client"] == "openai-mcp/1.0.0"
    assert ev[1]["name"] == "glossaire" and ev[1]["outcome"] == "refus" and json.loads(ev[1]["text"]) == {
        "terme": "zzz-introuvable"}
    assert ev[2]["outcome"] == "ok" and ev[2]["ms"] is not None
    assert all(e["uid"] == usage.uid(guest["id"]) for e in ev)
    assert token not in json.dumps(ev)


def test_telegram_web_and_admin_page(env, monkeypatch):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from cop_corpus import auth, telegram, usage, web, web_live

    importlib.reload(web)
    importlib.reload(web_live)
    conn = env
    code = auth.invite_settings(conn)["code"]
    guest, _, _ = auth.signup(conn, code, "g@test.org", profil="ong")
    conn.execute("UPDATE users SET telegram_chat_id='42' WHERE id=?", (guest["id"],))
    conn.commit()
    monkeypatch.setattr(telegram, "send", lambda *a, **k: None)
    telegram.handle(conn, {"message": {"chat": {"id": 42}, "text": "/search loss and damage fund"}})
    ev = _events(conn)[-1]
    assert ev["channel"] == "telegram" and ev["name"] == "/cherche" and "pertes-prejudices" in (ev["dossiers"] or "")
    assert ev["text"] is None                     # issue inconnue : le texte n'est pas gardé

    admin_id, _ = auth.create_user(conn, "a@test.org", "A", admin=True)
    auth.set_password(conn, admin_id, "mot-de-passe-admin")
    app = Starlette(routes=[*web.routes, *web_live.routes])
    admin = TestClient(app, base_url="https://testserver")
    admin.post("/connexion", data={"email": "a@test.org", "password": "mot-de-passe-admin"})
    admin.get("/database?q=adaptation")
    admin.get("/database/negotiations/finance")
    page = admin.get("/admin/usage?jours=7").text
    assert "Typologie" in page and "/cherche" in page and "ONG" in page
    # Les visites de l'administrateur sont exclues par défaut, incluses sur demande.
    assert "recherche" not in page.split("Outils, prompts")[1].split("Dossiers de")[0]
    assert "recherche" in admin.get("/admin/usage?jours=7&admins=1").text

    # Page publique de confidentialité, liée depuis le pied de page ; profil et opposition sur le compte.
    anon = TestClient(app, base_url="https://testserver")
    assert "/confidentialite" in anon.get("/").text
    priv = anon.get("/privacy?lang=en").text
    assert "OpenAI" in priv and "30 days" in priv and "never" in priv
    assert anon.get("/admin/usage").status_code in (302, 303, 403)
    acc = admin.get("/account").text
    assert 'name="profil"' in acc and 'name="usage_optout"' in acc
