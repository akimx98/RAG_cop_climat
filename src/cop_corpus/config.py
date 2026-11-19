"""Configuration par variables d'environnement (aucun secret dans le code)."""

from __future__ import annotations

import os
import re
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
SOURCE_URL = os.environ.get("COP_SOURCE_URL", "https://github.com/akimx98/RAG_cop_climat")   # dépôt public du code (licence MIT)
CONTACT_EMAIL = os.environ.get("COP_CONTACT_EMAIL", "")   # destinataire du formulaire de contact (jamais affiché)

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

# Fuseau de la COP (Antalya) pour l'affichage et le digest du matin.
TIMEZONE = os.environ.get("COP_TZ", "Europe/Istanbul")
DIGEST_HOURS = [int(h) for h in os.environ.get("COP_DIGEST_HOURS", "7,19").split(",") if h.strip()]  # récapitulatif Telegram

# --- Serveur MCP --------------------------------------------------------------------------------------------------
MCP_ALLOWED_HOSTS = [h for h in re.split(r"[,\s]+", os.environ.get("MCP_ALLOWED_HOSTS", "")) if h]
LOCAL_ADMIN = os.environ.get("COP_LOCAL_ADMIN", "1") == "1"  # MCP en stdio (usage local) : administrateur local

# --- Courrier sortant (code de connexion par e-mail) ----------------------------------------------------------------
MAIL_PROVIDER = os.environ.get("COP_MAIL_PROVIDER", "").strip().lower()   # resend | brevo | vide
MAIL_API_KEY = os.environ.get("COP_MAIL_API_KEY", "").strip()
MAIL_FROM = os.environ.get("COP_MAIL_FROM", f"COP31 tracker <noreply@{MAIL_DOMAIN}>")

# --- Modèle de langage (positions, briefs, analyses de soumissions ; sources publiques seulement) --------------------
LLM_EXTRACT_MODEL = os.environ.get("COP_LLM_EXTRACT_MODEL", "gpt-5.4-mini")
LLM_WRITE_MODEL = os.environ.get("COP_LLM_WRITE_MODEL", "gpt-5.6-luna")
LLM_BUDGET_USD = float(os.environ.get("COP_LLM_BUDGET_USD", "30"))


# --- Démo publique « Essayer » (questions sans compte ; documents publics seulement) -----------------------------
DEMO_MODEL = os.environ.get("COP_DEMO_MODEL", "gpt-6.1-terra")
DEMO_EFFORT = os.environ.get("COP_DEMO_EFFORT", "low")                       # effort de raisonnement ; vide = défaut
DEMO_PER_SESSION = int(os.environ.get("COP_DEMO_PER_SESSION", "3"))          # questions par visiteur (cookie)
DEMO_PER_IP_DAY = int(os.environ.get("COP_DEMO_PER_IP_DAY", "20"))            # par adresse IP et par jour (invités)
DEMO_PER_ACCOUNT_DAY = int(os.environ.get("COP_DEMO_PER_ACCOUNT_DAY", "3"))  # par compte connecté et par jour
DEMO_DAILY_USD = float(os.environ.get("COP_DEMO_DAILY_USD", "10"))           # plafond quotidien, invités
DEMO_BUDGET_USD = float(os.environ.get("COP_DEMO_BUDGET_USD", "150"))        # plafond total (invités + comptes)
# Fin de COP : les comptes gratuits travaillent sur le site, avec bien plus de questions, jusqu'à une date donnée
# (heure d'Antalya). Ensuite, retour automatique à DEMO_PER_ACCOUNT_DAY.
DEMO_BOOST_UNTIL = os.environ.get("COP_DEMO_BOOST_UNTIL", "2026-11-21T23:59:59+03:00")
DEMO_BOOST_PER_ACCOUNT_DAY = int(os.environ.get("COP_DEMO_BOOST_PER_ACCOUNT_DAY", "40"))
DEMO_ACCOUNT_DAILY_USD = float(os.environ.get("COP_DEMO_ACCOUNT_DAILY_USD", "40"))  # plafond quotidien, comptes

DEMO_KEEP_DAYS = int(os.environ.get("COP_DEMO_KEEP_DAYS", "90"))             # questions de la démo gardées (lien de partage)

# --- Statistiques d'usage (src/cop_corpus/usage.py, page /confidentialite) -------------------------------------------
USAGE_TEXT_DAYS = int(os.environ.get("COP_USAGE_TEXT_DAYS", "30"))   # texte des recherches sans résultat (invités)
USAGE_KEEP_DAYS = int(os.environ.get("COP_USAGE_KEEP_DAYS", "395"))  # événements d'usage


def demo_enabled() -> bool:
    return llm_enabled() and os.environ.get("COP_DEMO", "1") != "0"


def llm_enabled() -> bool:
    """Lu à chaque appel (la clé peut être posée ou retirée sans redémarrer, et les tests la changent)."""
    return bool(os.environ.get("OPENAI_API_KEY")) and os.environ.get("COP_LLM", "1") != "0"


# --- Veille en direct ------------------------------------------------------------------------------------------------
LIVE = os.environ.get("COP_LIVE", "1") != "0"                       # fil de la veille en direct
LIVE_SINCE = os.environ.get("COP_LIVE_SINCE", "2026-11-01")         # positions : sources publiées depuis
LIVE_UNDATED_SINCE = os.environ.get("COP_LIVE_UNDATED_SINCE", "2026-11-07T18:00:00Z")  # … ou reçues depuis (sans date)
BRIEFS = os.environ.get("COP_BRIEFS", "0") == "1"   # briefs IA par dossier (7 h / 19 h) ; désactivés par défaut
BRIEF_HOURS = [int(h) for h in os.environ.get("COP_BRIEF_HOURS", "7,19").split(",") if h.strip()]

# --- Soumissions officielles (portail CCNUCC) ------------------------------------------------------------------------
SUB = os.environ.get("COP_SUB", "1") != "0"                         # collecte et analyse
SUB_AUTO_ANALYSE = os.environ.get("COP_SUB_AUTO_ANALYSE", "0") == "1"  # 1 = analyser en tâche de fond (coût IA)
SUB_ANALYSE_SINCE = os.environ.get("COP_SUB_ANALYSE_SINCE", "2026-01-01")   # analyse auto. des Parties depuis
SUB_PER_TICK = int(os.environ.get("COP_SUB_PER_TICK", "60"))        # fichiers téléchargés par passage
SUB_EMBED_SINCE = os.environ.get("COP_SUB_EMBED_SINCE", "2026-01-01").replace("'", "")  # vectorisées depuis
