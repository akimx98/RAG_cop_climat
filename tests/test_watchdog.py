"""Chien de garde : une panne bloquante (processus planté, boucle arrêtée, alertes bloquées, récapitulatif non parti)
est signalée aux administrateurs, puis son rétablissement ; une dégradation (sources en échec) une fois par jour."""

import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest

from test_parcours_question import admin_chat, env  # noqa: F401  (fixtures)


@pytest.fixture()
def wd(env, admin_chat, monkeypatch):  # noqa: F811
    import importlib

    from cop_corpus import config, monitor, watchdog

    importlib.reload(watchdog)
    monkeypatch.setattr(monitor, "_spawn", lambda fn, *a: fn(*a))
    monkeypatch.setattr(config, "DIGEST_HOURS", [])
    monkeypatch.setattr(watchdog, "_cgroup_memory", lambda: None)
    return watchdog


def _ago(wd, **kw):
    return wd._fmt(wd._now() - timedelta(**kw))


def _status(wd, env):
    wd._status_cache = None
    return wd.status(env)


def test_boucle_arretee_puis_retablie(env, admin_chat, wd):  # noqa: F811
    from cop_corpus import db

    wd.expect("worker", {"veille": 90}, env)
    assert wd.check(env, "worker") == []
    db.meta_set(env, "hb:veille", _ago(wd, minutes=120))
    found = wd.check(env, "worker")
    assert [i["key"] for i in found] == ["hb:veille"]
    assert "boucle « veille »" in admin_chat[-1][1] and "120 min" in admin_chat[-1][1]
    assert _status(wd, env)["ok"] is False
    wd.beat("veille", env, every=0)
    assert wd.check(env, "worker") == []
    assert "Rétabli" in admin_chat[-1][1]
    assert _status(wd, env) == {"ok": True, "bloquant": [], "degrade": []}


def test_arret_anormal_seulement(env, admin_chat, wd):  # noqa: F811
    for _ in range(4):                                            # déploiements : arrêts demandés
        assert not wd.process_started(env, "worker")
        wd.process_stopping("worker", env)
    assert admin_chat == []                                       # ni arrêt anormal ni « en boucle »
    env.execute("INSERT INTO documents(sha256, raw_path, status, fetched_at) VALUES('x','r','pending','2026-01-01')")
    env.commit()
    assert not wd.process_started(env, "worker")                  # le précédent était propre
    assert wd.process_started(env, "worker")                      # pas d'arrêt propre : plantage
    assert "arrêt anormal" in admin_chat[-1][1] and "suspects, repris à part) : 1" in admin_chat[-1][1]
    wd.process_started(env, "worker")
    wd.process_started(env, "worker")
    assert any("redémarrages en boucle" in t and "3 arrêts anormaux" in t for _, t in admin_chat)
    assert not any(": app :" in t for _, t in admin_chat)          # le site a son propre suivi
    assert not wd.process_started(env, "app")


def test_exception_non_rattrapee_n_est_pas_un_arret_propre():
    """Seul SIGTERM marque l'arrêt propre (pas atexit, qui tourne aussi après une exception)."""
    from cop_corpus import server, worker

    assert "atexit" not in inspect.getsource(worker)
    assert "process_stopping" in inspect.getsource(worker._install_stop_handler)
    assert "process_stopping" in inspect.getsource(server.run)


def test_document_bloque_mesure_par_observation(env, admin_chat, wd):  # noqa: F811
    from cop_corpus import db

    db.meta_set(env, "alerts_last_doc_id", "0")
    old = _ago(wd, hours=3)
    env.execute("INSERT INTO documents(sha256, raw_path, status, fetched_at) VALUES('a','r','pending',?)", (old,))
    env.execute("INSERT INTO documents(sha256, raw_path, status, fetched_at) VALUES('b','r','indexed',?)", (old,))
    env.execute("INSERT INTO documents(sha256, raw_path, status, fetched_at) VALUES('c','r','indexed',?)",
                (wd._fmt(wd._now()),))                            # récent : pas encore en retard
    env.commit()
    keys = sorted(i["key"] for i in wd.check(env, "app"))
    assert keys == ["alerts_lag"]                                 # 'pending' vu pour la première fois : pas encore
    assert any("alertes en retard : 1 document" in t for _, t in admin_chat)
    db.meta_set(env, "watchdog_pending", json.dumps({"1": _ago(wd, minutes=31)}))
    assert sorted(i["key"] for i in wd.check(env, "app")) == ["alerts_lag", "pending"]
    env.execute("UPDATE documents SET status='indexed' WHERE id=1")
    env.commit()
    db.meta_set(env, "alerts_last_doc_id", "3")
    assert wd.check(env, "app") == []
    assert json.loads(db.meta_get(env, "watchdog_pending")) == {}


