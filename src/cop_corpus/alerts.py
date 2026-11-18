"""Abonnements (sujets suivis) et alertes.

Un abonnement = une requête (mots-clés séparés par des virgules, ou syntaxe
FTS5) + des filtres (niveaux, types). À chaque passage du veilleur, les
nouveaux documents sont confrontés à tous les abonnements actifs ; chaque
correspondance crée une notification (visible sur le tableau de bord) et, si
l'utilisateur a lié Telegram, un message.
"""

from __future__ import annotations

import html
import re
import sqlite3
from typing import Any

from . import auth, config, db, search, telegram


def sub_to_fts(query: str) -> str | None:
    query = (query or "").strip()
    if not query or query == "*":
        return None
    if search.FTS_OPS.search(query) and not ("," in query and '"' not in query):
        return query
    parts = [p.strip() for p in re.split(r"[,;\n]+", query) if p.strip()]
    terms = []
    for p in parts:
        words = [w for w in search.WORD.findall(p)]
        if not words:
            continue
        if len(words) == 1:
            w = words[0]
            terms.append(f'"{w}"*' if len(w) >= 5 else f'"{w}"')
        else:
            terms.append('"' + " ".join(words) + '"' + ("*" if len(words[-1]) >= 5 else ""))
    return " OR ".join(terms) or None


