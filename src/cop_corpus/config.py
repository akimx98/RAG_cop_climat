"""Configuration par variables d'environnement (aucun secret dans le code)."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


DATA_DIR = _path("COP_DATA_DIR", ROOT / "data")
RAW_DIR = DATA_DIR / "raw"
DB_PATH = _path("COP_DB", DATA_DIR / "cop.sqlite3")
CATALOG_DIR = _path("COP_CATALOG", ROOT / "catalog")

# URL publique du service (liens dans les alertes et l'interface).
PUBLIC_BASE = os.environ.get("COP_PUBLIC_BASE", "http://localhost:8000").rstrip("/")
# Courrier entrant : domaine des adresses (veille@, depot-<code>@) et certificat TLS (STARTTLS).
from urllib.parse import urlparse as _urlparse  # noqa: E402

MAIL_DOMAIN = os.environ.get("COP_MAIL_DOMAIN", _urlparse(PUBLIC_BASE).hostname or "localhost")
MAIL_DIR = DATA_DIR / "mail"
_CERTS = f"/caddy/caddy/certificates/acme-v02.api.letsencrypt.org-directory/{MAIL_DOMAIN}/{MAIL_DOMAIN}"
MAIL_CERT = os.environ.get("COP_MAIL_CERT", _CERTS + ".crt")
MAIL_KEY = os.environ.get("COP_MAIL_KEY", _CERTS + ".key")
MAIL_MAX_MB = float(os.environ.get("COP_MAIL_MAX_MB", "40"))
# Clé de signature des cookies et liens. Obligatoire en production.
SECRET = os.environ.get("COP_SECRET", "dev-secret-change-me")
SOURCE_URL = os.environ.get("COP_SOURCE_URL", "")   # dépôt public du code, affiché s'il est renseigné

USER_AGENT = os.environ.get(
    "COP_USER_AGENT",
    "Mozilla/5.0 (compatible; cop-corpus/0.1; veille documentaire delegation COP31)",
)
# Délai minimal entre deux requêtes vers un même hôte (secondes).
HOST_DELAY = float(os.environ.get("COP_HOST_DELAY", "1.0"))
MAX_DOWNLOAD_MB = float(os.environ.get("COP_MAX_DOWNLOAD_MB", "60"))

# Embeddings (API). Vide → recherche lexicale seule.
EMBED_PROVIDER = os.environ.get("COP_EMBED_PROVIDER", "openai" if os.environ.get("OPENAI_API_KEY") else "")
EMBED_MODEL = os.environ.get("COP_EMBED_MODEL", "text-embedding-3-small")
EMBED_DIM = int(os.environ.get("COP_EMBED_DIM", "512"))
# Les documents restreints ne partent PAS vers une API d'embeddings, sauf choix explicite.
EMBED_RESTRICTED = os.environ.get("COP_EMBED_RESTRICTED", "0") == "1"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_BOT_NAME = os.environ.get("TELEGRAM_BOT_NAME", "")
# Envoyer des extraits de documents restreints dans Telegram ? (non chiffré de bout en bout)
TELEGRAM_RESTRICTED_SNIPPETS = os.environ.get("COP_TELEGRAM_RESTRICTED_SNIPPETS", "0") == "1"
TELEGRAM_ALLOW_UPLOAD = os.environ.get("COP_TELEGRAM_ALLOW_UPLOAD", "1") == "1"

# Fuseau de la COP (Antalya) pour l'affichage et le digest du matin.
TIMEZONE = os.environ.get("COP_TZ", "Europe/Istanbul")
DIGEST_HOUR = int(os.environ.get("COP_DIGEST_HOUR", "7"))
