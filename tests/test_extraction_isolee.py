"""Un PDF qui fait planter PyMuPDF (segfault) ne doit plus relancer le plantage du worker à chaque tour : les
documents restés 'pending' sont extraits dans un sous-processus, avec repli sans tableaux, puis marqués en erreur."""

import os
import stat
import sys

from test_core import env  # noqa: F401  (fixture)

TEXT = "Draft decision -/CMA.8\n1. Urges Parties to accelerate the transition away from fossil fuels;\n"


def _pending_pdf(conn):
    import pymupdf

    from cop_corpus import ingest

    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), TEXT)
    data = pdf.tobytes()
    doc_id, new = ingest.register(conn, data, url="https://x/piege.pdf", meta={"title": "Piège", "level": "2"},
                                  filename="piege.pdf")
    assert new
    assert conn.execute("SELECT status FROM documents WHERE id=?", (doc_id,)).fetchone()[0] == "pending"
    return doc_id


def _fake_python(tmp_path, monkeypatch, crash_when: str):
    """Remplace l'interpréteur du sous-processus : meurt par SIGSEGV quand l'argument « tableaux » vaut crash_when
    ('1' : seulement avec tableaux ; '*' : toujours), sinon lance le vrai Python."""
    script = tmp_path / "python-piege"
    cond = 'true' if crash_when == "*" else f'[ "$5" = "{crash_when}" ]'
    script.write_text(f'#!/bin/sh\nif {cond}; then kill -SEGV $$; fi\nexec "{sys.executable}" "$@"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(sys, "executable", str(script))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in sys.path if p))


def test_pending_isolated_ok(env):  # noqa: F811
    from cop_corpus import ingest

    doc_id = _pending_pdf(env)
    assert ingest.reindex_pending(env) == {"indexed": 1}
    assert "fossil fuels" in " ".join(r[0] for r in env.execute("SELECT text FROM passages WHERE doc_id=?", (doc_id,)))


def test_crash_with_tables_falls_back(env, tmp_path, monkeypatch):  # noqa: F811
    from cop_corpus import ingest, monitor

    reports = []
    monkeypatch.setattr(monitor, "report", lambda place, err, **k: reports.append((place, str(err))))
    _fake_python(tmp_path, monkeypatch, "1")
    doc_id = _pending_pdf(env)
    assert ingest.reindex_pending(env) == {"indexed": 1}
    assert reports and "nouvel essai sans tableaux" in reports[0][1]
    assert doc_id


def test_crash_always_marks_error_and_unblocks_alerts(env, tmp_path, monkeypatch):  # noqa: F811
    from cop_corpus import alerts, ingest, monitor

    monkeypatch.setattr(monitor, "report", lambda *a, **k: None)
    alerts.run(env)  # initialise le curseur
    _fake_python(tmp_path, monkeypatch, "*")
    doc_id = _pending_pdf(env)
    assert ingest.reindex_pending(env) == {"error": 1}
    row = env.execute("SELECT status, error FROM documents WHERE id=?", (doc_id,)).fetchone()
    assert row["status"] == "error" and "même sans tableaux" in row["error"]
    alerts.run(env)
    from cop_corpus import db

    assert int(db.meta_get(env, "alerts_last_doc_id")) >= doc_id  # le curseur des alertes n'est plus bloqué


def test_extraction_isolee_partout(env, tmp_path, monkeypatch):  # noqa: F811
    """Tout appel à extract.extract sur un PDF (dépôt MCP, ENB, réextraction) passe par le sous-processus."""
    from cop_corpus import extract, monitor

    monkeypatch.setattr(monitor, "report", lambda *a, **k: None)
    _fake_python(tmp_path, monkeypatch, "*")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.7 garbage")
    import pytest

    with pytest.raises(RuntimeError, match="même sans tableaux"):
        extract.extract(pdf)


def test_quarantaine_apres_deux_tentatives_interrompues(env, monkeypatch):  # noqa: F811
    from cop_corpus import ingest, monitor

    reports = []
    monkeypatch.setattr(monitor, "report", lambda place, err, **k: reports.append(str(err)))
    doc_id = _pending_pdf(env)
    marker = ingest._attempts_dir() / str(doc_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("2")                                   # le processus est mort deux fois pendant ce document
    assert ingest.reindex_pending(env) == {"error": 1}
    assert env.execute("SELECT status FROM documents WHERE id=?", (doc_id,)).fetchone()[0] == "error"
    assert any(f"document {doc_id} abandonné" in r for r in reports)
    assert not marker.exists()


def test_arret_propre_efface_les_tentatives(env):  # noqa: F811
    from cop_corpus import ingest

    doc_id = _pending_pdf(env)
    marker = ingest._attempts_dir() / str(doc_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1")                                   # interrompu par un déploiement (SIGTERM)
    ingest.clear_attempts()
    assert not marker.exists()


def test_tentative_comptee_puis_effacee(env):  # noqa: F811
    from cop_corpus import ingest

    doc_id = _pending_pdf(env)
    assert ingest.reindex_pending(env) == {"indexed": 1}
    assert not (ingest._attempts_dir() / str(doc_id)).exists()
