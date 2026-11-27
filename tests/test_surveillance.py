"""Surveillance robuste (note docs/notes/2026-11-27-surveillance-robuste.md) : alerte envoyée même base verrouillée,
WAL limité et contrôlé, état enregistré avant « Rétabli », lien de connexion non brûlé."""

import os
import sqlite3
import time
from datetime import timedelta

import pytest

from test_parcours_question import admin_chat, env  # noqa: F401  (fixtures)


class _Verrouillee:
    """Connexion dont toute requête échoue comme une base verrouillée depuis plus de 30 s."""

    def execute(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")

    def commit(self):
        raise sqlite3.OperationalError("database is locked")


@pytest.fixture()
def mon(env, admin_chat, monkeypatch):  # noqa: F811
    from cop_corpus import monitor

    monkeypatch.setattr(monitor, "_spawn", lambda fn, *a: fn(*a))
    return monitor


def test_alerte_envoyee_meme_base_verrouillee(env, admin_chat, mon):  # noqa: F811
    mon.admin_chats(env), mon.admin_emails(env)                   # dernier relevé réussi
    assert mon.report("worker : indexation", RuntimeError("panne 12"), conn=_Verrouillee()) is True
    assert len(admin_chat) == 1 and admin_chat[0][0] == "42"
    text = admin_chat[0][1]
    assert "worker : indexation" in text and "RuntimeError: panne 12" in text and "non comptée" in text
    # même erreur (à des chiffres près) : pas de nouvel envoi avant RESEND_MINUTES
    assert mon.report("worker : indexation", RuntimeError("panne 13"), conn=_Verrouillee()) is False
    assert len(admin_chat) == 1
    # le repli ne lève jamais, même sans destinataires connus
    mon._last_admins.update(chats=[], emails=[])
    assert mon.report("autre lieu", "Erreur: x", conn=_Verrouillee(), background=False) is False


def test_journal_size_limit(env):  # noqa: F811
    from cop_corpus import db

    assert env.execute("PRAGMA journal_size_limit").fetchone()[0] == db.WAL_LIMIT == 64 * 2**20


def test_wal_bloque_signale(env, monkeypatch):  # noqa: F811
    """Un lecteur tient une ancienne version : les pages écrites ensuite restent en attente → alerte. Un gros
    fichier WAL déjà reporté (cas d'après l'incident) ne déclenche rien."""
    from cop_corpus import db, watchdog

    monkeypatch.setattr(watchdog, "WAL_STUCK_BYTES", 256 * 1024)
    keys = lambda: {i["key"]: i for i in watchdog.issues(env, "worker")}  # noqa: E731
    env.execute("CREATE TABLE IF NOT EXISTS lest(x BLOB)")
    env.commit()
    writer = db.connect()
    writer.executemany("INSERT INTO lest VALUES(?)", [(b"x" * 4000,) for _ in range(200)])   # ~800 Ko
    writer.commit()
    assert "wal" not in keys()                         # reporté par le checkpoint passif : gros WAL mais rien en attente
    reader = db.connect()
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM lest").fetchone()   # lecteur coincé sur cette version
    writer.executemany("INSERT INTO lest VALUES(?)", [(b"y" * 4000,) for _ in range(200)])
    writer.commit()
    assert keys()["wal"]["level"] == "bloquant"
    reader.rollback()                                  # le lecteur lâche sa version
    assert "wal" not in keys()


def test_repli_sans_destinataires_ne_masque_pas_l_erreur(env, admin_chat, mon):  # noqa: F811
    mon._last_admins.update(chats=[], emails=[])
    assert mon.report("lieu", RuntimeError("x"), conn=_Verrouillee(), background=False) is False
    mon.admin_chats(env)                               # destinataires connus (vérification suivante)
    assert mon.report("lieu", RuntimeError("x"), conn=_Verrouillee(), background=False) is True
    assert len(admin_chat) == 1


def test_retabli_seulement_apres_etat_enregistre(env, admin_chat, monkeypatch):  # noqa: F811
    import json

    from cop_corpus import db, monitor, watchdog

    monkeypatch.setattr(monitor, "_spawn", lambda fn, *a: fn(*a))
    monkeypatch.setattr(watchdog, "_cgroup_memory", lambda: None)
    state = {"hb:veille": {"since": "2026-11-26T17:45:03Z", "sent": "2026-11-26T18:00:00Z", "level": "bloquant",
                           "message": "boucle « veille » arrêtée"}}
    db.meta_set(env, "watchdog_state", json.dumps(state))
    real = db.meta_set

    def failing(conn, key, value):
        if key == "watchdog_state":
            raise sqlite3.OperationalError("database is locked")
        return real(conn, key, value)

    monkeypatch.setattr(watchdog.db, "meta_set", failing)
    watchdog.check(env, "worker")
    assert not any("Rétabli" in t for _, t in admin_chat)        # état non enregistré : rien d'annoncé
    monkeypatch.setattr(watchdog.db, "meta_set", real)
    watchdog.check(env, "worker")
    assert sum("Rétabli" in t for _, t in admin_chat) == 1


def test_lien_de_connexion_non_brule_si_verification_echoue(env, monkeypatch):  # noqa: F811
    from cop_corpus import auth, db

    uid, _ = auth.create_user(env, "lien2@example.org", "Lien")
    value = auth.login_link(auth.by_id(env, uid)).split("/entrer/")[1]
    other = db.connect()
    other.execute("PRAGMA busy_timeout=100")
    env.execute("PRAGMA busy_timeout=100")
    # l'UPDATE de la preuve d'adresse échoue : un déclencheur simule le verrou
    env.execute("CREATE TEMP TRIGGER t_fail BEFORE UPDATE OF email_verified_at ON users "
                "BEGIN SELECT RAISE(ABORT, 'database is locked'); END")
    with pytest.raises(sqlite3.Error):
        auth.by_login_link(env, value)
    env.execute("DROP TRIGGER t_fail")
    assert not env.in_transaction
    assert auth.by_login_link(env, value)                         # le lien sert encore
    assert auth.by_login_link(env, value) is None                 # une seule fois