def test_recapitulatif_non_parti(env, admin_chat, wd, monkeypatch):  # noqa: F811
    from cop_corpus import config, db

    monkeypatch.setattr(config, "DIGEST_HOURS", [7])
    now = datetime(2026, 11, 22, 6, 30, tzinfo=timezone.utc)      # 9 h 30 à Antalya
    db.meta_set(env, "digest_slot", "2026-11-21T19")
    assert [i["key"] for i in wd.issues(env, now=now)] == ["digest"]
    assert wd.issues(env, now=now - timedelta(hours=2)) == []     # 7 h 30 : délai de grâce
    db.meta_set(env, "digest_slot", "2026-11-22T07")
    assert wd.issues(env, now=now) == []


def test_sources_en_echec_une_fois_par_jour(env, admin_chat, wd):  # noqa: F811
    recent = _ago(wd, hours=1)
    rows = [("morte", recent, "{'error': 1}", _ago(wd, hours=30)),
            ("recente", recent, "{'error': 1}", _ago(wd, hours=3)),           # en échec depuis peu
            ("ok", recent, "{'unchanged': 1}", None),
            ("antibot", recent, "{'antibot': 1}", _ago(wd, hours=30)),        # connue, jamais contournée
            ("abandonnee", _ago(wd, days=5), "{'error': 1}", _ago(wd, days=6))]  # plus relevée
    env.executemany("INSERT INTO source_state(source_id,last_run,last_result,failing_since) VALUES(?,?,?,?)", rows)
    env.commit()
    found = wd.check(env, "app")
    assert [i["key"] for i in found] == ["sources"]
    assert "1 source(s)" in found[0]["message"] and "morte" in found[0]["message"]
    assert "1 bloquée(s) par un anti-bot" in found[0]["message"]
    assert "Dégradé" in admin_chat[-1][1]
    n = len(admin_chat)
    wd.check(env, "app")
    assert len(admin_chat) == n                                   # pas de rappel avant 24 h


def test_debut_de_serie_d_echecs_note_par_la_collecte(env, monkeypatch):  # noqa: F811
    from cop_corpus import catalog, fetch, ingest

    src = catalog.Source(id="s1", type="inconnu", raw={}, file="t.yaml")
    ingest.run_source(src, fetch.Client())
    first = env.execute("SELECT failing_since FROM source_state WHERE source_id='s1'").fetchone()[0]
    assert first
    ingest.run_source(src, fetch.Client())
    assert env.execute("SELECT failing_since FROM source_state WHERE source_id='s1'").fetchone()[0] == first
    monkeypatch.setattr(ingest, "FAIL_KEYS", ())                 # même passage compté comme un succès
    ingest.run_source(src, fetch.Client())
    assert env.execute("SELECT failing_since FROM source_state WHERE source_id='s1'").fetchone()[0] is None


def test_repli_e_mail_si_telegram_muet(env, admin_chat, wd, monkeypatch):  # noqa: F811
    from cop_corpus import mailout, monitor, telegram

    mails = []
    monkeypatch.setattr(telegram, "send", lambda chat, text, **k: False)  # jeton révoqué, Telegram injoignable
    monkeypatch.setattr(mailout, "enabled", lambda: True)
    monkeypatch.setattr(mailout, "send", lambda to, subject, text, **k: mails.append((to, subject, text)) or True)
    assert monitor.report("worker : veille", ValueError("boom"), conn=env, background=False)
    assert [m[0] for m in mails] == ["a@x.fr"] and "boom" in mails[0][2] and "<b>" not in mails[0][2]


