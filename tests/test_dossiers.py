"""Pages dossier (note docs/notes/2026-11-27-dossiers.md) : frises plus rapides à résultat identique. Le préchauffage
du site a disparu en phase 2 (frises calculées par le worker : tests/test_frises.py)."""

import threading

from test_lenteur import env  # noqa: F401  (fixture)


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
