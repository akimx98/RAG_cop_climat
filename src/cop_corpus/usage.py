"""Mesure d'usage respectueuse de la vie privée : MCP (canal principal), Telegram, site.

Ce qu'on enregistre, par appel : date, canal, outil (ou commande), client LLM, pseudonyme du compte, langue, dossiers
de négociation détectés, paramètres non textuels (niveaux, types, session, partie…), nombre de résultats, issue
(ok, vide, partiel, refus, erreur), durée.

Ce qu'on n'enregistre jamais : jeton, adresse IP, contenu des documents, réponse du LLM (elle ne passe pas par nous :
via MCP, on ne voit que les appels d'outils, pas la question posée au LLM).

Texte libre (requête, sujet, mots-clés) : gardé seulement quand la recherche n'a rien trouvé (pour repérer les trous
du corpus), USAGE_TEXT_DAYS jours, puis effacé ; jamais enregistré pour les comptes à accès restreint (contributeurs, délégation) : leurs requêtes peuvent révéler un
travail en cours. Événements entiers supprimés après USAGE_KEEP_DAYS jours, et avec le compte s'il est supprimé.

Le compte n'apparaît que sous un pseudonyme (HMAC de son identifiant avec le secret du site) : un export de la table
ne contient ni adresse e-mail ni identifiant ; le lien n'est refait qu'à l'affichage des statistiques agrégées."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import statistics
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config, db

TEXT_DAYS = config.USAGE_TEXT_DAYS
KEEP_DAYS = config.USAGE_KEEP_DAYS
DEMO_KEEP_DAYS = config.DEMO_KEEP_DAYS
SESSION_GAP_MIN = 15   # deux appels d'un même compte à moins de 15 min : même « conversation »

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
  id       INTEGER PRIMARY KEY,
  at       TEXT NOT NULL,   -- UTC, à la seconde
  channel  TEXT NOT NULL,   -- mcp | telegram | web
  kind     TEXT NOT NULL,   -- tool | prompt | connect | command | page
  name     TEXT,            -- outil, prompt, commande ou page
  uid      TEXT,            -- pseudonyme du compte (NULL : visiteur anonyme)
  client   TEXT,            -- client LLM (clientInfo à la connexion, sinon User-Agent)
  lang     TEXT,
  dossiers TEXT,            -- slugs détectés, séparés par des virgules
  params   TEXT,            -- JSON : paramètres non textuels (filtres, cotes, partie…)
  text     TEXT,            -- JSON : paramètres en texte libre ; effacé après COP_USAGE_TEXT_DAYS jours
  n        INTEGER,         -- nombre de résultats principaux
  outcome  TEXT,            -- ok | vide | partiel | refus | erreur
  ms       INTEGER,
  detail   TEXT             -- message d'erreur court
);
CREATE INDEX IF NOT EXISTS usage_at ON usage_events(at);
CREATE INDEX IF NOT EXISTS usage_uid ON usage_events(uid, at);
"""

# Issues dont on garde le texte libre (et elles seules) : les recherches qui n'ont rien trouvé, pour combler le corpus.
MISSES = {"vide", "partiel", "refus"}

# Paramètres en texte libre (formulés par l'utilisateur ou son LLM) : soumis à la rétention courte.
FREE_TEXT = {"requete", "sujet", "mots_cles", "theme", "terme", "demande", "public", "interlocuteur", "titre",
             "libelle", "url", "qualifications", "texte", "q", "question"}

# Profils déclarés (facultatifs, à l'inscription ou sur la page du compte).
PROFILES = [
    ("delegation", "Délégation, négociation", "Delegation, negotiator"),
    ("presse", "Presse", "Press"),
    ("ong", "ONG, société civile, militants", "NGO, civil society, activists"),
    ("recherche", "Recherche, université", "Research, academia"),
    ("etudiant", "Études", "Student"),
    ("institution", "Institution, administration", "Institution, government"),
    ("entreprise", "Entreprise, conseil", "Business, consultancy"),
    ("autre", "Autre", "Other"),
]
PROFILE_KEYS = {k for k, _, _ in PROFILES}

