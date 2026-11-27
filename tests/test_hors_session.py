"""Hors session (COP_SESSION=0, note docs/notes/2026-11-27-phase0-hors-session.md) : collecte espacée, Telegram
coupé pour les utilisateurs, seules les alertes bloquantes aux administrateurs, pages /doc anonymes non mesurées et
purgées par lots. En session (COP_SESSION=1, défaut des autres tests via conftest) : inchangé."""

import importlib
from datetime import datetime, timedelta, timezone

import pytest

from test_parcours_question import admin_chat, env  # noqa: F401  (fixtures)


@pytest.fixture()
def hors(monkeypatch):
    """Passe en hors session (le conftest met SESSION=1 par défaut)."""
    from cop_corpus import config

    monkeypatch.setenv("COP_SESSION", "0")
    monkeypatch.setattr(config, "SESSION", False)
    return config


def _fmt(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- critère 1 : réglage --------------------------------------------------------------------------------------------

def test_reglage_session_defaut_zero(tmp_path, monkeypatch):
    from cop_corpus import config

    monkeypatch.setenv("COP_DATA_DIR", str(tmp_path / "data"))   # jamais le vrai dossier de données après rechargement
    monkeypatch.delenv("COP_SESSION", raising=False)
    importlib.reload(config)
    assert config.SESSION is False
    monkeypatch.setenv("COP_SESSION", "1")
    importlib.reload(config)
    assert config.SESSION is True


# --- critère 2 : live_worker ------------------------------------------------------------------------------------------

def test_periodes_du_direct(monkeypatch):
    from cop_corpus import config, live_worker

    assert live_worker.periods() == {"seors": 3600, "texts": 300, "social": 600, "positions": 600,
                                     "reference": 3600, "agenda": 21600, "pavillons": 21600, "soumissions": 600,
                                     "groupes": 86400}   # en session : inchangé
    monkeypatch.setattr(config, "SESSION", False)
    off = live_worker.periods()
    assert {k: off[k] for k in ("texts", "reference", "agenda", "pavillons")} == dict.fromkeys(
        ("texts", "reference", "agenda", "pavillons"), 86400)
    assert off["social"] == off["positions"] == 6 * 3600
    assert off["groupes"] == 7 * 86400 and off["seors"] is None


def _fake_live(monkeypatch):
    """Remplace les relevés du direct par des enregistreurs ; renvoie la liste des tâches lancées et des `send`."""
    from cop_corpus import agenda, briefs, docs_unfccc, groupes, live, llm, pavillons, positions

    ran, sends = [], []
    rec = lambda name, value=None: (lambda *a, **k: ran.append(name) or value)  # noqa: E731
    monkeypatch.setattr(live, "sync_status", rec("reference"))
    monkeypatch.setattr(live, "sync_positions_cb", rec("reference-positions", 0))
    monkeypatch.setattr(positions, "tidy", lambda conn: {})
    monkeypatch.setattr(positions, "run", rec("positions", {}))
    monkeypatch.setattr(agenda, "sync_planner", rec("agenda", {}))
    monkeypatch.setattr(agenda, "sync_seors", rec("seors", {}))
    monkeypatch.setattr(pavillons, "sync", rec("pavillons", {}))
    monkeypatch.setattr(live, "sync_texts", rec("texts", []))
    monkeypatch.setattr(docs_unfccc, "sync", lambda *a: {})
    monkeypatch.setattr(live, "link_existing", lambda conn: 0)
    monkeypatch.setattr(live, "retrieve_pending", lambda *a: {})
    monkeypatch.setattr(live, "tidy_texts", lambda conn: {})
    monkeypatch.setattr(live, "notify_new_texts", lambda conn, send: sends.append(send) or 0)
    monkeypatch.setattr(live, "notify_retrieved", lambda conn, send: sends.append(send) or 0)
    monkeypatch.setattr(live, "sync_bluesky", rec("social", 0))
    monkeypatch.setattr(groupes, "sync", rec("groupes", {}))
    monkeypatch.setattr(briefs, "run_slot", rec("briefs", 0))
    monkeypatch.setattr(llm, "enabled", lambda: True)
    for mod in (live, agenda, pavillons, groupes):
        monkeypatch.setattr(mod, "log", lambda *a: None)
    return ran, sends


def test_direct_hors_session_espace_et_muet(env, hors, monkeypatch):  # noqa: F811
    from cop_corpus import config, live_worker, telegram

    monkeypatch.setattr(config, "BRIEFS", True)
    ran, sends = _fake_live(monkeypatch)
    last: dict[str, float] = {}
    t0 = 1_800_000_000.0
    live_worker.tick(env, None, last, now=t0)
    assert "seors" not in ran and "briefs" not in ran                 # jamais hors session
    assert {"reference", "agenda", "pavillons", "texts", "social", "positions", "groupes"} <= set(ran)
    assert sends and all(s is live_worker._silent for s in sends)      # textes marqués, aucun Telegram
    ran.clear()
    live_worker.tick(env, None, last, now=t0 + 5 * 3600)               # 5 h : rien n'est dû
    assert ran == []
    live_worker.tick(env, None, last, now=t0 + 6 * 3600)               # 6 h : social et positions
    assert sorted(ran) == ["positions", "social"]
    ran.clear()
    live_worker.tick(env, None, last, now=t0 + 24 * 3600)              # 24 h : textes, référence, agenda, pavillons
    assert {"texts", "reference", "agenda", "pavillons"} <= set(ran) and "groupes" not in ran
    live_worker.tick(env, None, last, now=t0 + 7 * 86400)
    assert "groupes" in ran

    # En session : rythmes et Telegram actuels.
    monkeypatch.setattr(config, "SESSION", True)
    ran.clear(), sends.clear()
    live_worker.tick(env, None, {}, now=t0)
    assert "seors" in ran and "briefs" in ran and sends and all(s is telegram.send for s in sends)


# --- critère 3 : sources du catalogue ---------------------------------------------------------------------------------

def test_plancher_de_six_heures_pour_le_catalogue(env, monkeypatch):  # noqa: F811
    from cop_corpus import catalog, config, worker

    now = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)
    rapide = catalog.Source(id="rapide", type="rss", raw={"id": "rapide", "watch_minutes": 15}, file="x")
    lente = catalog.Source(id="lente", type="rss", raw={"id": "lente", "watch_minutes": 1440}, file="x")
    for s in (rapide, lente):
        env.execute("INSERT INTO source_state(source_id,last_run,last_result) VALUES(?,?,?)",
                    (s.id, _fmt(now - timedelta(minutes=300)), str({"unchanged": 1})))
    env.commit()
    assert worker._due(env, rapide, now=now)                          # en session : 15 min
    monkeypatch.setattr(config, "SESSION", False)
    assert not worker._due(env, rapide, now=now)                      # 300 min < 360
    assert worker._due(env, rapide, now=now + timedelta(minutes=60))  # 360 min
    assert not worker._due(env, lente, now=now + timedelta(minutes=60))   # 24 h : inchangée
    assert rapide.watch_minutes == 15                                 # catalogue non modifié


