"""Tableau de bord web : fil d'alertes, sujets suivis, recherche, documents,
versions, dépôt de documents internes, liaison Telegram, connecteur MCP, admin."""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import alerts, auth, config, db, diff, i18n, ingest, mailin, qr, search

TEMPLATES = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html"]),
)
TEMPLATES.globals["LEVELS"] = search.LEVEL_LABELS


def _diffhtml(text: str) -> Markup:
    s = str(escape(text))
    for a, b in (("[-", '<span class="del">'), ("-]", "</span>"), ("{+", '<span class="ins">'), ("+}", "</span>")):
        s = s.replace(a, b)
    return Markup(s)


TEMPLATES.filters["diffhtml"] = _diffhtml

_MARKER = re.compile(r"^\s*(\(?[0-9a-zivx]{1,4}[.)]|[•▪●\-–]|[A-Z][A-Z .'’-]{6,}$)")


def _reflow(text: str) -> str:
    """Affichage : recolle les lignes coupées par la mise en page du PDF, garde les alinéas et les énumérations."""
    out: list[str] = []
    for line in (text or "").split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if out and not _MARKER.match(stripped) and not out[-1].endswith((".", ";", ":", "?", "!")):
            out[-1] += " " + stripped
        else:
            out.append(stripped)
    return "\n".join(out)


TEMPLATES.filters["reflow"] = _reflow
TEMPLATES.globals["BOT"] = config.TELEGRAM_BOT_NAME
DOC_TYPES = ["traite", "decision", "conclusions_sb", "ordre_du_jour", "projet_texte", "ndc", "btr", "rapport_synthese",
             "loi_ue", "proposition_ue", "conclusions_conseil", "strategie_nationale", "rapport_scientifique",
             "donnees", "compte_rendu", "communique", "analyse", "programme", "note_interne", "autre"]
TEMPLATES.globals["DOC_TYPES"] = DOC_TYPES
TEMPLATES.globals["can_contribute"] = auth.can_contribute
TEMPLATES.globals["QR"] = lambda text, scale=6: Markup(qr.svg(text, ecc="M", scale=scale, title=text))
COOKIE = "cop_session"
TOKEN_COOKIE = "cop_tok"


def lang_of(request: Request, user: dict | None = None) -> str:
    q = request.query_params.get("lang")
    if q in i18n.LANGS:
        return q
    if user and user.get("lang") in i18n.LANGS:
        return user["lang"]
    c = request.cookies.get(i18n.COOKIE)
    return c if c in i18n.LANGS else i18n.default_for(user)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("user", request.state.user if hasattr(request.state, "user") else None)
    lang = lang_of(request, ctx["user"])
    ctx["lang"] = lang
    ctx["L"] = lambda fr, en: i18n.pick(lang, fr, en)
    ctx["DN"] = lambda d, key="nom": (d.get(f"{key}_en") or d.get(key)) if lang == "en" else d.get(key)
    ctx["LEVELS"] = i18n.LEVELS_EN if lang == "en" else search.LEVEL_LABELS
    ctx["base"] = config.PUBLIC_BASE
    ctx["path"] = request.url.path
    ctx["SOURCE_URL"] = config.SOURCE_URL
    resp = HTMLResponse(TEMPLATES.get_template(name).render(**ctx))
    if request.query_params.get("lang") in i18n.LANGS:
        resp.set_cookie(i18n.COOKIE, lang, max_age=365 * 86400, samesite="lax")
    return resp


async def set_lang(request: Request):
    """Lien EN · FR : mémorisé dans un cookie et, si connecté, dans le compte."""
    code = request.path_params["code"]
    nxt = request.query_params.get("next") or "/"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    resp = RedirectResponse(nxt, status_code=303)
    if code in i18n.LANGS:
        user = current_user(request)
        if user:
            db.get().execute("UPDATE users SET lang=? WHERE id=?", (code, user["id"]))
            db.get().commit()
        resp.set_cookie(i18n.COOKIE, code, max_age=365 * 86400, samesite="lax")
    return resp