# Familles d'usage (typologie observée) : outil MCP, prompt ou commande Telegram → famille.
FAMILIES = {
    "veille": {"nouveautes", "mes_sujets", "suivre", "quoi_de_neuf", "/brief", "/nouveau", "/sujets", "/suivre",
               "/arreter", "/suivi", "/textes"},
    "negociation": {"dossier", "dossiers", "crochets", "comparer_versions", "historique_formulation", "positions", "soumissions",
                    "comparer_soumissions", "groupes", "point_negociation", "position_pays", "preparer_rencontre",
                    "/dossier", "/dossiers"},
    "agenda": {"programme", "pavillons"},
    "verification": {"rechercher", "lire_passage", "document", "donnees", "plaidoyer", "/cherche"},
    "decouverte": {"glossaire", "decouvrir_sujet", "etat_corpus"},
    "contribution": {"deposer_url", "/note"},
}
FAMILY_OF = {name: fam for fam, names in FAMILIES.items() for name in names}
FAMILY_LABELS = {"veille": "Veille", "negociation": "Négociation", "agenda": "Agenda", "verification": "Recherche, vérification",
                 "decouverte": "Découverte", "contribution": "Contribution", "mixte": "Mixte"}

_ready: set[str] = set()


def ensure_schema(conn) -> None:
    key = str(config.DB_PATH)
    if key not in _ready:
        conn.executescript(SCHEMA)
        _ready.add(key)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def uid(user_id: int | None) -> str | None:
    if not user_id:
        return None
    return hmac.new(config.SECRET.encode(), f"usage:{user_id}".encode(), hashlib.sha256).hexdigest()[:16]


def keeps_text(user: dict | None) -> bool:
    """Texte libre enregistré ? Jamais pour les comptes à accès restreint (contributeurs, délégation, admins)."""
    return not (user and (user.get("can_restricted") or user.get("is_admin")))


# --- dossiers détectés dans un texte -----------------------------------------------------------------------------------

def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9.]+", " ", text).strip()


_PATTERNS: list[tuple[str, re.Pattern]] | None = None


def _patterns() -> list[tuple[str, re.Pattern]]:
    global _PATTERNS
    if _PATTERNS is None:
        from . import live

        out = []
        for d in live.dossiers():
            terms = {_norm(str(a)) for a in d.get("alias", [])}
            terms |= {_norm(t) for t in (d.get("requete") or "").split(",")}
            terms = {t for t in terms if len(t) >= 3}
            if terms:
                alt = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
                out.append((d["slug"], re.compile(rf"\b(?:{alt})\b")))
        _PATTERNS = out
    return _PATTERNS


def detect_dossiers(*texts: str | None) -> list[str]:
    """Dossiers de négociation évoqués (alias ou expressions de leur requête), au plus 3."""
    text = " ".join(_norm(t) for t in texts if t)
    if not text:
        return []
    return [slug for slug, rx in _patterns() if rx.search(text)][:3]


# --- écriture ------------------------------------------------------------------------------------------------------

