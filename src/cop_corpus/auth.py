"""Utilisateurs (collègues accrédités), jetons personnels, cookies signés.

Un utilisateur reçoit UN jeton secret, qui sert à la fois :
- de lien de connexion au tableau de bord (`/login/<jeton>`) ;
- d'adresse personnelle du connecteur MCP (`/u/<jeton>/mcp`) ;
- de jeton Bearer pour l'API.
Seule l'empreinte SHA-256 du jeton sert à l'authentification ; une copie chiffrée
(clé dérivée de COP_SECRET) permet de réafficher l'adresse MCP sur la page Connecteur.
Révoquer = régénérer.

On peut aussi se connecter par e-mail + mot de passe (scrypt). Mot de passe perdu :
l'administrateur génère un lien de connexion signé valable 24 h, qui ne touche pas au jeton MCP
et devient caduc dès que le mot de passe est changé.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
import time
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from . import config, db

PASSWORD_MIN = 10


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _fernet() -> Fernet:
    key = hashlib.sha256(f"token-enc:{config.SECRET}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _enc(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def mcp_token(user: dict | None) -> str | None:
    """Jeton en clair de l'utilisateur, pour réafficher son adresse MCP (None si inconnu)."""
    if not user or not user.get("token_enc"):
        return None
    try:
        return _fernet().decrypt(user["token_enc"].encode()).decode()
    except InvalidToken:
        return None


def can_restricted(user: dict | None) -> bool:
    return bool(user and user.get("can_restricted") and user.get("active", 1))


def can_contribute(user: dict | None) -> bool:
    """Ajouter des documents au corpus commun, écrire des notes, lancer un brief IA :
    administrateurs et contributeurs, jamais les invités."""
    return bool(user and user.get("active", 1) and (user.get("is_admin") or user.get("can_restricted")))


def is_guest(user: dict | None) -> bool:
    return bool(user) and not can_contribute(user)


