"""Site web : accueil, connecter un LLM, planifier ma journée, mes sujets, à propos (comment ça marche, base de
données, contact), compte, documents et versions, administration. Anglais par défaut, français au choix."""

from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import alerts, auth, config, db, diff, i18n, mailin, qr, search

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


def _safe_url(url: str | None) -> str:
    """Lien venu d'une source tierce (programmes, index) : http(s) seulement, jamais javascript: ni data:."""
    url = (url or "").strip()
    return url if re.match(r"https?://", url, re.I) else ""


TEMPLATES.filters["safe_url"] = _safe_url
TEMPLATES.globals["BOT"] = config.TELEGRAM_BOT_NAME
DOC_TYPES = ["traite", "decision", "conclusions_sb", "ordre_du_jour", "projet_texte", "ndc", "btr", "soumission", "declaration", "rapport_synthese",
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
    ctx["here"] = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    ctx["SOURCE_URL"] = config.SOURCE_URL
    if not ctx["user"] and "open_signup" not in ctx:
        try:
            o = auth.open_signup_settings(db.get())
            ctx["open_signup"] = o["open"] and o["used"] < o["max"]
        except Exception:
            ctx["open_signup"] = False
    resp = HTMLResponse(TEMPLATES.get_template(name).render(**ctx))
    if request.query_params.get("lang") in i18n.LANGS:
        resp.set_cookie(i18n.COOKIE, lang, max_age=365 * 86400, samesite="lax")
    return resp


async def set_lang(request: Request):
    """Lien EN · FR : mémorisé dans un cookie et, si connecté, dans le compte."""
    code = request.path_params["code"]
    nxt = _safe_next(request.query_params.get("next"))
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
    return None


def _next_url(request: Request) -> str:
    q = request.url.query
    return request.url.path + (f"?{q}" if q else "")


def login_required(handler):
    """Page réservée aux comptes : sinon l'accueil, avec la fenêtre de connexion ouverte et un retour ici."""
    async def wrapper(request: Request):
        user = current_user(request)
        if not user:
            from urllib.parse import quote

            back = _next_url(request) if request.method == "GET" else "/"
            return RedirectResponse(f"/?signin=1&next={quote(back)}", status_code=303)
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
    return _session(RedirectResponse("/" if user.get("password_hash") else "/account", status_code=303), user, token)


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


def _wants_json(request: Request) -> bool:
    """Formulaires de la fenêtre de connexion : envoyés en arrière-plan, réponse JSON (l'erreur reste dans la fenêtre)."""
    return request.headers.get("x-requested-with") == "fetch"


_LOCAL_PATH = re.compile(r"/(?![/\\])[^\s\\\x00-\x1f]*")


def _safe_next(value: str | None) -> str:
    """Adresse de retour : un chemin du site seulement (ni « //hôte », ni « /\\hôte », ni caractère de contrôle,
    que les navigateurs traitent comme une autre origine)."""
    value = value or "/"
    return value if _LOCAL_PATH.fullmatch(value) else "/"


def _go(request: Request, url: str, user: dict | None = None, token: str | None = None) -> Response:
    resp = JSONResponse({"redirect": url}) if _wants_json(request) else RedirectResponse(url, status_code=303)
    return _session(resp, user, token) if user else resp


async def login_form(request: Request):
    """Connexion en deux temps : l'e-mail, puis le mot de passe si le compte en a un, sinon un code à 6 chiffres
    envoyé par e-mail (et par Telegram si le compte est relié). Mot de passe oublié : le même code, puis la page
    Compte pour en choisir un nouveau. Utilisée par la fenêtre de connexion (JSON) et par la page /signin."""
    if request.method == "GET":
        if current_user(request):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", user=None, step="email", email=request.query_params.get("email", ""),
                      next=_safe_next(request.query_params.get("next")))
    conn = db.get()
    form = await request.form()
    email = (form.get("email") or "").strip()
    nxt = _safe_next(form.get("next"))
    T = lambda fr, en: i18n.pick(lang_of(request), fr, en)  # noqa: E731

    def page(step, **kw):
        if _wants_json(request):
            return JSONResponse({"step": step, "email": email, **{k: v for k, v in kw.items() if v}})
        return render(request, "login.html", user=None, step=step, email=email, next=nxt, **kw)

    if form.get("password"):
        user = auth.check_password(conn, email, form.get("password") or "", _client(request))
        if not user:
            return page("password", error=T("E-mail ou mot de passe incorrect (ou trop d'essais : réessayez dans 15 minutes).",
                                            "Incorrect e-mail or password (or too many attempts: try again in 15 minutes)."))
        return _go(request, nxt, user)

    user = auth.by_email(conn, email)
    if form.get("code") is not None and form.get("action") != "resend":
        key = f"code|{email.lower()}|{_client(request)}"
        if auth.throttled(key) or not auth.check_login_code(conn, user, form.get("code") or ""):
            auth._failures.setdefault(key, []).append(__import__("time").time())
            return page("code", error=T("Code incorrect ou expiré.", "Wrong or expired code."))
        if user.get("password_hash"):  # mot de passe oublié : on propose d'en choisir un nouveau
            return _go(request, "/account?forgot=1", user)
        return _go(request, nxt, user)

    if not user:
        return page("email", error=T("Aucun compte avec cet e-mail : créez-en un (onglet « Créer un compte »).",
                                     "No account with this e-mail: create one (Create account tab)."))
    if user.get("password_hash") and form.get("action") not in ("forgot", "resend"):
        return page("password")
    code, why = auth.issue_login_code(conn, user)
    if why:
        return page("code", error=T("Un code vient d'être envoyé : patientez une minute avant d'en demander un autre.",
                                    "A code was just sent: wait a minute before asking for another one."))
    from starlette.concurrency import run_in_threadpool

    mailed, tg = await run_in_threadpool(_send_login_code, user, code)
    if not (mailed or tg):
        return page("email", error=T("L'envoi de code n'est pas encore disponible : écrivez-nous (page Contact).",
                                     "Sign-in codes can't be sent yet: write to us (Contact page)."))
    where = " + ".join(x for x in (T("par e-mail", "by e-mail") if mailed else "", "Telegram" if tg else "") if x)
    return page("code", sent=where)


