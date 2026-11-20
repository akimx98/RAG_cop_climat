"""Page d'un dossier de négociation (dans la base de données) et dépôt d'un texte « à récupérer » (admin)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from . import auth, db, ingest, live, positions, search, usage
from .web import admin_required, login_required, render



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


def _frises(conn, texts: list[dict], user: dict) -> list[dict]:
    """Une frise par texte du dossier qui a au moins deux versions accessibles (crochets, options, notions)."""
    from . import formulations

    out, seen = [], set()
    for t in texts:
        if not t.get("doc_id"):
            continue
        row = conn.execute("SELECT * FROM documents WHERE id=?", (t["doc_id"],)).fetchone()
        if not row or not row["lineage_key"] or row["lineage_key"] in seen:
            continue
        seen.add(row["lineage_key"])
        if row["access"] != "public" and not auth.can_restricted(user):
            continue
        rows = formulations.lineage(conn, row, user)
        if len(rows) >= 2:
            out.append({"titre": rows[-1]["title"], "cote": rows[-1]["symbol"], "grille": formulations.grid(conn, rows)})
    return out[:4]


@login_required
async def dossier_page(request: Request):
    conn = db.get()
    user = request.state.user
    slug = request.path_params["slug"]
    d = live.dossier(slug)
    if not d:
        return Response("Dossier inconnu", status_code=404)
    usage.record("web", "page", "dossier", user, args={"dossier": slug})
    ts = live.texts(conn, slug)
    for t in ts:
        t["stage"] = live.text_stage(t)
        if t["doc_id"]:
            v = conn.execute("SELECT version FROM documents WHERE id=?", (t["doc_id"],)).fetchone()
            t["version"] = v["version"] if v else None
    extracted = [p for p in positions.for_dossier(conn, slug, limit=200) if p["source_kind"] not in ("carbonbrief", "soumission")]
    ref = [p for p in positions.for_dossier(conn, slug, limit=400) if p["source_kind"] == "carbonbrief"]
    by_actor: dict[str, list[dict]] = {}
    for p in ref:
        by_actor.setdefault(p["actor"], []).append(p)
    hits = search.search(conn, d["requete"], {"added_since": _since(24 * 7)}, user, limit=10, per_doc=1)
    frises = _frises(conn, ts, user)
    return render(
        request, "dossier.html", d=d, texts=ts, spark=_spark(live.bracket_series(conn, slug)),
        etat=live.status_by_dossier(conn).get(slug, []), extracted=extracted[:60],
        ref_actors=sorted(by_actor.items(), key=lambda kv: (-len(kv[1]), kv[0])),
        hits=hits, frises=frises,
        social=live.social(conn, live.dossier_words(d), since=_since(24 * 4), limit=15),
        subs=_sub_section(conn, slug),
        suivi=slug in live.followed(conn, user["id"]), credit=live.CB_CREDIT, cb_www=live.CB_WWW_PAGE,
        cb_texts=live.CB_TEXTS_PAGE, message=request.query_params.get("ok"))


@login_required
async def dossier_follow(request: Request):
    conn = db.get()
    slug = request.path_params["slug"]
    if live.dossier(slug):
        live.follow(conn, request.state.user["id"], slug, slug not in live.followed(conn, request.state.user["id"]))
    return RedirectResponse(f"/database/negotiations/{slug}", status_code=303)


@admin_required
async def texte_upload(request: Request):
    """Dépôt du PDF d'un texte annoncé que l'anti-bot a bloqué, récupéré par un administrateur dans son navigateur."""
    conn = db.get()
    tid = int(request.path_params["tid"])
    t = conn.execute("SELECT * FROM live_texts WHERE id=?", (tid,)).fetchone()
    if not t:
        return Response("Texte inconnu", status_code=404)
    form = await request.form()
    f = form.get("file")
    if not f or not getattr(f, "filename", ""):
        return RedirectResponse("/database?tab=texts", status_code=303)
    content = await f.read()
    doc_id, _new, _st = ingest.ingest_bytes(conn, content, url=t["pdf_url"], mime=f.content_type or "",
                                            meta=live._meta(t), source_id="live-texts", access="public",
                                            uploaded_by=request.state.user["id"], filename=f.filename)
    live.attach_upload(conn, tid, doc_id)
    return RedirectResponse(f"/doc/{doc_id}", status_code=303)


routes = [
    Route("/database/negotiations/{slug}", dossier_page),
    Route("/database/negotiations/{slug}/follow", dossier_follow, methods=["POST"]),
    Route("/dossiers/{slug}", lambda r: RedirectResponse(
        f"/database/negotiations/{r.path_params['slug']}" + (f"?{r.url.query}" if r.url.query else ""), status_code=301)),
    Route("/textes/{tid:int}/deposer", texte_upload, methods=["POST"]),
]


def _sub_section(conn, slug: str):
    from . import soumissions as sm

    try:
        out = sm.dossier_section(conn, slug, limit=8)
        return out if out["appels"] else None
    except Exception:
        return None
