"""Création de compte et connexion, source par source, avec les requêtes exactes de la fenêtre de connexion
(JSON, champs envoyés par le navigateur à chaque étape).

- Invitation (lien ou QR /join/<code>, ou code saisi) : e-mail seul, pas de mot de passe, connecté tout de suite ;
  ensuite, sur un autre appareil : e-mail → code à 6 chiffres.
- Inscription libre (/join) : e-mail + mot de passe (8 caractères, sans confirmation obligatoire).
- Connexion avec mot de passe ; mot de passe oublié : code, puis nouveau mot de passe.
- Compte créé par un administrateur : lien de connexion 24 h."""

import importlib
import re
from pathlib import Path

import pytest

FETCH = {"X-Requested-With": "fetch"}


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
    for mod in ("embed", "auth", "search", "ingest", "alerts", "diff", "telegram", "mailin", "web", "web_live"):
        importlib.reload(importlib.import_module(f"cop_corpus.{mod}"))
    from cop_corpus import live, mailout, usage

    live._ready.clear()
    usage._ready.clear()
    outbox: list[tuple[str, str]] = []
    monkeypatch.setattr(mailout, "send", lambda to, subject, text, **kw: outbox.append((to, subject)) or True)
    return db.get(), outbox


def _client():
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from cop_corpus import web, web_live

    return TestClient(Starlette(routes=[*web.routes, *web_live.routes]), base_url="https://testserver")


def _code(outbox) -> str:
    return re.findall(r"\d{6}", outbox[-1][1])[-1]


def test_modal_never_drops_the_email():
    """La fenêtre désactivait le champ e-mail aux étapes mot de passe et code : le navigateur ne l'envoyait plus,
    et toute connexion échouait (« incorrect », « aucun compte »). L'e-mail doit rester actif."""
    src = (Path(__file__).resolve().parents[1] / "src/cop_corpus/templates/_auth.html").read_text(encoding="utf-8")
    assert "input:not([name=email])" in src
    assert "fetch(form.getAttribute('action')" in src


def test_invitation_email_only_then_code_on_other_device(env):
    from cop_corpus import auth

    conn, outbox = env
    code = auth.invite_settings(conn)["code"]
    c = _client()
    page = c.get(f"/join/{code}").text
    assert 'data-open="signup"' in page and 'id="su-pw"' not in page          # pas de mot de passe demandé
    j = c.post(f"/join/{code}", data={"code": code, "email": "Ana@NGO.org", "organisation": "", "profil": "ong",
                                     "next": "/"}, headers=FETCH).json()
    assert j == {"redirect": "/?welcome=1"}
    user = auth.by_email(conn, "ana@ngo.org")
    assert user["source"] == "invitation" and not user["password_hash"] and user["profil"] == "ong"
    assert "Welcome" in c.get("/?welcome=1").text                           # connecté tout de suite

    # Autre appareil : e-mail → code (étape 1), puis e-mail + code, comme les envoie la fenêtre (étape 2).
    other = _client()
    j = other.post("/signin", data={"next": "/", "email": "ana@ngo.org"}, headers=FETCH).json()
    assert j["step"] == "code" and j["email"] == "ana@ngo.org" and len(outbox) == 1 and outbox[0][0] == "ana@ngo.org"
    j = other.post("/signin", data={"next": "/", "email": "ana@ngo.org", "code": "000000" if _code(outbox) != "000000"
                                    else "111111"}, headers=FETCH).json()
    assert j["step"] == "code" and "Wrong" in j["error"]
    j = other.post("/signin", data={"next": "/topics", "email": "ana@ngo.org", "code": _code(outbox)}, headers=FETCH).json()
    assert j == {"redirect": "/account?first=1"}                 # première preuve : « ce n'est pas moi » proposé
    assert "Not me: secure everything" in other.get("/account?first=1").text
    other.cookies.clear()                                        # ensuite : retour à la page demandée
    conn.execute("UPDATE login_codes SET sent_at='[]' WHERE user_id=?", (user["id"],))
    conn.commit()
    other.post("/signin", data={"email": "ana@ngo.org"}, headers=FETCH)
    j = other.post("/signin", data={"next": "/topics", "email": "ana@ngo.org", "code": _code(outbox)}, headers=FETCH).json()
    assert j == {"redirect": "/topics"}

    # Code d'invitation saisi à la main
    j = _client().post("/join", data={"code": code.upper(), "check_code": "1"}, headers=FETCH).json()
    assert j == {"redirect": f"/join/{code}"}


