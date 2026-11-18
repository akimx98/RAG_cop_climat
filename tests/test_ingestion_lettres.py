"""Lettres d'information (texte complet, date, un document par lettre), ENB en PDF, nettoyage de l'extraction."""

import importlib
from email.message import EmailMessage

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
    for mod in ("embed", "auth", "search", "ingest", "alerts", "diff", "telegram", "mailin", "enb"):
        importlib.reload(importlib.import_module(f"cop_corpus.{mod}"))
    return db.get()


CB_HTML = """<html><head><style>.x{color:red}</style></head><body>
<div style="display:none">Preview text</div>
<table><tr><td><h2>COP31 ‘cover text’</h2><p>Negotiators at COP31 in Antalya received the first draft of the
cover decision, which mentions a global electrification goal and a roadmap for transitioning away from fossil fuels.
The Arab Group and Russia rejected the text.</p>
<a href="https://pdpwbj.clicks.mlsend.com/tj/c/abc">Read more</a></td></tr>
<tr><td><h2>US data centres</h2><p>""" + "Data centres in the US are driving new gas demand, analysts say. " * 6 + """</p>
<a href="https://www.carbonbrief.org/daily-brief/cover-text">Carbon Brief</a>
<a href="https://www.facebook.com/carbonbrief">Facebook</a></td></tr></table></body></html>"""


def _cb(day="Tue, 17 Nov 2026 07:05:00 +0000"):
    m = EmailMessage()
    m["From"], m["Subject"], m["Date"] = "Carbon Brief <newsletter@carbonbrief.org>", \
        "Daily Briefing: COP31 ‘cover text’ | US data centres", day
    m.set_content("Hello,\nYou have received a newsletter from Carbon Brief.\nHowever, your email software can't display "
                  "HTML emails. You can view the newsletter by clicking here:\nhttps://pdpwbj.clicks.mlsend.com/tj/c/x\n"
                  "You're receiving this newsletter because you have shown interest in Carbon Brief.")
    m.add_alternative(CB_HTML, subtype="html")
    return m


def test_newsletter_full_text_from_html_when_plain_is_a_stub(env):
    from cop_corpus import mailin

    text = mailin.newsletter_text(_cb())
    assert "can't display" not in text and "Arab Group and Russia rejected" in text
    assert "Data centres in the US" in text and "Preview text" not in text and ".x{" not in text
    assert "https://www.carbonbrief.org/daily-brief/cover-text" in text            # lien de contenu gardé
    assert "mlsend" not in text and "facebook" not in text                          # pistage et réseaux sociaux écartés
    meta = mailin.newsletter_meta(_cb())
    assert meta["published"] == "2026-11-17" and meta["title"].endswith("(2026-11-17)")
    assert meta["doc_type"] == "compte_rendu" and meta["org"] == "Carbon Brief"


def test_each_newsletter_is_its_own_document(env):
    """Les lettres de l'ENB en français portent toutes le même objet : jamais des « versions » l'une de l'autre."""
    from cop_corpus import mailin

    ids = []
    for day, what in (("Mon, 16 Nov 2026 20:00:00 +0000", "l'adaptation"), ("Tue, 17 Nov 2026 20:00:00 +0000", "la finance")):
        m = EmailMessage()
        m["From"], m["Date"] = "Earth Negotiations Bulletin <enb@iisd.org>", day
        m["Subject"] = "Conférence des Nations Unies sur les changements climatiques à Antalya"
        m.set_content(f"Les Parties ont débattu de {what} et de FCCC/CP/2026/L.3. " * 12)
        ids += mailin.ingest_newsletter(env, m, "x.eml")
    rows = env.execute("SELECT version, lineage_key, symbol, published FROM documents WHERE id IN (?,?)", ids).fetchall()
    assert [r["version"] for r in rows] == [1, 1] and rows[0]["lineage_key"] != rows[1]["lineage_key"]
    assert all(r["symbol"] is None for r in rows)                                   # une cote citée n'en fait pas une version
    assert [r["published"] for r in rows] == ["2026-11-16", "2026-11-17"]