async def login_link(request: Request):
    """Lien de connexion signé, 24 h, généré par un administrateur (mot de passe oublié)."""
    user = auth.by_login_link(db.get(), request.path_params["value"])
    if not user:
        return render(request, "login.html", user=None,
                      error=i18n.pick(lang_of(request), "Lien expiré ou déjà utilisé (le mot de passe a changé depuis). Demandez-en un nouveau.",
                                     "Link expired or already used (the password has changed since). Ask for a new one."))
    return _session(RedirectResponse("/account", status_code=303), user)


async def join(request: Request):
    """Création d'un compte invité (corpus public, pas de dépôt), depuis la fenêtre de l'accueil.

    - `/join` (POST) : inscription libre, e-mail + mot de passe (+ organisation) ;
    - `/join/<code>` (lien ou QR d'invitation) : l'accueil s'ouvre sur la fenêtre d'inscription ; e-mail (+ organisation)
      seulement, connexion immédiate."""
    conn = db.get()
    code = (request.path_params.get("code") or request.query_params.get("code") or "").strip().lower()
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    lang = lang_of(request)
    if request.method == "GET":
        if code and not auth.invite_valid(conn, code):
            return await _landing(request, None, signup_error=i18n.pick(lang, *auth.SIGNUP_ERRORS["bad_code"]))
        return await _landing(request, None, invite_code=code if code else "", open_modal="signup")
    form = await request.form()
    code = (form.get("code") or code).strip().lower()

    async def fail(err: str):
        msg = i18n.pick(lang, *auth.SIGNUP_ERRORS[err])
        invited = bool(code) and auth.invite_valid(conn, code)
        if _wants_json(request):
            return JSONResponse({"error": msg, "invited": invited})
        return await _landing(request, None, invite_code=code if invited else "", open_modal="signup",
                              signup_error=msg, form=form)

    if form.get("check_code"):  # saisie manuelle d'un code d'invitation
        if not auth.invite_valid(conn, code):
            return await fail("bad_code")
        return _go(request, f"/join/{code}")
    if form.get("website"):  # champ piège invisible : rempli seulement par les robots
        return await fail("bad_email")
    if not code and form.get("password") != form.get("password2"):
        return await fail("mismatch")
    user, token, error = auth.signup(conn, code, form.get("email") or "", form.get("organisation") or "",
                                     "" if code else (form.get("password") or ""), _client(request), lang=lang)
    if error:
        return await fail(error)
    nxt = _safe_next(form.get("next"))
    return _go(request, nxt if nxt != "/" else "/?welcome=1", user, token)


STATIC_DIR = Path(__file__).parent / "static"