def test_open_signup_password_rules_and_login(env):
    from cop_corpus import auth

    conn, outbox = env
    c = _client()
    page = c.get("/?signup=1").text
    assert 'id="su-pw"' in page and 'name="password2"' not in page and 'minlength="8"' in page
    form = {"code": "", "next": "/", "email": "lea@media.org", "organisation": "Media", "password": "court"}
    assert "at least 8" in c.post("/join", data=form, headers=FETCH).json()["error"]
    j = c.post("/join", data={**form, "password": "abcd1234"}, headers=FETCH).json()       # 8 caractères suffisent
    assert j == {"redirect": "/?welcome=1"}
    user = auth.by_email(conn, "lea@media.org")
    assert user["source"] == "inscription" and user["password_hash"]
    assert "exists" in _client().post("/join", data={**form, "password": "abcd1234"}, headers=FETCH).json()["error"]

    # Connexion : e-mail → mot de passe (la fenêtre renvoie l'e-mail avec le mot de passe).
    other = _client()
    assert other.post("/signin", data={"next": "/", "email": "lea@media.org"}, headers=FETCH).json()["step"] == "password"
    j = other.post("/signin", data={"next": "/", "email": "lea@media.org", "password": "mauvais"}, headers=FETCH).json()
    assert j["step"] == "password" and "Wrong password" in j["error"] and j["email"] == "lea@media.org"
    j = other.post("/signin", data={"next": "/", "email": "lea@media.org", "password": "abcd1234"}, headers=FETCH).json()
    assert j == {"redirect": "/"} and not outbox
    # Sans e-mail (ancienne fenêtre) : message clair, pas « mot de passe incorrect ».
    j = _client().post("/signin", data={"next": "/", "password": "abcd1234"}, headers=FETCH).json()
    assert j["step"] == "email" and "e-mail" in j["error"].lower()


def test_forgot_password_from_the_modal(env):
    from cop_corpus import auth

    conn, outbox = env
    uid, _ = auth.create_user(conn, "p@ngo.org", "", restricted=False)
    auth.set_password(conn, uid, "ancien-mdp")
    c = _client()
    # Mot de passe tapé (faux) puis « Mot de passe oublié ? » : la fenêtre envoie e-mail + mot de passe + action.
    j = c.post("/signin", data={"next": "/", "email": "p@ngo.org", "password": "je-ne-sais-plus", "action": "forgot"},
               headers=FETCH).json()
    assert j["step"] == "code" and j.get("sent") and len(outbox) == 1
    j = c.post("/signin", data={"next": "/", "email": "p@ngo.org", "code": "", "action": "resend"}, headers=FETCH).json()
    assert j["step"] == "code" and "wait a minute" in j["error"]               # pas de rafale de codes
    j = c.post("/signin", data={"next": "/", "email": "p@ngo.org", "code": _code(outbox)}, headers=FETCH).json()
    assert j["redirect"].startswith("/account?forgot=1")   # compte jamais prouvé : adresse MCP renouvelée aussi
    c.post("/account", data={"password": "nouveau1", "password2": "nouveau1"})
    assert auth.check_password(conn, "p@ngo.org", "nouveau1") and not auth.check_password(conn, "p@ngo.org", "ancien-mdp")


def test_too_many_attempts_message(env):
    from cop_corpus import auth

    conn, _ = env
    uid, _ = auth.create_user(conn, "t@ngo.org", "", restricted=False)
    auth.set_password(conn, uid, "le-bon-mdp")
    c = _client()
    for _ in range(auth.MAX_FAILURES):
        c.post("/signin", data={"email": "t@ngo.org", "password": "faux"}, headers=FETCH)
    j = c.post("/signin", data={"email": "t@ngo.org", "password": "le-bon-mdp"}, headers=FETCH).json()
    assert "Too many attempts" in j["error"]


