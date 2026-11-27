"""Réglages communs aux tests.

Les tests existants décrivent le comportement en session de négociation (rythmes de la conférence, Telegram
complet) : `COP_SESSION=1` par défaut ici. Le comportement hors session (défaut en production depuis le 27/11/2026)
est testé à part, en le demandant explicitement (`tests/test_hors_session.py`)."""

import pytest


@pytest.fixture(autouse=True)
def _session(monkeypatch):
    from cop_corpus import config

    monkeypatch.setenv("COP_SESSION", "1")
    monkeypatch.setattr(config, "SESSION", True)
