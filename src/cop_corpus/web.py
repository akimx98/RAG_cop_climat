"""Site web : accueil, connecter un LLM, planifier ma journée, mes sujets, à propos (comment ça marche, base de
données, contact), compte, documents et versions, administration. Anglais par défaut, français au choix."""

from __future__ import annotations

import json
import mimetypes
import re
import threading
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import alerts, auth, config, db, diff, i18n, mailin, qr, search, usage

TEMPLATES = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html"]),
)
TEMPLATES.globals["LEVELS"] = search.LEVEL_LABELS


_DIFF_MARK = re.compile(r"\[-(.*?)-\]|\{\+(.*?)\+\}", re.S)


def _diffhtml(text: str) -> Markup:
    """[-supprimé-] {+ajouté+} → surlignage ; les crochets du texte (non agréé) restent visibles et marqués."""
    def brk(s: str) -> str:
        return s.replace("[", '<b class="br">[</b>').replace("]", '<b class="br">]</b>')

    out, pos = [], 0
    for m in _DIFF_MARK.finditer(text):
        out.append(brk(str(escape(text[pos:m.start()]))))
        if m.group(1) is not None:
            out.append(f'<del class="del">{brk(str(escape(m.group(1))))}</del>')
        else:
            out.append(f'<ins class="ins">{brk(str(escape(m.group(2))))}</ins>')
        pos = m.end()
    out.append(brk(str(escape(text[pos:]))))
    return Markup("".join(out))


TEMPLATES.filters["diffhtml"] = _diffhtml


def _notionmark(html: Markup, key: str) -> Markup:
    """Surligne une notion (formulations.CONCEPTS) dans un texte déjà échappé, hors balises."""
    from . import formulations

    c = formulations.CONCEPTS.get(key)
    if not c:
        return html
    pat = re.compile("|".join(f"(?:{m})" for m in c["motifs"]), re.I)
    parts = re.split(r"(<[^>]+>)", str(html))
    return Markup("".join(p if p.startswith("<") else pat.sub(lambda m: f"<mark>{m.group(0)}</mark>", p)
                          for p in parts))


TEMPLATES.filters["notionmark"] = _notionmark
TEMPLATES.globals["CLIENT_VERSION"] = json.loads(
    (Path(__file__).resolve().parents[2] / "clients/claude-plugin/.claude-plugin/plugin.json").read_text())["version"]

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
LLM_ACTIVE_DAYS = 14