def test_sonde_profonde(env, wd):  # noqa: F811
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from cop_corpus import db, web

    c = TestClient(Starlette(routes=web.routes))
    assert c.get("/healthz/deep").json()["ok"] is True
    wd.expect("worker", {"veille": 90}, env)
    db.meta_set(env, "hb:veille", _ago(wd, hours=3))
    assert c.get("/healthz/deep").status_code == 200              # en cache 60 s (route publique)
    wd._status_cache = None
    r = c.get("/healthz/deep")
    assert r.status_code == 503 and r.json()["bloquant"] == ["hb:veille"]
    assert json.dumps(r.json()).count("veille") == 1               # des noms, aucun détail


def test_lettre_interrompue_reprise_une_fois_puis_signalee(env, admin_chat, wd):  # noqa: F811
    import importlib

    from cop_corpus import config, mailin

    importlib.reload(mailin)
    archive = config.MAIL_DIR / mailin.ARCHIVE / "2026-11"
    archive.mkdir(parents=True)
    eml = archive / "123.eml"
    eml.write_bytes(b"Subject: x\n\nbody")
    inflight = config.MAIL_DIR / mailin.INFLIGHT
    inflight.mkdir(parents=True)
    rel = str(eml.relative_to(config.DATA_DIR))
    (inflight / "123.eml.json").write_text(json.dumps({"rel": rel, "tries": 0}))
    assert mailin.requeue_interrupted(env) == 1                   # arrêt pendant le traitement : remise en file
    again = config.MAIL_DIR / mailin.SPOOL / "123-reprise.eml"
    assert again.exists() and not list(inflight.glob("*"))
    target = archive / again.name
    again.rename(target)                                          # nouvel arrêt pendant le 2e traitement
    (inflight / "x.json").write_text(json.dumps({"rel": str(target.relative_to(config.DATA_DIR)), "tries": 1}))
    assert mailin.requeue_interrupted(env) == 0
    assert env.execute("SELECT status FROM mail_inbox").fetchone()[0] == "erreur"
    assert any("abandonnée après deux traitements interrompus" in t for _, t in admin_chat)


def test_battement_depuis_la_boucle_du_site(env, wd, monkeypatch):  # noqa: F811
    """Le battement « app-boucle » part de la boucle d'événements (tâche lancée par le lifespan si ACTIVE)."""
    import asyncio
    import inspect as _inspect

    from cop_corpus import db, server

    async def run():
        task = asyncio.create_task(wd.loop_heartbeat("t", every=3600))
        await asyncio.sleep(0.3)
        task.cancel()

    asyncio.run(run())
    assert db.meta_get(env, "hb:t-boucle")
    assert "loop_heartbeat(watchdog.ACTIVE)" in _inspect.getsource(server.build_app)


def test_indexation_ne_reprend_pas_un_document_en_cours(env):  # noqa: F811
    from cop_corpus import ingest

    env.execute("INSERT INTO documents(id, sha256, raw_path, status, fetched_at) VALUES(77,'z','r','pending','2026-11-22T00:00:00Z')")
    env.commit()
    ingest._indexing.add(77)
    try:
        assert ingest.reindex_pending(env) == {}
    finally:
        ingest._indexing.discard(77)


def test_le_site_n_alerte_que_si_le_worker_ne_verifie_plus(env, admin_chat, wd):  # noqa: F811
    from cop_corpus import db

    wd.expect("worker", {"veille": 90}, env)
    db.meta_set(env, "hb:veille", _ago(wd, minutes=120))
    db.meta_set(env, "watchdog_check:worker", _ago(wd, minutes=2))
    assert [i["key"] for i in wd.check(env, "app")] == ["hb:veille"]   # constaté…
    assert admin_chat == []                                          # …mais l'alerte revient au worker
    db.meta_set(env, "watchdog_check:worker", _ago(wd, minutes=30))  # le worker ne vérifie plus
    wd.check(env, "app")
    assert admin_chat and "veille" in admin_chat[-1][1]


def test_exception_d_indexation_ne_compte_pas_comme_tentative(env, monkeypatch):  # noqa: F811
    from cop_corpus import ingest

    env.execute("INSERT INTO documents(id, sha256, raw_path, status, fetched_at) VALUES(78,'y','r','pending','2026-11-22T00:00:00Z')")
    env.commit()

    def boom(conn, doc_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ingest, "index_document", boom)
    for _ in range(3):
        try:
            ingest.reindex_pending(env)
        except RuntimeError:
            pass
    assert not (ingest._attempts_dir() / "78").exists()
    assert env.execute("SELECT status FROM documents WHERE id=78").fetchone()[0] == "pending"