async def static_file(request: Request):
    """Images du site (photos sous licence, crédit affiché à côté). Noms simples seulement."""
    name = request.path_params["name"]
    path = STATIC_DIR / name
    if not re.fullmatch(r"[a-z0-9-]+\.(jpg|png|webp|svg)", name) or not path.is_file():
        return Response("Inconnu", status_code=404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=604800"})


# --- pages publiques ---------------------------------------------------------------------------------------------

def _onboarding(conn, user: dict) -> dict[str, bool]:
    from . import live

    return {"llm": bool(user.get("mcp_seen_at")),
            "topics": bool(alerts.list_subscriptions(conn, user["id"]) or live.followed(conn, user["id"])),
            "telegram": bool(user.get("telegram_chat_id"))}


async def _landing(request: Request, user: dict | None, invite_code: str = "", open_modal: str = "",
                   signup_error: str = "", form=None) -> HTMLResponse:
    """Accueil, connecté ou non : les trois usages, l'appel à l'action principal, et la fenêtre de connexion."""
    conn = db.get()
    open_ = auth.open_signup_settings(conn)
    if not open_modal and not user:
        if request.query_params.get("signin"):
            open_modal = "signin"
        elif request.query_params.get("signup"):
            open_modal = "signup"
    from . import demo

    return render(request, "home.html", user=user, stats=search.stats(conn),
                  counts=None if user else _corpus_counts(conn), demo_on=demo.enabled(),
                  steps=_onboarding(conn, user) if user else None, welcome=bool(request.query_params.get("welcome")),
                  invite_code=invite_code, open_modal=open_modal, signup_error=signup_error, form=form or {},
                  open_signup=open_["open"] and open_["used"] < open_["max"],
                  next=_safe_next(request.query_params.get("next")))


async def home(request: Request):
    user = current_user(request)
    if user:
        request.state.user = user
    return await _landing(request, user)


async def about_page(request: Request):
    """À propos : qu'est-ce que COP Tracker (court, renvoie vers les autres pages)."""
    return render(request, "about.html", user=current_user(request), stats=search.stats(db.get()))


async def how_page(request: Request):
    """Comment ça marche : schéma et explications pour un non-développeur, forces et limites."""
    from . import catalog

    conn = db.get()
    try:
        n_sources = len(catalog.load())
    except Exception:
        n_sources = None
    pav = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='pav_events'").fetchone()[0]
    n_events = conn.execute("SELECT COUNT(*) FROM pav_events WHERE cancelled=0").fetchone()[0] if pav else None
    return render(request, "how.html", user=current_user(request), stats=search.stats(conn), n_sources=n_sources,
                  n_events=n_events)


_contact_hits: dict[str, list[float]] = {}


async def contact_page(request: Request):
    """Contact : formulaire court ; le message part par e-mail vers COP_CONTACT_EMAIL (adresse jamais affichée)."""
    import time

    from . import mailout

    user = current_user(request)
    sent = error = None
    form: dict = {}
    if request.method == "POST":
        form = dict(await request.form())
        T = lambda fr, en: i18n.pick(lang_of(request, user), fr, en)  # noqa: E731
        ip = _client(request)
        now = time.time()
        recent = [t for t in _contact_hits.get(ip, []) if now - t < 3600]
        message = (form.get("message") or "").strip()
        reply = (form.get("email") or (user or {}).get("email") or "").strip()
        if form.get("website"):  # champ piège : un robot
            sent = True
        elif len(recent) >= 5:
            error = T("Trop de messages envoyés : réessayez plus tard.", "Too many messages: try again later.")
        elif len(message) < 10:
            error = T("Le message est trop court.", "Your message is too short.")
        elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", reply):
            error = T("Indiquez une adresse e-mail valide pour la réponse.", "Please give a valid e-mail address for the reply.")
        elif not config.CONTACT_EMAIL or not mailout.enabled():
            error = T("Le formulaire n'est pas encore configuré.", "The contact form is not configured yet.")
        else:
            name = re.sub(r"[\x00-\x1f\x7f]+", " ", form.get("name") or "").strip()[:80]
            body = (f"From: {name} <{reply}>\nAccount: {(user or {}).get('email') or '—'}\n"
                    f"Organisation: {(form.get('organisation') or '').strip()[:120] or '—'}\n\n{message[:5000]}")
            from starlette.concurrency import run_in_threadpool

            ok = await run_in_threadpool(lambda: mailout.send(config.CONTACT_EMAIL, f"[COP Tracker] {name or reply}"[:120],
                                                              body, reply_to=reply))
            _contact_hits[ip] = recent + [now]
            sent = ok or None
            if not ok:
                error = T("L'envoi a échoué : réessayez dans un moment.", "Sending failed: please try again in a moment.")
    return render(request, "contact.html", user=user, sent=sent, error=error, form=form)


# --- connecter un LLM ---------------------------------------------------------------------------------------------

def _token_for(request: Request, conn, user: dict) -> str | None:
    token = auth.mcp_token(user)
    if not token:  # compte ancien jamais revu depuis le chiffrement : le cookie du navigateur peut le fournir
        cookie = request.cookies.get(TOKEN_COOKIE)
        token = cookie if cookie and auth.by_token(conn, cookie) else None
    return token


SCREENSHOTS = ("connect-claude", "connect-chatgpt", "connect-lechat", "connect-example")


def _shots() -> dict[str, str]:
    """Captures d'écran fournies (static/<nom>.png|.jpg|.webp), affichées si présentes."""
    out = {}
    for name in SCREENSHOTS:
        for ext in ("png", "jpg", "webp"):
            if (STATIC_DIR / f"{name}.{ext}").is_file():
                out[name] = f"/static/{name}.{ext}"
                break
    return out


async def connect_page(request: Request):
    conn = db.get()
    user = current_user(request)
    token = regenerated = None
    if user:
        request.state.user = user
        token = _token_for(request, conn, user)
        if request.method == "POST":
            token = auth.reset_token(conn, user["id"])
            regenerated = True
    resp = render(request, "connect.html", user=user, token=token, regenerated=regenerated, shots=_shots())
    if regenerated:
        resp.set_cookie(TOKEN_COOKIE, token, max_age=30 * 86400, httponly=True,
                        secure=config.PUBLIC_BASE.startswith("https"), samesite="strict")
    return resp


# --- planifier ma journée -----------------------------------------------------------------------------------------

def _today_day() -> int:
    from datetime import datetime

    from . import agenda

    d = datetime.now(agenda.ANTALYA).date()
    if (d.year, d.month) == (agenda.YEAR, agenda.MONTH) and agenda.FIRST_DAY <= d.day <= agenda.LAST_DAY:
        return d.day
    return agenda.FIRST_DAY


async def plan_page(request: Request):
    """Calendrier géant : tous les événements connus, filtrables (jour, mots, type, zone, pavillon, mes sujets)."""
    from . import events

    conn = db.get()
    user = current_user(request)
    if user:
        request.state.user = user
    qp = request.query_params
    day_s = qp.get("day") or ""
    day = int(day_s) if day_s.isdigit() and int(day_s) in events.DAYS else _today_day()
    kinds = [k for k in qp.getlist("type") if k in events.KINDS]
    zone = qp.get("zone") if qp.get("zone") in ("blue", "green") else ""
    place = (qp.get("place") or "").strip()
    q = (qp.get("q") or "").strip()[:200]
    mine = bool(qp.get("mine")) and bool(user)
    terms = events.topic_terms(conn, user) if mine else None
    res = events.query(conn, day, q=q, kinds=kinds, zone=zone, place=place, terms=terms)
    return render(request, "plan.html", user=user, day=day, days=events.DAYS, kinds=kinds, zone=zone, place=place,
                  q=q, mine=mine, no_terms=mine and not terms, res=res, slots=events.by_slot(res["events"]),
                  KINDS=events.KINDS, today=_today_day())


# --- mes sujets ---------------------------------------------------------------------------------------------------

@login_required
async def topics_page(request: Request):
    from datetime import datetime, timedelta, timezone

    from . import live, topics

    conn = db.get()
    user = request.state.user
    lang = lang_of(request, user)
    message = None
    preview = None
    form: dict = {}
    if request.method == "POST":
        f = await request.form()
        action = f.get("action")
        if action == "dossiers":
            chosen = set(f.getlist("dossiers"))
            for d in live.dossiers():
                live.follow(conn, user["id"], d["slug"], d["slug"] in chosen)
            message = "saved"
        elif action in ("add", "preview"):
            query = (f.get("query") or "").strip()
            levels = f.getlist("levels") or None
            types = f.getlist("types") or None
            if action == "preview":
                preview = alerts.preview(conn, query or "*", levels, types, user, days=14)
                form = {"query": query, "label": f.get("label") or ""}
            elif query or types:
                alerts.add_subscription(conn, user["id"], (f.get("label") or "").strip() or query[:60] or "New documents",
                                        query or "*", levels, types)
                message = "added"
        if message:
            return RedirectResponse(f"/topics?ok={message}", status_code=303)
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    news = topics.whats_new(conn, user, since, lang)
    followed = live.followed(conn, user["id"])
    return render(request, "topics.html", dossiers=live.dossiers(), followed=followed,
                  subs=alerts.list_subscriptions(conn, user["id"]), news=news, preview=preview, form=form,
                  message=request.query_params.get("ok"))


@login_required
async def topic_delete(request: Request):
    alerts.remove_subscription(db.get(), request.state.user["id"], int(request.path_params["sid"]))
    return RedirectResponse("/topics?ok=removed", status_code=303)


# --- base de données ----------------------------------------------------------------------------------------------

@login_required
async def database_page(request: Request):
    """Base de données : recherche, dossiers de négociation, textes de session, derniers documents."""
    from . import live

    conn = db.get()
    user = request.state.user
    qp = request.query_params
    tab = qp.get("tab") if qp.get("tab") in ("search", "negotiations", "texts", "latest") else "search"
    q = (qp.get("q") or "").strip()
    levels = [x for x in qp.getlist("niveau") if x]
    types = [x for x in qp.getlist("type") if x]
    ctx: dict = {"tab": tab, "q": q, "levels": levels, "types": types}
    if tab == "search" and q:
        ctx["hits"] = search.search(conn, q, {"levels": levels, "doc_types": types}, user, limit=25)
    elif tab == "negotiations":
        ctx["rows"] = live.overview(conn, user)
    elif tab == "texts":
        ctx["dossier"] = qp.get("dossier") or ""
        ctx["texts"] = live.texts(conn, ctx["dossier"] or None, limit=300)
        ctx["names"] = {d["slug"]: (d.get("nom_en") or d["nom"]) if lang_of(request, user) == "en" else d["nom"]
                        for d in live.dossiers()}
        ctx["credit"], ctx["cb_page"] = live.CB_CREDIT, live.CB_TEXTS_PAGE
        for t in ctx["texts"]:
            t["stage"] = live.text_stage(t)
    elif tab == "latest":
        level = qp.get("niveau")
        ctx["level"] = level
        ctx["latest"] = search.latest(conn, user, {"levels": [level]} if level else {}, limit=60)
    ctx["stats"] = search.stats(conn)
    return render(request, "database.html", **ctx)


async def account_redirect(request: Request):
    return RedirectResponse("/account" + (f"?{request.url.query}" if request.url.query else ""), status_code=303)


@login_required
async def account_page(request: Request):
    conn = db.get()
    user = request.state.user
    error = done = None
    if request.method == "POST":
        form = await request.form()
        action = form.get("action") or "password"
        if action == "profile":
            conn.execute("UPDATE users SET name=?, organisation=? WHERE id=?",
                         ((form.get("name") or "").strip()[:80] or None, (form.get("organisation") or "").strip()[:120] or None,
                          user["id"]))
            conn.commit()
            done = "profile"
        elif action == "telegram_unlink":
            import secrets

            # nouveau code : l'ancien (affiché dans le lien t.me) ne doit plus pouvoir relier un autre téléphone
            conn.execute("UPDATE users SET telegram_chat_id=NULL, telegram_code=? WHERE id=?",
                         (secrets.token_hex(4), user["id"]))
            conn.commit()
            done = "telegram"
        elif action == "delete":
            if (form.get("confirm") or "").strip().lower() in ("delete", "supprimer") and auth.delete_guest(conn, user["id"]):
                return await logout(request)
            error = "delete"
        else:
            pw, pw2 = form.get("password") or "", form.get("password2") or ""
            if len(pw) < auth.PASSWORD_MIN:
                error = "short"
            elif pw != pw2:
                error = "mismatch"
            else:
                auth.set_password(conn, user["id"], pw)
                done = "password"
        user = auth.by_id(conn, user["id"])
        request.state.user = user
    return render(request, "account.html", user=user, error=error, done=done, min=auth.PASSWORD_MIN,
                  forgot=request.query_params.get("forgot") == "1" and not done, guest=auth.is_guest(user),
                  token=_token_for(request, conn, user), invite=auth.invite_settings(conn))


async def logout(request: Request):
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE)
    resp.delete_cookie(TOKEN_COOKIE)
    return resp


