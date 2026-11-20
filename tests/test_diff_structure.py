"""Diff structuré sur un cas réel (19 novembre 2026) : DT.CMA8.i4.2 (16/11) → DT.CMA8.i4.3 (19/11), CMA 4, rapport
sur le dialogue annuel sur le bilan mondial. Passages tels qu'indexés en production depuis les PDF de la CCNUCC
(le « 4. Option 1 » collé au §3, les notes de bas de page au milieu des puces)."""

import importlib
import json
from pathlib import Path

import pytest

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "gst_dialogue_v2_v3.json").read_text())


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
    for mod in ("embed", "auth", "search", "ingest", "diff"):
        importlib.reload(importlib.import_module(f"cop_corpus.{mod}"))
    conn = db.get()
    for item in FIXTURE:
        d = item["doc"]
        conn.execute("INSERT INTO documents(id, sha256, url, title, level, doc_type, symbol, published, fetched_at, "
                     "mime, raw_path, access, lineage_key, version, status, n_pages) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,"
                     "?,?,?)", (d["id"], f"sha{d['id']}", d["url"], d["title"], "2", "projet_texte", d["symbol"],
                                d["published"], d["published"], "application/pdf", f"raw/{d['id']}.pdf", "public",
                                d["lineage_key"], d["version"], "indexed", d["n_pages"]))
        for p in item["passages"]:
            conn.execute("INSERT INTO passages(id, doc_id, seq, page, para, text) VALUES(?,?,?,?,?,?)",
                         (p["id"], d["id"], p["seq"], p["page"], p["para"], p["text"]))
    conn.commit()
    return conn


def test_structure_splits_glued_paragraphs_options_bullets_and_footnotes(env):
    from cop_corpus import structure

    v2 = structure.load(env, 2623)
    paras = [u.uid for u in v2.units if u.kind == "para"]
    assert paras == ["§1", "§2", "§3", "§4", "§5", "§6", "§7"]
    assert [c for c in v2.get("§4").children] == ["§4.opt1", "§4.opt2"]
    assert v2.get("§4.opt1").no_text
    assert v2.get("§4.opt2").children == [f"§4.opt2.b{i}" for i in range(1, 6)]
    assert v2.get("§3").body.endswith("processes;")            # « 4. Option 1 » n'est plus collé au §3
    assert v2.footnotes[1] == "FCCC/PA/CMA/2026/3." and v2.footnotes[4] == "Decision 3/CMA.7, para. 14."
    # Notes retirées du texte (« 1 FCCC/… » au milieu de la puce 4, appels « thereon1 », « [first,2 second3] »)
    full = " ".join(u.text for u in v2.units)
    assert "FCCC/" not in full and "thereon1" not in full and "second 3" not in full
    assert "unilateral trade-related measures constitute barriers" in v2.get("§4.opt2.b4").text
    assert v2.get("§5.opt1").notes == ["FCCC/PA/CMA/2024/5.", "FCCC/PA/CMA/2025/6."]
    assert v2.get("§4.opt2.b1").pages == [1] and v2.get("§4.opt2.b1").passages == [194369]


