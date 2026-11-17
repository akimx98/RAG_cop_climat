"""Briefs par dossier (matin et soir) : ce qui a bougé, sources numérotées, rédigé par le modèle de rédaction.

Ne sont envoyés au modèle que des éléments PUBLICS : textes annoncés, positions extraites, documents publics,
messages Bluesky. Les notes de terrain des contributeurs sont ajoutées telles quelles à l'affichage, sans passer
par le modèle.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, db, live, llm, positions, search

HOURS = config.BRIEF_HOURS

SYSTEM = """Tu rédiges le brief d'un dossier de négociation pour les participants à la COP31 (négociateurs, observateurs, société civile).
Lecteurs : des négociateurs pressés ET des novices. Français clair, phrases courtes, aucun jargon non expliqué.
Tu ne disposes QUE des éléments fournis (sources numérotées S1, S2…). N'ajoute aucun fait extérieur.
Chaque affirmation renvoie à au moins une source (liste de numéros). S'il ne s'est rien passé de notable, dis-le.
Distingue : textes (version, crochets/options, ce qui avance ou bloque), positions (qui veut quoi, qui bouge),
signaux (presse, observateurs : niveau de confiance moindre). Termine par ce qu'il faut surveiller."""

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["essentiel", "points", "a_surveiller"],
    "properties": {
        "essentiel": {"type": "string"},
        "points": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["texte", "sources"],
            "properties": {"texte": {"type": "string"}, "sources": {"type": "array", "items": {"type": "integer"}}}}},
        "a_surveiller": {"type": "array", "items": {"type": "string"}},
    },
}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gather(conn, slug: str, since: str) -> tuple[list[dict], str]:
    """Éléments publics du dossier depuis `since` ; renvoie (sources numérotées, texte pour le modèle)."""
    d = live.dossier(slug)
    sources: list[dict] = []
    lines: list[str] = []

    def add(label: str, url: str | None, body: str) -> int:
        sources.append({"n": len(sources) + 1, "label": label, "url": url})
        lines.append(f"[S{len(sources)}] {body}")
        return len(sources)

    status = [s["status"] for s in live.status_by_dossier(conn).get(slug, [])]
    lines.append(f"État du dossier selon {live.CB_CREDIT} : {', '.join(status) or 'non renseigné'}")
    for t in conn.execute(
            """SELECT * FROM live_texts WHERE dossier=? AND (first_seen>=? OR updated_at>=?)
               ORDER BY COALESCE(published, first_seen)""", (slug, since, since)).fetchall():
        url = f"{config.PUBLIC_BASE}/doc/{t['doc_id']}" if t["doc_id"] else t["pdf_url"]
        add(f"{t['symbol'] or ''} {t['title'] or ''}".strip()[:160], url,
            f"Texte {t['symbol'] or ''} « {t['title']} » ({t['body']}, point {t['agenda_item']}), publié {t['published']}, "
            f"{t['pages'] or '?'} p., {t['brackets'] if t['brackets'] is not None else '?'} crochets, "
            f"{t['options'] if t['options'] is not None else '?'} options. {t['note'] or ''}")
    base = [p for p in positions.for_dossier(conn, slug, limit=400) if p["source_kind"] == "carbonbrief"]
    if base:
        add(f"{live.CB_CREDIT} — Who wants what (COP31)", live.CB_WWW_PAGE,
            "Positions de référence avant la COP (contexte, pas une nouveauté) : "
            + " ; ".join(f"{p['actor']} {p['stance']} « {p['issue']} »" for p in base[:40]))
    for p in positions.for_dossier(conn, slug, since=since, limit=60):
        if p["access"] != "public" or p["source_kind"] == "carbonbrief":
            continue
        if p["source_kind"] == "soumission" and (p["said_on"] or "") < since[:10]:
            continue  # position écrite avant la période : pas une nouveauté (analyse faite après coup)
        add(p["source_label"] or "source", p["source_url"] if (p["source_url"] or "").startswith("http")
            else f"{config.PUBLIC_BASE}{p['source_url'] or ''}",
            f"Position — {p['actor']} ({p['stance']}) sur « {p['issue']} » : {p['summary'] or ''} "
            f"Citation : « {p['quote'] or ''} » ({p['said_on'] or ''})")
    hits = search.search(conn, d["requete"], {"added_since": since, "access": "public"},
                         {"can_restricted": 0, "active": 1}, limit=8, per_doc=1)
    for h in hits:
        add(h["titre"][:160], h.get("lien_source") or h.get("lien_passage"),
            f"Document {h['titre']} (niveau {h.get('niveau')}, {h.get('organisation') or ''}) : « {h['extrait'][:500]} »")
    for s in live.social(conn, live.dossier_words(d), since=since, limit=12):
        add(f"{s['author']} (Bluesky)", s["url"], f"Message de {s['author']} : « {s['text'][:400]} »")
    return sources, "\n".join(lines)