# --- critère 4 : index des soumissions --------------------------------------------------------------------------------

def test_index_des_soumissions_une_fois_par_jour(env, monkeypatch):  # noqa: F811
    from cop_corpus import config, db, soumissions

    importlib.reload(soumissions)
    calls = []
    monkeypatch.setattr(soumissions, "sync_index", lambda conn, client: calls.append("index") or {})
    monkeypatch.setattr(soumissions, "download_pending",
                        lambda conn, client, limit, since=None: calls.append("dl") or {})
    now = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)
    db.meta_set(env, "soumissions_index", _fmt(now - timedelta(hours=1)))
    soumissions.harvest_tick(env, None, now=now)
    assert calls == ["index", "dl", "dl"]                             # en session : 30 min
    calls.clear()
    monkeypatch.setattr(config, "SESSION", False)
    soumissions.harvest_tick(env, None, now=now)
    assert calls == ["dl", "dl"]                                      # index relu il y a 1 h : pas avant 24 h
    calls.clear()
    soumissions.harvest_tick(env, None, now=now + timedelta(hours=24))
    assert calls[0] == "index" and "dl" in calls                      # téléchargements inchangés


# --- critère 5 : Telegram coupé pour les utilisateurs -----------------------------------------------------------------

def test_aucun_telegram_utilisateur_hors_session(env, admin_chat, hors, monkeypatch):  # noqa: F811
    from cop_corpus import alerts, db, digest, worker

    uid = env.execute("SELECT id FROM users WHERE email='u@x.fr'").fetchone()[0]
    doc = env.execute("INSERT INTO documents(sha256, raw_path, status, title, access, fetched_at) "
                      "VALUES('s','r','indexed','Draft text','public','2026-11-28')").lastrowid
    sub = env.execute("INSERT INTO subscriptions(user_id, label, query, active, notify_telegram) "
                      "VALUES(?,?,?,1,1)", (uid, "finance", "finance")).lastrowid
    env.execute("INSERT INTO notifications(user_id, subscription_id, doc_id, created_at) VALUES(?,?,?,?)",
                (uid, sub, doc, db.now()))
    env.commit()
    assert alerts.send_pending(env) == 0
    assert env.execute("SELECT telegram_sent_at FROM notifications").fetchone()[0] is None   # gardée en base
    ran = []
    monkeypatch.setattr(digest, "run", lambda *a, **k: ran.append(1) or 0)
    worker.digest(env)
    assert ran == [] and admin_chat == []
    assert not worker._bot_on(True)                                   # pas de fil de long polling
    monkeypatch.setattr(worker.config, "SESSION", True)
    assert worker._bot_on(True)
    worker.digest(env)
    assert ran == [1]
    assert alerts.send_pending(env) == 1 and admin_chat[-1][0] == "43"   # en session : inchangé


