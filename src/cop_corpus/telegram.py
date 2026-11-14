"""Bot Telegram : liaison de compte, gestion des sujets suivis, recherche rapide,
dépôt de documents (PDF/Word transférés), alertes.

Attention : les conversations avec un bot Telegram ne sont PAS chiffrées de
bout en bout. Par défaut, les alertes sur des documents restreints ne
contiennent ni titre ni extrait (COP_TELEGRAM_RESTRICTED_SNIPPETS=0).
"""

from __future__ import annotations

import html
import re
import time
from typing import Any

import httpx

from . import config, db

API = "https://api.telegram.org/bot{token}/{method}"
FILE_API = "https://api.telegram.org/file/bot{token}/{path}"
_http = httpx.Client(timeout=httpx.Timeout(70.0, connect=15.0))


def call(method: str, **params) -> dict[str, Any] | None:
    if not config.TELEGRAM_TOKEN:
        return None
    for attempt in range(3):
        try:
            r = _http.post(API.format(token=config.TELEGRAM_TOKEN, method=method), json=params)
            data = r.json()
            if data.get("ok"):
                return data.get("result")
            if r.status_code == 429:
                time.sleep(int(data.get("parameters", {}).get("retry_after", 3)))
                continue
            return None
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None


def send(chat_id: str | int, text: str) -> bool:
    res = call("sendMessage", chat_id=chat_id, text=text[:4000], parse_mode="HTML", disable_web_page_preview=True)
    return res is not None


def download(file_id: str) -> bytes | None:
    info = call("getFile", file_id=file_id)
    if not info or not info.get("file_path"):
        return None
    r = _http.get(FILE_API.format(token=config.TELEGRAM_TOKEN, path=info["file_path"]))
    return r.content if r.status_code == 200 else None


HELP_BASE = (
    "<b>Assistant COP31</b>\n"
    "/suivre mots, clés, séparés par des virgules — suivre un sujet\n"
    "/sujets — vos sujets suivis\n"
    "/arreter N — arrêter le suivi n° N\n"
    "/cherche question — recherche dans le corpus\n"
    "/nouveau — derniers documents entrés\n"
    "/tableau — lien vers le tableau de bord\n"
    "/lang en — English\n"
)
HELP_BASE_EN = (
    "<b>COP31 assistant</b>\n"
    "/follow keywords, separated, by commas — follow a topic\n"
    "/topics — your topics\n"
    "/stop N — stop following topic no. N\n"
    "/search question — search the corpus\n"
    "/new — latest documents\n"
    "/dashboard — link to the website\n"
    "/lang fr — français\n"
)
HELP = HELP_BASE + (
    "Envoyez un <b>PDF ou un Word</b> (légende = titre) : il est ajouté au corpus comme document "
    "<b>interne restreint</b>. Envoyez une <b>URL</b> : la page est ajoutée au corpus public.\n"
    "Ajoutez <code>#public</code> dans la légende d'un fichier pour le classer public."
)
# Alias anglais des commandes (le traitement reste sur la commande française).
ALIASES = {"/search": "/cherche", "/follow": "/suivre", "/topics": "/sujets", "/stop": "/arreter",
           "/unfollow": "/arreter", "/new": "/nouveau", "/dashboard": "/tableau", "/negotiations": "/dossiers",
           "/negotiation": "/dossier", "/track": "/suivi", "/texts": "/textes", "/langue": "/lang"}


def help_for(user: dict | None) -> str:
    from . import auth, i18n, live_bot

    if i18n.default_for(user) == "en":
        return HELP_BASE_EN + live_bot.HELP_EN
    if auth.can_contribute(user):
        return HELP + live_bot.HELP
    return HELP_BASE + live_bot.HELP_GUEST


