"""Phase 2 : le site ne calcule plus en fond (note docs/notes/2026-11-27-phase2-site-sans-fond.md).

Frises des pages dossier calculées par le fil `frises` du worker (table `dossier_frises`), lues par le site ;
« nouveautés » calculées à la demande ; vecteurs chargés au démarrage du site. Critères 1 à 9 (les critères 1 et 2,
statiques, sont aussi dans tests/test_un_ecrivain.py)."""

import importlib
import json
import threading
from pathlib import Path

import pytest

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "gst_dialogue_v2_v3.json").read_text())
SLUG = "bilan-ndc"
V2, V3, V4 = 2623, 10821, 20000
BLOC = 'id="versions"'   # bloc « Au fil des versions » (dossier.html)


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
    for mod in ("embed", "auth", "search", "ingest", "diff", "formulations", "web", "frises", "web_live"):
        importlib.reload(importlib.import_module(f"cop_corpus.{mod}"))
    from cop_corpus import live, usage

    live._ready.clear()
    usage._ready.clear()
    conn = db.get()
    for item in FIXTURE:
        _insert(conn, item["doc"], item["passages"])
    # Troisième version, fabriquée : la v3 où le charbon apparaît entre crochets.
    v3 = FIXTURE[1]
    doc = dict(v3["doc"], id=V4, symbol="DT.CMA8.i4.4", version=4, published="2026-11-20",
               title=v3["doc"]["title"].replace("i4.3", "i4.4"))
    passages = [dict(p, id=p["id"] + 10**6, text=p["text"] + (" [Phase down unabated coal power.]" if i == 2 else ""))
                for i, p in enumerate(v3["passages"])]
    _insert(conn, doc, passages)
    live.ensure_schema(conn)
    conn.execute("INSERT INTO live_texts(key, symbol, title, dossier, published, doc_id, first_seen) "
                 "VALUES('DT.CMA8.i4.4', 'DT.CMA8.i4.4', 'GST dialogue', ?, '2026-11-20', ?, '2026-11-20')", (SLUG, V4))
    conn.commit()
    return conn


def _insert(conn, d, passages):
    conn.execute("INSERT INTO documents(id, sha256, url, title, level, doc_type, symbol, published, fetched_at, "
                 "mime, raw_path, access, lineage_key, version, status, n_pages) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,"
                 "?,?,?)", (d["id"], f"sha{d['id']}", d["url"], d["title"], "2", "projet_texte", d["symbol"],
                            d["published"], d["published"], "application/pdf", f"raw/{d['id']}.pdf", "public",
                            d["lineage_key"], d["version"], "indexed", d["n_pages"]))
    for p in passages:
        conn.execute("INSERT INTO passages(id, doc_id, seq, page, para, text) VALUES(?,?,?,?,?,?)",
                     (p["id"], d["id"], p["seq"], p["page"], p["para"], p["text"]))
    conn.commit()


def _dossier():
    from cop_corpus import live

    return [live.dossier(SLUG)]


def _old_frises(conn, texts, user):
    """Copie conforme de l'ancien `web_live._frises` (avant la phase 2) : la référence du critère 3."""
    from cop_corpus import auth, formulations

    out, seen = [], set()
    for t in texts:
        if not t.get("doc_id"):
            continue
        row = conn.execute("SELECT * FROM documents WHERE id=?", (t["doc_id"],)).fetchone()
        if not row or not row["lineage_key"] or row["lineage_key"] in seen:
            continue
        seen.add(row["lineage_key"])
        if row["access"] != "public" and not auth.can_restricted(user):
            continue
        rows = formulations.lineage(conn, row, user)
        if len(rows) >= 2:
            out.append({"titre": rows[-1]["title"], "cote": rows[-1]["symbol"], "grille": formulations.grid(conn, rows)})
    return out[:4]


def _client():
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from cop_corpus import web, web_live

    return TestClient(Starlette(routes=[*web.routes, *web_live.routes]), raise_server_exceptions=False)


