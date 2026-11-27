"""Parcours complet d'une question sur le site (/try/ask), avec un faux client OpenAI : vrai prompt système,
vraie boucle d'outils, vraie recherche dans le corpus, rendu et vérification des citations, flux SSE.

C'est le test qui doit casser quand une modification empêche le site de répondre (ex. le 2026-11-19 : des
accolades dans le prompt faisaient planter `SYSTEM.format`, et toutes les questions échouaient)."""

import importlib
import json
from types import SimpleNamespace

import pytest

PASSAGE = "Parties shall pursue domestic mitigation measures, with the aim of achieving the objectives of such contributions."


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COP_EMBED_PROVIDER", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from cop_corpus import config

    importlib.reload(config)
    from cop_corpus import db

    importlib.reload(db)
    db._local.__dict__.clear()
    for mod in ("embed", "auth", "search", "ingest", "alerts", "diff", "llm", "demo", "telegram", "mailin", "web",
                "web_live"):
        importlib.reload(importlib.import_module(f"cop_corpus.{mod}"))
    from cop_corpus import live, usage

    live._ready.clear()
    usage._ready.clear()
    return db.get()


class FakeOpenAI:
    """Premier tour : le modèle appelle `rechercher` ; second tour : il répond en citant le passage trouvé."""

    calls: list[dict] = []

    def __init__(self, *a, **kw):
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        FakeOpenAI.calls.append(kwargs)
        usage = SimpleNamespace(input_tokens=100, output_tokens=20)
        if len(FakeOpenAI.calls) == 1:
            call = SimpleNamespace(type="function_call", name="rechercher", call_id="c1",
                                   arguments=json.dumps({"requete": "domestic mitigation measures"}))
            return SimpleNamespace(id="r1", output=[call], output_text="", usage=usage, status="completed")
        # La réponse cite la première source renvoyée par l'outil.
        tool_out = json.loads(kwargs["input"][0]["output"])
        ref = tool_out["resultats"][0]["ref"]
        text = f'Le texte dit que les Parties "shall pursue domestic mitigation measures" [{ref}].'
        return SimpleNamespace(id="r2", output=[], output_text=text, usage=usage, status="completed")


def _events(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


def test_une_question_sur_le_site_produit_une_reponse_sourcee(env, monkeypatch):
    import openai
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from cop_corpus import ingest, web, web_live

    ingest.ingest_bytes(env, (PASSAGE + " ") .encode() * 20, url="https://unfccc.int/x.pdf", filename="decision.txt")
    FakeOpenAI.calls = []
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    c = TestClient(Starlette(routes=[*web.routes, *web_live.routes]), base_url="https://testserver")

    assert c.get("/try").status_code == 200            # pose le cookie de visite
    r = c.post("/try/ask", json={"question": "Que dit le texte sur l'atténuation ?", "lang": "fr"})
    assert r.status_code == 200, r.text
    evs = _events(r.text)
    errors = [e for e in evs if e.get("type") == "error"]
    assert not errors, errors
    done = [e for e in evs if e.get("type") == "done"]
    assert done, evs
    assert "shall pursue domestic mitigation measures" in done[0]["html"]
    assert done[0]["sources"], "la réponse doit citer au moins une source"
    # Le vrai prompt système a bien été construit et envoyé.
    assert "ABSOLUTE RULES" in FakeOpenAI.calls[0]["instructions"]
    assert "{" not in FakeOpenAI.calls[0]["instructions"].replace("{+…+}", "")


def test_le_prompt_systeme_se_remplit(env):
    from cop_corpus import demo

    for lang in ("fr", "en"):
        demo.SYSTEM.format(today="lundi", lang_name=lang)


@pytest.fixture()
def admin_chat(env, monkeypatch):
    """Un administrateur relié à Telegram ; les messages envoyés sont capturés au lieu de partir."""
    from cop_corpus import config, monitor, telegram

    importlib.reload(monitor)
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "test")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(telegram, "send", lambda chat, text, **k: sent.append((chat, text)) or True)
    env.execute("INSERT INTO users(email, name, is_admin, active, telegram_chat_id) VALUES('a@x.fr','A',1,1,'42')")
    env.execute("INSERT INTO users(email, name, is_admin, active, telegram_chat_id) VALUES('u@x.fr','U',0,1,'43')")
    env.commit()
    return sent


def test_une_reponse_en_echec_previent_les_administrateurs(env, admin_chat, monkeypatch):
    import openai
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from cop_corpus import monitor, web, web_live

    class Broken:
        def __init__(self, *a, **kw):
            raise RuntimeError("quota exceeded")

    monkeypatch.setattr(openai, "OpenAI", Broken)
    monkeypatch.setattr(monitor, "_spawn", lambda fn, *a: fn(*a))
    c = TestClient(Starlette(routes=[*web.routes, *web_live.routes]), base_url="https://testserver")
    c.get("/try")
    r = c.post("/try/ask", json={"question": "Où en est la finance climat ?", "lang": "fr"})
    assert any(e.get("type") == "error" for e in _events(r.text))
    assert [chat for chat, _ in admin_chat] == ["42"]              # l'administrateur seulement
    assert "RuntimeError: quota exceeded" in admin_chat[0][1]
    assert "finance climat" not in admin_chat[0][1]                # jamais la question de l'utilisateur


def test_alertes_regroupees_puis_renvoyees(env, admin_chat, monkeypatch):
    from datetime import timedelta

    from cop_corpus import monitor

    for i in range(5):
        monitor.report("worker : veille", ValueError(f"boom {i}"), background=False)
    assert len(admin_chat) == 1                                    # même erreur à un chiffre près : un message
    monitor.report("worker : soumissions", ValueError("autre"), background=False)
    assert len(admin_chat) == 2                                    # autre endroit : autre message
    later = monitor._now() + timedelta(minutes=monitor.RESEND_MINUTES + 1)
    monkeypatch.setattr(monitor, "_now", lambda: later)
    monitor.report("worker : veille", ValueError("boom 9"), background=False)
    assert len(admin_chat) == 3 and "5 fois depuis la dernière alerte" in admin_chat[2][1]


def test_serie_de_questions_en_echec(env, admin_chat):
    from cop_corpus import demo, monitor

    for _ in range(2):
        demo.save(env, question="q", lang="fr", session="s", ip="1.2.3.4", user=None, payload=None, error="KeyError: 'x'")
    assert not monitor.check_answers(env)                          # 2 échecs : pas encore une série
    demo.save(env, question="q", lang="fr", session="s", ip="1.2.3.4", user=None, payload=None, error="KeyError: 'x'")
    assert monitor.check_answers(env)
    assert "3 dernières questions ont échoué" in admin_chat[-1][1]


def test_page_en_erreur_500_previent_et_masque_les_jetons(env, admin_chat, monkeypatch):
    from starlette.testclient import TestClient

    from cop_corpus import monitor, server

    monkeypatch.setattr(monitor, "_spawn", lambda fn, *a: fn(*a))

    async def boom(request):
        raise ZeroDivisionError("division by zero")

    from starlette.applications import Starlette
    from starlette.routing import Route

    app = Starlette(routes=[Route("/u/{tok}/x", boom)], exception_handlers={Exception: server.server_error})
    r = TestClient(app, raise_server_exceptions=False).get("/u/SECRET123/x")
    assert r.status_code == 500
    assert "ZeroDivisionError" in admin_chat[0][1] and "SECRET123" not in admin_chat[0][1]