def test_reread_replaces_teaser_in_place(env):
    from cop_corpus import config, ingest, mailin

    rel = "mail/archive/2026-11/cb.eml"
    path = config.DATA_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_cb().as_bytes())
    # Document créé par l'ancienne extraction : la partie texte de remplacement seule.
    stub = "You have received a newsletter from Carbon Brief. However, your email software can't display HTML. " * 3
    old, _, _ = ingest.ingest_bytes(env, stub.encode(), url=None, mime="text/plain", filename="message.txt",
                                    meta={"title": "Carbon Brief — Daily Briefing", "level": "4"}, source_id="veille-mail")
    env.execute("INSERT INTO mail_inbox(received_at,mailbox,sender,subject,path,status,doc_ids) VALUES(?,?,?,?,?,?,?)",
                ("2026-11-17T07:06:00Z", "veille", "newsletter@carbonbrief.org", "Daily", rel, "ingere", f"[{old}]"))
    env.commit()
    assert mailin.reread_newsletters(env, dry_run=True)["remplacees"] == 1
    out = mailin.reread_newsletters(env)
    assert out["remplacees"] == 1
    doc = env.execute("SELECT * FROM documents WHERE id=?", (old,)).fetchone()
    text = " ".join(r["text"] for r in env.execute("SELECT text FROM passages WHERE doc_id=?", (old,)))
    assert "Arab Group and Russia rejected" in text and "can't display" not in text
    assert doc["published"] == "2026-11-17" and doc["version"] == 1 and doc["status"] == "indexed"
    assert mailin.reread_newsletters(env)["inchangees"] == 1                        # idempotent


def test_text_cleanup():
    from cop_corpus import extract

    assert extract.fix_text("TÃ¼rkiye") == "Türkiye"
    assert extract.fix_text("lâ€™Union europÃ©enne") == "l’Union européenne"
    assert extract.fix_text("S u b m i s s i o n  b y  t h e  E u r o p e a n  U n i o n") == "Submission by the European Union"
    assert extract.fix_text("Parties (a) b c d e are x") == "Parties (a) b c d e are x"     # énumérations intactes
    assert extract.decode_html("Türkiye ’".encode("cp1252")) == "Türkiye ’"          # TWN : pas de « � »
    assert extract.parse_date("Vol. 12 No. 909 Wednesday, 18 November 2026") == "2026-11-18"
    assert extract.parse_date("Mercredi 18 novembre 2026") == "2026-11-18"
    assert extract.parse_date("Published November 17, 2026") == "2026-11-17"


def _two_column_pdf() -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(pymupdf.Rect(24, 20, 571, 60), "Earth Negotiations Bulletin   Wednesday, 18 November 2026", fontsize=11)
    left = ["Action for Climate Empowerment: Parties debated the", "activities in the ACE action plan and the gender-responsive",
            "activities in priority area C on tools and support.", "The RUSSIAN FEDERATION insisted on replacing the term",
            "with gender-sensitive language in all the activities."]
    right = ["Response measures: The G-77/CHINA stressed the overall", "budgetary implications of the forum and its Katowice",
             "committee of experts on the impacts of implementation.", "The EU supported the workplan of the forum as drafted.",
             "Parties will continue discussions at the next session."]
    y = 80
    for l, r in zip(left, right):
        page.insert_textbox(pymupdf.Rect(24, y, 280, y + 40), l, fontsize=10)
        page.insert_textbox(pymupdf.Rect(310, y, 570, y + 40), r, fontsize=10)
        y += 50
    return doc.tobytes()


def test_pdf_columns_are_read_one_after_the_other(tmp_path):
    from cop_corpus import extract

    path = tmp_path / "enb.pdf"
    path.write_bytes(_two_column_pdf())
    _t, pages = extract.extract(path)
    text = " ".join(pages[0].text.split())
    ace, g77 = text.index("Action for Climate Empowerment"), text.index("budgetary implications")
    assert text.index("gender-sensitive language") < g77                       # la colonne de gauche d'abord
    assert ace < text.index("gender-responsive") < text.index("RUSSIAN FEDERATION") < g77


ENB_P1 = ("COP 31 #8\n\nEarth Negotiations Bulletin\n\nOnline at: enb.iisd.org/antalya-un-climate-change-conference-cop31\n"
          "Vol. 12 No. 909\n\nAntalya Climate Change Conference:\n\nTuesday, 17 November 2026\n\nHours-long consultations")