@login_required
async def invite_page(request: Request):
    """Écran à montrer à quelqu'un qu'on croise : QR code + lien court de l'invitation."""
    inv = auth.invite_settings(db.get())
    return render(request, "invite.html", invite=inv)


def _redirect(target: str):
    async def handler(request: Request):
        url, _, frag = target.format(**request.path_params).partition("#")
        q = request.url.query
        url += (("&" if "?" in url else "?") + q) if q else ""
        return RedirectResponse(url + ("#" + frag if frag else ""), status_code=301)
    return handler


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
    elif name in ("chatgpt-instructions.txt", "mistral-instructions.txt"):
        return Response((CLIENTS_DIR / name.replace(".txt", ".md")).read_text(encoding="utf-8"),
                        media_type="text/plain; charset=utf-8")
    else:
        return Response("Inconnu", status_code=404)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _skills() -> list[str]:
    folder = CLIENTS_DIR / "claude-plugin" / "skills"
    return sorted(p.name for p in folder.iterdir() if (p / "SKILL.md").exists()) if folder.exists() else []


TEMPLATES.globals["SKILLS"] = _skills


SEORS_ORIGIN = "https://seors.unfccc.int"


@admin_required
async def admin_programme(request: Request):
    """Programme : état des relevés SEORS, bouton de relevé depuis le navigateur, import d'un relevé collé."""
    from . import agenda

    conn = db.get()
    user = request.state.user
    result = error = None
    if request.method == "POST":
        form = await request.form()
        text = (form.get("releve") or "").strip()
        try:
            result = agenda.import_seors(conn, json.loads(text) if text.startswith("{") else agenda.parse_seors_tsv(text))
        except (ValueError, KeyError, IndexError) as exc:
            error = str(exc)
    link = agenda.seors_bookmarklet(f"{config.PUBLIC_BASE}/admin/seors/import", auth.import_token(user))
    return render(request, "programme_admin.html", user=user, status=agenda.seors_status(conn), bookmarklet=link,
                  seors_url=agenda.SEORS_LIST, result=result, error=error)