def _stored(conn, variant="public"):
    row = conn.execute("SELECT value FROM dossier_frises WHERE slug=? AND variant=?", (SLUG, variant)).fetchone()
    return json.loads(row[0]) if row else None


# --- critère 1 : plus de préchauffage dans le site -------------------------------------------------------------------

def test_plus_de_prechauffage_dans_le_site():
    from cop_corpus import server, web_live

    for name in ("start_warming", "warm_forever", "_heavy_cache", "_heavy", "_frises", "WARM_EVERY"):
        assert not hasattr(web_live, name), name
    assert "web_live" not in Path(server.__file__).read_text().split("def run(")[1]


# --- critère 3 : frises stockées identiques à l'ancien calcul -------------------------------------------------------

def test_frises_identiques_a_l_ancien_calcul(env):
    from cop_corpus import formulations, frises, live

    out = frises.compute_all(env, _dossier())
    assert out == {"recalcules": 1, "inchanges": 0, "echecs": 0, "differes": 0}
    stored = _stored(env)
    formulations._CACHE.clear()
    formulations._DIFFS.clear()
    ref = _old_frises(env, live.texts(env, SLUG), None)
    assert stored == json.loads(json.dumps(ref, ensure_ascii=False))
    assert len(stored) == 1 and [v["doc_id"] for v in stored[0]["grille"]["versions"]] == [V2, V3, V4]
    assert any(n["cle"] == "charbon" for n in stored[0]["grille"]["notions"])   # la v4 change bien quelque chose
    assert frises.read(env, SLUG, None) == stored                                # lecture : rien de retiré
    # Pas de variante `restreint` tant qu'aucun document n'est restreint ; un lecteur habilité lit la variante publique.
    assert _stored(env, "restreint") is None
    assert frises.read(env, SLUG, {"id": 1, "can_restricted": 1}) == stored
    row = env.execute("SELECT signature, computed_at, duration_ms FROM dossier_frises").fetchone()
    assert row["signature"] and row["computed_at"] and row["duration_ms"] is not None


def test_variante_restreinte_identique_a_l_ancien_calcul(env):
    from cop_corpus import frises, live

    env.execute("UPDATE documents SET access='restreint' WHERE id=?", (V3,))
    env.commit()
    frises.compute_all(env, _dossier())
    reader = {"id": 1, "can_restricted": 1, "active": 1}
    texts = live.texts(env, SLUG)
    assert _stored(env) == json.loads(json.dumps(_old_frises(env, texts, None)))
    assert _stored(env, "restreint") == json.loads(json.dumps(_old_frises(env, texts, reader)))
    assert [v["doc_id"] for v in _stored(env)[0]["grille"]["versions"]] == [V2, V4]
    assert [v["doc_id"] for v in _stored(env, "restreint")[0]["grille"]["versions"]] == [V2, V3, V4]
    assert frises.read(env, SLUG, reader) == _stored(env, "restreint")
    assert frises.read(env, SLUG, None) == _stored(env)


# --- critère 4 : recalcul quand les textes changent, pas sinon ------------------------------------------------------

@pytest.mark.parametrize("change", [
    "UPDATE documents SET version=5 WHERE id=20000",
    "UPDATE documents SET title='Nouveau titre' WHERE id=10821",
    "UPDATE documents SET access='restreint' WHERE id=2623",
    "UPDATE documents SET duplicate_of=2623 WHERE id=10821",
    "UPDATE documents SET status='error' WHERE id=10821",
    "UPDATE live_texts SET doc_id=10821",
    "INSERT INTO live_texts(key, dossier, doc_id, first_seen) VALUES('autre', 'bilan-ndc', NULL, '2026-11-21')",
])
def test_recalcul_seulement_quand_les_textes_changent(env, monkeypatch, change):
    from cop_corpus import frises

    frises.compute_all(env, _dossier())
    calls = []
    real = frises.compute
    monkeypatch.setattr(frises, "compute", lambda *a: calls.append(1) or real(*a))
    assert frises.compute_all(env, _dossier())["inchanges"] == 1 and calls == []
    env.execute(change)
    env.commit()
    assert frises.compute_all(env, _dossier())["recalcules"] == 1 and calls
    calls.clear()
    assert frises.compute_all(env, _dossier())["inchanges"] == 1 and calls == []