# --- critère 10 : canal administrateur --------------------------------------------------------------------------------

def test_seuls_les_blocages_partent_aux_administrateurs(env, admin_chat, hors, monkeypatch):  # noqa: F811
    from cop_corpus import config, db, monitor, watchdog

    importlib.reload(watchdog)
    monkeypatch.setattr(monitor, "_spawn", lambda fn, *a: fn(*a))
    monkeypatch.setattr(watchdog, "_cgroup_memory", lambda: None)
    monkeypatch.setattr(config, "DIGEST_HOURS", [7])                  # pas de « récapitulatif manqué » hors session

    assert monitor.report("worker : veille", RuntimeError("boom"), conn=env, background=False) is False
    row = env.execute("SELECT count, unsent FROM error_events WHERE place='worker : veille'").fetchone()
    assert tuple(row) == (1, 1) and admin_chat == []                  # compté, pas envoyé

    ago = lambda **kw: _fmt(datetime.now(timezone.utc) - timedelta(**kw))  # noqa: E731
    env.execute("INSERT INTO source_state(source_id,last_run,last_result,failing_since) VALUES(?,?,?,?)",
                ("morte", ago(hours=1), "{'error': 1}", ago(hours=30)))
    env.commit()
    assert [i["key"] for i in watchdog.check(env, "app")] == ["sources"]
    assert admin_chat == []                                           # dégradé : error_events seulement
    assert env.execute("SELECT count FROM error_events WHERE place='chien de garde : sources'").fetchone()[0] == 1
    watchdog.check(env, "app")
    assert env.execute("SELECT count FROM error_events WHERE place='chien de garde : sources'").fetchone()[0] == 1

    watchdog.expect("worker", {"veille": 90}, env)
    db.meta_set(env, "hb:veille", ago(minutes=120))
    watchdog.check(env, "worker")
    assert len(admin_chat) == 1 and "boucle « veille »" in admin_chat[-1][1]   # bloquant : part
    env.execute("DELETE FROM source_state")
    env.commit()
    watchdog.beat("veille", env, every=0)
    watchdog.check(env, "worker")
    assert len(admin_chat) == 2 and "Rétabli" in admin_chat[-1][1]   # son rétablissement aussi, pas celui du dégradé


# --- critère 6 et 7 : pages /doc anonymes -----------------------------------------------------------------------------

def test_page_document_anonyme_non_mesuree(env):  # noqa: F811
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from cop_corpus import auth, ingest, usage, web, web_live

    usage.ensure_schema(env)
    doc_id, _, _ = ingest.ingest_bytes(env, ("Public climate finance text. " * 20).encode(),
                                       url="https://x/a.txt", filename="a.txt")
    env.execute("UPDATE documents SET status='indexed' WHERE id=?", (doc_id,))
    env.commit()
    app = Starlette(routes=[*web.routes, *web_live.routes])
    anon = TestClient(app, base_url="https://testserver")
    assert anon.get(f"/doc/{doc_id}").status_code == 200
    count = lambda: env.execute("SELECT COUNT(*) FROM usage_events WHERE name='document'").fetchone()[0]  # noqa: E731
    assert count() == 0
    uid, _ = auth.create_user(env, "m@test.org", "M")
    auth.set_password(env, uid, "mot-de-passe-membre")
    member = TestClient(app, base_url="https://testserver")
    member.post("/connexion", data={"email": "m@test.org", "password": "mot-de-passe-membre"})
    assert member.get(f"/doc/{doc_id}").status_code == 200
    assert env.execute("SELECT uid FROM usage_events WHERE name='document'").fetchone()[0] is not None