def current_user(request: Request) -> dict | None:
    uid = auth.read_cookie(request.cookies.get(COOKIE))
    if uid:
        return auth.by_id(db.get(), uid)
    bearer = request.headers.get("authorization", "")
    if bearer.lower().startswith("bearer "):
        return auth.by_token(db.get(), bearer[7:].strip())
    return None


def login_required(handler):
    async def wrapper(request: Request):
        user = current_user(request)
        if not user:
            return render(request, "login.html", user=None, next=request.url.path)
        request.state.user = user
        return await handler(request)
    return wrapper


def admin_required(handler):
    async def wrapper(request: Request):
        user = current_user(request)
        if not user or not user.get("is_admin"):
            return Response("Réservé aux administrateurs", status_code=403)
        request.state.user = user
        return await handler(request)
    return wrapper


def contributor_required(handler):
    """Ajout au corpus commun : administrateurs et contributeurs seulement (pas les invités)."""
    async def wrapper(request: Request):
        user = current_user(request)
        if not user:
            return render(request, "login.html", user=None, next=request.url.path)
        if not auth.can_contribute(user):
            return Response("Réservé aux contributeurs : les comptes invités consultent le corpus sans y ajouter de documents.",
                            status_code=403)
        request.state.user = user
        return await handler(request)
    return wrapper


def _session(resp: Response, user: dict, token: str | None = None) -> Response:
    """Session : un an pour un invité (documents publics seulement, sur son téléphone), 30 jours sinon."""
    secure = config.PUBLIC_BASE.startswith("https")
    days = 365 if auth.is_guest(user) else 30
    resp.set_cookie(COOKIE, auth.make_cookie(user["id"], days=days), max_age=days * 86400, httponly=True, secure=secure,
                    samesite="lax")
    if token:
        resp.set_cookie(TOKEN_COOKIE, token, max_age=days * 86400, httponly=True, secure=secure, samesite="strict")
    return resp