def test_recalcul_quand_le_format_change(env, monkeypatch):
    from cop_corpus import frises

    frises.compute_all(env, _dossier())
    monkeypatch.setattr(frises, "FORMAT", frises.FORMAT + 1)
    assert frises.compute_all(env, _dossier())["recalcules"] == 1


def test_document_d_un_autre_dossier_sans_effet(env):
    from cop_corpus import frises, ingest

    frises.compute_all(env, _dossier())
    ingest.ingest_bytes(env, b"Unrelated text on finance. " * 20, url="https://x/a.txt", filename="a.txt")
    assert frises.compute_all(env, _dossier())["inchanges"] == 1


# --- critère 5 : « nouveautés » (#18) et échecs jamais gardés -------------------------------------------------------

def _recent(conn, title, text):
    from cop_corpus import ingest

    doc_id, _, _ = ingest.ingest_bytes(conn, (text * 20).encode(), url=f"https://x/{title}.txt", filename=f"{title}.txt",
                                       meta={"title": title})
    conn.execute("UPDATE documents SET status='indexed' WHERE id=?", (doc_id,))
    conn.commit()
    return doc_id


def test_nouveautes_titre_modifie_et_doublon(env):
    c = _client()
    text = "Global stocktake dialogue on nationally determined contributions, NDC synthesis. "
    a = _recent(env, "Premier titre", text)
    page = c.get(f"/database/negotiations/{SLUG}").text
    assert f"/doc/{a}" in page and "Premier titre" in page
    env.execute("UPDATE documents SET title='Titre corrigé' WHERE id=?", (a,))
    env.commit()
    page = c.get(f"/database/negotiations/{SLUG}").text
    assert "Titre corrigé" in page and "Premier titre" not in page
    env.execute("UPDATE documents SET duplicate_of=? WHERE id=?", (V2, a))
    env.commit()
    assert f"/doc/{a}" not in c.get(f"/database/negotiations/{SLUG}").text


def test_nouveautes_echec_jamais_garde(env, monkeypatch):
    from cop_corpus import web_live

    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return []

    monkeypatch.setattr(web_live.search, "search", flaky)
    c = _client()
    assert c.get(f"/database/negotiations/{SLUG}").status_code == 500
    assert not web_live._news_cache
    assert c.get(f"/database/negotiations/{SLUG}").status_code == 200
    assert c.get(f"/database/negotiations/{SLUG}").status_code == 200
    assert len(calls) == 2


# --- critère 6 : version devenue inaccessible retirée à la lecture, sans recalcul -----------------------------------

@pytest.mark.parametrize("change", ["UPDATE documents SET access='restreint' WHERE id=?",
                                    "UPDATE documents SET status='error' WHERE id=?"])