def test_diff_links_units_flags_deletions_and_settles_options(env):
    from cop_corpus import diff

    res = diff.compare(env, 2623, 10821)
    by_new = {c["unite"]: c for c in res["changements"]}
    # §4 et §5 de la v3 viennent de la puce 1 de l'option 2 du §4 ; le §6, de la puce 2.
    assert by_new["§4"]["statut"] == "modifié" and by_new["§4"]["ancien"]["unite"] == "§4.opt2.b1"
    assert by_new["§4"]["repris_aussi_dans"] == ["§5"]
    assert by_new["§5"]["statut"] == "repris_de" and by_new["§5"]["repris_de"] == "§4.opt2.b1"
    assert by_new["§6"]["ancien"]["unite"] == "§4.opt2.b2"
    assert by_new["§7"]["ancien"]["unite"] == "§6" and by_new["§8"]["ancien"]["unite"] == "§7"
    # Écarts et 1,5 °C, mesures commerciales unilatérales, trajectoires du GIEC : supprimés sans reprise.
    gone = {c["unite"] for c in res["changements"] if c["statut"] == "supprimé_sans_reprise"}
    assert {"§4.opt2.b3", "§4.opt2.b4", "§4.opt2.b5"} <= gone
    assert "§4.opt1" not in gone and "§5.opt2" not in gone     # « no text » : jugé avec les options
    opts = {o["paragraphe"]: o for o in res["options"]}
    assert opts["§4"]["issue"].startswith("Option 2 retenue en partie")
    assert "§4.opt2.b3, §4.opt2.b4, §4.opt2.b5" in opts["§4"]["issue"]
    assert opts["§5"]["issue"].startswith("Option 2 (pas de texte) retenue de fait")
    # Crochets comptés et suivis ; alternative [Notes with appreciation][Welcomes] tranchée.
    assert res["avant"]["crochets"] == 32 and res["apres"]["crochets"] == 0
    assert res["avant"]["alternatives"] >= 4 and res["avant"]["unites_avec_crochets"] >= 10
    alt = by_new["§2"]["crochets"][0]
    assert alt["type"] == "alternative" and alt["devenir"] == "« Notes with appreciation » retenu"
    nested = [c for c in by_new["§6"]["crochets"] if c.get("choix") == ["the need for", "the importance of"]]
    assert nested and nested[0]["devenir"] == "« the importance of » retenu"
    assert any(c.get("contenu") == "and grant-based" and c["devenir"] == "supprimé" for c in by_new["§6"]["crochets"])
    # Rien de tronqué, pas de note de bas de page dans les diffs, une page et un lien PDF partout.
    assert res["tronque"] is False and res["pages"] == 1
    for c in res["changements"]:
        assert "FCCC/" not in (c.get("diff") or "") + (c.get("texte_ancien") or "") + (c.get("texte_nouveau") or "")
        for side in ("ancien", "nouveau"):
            if c.get(side):
                assert c[side]["page"] and "#page=" in c[side]["lien_source"] and c[side]["passage_id"]
    # Diff complet (pas de « … ») ; nuance ajoutée au §3.
    assert "…" not in by_new["§1"]["diff"] and by_new["§1"]["diff"].startswith("Expresses appreciation")
    assert by_new["§3"]["nuances"]["ajoutees"] == ["as appropriate"]
    assert by_new["§7"]["notes"] == {"supprimees": ["Decision 3/CMA.7, para. 14."]}


def test_mcp_pages_fit_ask_budget(env):
    """Via /ask, chaque page tient sous la limite d'un appel d'outil ; un client MCP reçoit tout d'un coup."""
    from cop_corpus import demo, mcp_server

    full = mcp_server.comparer_versions("10821")
    assert full["pages"] == 1 and len(full["changements"]) == full["nb_changements"]
    first = demo.call_tool("comparer_versions", {"document_recent": "10821"})
    assert first["ancien"]["doc_id"] == 2623 and first["resume"]
    seen = [c["unite"] for c in first["changements"]]
    assert len(json.dumps(first, ensure_ascii=False)) < demo.MAX_TOOL_CHARS
    for p in range(2, first["pages"] + 1):
        nxt = demo.call_tool("comparer_versions", {"document_recent": "10821", "page": p})
        assert len(json.dumps(nxt, ensure_ascii=False)) < demo.MAX_TOOL_CHARS
        seen += [c["unite"] for c in nxt["changements"]]
    assert first["tronque"] is (first["pages"] > 1)
    assert seen == [c["unite"] for c in full["changements"]]
    if first["pages"] > 1:
        assert len(first["sommaire"]) == full["nb_changements"]
    only = mcp_server.comparer_versions("10821", filtre="§4.opt2")
    assert only["changements"] and all("§4.opt2" in (c["ancien"] or {}).get("unite", "") for c in only["changements"])


