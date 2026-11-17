"""Pages de la veille en direct : dossiers de négociation, textes en cours, notes de terrain."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from . import auth, briefs, db, ingest, live, llm, positions, search
from .web import TEMPLATES, contributor_required, login_required, render

TEMPLATES.filters["briefclean"] = briefs.clean


def _since(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _spark(series: list[dict], width: int = 260, height: int = 48) -> str | None:
    """Petite courbe SVG du nombre de crochets d'un dossier (versions successives)."""
    pts = [s for s in series if s.get("brackets") is not None]
    if len(pts) < 2:
        return None
    top = max(p["brackets"] for p in pts) or 1
    step = width / (len(pts) - 1)
    coords = [(round(i * step, 1), round(height - 4 - (p["brackets"] / top) * (height - 8), 1)) for i, p in enumerate(pts)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x},{y}" for i, (x, y) in enumerate(coords))
    dots = "".join(f'<circle cx="{x}" cy="{y}" r="2.5"><title>{p["brackets"]} crochets — {p["published"]}</title></circle>'
                   for (x, y), p in zip(coords, pts))
    return (f'<svg class="spark" viewBox="-4 0 {width + 8} {height}" width="{width}" height="{height}" role="img" '
            f'aria-label="Évolution des crochets"><path d="{path}" fill="none" stroke="currentColor" stroke-width="2"/>'
            f'<g fill="currentColor">{dots}</g></svg>')


@login_required
async def dossiers_page(request: Request):
    conn = db.get()
    user = request.state.user
    return render(request, "dossiers.html", rows=live.overview(conn, user),
                  usage=llm.usage_summary(conn) if user.get("is_admin") else None,
                  credit=live.CB_CREDIT, cb_page=live.CB_TEXTS_PAGE)


@login_required
async def dossier_page(request: Request):
    conn = db.get()
    user = request.state.user
    slug = request.path_params["slug"]
    d = live.dossier(slug)
    if not d:
        return Response("Dossier inconnu", status_code=404)
    restricted = auth.can_restricted(user)
    ts = live.texts(conn, slug)
    for t in ts:
        if t["doc_id"]:
            v = conn.execute("SELECT version FROM documents WHERE id=?", (t["doc_id"],)).fetchone()
            t["version"] = v["version"] if v else None
    b = briefs.latest(conn, slug)
    extracted = [p for p in positions.for_dossier(conn, slug, limit=200) if p["source_kind"] not in ("carbonbrief", "soumission")]
    ref = [p for p in positions.for_dossier(conn, slug, limit=400) if p["source_kind"] == "carbonbrief"]
    by_actor: dict[str, list[dict]] = {}
    for p in ref:
        by_actor.setdefault(p["actor"], []).append(p)
    hits = search.search(conn, d["requete"], {"added_since": _since(24 * 7)}, user, limit=10, per_doc=1)
    return render(
        request, "dossier.html", d=d, texts=ts, brief=b, spark=_spark(live.bracket_series(conn, slug)),
        etat=live.status_by_dossier(conn).get(slug, []), extracted=extracted[:60],
        ref_actors=sorted(by_actor.items(), key=lambda kv: (-len(kv[1]), kv[0])),
        notes=live.notes(conn, slug) if restricted else [], restricted=restricted, hits=hits,
        social=live.social(conn, live.dossier_words(d), since=_since(24 * 4), limit=15),
        suivi=slug in live.followed(conn, user["id"]), credit=live.CB_CREDIT, cb_www=live.CB_WWW_PAGE,
        cb_texts=live.CB_TEXTS_PAGE, message=request.query_params.get("ok"))


@login_required
async def dossier_follow(request: Request):
    conn = db.get()
    slug = request.path_params["slug"]
    if live.dossier(slug):
        live.follow(conn, request.state.user["id"], slug, slug not in live.followed(conn, request.state.user["id"]))
    back = request.headers.get("referer") or f"/dossiers/{slug}"
    return RedirectResponse(back, status_code=303)


@login_required
async def dossier_note(request: Request):
    conn = db.get()
    user = request.state.user
    slug = request.path_params["slug"]
    if not auth.can_restricted(user) or not live.dossier(slug):
        return Response("Non autorisé", status_code=403)
    form = await request.form()
    text = (form.get("text") or "").strip()
    if text:
        live.add_note(conn, user, text, dossier_slug=slug, via="web")
    return RedirectResponse(f"/dossiers/{slug}?ok=note#notes", status_code=303)


@contributor_required
async def dossier_brief(request: Request):
    """Régénérer le brief à la demande (au plus une fois toutes les 20 minutes)."""
    conn = db.get()
    slug = request.path_params["slug"]
    if not live.dossier(slug) or not llm.enabled():
        return Response("Indisponible", status_code=400)
    last = briefs.latest(conn, slug)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not last or last["created_at"] < cutoff:
        try:
            briefs.generate(conn, slug, _since(24), force=True)
        except llm.BudgetExceeded:
            return RedirectResponse(f"/dossiers/{slug}?ok=plafond", status_code=303)
    return RedirectResponse(f"/dossiers/{slug}?ok=brief", status_code=303)


@login_required
async def textes_page(request: Request):
    conn = db.get()
    pending = request.query_params.get("a_recuperer") == "1"
    ts = live.texts(conn, request.query_params.get("dossier") or None, pending_only=pending, limit=300)
    names = {d["slug"]: d["nom"] for d in live.dossiers()}
    return render(request, "textes.html", texts=ts, names=names, pending=pending, credit=live.CB_CREDIT,
                  cb_page=live.CB_TEXTS_PAGE, message=request.query_params.get("ok"))


@contributor_required
async def texte_upload(request: Request):
    """Dépôt du PDF d'un texte annoncé, récupéré par un contributeur dans son navigateur."""
    conn = db.get()
    tid = int(request.path_params["tid"])
    t = conn.execute("SELECT * FROM live_texts WHERE id=?", (tid,)).fetchone()
    if not t:
        return Response("Texte inconnu", status_code=404)
    form = await request.form()
    f = form.get("file")
    if not f or not getattr(f, "filename", ""):
        return RedirectResponse("/textes?ok=vide", status_code=303)
    content = await f.read()
    doc_id, _new, _st = ingest.ingest_bytes(conn, content, url=t["pdf_url"], mime=f.content_type or "",
                                            meta=live._meta(t), source_id="live-texts", access="public",
                                            uploaded_by=request.state.user["id"], filename=f.filename)
    live.attach_upload(conn, tid, doc_id)
    return RedirectResponse(f"/doc/{doc_id}", status_code=303)


routes = [
    Route("/dossiers", dossiers_page),
    Route("/dossiers/{slug}", dossier_page),
    Route("/dossiers/{slug}/suivre", dossier_follow, methods=["POST"]),
    Route("/dossiers/{slug}/note", dossier_note, methods=["POST"]),
    Route("/dossiers/{slug}/brief", dossier_brief, methods=["POST"]),
    Route("/textes", textes_page),
    Route("/textes/{tid:int}/deposer", texte_upload, methods=["POST"]),
]