def test_admin_created_account_login_link(env):
    from cop_corpus import auth

    conn, _ = env
    uid, _ = auth.create_user(conn, "coll@deleg.org", "Coll")
    link = auth.login_link(auth.by_id(conn, uid)).split("://", 1)[1].split("/", 1)[1]
    c = _client()
    assert 'method="post"' in c.get("/" + link).text                       # un antivirus qui ouvre le lien ne le consomme pas
    assert 'method="post"' in c.get("/" + link).text
    r = c.post("/" + link, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account"
    assert "Choose a password" in c.get("/account").text or "password" in c.get("/account").text.lower()


# --- retours de la revue adversariale --------------------------------------------------------------------------------

def test_squatted_email_is_reclaimed_by_reset(env, monkeypatch):
    """Quelqu'un crée un compte avec l'adresse d'une autre personne et relie son Telegram : il ne reçoit pas les codes ;
    quand la vraie titulaire réinitialise le mot de passe par e-mail, ses sessions, son Telegram et l'adresse MCP
    tombent."""
    from cop_corpus import auth, telegram

    conn, outbox = env
    tg: list = []
    monkeypatch.setattr(telegram, "send", lambda chat, text, **kw: tg.append((chat, text)) or True)
    attacker = _client()
    attacker.post("/join", data={"code": "", "email": "victim@ngo.org", "password": "attaquant1"}, headers=FETCH)
    squat = auth.by_email(conn, "victim@ngo.org")
    old_token = auth.mcp_token(squat)
    conn.execute("UPDATE users SET telegram_chat_id='999' WHERE id=?", (squat["id"],))
    conn.commit()
    assert "victim@ngo.org" in attacker.get("/account").text

    victim = _client()
    j = victim.post("/signin", data={"email": "victim@ngo.org", "action": "forgot"}, headers=FETCH).json()
    assert j["sent"] == "by e-mail" and not tg                       # adresse non prouvée : rien sur Telegram
    j = victim.post("/signin", data={"email": "victim@ngo.org", "code": _code(outbox)}, headers=FETCH).json()
    assert j["redirect"] == "/account?forgot=1&renewed=1"
    now = auth.by_id(conn, squat["id"])
    assert now["email_verified_at"] and not now["telegram_chat_id"] and auth.mcp_token(now) != old_token
    assert attacker.get("/account", follow_redirects=False).status_code == 303   # session de l'intrus fermée
    assert "victim@ngo.org" in victim.get("/account").text


def test_verified_accounts_get_codes_on_telegram_too(env, monkeypatch):
    from cop_corpus import auth, telegram

    conn, outbox = env
    tg: list = []
    monkeypatch.setattr(telegram, "send", lambda chat, text, **kw: tg.append(chat) or True)
    code = auth.invite_settings(conn)["code"]
    _client().post(f"/join/{code}", data={"code": code, "email": "inv@ngo.org"}, headers=FETCH)
    u = auth.by_email(conn, "inv@ngo.org")
    conn.execute("UPDATE users SET telegram_chat_id='42' WHERE id=?", (u["id"],))
    conn.commit()
    c = _client()
    c.post("/signin", data={"email": "inv@ngo.org"}, headers=FETCH)            # pas encore prouvée : e-mail seul
    assert not tg
    c.post("/signin", data={"email": "inv@ngo.org", "code": _code(outbox)}, headers=FETCH)
    conn.execute("UPDATE login_codes SET sent_at='[]' WHERE user_id=?", (u["id"],))
    conn.commit()
    j = _client().post("/signin", data={"email": "inv@ngo.org"}, headers=FETCH).json()
    assert j["sent"] == "by e-mail" and not tg                  # relié avant la preuve : toujours pas digne de confiance
    tcode = auth.by_id(conn, u["id"])["telegram_code"]
    telegram.handle(conn, {"message": {"chat": {"id": 42}, "text": f"/start {tcode}"}})   # re-relié après la preuve
    conn.execute("UPDATE login_codes SET sent_at='[]' WHERE user_id=?", (u["id"],))
    conn.commit()
    j = _client().post("/signin", data={"email": "inv@ngo.org"}, headers=FETCH).json()
    assert j["sent"] == "by e-mail + Telegram" and "42" in [str(x) for x in tg]


def test_sessions_revocable_and_old_cookies_still_valid(env):
    from cop_corpus import auth

    conn, _ = env
    uid, _ = auth.create_user(conn, "s@ngo.org", "", restricted=False)
    auth.set_password(conn, uid, "motdepasse")
    import base64
    payload = f"{uid}.{int(__import__('time').time()) + 3600}"
    legacy = base64.urlsafe_b64encode(f"{payload}.{auth._sign(payload)}".encode()).decode()
    assert auth.user_from_cookie(conn, legacy)["id"] == uid                    # cookies d'avant : toujours valables
    a, b = _client(), _client()
    for c in (a, b):
        c.post("/signin", data={"email": "s@ngo.org", "password": "motdepasse"}, headers=FETCH)
    a.post("/account", data={"action": "logout_all"})
    assert a.get("/account", follow_redirects=False).status_code == 200      # cet appareil reste connecté
    assert b.get("/account", follow_redirects=False).status_code == 303      # les autres non
    assert auth.user_from_cookie(conn, legacy) is None
    a.post("/account", data={"action": "password", "password": "nouveau12", "password2": "nouveau12"})
    assert a.get("/account", follow_redirects=False).status_code == 200


def test_shared_wifi_does_not_block_invitations(env):
    from cop_corpus import auth

    conn, _ = env
    code = auth.invite_settings(conn)["code"]
    c = _client()
    for i in range(12):                                                        # une salle qui s'inscrit librement
        assert "redirect" in c.post("/join", data={"code": "", "email": f"o{i}@x.org", "password": "abcdefgh"},
                                    headers=FETCH).json()
        c.cookies.clear()
    for i in range(12):                                                        # puis scanne le QR code
        assert "redirect" in c.post(f"/join/{code}", data={"code": code, "email": f"q{i}@x.org"}, headers=FETCH).json()
        c.cookies.clear()
    for _ in range(auth.MAX_FAILURES):                                         # mauvais codes saisis : limités
        c.post("/join", data={"code": "zzzzzzz", "check_code": "1"}, headers=FETCH)
    assert "Too many" in c.post("/join", data={"code": code, "check_code": "1"}, headers=FETCH).json()["error"]


def test_failed_send_does_not_pretend_a_code_was_sent(env, monkeypatch):
    from cop_corpus import auth, mailout

    conn, _ = env
    code = auth.invite_settings(conn)["code"]
    _client().post(f"/join/{code}", data={"code": code, "email": "f@ngo.org"}, headers=FETCH)
    monkeypatch.setattr(mailout, "send", lambda *a, **k: False)
    c = _client()
    for _ in range(3):
        j = c.post("/signin", data={"email": "f@ngo.org"}, headers=FETCH).json()
        assert j["step"] == "email" and "could not be sent" in j["error"]       # jamais « patientez une minute »


def test_empty_code_and_resend_without_submitter(env):
    from cop_corpus import auth

    conn, outbox = env
    code = auth.invite_settings(conn)["code"]
    _client().post(f"/join/{code}", data={"code": code, "email": "e@ngo.org"}, headers=FETCH)
    c = _client()
    c.post("/signin", data={"email": "e@ngo.org"}, headers=FETCH)
    j = c.post("/signin", data={"email": "e@ngo.org", "code": ""}, headers=FETCH).json()
    assert j["step"] == "code" and "Enter the 6-digit code" in j["error"]
    u = auth.by_email(conn, "e@ngo.org")
    assert conn.execute("SELECT tries FROM login_codes WHERE user_id=?", (u["id"],)).fetchone()[0] == 0


def test_invited_account_typing_a_password_gets_a_code(env):
    from cop_corpus import auth

    conn, outbox = env
    code = auth.invite_settings(conn)["code"]
    _client().post(f"/join/{code}", data={"code": code, "email": "np@ngo.org"}, headers=FETCH)
    j = _client().post("/signin", data={"email": "np@ngo.org", "password": "jen-ai-pas"}, headers=FETCH).json()
    assert j["step"] == "code" and j.get("sent") and outbox


def test_cross_site_forms_refused(env):
    from cop_corpus import auth

    conn, _ = env
    uid, _ = auth.create_user(conn, "x@ngo.org", "", restricted=False)
    auth.set_password(conn, uid, "motdepasse")
    c = _client()
    r = c.post("/signin", data={"email": "x@ngo.org", "password": "motdepasse"},
               headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403 and "cop_session" not in r.headers.get("set-cookie", "")
    assert c.post("/join", data={"email": "y@ngo.org", "password": "abcdefgh"},
                  headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    r = c.post("/signin", data={"email": "x@ngo.org", "password": "motdepasse"},
               headers={"Origin": "https://testserver", **FETCH})
    assert r.json() == {"redirect": "/"}


def test_disabled_account_message_and_single_use_link(env):
    from cop_corpus import auth

    conn, _ = env
    uid, _ = auth.create_user(conn, "d@ngo.org", "")
    link = "/" + auth.login_link(auth.by_id(conn, uid)).split("://", 1)[1].split("/", 1)[1]
    c = _client()
    assert c.post(link, follow_redirects=False).status_code == 303
    assert auth.by_id(conn, uid)["email_verified_at"]
    assert "expired or already used" in _client().get(link).text              # usage unique
    assert "expired or already used" in _client().post(link).text
    conn.execute("UPDATE users SET active=0 WHERE id=?", (uid,))
    conn.commit()
    assert "disabled" in _client().post("/signin", data={"email": "d@ngo.org"}, headers=FETCH).json()["error"]


def test_code_caps_per_day_and_per_account(env, monkeypatch):
    """Plafonds par compte ET par réseau : un tiers qui réclame des codes ou en tape de faux depuis son réseau ne
    bloque pas la personne, qui se connecte depuis le sien."""
    from cop_corpus import auth

    conn, outbox = env
    code = auth.invite_settings(conn)["code"]
    _client().post(f"/join/{code}", data={"code": code, "email": "cap@ngo.org"}, headers=FETCH)
    u = auth.by_email(conn, "cap@ngo.org")
    import json
    import time as _t
    now = _t.time()
    conn.execute("INSERT INTO login_codes(user_id, sent_at, tries) VALUES(?,?,0) ON CONFLICT(user_id) DO UPDATE SET "
                 "sent_at=excluded.sent_at",
                 (u["id"], json.dumps([[now - 7200 - i, "testclient"] for i in range(auth.CODES_PER_DAY)])))
    conn.commit()
    j = _client().post("/signin", data={"email": "cap@ngo.org"}, headers=FETCH).json()     # même réseau : plafonné
    assert "Too many codes sent today" in j["error"] and not outbox
    assert auth.issue_login_code(conn, u, "198.51.100.7")[0]                               # autre réseau : passe
    conn.execute("UPDATE login_codes SET sent_at='[]', fail_log=? WHERE user_id=?",
                 (json.dumps([[now - 60, "203.0.113.5"]] * auth.CODE_FAILS_PER_DAY), u["id"]))
    conn.commit()
    assert auth.code_locked(conn, u, "203.0.113.5") and not auth.code_locked(conn, u, "198.51.100.7")
    assert auth.issue_login_code(conn, u, "203.0.113.5") == (None, "locked")
    conn.execute("UPDATE login_codes SET fail_log=? WHERE user_id=?",
                 (json.dumps([[now - 60, f"n{i}"] for i in range(auth.CODE_FAILS_PER_DAY_ACCOUNT)]), u["id"]))
    conn.commit()
    assert auth.code_locked(conn, u, "198.51.100.7")                                       # attaque répartie : freinée


def test_ipv6_throttle_groups_by_64():
    from cop_corpus import auth

    assert auth.ip_key("2001:db8:1:2::5") == auth.ip_key("2001:db8:1:2:ffff::1") == "2001:db8:1:2::/64"
    assert auth.ip_key("203.0.113.9") == "203.0.113.9"


def test_reset_on_verified_account_keeps_connector(env):
    from cop_corpus import auth

    conn, outbox = env
    uid, token = auth.create_user(conn, "v@deleg.org", "V", verified=True)   # créé par un administrateur
    auth.set_password(conn, uid, "ancienmdp")
    laptop, phone = _client(), _client()
    phone.post("/signin", data={"email": "v@deleg.org", "password": "ancienmdp"}, headers=FETCH)
    laptop.post("/signin", data={"email": "v@deleg.org", "action": "forgot"}, headers=FETCH)
    j = laptop.post("/signin", data={"email": "v@deleg.org", "code": _code(outbox)}, headers=FETCH).json()
    assert j == {"redirect": "/account?forgot=1"}
    assert auth.mcp_token(auth.by_id(conn, uid)) == token                    # connecteur LLM intact
    assert phone.get("/account", follow_redirects=False).status_code == 303  # autre appareil déconnecté


def test_origin_rules_match_real_browsers(env):
    """Referrer-Policy: no-referrer → les navigateurs envoient « Origin: null » sur nos propres formulaires : accepté.
    Sec-Fetch-Site tranche quand il est présent ; refus en JSON pour la fenêtre."""
    from cop_corpus import auth

    conn, _ = env
    uid, _ = auth.create_user(conn, "o@ngo.org", "", restricted=False)
    auth.set_password(conn, uid, "motdepasse")
    data = {"email": "o@ngo.org", "password": "motdepasse"}
    assert _client().post("/signin", data=data, headers={"Origin": "null", **FETCH}).json() == {"redirect": "/"}
    assert _client().post("/signin", data=data, headers={"Origin": "null", "Sec-Fetch-Site": "same-origin", **FETCH}
                          ).json() == {"redirect": "/"}
    r = _client().post("/signin", data=data, headers={"Sec-Fetch-Site": "same-site", **FETCH})
    assert r.status_code == 403 and "reload" in r.json()["error"]


def test_network_limit_counts_failures_only(env, monkeypatch):
    from cop_corpus import auth, web

    conn, _ = env
    monkeypatch.setattr(web, "SIGNIN_PER_IP", 3)
    uid, _ = auth.create_user(conn, "n@ngo.org", "", restricted=False)
    auth.set_password(conn, uid, "motdepasse")
    for _ in range(6):                                                  # connexions réussies : jamais comptées
        assert _client().post("/signin", data={"email": "n@ngo.org", "password": "motdepasse"},
                              headers=FETCH).json() == {"redirect": "/"}
    for i in range(3):
        _client().post("/signin", data={"email": f"inconnu{i}@x.org"}, headers=FETCH)
    j = _client().post("/signin", data={"email": "n@ngo.org", "password": "motdepasse"}, headers=FETCH).json()
    assert "from this network" in j["error"]


def test_backfill_is_idempotent(env):
    from cop_corpus import db

    conn, _ = env
    conn.execute("INSERT INTO users(email, created_at, telegram_chat_id) VALUES('old@x.org', '2026-11-01', '5')")
    conn.execute("UPDATE users SET email_verified_at=NULL, telegram_trusted=NULL WHERE email='old@x.org'")
    conn.execute("DELETE FROM meta WHERE key='auth_backfill_v1'")   # comme si le processus était mort avant
    conn.commit()
    db._migrate(conn)
    row = conn.execute("SELECT email_verified_at, telegram_trusted FROM users WHERE email='old@x.org'").fetchone()
    assert row["email_verified_at"] == "2026-11-01" and row["telegram_trusted"] == 1
    conn.execute("INSERT INTO users(email, created_at) VALUES('new@x.org', '2026-11-19')")
    conn.commit()
    db._migrate(conn)                                                   # drapeau posé : les nouveaux restent à prouver
    assert conn.execute("SELECT email_verified_at FROM users WHERE email='new@x.org'").fetchone()[0] is None