def create_user(conn: sqlite3.Connection, email: str, name: str = "", admin: bool = False,
                restricted: bool = True) -> tuple[int, str]:
    token = secrets.token_urlsafe(24)
    code = secrets.token_hex(4)
    cur = conn.execute(
        """INSERT INTO users(email,name,token_hash,token_enc,is_admin,can_restricted,telegram_code,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (email.lower().strip(), name, _hash(token), _enc(token), int(admin), int(restricted), code, db.now()),
    )
    conn.commit()
    return cur.lastrowid, token


def reset_token(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(24)
    conn.execute("UPDATE users SET token_hash=?, token_enc=? WHERE id=?", (_hash(token), _enc(token), user_id))
    conn.commit()
    return token


def by_token(conn: sqlite3.Connection, token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    row = conn.execute("SELECT * FROM users WHERE token_hash=? AND active=1", (_hash(token),)).fetchone()
    if not row:
        return None
    user = dict(row)
    if not user.get("token_enc"):  # comptes créés avant le chiffrement : on mémorise au passage
        user["token_enc"] = _enc(token)
        conn.execute("UPDATE users SET token_enc=? WHERE id=?", (user["token_enc"], user["id"]))
        conn.commit()
    return user


def by_id(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
    return dict(row) if row else None


def by_email(conn: sqlite3.Connection, email: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE email=? AND active=1", (email.lower().strip(),)).fetchone()
    return dict(row) if row else None


# --- mots de passe -------------------------------------------------------------------

def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    salt = secrets.token_bytes(16)
    value = f"scrypt${salt.hex()}${_scrypt(password, salt).hex()}"
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (value, user_id))
    conn.commit()


_DUMMY_SALT = secrets.token_bytes(16)
_failures: dict[str, list[float]] = {}
MAX_FAILURES, WINDOW = 8, 900


def throttled(key: str) -> bool:
    now = time.time()
    recent = [t for t in _failures.get(key, []) if now - t < WINDOW]
    _failures[key] = recent
    return len(recent) >= MAX_FAILURES


def check_password(conn: sqlite3.Connection, email: str, password: str, client: str = "") -> dict[str, Any] | None:
    """Utilisateur si e-mail + mot de passe corrects, sinon None (temps constant, essais limités
    par couple e-mail × adresse IP, pour qu'un tiers ne puisse pas bloquer le compte d'un collègue)."""
    key = f"{email.lower().strip()}|{client}"
    if throttled(key):
        return None
    user = by_email(conn, email)
    stored = (user or {}).get("password_hash") or ""
    try:
        _, salt_hex, digest_hex = stored.split("$")
        salt, digest = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
    except ValueError:
        salt, digest = _DUMMY_SALT, b""
    ok = hmac.compare_digest(_scrypt(password, salt), digest) and bool(digest)
    if ok:
        _failures.pop(key, None)
        return user
    _failures.setdefault(key, []).append(time.time())
    return None


# --- cookies et liens signés ------------------------------------------------------

def _sign(payload: str) -> str:
    return hmac.new(config.SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_cookie(user_id: int, days: int = 30) -> str:
    exp = int(time.time()) + days * 86400
    payload = f"{user_id}.{exp}"
    return base64.urlsafe_b64encode(f"{payload}.{_sign(payload)}".encode()).decode()


def read_cookie(value: str | None) -> int | None:
    if not value:
        return None
    try:
        uid, exp, sig = base64.urlsafe_b64decode(value.encode()).decode().split(".")
    except Exception:
        return None
    if not hmac.compare_digest(_sign(f"{uid}.{exp}"), sig) or int(exp) < time.time():
        return None
    return int(uid)


def _login_sig(user: dict, exp: int) -> str:
    # le mot de passe actuel entre dans la signature : le lien devient caduc dès qu'il change
    return _sign(f"login:{user['id']}:{exp}:{user.get('password_hash') or ''}")


def login_link(user: dict, hours: int = 24) -> str:
    exp = int(time.time()) + hours * 3600
    return f"{config.PUBLIC_BASE}/entrer/{user['id']}.{exp}.{_login_sig(user, exp)}"


def by_login_link(conn: sqlite3.Connection, value: str) -> dict[str, Any] | None:
    try:
        uid, exp, sig = value.split(".")
        e = int(exp)
        user = by_id(conn, int(uid))
    except ValueError:
        return None
    if not user or e < time.time() or not hmac.compare_digest(_login_sig(user, e), sig):
        return None
    return user


def file_signature(doc_id: int, exp: int) -> str:
    return _sign(f"file:{doc_id}:{exp}")


def signed_file_url(doc_id: int, hours: int = 72) -> str:
    exp = int(time.time()) + hours * 3600
    return f"{config.PUBLIC_BASE}/fichier/{doc_id}?exp={exp}&sig={file_signature(doc_id, exp)}"


def check_file_signature(doc_id: int, exp: str | None, sig: str | None) -> bool:
    if not exp or not sig:
        return False
    try:
        e = int(exp)
    except ValueError:
        return False
    return e >= time.time() and hmac.compare_digest(file_signature(doc_id, e), sig)


# --- invitations (comptes invités en libre-service) ---------------------------------
#
# Un seul code d'invitation, partagé de vive voix (lien court /rejoindre/<code>). Il crée des comptes
# « invité » : documents publics seulement, aucun ajout au corpus. L'administrateur peut fermer les
# inscriptions, régénérer le code (l'ancien cesse de marcher) et plafonner le nombre de comptes.

INVITE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # sans 0/o, 1/l/i : se lit et se tape sur un téléphone
INVITE_MAX_DEFAULT = 60
_EMAIL = __import__("re").compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def invite_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    code = db.meta_get(conn, "invite_code")
    if not code:
        code = new_invite_code(conn)
    used = conn.execute("SELECT COUNT(*) FROM users WHERE source='invitation'").fetchone()[0]
    return {"code": code, "open": db.meta_get(conn, "invite_open", "1") == "1",
            "max": int(db.meta_get(conn, "invite_max", str(INVITE_MAX_DEFAULT)) or INVITE_MAX_DEFAULT),
            "used": used, "url": f"{config.PUBLIC_BASE}/join/{code}"}


def new_invite_code(conn: sqlite3.Connection) -> str:
    code = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(7))
    db.meta_set(conn, "invite_code", code)
    return code


def set_invites(conn: sqlite3.Connection, open_: bool | None = None, maximum: int | None = None) -> None:
    if open_ is not None:
        db.meta_set(conn, "invite_open", "1" if open_ else "0")
    if maximum is not None:
        db.meta_set(conn, "invite_max", str(max(0, maximum)))


def invite_valid(conn: sqlite3.Connection, code: str | None) -> bool:
    s = invite_settings(conn)
    return bool(code) and s["open"] and hmac.compare_digest(code.strip().lower(), s["code"])


SIGNUP_ERRORS = {  # code → (français, anglais)
    "throttled": ("Trop d'essais : réessayez dans 15 minutes.", "Too many attempts: try again in 15 minutes."),
    "bad_code": ("Ce lien d'invitation n'est pas (ou plus) valable : vous pouvez créer un compte avec un mot de passe.",
                 "This invitation link is not (or no longer) valid: you can still create an account with a password."),
    "full": ("Toutes les places de cette invitation sont prises : demandez à la personne qui vous a invité.",
             "All places for this invitation are taken: ask the person who invited you."),
    "closed": ("Les inscriptions sont fermées pour le moment.", "Sign-ups are closed for now."),
    "bad_email": ("Adresse e-mail invalide.", "Invalid e-mail address."),
    "short_password": (f"Mot de passe : au moins {PASSWORD_MIN} caractères.", f"Password: at least {PASSWORD_MIN} characters."),
    "exists": ("Un compte existe déjà avec cette adresse : connectez-vous.", "An account already exists for this address: sign in."),
    "mismatch": ("Les deux mots de passe diffèrent.", "The two passwords do not match."),
}
GUEST_SOURCES = ("invitation", "inscription")   # invitation : lien ou QR ; inscription : libre, avec mot de passe
OPEN_MAX_DEFAULT = 200


def open_signup_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    used = conn.execute("SELECT COUNT(*) FROM users WHERE source='inscription'").fetchone()[0]
    return {"open": db.meta_get(conn, "signup_open", "1") == "1", "used": used,
            "max": int(db.meta_get(conn, "signup_max", str(OPEN_MAX_DEFAULT)) or OPEN_MAX_DEFAULT)}


def set_open_signup(conn: sqlite3.Connection, open_: bool | None = None, maximum: int | None = None) -> None:
    if open_ is not None:
        db.meta_set(conn, "signup_open", "1" if open_ else "0")
    if maximum is not None:
        db.meta_set(conn, "signup_max", str(max(0, maximum)))


def signup(conn: sqlite3.Connection, code: str, email: str, organisation: str = "", password: str = "",
           client: str = "", lang: str | None = None) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Compte invité → (utilisateur, jeton MCP, code d'erreur de SIGNUP_ERRORS).

    - avec un code d'invitation valable : e-mail (+ organisation), rien d'autre, connexion immédiate ;
    - sans code : inscription libre, mot de passe obligatoire (personne ne s'est porté garant de l'adresse).
    Mauvais codes et inscriptions libres sont limités par adresse IP, avec un seuil large (Wi-Fi partagé à la COP)."""
    key = f"signup|{client}"
    if throttled(key):
        return None, None, "throttled"
    invited = bool(code)
    if invited:
        if not invite_valid(conn, code):
            _failures.setdefault(key, []).append(time.time())
            return None, None, "bad_code"
        s = invite_settings(conn)
        if s["used"] >= s["max"]:
            return None, None, "full"
    else:
        s = open_signup_settings(conn)
        if not s["open"] or s["used"] >= s["max"]:
            return None, None, "closed"
    email = (email or "").strip().lower()
    organisation = (organisation or "").strip()[:120]
    if not _EMAIL.match(email):
        return None, None, "bad_email"
    if not invited and len(password or "") < PASSWORD_MIN:
        return None, None, "short_password"
    if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        return None, None, "exists"
    if not invited:  # au plus ~ MAX_FAILURES inscriptions libres par IP et par quart d'heure
        _failures.setdefault(key, []).append(time.time())
    uid, token = create_user(conn, email, "", admin=False, restricted=False)
    conn.execute("UPDATE users SET organisation=?, source=?, lang=? WHERE id=?",
                 (organisation or None, "invitation" if invited else "inscription",
                  lang if lang in ("en", "fr") else None, uid))
    conn.commit()
    if not invited:
        set_password(conn, uid, password)
    return by_id(conn, uid), token, None


def delete_guest(conn: sqlite3.Connection, user_id: int) -> bool:
    """Supprime définitivement un compte invité (invitation ou inscription libre, jamais un admin ni un
    contributeur) et ce qui lui appartient : sujets, alertes, dossiers suivis. Libère sa place d'invitation."""
    row = conn.execute("SELECT source, is_admin, can_restricted FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or row["source"] not in GUEST_SOURCES or row["is_admin"] or row["can_restricted"]:
        return False
    with conn:
        conn.execute("DELETE FROM notifications WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
        for table in ("dossier_follows", "field_notes"):
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
        conn.execute("UPDATE documents SET uploaded_by=NULL WHERE uploaded_by=?", (user_id,))
        conn.execute("UPDATE mail_inbox SET user_id=NULL WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    return True


# --- connexion par code envoyé par e-mail (comptes sans mot de passe) ---------------

CODE_MINUTES, CODE_TRIES, CODES_PER_HOUR = 15, 5, 5


def _code_hash(user_id: int, code: str) -> str:
    return _sign(f"login-code:{user_id}:{code}")


def issue_login_code(conn: sqlite3.Connection, user: dict) -> tuple[str | None, str | None]:
    """Nouveau code à 6 chiffres → (code, None), ou (None, 'wait') si trop d'envois récents
    (une minute entre deux codes, cinq par heure)."""
    import json

    now = time.time()
    row = conn.execute("SELECT sent_at FROM login_codes WHERE user_id=?", (user["id"],)).fetchone()
    sent = [t for t in json.loads(row["sent_at"] or "[]") if now - t < 3600] if row else []
    if sent and (now - sent[-1] < 60 or len(sent) >= CODES_PER_HOUR):
        return None, "wait"
    code = f"{secrets.randbelow(10**6):06d}"
    conn.execute(
        """INSERT INTO login_codes(user_id, code_hash, expires, tries, sent_at) VALUES(?,?,?,0,?)
           ON CONFLICT(user_id) DO UPDATE SET code_hash=excluded.code_hash, expires=excluded.expires, tries=0,
           sent_at=excluded.sent_at""",
        (user["id"], _code_hash(user["id"], code), now + CODE_MINUTES * 60, json.dumps([*sent, now])))
    conn.commit()
    return code, None


def check_login_code(conn: sqlite3.Connection, user: dict | None, code: str) -> bool:
    """Vrai si le code est le bon, pas expiré, et moins de 5 essais (le code est alors consommé)."""
    if not user:
        return False
    row = conn.execute("SELECT * FROM login_codes WHERE user_id=?", (user["id"],)).fetchone()
    if not row or not row["code_hash"] or row["expires"] < time.time() or row["tries"] >= CODE_TRIES:
        return False
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if hmac.compare_digest(_code_hash(user["id"], code), row["code_hash"]):
        conn.execute("UPDATE login_codes SET code_hash=NULL WHERE user_id=?", (user["id"],))
        conn.commit()
        return True
    conn.execute("UPDATE login_codes SET tries=tries+1 WHERE user_id=?", (user["id"],))
    conn.commit()
    return False