def _client(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[-1].strip() if fwd else (request.client.host if request.client else "")


async def login(request: Request):
    """Ancien lien personnel /login/<jeton> (toujours valable)."""
    token = request.path_params["token"]
    user = auth.by_token(db.get(), token)
    if not user:
        return render(request, "login.html", user=None, error=i18n.pick(lang_of(request), "Lien invalide ou révoqué.", "Invalid or revoked link."))
    return _session(RedirectResponse("/" if user.get("password_hash") else "/compte", status_code=303), user, token)


def _send_login_code(user: dict, code: str) -> tuple[bool, bool]:
    """Envoie le code par e-mail et, si le compte est relié, par Telegram → (e-mail parti, Telegram parti)."""
    from . import mailout, telegram

    en = i18n.default_for(user) == "en"
    subject = (f"Your COP31 tracker sign-in code: {code}" if en else f"Votre code de connexion à la veille COP31 : {code}")
    text = (f"Your sign-in code: {code}\n\nIt is valid for {auth.CODE_MINUTES} minutes. If you did not ask for it, ignore this e-mail.\n\n{config.PUBLIC_BASE}/"
            if en else
            f"Votre code de connexion : {code}\n\nValable {auth.CODE_MINUTES} minutes. Si vous ne l'avez pas demandé, ignorez cet e-mail.\n\n{config.PUBLIC_BASE}/")
    mailed = mailout.send(user["email"], subject, text, idempotency_key=f"login-code/{user['id']}/{auth._code_hash(user['id'], code)[:16]}")
    tg = bool(user.get("telegram_chat_id")) and telegram.send(
        user["telegram_chat_id"], (f"🔑 Sign-in code: <b>{code}</b> (valid {auth.CODE_MINUTES} min)" if en
                                   else f"🔑 Code de connexion : <b>{code}</b> (valable {auth.CODE_MINUTES} min)"))
    return mailed, tg


async def login_form(request: Request):
    """Connexion en deux temps : l'e-mail, puis le mot de passe si le compte en a un, sinon un code à 6 chiffres
    envoyé par e-mail (et par Telegram si le compte est relié). Mot de passe oublié : le même code, puis la page
    Compte pour en choisir un nouveau."""
    if request.method == "GET":
        if current_user(request):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", user=None, step="email", email=request.query_params.get("email", ""))
    conn = db.get()
    form = await request.form()
    email = (form.get("email") or "").strip()
    nxt = form.get("next") or "/"
    nxt = nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"
    T = lambda fr, en: i18n.pick(lang_of(request), fr, en)  # noqa: E731
    page = lambda step, **kw: render(request, "login.html", user=None, step=step, email=email, next=nxt, **kw)  # noqa: E731

    if form.get("password"):
        user = auth.check_password(conn, email, form.get("password") or "", _client(request))
        if not user:
            return page("password", error=T("E-mail ou mot de passe incorrect (ou trop d'essais : réessayez dans 15 minutes).",
                                            "Incorrect e-mail or password (or too many attempts: try again in 15 minutes)."))
        return _session(RedirectResponse(nxt, status_code=303), user)

    user = auth.by_email(conn, email)
    if form.get("code") is not None and form.get("action") != "resend":
        key = f"code|{email.lower()}|{_client(request)}"
        if auth.throttled(key) or not auth.check_login_code(conn, user, form.get("code") or ""):
            auth._failures.setdefault(key, []).append(__import__("time").time())
            return page("code", error=T("Code incorrect ou expiré.", "Wrong or expired code."))
        if user.get("password_hash"):  # mot de passe oublié : on propose d'en choisir un nouveau
            return _session(RedirectResponse("/compte?oubli=1", status_code=303), user)
        return _session(RedirectResponse(nxt, status_code=303), user)

    if not user:
        return page("email", error=T("Aucun compte avec cet e-mail : créez-en un depuis la page d'accueil.",
                                     "No account with this e-mail: create one from the home page."))
    if user.get("password_hash") and form.get("action") not in ("forgot", "resend"):
        return page("password")
    code, why = auth.issue_login_code(conn, user)
    if why:
        return page("code", error=T("Un code vient d'être envoyé : patientez une minute avant d'en demander un autre.",
                                    "A code was just sent: wait a minute before asking for another one."))
    mailed, tg = _send_login_code(user, code)
    if not (mailed or tg):
        return page("email", error=T("L'envoi de code n'est pas encore disponible : demandez un lien de connexion à un administrateur.",
                                     "Sign-in codes can't be sent yet: ask an administrator for a sign-in link."))
    where = " + ".join(x for x in (T("par e-mail", "by e-mail") if mailed else "", "Telegram" if tg else "") if x)
    return page("code", sent=where)


async def login_link(request: Request):
    """Lien de connexion signé, 24 h, généré par un administrateur (mot de passe oublié)."""
    user = auth.by_login_link(db.get(), request.path_params["value"])
    if not user:
        return render(request, "login.html", user=None,
                      error=i18n.pick(lang_of(request), "Lien expiré ou déjà utilisé (le mot de passe a changé depuis). Demandez-en un nouveau.",
                                     "Link expired or already used (the password has changed since). Ask for a new one."))
    return _session(RedirectResponse("/compte", status_code=303), user)


async def join(request: Request):
    """Page d'arrivée publique : ce que fait l'outil, puis création d'un compte invité (documents publics, pas de dépôt).

    - `/` et `/join` : inscription libre, e-mail + mot de passe (+ organisation) ;
    - `/join/<code>` (lien ou QR d'invitation) : e-mail (+ organisation) seulement, connexion immédiate."""
    conn = db.get()
    code = (request.path_params.get("code") or request.query_params.get("code") or "").strip().lower()
    if current_user(request):
        return RedirectResponse("/bienvenue", status_code=303)
    stats = search.stats(conn)
    open_ = auth.open_signup_settings(conn)

    def page(**kw):
        lang = lang_of(request)
        if kw.get("error"):
            kw["error"] = i18n.pick(lang, *auth.SIGNUP_ERRORS[kw["error"]])
        kw.setdefault("form", {})
        invited = bool(code) and auth.invite_valid(conn, code)
        return render(request, "join.html", user=None, code=code if invited else "", invited=invited, stats=stats,
                      open_signup=open_["open"] and open_["used"] < open_["max"], **kw)

    if request.method == "GET":
        if code and not auth.invite_valid(conn, code):
            return page(error="bad_code")
        return page()
    form = await request.form()
    code = (form.get("code") or code).strip().lower()
    if form.get("check_code"):  # saisie manuelle d'un code d'invitation
        return RedirectResponse(f"/join/{code}#signup", status_code=303)
    if form.get("website"):  # champ piège invisible : rempli seulement par les robots
        return page(form=form, error="bad_email")
    if not code and form.get("password") != form.get("password2"):
        return page(form=form, error="mismatch")
    user, token, error = auth.signup(conn, code, form.get("email") or "", form.get("organisation") or "",
                                     "" if code else (form.get("password") or ""), _client(request), lang=lang_of(request))
    if error:
        if error == "bad_code":
            code = ""
        return page(form=form, error=error)
    return _session(RedirectResponse("/bienvenue", status_code=303), user, token)


@login_required
async def welcome(request: Request):
    """Bien démarrer : dossiers à suivre, connecteur Claude / ChatGPT, Telegram."""
    from . import live

    conn = db.get()
    user = request.state.user
    saved = False
    if request.method == "POST":
        form = await request.form()
        chosen = set(form.getlist("dossiers"))
        for d in live.dossiers():
            live.follow(conn, user["id"], d["slug"], d["slug"] in chosen)
        topic = (form.get("sujet") or "").strip()
        if topic:
            alerts.add_subscription(conn, user["id"], topic[:60], topic)
        saved = True
    token = auth.mcp_token(user) or request.cookies.get(TOKEN_COOKIE)
    return render(request, "welcome.html", dossiers=live.dossiers(), followed=live.followed(conn, user["id"]),
                  subs=alerts.list_subscriptions(conn, user["id"]), token=token, saved=saved,
                  invite=auth.invite_settings(conn))


STATIC_DIR = Path(__file__).parent / "static"


async def static_file(request: Request):
    """Images du site (photos sous licence, crédit affiché à côté). Noms simples seulement."""
    name = request.path_params["name"]
    path = STATIC_DIR / name
    if not re.fullmatch(r"[a-z0-9-]+\.(jpg|png|webp|svg)", name) or not path.is_file():
        return Response("Inconnu", status_code=404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=604800"})


async def about_page(request: Request):
    """Page publique « Qu'est-ce que COP Tracker ? » : fonctionnement, sources, limites, code ouvert."""
    return render(request, "about.html", user=current_user(request), stats=search.stats(db.get()))


@login_required
async def invite_page(request: Request):
    """Écran à montrer à quelqu'un qu'on croise : QR code + lien court de l'invitation."""
    inv = auth.invite_settings(db.get())
    return render(request, "invite.html", invite=inv)


@login_required
async def account_page(request: Request):
    conn = db.get()
    user = request.state.user
    error = done = None
    if request.method == "POST":
        form = await request.form()
        pw, pw2 = form.get("password") or "", form.get("password2") or ""
        if len(pw) < auth.PASSWORD_MIN:
            error = "short"
        elif pw != pw2:
            error = "mismatch"
        else:
            auth.set_password(conn, user["id"], pw)
            user = auth.by_id(conn, user["id"])
            done = True
    return render(request, "account.html", user=user, error=error, done=done, min=auth.PASSWORD_MIN,
                  forgot=request.query_params.get("oubli") == "1" and not done)


async def logout(request: Request):
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE)
    resp.delete_cookie(TOKEN_COOKIE)
    return resp


async def home(request: Request):
    """Fil de l'utilisateur ; pour un visiteur, la page d'arrivée avec l'inscription."""
    user = current_user(request)
    if not user:
        return await join(request)
    request.state.user = user
    return await _home(request)


async def _home(request: Request):
    conn = db.get()
    user = request.state.user
    notes = conn.execute(
        """SELECT n.id, n.created_at, n.snippet, n.doc_id, s.label, d.title, d.level, d.org, d.symbol, d.access,
                  d.doc_type, d.version, d.url
           FROM notifications n JOIN documents d ON d.id=n.doc_id LEFT JOIN subscriptions s ON s.id=n.subscription_id
           WHERE n.user_id=? ORDER BY n.id DESC LIMIT 60""",
        (user["id"],),
    ).fetchall()
    level = request.query_params.get("niveau")
    filters = {"levels": [level]} if level and level != "R" else ({"access": "restreint"} if level == "R" else {})
    latest = search.latest(conn, user, filters, limit=40)
    last_id = notes[0]["id"] if notes else 0
    subs = alerts.list_subscriptions(conn, user["id"])
    from . import live

    onboarding = not subs and not live.followed(conn, user["id"])
    return render(request, "home.html", notes=notes, latest=latest, last_id=last_id, subs=subs, level=level,
                  stats=search.stats(conn), onboarding=onboarding)


@login_required
async def feed(request: Request):
    """Petit point d'API pour le rafraîchissement automatique."""
    after = int(request.query_params.get("after", "0") or 0)
    n = db.get().execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND id>?",
                         (request.state.user["id"], after)).fetchone()[0]
    return JSONResponse({"nouvelles": n})