async def seors_import(request: Request):
    """Relevé envoyé par le bouton depuis la page SEORS (autre origine : jeton du compte, réponse CORS limitée à
    seors.unfccc.int)."""
    from . import agenda

    cors = {"Access-Control-Allow-Origin": SEORS_ORIGIN, "Access-Control-Allow-Methods": "POST",
            "Access-Control-Allow-Headers": "Authorization, Content-Type", "Vary": "Origin"}
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=cors)
    # Jeton dédié à cet import (signé, propre à l'administrateur), jamais l'adresse MCP : le bouton s'exécute sur la
    # page SEORS, d'où un script tiers pourrait le lire.
    bearer = request.headers.get("authorization", "")
    user = auth.check_import_token(db.get(), bearer[7:].strip()) if bearer.lower().startswith("bearer ") else None
    if not user or not user.get("is_admin"):
        return JSONResponse({"erreur": "réservé aux administrateurs"}, status_code=403, headers=cors)
    body = (await request.body())[:5_000_000].decode("utf-8", "replace")
    try:
        out = agenda.import_seors(db.get(), agenda.parse_seors_tsv(body))
    except (ValueError, KeyError, IndexError) as exc:
        return JSONResponse({"erreur": str(exc)}, status_code=400, headers=cors)
    return JSONResponse({"importes": out["importes"], "releve": out["releve"],
                         "salle_ou_horaire_change": len(out["salle_ou_horaire_change"])}, headers=cors)


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
                         "FROM users WHERE COALESCE(source,'')<>'supprime' ORDER BY id").fetchall()
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
    conn.execute("UPDATE users SET active=1-active WHERE id=? AND id<>? AND COALESCE(source,'')<>'supprime'",
                 (uid, request.state.user["id"]))
    conn.commit()
    return RedirectResponse("/admin", status_code=303)


