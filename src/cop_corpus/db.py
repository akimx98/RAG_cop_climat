"""Schéma SQLite unique : documents, passages (FTS5), vecteurs, utilisateurs,
abonnements, alertes, journal de collecte, données chiffrées."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS documents (
  id            INTEGER PRIMARY KEY,
  sha256        TEXT NOT NULL UNIQUE,
  url           TEXT,
  source_id     TEXT,
  title         TEXT,
  level         TEXT,          -- '1'..'5', 'G'
  org           TEXT,
  doc_type      TEXT,
  session       TEXT,
  symbol        TEXT,
  parties       TEXT,          -- JSON
  topics        TEXT,          -- JSON
  lang          TEXT,
  published     TEXT,
  fetched_at    TEXT NOT NULL,
  mime          TEXT,
  raw_path      TEXT,
  size          INTEGER,
  access        TEXT NOT NULL DEFAULT 'public',   -- public | restreint
  lineage_key   TEXT,
  version       INTEGER DEFAULT 1,
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending | indexed | empty | error | data
  error         TEXT,
  n_pages       INTEGER,
  n_passages    INTEGER,
  uploaded_by   INTEGER,
  note          TEXT
);
CREATE INDEX IF NOT EXISTS documents_lineage ON documents(lineage_key);
CREATE INDEX IF NOT EXISTS documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS documents_url ON documents(url);

CREATE TABLE IF NOT EXISTS url_state (
  url           TEXT PRIMARY KEY,
  source_id     TEXT,
  etag          TEXT,
  last_modified TEXT,
  last_sha      TEXT,
  last_status   INTEGER,
  last_checked  TEXT,
  first_seen    TEXT
);

CREATE TABLE IF NOT EXISTS passages (
  id        INTEGER PRIMARY KEY,
  doc_id    INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  seq       INTEGER NOT NULL,
  page      INTEGER,
  para      TEXT,
  text      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS passages_doc ON passages(doc_id, seq);

CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(
  text, title,
  content='', contentless_delete=1,
  tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS vectors (
  passage_id INTEGER PRIMARY KEY,
  vec        BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id                INTEGER PRIMARY KEY,
  email             TEXT UNIQUE,
  name              TEXT,
  token_hash        TEXT UNIQUE,
  is_admin          INTEGER DEFAULT 0,
  can_restricted    INTEGER DEFAULT 1,
  telegram_chat_id  TEXT,
  telegram_code     TEXT UNIQUE,
  active            INTEGER DEFAULT 1,
  created_at        TEXT
);

CREATE TABLE IF NOT EXISTS subscriptions (
  id                 INTEGER PRIMARY KEY,
  user_id            INTEGER NOT NULL REFERENCES users(id),
  label              TEXT NOT NULL,
  query              TEXT NOT NULL,
  levels             TEXT,       -- JSON list ou NULL = tous
  doc_types          TEXT,       -- JSON list ou NULL = tous
  notify_telegram    INTEGER DEFAULT 1,
  active             INTEGER DEFAULT 1,
  created_at         TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
  id               INTEGER PRIMARY KEY,
  user_id          INTEGER NOT NULL,
  subscription_id  INTEGER,
  doc_id           INTEGER NOT NULL,
  passage_id       INTEGER,
  snippet          TEXT,
  created_at       TEXT NOT NULL,
  telegram_sent_at TEXT,
  UNIQUE(user_id, subscription_id, doc_id)
);
CREATE INDEX IF NOT EXISTS notifications_user ON notifications(user_id, id);

CREATE TABLE IF NOT EXISTS fetch_log (
  id        INTEGER PRIMARY KEY,
  ts        TEXT NOT NULL,
  source_id TEXT,
  url       TEXT,
  outcome   TEXT,
  detail    TEXT
);

CREATE TABLE IF NOT EXISTS source_state (
  source_id   TEXT PRIMARY KEY,
  last_run    TEXT,
  last_result TEXT
);

CREATE TABLE IF NOT EXISTS mail_inbox (
  id           INTEGER PRIMARY KEY,
  received_at  TEXT,
  mailbox      TEXT,        -- 'veille' ou 'depot'
  user_id      INTEGER,     -- déposant (adresse depot-)
  sender       TEXT,
  subject      TEXT,
  path         TEXT,        -- .eml relatif à DATA_DIR
  status       TEXT,        -- ingere | attente | ignore | erreur
  detail       TEXT,
  doc_ids      TEXT         -- JSON
);

CREATE TABLE IF NOT EXISTS login_codes (   -- code de connexion envoyé par e-mail (comptes sans mot de passe)
  user_id     INTEGER PRIMARY KEY,
  code_hash   TEXT,
  expires     REAL,
  tries       INTEGER DEFAULT 0,
  sent_at     TEXT       -- JSON : horodatages des derniers envois (limite par heure)
);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS emissions (
  iso3       TEXT,
  country    TEXT,
  year       INTEGER,
  indicator  TEXT,
  value      REAL,
  unit       TEXT,
  source     TEXT,
  PRIMARY KEY (iso3, year, indicator, source)
);
"""

