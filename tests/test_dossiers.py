"""Pages dossier (note docs/notes/2026-11-27-dossiers.md) : préchauffage en arrière-plan, frises plus rapides à
résultat identique."""

import threading

from test_lenteur import env  # noqa: F401  (fixture)


def test_prechauffage_remplit_le_cache_public(env, monkeypatch):  # noqa: F811
    from cop_corpus import live, web_live

    ds = live.dossiers()[:3]   # liste figée d'abord : live.dossier appelle lui-même live.dossiers
    slugs = [d["slug"] for d in ds]
    monkeypatch.setattr(live, "dossiers", lambda: ds)
    stop = threading.Event()
    calls = []
    real = web_live._heavy

    def heavy(conn, slug, d, ts, user):
        calls.append((slug, user))
        if len(calls) == len(slugs):
            stop.set()
        return real(conn, slug, d, ts, user)

    monkeypatch.setattr(web_live, "_heavy", heavy)
    monkeypatch.setattr(stop, "wait", lambda t=None: stop.is_set())   # pas d'attente réelle
    web_live.warm_forever(stop, pause=0)
    assert [c[0] for c in calls] == slugs and all(u is None for _, u in calls)
    assert {k for k in web_live._heavy_cache} == {(s, False) for s in slugs}


def test_prechauffage_ne_s_arrete_pas_sur_une_erreur(env, monkeypatch):  # noqa: F811
    from cop_corpus import live, monitor, web_live

    reported = []
    monkeypatch.setattr(monitor, "report", lambda place, exc, **k: reported.append(place))
    stop = threading.Event()
    tours = []

    def broken():
        tours.append(1)
        if len(tours) == 2:
            stop.set()
        raise RuntimeError("base verrouillée")

    monkeypatch.setattr(live, "dossiers", broken)
    monkeypatch.setattr(stop, "wait", lambda t=None: stop.is_set())
    web_live.warm_forever(stop, pause=0)
    assert len(tours) == 2 and reported == ["site : préchauffage des dossiers"] * 2


def test_occurrences_identiques_avec_et_sans_crochets():
    """Le texte sans crochets (mis en cache) et le préfiltre de notion donnent les mêmes états qu'un calcul direct."""
    from cop_corpus import formulations as f

    for text in ["Parties shall [transition away from fossil fuels] in energy systems",
                 "Parties shall transition away from fossil fuels", "[[nested] brackets] and coal", "no match here",
                 "fin [ouverte sans fermeture fossil fuels"]:
        plain, depths = f._plain(text)
        ref, ref_depths, depth = [], [], 0
        for ch in text:
            if ch == "[":
                depth += 1
                continue
            if ch == "]":
                depth = max(0, depth - 1)
                continue
            ref.append(ch)
            ref_depths.append(depth)
        assert plain == "".join(ref)
        assert (depths or [0] * len(plain)) == ref_depths
        for pos in range(len(plain) + 2):
            k, raw = 0, len(text)
            for i, ch in enumerate(text):
                if ch in "[]":
                    continue
                if k == pos:
                    raw = i
                    break
                k += 1
            assert f._raw_pos(text, pos) == raw


def test_un_dossier_en_echec_n_empeche_pas_les_suivants(env, monkeypatch):  # noqa: F811
    from cop_corpus import live, monitor, web_live

    ds = live.dossiers()[:3]
    monkeypatch.setattr(live, "dossiers", lambda: ds)
    monkeypatch.setattr(monitor, "report", lambda *a, **k: None)
    stop = threading.Event()
    done = []

    def heavy(conn, slug, d, ts, user):
        done.append(slug)
        if slug == ds[0]["slug"]:
            raise RuntimeError("document corrompu")
        if len(done) == 3:
            stop.set()

    monkeypatch.setattr(web_live, "_heavy", heavy)
    monkeypatch.setattr(stop, "wait", lambda t=None: stop.is_set())
    web_live.warm_forever(stop, pause=0)
    assert done == [d["slug"] for d in ds]


def test_occurrences_en_parallele(env, monkeypatch):  # noqa: F811
    """Plusieurs fils sur le même document en cache : mêmes occurrences qu'un appel seul, aucune erreur."""
    from cop_corpus import formulations as f, structure

    doc = structure.Document(1, [], {}, "")
    for i, t in enumerate(["1. Parties shall [phase out] fossil fuels.", "2. Coal remains.", "3. Nothing here."] * 300):
        u = structure.Unit(f"§{i}", "para", None)
        u.text = t
        doc.units.append(u)
    f._CACHE.clear()
    monkeypatch.setattr(f, "_doc", lambda conn, doc_id: doc)   # document fixe (pas de base)
    pats = [__import__("re").compile(m, 2) for m in f.CONCEPTS["fossiles"]["motifs"]]
    ref = f.occurrences(None, 1, pats)
    doc.__dict__.pop("_plains", None)
    out, errors = [], []

    def run():
        try:
            out.append(f.occurrences(None, 1, pats))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    ts = [threading.Thread(target=run) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors and all(o == ref for o in out) and len(ref) == 300