@admin_required
async def admin_delete(request: Request):
    """Suppression définitive d'un compte invité (comptes de test, demandes de suppression)."""
    auth.delete_guest(db.get(), int(request.path_params["uid"]))
    return RedirectResponse("/admin#utilisateurs", status_code=303)


# --- démo publique « Essayer » et pages par public -----------------------------------------------------------------

DEMO_COOKIE = "cop_demo"
_demo_busy: set[str] = set()


DEMO_MAX_CONCURRENT = 4   # questions traitées en même temps, tous visiteurs confondus (coût, API)


def _demo_token_ok(value: str) -> bool:
    """Cookie de démo signé par le serveur : un cookie fabriqué ne remet pas le compteur à zéro."""
    import hmac

    token, _, sig = (value or "").partition(".")
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{16,40}", token)) and hmac.compare_digest(auth._sign(f"demo:{token}")[:16], sig)


def _demo_session(request: Request) -> tuple[str, bool]:
    import secrets

    value = request.cookies.get(DEMO_COOKIE) or ""
    if _demo_token_ok(value):
        return value, False
    token = secrets.token_urlsafe(18)
    return f"{token}.{auth._sign(f'demo:{token}')[:16]}", True


def _set_demo_cookie(resp: Response, session: str) -> Response:
    resp.set_cookie(DEMO_COOKIE, session, max_age=30 * 86400, httponly=True,
                    secure=config.PUBLIC_BASE.startswith("https"), samesite="lax")
    return resp


DEMO_REFUSALS = {
    "disabled": ("La démo est momentanément indisponible.", "The demo is temporarily unavailable."),
    "budget": ("La démo a atteint son plafond du jour : revenez demain, ou connectez l'outil à votre LLM (gratuit, sans limite).",
               "The demo has reached today's limit: come back tomorrow, or connect the tool to your LLM (free, no limit)."),
    "session": ("Vous avez utilisé vos 3 questions d'essai. Pour continuer sans limite : connectez l'outil à votre LLM (gratuit).",
                "You have used your 3 trial questions. To keep going without limits: connect the tool to your LLM (free)."),
    "ip": ("Limite quotidienne atteinte depuis cette connexion : connectez l'outil à votre LLM pour continuer (gratuit).",
           "Daily limit reached from this connection: connect the tool to your LLM to keep going (free)."),
    "account": ("Vous avez utilisé vos 3 questions d'essai. Pour continuer sans limite : connectez l'outil à votre LLM.",
                "You have used your 3 trial questions. To keep going without limits: connect the tool to your LLM."),
    "busy": ("Une question est déjà en cours : attendez sa réponse.", "A question is already running: wait for its answer."),
    "crowded": ("Beaucoup de questions en même temps : réessayez dans une minute.", "Many questions at once: please try again in a minute."),
    "short": ("Posez une question un peu plus précise.", "Please ask a slightly more specific question."),
}


def _demo_public(payload: dict) -> dict:
    return {k: payload.get(k) for k in ("html", "sources", "events", "checks", "trace", "model")}


async def demo_page(request: Request):
    """Essayer : quelques questions sans compte, avec exemples par public, réponse sourcée et vérifiée."""
    from . import demo

    conn = db.get()
    user = current_user(request)
    if user:
        request.state.user = user
    session, new = _demo_session(request)
    who = request.query_params.get("pour") or request.query_params.get("for") or ""
    who = who if who in demo.EXAMPLES else "delegations"
    resp = render(request, "try.html", user=user, quota=demo.quota(conn, session, _client(request), user),
                  examples=demo.EXAMPLES, who=who, prefill=(request.query_params.get("q") or "")[:demo.MAX_QUESTION],
                  shared=None, as_of=demo.corpus_as_of(conn), refusals=DEMO_REFUSALS)
    return _set_demo_cookie(resp, session) if new else resp