ENB_P2 = "Earth Negotiations Bulletin\nWednesday, 18 November 2026\nVol. 12 No. 909  Page 2\n\nThe LMDCs"


def test_enb_header_and_title():
    from cop_corpus import enb, extract

    h = enb.header([extract.Page(1, ENB_P1), extract.Page(2, ENB_P2)])
    assert (h["no"], h["issue"], h["day"], h["published"]) == (909, 8, "2026-11-17", "2026-11-18")
    assert enb.title_for(h, "en") == ("Earth Negotiations Bulletin Vol. 12 No. 909 (COP 31 #8) — daily report, "
                                      "Tuesday, 17 November 2026 — published 2026-11-18")
    fr = enb.header([extract.Page(1, "COP 31 #8\n\nBulletin des Négociations de la Terre\nMercredi 18 novembre 2026\n"
                                     "Vol. 12 No. 909\n\nConférence d’Antalya sur \nle changement climatique\xa0:\n\n"
                                     "Mardi 17 novembre 2026\n\nDes consultations"),
                     extract.Page(2, "Bulletin des Négociations de la Terre\nMercredi 18 novembre 2026\nVol. 12 No. 909")])
    assert (fr["day"], fr["published"]) == ("2026-11-17", "2026-11-18")
    assert "compte rendu du mardi 17 novembre 2026" in enb.title_for(fr, "fr")
    assert enb.classify_link("https://enb.iisd.org/antalya-un-climate-change-conference-cop31-daily-report-16nov2026") \
        == ("daily_report", "2026-11-16")
    assert enb.classify_link("https://enb.iisd.org/antalya-un-climate-change-conference-cop31-16Nov2026")[0] == "highlights"
    assert enb.classify_link("https://enb.iisd.org/masterclass-ai-water-energy-food-systems-cop31")[0] == "side_event"


def test_enb_numbered_pdfs_are_probed_and_annotated(env, monkeypatch):
    """Numéros successifs essayés dans le dossier du mois ; titre et dates relus dans le PDF ; arrêt après 2 absents."""
    import pymupdf
    from cop_corpus import catalog, enb, ingest
    from cop_corpus.fetch import FetchResult

    def pdf(no, issue, day, pub):
        doc = pymupdf.open()
        p1 = doc.new_page()
        p1.insert_text((40, 60), f"COP 31 #{issue}")
        p1.insert_text((40, 90), "Earth Negotiations Bulletin")
        p1.insert_text((40, 120), f"Vol. 12 No. {no}")
        p1.insert_text((40, 150), "Antalya Climate Change Conference:")
        p1.insert_text((40, 180), day)
        p1.insert_text((40, 210), "The EU, supported by NORWAY, opposed by SAUDI ARABIA, proposed text.")
        p2 = doc.new_page()
        p2.insert_text((40, 40), "Earth Negotiations Bulletin")
        p2.insert_text((40, 60), pub)
        p2.insert_text((40, 80), f"Vol. 12 No. {no}  Page 2")
        return doc.tobytes()

    files = {"enb12902e.pdf": pdf(902, 1, "Monday, 9 November 2026", "Tuesday, 10 November 2026"),
             "enb12903e.pdf": pdf(903, 2, "Tuesday, 10 November 2026", "Wednesday, 11 November 2026")}
    asked = []

    class Client:
        def get(self, url, etag=None, last_modified=None):
            asked.append(url)
            name = url.rsplit("/", 1)[-1]
            if "/2026-11/" in url and name in files:
                return FetchResult(url=url, status=200, content=files[name], mime="application/pdf")
            return FetchResult(url=url, status=404)

    monkeypatch.setattr(enb, "_folders", lambda raw: ["2026-11", "2026-10"])
    src = next(s for s in catalog.load() if s.id == "enb-daily-pdf")
    src.raw["langs"] = ["e"]
    out = ingest.run_source(src, Client())
    assert out.get("new") == 2 and out.get("absent") == 2 and "error" not in out
    rows = env.execute("SELECT title, published, note, doc_type, lang, session FROM documents ORDER BY id").fetchall()
    assert rows[0]["title"].startswith("Earth Negotiations Bulletin Vol. 12 No. 902 (COP 31 #1) — daily report, Monday, 9 November")
    assert rows[1]["published"] == "2026-11-11" and "rend compte du 2026-11-10" in rows[1]["note"]
    assert (rows[0]["doc_type"], rows[0]["lang"], rows[0]["session"]) == ("compte_rendu", "en", "COP31")
    n_before = len(asked)
    ingest.run_source(src, Client())                    # deuxième passage : seuls les numéros suivants sont essayés
    assert all("enb12902e" not in u and "enb12903e" not in u for u in asked[n_before:])