def test_version_inaccessible_retiree_sans_recalcul(env, monkeypatch, change):
    from cop_corpus import formulations, frises

    frises.compute_all(env, _dossier())
    before = frises.read(env, SLUG, None)[0]
    env.execute(change, (V4,))   # la dernière version : titre et cote repris de la précédente
    env.commit()

    def interdit(*a, **k):
        raise AssertionError("calcul de frise dans le site")

    monkeypatch.setattr(frises, "compute", interdit)
    monkeypatch.setattr(formulations, "grid", interdit)
    f = frises.read(env, SLUG, None)[0]
    g = f["grille"]
    assert [v["doc_id"] for v in g["versions"]] == [V2, V3]
    assert [v["version"] for v in g["versions"]] == [1, 2] and g["versions"][1]["prev"] == V2
    assert f["cote"] == "DT.CMA8.i4.3" and "i4.4" not in f["titre"]
    assert all(len(n["cellules"]) == 2 for n in g["notions"])
    assert all(b["doc_id"] != V4 for n in g["notions"] for b in n["bascules"])
    assert not any(n["cle"] == "charbon" for n in g["notions"])   # notion présente seulement dans la v4 : ôtée
    assert len(before["grille"]["versions"]) == 3
    page = _client().get(f"/database/negotiations/{SLUG}").text
    assert BLOC in page and f"/diff/{V3}?avec={V2}" in page and f"/diff/{V4}?avec=" not in page
    # Version du milieu retirée : la suivante pointe vers la précédente gardée.
    env.execute(change, (V3,))
    env.execute("UPDATE documents SET access='public', status='indexed' WHERE id=?", (V4,))
    env.commit()
    g = frises.read(env, SLUG, None)[0]["grille"]
    assert [(v["doc_id"], v["version"], v["prev"]) for v in g["versions"]] == [(V2, 1, None), (V4, 2, V2)]
    # Moins de deux versions : plus de frise.
    env.execute(change, (V4,))
    env.commit()
    assert frises.read(env, SLUG, None) == []
    # Un lecteur habilité garde les versions restreintes (variante calculée sans elles : rien de plus à montrer).
    if "access" in change:
        assert len(frises.read(env, SLUG, {"id": 1, "can_restricted": 1})[0]["grille"]["versions"]) == 3


# --- critère 7 : frises absentes, page sans le bloc et sans calcul -------------------------------------------------

def test_frises_absentes_page_sans_bloc_sans_calcul(env, monkeypatch):
    from cop_corpus import formulations, frises

    def interdit(*a, **k):
        raise AssertionError("calcul de frise dans le site")

    monkeypatch.setattr(frises, "compute", interdit)
    monkeypatch.setattr(formulations, "grid", interdit)
    monkeypatch.setattr(formulations, "lineage", interdit)
    res = _client().get(f"/database/negotiations/{SLUG}")
    assert res.status_code == 200 and BLOC not in res.text
    assert env.execute("SELECT COUNT(*) FROM dossier_frises").fetchone()[0] == 0


def test_frises_stockees_affichees(env):
    from cop_corpus import frises

    frises.compute_all(env, _dossier())
    page = _client().get(f"/database/negotiations/{SLUG}").text
    assert BLOC in page and f"/diff/{V4}?avec={V3}" in page


# --- critère 8 : vecteurs chargés avant la première requête ---------------------------------------------------------

def _fresh_mcp(monkeypatch):
    """Le gestionnaire de sessions MCP ne tourne qu'une fois par instance : un neuf pour chaque application."""
    from cop_corpus.mcp_server import mcp

    monkeypatch.setattr(mcp, "_session_manager", None)


def test_vecteurs_charges_dans_le_lifespan(env, monkeypatch):
    from starlette.testclient import TestClient
    from cop_corpus import embed, server

    _fresh_mcp(monkeypatch)
    order = []
    monkeypatch.setattr(embed, "enabled", lambda: True)
    monkeypatch.setattr(embed, "_refresh", lambda conn: order.append("vecteurs"))
    app = server.build_app()
    with TestClient(app) as c:
        assert order == ["vecteurs"]          # avant toute requête
        order.append("requete")
        assert c.get("/healthz").status_code == 200
    assert order == ["vecteurs", "requete"]


def test_vecteurs_en_echec_le_site_demarre(env, monkeypatch):
    from starlette.testclient import TestClient
    from cop_corpus import embed, monitor, server

    _fresh_mcp(monkeypatch)
    reported = []
    monkeypatch.setattr(embed, "enabled", lambda: True)
    monkeypatch.setattr(embed, "_refresh", lambda conn: (_ for _ in ()).throw(RuntimeError("database is locked")))
    monkeypatch.setattr(monitor, "report", lambda place, exc, **k: reported.append(place))
    with TestClient(server.build_app()) as c:
        assert c.get("/healthz").status_code == 200
    assert reported == ["site : chargement des vecteurs"]


# --- critère 9 : le tour tient extract._slots ; un dossier en échec n'empêche pas les autres ------------------------