def _user_for_chat(conn, chat_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE telegram_chat_id=? AND active=1", (str(chat_id),)).fetchone()
    return dict(row) if row else None


def handle(conn, update: dict) -> None:
    from . import alerts, ingest, search  # import tardif (cycle)

    msg = update.get("message") or update.get("channel_post")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or msg.get("caption") or "").strip()

    if text.startswith("/start"):
        code = text.split(maxsplit=1)[1].strip() if " " in text else ""
        if code:
            row = conn.execute("SELECT * FROM users WHERE telegram_code=? AND active=1", (code,)).fetchone()
            if row:
                conn.execute("UPDATE users SET telegram_chat_id=? WHERE id=?", (str(chat_id), row["id"]))
                conn.commit()
                send(chat_id, f"✅ Compte lié : {html.escape(row['name'] or row['email'])}.\n\n{help_for(dict(row))}")
                return
        send(chat_id, "Send <code>/start YOUR_CODE</code> (shown on the website, Telegram page).\n"
                      "Envoyez <code>/start VOTRE_CODE</code> (code affiché sur le site, page Telegram).")
        return

    user = _user_for_chat(conn, chat_id)
    if not user:
        send(chat_id, "Account not linked: send <code>/start YOUR_CODE</code>.\n"
                      "Compte non lié. Envoyez <code>/start VOTRE_CODE</code>.")
        return

    from . import auth, i18n

    lang = i18n.default_for(user)

    def T(fr: str, en: str) -> str:
        return i18n.pick(lang, fr, en)

    contributor = auth.can_contribute(user)
    doc = msg.get("document")
    if doc and not contributor:
        send(chat_id, T("Seuls les contributeurs peuvent ajouter des documents au corpus commun.",
                        "Only contributors can add documents to the shared corpus."))
        return
    if doc:
        if not config.TELEGRAM_ALLOW_UPLOAD:
            send(chat_id, "Le dépôt par Telegram est désactivé : utilisez le tableau de bord.")
            return
        if doc.get("file_size", 0) > 20 * 1024 * 1024:
            send(chat_id, "Fichier > 20 Mo (limite Telegram) : déposez-le sur le tableau de bord.")
            return
        content = download(doc["file_id"])
        if not content:
            send(chat_id, "Téléchargement impossible.")
            return
        public = "#public" in text.lower()
        title = re.sub(r"#\w+", "", text).strip() or doc.get("file_name")
        doc_id, new, status = ingest.ingest_bytes(
            conn, content, url=None, mime=doc.get("mime_type", ""), meta={"title": title} if title else {},
            source_id="depot-telegram", access="public" if public else "restreint", uploaded_by=user["id"],
            filename=doc.get("file_name"),
        )
        verdict = "ajouté" if new else "déjà présent"
        send(chat_id, f"📥 Document {verdict} ({'public' if public else 'restreint'}, {status}) : "
                      f"{config.PUBLIC_BASE}/doc/{doc_id}")
        alerts.run(conn)
        return
    if msg.get("photo"):
        send(chat_id, T("Les photos ne sont pas lues : utilisez /cherche …", "Photos are not read: use /search …"))
        return

    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower().split("@")[0]
    cmd = ALIASES.get(cmd, cmd)
    arg = arg.strip()
    if cmd == "/lang":
        if arg.lower() in i18n.LANGS:
            conn.execute("UPDATE users SET lang=? WHERE id=?", (arg.lower(), user["id"]))
            conn.commit()
            user["lang"] = arg.lower()
            send(chat_id, ("✓ English\n\n" if arg.lower() == "en" else "✓ Français\n\n") + help_for(user))
        else:
            send(chat_id, "/lang en · /lang fr")
        return
    from . import live_bot

    if live_bot.handle(conn, chat_id, user, cmd, arg, send):
        return
    if cmd in ("/aide", "/help"):
        send(chat_id, help_for(user))
    elif cmd == "/suivre":
        if not arg:
            send(chat_id, T("Exemple : <code>/suivre combustibles fossiles, fossil fuels, TAFF</code>",
                            "Example: <code>/follow fossil fuels, combustibles fossiles, TAFF</code>"))
            return
        sid = alerts.add_subscription(conn, user["id"], arg[:60], arg)
        found = alerts.preview(conn, arg, None, None, user, days=7, limit=3)
        extra = "".join(f"\n• {html.escape((f.get('titre') or '')[:120])}" for f in found)
        send(chat_id, T(f"✅ Sujet n° {sid} suivi : ", f"✅ Following topic no. {sid}: ") + f"<b>{html.escape(arg)}</b>"
                      + (T("\nDéjà dans le corpus cette semaine :", "\nAlready in the corpus this week:") + extra if extra else ""))
    elif cmd == "/sujets":
        subs = alerts.list_subscriptions(conn, user["id"])
        if not subs:
            send(chat_id, T("Aucun sujet suivi. <code>/suivre …</code>", "No topics yet. <code>/follow …</code>"))
        else:
            send(chat_id, "\n".join(f"{s['id']}. {html.escape(s['libelle'])}" for s in subs))
    elif cmd == "/arreter":
        ok = arg.isdigit() and alerts.remove_subscription(conn, user["id"], int(arg))
        send(chat_id, T("Suivi arrêté.", "Stopped.") if ok else T("Numéro inconnu : /sujets", "Unknown number: /topics"))
    elif cmd == "/cherche":
        if not arg:
            send(chat_id, T("Exemple : <code>/cherche objectif 2040 de l'UE</code>",
                            "Example: <code>/search transitioning away from fossil fuels</code>"))
            return
        hits = search.search(conn, arg, {}, user, limit=5)
        if not hits:
            send(chat_id, T("Rien trouvé dans le corpus.", "Nothing found in the corpus."))
            return
        lines = []
        for h in hits:
            if h.get("acces") == "restreint" and not config.TELEGRAM_RESTRICTED_SNIPPETS:
                # Telegram n'est pas chiffré de bout en bout : ni titre ni extrait d'un document interne.
                lines.append(f"🔒 <b>{T('Document interne', 'Internal document')}</b> [R]\n{config.PUBLIC_BASE}/doc/{h['doc_id']}")
                continue
            ref = " ".join(x for x in (h.get("cote"), h.get("paragraphe"), f"p.{h.get('page')}") if x)
            lines.append(
                f"<b>{html.escape((h.get('titre') or '')[:110])}</b> [{T('niv.', 'lvl')} {h.get('niveau', '?')}] {html.escape(ref)}\n"
                f"« {html.escape(h['extrait'][:260])} »\n"
                f"<a href=\"{html.escape(h['lien_passage'])}\">passage</a>"
                + (f" · <a href=\"{html.escape(h['lien_source'])}\">original p. {h.get('page') or 1}</a>" if h.get("lien_source") else "")
            )
        send(chat_id, "\n\n".join(lines))
    elif cmd == "/nouveau":
        docs = search.latest(conn, user, limit=10)
        def label(d: dict) -> str:
            if d.get("acces") == "restreint" and not config.TELEGRAM_RESTRICTED_SNIPPETS:
                return "🔒 " + T("Document interne", "Internal document")
            return html.escape((d.get("titre") or "")[:110])

        send(chat_id, "\n".join(
            f"• [{T('niv.', 'lvl')} {d.get('niveau', '?')}] {label(d)} — {config.PUBLIC_BASE}/doc/{d['doc_id']}"
            for d in docs) or T("Rien.", "Nothing."))
    elif cmd == "/tableau":
        send(chat_id, f"{config.PUBLIC_BASE}/")
    elif re.match(r"https?://\S+$", text) and not contributor:
        send(chat_id, T("Seuls les contributeurs peuvent ajouter des pages au corpus commun. Pour chercher : /cherche …",
                        "Only contributors can add pages to the shared corpus. To search: /search …"))
    elif re.match(r"https?://\S+$", text):
        from . import fetch

        res = fetch.Client().get(text)
        if not res.ok or fetch.is_antibot(res):
            send(chat_id, "Page inaccessible (ou protégée par un anti-bot) : envoyez le fichier directement.")
            return
        doc_id, new, status = ingest.ingest_bytes(
            conn, res.content, url=res.url, mime=res.mime, meta={}, source_id="depot-telegram",
            access="public", uploaded_by=user["id"],
        )
        send(chat_id, f"📥 {'Ajouté' if new else 'Déjà présent'} : {config.PUBLIC_BASE}/doc/{doc_id}")
        alerts.run(conn)
    else:
        send(chat_id, help_for(user))