def generate(conn, slug: str, since: str, force: bool = False) -> int | None:
    live.ensure_schema(conn)
    d = live.dossier(slug)
    sources, material = gather(conn, slug, since)
    news = [x for x in sources if not x["label"].startswith(live.CB_CREDIT + " — Who wants what")]
    if not news and not force:
        return None  # rien de nouveau : pas de brief (ni dépense)
    notes = live.notes(conn, slug, since=since)
    if sources:
        out = llm.json_call(
            conn, model=llm.WRITE_MODEL, purpose="brief", system=SYSTEM, name="brief", effort="low", schema=SCHEMA,
            user=f"Dossier : {d['nom']}\nEnjeu : {d['enjeu']}\nPériode : depuis {since}\n\n{material}")
    else:
        out = {"essentiel": "Rien de nouveau dans les sources publiques sur la période.", "points": [], "a_surveiller": []}
    cur = conn.execute(
        "INSERT INTO briefs(dossier,created_at,period_start,body,sources,model) VALUES(?,?,?,?,?,?)",
        (slug, live.now_iso(), since, json.dumps({**out, "notes": [n["id"] for n in notes]}, ensure_ascii=False),
         json.dumps(sources, ensure_ascii=False),
         llm.WRITE_MODEL))
    conn.commit()
    return cur.lastrowid


def latest(conn, slug: str) -> dict | None:
    live.ensure_schema(conn)
    r = conn.execute("SELECT * FROM briefs WHERE dossier=? ORDER BY id DESC LIMIT 1", (slug,)).fetchone()
    if not r:
        return None
    b = dict(r)
    b["body"] = json.loads(b["body"] or "{}")
    b["sources"] = json.loads(b["sources"] or "[]")
    return b


def clean(text: str) -> str:
    """Retire les renvois [S1] laissés dans le texte (les sources sont affichées à part)."""
    import re

    return re.sub(r"\s*\[S\d+\](?:\s*\[S\d+\])*", "", text or "").strip()


def render_telegram(b: dict, d: dict, with_notes: list[dict] | None = None, lang: str = "fr") -> str:
    body = b["body"]
    src = {s["n"]: s for s in b["sources"]}

    def refs(ns):
        return " ".join(f"<a href=\"{html.escape(src[n]['url'])}\">[{n}]</a>" if n in src and src[n].get("url")
                        else f"[{n}]" for n in ns)

    name = (d.get("nom_en") or d["nom"]) if lang == "en" else d["nom"]
    head = f"🗂 <b>{html.escape(name)}</b>" + (" — AI brief (in French)" if lang == "en" else "")
    lines = [head, html.escape(clean(body.get("essentiel", "")))]
    for p in body.get("points", [])[:8]:
        lines.append(f"• {html.escape(clean(p['texte']))} {refs(p.get('sources', []))}")
    if body.get("a_surveiller"):
        lines.append("👀 " + html.escape(" · ".join(clean(x) for x in body["a_surveiller"][:4])))
    if with_notes:
        lines.append(f"🔒 {len(with_notes)} note(s) de terrain : {config.PUBLIC_BASE}/dossiers/{d['slug']}")
    lines.append(f"{config.PUBLIC_BASE}/dossiers/{d['slug']}")
    return "\n".join(lines)


def due_slot(conn) -> str | None:
    """Créneau de brief en cours (« 2026-11-09T07 ») s'il n'a pas encore été traité."""
    local = datetime.now(ZoneInfo(config.TIMEZONE))
    slots = [h for h in HOURS if local.hour >= h]
    if not slots:
        return None
    slot = f"{local:%Y-%m-%d}T{max(slots):02d}"
    return None if db.meta_get(conn, "brief_slot") == slot else slot


def run_slot(conn, send) -> int:
    """Génère les briefs des dossiers actifs et les envoie à leurs abonnés Telegram."""
    slot = due_slot(conn)
    if not slot or not llm.enabled():
        return 0
    db.meta_set(conn, "brief_slot", slot)  # marqué d'abord : pas de double envoi en cas d'erreur
    since = _iso(datetime.now(timezone.utc) - timedelta(hours=14))
    n = 0
    for d in live.dossiers():
        try:
            bid = generate(conn, d["slug"], since)
        except llm.BudgetExceeded:
            live.log("plafond de dépense atteint : briefs suspendus")
            break
        except Exception as exc:
            live.log(f"brief {d['slug']} : {exc}")
            continue
        if not bid:
            continue
        n += 1
        b = latest(conn, d["slug"])
        notes = live.notes(conn, d["slug"], since=since)
        for u in live.followers(conn, d["slug"]):
            from . import i18n

            send(u["telegram_chat_id"], render_telegram(b, d, notes if u.get("can_restricted") else None,
                                                        lang=i18n.default_for(u)))
    return n
