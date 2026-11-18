"""Journaux d'accès sans secrets ni adresses IP complètes.

Plusieurs URL du site portent un secret dans leur chemin : l'adresse MCP personnelle (/u/<jeton>/mcp), le lien de
connexion signé (/entrer/…), l'ancien lien à jeton (/login/…) et les codes d'invitation (/join/…, /rejoindre/…).
Le journal d'accès d'uvicorn les écrirait en clair (le middleware travaille sur une copie de la requête : le chemin
d'origine reste celui qu'uvicorn journalise). On les masque, et on tronque l'adresse IP (/24 en IPv4, /48 en IPv6).
La même règle est appliquée côté Caddy (deploy/edge/sites/cop31.caddy.template)."""

from __future__ import annotations

import ipaddress
import logging
import re

SECRET_PATH = re.compile(r"/(u|entrer|login|join|rejoindre)/[^/?\s\"]+")


def redact(text: str) -> str:
    return SECRET_PATH.sub(lambda m: f"/{m.group(1)}/***", text)


def mask_ip(value: str) -> str:
    """'203.0.113.57:51234' → '203.0.113.0' ; IPv6 → préfixe /48. Valeur inconnue → '-'."""
    host = (value or "").strip()
    if host.startswith("[") and "]" in host:
        host = host[1:host.index("]")]
    elif host.count(":") == 1:
        host = host.split(":")[0]
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return "-"
    prefix = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False).network_address)


class RedactFilter(logging.Filter):
    """Pour le logger « uvicorn.access » : args = (client, méthode, chemin, version HTTP, statut)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and args:
            out = list(args)
            out[0] = mask_ip(str(out[0]))
            out = [redact(a) if isinstance(a, str) else a for a in out]
            record.args = tuple(out)
        elif isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def uvicorn_log_config() -> dict:
    """La configuration de journalisation d'uvicorn, avec le filtre sur le journal d'accès."""
    import copy

    from uvicorn.config import LOGGING_CONFIG

    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg.setdefault("filters", {})["redact"] = {"()": "cop_corpus.logs.RedactFilter"}
    cfg["handlers"]["access"].setdefault("filters", []).append("redact")
    return cfg