def llm_last_call(user: dict | None) -> str | None:
    """Dernier appel MCP s'il date de moins de LLM_ACTIVE_DAYS jours (« votre LLM utilise le corpus »), sinon None :
    un connecteur essayé une fois puis retiré ne doit pas passer pour branché."""
    from datetime import datetime, timedelta, timezone

    seen = (user or {}).get("mcp_seen_at")
    if not seen:
        return None
    try:
        when = datetime.strptime(seen[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return seen if datetime.now(timezone.utc) - when < timedelta(days=LLM_ACTIVE_DAYS) else None


TEMPLATES.globals["llm_last_call"] = llm_last_call
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
    # ?frise=1 : panneau « Au fil des versions » ouvert d'emblée (lien partagé), replié sinon
    ctx["frise_open"] = request.query_params.get("frise") in ("1", "open", "ouvert")
    ctx["SOURCE_URL"] = config.SOURCE_URL
    ctx["PROFILES"] = usage.PROFILES
    ctx["PASSWORD_MIN"] = auth.PASSWORD_MIN
    from . import demo as _demo
    ctx["OFFER"] = _demo.offer()   # questions sur le site : invités, comptes gratuits, date de fin de l'offre
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
    return auth.user_from_cookie(db.get(), request.cookies.get(COOKIE))


SIGNIN_PER_IP = 500   # échecs et envois de code par réseau et par quart d'heure (seuil large : Wi-Fi de la COP) ;
                      # les connexions réussies ne comptent pas, les vraies limites sont par compte


def _same_origin(request: Request) -> bool:
    """Formulaires de connexion, d'inscription et de compte : refusés s'ils sont envoyés depuis un autre site
    (connexion forcée au compte de quelqu'un d'autre). Sans en-tête Origin (anciens navigateurs, tests) : accepté."""
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site:   # navigateurs récents : leur verdict suffit (« none » = adresse tapée, favori)
        return fetch_site in ("same-origin", "none")
    origin = (request.headers.get("origin") or "").rstrip("/")
    if not origin or origin == "null":   # Referrer-Policy: no-referrer → Origin « null » sur nos propres formulaires
        return True
    return origin in (config.PUBLIC_BASE, f"{request.url.scheme}://{request.url.netloc}")


def _forbidden(request: Request) -> Response:
    if _wants_json(request):
        return JSONResponse({"error": i18n.pick(lang_of(request), "Requête refusée : rechargez la page.",
                                                "Request refused: reload the page.")}, status_code=403)
    return Response("Forbidden", status_code=403)


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


def public_page(handler):
    """Page ouverte à tous, sans compte : `request.state.user` vaut l'utilisateur connecté ou None.
    Les documents internes (restreint) restent filtrés par search.document / search.versions / can_restricted."""
    async def wrapper(request: Request):
        request.state.user = current_user(request)
        return await handler(request)
    return wrapper


def _signin_redirect(request: Request) -> Response:
    """Visiteur sans compte sur un document interne : la fenêtre de connexion, avec retour ici."""
    from urllib.parse import quote

    return RedirectResponse(f"/?signin=1&next={quote(_next_url(request))}", status_code=303)


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
    resp.set_cookie(COOKIE, auth.make_cookie(user["id"], days=days, epoch=user.get("session_epoch") or 0),
                    max_age=days * 86400, httponly=True, secure=secure,
                    samesite="lax")
    if token:
        resp.set_cookie(TOKEN_COOKIE, token, max_age=days * 86400, httponly=True, secure=secure, samesite="strict")
    return resp


def _client(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[-1].strip() if fwd else (request.client.host if request.client else "")


async def login(request: Request):
    """Ancien lien /login/<jeton>, retiré : il ouvrait une session du site à partir de l'adresse du connecteur MCP
    (enregistrée chez les fournisseurs de LLM), ce qui permettait, si elle fuyait, de prendre le compte. Le jeton
    n'est même pas lu : la page de connexion s'affiche, avec une explication."""
    return render(request, "login.html", user=None, step="email", next="/",
                  error=i18n.pick(lang_of(request),
                                  "Ce lien n'est plus utilisé. Connectez-vous avec votre e-mail : mot de passe, ou code reçu par e-mail.",
                                  "This link is no longer used. Sign in with your e-mail: password, or a code sent by e-mail."))


def _send_login_code(user: dict, code: str) -> tuple[bool, bool]:
    """Envoie le code par e-mail et, si le compte est relié, par Telegram → (e-mail parti, Telegram parti)."""
    from . import mailout, telegram

    en = i18n.default_for(user) == "en"
    subject = (f"Your OpenCOP sign-in code: {code}" if en else f"Votre code de connexion à OpenCOP : {code}")
    text = (f"Your sign-in code: {code}\n\nIt is valid for {auth.CODE_MINUTES} minutes. If you did not ask for it, ignore this e-mail.\n\n{config.PUBLIC_BASE}/"
            if en else
            f"Votre code de connexion : {code}\n\nValable {auth.CODE_MINUTES} minutes. Si vous ne l'avez pas demandé, ignorez cet e-mail.\n\n{config.PUBLIC_BASE}/")
    mailed = mailout.send(user["email"], subject, text, idempotency_key=f"login-code/{user['id']}/{auth._code_hash(user['id'], code)[:16]}")
    # Telegram seulement si l'adresse est prouvée : sinon, qui a créé le compte avec l'adresse de quelqu'un d'autre
    # recevrait les codes de connexion sur son propre Telegram.
    tg = bool(user.get("telegram_chat_id")) and bool(user.get("telegram_trusted")) and telegram.send(
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
    """Connexion en deux temps, pour tous les comptes :
    1. l'e-mail ;
    2. le mot de passe si le compte en a un (inscription libre, ou choisi ensuite), sinon un code à 6 chiffres envoyé
       par e-mail (comptes invités par lien ou QR code, sans mot de passe) — et par Telegram si l'adresse est prouvée.
    Mot de passe oublié : le même code ; toutes les autres sessions sont alors fermées, puis la page Compte propose
    un nouveau mot de passe. La fenêtre de connexion (JSON) et la page /signin (sans JavaScript) passent ici."""
    if request.method == "GET":
        if current_user(request):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", user=None, step="email", email=request.query_params.get("email", ""),
                      next=_safe_next(request.query_params.get("next")))
    if not _same_origin(request):
        return _forbidden(request)
    conn = db.get()
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    nxt = _safe_next(form.get("next"))
    action = form.get("action") or ""
    code_in = (form.get("code") or "").strip()
    ip = auth.ip_key(_client(request))
    T = lambda fr, en: i18n.pick(lang_of(request), fr, en)  # noqa: E731

    def page(step, **kw):
        if _wants_json(request):
            return JSONResponse({"step": step, "email": email, **{k: v for k, v in kw.items() if v}})
        return render(request, "login.html", user=None, step=step, email=email, next=nxt, **kw)

    # Garde-fou par adresse IP sur toute la connexion (énumération, rafales de codes) ; seuil large : Wi-Fi partagé.
    net = f"signin|{ip}"
    if auth.throttled(net, limit=SIGNIN_PER_IP):
        return page("email", error=T("Trop de tentatives depuis ce réseau : réessayez dans quelques minutes.",
                                     "Too many attempts from this network: try again in a few minutes."))
    if not email:
        return page("email", error=T("Indiquez votre e-mail.", "Enter your e-mail.") if (form.get("password") or action
                                                                                        or "code" in form) else None)
    user = auth.by_email(conn, email)
    if not user:
        auth.hit(net)
        if auth.disabled_email(conn, email):
            return page("email", error=T("Ce compte est désactivé : écrivez-nous (page Contact).",
                                         "This account is disabled: write to us (Contact page)."))
        return page("email", error=T("Aucun compte avec cet e-mail : créez-en un (onglet « Créer un compte »).",
                                     "No account with this e-mail: create one (Create account tab)."))

    # Étape mot de passe
    if form.get("password") and action not in ("forgot", "resend"):
        if not user.get("password_hash"):
            action = "code"   # compte sans mot de passe (invitation) : on envoie un code au lieu d'un refus
        else:
            if auth.throttled(f"{email}|{ip}"):
                return page("password", error=T("Trop d'essais : réessayez dans 15 minutes, ou « Mot de passe oublié ? ».",
                                                "Too many attempts: try again in 15 minutes, or use “Forgot password?”."))
            ok = auth.check_password(conn, email, form.get("password") or "", ip)
            if not ok:
                auth.hit(net)
                return page("password", error=T("Mot de passe incorrect. Oublié ? Recevez un code par e-mail.",
                                                "Wrong password. Forgotten it? Get a code by e-mail."))
            return _go(request, nxt, ok)

    # Étape code (un code vide n'est pas un essai : « Renvoyer » sans e.submitter, ou Entrée par erreur)
    if code_in and action not in ("resend", "forgot"):
        was_verified = bool(user.get("email_verified_at"))   # avant que le bon code ne la prouve
        if auth.throttled(f"code|{email}|{ip}") or not auth.check_login_code(conn, user, code_in, ip):
            auth.hit(f"code|{email}|{ip}")
            auth.hit(net)
            if auth.code_locked(conn, user, ip):
                return page("code", error=T("Trop de codes faux aujourd'hui depuis ce réseau : réessayez demain, "
                                            "depuis un autre réseau, ou écrivez-nous.",
                                            "Too many wrong codes today from this network: try again tomorrow, "
                                            "from another network, or write to us."))
            return page("code", error=T("Code incorrect ou expiré.", "Wrong or expired code."))
        user = auth.by_id(conn, user["id"])
        if user.get("password_hash"):   # mot de passe oublié : les autres appareils sont déconnectés
            info = auth.secure_after_reset(conn, user, was_verified)
            user = auth.by_id(conn, user["id"])
            return _go(request, "/account?forgot=1" + ("&renewed=1" if info["mcp"] else ""), user)
        if not was_verified:   # première preuve de l'adresse : le profil propose « ce n'est pas moi : tout sécuriser »
            return _go(request, "/account?first=1", user)
        return _go(request, nxt, user)
    if "code" in form and not code_in and action not in ("resend", "forgot"):
        return page("code", error=T("Saisissez le code à 6 chiffres reçu par e-mail.", "Enter the 6-digit code sent by e-mail."))

    if user.get("password_hash") and action not in ("forgot", "resend", "code"):
        return page("password")

    # Envoi d'un code : compte sans mot de passe, « Mot de passe oublié ? » ou « Renvoyer le code »
    code, why = auth.issue_login_code(conn, user, ip)
    auth.hit(net)
    if why == "locked":
        return page("code", error=T("Trop de codes faux aujourd'hui depuis ce réseau : réessayez demain, depuis un "
                                    "autre réseau, ou écrivez-nous.",
                                    "Too many wrong codes today from this network: try again tomorrow, from another "
                                    "network, or write to us."))
    if why == "day":
        return page("code", error=T("Trop de codes envoyés aujourd'hui : utilisez le dernier reçu, ou écrivez-nous.",
                                    "Too many codes sent today: use the last one you received, or write to us."))
    if why:
        return page("code", error=T("Un code vient d'être envoyé : patientez une minute avant d'en demander un autre.",
                                    "A code was just sent: wait a minute before asking for another one."))
    from starlette.concurrency import run_in_threadpool

    mailed, tg = await run_in_threadpool(_send_login_code, user, code)
    if not (mailed or tg):
        auth.cancel_login_code(conn, user)
        return page("email", error=T("Le code n'a pas pu être envoyé : réessayez dans un instant, ou écrivez-nous (page Contact).",
                                     "The code could not be sent: try again in a moment, or write to us (Contact page)."))
    where = " + ".join(x for x in (T("par e-mail", "by e-mail") if mailed else "", "Telegram" if tg else "") if x)
    return page("code", sent=where)


async def login_link(request: Request):
    """Lien de connexion signé, 24 h, à usage unique, généré par un administrateur. GET affiche un bouton ; seul le
    POST consomme le lien (les antivirus de messagerie ouvrent les liens avant la personne)."""
    value = request.path_params["value"]
    lang = lang_of(request)
    expired = i18n.pick(lang, "Lien expiré ou déjà utilisé. Demandez-en un nouveau, ou connectez-vous avec un code reçu par e-mail.",
                        "Link expired or already used. Ask for a new one, or sign in with a code sent by e-mail.")
    if request.method == "GET":
        if not auth.login_link_valid(db.get(), value):
            return render(request, "login.html", user=None, error=expired)
        return render(request, "login.html", user=None, step="link", link_path=request.url.path)
    if not _same_origin(request):
        return _forbidden(request)
    user = auth.by_login_link(db.get(), value)
    if not user:
        return render(request, "login.html", user=None, error=expired)
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
    if not _same_origin(request):
        return _forbidden(request)
    form = await request.form()
    code = (form.get("code") or code).strip().lower()
    invite_key = f"invite|{auth.ip_key(_client(request))}"

    async def fail(err: str):
        msg = i18n.pick(lang, *auth.SIGNUP_ERRORS[err])
        invited = bool(code) and auth.invite_valid(conn, code)
        if _wants_json(request):
            return JSONResponse({"error": msg, "invited": invited})
        return await _landing(request, None, invite_code=code if invited else "", open_modal="signup",
                              signup_error=msg, form=form)

    if form.get("check_code"):  # saisie manuelle d'un code d'invitation
        if auth.throttled(invite_key):
            return await fail("throttled")
        if not auth.invite_valid(conn, code):
            auth.hit(invite_key)
            return await fail("bad_code")
        return _go(request, f"/join/{code}")
    if form.get("hp_x7"):  # champ piège invisible (nom que les navigateurs ne remplissent pas d'eux-mêmes)
        return await fail("robot")
    if not code and form.get("password2") is not None and form.get("password") != form.get("password2"):
        return await fail("mismatch")
    user, token, error = auth.signup(conn, code, form.get("email") or "", form.get("organisation") or "",
                                     "" if code else (form.get("password") or ""), _client(request), lang=lang,
                                     profil=form.get("profil") or "")
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
    """À propos : qu'est-ce qu'OpenCOP (court, renvoie vers les autres pages)."""
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

            ok = await run_in_threadpool(lambda: mailout.send(config.CONTACT_EMAIL, f"[OpenCOP] {name or reply}"[:120],
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

@public_page
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
        usage.record("web", "page", "recherche", user, args={"q": q, "niveaux": levels, "types": types},
                     lang=lang_of(request, user), n=len(ctx["hits"]), outcome="ok" if ctx["hits"] else "vide")
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
        if not _same_origin(request):
            return _forbidden(request)
        if action == "profile":
            profil = form.get("profil") if form.get("profil") in usage.PROFILE_KEYS else None
            conn.execute("UPDATE users SET name=?, organisation=?, profil=?, usage_optout=? WHERE id=?",
                         ((form.get("name") or "").strip()[:80] or None, (form.get("organisation") or "").strip()[:120] or None,
                          profil, 1 if form.get("usage_optout") else None, user["id"]))
            if form.get("usage_optout"):
                usage.forget_user(conn, user["id"], demo=False)
            conn.commit()
            done = "profile"
        elif action == "telegram_unlink":
            import secrets

            # nouveau code : l'ancien (affiché dans le lien t.me) ne doit plus pouvoir relier un autre téléphone
            conn.execute("UPDATE users SET telegram_chat_id=NULL, telegram_trusted=NULL, telegram_code=? WHERE id=?",
                         (secrets.token_hex(4), user["id"]))
            conn.commit()
            done = "telegram"
        elif action == "delete":
            if (form.get("confirm") or "").strip().lower() in ("delete", "supprimer") and auth.delete_guest(conn, user["id"]):
                return await logout(request)
            error = "delete"
        elif action == "password":
            pw, pw2 = form.get("password") or "", form.get("password2") or ""
            if len(pw) < auth.PASSWORD_MIN:
                error = "short"
            elif pw != pw2:
                error = "mismatch"
            else:
                auth.set_password(conn, user["id"], pw)
                auth.end_sessions(conn, user["id"])   # les autres appareils sont déconnectés, pas celui-ci
                done = "password"
        if action == "logout_all":
            auth.end_sessions(conn, user["id"])
            done = "logout_all"
        elif action == "secure_all":
            auth.secure_all(conn, user)
            done = "secure_all"
        user = auth.by_id(conn, user["id"])
        request.state.user = user
        if done in ("password", "logout_all", "secure_all"):
            resp = render(request, "account.html", user=user, error=error, done=done, min=auth.PASSWORD_MIN, forgot=False,
                          guest=auth.is_guest(user), token=_token_for(request, conn, user),
                          invite=auth.invite_settings(conn))
            return _session(resp, user)
    return render(request, "account.html", user=user, error=error, done=done, min=auth.PASSWORD_MIN,
                  forgot=request.query_params.get("forgot") == "1" and not done,
                  renewed=request.query_params.get("renewed") == "1" and not done,
                  first=request.query_params.get("first") == "1" and not done, guest=auth.is_guest(user),
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


@public_page
async def doc_page(request: Request):
    conn = db.get()
    user = request.state.user
    doc_id = int(request.path_params["doc_id"])
    info = search.document(conn, doc_id, user)
    if not info:
        if not user and conn.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone():
            return _signin_redirect(request)
        return Response("Document introuvable ou non autorisé", status_code=404)
    per = 40
    hl = request.query_params.get("passage")
    hl = int(hl) if hl and hl.isdigit() else None
    # Ni le document ni le passage : seulement son niveau, son type et si l'on arrive sur un passage cité
    # (lien de citation d'une réponse de LLM ou du bot).
    usage.record("web", "page", "document", user, lang=lang_of(request, user),
                 args={"niveau": info.get("niveau"), "type_doc": info.get("type"), "passage_cite": bool(hl)})
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


@public_page
async def diff_page(request: Request):
    conn = db.get()
    user = request.state.user
    new_id = int(request.path_params["doc_id"])
    new = search.document(conn, new_id, user)
    if not new:
        if not user and conn.execute("SELECT 1 FROM documents WHERE id=?", (new_id,)).fetchone():
            return _signin_redirect(request)
        return Response("introuvable", status_code=404)
    row = conn.execute("SELECT lineage_key FROM documents WHERE id=?", (new_id,)).fetchone()
    versions = search.versions(conn, row["lineage_key"], user) if row and row["lineage_key"] else []
    old_id = request.query_params.get("avec")
    if not re.fullmatch(r"[0-9]{1,12}", old_id or ""):
        old_id = diff.previous_in_lineage(conn, new_id)
    old = search.document(conn, int(old_id), user) if old_id else None
    if not old:
        return render(request, "diff.html", doc=new, old=None, result=None, versions=versions)
    result = diff.compare(conn, int(old_id), new_id, lang=lang_of(request, user))
    from . import formulations

    rows = formulations.lineage(conn, conn.execute("SELECT * FROM documents WHERE id=?", (new_id,)).fetchone(), user)
    grille = formulations.grid(conn, rows) if len(rows) >= 2 else None
    return render(request, "diff.html", doc=new, old=old, result=result, versions=versions, grille=grille)


async def file_page(request: Request):
    conn = db.get()
    doc_id = int(request.path_params["doc_id"])
    row = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        return Response("introuvable", status_code=404)
    user = current_user(request)
    signed = auth.check_file_signature(doc_id, request.query_params.get("exp"), request.query_params.get("sig"))
    if row["access"] != "public" and not (signed or auth.can_restricted(user)):
        return _signin_redirect(request) if not user else Response("Non autorisé", status_code=403)
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
    elif name in ("chatgpt-instructions.txt", "mistral-instructions.txt", "assistant-instructions.txt"):
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
                                              admin=bool(form.get("admin")), restricted=not form.get("public_only"), verified=True)
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
async def usage_page(request: Request):
    """Statistiques d'usage agrégées (MCP, Telegram, site, démo)."""
    qp = request.query_params
    days = int(qp.get("jours")) if (qp.get("jours") or "").isdigit() else 7
    s = usage.stats(db.get(), days=max(1, min(days, 400)), include_admins=qp.get("admins") == "1")
    return render(request, "usage.html", s=s)


async def privacy_page(request: Request):
    return render(request, "privacy.html", user=current_user(request), contact=bool(config.CONTACT_EMAIL),
                  text_days=usage.TEXT_DAYS, keep_days=usage.KEEP_DAYS, demo_days=usage.DEMO_KEEP_DAYS)


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
_demo_busy: set[str] = set()      # visiteur, adresse IP (invités) ou compte ayant une question en cours
_demo_running = [0]               # questions en cours, tous visiteurs confondus
_demo_net: dict[str, int] = {}    # questions de comptes en cours, par réseau
_demo_lock = threading.Lock()     # ces compteurs changent depuis la boucle et depuis les fils de réponse


DEMO_MAX_CONCURRENT = 6   # questions traitées en même temps, tous visiteurs confondus (coût, API)


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


def demo_refusals() -> dict[str, tuple[str, str]]:
    """Messages de refus de la démo : la raison, courte. Les deux façons de continuer (questions sur le site avec
    un compte gratuit, ou corpus branché sur son propre LLM) sont présentées juste en dessous, dans la page."""
    from . import demo

    o = demo.offer()
    after_fr = (f" Après le {o['until_fr']}, la limite repasse à {o['after']} questions par jour." if o["boost"] else "")
    after_en = (f" After {o['until_en']}, the limit goes back to {o['after']} questions a day." if o["boost"] else "")
    return {
        "disabled": ("La démo est momentanément indisponible.", "The demo is temporarily unavailable."),
        "budget": ("Les questions sans compte ont atteint leur plafond du jour.",
                   "Questions without an account have reached today's limit."),
        "session": (f"Vous avez utilisé vos {o['guest']} questions d'essai sans compte.",
                    f"You have used your {o['guest']} trial questions without an account."),
        "ip": ("Les questions d'essai sans compte sont épuisées pour aujourd'hui sur ce réseau (souvent partagé).",
               "Trial questions without an account are used up for today on this network (often shared)."),
        "account": ("Vous avez atteint votre limite de questions du jour sur le site ; elle se renouvelle à minuit "
                    "(heure d'Antalya)." + after_fr,
                    "You have reached today's question limit on the site; it renews at midnight (Antalya time)."
                    + after_en),
        "budget-account": ("Les questions sur le site ont atteint leur plafond du jour ; il se renouvelle à minuit "
                           "(heure d'Antalya).",
                           "Questions on the site have reached today's limit; it renews at midnight (Antalya time)."),
        "budget-total": ("Les questions sur le site ont atteint le budget prévu pour cette COP.",
                         "Questions on the site have reached the budget set for this COP."),
        "network": ("Trop de questions depuis ce réseau aujourd'hui : réessayez demain.",
                    "Too many questions from this network today: try again tomorrow."),
        "busy": ("Une question est déjà en cours : attendez sa réponse.", "A question is already running: wait for its answer."),
        "crowded": ("Beaucoup de questions en même temps : réessayez dans une minute.", "Many questions at once: please try again in a minute."),
        "short": ("Posez une question un peu plus précise.", "Please ask a slightly more specific question."),
    }


def _demo_public(payload: dict) -> dict:
    return {k: payload.get(k) for k in ("html", "sources", "events", "checks", "trace", "model", "diffs", "trajectoires")}


async def demo_page(request: Request):
    """Essayer : quelques questions sans compte, avec exemples par public, réponse sourcée et vérifiée."""
    from . import demo

    conn = db.get()
    user = current_user(request)
    if user:   # /try est l'essai sans compte ; un compte travaille dans ses conversations
        q = request.query_params.get("q")
        from urllib.parse import quote

        return RedirectResponse("/ask" + (f"?q={quote(q[:demo.MAX_QUESTION])}" if q else ""), status_code=303)
    session, new = _demo_session(request)
    who = request.query_params.get("pour") or request.query_params.get("for") or ""
    who = who if who in demo.EXAMPLES else "delegations"
    resp = render(request, "try.html", user=user, quota=demo.quota(conn, session, _client(request), user),
                  examples=demo.EXAMPLES, who=who, prefill=(request.query_params.get("q") or "")[:demo.MAX_QUESTION],
                  shared=None, as_of=demo.corpus_as_of(conn), refusals=demo_refusals(),
                  next="/ask", mode="try")
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
                  as_of=demo.corpus_as_of(conn), refusals=demo_refusals(), next="/ask", mode="try")
    return _set_demo_cookie(resp, session) if new else resp


async def ask_page(request: Request):
    """Questions des comptes : conversations, historique, réponses sourcées (mêmes outils que la démo)."""
    from . import demo

    conn = db.get()
    user = current_user(request)
    if not user:
        return RedirectResponse("/?signup=1&next=/ask", status_code=303)
    request.state.user = user
    cid = request.path_params.get("cid")
    conv, turns = None, []
    if cid:
        conv = demo.conv_get(conn, cid, user["id"])
        if not conv:
            return RedirectResponse("/ask", status_code=303)
        turns = demo.conv_turns(conn, cid)
        for t in turns:
            t["payload"] = {**_demo_public(t["payload"]), "sources": demo.sign_links(conn, t["payload"].get("sources") or [])}
    return render(request, "try.html", user=user, mode="ask", quota=demo.quota(conn, None, _client(request), user),
                  examples=demo.EXAMPLES, who="delegations", prefill=(request.query_params.get("q") or "")[:demo.MAX_QUESTION],
                  shared=None, as_of=demo.corpus_as_of(conn), refusals=demo_refusals(),
                  conv=conv, turns=[{"id": t["id"], "question": t["question"], "created_at": t["created_at"],
                                     **t["payload"]} for t in turns],
                  convs=demo.conv_list(conn, user["id"]), KEEP_DAYS=usage.DEMO_KEEP_DAYS)


async def ask_delete(request: Request):
    from . import demo

    user = current_user(request)
    if not user or not _same_origin(request):
        return _forbidden(request)
    demo.conv_delete(db.get(), request.path_params["cid"], user["id"])
    return RedirectResponse("/ask", status_code=303)


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
    refusals = demo_refusals()
    T = lambda key: i18n.pick(lang, *refusals[key])  # noqa: E731
    session = request.cookies.get(DEMO_COOKIE) or ""
    # Robot : champ piège rempli, ou (invité) cookie de visite absent ou fabriqué.
    if body.get("website") or (not user and not _demo_token_ok(session)):
        return JSONResponse({"error": T("disabled"), "reason": "session"}, status_code=403)
    question = re.sub(r"\s+", " ", str(body.get("question") or "")).strip()[:demo.MAX_QUESTION]
    if len(question) < 6:
        return JSONResponse({"error": T("short"), "reason": "short"}, status_code=400)
    ip = _client(request)
    conn = db.get()
    q = demo.quota(conn, session, ip, user)
    if q["remaining"] <= 0:
        return JSONResponse({"error": T(q["reason"]), "reason": q["reason"]}, status_code=429)
    follow, history, conv = None, None, None
    if user:
        # Compte : la question appartient à une conversation (/ask) ; sans conversation, une nouvelle est créée
        # à la première réponse réussie.
        cid = str(body.get("conv") or "")
        if cid:
            conv = demo.conv_get(conn, cid, user["id"]) if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", cid) else None
            if not conv:
                return JSONResponse({"error": i18n.pick(lang, "Conversation introuvable.", "Conversation not found."),
                                     "reason": "conv"}, status_code=404)
            ctx = demo.conv_context(conn, conv["id"])
            history, follow = ctx["history"], {"state": ctx["state"]}
        keys = {f"u:{user['id']}"}   # pas de clé d'IP exclusive : sur le Wi-Fi de la COP, tout le monde la partage
    else:
        if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", str(body.get("follow") or "")):
            follow = demo.follow_context(conn, body["follow"], session)
        keys = {f"s:{session}", f"ip:{demo.ip_hash(ip)}"}
    net = demo.ip_hash(ip) if user else None
    with _demo_lock:
        if keys & _demo_busy:
            return JSONResponse({"error": T("busy"), "reason": "busy"}, status_code=429)
        # Places partagées : au plus DEMO_MAX_CONCURRENT en tout, et quelques-unes seulement pour les comptes d'un
        # même réseau (une rafale de comptes jetables ne bloque pas tout le monde).
        if _demo_running[0] >= DEMO_MAX_CONCURRENT or (net and _demo_net.get(net, 0) >= demo.IP_INFLIGHT_ACCOUNTS):
            return JSONResponse({"error": T("crowded"), "reason": "crowded"}, status_code=429)
        _demo_busy.update(keys)
        _demo_running[0] += 1
        if net:
            _demo_net[net] = _demo_net.get(net, 0) + 1
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(ev):
        loop.call_soon_threadsafe(queue.put_nowait, ev)

    def release():
        with _demo_lock:
            _demo_busy.difference_update(keys)
            _demo_running[0] -= 1
            if net:
                _demo_net[net] -= 1
                if _demo_net[net] <= 0:
                    del _demo_net[net]

    def failed(msg_fr: str, msg_en: str, error: str | None):
        if error:
            from . import monitor

            monitor.report("site : réponse à une question", error)
            try:
                demo.save(db.get(), question=question, lang=lang, session=session, ip=ip, user=user, payload=None,
                          error=error[:500])
            except Exception:
                pass
        emit({"type": "error", "message": i18n.pick(lang, msg_fr, msg_en)})

    def work():
        try:
            try:
                res = demo.answer(question, lang, emit, follow=follow, purpose=demo.purpose_for(user), history=history)
            except Exception as exc:  # API indisponible, quota OpenAI…
                return failed("La réponse n'a pas pu être produite : réessayez dans un moment.",
                              "The answer could not be produced: please try again in a moment.",
                              f"{type(exc).__name__}: {exc}")
            c = db.get()
            cid, title = None, ""
            if user:
                if conv:
                    still = demo.conv_get(c, conv["id"], user["id"])
                    if not still:   # supprimée pendant la réponse : on ne garde pas son contenu
                        demo.save(c, question="", lang=lang, session=session, ip=ip, user=user,
                                  payload=None, error="conversation deleted")
                        return failed("Cette conversation a été supprimée entre-temps.",
                                      "This conversation was deleted in the meantime.", None)
                    cid, title = still["id"], still["title"]
                else:
                    cid = demo.conv_create(c, user["id"], question)
                    title = demo.conv_get(c, cid, user["id"])["title"]
            aid = demo.save(c, question=question, lang=lang, session=session, ip=ip, user=user, payload=res,
                            conv_id=cid)
            try:
                left = demo.quota(c, session, ip, user)
                pub = _demo_public(res)
                pub["sources"] = demo.sign_links(c, pub["sources"])
            except Exception:   # réponse enregistrée : on l'affiche quand même
                left, pub = {"remaining": 1, "reason": None}, _demo_public(res)
            emit({"type": "done", "id": aid, "url": f"/try/r/{aid}", **pub,
                  "remaining": left["remaining"], "reason": left["reason"],
                  "message": T(left["reason"]) if left["reason"] else "", "conv": cid, "title": title})
        finally:
            release()
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


ROBOTS = """User-agent: *
Disallow: /diff/
Disallow: /fichier/
Disallow: /database?
Disallow: /ask
Disallow: /try/
Disallow: /u/
Disallow: /mcp
Disallow: /admin
Disallow: /account
Disallow: /topics
Disallow: /lang/
"""

# Pages fixes proposées aux moteurs (les pages personnelles et coûteuses restent exclues par ROBOTS).
SITEMAP_PAGES = ("/", "/about", "/how-it-works", "/connect", "/plan", "/try", "/database", "/contact",
                 "/confidentialite", "/for/delegations", "/for/press", "/for/activists")


async def robots(request: Request):
    """Pages publiques, mais les robots d'indexation restent hors des pages coûteuses ou sans fin
    (comparaisons de versions deux à deux, recherches, fichiers bruts) et des pages personnelles."""
    return Response(ROBOTS + f"\nSitemap: {config.PUBLIC_BASE}/sitemap.xml\n", media_type="text/plain")


def sitemap_urls(conn) -> list[tuple[str, str | None]]:
    """(chemin, date) proposés aux moteurs : pages fixes, dossiers de négociation, documents **publics** indexés
    (jamais un document restreint, en attente ou doublon : seulement ce qu'un visiteur anonyme peut ouvrir)."""
    from . import live

    urls: list[tuple[str, str | None]] = [(p, None) for p in SITEMAP_PAGES]
    urls += [(f"/database/negotiations/{d['slug']}", None) for d in live.dossiers()]
    rows = conn.execute("SELECT id, fetched_at FROM documents WHERE access='public' AND status='indexed' "
                        "AND duplicate_of IS NULL ORDER BY id LIMIT 45000").fetchall()   # plafond du format : 50 000
    urls += [(f"/doc/{r['id']}", (r["fetched_at"] or "")[:10] or None) for r in rows]
    return urls


async def sitemap(request: Request):
    from xml.sax.saxutils import escape

    from starlette.concurrency import run_in_threadpool

    urls = await run_in_threadpool(sitemap_urls, db.get())
    body = "".join(f"<url><loc>{escape(config.PUBLIC_BASE + p)}</loc>" + (f"<lastmod>{d}</lastmod>" if d else "")
                   + "</url>\n" for p, d in urls)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           + body + "</urlset>\n")
    return Response(xml, media_type="application/xml")


async def health(request: Request):
    db.get().execute("SELECT 1").fetchone()
    return JSONResponse({"ok": True})


async def health_deep(request: Request):
    """Sonde externe (uptime) : 503 si un problème bloquant est en cours (worker arrêté, alertes bloquées…).
    Seulement des noms de problèmes, aucun contenu. /healthz reste la sonde de vie du conteneur."""
    from starlette.concurrency import run_in_threadpool

    from . import watchdog

    st = await run_in_threadpool(watchdog.status)
    return JSONResponse(st, status_code=200 if st["ok"] else 503)


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
    Route("/entrer/{value}", login_link, methods=["GET", "POST"]),
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
    Route("/admin/usage", usage_page),
    Route("/confidentialite", privacy_page),
    Route("/privacy", privacy_page),
    Route("/admin/utilisateur/{uid:int}/basculer", admin_toggle, methods=["POST"]),
    Route("/admin/utilisateur/{uid:int}/supprimer", admin_delete, methods=["POST"]),
    Route("/admin/courrier", mail_page, methods=["GET", "POST"]),
    Route("/admin/programme", admin_programme, methods=["GET", "POST"]),
    Route("/admin/seors/import", seors_import, methods=["POST", "OPTIONS"]),
    Route("/try", demo_page),
    Route("/try/ask", demo_ask, methods=["POST"]),
    Route("/try/r/{aid}", demo_answer_page),
    Route("/ask", ask_page),
    Route("/ask/c/{cid}", ask_page),
    Route("/ask/c/{cid}/delete", ask_delete, methods=["POST"]),
    Route("/questions", _redirect("/ask")),
    Route("/essayer", _redirect("/try")),
    Route("/for/{who}", audience_page),
    Route("/pour/{who}", audience_page),
    Route("/healthz", health),
    Route("/healthz/deep", health_deep),
    Route("/robots.txt", robots),
    Route("/sitemap.xml", sitemap),
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