def record(channel: str, kind: str, name: str | None, user: dict | None = None, *, args: dict | None = None,
           client: str | None = None, lang: str | None = None, n: int | None = None, outcome: str | None = None,
           ms: int | None = None, detail: str | None = None, conn=None) -> None:
    """Enregistre un événement ; ne lève jamais (la mesure ne doit pas casser le service)."""
    if user and user.get("usage_optout"):
        return
    try:
        conn = conn or db.get()
        ensure_schema(conn)
        args = {k: v for k, v in (args or {}).items() if v not in (None, "", [], {}) and k != "ctx"}
        free = {k: v for k, v in args.items() if k in FREE_TEXT}
        params = {k: v for k, v in args.items() if k not in FREE_TEXT}
        slugs = []
        for key in ("dossier", "slug"):
            if isinstance(params.get(key), str):
                from . import live

                cands = live.dossier_candidates(params[key])
                slugs += cands[:1]
        slugs += [s for s in detect_dossiers(*(str(v) for v in free.values())) if s not in slugs]
        if lang is None and user:
            from . import i18n

            lang = i18n.default_for(user)
        conn.execute(
            "INSERT INTO usage_events(at,channel,kind,name,uid,client,lang,dossiers,params,text,n,outcome,ms,detail)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), channel, kind, (name or "")[:40] or None, uid((user or {}).get("id")), (client or "")[:80] or None,
             lang, ",".join(slugs[:3]) or None,
             json.dumps(params, ensure_ascii=False)[:400] if params else None,
             json.dumps(free, ensure_ascii=False)[:600] if free and keeps_text(user) and outcome in MISSES else None,
             n, outcome, ms, (detail or "")[:200] or None))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"[usage] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


# --- MCP : lecture des messages JSON-RPC au passage -----------------------------------------------------------------

def _tool_data(result: dict) -> Any:
    sc = result.get("structuredContent")
    if isinstance(sc, dict):
        return sc["result"] if set(sc) == {"result"} else sc
    for c in result.get("content") or []:
        if c.get("type") == "text":
            try:
                return json.loads(c.get("text") or "")
            except ValueError:
                return None
    return None


# Liste principale de la réponse de chaque outil (pour compter les résultats et repérer le « rien trouvé »).
PRIMARY = {"nouveautes": "documents", "positions": "positions", "glossaire": "resultats", "programme": "evenements",
           "pavillons": "evenements", "crochets": "crochets", "soumissions": "soumissions", "dossiers": "dossiers",
           "lire_passage": "passages"}


def outcome_of(tool: str, data: Any) -> tuple[int | None, str, str | None]:
    """(nombre de résultats, issue, détail) d'une réponse d'outil."""
    if not isinstance(data, dict):
        return None, "ok", None
    if data.get("erreur"):
        return 0, "refus", str(data["erreur"])
    if tool == "rechercher":
        n = int(data.get("nb") or 0)
        return n, ("ok" if n else "partiel" if data.get("pertinence_partielle") else "vide"), None
    if tool == "dossier":   # état, textes et positions : vide seulement si tout l'est
        n = sum(len(data.get(k) or []) for k in ("etat", "textes", "positions_cop", "positions_ecrites",
                                                  "positions_reference"))
        return n, ("vide" if n == 0 else "ok"), None
    key = PRIMARY.get(tool)
    if key is None or not isinstance(data.get(key), list):
        return None, "ok", None   # pas de mesure fiable du « rien trouvé » pour cet outil
    n = len(data[key])
    return n, ("vide" if n == 0 else "ok"), None


def _client_from_headers(scope) -> str | None:
    for k, v in scope.get("headers") or []:
        if k == b"user-agent":
            return v.decode("latin-1")[:80]
    return None


def _events_from(req: Any, resp: Any) -> list[dict]:
    reqs = req if isinstance(req, list) else [req]
    resps = resp if isinstance(resp, list) else [resp]
    by_id = {r.get("id"): r for r in resps if isinstance(r, dict)}
    out = []
    for m in reqs:
        if not isinstance(m, dict):
            continue
        method, p = m.get("method"), m.get("params") or {}
        r = by_id.get(m.get("id")) or {}
        err = r.get("error")
        if method == "tools/call":
            name = p.get("name")
            result = r.get("result") or {}
            if err or result.get("isError"):
                text = (err or {}).get("message") or next(
                    (c.get("text") for c in result.get("content") or [] if c.get("type") == "text"), "")
                n, outcome, detail = None, "erreur", text
            else:
                n, outcome, detail = outcome_of(name, _tool_data(result))
            out.append({"kind": "tool", "name": name, "args": p.get("arguments") or {}, "n": n, "outcome": outcome,
                        "detail": detail})
        elif method == "prompts/get":
            out.append({"kind": "prompt", "name": p.get("name"), "args": p.get("arguments") or {},
                        "outcome": "erreur" if err else "ok", "detail": (err or {}).get("message")})
        elif method == "initialize":
            ci = p.get("clientInfo") or {}
            client = " ".join(x for x in (ci.get("name"), ci.get("version")) if x) or None
            out.append({"kind": "connect", "name": p.get("protocolVersion"), "client": client})
    return out