G77 = ("Statement on behalf of the Group of 77 and China at the opening of COP31. " +
       " ".join(f"On item {i}, the Group calls for scaled-up finance, technology transfer and capacity-building "
                f"for developing countries, and for action for climate empowerment number {i}." for i in range(40)))


def test_content_duplicates_keep_the_official_copy(env):
    from cop_corpus import doublons, ingest

    site, _, _ = ingest.ingest_bytes(env, ("G77 website\n" + G77).encode(), url="https://www.g77.org/statement/cop31.htm",
                                     mime="text/plain", filename="g77.txt", meta={"level": "5", "title": "G77 statement"})
    portal, _, _ = ingest.ingest_bytes(env, G77.encode(), url="https://submissions.unfccc.int/x.pdf", mime="text/plain",
                                       filename="p.txt", meta={"level": "2", "title": "UNFCCC-SUB/13600 — G77 opening"})
    rows = {r["id"]: r["duplicate_of"] for r in env.execute("SELECT id, duplicate_of FROM documents")}
    assert rows == {site: portal, portal: None}                        # le niveau 2 l'emporte, quel que soit l'ordre
    # Deux versions d'un projet de texte (même lignée) ou deux cotes différentes : jamais des doublons.
    draft = G77.replace("Group calls for", "Conference of the Parties decides on")
    a, _, _ = ingest.ingest_bytes(env, draft.replace("COP31", "COP 31").encode(), url=None, mime="text/plain", filename="a.txt",
                                  meta={"level": "2", "title": "Draft ACE decision", "doc_type": "projet_texte",
                                        "symbol": "FCCC/CP/2026/L.7"})
    b, _, _ = ingest.ingest_bytes(env, draft.replace("COP31", "COP-31").encode(), url=None, mime="text/plain", filename="b.txt",
                                  meta={"level": "2", "title": "Draft ACE decision", "doc_type": "projet_texte",
                                        "symbol": "FCCC/CP/2026/L.8"})
    assert env.execute("SELECT duplicate_of FROM documents WHERE id=?", (b,)).fetchone()[0] is None
    assert doublons.listing(env)[0]["id"] == site


def test_presscorner_text_comes_from_the_api():
    """Discours de la Commission à la COP : page JavaScript vide, texte servi par l'API, sans coordonnées de presse."""
    import json
    from cop_corpus import ingest
    from cop_corpus.fetch import FetchResult

    asked = []

    class Client:
        def get(self, url, accept=None, **kw):
            asked.append(url)
            return FetchResult(url=url, status=200, mime="application/json", content=json.dumps({
                "refCd": "SPEECH/26/2468", "eventDate": "2026-11-17", "placeResource": {"description": "Antalya"},
                "contactsResource": [{"phone": "+32 2 295 75 01", "mail": "x@ec.europa.eu"}],
                "docuLanguageResource": {"title": "Remarks by Commissioner Hoekstra at the high-level segment at COP31",
                                         "htmlContent": "<p>The EU stays the course.</p>"}}).encode())

    res = ingest.resolve(Client(), "https://ec.europa.eu/commission/presscorner/detail/en/speech_26_2468")
    assert asked == ["https://ec.europa.eu/commission/presscorner/api/documents?reference=SPEECH/26/2468&language=EN"]
    page = res.content.decode()
    assert res.ok and "The EU stays the course." in page and "high-level segment" in page
    assert "+32" not in page and "ec.europa.eu" not in page.split("<body>")[1]
    assert res.url.endswith("speech_26_2468")                     # l'adresse citée reste la page lisible
