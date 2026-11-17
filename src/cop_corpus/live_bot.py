"""Commandes Telegram de la veille en direct : dossiers, textes, notes de terrain, briefs."""

from __future__ import annotations

import html

from . import briefs, config, live

HELP = (
    "\n<b>Négociations en direct</b>\n"
    "/dossiers — les dossiers et leur état (✓ = suivis)\n"
    "/dossier GGA — état, derniers textes, dernier brief d'un dossier\n"
    "/suivi GGA — suivre / ne plus suivre un dossier (alertes textes + briefs 7 h et 19 h)\n"
    "/textes — derniers textes de négociation annoncés (⏳ = à récupérer)\n"
    "/note GGA L'Arabie saoudite bloque le §12… — note de terrain (interne, rattachée au dossier)\n"
    "/brief — derniers briefs de mes dossiers\n"
    "<i>Un texte ⏳ : ouvrez le lien dans votre navigateur et transférez le PDF ici.</i>"
)
HELP_GUEST = (
    "\n<b>Négociations en direct</b>\n"
    "/dossiers — les dossiers et leur état (✓ = suivis)\n"
    "/dossier GGA — état, derniers textes, dernier brief d'un dossier\n"
    "/suivi GGA — suivre / ne plus suivre un dossier (alertes textes + briefs 7 h et 19 h)\n"
    "/textes — derniers textes de négociation annoncés\n"
    "/brief — derniers briefs de mes dossiers"
)
HELP_EN = (
    "\n<b>Live negotiations</b>\n"
    "/negotiations — negotiation items and their status (✓ = followed)\n"
    "/negotiation GGA — status, latest texts, latest brief of an item\n"
    "/track GGA — follow / unfollow an item (new-text alerts + 7am and 7pm briefs)\n"
    "/texts — latest negotiation texts announced\n"
    "/brief — latest briefs of my items"
)
def _no_briefs(text: str) -> str:
    """Textes d'aide sans les briefs quand ils sont désactivés (COP_BRIEFS=0)."""
    text = text.replace(" (alertes textes + briefs 7 h et 19 h)", " (alerte à chaque nouveau texte)")
    text = text.replace(" (new-text alerts + 7am and 7pm briefs)", " (an alert for each new text)")
    text = text.replace(", dernier brief d'un dossier", " d'un dossier").replace(", latest brief of an item", " of an item")
    return "\n".join(l for l in text.split("\n") if not l.startswith("/brief"))


COMMANDS_EN = [
    {"command": "negotiations", "description": "negotiation items"},
    {"command": "negotiation", "description": "one item (e.g. /negotiation GGA)"},
    {"command": "track", "description": "follow an item (e.g. /track finance)"},
    {"command": "texts", "description": "latest negotiation texts"},
    {"command": "brief", "description": "briefs of my items"},
]
COMMANDS = [
    {"command": "dossiers", "description": "les dossiers de négociation"},
    {"command": "dossier", "description": "état d'un dossier (ex. /dossier GGA)"},
    {"command": "suivi", "description": "suivre un dossier (ex. /suivi finance)"},
    {"command": "textes", "description": "derniers textes de négociation"},
    {"command": "note", "description": "note de terrain (ex. /note GGA …)"},
    {"command": "brief", "description": "briefs de mes dossiers"},
]

if not config.BRIEFS:  # briefs désactivés : ni commande /brief ni mention dans l'aide
    HELP, HELP_GUEST, HELP_EN = (_no_briefs(HELP), _no_briefs(HELP_GUEST), _no_briefs(HELP_EN))
    COMMANDS = [c for c in COMMANDS if c["command"] != "brief"]
    COMMANDS_EN = [c for c in COMMANDS_EN if c["command"] != "brief"]


def _slug(arg: str) -> str | None:
    first = (arg or "").split(" ", 1)[0]
    return live.dossier_from_alias(first) if first else None


def _name(d: dict, lang: str, key: str = "nom") -> str:
    return (d.get(f"{key}_en") or d.get(key)) if lang == "en" else d.get(key)


def _text_line(t: dict, lang: str = "fr") -> str:
    counts = ", ".join(x for x in (f"{t['brackets']} {'brackets' if lang == 'en' else 'crochets'}" if t["brackets"] is not None else "",
                                   f"{t['options']} options" if t["options"] else "") if x)
    head = f"{'✅' if t['doc_id'] else '⏳'} {html.escape(t['symbol'] or '')} {html.escape((t['title'] or '')[:110])}"
    link = f"{config.PUBLIC_BASE}/doc/{t['doc_id']}" if t["doc_id"] else (t["pdf_url"] or "")
    return f"{head}\n   {html.escape((t['published'] or '')[:16].replace('T', ' '))} · {counts or '—'} · {link}"