def test_tour_tient_le_creneau_d_extraction(env, monkeypatch):
    from cop_corpus import extract, formulations, frises

    seen = []
    real = frises.compute

    def compute(*a):
        seen.append(extract._slots._value)
        return real(*a)

    monkeypatch.setattr(frises, "compute", compute)
    frises.compute_all(env, _dossier())
    assert seen == [0] and extract._slots._value == 1
    assert not formulations._CACHE and not formulations._DIFFS   # documents découpés libérés après le tour


def test_tour_attend_une_extraction_en_cours(env):
    from cop_corpus import extract, frises

    done = threading.Event()
    with extract._slots:   # une lecture de PDF en cours
        t = threading.Thread(target=lambda: (frises.compute_all(_conn(), _dossier()), done.set()))
        t.start()
        assert not done.wait(0.3)
    assert done.wait(10)
    t.join(5)


def _conn():
    from cop_corpus import db

    return db.connect()


def test_un_dossier_en_echec_n_empeche_pas_les_autres(env, monkeypatch):
    from cop_corpus import formulations, frises, live, monitor

    reported = []
    monkeypatch.setattr(monitor, "report", lambda place, exc, **k: reported.append(place))
    ds = [live.dossier("finance"), live.dossier(SLUG), live.dossier("adaptation")]
    real = frises.compute

    def compute(conn, texts, user):
        if not texts:   # « finance » et « adaptation » : aucun texte dans ce corpus de test
            raise RuntimeError("document corrompu")
        return real(conn, texts, user)

    monkeypatch.setattr(frises, "compute", compute)
    out = frises.compute_all(env, ds)
    assert out == {"recalcules": 1, "inchanges": 0, "echecs": 2, "differes": 0}
    assert reported == ["worker : frises finance", "worker : frises adaptation"]
    assert _stored(env) and not env.in_transaction
    assert not formulations._CACHE


def test_dossier_en_echec_retenu_30_min_ou_jusqu_a_un_changement(env, monkeypatch):
    from cop_corpus import frises, monitor

    reported, calls, clock = [], [], [1000.0]
    monkeypatch.setattr(monitor, "report", lambda place, exc, **k: reported.append(place))
    monkeypatch.setattr(frises.time, "monotonic", lambda: clock[0])

    def boom(*a):
        calls.append(1)
        raise RuntimeError("document corrompu")

    real = frises.compute
    monkeypatch.setattr(frises, "compute", boom)
    assert frises.compute_all(env, _dossier())["echecs"] == 1 and len(calls) == 1
    clock[0] += 60
    assert frises.compute_all(env, _dossier())["differes"] == 1      # même signature, < 30 min : pas d'essai
    assert len(calls) == 1 and reported == [f"worker : frises {SLUG}"]
    clock[0] += frises.RETRY_AFTER                                    # 30 min passées : nouvel essai, signalé
    assert frises.compute_all(env, _dossier())["echecs"] == 1 and len(calls) == 2 and len(reported) == 2
    clock[0] += 60
    env.execute("UPDATE documents SET title='Corrigé' WHERE id=?", (V4,))   # signature changée : essai aussitôt
    env.commit()
    assert frises.compute_all(env, _dossier())["echecs"] == 1 and len(calls) == 3 and len(reported) == 3
    monkeypatch.setattr(frises, "compute", real)   # réparé : le changement suivant recalcule et oublie l'échec
    env.execute("UPDATE documents SET title='Encore' WHERE id=?", (V4,))
    env.commit()
    assert frises.compute_all(env, _dossier())["recalcules"] == 1 and SLUG not in frises._failed


def test_echec_de_la_signature_signale_une_fois(env, monkeypatch):
    from cop_corpus import frises, monitor

    reported = []
    monkeypatch.setattr(monitor, "report", lambda place, exc, **k: reported.append(place))
    monkeypatch.setattr(frises, "state", lambda conn, slug: (_ for _ in ()).throw(RuntimeError("base verrouillée")))
    for _ in range(3):
        assert frises.compute_all(env, _dossier())["echecs"] == 1
    assert reported == [f"worker : frises {SLUG}"]


