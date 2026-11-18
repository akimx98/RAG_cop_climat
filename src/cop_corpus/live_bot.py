"""Commandes Telegram de la veille en direct : dossiers, textes, récapitulatif de mes sujets (sans IA)."""

from __future__ import annotations

import html

from . import config, live


def _slug(arg: str) -> str | None:
    first = (arg or "").split(" ", 1)[0]
    return live.dossier_from_alias(first) if first else None


def _name(d: dict, lang: str, key: str = "nom") -> str:
    return (d.get(f"{key}_en") or d.get(key)) if lang == "en" else d.get(key)


def _text_line(t: dict, lang: str = "fr") -> str:
    counts = ", ".join(x for x in (f"{t['brackets']} {'brackets' if lang == 'en' else 'crochets'}" if t["brackets"] is not None else "",
                                   f"{t['options']} options" if t["options"] else "") if x)
    head = f"{live.stage_label(live.text_stage(dict(t)), lang)} {html.escape(t['symbol'] or '')} {html.escape((t['title'] or '')[:110])}"
    link = f"{config.PUBLIC_BASE}/doc/{t['doc_id']}" if t["doc_id"] else (t["pdf_url"] or "")
    return f"{head}\n   {html.escape((t['published'] or '')[:16].replace('T', ' '))} · {counts or '—'} · {link}"


def handle(conn, chat_id, user: dict, cmd: str, arg: str, send) -> bool:
    """Traite une commande de la veille en direct ; renvoie False si elle n'en est pas une."""
    from . import i18n

    live.ensure_schema(conn)
    lang = i18n.default_for(user)

    def T(fr: str, en: str) -> str:
        return i18n.pick(lang, fr, en)

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
        suivi = slug in live.followed(conn, user["id"])
        lines.append(f"{T('✓ Suivi', '✓ Followed') if suivi else T('Non suivi', 'Not followed')} — "
                     f"{T('/suivi', '/track')} {d['alias'][0]} · {config.PUBLIC_BASE}/database/negotiations/{slug}")
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
        send(chat_id, T(f"✓ Vous suivez « {name} » : alerte à chaque nouveau texte (projet, proposé pour adoption, "
                          "adopté), et ce dossier entre dans votre récapitulatif de 7 h et 19 h.",
                          f"✓ You now follow “{name}”: an alert for each new text (draft, forwarded for adoption, "
                          "adopted), and this item is in your 7am and 7pm digest.")
                      if on else T(f"Vous ne suivez plus « {name} ».", f"You no longer follow “{name}”."))
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
        from datetime import datetime, timedelta, timezone

        from . import digest

        since = (datetime.now(timezone.utc) - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
        text = digest.render(conn, user, since)
        send(chat_id, text or T("Rien de neuf sur vos sujets depuis 12 h. Suivez un dossier (/suivi finance) ou un sujet "
                                "(/suivre mots-clés), ou gérez-les sur " + config.PUBLIC_BASE + "/topics",
                                "Nothing new on your topics in the last 12 hours. Follow an item (/track finance) or a "
                                "topic (/follow keywords), or manage them at " + config.PUBLIC_BASE + "/topics"))
        return True
    return False
