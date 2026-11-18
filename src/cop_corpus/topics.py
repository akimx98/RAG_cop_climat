"""« Mes sujets » : les dossiers de négociation suivis et les sujets par mots-clés d'un utilisateur, vus comme une seule
liste, avec ce qui a bougé sur chacun depuis une date. Sans IA (recherche par mots seulement).

Sert à la page « My topics », au récapitulatif Telegram de 7 h et 19 h (digest.py) et à l'outil MCP
`nouveautes(mes_sujets=true)` (« comment ont évolué les drafts depuis hier sur mes sujets ? »).
"""

from __future__ import annotations

from typing import Any

from . import alerts, config, diff, live, search

REPORT_TYPES = ["compte_rendu", "analyse"]


def _name(d: dict, lang: str) -> str:
    return (d.get("nom_en") if lang == "en" else None) or d["nom"]


def text_item(conn, t: dict, lang: str = "en", with_delta: bool = True) -> dict[str, Any]:
    stage = live.text_stage(t)
    out = {"cote": t.get("symbol"), "titre": t.get("title"), "publie_le": t.get("published"),
           "etape": stage, "etape_libelle": live.stage_label(stage, lang), "organe": t.get("body"),
           "crochets": t.get("brackets"), "options": t.get("options"), "doc_id": t.get("doc_id"),
           "lien": f"{config.PUBLIC_BASE}/doc/{t['doc_id']}" if t.get("doc_id") else t.get("pdf_url")}
    if with_delta and t.get("doc_id"):
        delta = live.since_previous(conn, t["doc_id"])
        if delta:
            out["changements"] = diff.delta_line(delta, lang)
            out["lien_diff"] = f"{config.PUBLIC_BASE}/diff/{t['doc_id']}"
    return {k: v for k, v in out.items() if v not in (None, "")}


def new_texts(conn, slug: str, since: str, limit: int = 8) -> list[dict]:
    live.ensure_schema(conn)
    # first_seen seulement : la synchronisation de l'index réécrit updated_at toutes les 5 minutes, et `published`
    # est une heure locale sans fuseau.
    rows = conn.execute(
        """SELECT * FROM live_texts WHERE dossier=? AND first_seen>=?
           ORDER BY COALESCE(published, first_seen) DESC LIMIT ?""", (slug, since, limit)).fetchall()
    return [dict(r) for r in rows]


def reports(conn, query: str, since: str, user: dict, limit: int = 3) -> list[dict]:
    """Lettres d'information et comptes rendus récents qui parlent du sujet (mots seulement : aucun appel payant)."""
    if not query:
        return []
    try:
        # documents publics seulement : ce bloc part aussi sur Telegram (pas chiffré de bout en bout)
        hits = search.search(conn, query, {"added_since": since, "doc_types": REPORT_TYPES, "access": "public"}, user,
                             limit=limit, per_doc=1, semantic=False)
    except Exception:
        return []
    return [{"titre": h.get("titre"), "organisation": h.get("organisation"), "extrait": (h.get("extrait") or "")[:300],
             "lien": h.get("lien_passage"), "niveau": h.get("niveau")} for h in hits if h.get("acces") == "public"]


def keyword_hits(conn, user_id: int, sub_id: int, since: str | None = None, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        """SELECT n.created_at, n.snippet, n.doc_id, d.title, d.level, d.access, d.org, d.version
           FROM notifications n JOIN documents d ON d.id=n.doc_id
           WHERE n.user_id=? AND n.subscription_id=? AND (? IS NULL OR n.created_at>=?)
           ORDER BY n.id DESC LIMIT ?""", (user_id, sub_id, since, since, limit)).fetchall()
    out = []
    for r in rows:
        internal = r["access"] != "public"
        out.append({"titre": r["title"], "niveau": "R" if internal else r["level"], "organisation": r["org"],
                    "extrait": None if internal else r["snippet"], "doc_id": r["doc_id"], "recu_le": r["created_at"],
                    "version": r["version"], "lien": f"{config.PUBLIC_BASE}/doc/{r['doc_id']}"})
    return out


def keyword_count(conn, user_id: int, sub_id: int, since: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND subscription_id=? AND created_at>=?",
                        (user_id, sub_id, since)).fetchone()[0]


def list_topics(conn, user: dict, lang: str = "en") -> dict[str, Any]:
    """Dossiers suivis et sujets par mots-clés, sans activité (rapide)."""
    followed = live.followed(conn, user["id"])
    return {"dossiers": [d for d in live.dossiers() if d["slug"] in followed],
            "mots_cles": alerts.list_subscriptions(conn, user["id"])}


def whats_new(conn, user: dict, since: str, lang: str = "en", texts_per_item: int = 6,
              reports_per_item: int = 3) -> dict[str, Any]:
    """Ce qui a bougé depuis `since` sur chaque sujet de l'utilisateur : textes de négociation (étape, crochets,
    changements par rapport à la version précédente), comptes rendus et lettres qui en parlent, documents signalés
    par les sujets par mots-clés."""
    topic_list = list_topics(conn, user, lang)
    items: list[dict[str, Any]] = []
    for d in topic_list["dossiers"]:
        texts = [text_item(conn, x, lang) for x in new_texts(conn, d["slug"], since, texts_per_item)]
        rep = reports(conn, d.get("requete") or "", since, user, reports_per_item)
        items.append({"type": "dossier", "slug": d["slug"], "nom": _name(d, lang), "alias": d.get("alias", [])[:3],
                      "textes": texts, "comptes_rendus": rep,
                      "lien": f"{config.PUBLIC_BASE}/database/negotiations/{d['slug']}"})
    for s in topic_list["mots_cles"]:
        n = keyword_count(conn, user["id"], s["id"], since)
        items.append({"type": "mots_cles", "id": s["id"], "nom": s["libelle"], "requete": s["requete"],
                      "nb_documents": n, "documents": keyword_hits(conn, user["id"], s["id"], since) if n else []})
    quiet = [i["nom"] for i in items if not (i.get("textes") or i.get("comptes_rendus") or i.get("documents"))]
    return {"depuis": since, "sujets": items, "sans_nouveaute": quiet,
            "aucun_sujet": not items}
