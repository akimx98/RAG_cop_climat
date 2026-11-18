"""Récapitulatif Telegram du matin et du soir, centré sur les sujets de chaque utilisateur, SANS IA.

Pour chaque dossier suivi : les textes de négociation apparus depuis le dernier récapitulatif (étape : projet,
proposé pour adoption, adopté ; crochets ; ce qui a changé), et les comptes rendus ou lettres d'information qui en
parlent. Pour chaque sujet par mots-clés : le nombre de documents signalés et les premiers titres. Rien de neuf → pas
de message. En fin de message, une question toute prête à poser à son LLM connecté, qui fera la synthèse.

Confidentialité : les documents internes ne montrent ni titre ni extrait (Telegram n'est pas chiffré de bout en bout).
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, db, i18n, topics

MAX_LEN = 3900


def _since(conn, user_id: int, now: datetime) -> str:
    last = db.meta_get(conn, f"digest_last:{user_id}")
    floor = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return max(last, floor) if last else (now - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")


def render(conn, user: dict, since: str, now: datetime | None = None) -> str | None:
    """Texte HTML du récapitulatif, ou None s'il n'y a rien de neuf sur les sujets de l'utilisateur."""
    lang = i18n.default_for(user)
    en = lang == "en"
    T = lambda fr, e: e if en else fr  # noqa: E731
    news = topics.whats_new(conn, user, since, lang)
    if news["aucun_sujet"]:
        return None
    blocks: list[str] = []
    seen_reports: set[str] = set()  # un bulletin qui parle de tout (ENB) n'apparaît qu'une fois, sous le premier sujet
    for item in news["sujets"]:
        lines: list[str] = []
        if item["type"] == "dossier":
            for t in item["textes"]:
                ref = " ".join(x for x in (t.get("cote"), (t.get("titre") or "")[:110]) if x)
                extra = " · ".join(x for x in (
                    f"{t['crochets']} {T('crochets', 'brackets')}" if t.get("crochets") is not None else "",
                    t.get("changements") or "") if x)
                lines.append(f"{t['etape_libelle']} {html.escape(ref)}" + (f"\n   {html.escape(extra)}" if extra else "")
                             + f"\n   {html.escape(t.get('lien_diff') or t.get('lien') or '')}")
            for r in [r for r in item["comptes_rendus"] if (r.get("titre") or r.get("lien")) not in seen_reports][:1]:
                seen_reports.add(r.get("titre") or r.get("lien"))
                lines.append(f"📰 {html.escape((r.get('titre') or '')[:100])}\n   {html.escape(r.get('lien') or '')}")
        else:
            for d in item["documents"][:3]:
                if d.get("niveau") == "R" and not config.TELEGRAM_RESTRICTED_SNIPPETS:
                    lines.append(f"🔒 {T('Document interne', 'Internal document')} · {d['lien']}")
                else:
                    lines.append(f"• {html.escape((d.get('titre') or '')[:110])} · {d['lien']}")
            if item["nb_documents"] > 3:
                lines.append(T(f"… et {item['nb_documents'] - 3} autres", f"… and {item['nb_documents'] - 3} more"))
        if lines:
            blocks.append(f"<b>{html.escape(item['nom'])}</b>\n" + "\n".join(lines))
    if not blocks:
        return None
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(config.TIMEZONE))
    head = (T("☀️ <b>Récapitulatif du matin</b>", "☀️ <b>Morning digest</b>") if local.hour < 14
            else T("🌙 <b>Récapitulatif du soir</b>", "🌙 <b>Evening digest</b>"))
    head += T(f" · vos sujets depuis {since[11:16]} UTC", f" · your topics since {since[11:16]} UTC")
    quiet = news["sans_nouveaute"]
    tail = []
    if quiet:
        tail.append(T("Rien de neuf : ", "Nothing new: ") + html.escape(", ".join(quiet[:8])))
    tail.append(T("💬 Pour la synthèse, demandez à votre LLM connecté : <i>« Comment ont évolué les textes sur mes "
                  "sujets depuis " + since[11:16] + " UTC ? Qui a bougé ? »</i>",
                  "💬 For the analysis, ask your connected LLM: <i>“How have the texts on my topics changed since "
                  + since[11:16] + " UTC? Who moved?”</i>"))
    tail.append(T("Connecter un LLM : ", "Connect an LLM: ") + f"{config.PUBLIC_BASE}/connect · "
                + T("Mes sujets : ", "My topics: ") + f"{config.PUBLIC_BASE}/topics")
    body = head + "\n\n" + "\n\n".join(blocks)
    foot = "\n\n" + "\n".join(tail)
    if len(body) + len(foot) > MAX_LEN:
        body = body[: MAX_LEN - len(foot) - 2].rsplit("\n", 1)[0] + "\n…"
    return body + foot


def due_slot(conn, now: datetime | None = None) -> str | None:
    """Créneau à envoyer (« 2026-11-18T07 ») si l'heure locale a passé un des créneaux et qu'il n'a pas été envoyé."""
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(config.TIMEZONE))
    passed = [h for h in config.DIGEST_HOURS if local.hour >= h]
    if not passed:
        return None
    slot = f"{local.strftime('%Y-%m-%d')}T{max(passed):02d}"
    return None if db.meta_get(conn, "digest_slot") == slot else slot


def run(conn, send, now: datetime | None = None) -> int:
    """Envoie le récapitulatif aux utilisateurs reliés à Telegram (au plus une fois par créneau)."""
    if not config.TELEGRAM_TOKEN:
        return 0
    now = now or datetime.now(timezone.utc)
    slot = due_slot(conn, now)
    if not slot:
        return 0
    db.meta_set(conn, "digest_slot", slot)  # marqué d'abord : pas de double envoi en cas d'erreur
    sent = 0
    for u in conn.execute("SELECT * FROM users WHERE active=1 AND telegram_chat_id IS NOT NULL").fetchall():
        user = dict(u)
        since = _since(conn, user["id"], now)
        try:
            text = render(conn, user, since, now)
        except Exception as exc:  # un utilisateur en erreur ne bloque pas les autres
            print(f"[récapitulatif] {user['id']}: {exc}")
            continue
        db.meta_set(conn, f"digest_last:{user['id']}", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        if text and send(user["telegram_chat_id"], text):
            sent += 1
    return sent