# --- retouche de revue : lecture robuste ------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "{pas du json",
    json.dumps({"autre": "forme"}),
    json.dumps([{"titre": "t", "cote": "c"}]),                                        # clé « grille » manquante
    json.dumps([{"titre": "t", "cote": "c", "grille": {"versions": [{"doc_id": 1}], "notions": []}}]),
    json.dumps([{"titre": "t", "cote": "c", "grille": {"versions": [], "notions": [
        {"cle": "x", "fr": "x", "en": "x", "cellules": [{"etat": "inconnu", "unites": []}], "bascules": []}]}}]),
])
def test_ligne_illisible_traitee_comme_absente(env, monkeypatch, value):
    from cop_corpus import frises, monitor

    frises.compute_all(env, _dossier())
    env.execute("UPDATE dossier_frises SET value=?", (value,))
    env.commit()
    reported = []
    monkeypatch.setattr(monitor, "report", lambda place, exc, **k: reported.append(place))
    assert frises.read(env, SLUG, None) == []
    assert frises.read(env, SLUG, None) == []
    res = _client().get(f"/database/negotiations/{SLUG}")
    assert res.status_code == 200 and BLOC not in res.text
    assert reported == [f"site : frises illisibles {SLUG}"]      # un seul rapport pour cette ligne


def test_fil_frises_un_tour(env, monkeypatch):
    from cop_corpus import db, frises, live, watchdog, worker

    watchdog._last_beat.clear()
    ds = _dossier()   # liste figée d'abord : live.dossier appelle lui-même live.dossiers
    monkeypatch.setattr(live, "dossiers", lambda: ds)
    stop = threading.Event()
    th = threading.Thread(target=worker.frises_forever, args=(stop, 3600), daemon=True)
    th.start()
    import time

    deadline = time.monotonic() + 10
    while db.meta_get(env, "hb:frises") is None and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    th.join(5)
    assert db.meta_get(env, "hb:frises") and _stored(env)
    assert frises.read(env, SLUG, None)


def test_fil_frises_survit_a_une_erreur(env, monkeypatch):
    from cop_corpus import frises, monitor, worker

    stop, reported = threading.Event(), []

    def boom(conn):
        stop.set()
        raise RuntimeError("base verrouillée")

    monkeypatch.setattr(frises, "compute_all", boom)
    monkeypatch.setattr(monitor, "report", lambda place, exc, **k: reported.append(place))
    worker.frises_forever(stop, 0)
    assert reported == ["worker : frises"]


# --- critère 3, en production : script de comparaison en lecture seule ----------------------------------------------

def _script():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "compare_frises.py"
    spec = importlib.util.spec_from_file_location("compare_frises", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, path.read_text()


def test_script_de_comparaison(env, monkeypatch, capsys):
    from cop_corpus import config, frises, live

    ds = _dossier()
    monkeypatch.setattr(live, "dossiers", lambda: ds)
    frises.compute_all(env, ds)
    mod, src = _script()
    assert "mode=ro" in src and "db.connect(" not in src and ".commit(" not in src
    before = env.execute("SELECT * FROM dossier_frises").fetchall()
    assert mod.main([str(config.DB_PATH)]) == 0
    assert "identique" in capsys.readouterr().out
    assert [tuple(r) for r in env.execute("SELECT * FROM dossier_frises").fetchall()] == [tuple(r) for r in before]
    # Frise stockée altérée : signalée, code de sortie 1.
    value = json.loads(before[0]["value"])
    value[0]["grille"]["versions"][0]["crochets"] += 1
    env.execute("UPDATE dossier_frises SET value=?", (json.dumps(value),))
    env.commit()
    assert mod.main([str(config.DB_PATH)]) == 1
    assert "DIFFÉRENT" in capsys.readouterr().out
    # Textes changés depuis le dernier tour : « périmé », pas une différence.
    env.execute("UPDATE documents SET title='x' WHERE id=?", (V3,))
    env.commit()
    assert mod.main([str(config.DB_PATH)]) == 0
    assert "périmé" in capsys.readouterr().out