def poll_forever(stop=None) -> None:
    """Boucle de réception (long polling)."""
    if not config.TELEGRAM_TOKEN:
        return
    conn = db.connect()
    offset = int(db.meta_get(conn, "telegram_offset", "0") or 0)
    live_bot = __import__("cop_corpus.live_bot", fromlist=["COMMANDS"])
    call("setMyCommands", commands=[  # menu par défaut : anglais
        {"command": "search", "description": "search the corpus"},
        {"command": "follow", "description": "follow a topic (keywords)"},
        {"command": "topics", "description": "my topics"},
        {"command": "new", "description": "latest documents"},
        *live_bot.COMMANDS_EN,
        {"command": "dashboard", "description": "link to the website"},
        {"command": "lang", "description": "language: /lang fr · /lang en"},
        {"command": "help", "description": "help"},
    ])
    call("setMyCommands", language_code="fr", commands=[  # Telegram réglé en français
        {"command": "suivre", "description": "suivre un sujet (mots-clés)"},
        {"command": "sujets", "description": "mes sujets suivis"},
        {"command": "cherche", "description": "chercher dans le corpus"},
        {"command": "nouveau", "description": "derniers documents"},
        {"command": "tableau", "description": "lien du tableau de bord"},
        {"command": "lang", "description": "langue : /lang en · /lang fr"},
        {"command": "aide", "description": "aide"},
        *live_bot.COMMANDS,
    ])
    while not (stop and stop.is_set()):
        updates = call("getUpdates", offset=offset, timeout=50, allowed_updates=["message"]) or []
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                handle(conn, upd)
            except Exception as exc:
                chat = (upd.get("message") or {}).get("chat", {}).get("id")
                if chat:
                    send(chat, f"Erreur : {html.escape(str(exc))[:300]}")
            db.meta_set(conn, "telegram_offset", str(offset))
        if not updates:
            time.sleep(1)
