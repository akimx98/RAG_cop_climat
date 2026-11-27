"""Connexion coincée après un « database is locked » (incident du 26/11/2026, note
docs/notes/2026-11-26-connexion-sqlite-coincee.md) : une écriture en échec sur verrou laissait la transaction
ouverte, et toute écriture suivante sur la connexion échouait sans fin. `db.connect` annule alors la transaction."""

import sqlite3

import pytest

from cop_corpus import db


def _conns(tmp_path):
    path = tmp_path / "verrou.sqlite3"
    a, b = db.connect(path), db.connect(path)
    for c in (a, b):
        c.execute("PRAGMA busy_timeout=100")  # le test n'attend pas 30 s
    return a, b


@pytest.mark.parametrize("ecrire", [
    lambda c, v: c.execute("INSERT INTO meta(key,value) VALUES('k',?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (v,)),
    lambda c, v: c.executemany("INSERT INTO meta(key,value) VALUES('k',?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", [(v,)]),
], ids=["execute", "executemany"])
def test_connexion_repart_apres_verrou(tmp_path, ecrire):
    fil, autre = _conns(tmp_path)
    fil.execute("SELECT 1 FROM meta").fetchall()
    autre.execute("INSERT INTO meta(key,value) VALUES('x','1')")   # l'autre tient le verrou d'écriture (gel)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        ecrire(fil, "1")
    autre.commit()                                                  # fin du gel
    fil.execute("SELECT count(*) FROM meta").fetchone()             # tour suivant : une lecture…
    autre.execute("INSERT INTO meta(key,value) VALUES('y','1')")
    autre.commit()                                                  # … puis un autre fil écrit
    ecrire(fil, "2")                                                # coincée sans le correctif (BUSY_SNAPSHOT)
    fil.commit()
    assert db.meta_get(autre, "k") == "2"


@pytest.mark.parametrize("ecrire", [
    lambda c: c.execute("INSERT INTO meta(key,value) VALUES('k','1')"),
    lambda c: c.executemany("INSERT INTO meta(key,value) VALUES(?,'1')", [("k",)]),
    lambda c: c.executescript("BEGIN; INSERT INTO meta(key,value) VALUES('k','1');"),
], ids=["execute", "executemany", "executescript"])
def test_verrou_annule_et_remonte(tmp_path, ecrire):
    fil, autre = _conns(tmp_path)
    autre.execute("INSERT INTO meta(key,value) VALUES('x','1')")    # l'autre tient le verrou d'écriture
    with pytest.raises(sqlite3.OperationalError) as err:
        ecrire(fil)
    assert err.value.sqlite_errorname.startswith("SQLITE_BUSY")      # l'erreur d'origine, inchangée
    assert not fil.in_transaction                                    # transaction annulée, pas laissée ouverte
    autre.rollback()


def test_commit_en_echec_sur_verrou(tmp_path):
    """Un COMMIT refusé sur verrou (rare en WAL, simulé) annule aussi la transaction."""

    class _Refus(sqlite3.Connection):
        def commit(self):
            exc = sqlite3.OperationalError("database is locked")
            exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise exc

    class _Essai(db._Connexion, _Refus):
        pass

    conn = sqlite3.connect(tmp_path / "c.sqlite3", factory=_Essai)
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES(1)")
    assert conn.in_transaction
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        conn.commit()
    assert not conn.in_transaction


def test_autre_erreur_sans_annulation(tmp_path):
    """Une erreur qui n'est pas un verrou (syntaxe FTS rattrapée, colonne en double) garde la transaction."""
    fil, autre = _conns(tmp_path)
    db.meta_set(fil, "avant", "1")
    fil.execute("INSERT INTO meta(key,value) VALUES('garde','1')")
    with pytest.raises(sqlite3.OperationalError):
        fil.execute("SELECT rowid FROM passages_fts WHERE passages_fts MATCH ?", ('"',)).fetchall()
    with pytest.raises(sqlite3.OperationalError):
        fil.execute("ALTER TABLE meta ADD COLUMN value TEXT")
    assert fil.in_transaction
    fil.commit()
    assert db.meta_get(autre, "garde") == "1"