def test_purge_des_pages_anonymes_par_lots(env, monkeypatch):  # noqa: F811
    from cop_corpus import usage

    usage.ensure_schema(env)
    assert usage.PURGE_BATCH == 5000
    monkeypatch.setattr(usage, "PURGE_BATCH", 50)
    now = _fmt(datetime.now(timezone.utc))
    rows = [(now, "web", "page", "document", None)] * 120 + [
        (now, "web", "page", "document", "compte"), (now, "web", "page", "recherche", None),
        (now, "mcp", "tool", "document", None)]
    env.executemany("INSERT INTO usage_events(at,channel,kind,name,uid) VALUES(?,?,?,?,?)", rows)
    env.commit()
    assert usage.purge(env)["pages_anonymes_supprimees"] == 50        # un lot par passage
    assert usage.purge(env)["pages_anonymes_supprimees"] == 50
    assert usage.purge(env)["pages_anonymes_supprimees"] == 20
    assert usage.purge(env)["pages_anonymes_supprimees"] == 0
    left = sorted((r["channel"], r["name"], r["uid"]) for r in env.execute("SELECT * FROM usage_events"))
    assert left == [("mcp", "document", None), ("web", "document", "compte"), ("web", "recherche", None)]
    assert not env.in_transaction                                     # transaction courte, refermée
    st = usage.stats(env, days=7)                                     # /admin/usage sans ces lignes
    assert st["canaux"]["web"]["appels"] == 2


# --- critère 8 : sonde externe ----------------------------------------------------------------------------------------

def test_sonde_externe():
    from cop_corpus import config

    text = (config.ROOT / ".github/workflows/sonde.yml").read_text()
    assert 'cron: "*/15 * * * *"' in text and "workflow_dispatch:" in text
    assert "https://opencop.org/healthz/deep" in text and "https://cop31.akimx.fr/healthz" in text
    assert text.count("sleep 120") == 2 and text.count("for essai in 1 2") == 2
    assert "--fail" in text and "--max-time 20" in text
    assert "contents: read" in text and "secrets." not in text


# --- arbitrages : redémarrages en boucle, ancienneté des alertes ------------------------------------------------------

def test_redemarrages_en_boucle_urgents_hors_session(env, admin_chat, hors, monkeypatch):  # noqa: F811
    from cop_corpus import monitor, watchdog

    importlib.reload(watchdog)
    monkeypatch.setattr(monitor, "_spawn", lambda fn, *a: fn(*a))
    assert not watchdog.process_started(env, "worker")
    assert watchdog.process_started(env, "worker")                    # arrêt anormal isolé : enregistré seulement
    assert admin_chat == []
    assert env.execute("SELECT count FROM error_events WHERE place='worker : arrêt anormal'").fetchone()[0] == 1
    watchdog.process_started(env, "worker")
    watchdog.process_started(env, "worker")                           # 3e en 2 h : en boucle → part
    assert len(admin_chat) == 1 and "redémarrages en boucle" in admin_chat[0][1]


def test_alertes_de_plus_de_48_h_jamais_envoyees(env, admin_chat, monkeypatch):  # noqa: F811
    from cop_corpus import alerts

    now = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)
    uid = env.execute("SELECT id FROM users WHERE email='u@x.fr'").fetchone()[0]
    sub = env.execute("INSERT INTO subscriptions(user_id, label, query, active, notify_telegram) "
                      "VALUES(?,?,?,1,1)", (uid, "finance", "finance")).lastrowid
    for i, age in enumerate((49, 47)):
        doc = env.execute("INSERT INTO documents(sha256, raw_path, status, title, access, fetched_at) "
                          "VALUES(?,'r','indexed','Draft','public','2026-11-28')", (f"h{i}",)).lastrowid
        env.execute("INSERT INTO notifications(user_id, subscription_id, doc_id, created_at) VALUES(?,?,?,?)",
                    (uid, sub, doc, _fmt(now - timedelta(hours=age))))
    env.commit()
    assert alerts.send_pending(env, now=now) == 1 and len(admin_chat) == 1
    sent = [r[0] for r in env.execute("SELECT telegram_sent_at FROM notifications ORDER BY id")]
    assert sent[0] is None and sent[1] is not None                    # la vieille reste en base, non marquée
    assert alerts.send_pending(env, now=now) == 0                     # et ne part jamais
    # Salve (> max_per_user) : le message « … et N autres » ne marque pas non plus les anciennes.
    env.execute("UPDATE notifications SET telegram_sent_at=NULL")
    env.commit()
    alerts.send_pending(env, max_per_user=0, now=now)
    sent = [r[0] for r in env.execute("SELECT telegram_sent_at FROM notifications ORDER BY id")]
    assert sent[0] is None and sent[1] is not None