async def track_mcp(app, scope, receive, send) -> None:
    """Enveloppe ASGI d'une requête MCP authentifiée : laisse passer, puis journalise les appels d'outils."""
    if scope.get("method") != "POST":
        await app(scope, receive, send)
        return
    body = b""
    while True:
        msg = await receive()
        if msg["type"] != "http.request":
            break
        body += msg.get("body", b"")
        if not msg.get("more_body"):
            break
    replayed = False

    async def recv():
        nonlocal replayed
        if not replayed:
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    chunks: list[bytes] = []
    size = 0

    async def snd(message):
        nonlocal size
        if message["type"] == "http.response.body" and size < 4_000_000:
            b = message.get("body", b"")
            chunks.append(b)
            size += len(b)
        await send(message)

    t0 = time.monotonic()
    try:
        await app(scope, recv, snd)
    finally:
        ms = int((time.monotonic() - t0) * 1000)
        user = scope.get("cop_user")
        ua = _client_from_headers(scope)

        def write():
            try:
                req = json.loads(body or b"null")
            except ValueError:
                return
            try:
                resp = json.loads(b"".join(chunks) or b"null") if size < 4_000_000 else None
            except ValueError:
                resp = None
            for ev in _events_from(req, resp):
                record("mcp", ev["kind"], ev.get("name"), user, args=ev.get("args"), client=ev.get("client") or ua,
                       n=ev.get("n"), outcome=ev.get("outcome"), ms=ms, detail=ev.get("detail"))

        try:
            import anyio

            await anyio.to_thread.run_sync(write)
        except Exception as exc:  # noqa: BLE001
            print(f"[usage] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


# --- rétention ------------------------------------------------------------------------------------------------------

PURGE_BATCH = 5000   # lignes anonymes de pages /doc supprimées par lot (transaction courte)


def _purge_anonymous_docs(conn) -> int:
    """Pages /doc vues sans compte (plus enregistrées depuis le 27/11) : au plus PURGE_BATCH lignes par passage,
    en une transaction courte ; le reste au passage horaire suivant."""
    n = conn.execute("DELETE FROM usage_events WHERE id IN (SELECT id FROM usage_events WHERE channel='web' "
                     "AND kind='page' AND name='document' AND uid IS NULL LIMIT ?)", (PURGE_BATCH,)).rowcount
    conn.commit()
    return n


def purge(conn) -> dict[str, int]:
    ensure_schema(conn)
    now = datetime.now(timezone.utc)
    iso = lambda d: (now - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    out = {
        "texte_efface": conn.execute("UPDATE usage_events SET text=NULL WHERE text IS NOT NULL AND at<?",
                                     (iso(TEXT_DAYS),)).rowcount,
        "evenements_supprimes": conn.execute("DELETE FROM usage_events WHERE at<?", (iso(KEEP_DAYS),)).rowcount,
    }
    conn.commit()
    out["pages_anonymes_supprimees"] = _purge_anonymous_docs(conn)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='demo_answers'").fetchone():
        out["demo_supprimees"] = conn.execute("DELETE FROM demo_answers WHERE created_at<?",
                                              (iso(DEMO_KEEP_DAYS),)).rowcount
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations'").fetchone():
            conn.execute("DELETE FROM conversations WHERE NOT EXISTS (SELECT 1 FROM demo_answers a "
                         "WHERE a.conv_id=conversations.id) AND updated_at<?", (iso(DEMO_KEEP_DAYS),))
    conn.commit()
    return out


def forget_user(conn, user_id: int, demo: bool = True) -> None:
    """Suppression de compte (ou opposition) : ses événements, et à la suppression ses questions de démo."""
    ensure_schema(conn)
    conn.execute("DELETE FROM usage_events WHERE uid=?", (uid(user_id),))
    if demo and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='demo_answers'").fetchone():
        conn.execute("DELETE FROM demo_answers WHERE user_id=?", (user_id,))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations'").fetchone():
            conn.execute("DELETE FROM conversations WHERE user_id=?", (user_id,))


# --- statistiques (page /admin/usage) ----------------------------------------------------------------------------------

def client_label(raw: str | None) -> str:
    s = (raw or "").lower()
    for needle, label in (("claude-code", "Claude Code"), ("claude code", "Claude Code"), ("claude", "Claude"),
                          ("anthropic", "Claude"), ("openai", "ChatGPT"), ("chatgpt", "ChatGPT"),
                          ("mistral", "Le Chat"), ("lechat", "Le Chat"), ("le chat", "Le Chat"), ("cursor", "Cursor"),
                          ("vscode", "VS Code"), ("visual studio", "VS Code"), ("gemini", "Gemini"),
                          ("mcp-remote", "mcp-remote"), ("inspector", "MCP Inspector")):
        if needle in s:
            return label
    if not s:
        return "inconnu"
    return s.split("/")[0][:30]


def _family(names: Counter) -> str:
    fam = Counter()
    for name, c in names.items():
        if name in FAMILY_OF:
            fam[FAMILY_OF[name]] += c
    if not fam:
        return "mixte"
    top, c = fam.most_common(1)[0]
    return top if c >= 0.5 * sum(fam.values()) else "mixte"


def stats(conn, days: int = 7, include_admins: bool = False) -> dict[str, Any]:
    ensure_schema(conn)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    users = [dict(r) for r in conn.execute(
        "SELECT id, is_admin, can_restricted, source, profil, organisation, created_at, telegram_chat_id IS NOT NULL AS tg"
        " FROM users WHERE active=1 AND COALESCE(source,'')<>'supprime'")]
    by_uid = {uid(u["id"]): u for u in users}
    admins = {k for k, u in by_uid.items() if u["is_admin"]}
    rows = [dict(r) for r in conn.execute("SELECT * FROM usage_events WHERE at>=? ORDER BY at", (since,))]
    if not include_admins:
        rows = [r for r in rows if r["uid"] not in admins]
    calls = [r for r in rows if r["kind"] in ("tool", "prompt", "command")]
    acts = [r for r in rows if r["kind"] != "connect"]   # appels + pages suivies du site

    # Canaux
    channels = {}
    for ch in ("mcp", "telegram", "web"):
        rs = [r for r in calls if r["channel"] == ch] if ch != "web" else [r for r in rows if r["channel"] == "web"]
        channels[ch] = {"appels": len(rs), "comptes": len({r["uid"] for r in rs if r["uid"]})}

    # Actifs par jour
    daily: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for r in calls:
        if r["uid"]:
            daily[r["at"][:10]][r["channel"]].add(r["uid"])
    per_day = [{"jour": d, **{ch: len(v.get(ch, ())) for ch in ("mcp", "telegram")},
                "total": len(set().union(*v.values()))} for d, v in sorted(daily.items())]

    # Clients LLM (comptes distincts)
    clients: dict[str, set] = defaultdict(set)
    connects = Counter()
    for r in rows:
        if r["channel"] == "mcp" and r["uid"]:
            clients[client_label(r["client"])].add(r["uid"])
            if r["kind"] == "connect":
                connects[client_label(r["client"])] += 1
    client_rows = sorted(({"client": k, "comptes": len(v), "connexions": connects[k]} for k, v in clients.items()),
                         key=lambda x: -x["comptes"])

    # Outils
    tools: dict[str, list] = defaultdict(list)
    for r in acts:
        tools[(r["channel"], r["kind"], r["name"])].append(r)
    tool_rows = []
    for (ch, kind, name), rs in tools.items():
        ms = [r["ms"] for r in rs if r["ms"] is not None]
        oc = Counter(r["outcome"] for r in rs)
        tool_rows.append({"canal": ch, "type": kind, "nom": name, "appels": len(rs),
                          "comptes": len({r["uid"] for r in rs if r["uid"]}),
                          "vide": oc["vide"] + oc["partiel"], "refus": oc["refus"], "erreur": oc["erreur"],
                          "ms_median": int(statistics.median(ms)) if ms else None,
                          "famille": FAMILY_LABELS.get(FAMILY_OF.get(name, ""), "")})
    tool_rows.sort(key=lambda x: -x["appels"])

    # Dossiers
    dos: dict[str, list] = defaultdict(list)
    for r in acts:
        for s in (r["dossiers"] or "").split(","):
            if s:
                dos[s].append(r)
    from . import live

    names = {d["slug"]: d["nom"] for d in live.dossiers()}
    dossier_rows = sorted(({"slug": s, "nom": names.get(s, s), "appels": len(rs),
                            "comptes": len({r["uid"] for r in rs if r["uid"]})} for s, rs in dos.items()),
                          key=lambda x: -x["appels"])

    # Recherches sans résultat (texte gardé pour les seuls comptes invités, et seulement TEXT_DAYS jours)
    misses = []
    for r in reversed(calls):
        if r["outcome"] in ("vide", "partiel") and r["text"]:
            try:
                t = json.loads(r["text"])
            except ValueError:
                continue
            misses.append({"at": r["at"], "nom": r["name"], "issue": r["outcome"],
                           "texte": " · ".join(str(v) for v in t.values())[:200], "params": r["params"] or ""})
        if len(misses) >= 60:
            break
    errors = [{"at": r["at"], "canal": r["channel"], "nom": r["name"], "issue": r["outcome"], "detail": r["detail"]}
              for r in reversed(calls) if r["outcome"] in ("erreur", "refus")][:40]

    # Conversations (suite d'appels d'un compte espacés de moins de SESSION_GAP_MIN)
    seqs: list[list[dict]] = []
    last: dict[str, tuple[datetime, list]] = {}
    for r in calls:
        if r["channel"] != "mcp" or not r["uid"]:
            continue
        t = datetime.strptime(r["at"], "%Y-%m-%dT%H:%M:%SZ")
        prev = last.get(r["uid"])
        if prev and (t - prev[0]).total_seconds() <= SESSION_GAP_MIN * 60:
            prev[1].append(r)
            last[r["uid"]] = (t, prev[1])
        else:
            seq = [r]
            seqs.append(seq)
            last[r["uid"]] = (t, seq)
    lens = [len(s) for s in seqs]
    firsts = Counter(s[0]["name"] for s in seqs)
    pairs = Counter((a["name"], b["name"]) for s in seqs for a, b in zip(s, s[1:]) if a["name"] != b["name"])
    conversations = {"nombre": len(seqs), "appels_median": statistics.median(lens) if lens else 0,
                     "appels_max": max(lens) if lens else 0, "premier_outil": firsts.most_common(8),
                     "enchainements": [(f"{a} → {b}", c) for (a, b), c in pairs.most_common(12)]}

    # Heures (fuseau de la COP)
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(config.TIMEZONE)
    hours = Counter(datetime.strptime(r["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(tz).hour
                    for r in calls)
    hour_rows = [{"h": h, "n": hours.get(h, 0)} for h in range(24)]

    # Typologie : déclarée × observée
    per_user: dict[str, Counter] = defaultdict(Counter)
    for r in calls:
        if r["uid"]:
            per_user[r["uid"]][r["name"]] += 1
    population = [u for k, u in by_uid.items() if include_admins or k not in admins]
    typ: dict[str, dict[str, Any]] = {}
    for key, fr, _ in PROFILES + [("", "Non renseigné", "")]:
        members = [u for u in population if (u.get("profil") or "") == key]
        active = [uid(u["id"]) for u in members if uid(u["id"]) in per_user]
        fam = Counter(_family(per_user[a]) for a in active)
        typ[key or "-"] = {"label": fr, "comptes": len(members), "actifs": len(active),
                           "appels": sum(sum(per_user[a].values()) for a in active),
                           "familles": {FAMILY_LABELS[f]: c for f, c in fam.most_common()}}
    families = Counter(_family(c) for c in per_user.values())

    # Entonnoir (depuis l'ouverture du journal, tous les comptes non admin)
    ever = {r["uid"] for r in conn.execute("SELECT DISTINCT uid FROM usage_events WHERE kind IN ('tool','prompt','command')")}
    mcp_ever = {r["uid"] for r in conn.execute(
        "SELECT DISTINCT uid FROM usage_events WHERE channel='mcp' AND kind IN ('tool','prompt')")}
    days_by_uid = defaultdict(set)
    for r in conn.execute("SELECT uid, substr(at,1,10) d FROM usage_events WHERE kind IN ('tool','prompt','command')"):
        days_by_uid[r["uid"]].add(r["d"])
    pop_uids = [uid(u["id"]) for u in population]
    subs = {r[0] for r in conn.execute("SELECT DISTINCT user_id FROM subscriptions WHERE active=1")}
    try:
        subs |= {r[0] for r in conn.execute("SELECT DISTINCT user_id FROM dossier_follows")}
    except Exception:  # noqa: BLE001
        pass
    funnel = [
        ("Comptes", len(population)),
        ("Telegram lié", sum(1 for u in population if u["tg"])),
        ("Sujets ou dossiers suivis", sum(1 for u in population if u["id"] in subs)),
        ("Au moins un appel (tous canaux)", sum(1 for k in pop_uids if k in ever)),
        ("Au moins un appel MCP", sum(1 for k in pop_uids if k in mcp_ever)),
        ("Revenus un 2e jour", sum(1 for k in pop_uids if len(days_by_uid.get(k, ())) >= 2)),
        ("Actifs 3 jours ou plus", sum(1 for k in pop_uids if len(days_by_uid.get(k, ())) >= 3)),
    ]

    # Démo publique (table propre)
    demo = {"questions": 0, "langues": [], "dossiers": []}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='demo_answers'").fetchone():
        admin_ids = {u["id"] for u in users if u["is_admin"]}
        qs = [q for q in conn.execute("SELECT lang, question, error, user_id FROM demo_answers WHERE created_at>=?",
                                      (since,)).fetchall() if include_admins or q["user_id"] not in admin_ids]
        demo["questions"] = len(qs)
        demo["erreurs"] = sum(1 for q in qs if q["error"])
        demo["langues"] = Counter(q["lang"] or "?" for q in qs).most_common()
        demo["dossiers"] = Counter(s for q in qs for s in detect_dossiers(q["question"])).most_common(10)

    first = conn.execute("SELECT MIN(at) FROM usage_events").fetchone()[0]
    return {"jours": days, "depuis": since, "journal_depuis": first, "canaux": channels, "par_jour": per_day,
            "clients": client_rows, "outils": tool_rows, "dossiers": dossier_rows, "sans_resultat": misses,
            "erreurs": errors, "conversations": conversations, "heures": hour_rows, "typologie": typ,
            "familles": [(FAMILY_LABELS[f], c) for f, c in families.most_common()], "entonnoir": funnel,
            "demo": demo, "admins_inclus": include_admins, "texte_jours": TEXT_DAYS, "garde_jours": KEEP_DAYS}