@login_required
async def subjects(request: Request):
    conn = db.get()
    user = request.state.user
    message = None
    preview = None
    if request.method == "POST":
        form = await request.form()
        query = (form.get("query") or "").strip()
        levels = form.getlist("levels") or None
        types = form.getlist("types") or None
        if form.get("action") == "apercu":
            preview = alerts.preview(conn, query or "*", levels, types, user, days=int(form.get("jours") or 14))
            return render(request, "subjects.html", subs=alerts.list_subscriptions(conn, user["id"]), preview=preview,
                          form=form)
        if query or types:
            alerts.add_subscription(conn, user["id"], (form.get("label") or "").strip() or query[:60] or "Nouveaux documents",
                                    query or "*", levels, types, telegram_on=bool(form.get("telegram")))
            message = "added"
    return render(request, "subjects.html", subs=alerts.list_subscriptions(conn, user["id"]), message=message,
                  preview=preview, form={})


@login_required
async def subject_delete(request: Request):
    alerts.remove_subscription(db.get(), request.state.user["id"], int(request.path_params["sid"]))
    return RedirectResponse("/sujets", status_code=303)


@login_required
async def search_page(request: Request):
    q = request.query_params.get("q", "").strip()
    levels = request.query_params.getlist("niveau")
    types = request.query_params.getlist("type")
    access = request.query_params.get("acces") or None
    hits = []
    if q:
        hits = search.search(db.get(), q, {"levels": [l for l in levels if l], "doc_types": [t for t in types if t],
                                           "access": access}, request.state.user, limit=25)
    return render(request, "search.html", q=q, hits=hits, levels=levels, types=types, access=access)


