"""Voie de secours des textes de session : fiches numérotées de docs.unfccc.int."""

import importlib

import pytest


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
    return db.get()


def landing(n, symbol, title, conf="UN Climate Change Conference - Antalya, November 2026"):
    return f"""<html><body><h1>{title}</h1><dl>
<dt>Symbol</dt><dd>{symbol}</dd><dt>Publication date and time</dt><dd>18 Nov 2026 08:00 (GMT+3)</dd>
<dt>Document type</dt><dd>draft text</dd><dt>Conference</dt><dd>{conf}</dd>
<dt>Session</dt><dd>CMA 8</dd><dd>,</dd><dd>CMP 21</dd><dt>Topic</dt><dd>Climate finance</dd></dl>
<a href="/documents/{n}/files/en/AF Matters_DT_{n}.pdf">download the document</a>
<h3>Agenda item</h3><p>CMA 8</p><p>11(e). Matters relating to the Adaptation Fund</p></body></html>"""


def test_parse_landing():
    from cop_corpus import docs_unfccc

    d = docs_unfccc.parse_landing(landing(10000349, "DT.CMP21.i6_CMA8.i.11e.2", "Matters relating to the Adaptation Fund"),
                                  "https://docs.unfccc.int/documents/10000349")
    assert d["symbol"] == "DT.CMP21.i6_CMA8.i.11e.2" and d["published"] == "2026-11-18T08:00"
    assert d["pdf_url"] == "https://docs.unfccc.int/documents/10000349/files/en/AF%20Matters_DT_10000349.pdf"
    assert (d["body"], d["agenda_item"], d["doc_type"]) == ("CMA 8", "11 e", "draft text")


def test_sync_announces_new_texts_and_stops_on_404(env):
    from cop_corpus import db, docs_unfccc, live
    from cop_corpus.fetch import FetchResult

    live.ensure_schema(env)
    env.execute("INSERT INTO live_texts(key,symbol,title,pdf_url) VALUES('DT.X.1','DT.X.1','Known',"
                "'https://docs.unfccc.int/documents/10000349/files/en/x.pdf')")
    env.commit()
    pages = {10000350: landing(10000350, "DT.CMA8.i16b.3", "Further guidance for the mechanism established by Article 6, paragraph 4"),
             10000351: landing(10000351, "DT.OTHER.1", "Some other meeting", conf="Bonn, June 2027"),
             10000353: landing(10000353, "DT.COP31.i13.4", "Periodic review of the long-term global goal")}
    asked = []

    class Client:
        def get(self, url, **kw):
            n = int(url.rsplit("/", 1)[1])
            asked.append(n)
            return FetchResult(url=url, status=200, content=pages[n].encode(), mime="text/html") if n in pages \
                else FetchResult(url=url, status=404)

    out = docs_unfccc.sync(env, Client())
    assert out == {"nouveaux": 2, "connus": 0, "autres": 1, "absents": 4}           # trou 10000352 franchi
    assert asked == [10000350, 10000351, 10000352, 10000353, 10000354, 10000355, 10000356]
    row = env.execute("SELECT * FROM live_texts WHERE key='DT.CMA8.i16b.3'").fetchone()
    assert row["dossier"] == "article-6" and row["status"] == "a_recuperer" and row["published"] == "2026-11-18T08:00"
    assert db.meta_get(env, docs_unfccc.META_KEY) == "10000353"
    asked.clear()
    assert docs_unfccc.sync(env, Client())["nouveaux"] == 0 and asked == [10000354, 10000355, 10000356]