def handle(conn, chat_id, user: dict, cmd: str, arg: str, send) -> bool:
    """Traite une commande de la veille en direct ; renvoie False si elle n'en est pas une."""
    from . import i18n

    live.ensure_schema(conn)
    lang = i18n.default_for(user)

    def T(fr: str, en: str) -> str:
        return i18n.pick(lang, fr, en)

    if cmd in ("/note", "/n") and not user.get("can_restricted"):
        send(chat_id, T("Les notes de terrain sont réservées aux contributeurs.", "Field notes are for contributors only."))
        return True
    if cmd in ("/note", "/n"):
        if not arg:
            send(chat_id, "Exemple : <code>/note GGA L'Arabie saoudite refuse les indicateurs de moyens de mise en "
                          "œuvre</code>\nAstuce : dictez avec le micro du clavier.")
            return True
        _nid, slug = live.add_note(conn, user, arg, via="telegram")
        d = live.dossier(slug) if slug else None
        send(chat_id, f"🔒 Note enregistrée (interne){' — dossier ' + html.escape(d['nom']) if d else ' — sans dossier'}."
                      f"\n{config.PUBLIC_BASE}/dossiers/{slug or ''}")
        return True
    if cmd == "/dossiers":
        rows = live.overview(conn, user)
        lines = [T("<b>Dossiers</b> (✓ suivis) — /dossier ALIAS pour le détail",
                   "<b>Negotiations</b> (✓ followed) — /negotiation ALIAS for details, /track ALIAS to follow")]
        for d in rows:
            etat = ", ".join(d["etat"]) or "—"
            lines.append(f"{'✓' if d['suivi'] else '·'} <b>{html.escape(_name(d, lang))}</b> [{d['alias'][0]}] — {html.escape(etat)}"
                         f" · {d['nb_textes']} {T('textes', 'texts')}{' (' + str(d['a_recuperer']) + ' ⏳)' if d['a_recuperer'] else ''}")
        send(chat_id, "\n".join(lines))
        return True
    if cmd == "/dossier":
        slug = _slug(arg)
        if not slug:
            send(chat_id, T("Dossier inconnu. /dossiers donne la liste et les alias (ex. GGA, finance, MWP, art6).",
                            "Unknown item. /negotiations lists them with their aliases (e.g. GGA, finance, MWP, art6, ACE)."))
            return True
        d = live.dossier(slug)
        etat = ", ".join(x["status"] for x in live.status_by_dossier(conn).get(slug, [])) or T("non renseigné", "unknown")
        lines = [f"🗂 <b>{html.escape(_name(d, lang))}</b>", f"<i>{html.escape(_name(d, lang, 'enjeu'))}</i>",
                 f"{T('État', 'Status')} ({live.CB_CREDIT}) : {html.escape(etat)}"]
        ts = live.texts(conn, slug, limit=5)
        if ts:
            lines.append(T("<b>Derniers textes</b>", "<b>Latest texts</b>"))
            lines += [_text_line(t, lang) for t in ts]
        b = briefs.latest(conn, slug)
        if b:
            lines.append(f"<b>{T('Brief du', 'Brief of')} {b['created_at'][:16].replace('T', ' ')} UTC</b>"
                         + T("", " (in French)") + "\n"
                         + html.escape(briefs.clean(b["body"].get("essentiel", ""))))
        suivi = slug in live.followed(conn, user["id"])
        lines.append(f"{T('✓ Suivi', '✓ Followed') if suivi else T('Non suivi', 'Not followed')} — "
                     f"{T('/suivi', '/track')} {d['alias'][0]} · {config.PUBLIC_BASE}/dossiers/{slug}")
        send(chat_id, "\n".join(lines))
        return True
    if cmd == "/suivi":
        slug = _slug(arg)
        if not slug:
            send(chat_id, T("Exemple : <code>/suivi finance</code> (liste : /dossiers)", "Example: <code>/track finance</code> (list: /negotiations)"))
            return True
        on = slug not in live.followed(conn, user["id"])
        live.follow(conn, user["id"], slug, on)
        d = live.dossier(slug)
        name = html.escape(_name(d, lang))
        send(chat_id, (T(f"✓ Vous suivez « {name} » : alerte à chaque nouveau texte"
                         + (", brief à 7 h et 19 h." if config.BRIEFS else "."),
                         f"✓ You now follow “{name}”: an alert for each new text"
                         + (", a brief at 7am and 7pm." if config.BRIEFS else "."))
                       if on else T(f"Vous ne suivez plus « {name} ».", f"You no longer follow “{name}”.")))
        return True
    if cmd == "/textes":
        slug = _slug(arg)
        ts = live.texts(conn, slug, limit=12)
        if not ts:
            send(chat_id, T("Aucun texte annoncé pour l'instant.", "No text announced yet."))
            return True
        send(chat_id, T("<b>Derniers textes de négociation</b>\n", "<b>Latest negotiation texts</b>\n") + "\n".join(_text_line(t, lang) for t in ts)
             + f"\n<i>Index : {live.CB_CREDIT}</i>")
        return True
    if cmd == "/brief":
        if not config.BRIEFS:
            send(chat_id, T("Les briefs par dossier sont désactivés : /dossier GGA donne l'état et les derniers textes.",
                            "Item briefs are turned off: /negotiation GGA gives the status and latest texts."))
            return True
        slugs = [s for s in ([_slug(arg)] if arg else sorted(live.followed(conn, user["id"]))) if s]
        if not slugs:
            send(chat_id, T("Suivez d'abord un dossier (/suivi finance) ou précisez-en un : /brief GGA",
                            "Follow an item first (/track finance) or name one: /brief GGA"))
            return True
        sent = 0
        for slug in slugs[:6]:
            b = briefs.latest(conn, slug)
            if b:
                notes = live.notes(conn, slug, since=b["period_start"]) if user.get("can_restricted") else None
                send(chat_id, briefs.render_telegram(b, live.dossier(slug), notes, lang=lang))
                sent += 1
        if not sent:
            send(chat_id, T("Pas encore de brief pour ces dossiers (prochains à 7 h et 19 h, heure d'Antalya).",
                            "No brief yet for these items (next at 7am and 7pm, Antalya time)."))
        return True
    return False