@login_required
async def doc_page(request: Request):
    conn = db.get()
    user = request.state.user
    doc_id = int(request.path_params["doc_id"])
    info = search.document(conn, doc_id, user)
    if not info:
        return Response("Document introuvable ou non autorisé", status_code=404)
    per = 40
    hl = request.query_params.get("passage")
    hl = int(hl) if hl and hl.isdigit() else None
    if "p" in request.query_params:
        page = max(1, int(request.query_params.get("p") or 1))
    elif hl:  # ouvrir la tranche qui contient le passage visé
        idx = conn.execute(
            "SELECT COUNT(*) FROM passages WHERE doc_id=? AND seq < (SELECT seq FROM passages WHERE id=? AND doc_id=?)",
            (doc_id, hl, doc_id)).fetchone()[0]
        page = idx // per + 1
    else:
        page = 1
    rows = conn.execute(
        "SELECT id, page, para, text FROM passages WHERE doc_id=? ORDER BY seq LIMIT ? OFFSET ?",
        (doc_id, per, (page - 1) * per),
    ).fetchall()
    drow = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    passages = [dict(r, **search.deep_links(drow, r["page"], r["text"], r["id"])) for r in rows]
    n_pages = -(-(info.get("nb_passages") or 0) // per)
    return render(request, "doc.html", doc=info, passages=passages, p=page, per=per, highlight=hl, n_pages=n_pages)


@login_required
async def diff_page(request: Request):
    conn = db.get()
    user = request.state.user
    new_id = int(request.path_params["doc_id"])
    new = search.document(conn, new_id, user)
    if not new:
        return Response("introuvable", status_code=404)
    old_id = request.query_params.get("avec")
    if not old_id:
        row = conn.execute("SELECT id FROM documents WHERE lineage_key=(SELECT lineage_key FROM documents WHERE id=?) "
                           "AND id<? ORDER BY id DESC LIMIT 1", (new_id, new_id)).fetchone()
        old_id = row["id"] if row else None
    if not old_id or not search.document(conn, int(old_id), user):
        return render(request, "diff.html", doc=new, result=None)
    return render(request, "diff.html", doc=new, result=diff.compare(conn, int(old_id), new_id, max_items=200))


async def file_page(request: Request):
    conn = db.get()
    doc_id = int(request.path_params["doc_id"])
    row = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        return Response("introuvable", status_code=404)
    user = current_user(request)
    signed = auth.check_file_signature(doc_id, request.query_params.get("exp"), request.query_params.get("sig"))
    if not signed and not user:
        return Response("Connexion requise", status_code=401)
    if row["access"] != "public" and not (signed or auth.can_restricted(user)):
        return Response("Non autorisé", status_code=403)
    path = config.DATA_DIR / row["raw_path"]
    media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if path.suffix == ".html":
        media = "text/plain; charset=utf-8"  # ne jamais exécuter du HTML collecté sous notre domaine
    return FileResponse(path, media_type=media, headers={"Content-Disposition": "inline",
                                                         "X-Content-Type-Options": "nosniff"})


@contributor_required
async def upload(request: Request):
    conn = db.get()
    user = request.state.user
    results = []
    if request.method == "POST":
        form = await request.form()
        access = "public" if form.get("access") == "public" else "restreint"
        meta = {k: v for k, v in {"title": (form.get("title") or "").strip() or None, "level": form.get("level") or None,
                                  "doc_type": form.get("doc_type") or None, "org": (form.get("org") or "").strip() or None}.items() if v}
        note = (form.get("note") or "").strip() or None
        for f in form.getlist("files"):
            if not getattr(f, "filename", None):
                continue
            content = await f.read()
            if not content:
                continue
            m = dict(meta)
            if len(form.getlist("files")) > 1:
                m.pop("title", None)
            doc_id, new, status = ingest.ingest_bytes(conn, content, url=None, mime=f.content_type or "", meta=m,
                                                      source_id="depot-web", access=access, uploaded_by=user["id"],
                                                      filename=f.filename, note=note)
            results.append({"name": f.filename, "doc_id": doc_id, "new": new, "status": status})
        url = (form.get("url") or "").strip()
        if url:
            from . import fetch

            res = fetch.Client().get(url)
            if res.ok and not fetch.is_antibot(res):
                doc_id, new, status = ingest.ingest_bytes(conn, res.content, url=res.url, mime=res.mime, meta=meta,
                                                          source_id="depot-web", access=access, uploaded_by=user["id"],
                                                          note=note)
                results.append({"name": url, "doc_id": doc_id, "new": new, "status": status})
            else:
                results.append({"name": url, "error": f"inaccessible ({res.status}) ou protégé : déposer le fichier"})
        alerts.run(conn)
    mine = conn.execute(
        "SELECT id, title, access, fetched_at, status FROM documents WHERE uploaded_by=? ORDER BY id DESC LIMIT 30",
        (user["id"],),
    ).fetchall()
    if request.method == "POST" and (await request.form()).get("new_mail_code"):
        mailin.new_code(conn, user["id"])
        user = auth.by_id(conn, user["id"])
    return render(request, "upload.html", results=results, mine=mine,
                  mail_address=mailin.deposit_address(conn, user))


@login_required
async def telegram_page(request: Request):
    return render(request, "telegram.html")


CLIENTS_DIR = config.ROOT / "clients"


def _zip_dir(folder: Path, arc_root: str = "") -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(folder.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                z.write(f, str(Path(arc_root) / f.relative_to(folder)))
    return buf.getvalue()


@login_required
async def download_page(request: Request):
    """Plugin Claude (zip), skills un par un (zip), instructions ChatGPT (texte)."""
    name = request.path_params["name"]
    plugin = CLIENTS_DIR / "claude-plugin"
    if name == "cop31-plugin.zip":
        data = _zip_dir(plugin)
    elif name.startswith("skill-") and name.endswith(".zip"):
        skill = name[6:-4]
        folder = plugin / "skills" / skill
        if not skill.replace("-", "").isalnum() or not (folder / "SKILL.md").exists():
            return Response("Inconnu", status_code=404)
        data = _zip_dir(folder, skill)
    elif name == "chatgpt-instructions.txt":
        return Response((CLIENTS_DIR / "chatgpt-instructions.md").read_text(encoding="utf-8"),
                        media_type="text/plain; charset=utf-8")
    else:
        return Response("Inconnu", status_code=404)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _skills() -> list[str]:
    folder = CLIENTS_DIR / "claude-plugin" / "skills"
    return sorted(p.name for p in folder.iterdir() if (p / "SKILL.md").exists()) if folder.exists() else []


TEMPLATES.globals["SKILLS"] = _skills


@login_required
async def connector_page(request: Request):
    conn = db.get()
    user = request.state.user
    token = auth.mcp_token(user)
    if not token:  # compte ancien jamais revu depuis le chiffrement : le cookie du navigateur peut le fournir
        cookie = request.cookies.get(TOKEN_COOKIE)
        token = cookie if cookie and auth.by_token(conn, cookie) else None
    if request.method == "POST":
        token = auth.reset_token(conn, user["id"])
        resp = render(request, "connector.html", token=token, regenerated=True)
        resp.set_cookie(TOKEN_COOKIE, token, max_age=30 * 86400, httponly=True,
                        secure=config.PUBLIC_BASE.startswith("https"), samesite="strict")
        return resp
    return render(request, "connector.html", token=token)


@admin_required
async def admin_page(request: Request):
    conn = db.get()
    created = link = None
    if request.method == "POST":
        form = await request.form()
        invite = form.get("invite")
        if invite == "regenerer":
            auth.new_invite_code(conn)
        elif invite == "basculer":
            auth.set_invites(conn, open_=not auth.invite_settings(conn)["open"])
        elif invite == "plafond" and (form.get("max") or "").isdigit():
            auth.set_invites(conn, maximum=int(form.get("max")))
        libre = form.get("libre")
        if libre == "basculer":
            auth.set_open_signup(conn, open_=not auth.open_signup_settings(conn)["open"])
        elif libre == "plafond" and (form.get("max") or "").isdigit():
            auth.set_open_signup(conn, maximum=int(form.get("max")))
        if invite or libre:
            return RedirectResponse("/admin#invitations", status_code=303)
        email = (form.get("email") or "").strip()
        if form.get("link_for"):
            target = auth.by_id(conn, int(form.get("link_for")))
            if target:
                link = {"email": target["email"], "url": auth.login_link(target)}
            email = ""
        if email:
            existing = auth.by_email(conn, email)
            if existing:  # déjà inscrit : nouveau lien de connexion, l'adresse MCP n'est pas révoquée
                uid, token = existing["id"], auth.mcp_token(existing)
            else:
                uid, token = auth.create_user(conn, email, (form.get("name") or "").strip(),
                                              admin=bool(form.get("admin")), restricted=not form.get("public_only"))
            created = {"email": email, "login": auth.login_link(auth.by_id(conn, uid)),
                       "mcp": f"{config.PUBLIC_BASE}/u/{token}/mcp" if token else "inchangée (visible sur sa page Connecteur)"}
    users = conn.execute("SELECT id,email,name,organisation,source,is_admin,can_restricted,"
                         "telegram_chat_id IS NOT NULL AS tg,active,"
                         "password_hash IS NOT NULL AS has_pw,"
                         "(SELECT COUNT(*) FROM subscriptions s WHERE s.user_id=users.id AND s.active=1) AS nsubs "
                         "FROM users ORDER BY id").fetchall()
    sources = conn.execute("SELECT * FROM source_state ORDER BY last_run DESC LIMIT 80").fetchall()
    errors = conn.execute("SELECT * FROM fetch_log WHERE outcome IN ('error','antibot','crash') ORDER BY id DESC LIMIT 30").fetchall()
    return render(request, "admin.html", users=users, created=created, link=link, sources=sources, errors=errors,
                  stats=search.stats(conn), invite=auth.invite_settings(conn),
                  open_signup=auth.open_signup_settings(conn))


@admin_required
async def mail_page(request: Request):
    conn = db.get()
    flash = None
    if request.method == "POST":
        form = await request.form()
        row = conn.execute("SELECT * FROM mail_inbox WHERE id=?", (int(form.get("id") or 0),)).fetchone()
        action = form.get("action")
        if row and action in ("ingest", "allow"):
            msg = mailin.load(row["path"])
            if action == "allow":
                mailin.allow_sender(conn, row["sender"])
            ids = mailin.ingest_newsletter(conn, msg, row["path"], inbox_id=row["id"])
            alerts.run(conn)
            flash = f"{len(ids)} document(s) ajouté(s)" + (f" ; {row['sender']} autorisé" if action == "allow" else "")
        elif row and action == "ignore":
            conn.execute("UPDATE mail_inbox SET status='ignore' WHERE id=?", (row["id"],))
            conn.commit()
        if form.get("allow_new"):
            mailin.allow_sender(conn, form.get("allow_new"))
    show = request.query_params.get("voir")
    detail = None
    if show:
        row = conn.execute("SELECT * FROM mail_inbox WHERE id=? AND mailbox='veille'", (int(show),)).fetchone()
        if row:
            msg = mailin.load(row["path"])
            detail = {"row": row, "text": mailin.body_text(msg)[:6000], "links": mailin.links(msg)}
    return render(request, "mail.html", items=mailin.inbox(conn), detail=detail, flash=flash,
                  veille=mailin.veille_address(), allowed=mailin.allowed_senders(conn))


@admin_required
async def admin_toggle(request: Request):
    conn = db.get()
    uid = int(request.path_params["uid"])
    conn.execute("UPDATE users SET active=1-active WHERE id=? AND id<>?", (uid, request.state.user["id"]))
    conn.commit()
    return RedirectResponse("/admin", status_code=303)


@admin_required
async def admin_delete(request: Request):
    """Suppression définitive d'un compte invité (comptes de test, demandes de suppression)."""
    auth.delete_guest(db.get(), int(request.path_params["uid"]))
    return RedirectResponse("/admin#utilisateurs", status_code=303)


async def health(request: Request):
    db.get().execute("SELECT 1").fetchone()
    return JSONResponse({"ok": True})


routes = [
    Route("/", home, methods=["GET", "POST"]),
    Route("/connexion", login_form, methods=["GET", "POST"]),
    Route("/entrer/{value}", login_link),
    Route("/login/{token}", login),
    Route("/compte", account_page, methods=["GET", "POST"]),
    Route("/lang/{code}", set_lang),
    Route("/join", join, methods=["GET", "POST"]),
    Route("/join/{code}", join, methods=["GET", "POST"]),
    Route("/rejoindre", join, methods=["GET", "POST"]),
    Route("/rejoindre/{code}", join, methods=["GET", "POST"]),
    Route("/bienvenue", welcome, methods=["GET", "POST"]),
    Route("/inviter", invite_page),
    Route("/a-propos", about_page),
    Route("/about", about_page),
    Route("/static/{name}", static_file),
    Route("/logout", logout),
    Route("/api/feed", feed),
    Route("/sujets", subjects, methods=["GET", "POST"]),
    Route("/sujets/{sid:int}/supprimer", subject_delete, methods=["POST"]),
    Route("/recherche", search_page),
    Route("/doc/{doc_id:int}", doc_page),
    Route("/diff/{doc_id:int}", diff_page),
    Route("/fichier/{doc_id:int}", file_page),
    Route("/deposer", upload, methods=["GET", "POST"]),
    Route("/telegram", telegram_page),
    Route("/connecteur", connector_page, methods=["GET", "POST"]),
    Route("/telechargements/{name}", download_page),
    Route("/admin", admin_page, methods=["GET", "POST"]),
    Route("/admin/utilisateur/{uid:int}/basculer", admin_toggle, methods=["POST"]),
    Route("/admin/utilisateur/{uid:int}/supprimer", admin_delete, methods=["POST"]),
    Route("/admin/courrier", mail_page, methods=["GET", "POST"]),
    Route("/healthz", health),
]
