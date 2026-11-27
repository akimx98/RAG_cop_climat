"""Sources refusées par le site (#2) : 403 = robot refusé (anti-bot, jamais contourné), 402 = article payant ; ni l'un
ni l'autre ne fait apparaître la source dans la liste « sans aucun succès » du chien de garde."""

from test_core import env  # noqa: F401  (fixture)


class _Client:
    def __init__(self, status):
        self.status = status

    def get(self, url, **kw):
        from cop_corpus import fetch

        return fetch.FetchResult(url=url, status=self.status)


def _source(kind, url):
    from cop_corpus import catalog

    raw = {"id": f"essai-{kind}", "type": kind, "url": url}
    return catalog.Source(id=raw["id"], type=kind, raw=raw, file="essai.yaml")


def _state(conn, sid):
    return conn.execute("SELECT failing_since, last_result FROM source_state WHERE source_id=?", (sid,)).fetchone()


def test_403_et_402_classes_comme_refus(env, monkeypatch):  # noqa: F811
    from cop_corpus import ingest

    src = _source("doc", "https://exemple.org/a.pdf")
    assert ingest.fetch_one(env, _Client(403), src.urls[0], src, {}, recheck=False) == "antibot"
    assert ingest.fetch_one(env, _Client(402), "https://exemple.org/b.pdf", src, {}, recheck=False) == "payant"
    assert ingest.fetch_one(env, _Client(500), "https://exemple.org/c.pdf", src, {}, recheck=False) == "error"
    outcomes = [r[0] for r in env.execute("SELECT outcome FROM fetch_log ORDER BY id")]
    assert outcomes == ["antibot", "payant", "error"]


def test_flux_refuse_compte_comme_antibot_et_payant_pas_en_echec(env, monkeypatch):  # noqa: F811
    from cop_corpus import ingest, watchdog

    rss = _source("rss", "https://exemple.org/feed")
    ingest.run_source(rss, _Client(403))
    failing, result = _state(env, rss.id)
    assert eval(result) == {"antibot": 1} and failing                       # compté, signalé à part
    assert watchdog._antibot_only(result)

    class Paywall:   # flux lisible, articles payants
        def get(self, url, **kw):
            from cop_corpus import fetch

            if url.endswith("/feed"):
                body = (b"<rss><channel><item><title>A</title><link>https://exemple.org/art</link></item>"
                        b"</channel></rss>")
                return fetch.FetchResult(url=url, status=200, content=body, mime="application/rss+xml")
            return fetch.FetchResult(url=url, status=402)

    lm = _source("rss", "https://exemple.org/feed")
    lm.raw["id"] = lm.id = "essai-payant"
    ingest.run_source(lm, Paywall())
    failing, result = _state(env, lm.id)
    assert eval(result) == {"payant": 1} and failing is None                 # pas une source en échec


def test_source_payante_espacee(env):  # noqa: F811
    from datetime import datetime, timedelta, timezone

    from cop_corpus import catalog, worker

    src = catalog.Source(id="essai-espace", type="rss", raw={"id": "essai-espace", "watch_minutes": 15}, file="x")
    last = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    env.execute("INSERT INTO source_state(source_id,last_run,last_result) VALUES(?,?,?)",
                (src.id, last, str({"payant": 14})))
    env.commit()
    assert not worker._due(env, src)                    # 30 min < 240 : espacée comme une source en erreur


def test_serie_ods_refusee_s_arrete(env):  # noqa: F811
    from cop_corpus import catalog, ingest

    raw = {"id": "essai-ods", "type": "ods", "symbols": ["FCCC/CP/2026/L.{n}"], "range": [1, 40],
           "stop_after_misses": 3, "langs": ["en"]}
    src = catalog.Source(id="essai-ods", type="ods", raw=raw, file="x")
    counts = ingest.run_source(src, _Client(403))
    assert counts == {"antibot": 3}                      # arrêt après 3 refus, pas 40 requêtes


def test_codes_de_depot_par_courrier_vides(env):  # noqa: F811
    """#8 : le dépôt par courrier est retiré ; ses codes secrets sont effacés au démarrage (minimisation)."""
    from cop_corpus import db

    env.execute("INSERT INTO users(email, name, mail_code) VALUES('m@x.fr', 'M', 'abcdef123456')")
    env.commit()
    db._migrate(env)
    assert env.execute("SELECT COUNT(*) FROM users WHERE mail_code IS NOT NULL").fetchone()[0] == 0