def add_subscription(conn, user_id: int, label: str, query: str, levels=None, doc_types=None,
                     telegram_on: bool = True) -> int:
    cur = conn.execute(
        """INSERT INTO subscriptions(user_id,label,query,levels,doc_types,notify_telegram,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (user_id, label.strip() or query[:40], query.strip(), db.jdump(levels), db.jdump(doc_types),
         int(telegram_on), db.now()),
    )
    conn.commit()
    return cur.lastrowid


def list_subscriptions(conn, user_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM subscriptions WHERE user_id=? AND active=1 ORDER BY id", (user_id,)
    ).fetchall()
    return [
        {"id": r["id"], "libelle": r["label"], "requete": r["query"], "niveaux": db.jload(r["levels"]),
         "types": db.jload(r["doc_types"]), "telegram": bool(r["notify_telegram"])}
        for r in rows
    ]


def remove_subscription(conn, user_id: int, sub_id: int) -> bool:
    cur = conn.execute("UPDATE subscriptions SET active=0 WHERE id=? AND user_id=?", (sub_id, user_id))
    conn.commit()
    return cur.rowcount > 0


def matches(conn, sub: sqlite3.Row, user: dict, min_doc_id: int, max_doc_id: int | None = None,
            limit: int = 50) -> list[sqlite3.Row]:
    """Documents (id > min_doc_id) correspondant à l'abonnement, avec le meilleur passage."""
    clauses = ["d.id > ?", "d.status IN ('indexed','empty','data')",
               # Soumissions CCNUCC : alerte seulement pour un dépôt récent (le stock historique arrive en continu
               # pendant des heures et ne doit pas déclencher des milliers d'alertes).
               "NOT (COALESCE(d.source_id,'')='unfccc-submissions' AND COALESCE(d.published,'') < date('now','-14 days'))"]
    clauses.append("d.duplicate_of IS NULL")  # même texte déjà au corpus sous un exemplaire plus officiel
    params: list[Any] = [min_doc_id]
    if max_doc_id is not None:
        clauses.append("d.id <= ?")
        params.append(max_doc_id)
    if not auth.can_restricted(user):
        clauses.append("d.access='public'")
    levels = db.jload(sub["levels"])
    if levels:
        clauses.append(f"COALESCE(d.level, CASE WHEN d.access='restreint' THEN 'R' END) IN ({','.join('?' * len(levels))})")
        params += [str(x) for x in levels]
    types = db.jload(sub["doc_types"])
    if types:
        clauses.append(f"d.doc_type IN ({','.join('?' * len(types))})")
        params += types
    where = " AND ".join(clauses)
    fts = sub_to_fts(sub["query"])
    if fts is None:
        return conn.execute(
            f"SELECT d.id AS doc_id, NULL AS pid, NULL AS text FROM documents d WHERE {where} ORDER BY d.id LIMIT ?",
            [*params, limit],
        ).fetchall()
    try:
        return conn.execute(
            f"""SELECT d.id AS doc_id, MIN(p.id) AS pid, p.text AS text
                FROM passages_fts JOIN passages p ON p.id=passages_fts.rowid JOIN documents d ON d.id=p.doc_id
                WHERE passages_fts MATCH ? AND {where}
                GROUP BY d.id ORDER BY d.id LIMIT ?""",
            [fts, *params, limit],
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def preview(conn, sub_query: str, levels, doc_types, user: dict, days: int = 30, limit: int = 15) -> list[dict]:
    """Aperçu : ce que l'abonnement aurait signalé récemment."""
    fake = {"query": sub_query, "levels": db.jdump(levels), "doc_types": db.jdump(doc_types)}
    row = conn.execute(
        "SELECT MIN(id) FROM documents WHERE fetched_at >= datetime('now', ?)", (f"-{days} days",)
    ).fetchone()
    min_id = (row[0] or 1) - 1
    out = []
    for m in matches(conn, fake, user, min_id, limit=limit):  # type: ignore[arg-type]
        doc = conn.execute("SELECT * FROM documents WHERE id=?", (m["doc_id"],)).fetchone()
        item = search.doc_public(doc)
        item["extrait"] = search.snippet(m["text"], sub_query.replace(",", " "), 300) if m["text"] else ""
        out.append(item)
    return out


def run(conn, notify: bool = True) -> int:
    """Confronte les nouveaux documents aux abonnements. Renvoie le nb de notifications créées."""
    max_id = conn.execute("SELECT COALESCE(MAX(id),0) FROM documents").fetchone()[0]
    last = db.meta_get(conn, "alerts_last_doc_id")
    if last is None:
        # Premier passage : on n'alerte pas sur le corpus initial.
        db.meta_set(conn, "alerts_last_doc_id", str(max_id))
        return 0
    last_id = int(last)
    if max_id <= last_id:
        if notify:
            send_pending(conn)
        return 0
    # Ne traiter que les documents déjà indexés (un document 'pending' sera vu au tour suivant).
    pending = conn.execute(
        "SELECT MIN(id) FROM documents WHERE id>? AND status='pending'", (last_id,)
    ).fetchone()[0]
    upto = (pending - 1) if pending else max_id
    created = 0
    subs = conn.execute("SELECT * FROM subscriptions WHERE active=1").fetchall()
    for sub in subs:
        user = auth.by_id(conn, sub["user_id"])
        if not user:
            continue
        for m in matches(conn, sub, user, last_id, upto, limit=200):
            snip = search.snippet(m["text"], sub["query"].replace(",", " "), 350) if m["text"] else None
            cur = conn.execute(
                """INSERT OR IGNORE INTO notifications(user_id,subscription_id,doc_id,passage_id,snippet,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (user["id"], sub["id"], m["doc_id"], m["pid"], snip, db.now()),
            )
            created += cur.rowcount
    conn.commit()
    db.meta_set(conn, "alerts_last_doc_id", str(upto))
    if notify:
        send_pending(conn)
    return created


def format_notification(conn, n: sqlite3.Row) -> str:
    from . import i18n

    doc = conn.execute("SELECT * FROM documents WHERE id=?", (n["doc_id"],)).fetchone()
    sub = conn.execute("SELECT label FROM subscriptions WHERE id=?", (n["subscription_id"],)).fetchone()
    lang = i18n.default_for(auth.by_id(conn, n["user_id"]))

    def T(fr: str, en: str) -> str:
        return i18n.pick(lang, fr, en)

    label = html.escape(sub["label"] if sub else T("Suivi", "Topic"))
    link = f"{config.PUBLIC_BASE}/doc/{doc['id']}"
    if doc["access"] == "restreint" and not config.TELEGRAM_RESTRICTED_SNIPPETS:
        return (f"🔒 <b>{label}</b>\n{T('Nouveau document interne correspondant.', 'New matching internal document.')}\n"
                f"<a href=\"{link}\">{T('Ouvrir sur le tableau de bord', 'Open on the website')}</a>")
    level = doc["level"] or "?"
    bits = [b for b in (f"{T('niv.', 'lvl')} {level}", doc["org"], doc["symbol"], f"v{doc['version']}" if (doc["version"] or 1) > 1 else None) if b]
    text = f"🔔 <b>{label}</b>\n<b>{html.escape((doc['title'] or T('Sans titre', 'Untitled'))[:200])}</b>\n<i>{html.escape(' · '.join(bits))}</i>"
    if n["snippet"]:
        text += f"\n« {html.escape(n['snippet'][:350])} »"
    text += f"\n<a href=\"{link}\">{T('Tableau de bord', 'Website')}</a>"
    if doc["url"] and doc["access"] == "public":
        text += f" · <a href=\"{html.escape(doc['url'])}\">Original</a>"
    return text


def send_pending(conn, max_per_user: int = 15) -> int:
    if not config.TELEGRAM_TOKEN:
        return 0
    rows = conn.execute(
        """SELECT n.*, u.telegram_chat_id FROM notifications n
           JOIN users u ON u.id=n.user_id
           JOIN subscriptions s ON s.id=n.subscription_id
           WHERE n.telegram_sent_at IS NULL AND u.telegram_chat_id IS NOT NULL AND s.notify_telegram=1
           ORDER BY n.id"""
    ).fetchall()
    sent, per_user = 0, {}
    for n in rows:
        uid = n["user_id"]
        per_user[uid] = per_user.get(uid, 0) + 1
        if per_user[uid] > max_per_user:
            continue
        if telegram.send(n["telegram_chat_id"], format_notification(conn, n)):
            conn.execute("UPDATE notifications SET telegram_sent_at=? WHERE id=?", (db.now(), n["id"]))
            sent += 1
    # Trop d'alertes d'un coup : un message récapitulatif plutôt qu'un déluge.
    for uid, count in per_user.items():
        if count > max_per_user:
            chat = next(r["telegram_chat_id"] for r in rows if r["user_id"] == uid)
            from . import i18n

            more = count - max_per_user
            telegram.send(chat, i18n.pick(i18n.default_for(auth.by_id(conn, uid)),
                                          f"… et {more} autres alertes : voir {config.PUBLIC_BASE}/",
                                          f"… and {more} more alerts: see {config.PUBLIC_BASE}/"))
            conn.execute(
                "UPDATE notifications SET telegram_sent_at=? WHERE user_id=? AND telegram_sent_at IS NULL",
                (db.now(), uid),
            )
    conn.commit()
    return sent