async def demo_answer_page(request: Request):
    """Lien permanent d'une réponse de la démo (à partager, à citer)."""
    from . import demo

    conn = db.get()
    row = demo.load(conn, request.path_params["aid"])
    if not row:
        return Response("Réponse introuvable / answer not found", status_code=404)
    user = current_user(request)
    session, new = _demo_session(request)
    payload = _demo_public(row["payload"])
    payload["html"] = str(payload["html"])
    payload["sources"] = demo.sign_links(conn, payload["sources"])
    resp = render(request, "try.html", user=user, quota=demo.quota(conn, session, _client(request), user),
                  examples=demo.EXAMPLES, who="delegations", prefill="",
                  shared={"id": row["id"], "question": row["question"], "created_at": row["created_at"], **payload},
                  as_of=demo.corpus_as_of(conn), refusals=DEMO_REFUSALS)
    return _set_demo_cookie(resp, session) if new else resp


async def demo_ask(request: Request):
    """Question de la démo → flux SSE : étapes (outils appelés), puis la réponse rendue et vérifiée."""
    import asyncio
    import threading

    from starlette.responses import StreamingResponse

    from . import demo

    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in (config.PUBLIC_BASE, f"{request.url.scheme}://{request.url.netloc}"):
        return JSONResponse({"error": "origin"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    user = current_user(request)
    lang = body.get("lang") if body.get("lang") in i18n.LANGS else lang_of(request, user)
    T = lambda key: i18n.pick(lang, *DEMO_REFUSALS[key])  # noqa: E731
    session = request.cookies.get(DEMO_COOKIE) or ""
    if body.get("website") or not _demo_token_ok(session):  # robot : piège, ou cookie absent ou fabriqué
        return JSONResponse({"error": T("disabled"), "reason": "session"}, status_code=403)
    question = re.sub(r"\s+", " ", str(body.get("question") or "")).strip()[:demo.MAX_QUESTION]
    if len(question) < 6:
        return JSONResponse({"error": T("short"), "reason": "short"}, status_code=400)
    ip = _client(request)
    conn = db.get()
    q = demo.quota(conn, session, ip, user)
    if q["remaining"] <= 0:
        return JSONResponse({"error": T(q["reason"]), "reason": q["reason"]}, status_code=429)
    follow = None
    if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", str(body.get("follow") or "")):
        follow = demo.follow_context(conn, body["follow"], session)
    keys = {f"s:{session}", f"ip:{demo.ip_hash(ip)}"}
    if keys & _demo_busy:
        return JSONResponse({"error": T("busy"), "reason": "busy"}, status_code=429)
    if len(_demo_busy) >= 2 * DEMO_MAX_CONCURRENT:
        return JSONResponse({"error": T("crowded"), "reason": "crowded"}, status_code=429)
    _demo_busy.update(keys)
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(ev):
        loop.call_soon_threadsafe(queue.put_nowait, ev)

    def work():
        try:
            res = demo.answer(question, lang, emit, follow=follow)
            aid = demo.save(db.get(), question=question, lang=lang, session=session, ip=ip, user=user, payload=res)
            left = demo.quota(db.get(), session, ip, user)
            pub = _demo_public(res)
            pub["sources"] = demo.sign_links(db.get(), pub["sources"])
            emit({"type": "done", "id": aid, "url": f"/try/r/{aid}", **pub,
                  "remaining": left["remaining"], "reason": left["reason"]})
        except Exception as exc:  # API indisponible, quota OpenAI…
            try:
                demo.save(db.get(), question=question, lang=lang, session=session, ip=ip, user=user, payload=None,
                          error=f"{type(exc).__name__}: {exc}"[:500])
            except Exception:
                pass
            emit({"type": "error", "message": i18n.pick(lang, "La réponse n'a pas pu être produite : réessayez dans un moment.",
                                                        "The answer could not be produced: please try again in a moment.")})
        finally:
            _demo_busy.difference_update(keys)
            emit(None)

    threading.Thread(target=work, daemon=True).start()

    async def stream():
        yield ": ok\n\n"
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


AUDIENCES = ("delegations", "presse", "militants")
AUDIENCE_SLUGS = {"delegations": "delegations", "press": "presse", "presse": "presse", "activists": "militants",
                  "militants": "militants"}


async def audience_page(request: Request):
    """Pages par public : petites délégations, presse indépendante, militants et ONG."""
    from . import demo

    who = AUDIENCE_SLUGS.get(request.path_params["who"])
    if not who:
        return Response("Inconnu", status_code=404)
    user = current_user(request)
    if user:
        request.state.user = user
    return render(request, "audience.html", user=user, who=who, examples=demo.EXAMPLES[who],
                  counts=_corpus_counts(db.get()), demo_on=demo.enabled())


_COUNTS: dict[str, object] = {"at": 0.0, "value": None}


def _corpus_counts(conn) -> dict[str, int]:
    """Chiffres de la vitrine (documents publics seulement), recalculés au plus toutes les 10 minutes."""
    import time

    if _COUNTS["value"] is None or time.time() - float(_COUNTS["at"]) > 600:
        _COUNTS["value"] = _compute_counts(conn)
        _COUNTS["at"] = time.time()
    return _COUNTS["value"]  # type: ignore[return-value]


def _compute_counts(conn) -> dict[str, int]:
    def one(sql: str, *params) -> int:
        try:
            return int(conn.execute(sql, params).fetchone()[0] or 0)
        except Exception:
            return 0

    texts = ("traite", "decision", "conclusions_sb", "ordre_du_jour", "projet_texte", "rapport_synthese", "btr", "ndc")
    q = ",".join("?" * len(texts))
    return {
        "texts": one(f"SELECT COUNT(*) FROM documents WHERE access='public' AND doc_type IN ({q})", *texts),
        "versions": one("SELECT COUNT(*) FROM documents WHERE access='public' AND COALESCE(version,1)>1"),
        "drafts": one("SELECT COUNT(*) FROM live_texts WHERE doc_id IS NOT NULL"),
        "positions": one("SELECT COUNT(*) FROM positions WHERE access='public'"),
        "submissions": one("SELECT COUNT(*) FROM sub_items"),
        "reports": one("SELECT COUNT(*) FROM documents WHERE access='public' AND doc_type IN "
                       "('compte_rendu','communique','analyse')"),
        "events": one("SELECT COUNT(*) FROM agenda_events") + one("SELECT COUNT(*) FROM pav_events WHERE cancelled=0"),
        "documents": one("SELECT COUNT(*) FROM documents WHERE access='public'"),
        "passages": one("SELECT COUNT(*) FROM passages p JOIN documents d ON d.id=p.doc_id WHERE d.access='public'"),
    }


async def health(request: Request):
    db.get().execute("SELECT 1").fetchone()
    return JSONResponse({"ok": True})


routes = [
    Route("/", home, methods=["GET", "POST"]),
    Route("/connect", connect_page, methods=["GET", "POST"]),
    Route("/plan", plan_page),
    Route("/topics", topics_page, methods=["GET", "POST"]),
    Route("/topics/{sid:int}/delete", topic_delete, methods=["POST"]),
    Route("/about", about_page),
    Route("/how-it-works", how_page),
    Route("/database", database_page),
    Route("/contact", contact_page, methods=["GET", "POST"]),
    Route("/account", account_page, methods=["GET", "POST"]),
    Route("/signin", login_form, methods=["GET", "POST"]),
    Route("/connexion", login_form, methods=["GET", "POST"]),
    Route("/entrer/{value}", login_link),
    Route("/login/{token}", login),
    Route("/lang/{code}", set_lang),
    Route("/join", join, methods=["GET", "POST"]),
    Route("/join/{code}", join, methods=["GET", "POST"]),
    Route("/rejoindre", join, methods=["GET", "POST"]),
    Route("/rejoindre/{code}", join, methods=["GET", "POST"]),
    Route("/inviter", invite_page),
    Route("/static/{name}", static_file),
    Route("/logout", logout),
    Route("/doc/{doc_id:int}", doc_page),
    Route("/diff/{doc_id:int}", diff_page),
    Route("/fichier/{doc_id:int}", file_page),
    Route("/telechargements/{name}", download_page),
    Route("/admin", admin_page, methods=["GET", "POST"]),
    Route("/admin/utilisateur/{uid:int}/basculer", admin_toggle, methods=["POST"]),
    Route("/admin/utilisateur/{uid:int}/supprimer", admin_delete, methods=["POST"]),
    Route("/admin/courrier", mail_page, methods=["GET", "POST"]),
    Route("/admin/programme", admin_programme, methods=["GET", "POST"]),
    Route("/admin/seors/import", seors_import, methods=["POST", "OPTIONS"]),
    Route("/try", demo_page),
    Route("/try/ask", demo_ask, methods=["POST"]),
    Route("/try/r/{aid}", demo_answer_page),
    Route("/essayer", _redirect("/try")),
    Route("/for/{who}", audience_page),
    Route("/pour/{who}", audience_page),
    Route("/healthz", health),
    # anciennes adresses (liens dans les alertes, favoris)
    Route("/compte", account_redirect),
    Route("/sujets", _redirect("/topics")),
    Route("/connecteur", _redirect("/connect")),
    Route("/recherche", _redirect("/database")),
    Route("/a-propos", _redirect("/about")),
    Route("/bienvenue", _redirect("/")),
    Route("/telegram", _redirect("/topics#telegram")),
    Route("/deposer", _redirect("/")),
    Route("/dossiers", _redirect("/database?tab=negotiations")),
    Route("/textes", _redirect("/database?tab=texts")),
]