_local = threading.local()
_schema_lock = threading.Lock()
_initialized: set[str] = set()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_COLUMNS = {
    "users": {
        "password_hash": "TEXT",   # scrypt ; NULL = pas de mot de passe défini
        "token_enc": "TEXT",       # jeton MCP chiffré (clé dérivée de COP_SECRET) pour pouvoir le réafficher
        "mail_code": "TEXT",       # partie secrète de l'adresse de dépôt depot-<code>@<domaine>
        "organisation": "TEXT",    # déclarée à l'inscription par invitation
        "source": "TEXT",          # comptes invités : 'invitation' (lien/QR) ou 'inscription' (libre) ; NULL = créé par un admin
        "lang": "TEXT",            # 'en' | 'fr' choisi par l'utilisateur ; NULL = anglais
        "mcp_seen_at": "TEXT",     # dernier appel MCP (prise en main : « LLM connecté »), mis à jour au plus 1×/h
        "session_epoch": "INTEGER DEFAULT 0",  # +1 = toutes les sessions du compte sont fermées
        "email_verified_at": "TEXT",           # adresse prouvée (code e-mail, lien admin) ; NULL = pas encore
        "telegram_trusted": "INTEGER",         # 1 = Telegram relié après preuve de l'adresse : peut recevoir les codes
        "usage_optout": "INTEGER",  # 1 = activité exclue des statistiques d'usage (droit d'opposition)
        "profil": "TEXT",          # profil déclaré, facultatif (usage.PROFILES) : statistiques d'usage par public
    },
    "login_codes": {
        "fail_log": "TEXT",        # JSON : horodatages des codes faux (plafond par compte et par 24 h)
    },
}


def _migrate(conn: sqlite3.Connection) -> None:
    from . import doublons

    doublons.ensure_schema(conn)  # documents.duplicate_of, doc_sketch
    for table, cols in _COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()
    # Une fois (rejouable tant que le drapeau n'est pas posé) : les comptes d'avant la vérification d'adresse sont
    # considérés comme prouvés, et leur Telegram déjà relié reste digne de recevoir les codes (rien ne change pour eux).
    flag = conn.execute("SELECT value FROM meta WHERE key='auth_backfill_v1'").fetchone()
    if not flag:
        with conn:
            conn.execute("UPDATE users SET email_verified_at=COALESCE(created_at, 'avant') "
                         "WHERE email IS NOT NULL AND email_verified_at IS NULL")
            conn.execute("UPDATE users SET telegram_trusted=1 WHERE telegram_chat_id IS NOT NULL")
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('auth_backfill_v1', ?)", (now(),))


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    with _schema_lock:
        if str(path) not in _initialized:
            conn.executescript(SCHEMA)
            _migrate(conn)
            _initialized.add(str(path))
    return conn


def get() -> sqlite3.Connection:
    """Connexion propre au thread courant."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = connect()
        _local.conn = conn
    return conn


def meta_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def log(conn: sqlite3.Connection, source_id: str | None, url: str | None, outcome: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO fetch_log(ts,source_id,url,outcome,detail) VALUES(?,?,?,?,?)",
        (now(), source_id, url, outcome, detail[:500]),
    )
    conn.commit()


def jdump(value: Iterable[Any] | None) -> str | None:
    if not value:
        return None
    return json.dumps(list(value), ensure_ascii=False)


def jload(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        return json.loads(value)
    except ValueError:
        return []