def test_filter_is_exact_on_units_and_text_only(env):
    from cop_corpus import diff

    res = diff.compare(env, 2623, 10821, filtre="§4")
    def under4(u):
        return u == "§4" or u.startswith("§4.")
    # Nouveau §4 et tout ce qui vient de l'ancien §4 (§5, §6 de la v3 viennent de ses puces), pas §40…
    assert all(under4(c["unite"]) or under4((c["ancien"] or {}).get("unite", "")) for c in res["changements"])
    assert {"§4", "§5", "§6", "§4.opt2.b3"} <= {c["unite"] for c in res["changements"]}
    res = diff.compare(env, 2623, 10821, filtre="§1")      # pas §10…§19
    assert {c["unite"] for c in res["changements"]} == {"§1"}
    assert diff.compare(env, 2623, 10821, filtre="passage")["changements"] == []   # pas les clés ni les liens
    assert diff.compare(env, 2623, 10821, filtre="grant-based")["changements"]


def test_ask_diff_view_shows_cited_units_or_the_most_telling(env):
    """Sous une réponse /ask : les unités citées, sinon d'abord les suppressions sans reprise ; liens vers /diff."""
    from cop_corpus import demo

    refs = demo.Refs()
    res = refs.tag(demo.call_tool("comparer_versions", {"document_recent": "10821"}), "comparer_versions")
    assert all("#u-" in c["lien_diff"] and "avec=2623" in c["lien_diff"] for c in res["changements"])
    view = demo._diff_previews([res], set(), refs)[0]
    assert not view["cites"] and view["unites"][0]["statut"] == "supprimé_sans_reprise"
    assert view["lien"].endswith("/diff/10821?avec=2623") and view["crochets"] == [32, 0]
    s2 = next(c for c in res["changements"] if c["unite"] == "§2")["nouveau"]["ref"]
    cited = demo._diff_previews([res], {s2}, refs)[0]
    assert cited["cites"] and "§2" in [u["unite"] for u in cited["unites"]]
    assert "[-[-]Notes" in next(u["texte"] for u in cited["unites"] if u["unite"] == "§2")


def test_wording_timeline_tracks_brackets_options_and_switches(env):
    """1,5 °C, mesures unilatérales : entre crochets dans les puces de l'option 2 (v2), disparus en v3."""
    from cop_corpus import formulations, mcp_server

    out = mcp_server.historique_formulation("1.5", texte="10821")
    v2, v3 = out["versions"]
    assert v2["etat"] == "entre crochets" and {o["unite"] for o in v2["occurrences"]} == {"§4.opt2.b3", "§4.opt2.b5"}
    assert v3["etat"] == "absente" and v3["evenement"].startswith("disparaît")
    assert out["bascules"][0]["lien_diff"].endswith("/diff/10821?avec=2623")
    o = v2["occurrences"][0]
    assert "1.5 °C" in o["extrait"] and o["page"] == 1 and "#page=1" in o["lien_source"]
    assert mcp_server.historique_formulation("unilateral measures", texte="10821")["versions"][0]["etat"] == \
        "entre crochets"
    # Mots libres, cherchés tels quels ; présent sans crochets dans les deux versions.
    free = mcp_server.historique_formulation("annual global stocktake dialogue", texte="10821")
    assert [v["etat"] for v in free["versions"]] == ["sans crochets", "sans crochets"] and not free["bascules"]
    # Une mention sans crochets dans une option en balance : « dans une option ».
    opt = mcp_server.historique_formulation("take into account the information", texte="10821")
    assert opt["versions"][0]["etat"] == "dans une option"
    import sqlite3  # noqa: F401
    rows = formulations.lineage(env, env.execute("SELECT * FROM documents WHERE id=10821").fetchone(), None)
    grid = formulations.grid(env, rows)
    assert [c["crochets"] for c in grid["versions"]] == [32, 0]
    assert {line["cle"] for line in grid["notions"]} >= {"1.5", "mesures_unilaterales"}
