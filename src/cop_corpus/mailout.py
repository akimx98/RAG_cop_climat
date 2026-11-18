"""Envoi d'e-mails (codes de connexion) par un service transactionnel gratuit : Resend ou Brevo.

Réglages (deploy/.env) : COP_MAIL_PROVIDER=resend|brevo, COP_MAIL_API_KEY=…, et facultativement
COP_MAIL_FROM="COP31 tracker <noreply@cop31.akimx.fr>" (domaine à vérifier chez le service).
Sans réglage, rien n'est envoyé et l'appelant le signale à l'utilisateur.
"""

from __future__ import annotations

from email.utils import parseaddr

import httpx

from . import config

PROVIDER = config.MAIL_PROVIDER
API_KEY = config.MAIL_API_KEY
FROM = config.MAIL_FROM


def enabled() -> bool:
    return PROVIDER in ("resend", "brevo") and bool(API_KEY)


def send(to: str, subject: str, text: str, idempotency_key: str | None = None, reply_to: str | None = None) -> bool:
    """Envoie un e-mail texte ; vrai si le service l'a accepté. Trois essais sur erreur réseau, 5xx ou 429
    (en respectant Retry-After) ; une clé d'idempotence évite un double envoi si un essai a abouti sans réponse."""
    import logging
    import time

    if not enabled():
        return False
    if PROVIDER == "resend":
        url, headers = "https://api.resend.com/emails", {"Authorization": f"Bearer {API_KEY}"}
        body = {"from": FROM, "to": [to], "subject": subject, "text": text}
        if reply_to:
            body["reply_to"] = reply_to
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
    else:
        name, addr = parseaddr(FROM)
        url, headers = "https://api.brevo.com/v3/smtp/email", {"api-key": API_KEY, "accept": "application/json"}
        body = {"sender": {"name": name or "COP31 tracker", "email": addr}, "to": [{"email": to}],
                "subject": subject, "textContent": text}
        if reply_to:
            body["replyTo"] = {"email": reply_to}
    for attempt in range(3):
        try:
            r = httpx.post(url, headers=headers, json=body, timeout=15)
        except httpx.HTTPError as exc:
            logging.warning("mailout: erreur réseau (%s), essai %d", exc, attempt + 1)
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code < 300:
            try:
                logging.info("mailout: envoyé, id %s", r.json().get("id") or r.json().get("messageId"))
            except ValueError:
                pass
            return True
        if r.status_code == 429 or r.status_code >= 500:
            wait = r.headers.get("retry-after", "")
            time.sleep(min(float(wait), 10) if wait.replace(".", "", 1).isdigit() else 2 * (attempt + 1))
            continue
        logging.warning("mailout: refusé %s %s", r.status_code, r.text[:300])
        return False
    return False
