"""Démo publique « Essayer maintenant » : quelques questions sans compte, réponses sourcées.

Le modèle (OpenAI) interroge le corpus avec les MÊMES outils que le connecteur MCP, mais en lecture seule et comme
un visiteur anonyme : documents publics uniquement (aucun document interne ne part vers l'API). Chaque passage
renvoyé reçoit une étiquette [S1], [S2]… que le modèle doit citer ; chaque citation entre guillemets est ensuite
vérifiée mot pour mot dans le passage source avant affichage. Les événements du programme reçoivent [E1]… et
s'exportent en .ics.

Garde-fous : nombre de questions par session, par adresse IP et par jour, plafond de dépense quotidien et total
propres à la démo (table llm_usage, purpose='demo').
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import unicodedata
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable

from markupsafe import Markup, escape

from . import config, db, llm

MODEL = config.DEMO_MODEL
EFFORT = config.DEMO_EFFORT
PER_SESSION = config.DEMO_PER_SESSION
PER_IP_DAY = config.DEMO_PER_IP_DAY
PER_ACCOUNT_DAY = config.DEMO_PER_ACCOUNT_DAY
DAILY_USD = config.DEMO_DAILY_USD
TOTAL_USD = config.DEMO_BUDGET_USD
MAX_ROUNDS = 9
MAX_CALLS = 14            # appels d'outils par question
MAX_CALLS_PER_ROUND = 5
MAX_TOOL_CHARS = 14000
MAX_QUESTION = 600

# Visiteur de la démo : jamais administrateur ni contributeur → documents publics seulement.
DEMO_USER = {"id": 0, "email": "demo", "name": "demo", "is_admin": 0, "can_restricted": 0, "active": 1,
             "source": "demo", "lang": "en"}
_CTX = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(scope={"cop_user": DEMO_USER})))

# Outils exposés (lecture seule) et paramètres masqués (propres à un compte, ou sans objet pour un anonyme).
TOOLS = ("rechercher", "lire_passage", "document", "comparer_versions", "crochets", "nouveautes", "dossiers",
         "dossier", "positions", "programme", "pavillons", "soumissions", "groupes", "glossaire", "donnees")
HIDDEN = {"mes_sujets", "acces"}

STEP_LABELS = {
    "rechercher": ("Recherche dans le corpus", "Searching the corpus"),
    "lire_passage": ("Lecture du passage", "Reading the passage"),
    "document": ("Fiche du document", "Document record"),
    "comparer_versions": ("Comparaison des versions", "Comparing versions"),
    "crochets": ("Relevé des crochets", "Listing brackets"),
    "nouveautes": ("Nouveautés du corpus", "What's new in the corpus"),
    "dossiers": ("Liste des dossiers de négociation", "Negotiation items"),
    "dossier": ("État du dossier", "Negotiation item status"),
    "positions": ("Positions des Parties", "Parties' positions"),
    "programme": ("Programme de la COP", "COP programme"),
    "pavillons": ("Programme des pavillons", "Pavilion programmes"),
    "soumissions": ("Soumissions officielles", "Official submissions"),
    "groupes": ("Groupes de négociation", "Negotiating groups"),
    "glossaire": ("Glossaire", "Glossary"),
    "donnees": ("Données d'émissions", "Emissions data"),
    "etat_corpus": ("État du corpus", "Corpus status"),
}

SYSTEM = """You are the public demo of COP Tracker, an independent, free and open-source tool that answers
questions about the COP31 climate negotiations (Antalya, 9–20 November 2026) ONLY from its verifiable corpus:
UN law and decisions, draft negotiation texts and all their versions, Parties' submissions and positions,
daily negotiation reports, newsletters and briefs, the official programme and pavilion events.
Today is {today} (Antalya time).

ABSOLUTE RULES
1. Use the tools before answering. Nothing factual from memory: every fact must come from a tool result.
   Nothing relevant found → say plainly "not found in the corpus" (in the user's language) and suggest a
   better question.
2. Each tool result item carries a "ref" (S1, S2… for sources, E1, E2… for events). After EVERY factual
   sentence, put the ref(s) it relies on in square brackets, e.g. [S3] or [S3][S7]. Never invent a ref.
3. Quote the decisive wording EXACTLY, copied character for character from the source, between
   double quotes, immediately followed by its ref: "…exact words…" [S3]. Quotes stay in the original
   language. Keep quotes short (5–30 words). A quote you cannot copy exactly → paraphrase without quotes.
4. Give the reliability: adopted decision (level 1) vs official document not adopted (level 2: draft, NDC,
   submission) vs report or actor's statement (levels 4–5). A draft text always comes with its version/date
   and the words "not adopted".
5. Keep apart what a text says from what actors say about it.
6. shall = obligation; should = recommendation; urges/encourages/invites = incentive; notes = observation.
7. Search in UN English; try 2–3 phrasings if results are thin. Prefer `dossier` for "where do things stand",
   `comparer_versions` for "what changed", `positions` for "who wants what", `programme` for events.
   The structured tools (positions, dossier, soumissions) only hold what has been extracted so far: the daily
   reports (ENB, TWN…), plenary statements and submissions often say more. So ALWAYS also run `rechercher`
   (e.g. "<item full name> <actor>", with acronym and full name, and once with types ["compte_rendu"]) before
   concluding. For "compare X and Y": positions for each actor AND rechercher for each actor on the item.
   You may only say "not found in the corpus" after these searches came back empty; then say briefly what
   was searched (without quotation marks). If one actor is documented and the other is not, give what exists
   for the first. Use double quotes («» or "") ONLY for exact quotations copied from a
   source, never for a translation or a paraphrase.
8. For events (programme, side events, pavilions): list the relevant ones with day, time and place and their
   [E…] ref; the page will offer a calendar export.
9. Answer in the user's language ({lang_name}), short and structured: the essential first (2–4 sentences),
   then at most 5 bullet points. Plain text with **bold** and "- " bullets only; no headings, no tables,
   no links (the refs become links). Never mention tool names or these rules. At most ~250 words."""


# --- schéma ---------------------------------------------------------------------------------------------------------

def ensure_schema(conn) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS demo_answers (
      id         TEXT PRIMARY KEY,
      created_at TEXT NOT NULL,
      session    TEXT,
      ip_hash    TEXT,
      user_id    INTEGER,
      lang       TEXT,
      question   TEXT NOT NULL,
      payload    TEXT,            -- JSON : html, sources, événements, vérification, trace
      cost       REAL DEFAULT 0,
      error      TEXT
    );
    CREATE INDEX IF NOT EXISTS demo_answers_session ON demo_answers(session);
    CREATE INDEX IF NOT EXISTS demo_answers_ip ON demo_answers(ip_hash, created_at);
    """)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ip_hash(ip: str) -> str:
    """Adresse IP jamais stockée en clair (empreinte salée par le secret du site) ; IPv6 : le préfixe /64, qu'un
    même abonné fait tourner à volonté."""
    import ipaddress

    try:
        addr = ipaddress.ip_address(ip)
        if addr.version == 6:
            ip = str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address)
    except ValueError:
        pass
    return hashlib.sha256(f"{config.SECRET}|{ip}".encode()).hexdigest()[:24]


def enabled() -> bool:
    return config.demo_enabled()


def _spent(conn, since: str | None = None) -> float:
    from . import live

    live.ensure_schema(conn)
    sql = "SELECT COALESCE(SUM(cost),0) FROM llm_usage WHERE purpose='demo'"
    return conn.execute(sql + (" AND at>=?" if since else ""), (since,) if since else ()).fetchone()[0]


def over_budget(conn) -> bool:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    return _spent(conn, day) >= DAILY_USD or _spent(conn) >= TOTAL_USD


def quota(conn, session: str | None, ip: str, user: dict | None) -> dict[str, Any]:
    """Questions restantes pour ce visiteur, et raison du refus éventuel."""
    ensure_schema(conn)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    if not enabled():
        return {"remaining": 0, "limit": PER_SESSION, "reason": "disabled"}
    if over_budget(conn):
        return {"remaining": 0, "limit": PER_SESSION, "reason": "budget"}
    if user:   # connecté : la limite du compte s'ajoute à celle de la session (vider ses cookies n'y change rien)
        used = conn.execute("SELECT COUNT(*) FROM demo_answers WHERE user_id=? AND created_at>=? AND error IS NULL",
                            (user["id"], day)).fetchone()[0]
        if used >= PER_ACCOUNT_DAY:
            return {"remaining": 0, "limit": PER_SESSION, "reason": "account"}
    used_s = conn.execute("SELECT COUNT(*) FROM demo_answers WHERE session=? AND error IS NULL",
                          (session or "-",)).fetchone()[0] if session else 0
    used_ip = conn.execute("SELECT COUNT(*) FROM demo_answers WHERE ip_hash=? AND created_at>=?",
                           (ip_hash(ip), day)).fetchone()[0]
    left = min(PER_SESSION - used_s, PER_IP_DAY - used_ip,
               PER_ACCOUNT_DAY - used if user else PER_SESSION)
    reason = None if left > 0 else ("session" if used_s >= PER_SESSION else "ip")
    return {"remaining": max(0, left), "limit": PER_SESSION, "reason": reason}


# --- outils ---------------------------------------------------------------------------------------------------------

def _registry():
    from . import mcp_server

    return {t.name: t for t in mcp_server.mcp._tool_manager.list_tools() if t.name in TOOLS}


def tool_specs() -> list[dict[str, Any]]:
    specs = []
    for name, t in _registry().items():
        params = json.loads(json.dumps(t.parameters))
        for h in HIDDEN:
            params.get("properties", {}).pop(h, None)
            if h in params.get("required", []):
                params["required"].remove(h)
        specs.append({"type": "function", "function": {
            "name": name, "description": (t.description or "")[:1800], "parameters": params}})
    return specs


def call_tool(name: str, args: dict[str, Any]) -> Any:
    tools = _registry()
    if name not in tools:
        return {"erreur": f"unknown tool {name}"}
    t = tools[name]
    args = {k: v for k, v in (args or {}).items() if k not in HIDDEN}
    try:
        parsed = t.fn_metadata.arg_model.model_validate(args)
        kwargs = parsed.model_dump_one_level()
        if t.context_kwarg:
            kwargs[t.context_kwarg] = _CTX
        out = t.fn(**kwargs)
    except Exception as exc:  # argument invalide, document introuvable…
        return {"erreur": f"{type(exc).__name__}: {str(exc)[:300]}"}
    if name == "dossier" and isinstance(out, dict):
        # Le brief IA porte ses propres renvois [S1]… : ils se confondraient avec ceux de la démo.
        out.pop("brief", None)
    return out


# --- étiquetage des sources et des événements -----------------------------------------------------------------------

def _page_link(url: str | None, page: Any) -> str | None:
    if url and page and ".pdf" in url.lower() and "#" not in url:
        return f"{url}#page={page}"
    return url


class Refs:
    """Registre des sources ([S…]) et événements ([E…]) vus pendant une réponse."""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        """`state` : registre d'une réponse précédente (question de suivi) ; la numérotation continue, et les
        renvois de la réponse précédente restent valables."""
        state = state or {}
        self.sources: dict[str, dict[str, Any]] = dict(state.get("sources") or {})
        self.events: dict[str, dict[str, Any]] = dict(state.get("events") or {})
        self._seen: dict[str, str] = dict(state.get("seen") or {})

    def state(self) -> dict[str, Any]:
        return {"sources": self.sources, "events": self.events, "seen": self._seen}

    def _key(self, kind: str, ident: str) -> str | None:
        return self._seen.get(f"{kind}:{ident}")

    def add_source(self, item: dict[str, Any]) -> str | None:
        pid = item.get("passage_id")
        url = item.get("lien_source") or item.get("source_url") or item.get("url_source") or item.get("lien")
        text = item.get("texte") or item.get("text") or item.get("extrait") or item.get("quote") or ""
        if pid:
            ident = f"p{pid}"
        elif item.get("quote") and url:
            ident = "q" + hashlib.md5(f"{url}|{item['quote']}".encode()).hexdigest()[:10]
        elif url and (item.get("cote") or item.get("symbol") or item.get("titre") or item.get("title")):
            ident = "d" + hashlib.md5(str(url).encode()).hexdigest()[:10]
        else:
            return None
        known = self._key("S", ident)
        if known:
            return known
        ref = f"S{len(self.sources) + 1}"
        self._seen[f"S:{ident}"] = ref
        doc_id = item.get("doc_id")
        if not doc_id and str(item.get("source_url") or "").startswith("/doc/"):
            doc_id = int(re.sub(r"\D.*", "", item["source_url"][5:]) or 0) or None   # position : /doc/<id>
        level = item.get("niveau")
        if not level and item.get("quote") and not pid:   # position : le niveau dépend de sa source
            level = {"soumission": "2", "carbonbrief": "4"}.get(item.get("source_kind"), "")
            if not level and doc_id:
                row = db.get().execute("SELECT level FROM documents WHERE id=? AND access='public'",
                                       (doc_id,)).fetchone()
                level = row["level"] if row else ""
        self.sources[ref] = {
            "ref": ref,
            "passage_id": pid,
            "doc_id": doc_id,
            "title": item.get("titre") or item.get("title") or item.get("source_label") or "",
            "symbol": item.get("cote") or item.get("symbol") or "",
            "level": str(level or ""),
            "page": item.get("page"),
            "para": item.get("paragraphe") or item.get("para"),
            "date": (item.get("said_on") or item.get("publie_le") or item.get("published") or item.get("date")
                     or item.get("recupere_le")),
            "actor": item.get("actor"),
            "text": text,
            "url": item.get("lien_source") or _page_link(item.get("url_source"), item.get("page"))
            or (item.get("source_url") if str(item.get("source_url") or "").startswith("http") else None)
            or item.get("pdf_url"),
            "page_url": item.get("lien_passage") or item.get("lien"),
            "kind": "passage" if pid else ("position" if item.get("quote") else "document"),
        }
        return ref

    def add_event(self, item: dict[str, Any]) -> str:
        ident = item.get("id") or f"{item.get('titre')}|{item.get('jour') or item.get('date')}|{item.get('debut') or item.get('heure')}"
        known = self._key("E", str(ident))
        if known:
            return known
        ref = f"E{len(self.events) + 1}"
        self._seen[f"E:{ident}"] = ref
        start, end = item.get("debut"), item.get("fin")
        if not start and item.get("heure"):
            parts = re.split(r"[–-]", item["heure"])
            start, end = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else None)
        days = re.findall(r"\d{4}-\d{2}-\d{2}", str(item.get("jour") or item.get("date") or ""))
        self.events[ref] = {
            "ref": ref, "title": item.get("titre") or item.get("title") or "", "day": days[0] if days else "",
            "days": days, "start": start, "end": end,
            "place": item.get("lieu") or item.get("pavillon") or item.get("room") or "",
            "zone": item.get("zone"), "kind": item.get("nature") or ("pavillon" if item.get("pavillon") else ""),
            "url": item.get("lien") or item.get("url"), "organiser": item.get("organisateur") or
            ", ".join(item.get("organisateurs") or [])[:200],
        }
        return ref

    _DOC_FIELDS = ("doc_id", "titre", "cote", "niveau", "url_source", "publie_le", "recupere_le", "session",
                   "organisation")

    def tag(self, value: Any, tool: str, parent: dict[str, Any] | None = None) -> Any:
        """Ajoute une clé "ref" aux passages, positions, documents et événements d'un résultat d'outil.
        Les passages imbriqués (voisins de lire_passage, début de document) héritent des champs du document."""
        if isinstance(value, list):
            return [self.tag(v, tool, parent) for v in value]
        if not isinstance(value, dict):
            return value
        if parent and value.get("passage_id") and not value.get("doc_id"):
            value = {**{k: parent[k] for k in self._DOC_FIELDS if parent.get(k)}, **value}
        here = value if value.get("doc_id") else parent
        out = {k: self.tag(v, tool, here) for k, v in value.items()}
        is_event = tool in ("programme", "pavillons") and out.get("titre") and (
            out.get("debut") or out.get("heure") or out.get("jour") or out.get("date"))
        if is_event:
            out["ref"] = self.add_event(out)
        else:
            ref = self.add_source(out)
            if ref:
                out["ref"] = ref
        return out


# --- vérification des citations -------------------------------------------------------------------------------------

_QUOTE_CHARS = str.maketrans({"“": '"', "”": '"', "«": '"', "»": '"', "„": '"', "’": "'", "‘": "'", "–": "-",
                              "—": "-", " ": " ", " ": " "})


def _norm(text: str, brackets: bool = False) -> str:
    text = unicodedata.normalize("NFKC", text or "").translate(_QUOTE_CHARS).lower()
    text = re.sub(r"-\s*\n\s*", "", text)          # césure de fin de ligne
    if not brackets:
        text = re.sub(r"[\[\]]", "", text)
    else:
        text = re.sub(r"\[\s+", "[", re.sub(r"\s+\]", "]", text))
    text = re.sub(r"\s+", " ", text)
    return re.sub(r" ([,;.:])", r"\1", text).strip()


def _full_text(conn, src: dict[str, Any]) -> str:
    """Texte complet du passage (et des deux suivants : une citation peut chevaucher), publics seulement."""
    if src.get("passage_id"):
        row = conn.execute("""SELECT p.doc_id, p.seq FROM passages p JOIN documents d ON d.id=p.doc_id
                              WHERE p.id=? AND d.access='public'""", (src["passage_id"],)).fetchone()
        if row:
            rows = conn.execute("SELECT text FROM passages WHERE doc_id=? AND seq BETWEEN ? AND ? ORDER BY seq",
                                (row["doc_id"], row["seq"] - 1, row["seq"] + 2)).fetchall()
            return " ".join(r["text"] for r in rows)
    return src.get("text") or ""


def _in_order(parts: list[str], hay: str) -> bool:
    pos = 0
    for part in parts:
        at = hay.find(part, pos)
        if at < 0:
            return False
        pos = at + len(part)
    return True


def _parts(quote: str, brackets: bool) -> list[str] | None:
    """Morceaux d'une citation coupée par des points de suspension ; None si un morceau est trop court pour
    prouver quoi que ce soit (« the … ban »)."""
    parts = [p.strip(" .,;:") for p in re.split(r"\s*(?:…|\.\.\.)\s*", _norm(quote, brackets))]
    parts = [p for p in parts if p]
    if not parts or any(len(p.split()) < 3 for p in parts):
        return None
    return parts


def quote_status(quote: str, source_text: str) -> str:
    """ok : retrouvée mot pour mot, dans l'ordre, crochets compris ; brackets : retrouvée seulement en ignorant
    les crochets (la citation masque ou ajoute du texte non convenu) ; no : introuvable ou trop courte."""
    exact = _parts(quote, True)
    if exact and _in_order(exact, _norm(source_text, True)):
        return "ok"
    loose = _parts(quote, False)
    if loose and _in_order(loose, _norm(source_text, False)):
        return "brackets"
    return "no"


def quote_found(quote: str, source_text: str) -> bool:
    return quote_status(quote, source_text) == "ok"


# --- mise en forme de la réponse ------------------------------------------------------------------------------------

_MARK = re.compile(r"\[((?:[SE]\d+)(?:\s*[,;]\s*[SE]\d+)*)\]")
_QUOTE = re.compile(r'(["“«])\s?([^"“”«»\x00\n]{8,600}?)\s?(["”»])((?:\s*\[(?:[SE]\d+)(?:\s*[,;]\s*[SE]\d+)*\])+)')
# Citation sans renvoi : affichée comme non vérifiée (6 mots au moins ; un terme ou une requête entre guillemets passe).
_BARE = re.compile(r'(["“«])\s?((?:[^"“”«»\x00\n\s]+\s+){5,}[^"“”«»\x00\n\s]+)\s?(["”»])')

_BADGES = {
    "ok": '<span class="qv" title="verified word for word">✓</span>',
    "brackets": '<span class="qv no" title="in brackets in the source (not agreed): the quote drops or adds them">[?]</span>',
    "no": '<span class="qv no" title="not found word for word in the source">?</span>',
    "unsourced": '<span class="qv no" title="no source given for this quote">?</span>',
}


def _quote_html(quote: str, status: str) -> str:
    cls = "q ok" if status == "ok" else "q unverified"
    return f'<q class="{cls}">{escape(quote)}</q>{_BADGES[status]}'


def render(text: str, refs: Refs, conn) -> dict[str, Any]:
    """Réponse du modèle → HTML sûr : citations vérifiées, appels de source numérotés, listes, gras."""
    order: list[str] = []
    events: list[str] = []
    checks: list[dict[str, Any]] = []
    full_cache: dict[str, str] = {}
    holders: list[str] = []

    def hold(html: str) -> str:
        holders.append(html)
        return f"\x00{len(holders) - 1}\x00"

    def marks_html(group: str) -> str:
        out = []
        for ref in re.findall(r"[SE]\d+", group):
            if ref.startswith("S") and ref in refs.sources:
                if ref not in order:
                    order.append(ref)
                n = order.index(ref) + 1
                out.append(f'<button type="button" class="cite" data-ref="{ref}" aria-label="Source {n}">{n}</button>')
            elif ref.startswith("E") and ref in refs.events:
                if ref not in events:
                    events.append(ref)
                out.append(f'<a class="cite evc" href="#{ref}" data-ref="{ref}">📅</a>')
        return "".join(out)

    def on_quote(m: re.Match) -> str:
        quote, group = m.group(2), m.group(4)
        cited = [r for r in re.findall(r"S\d+", group) if r in refs.sources]
        status = "no"
        for ref in cited:
            if ref not in full_cache:
                full_cache[ref] = _full_text(conn, refs.sources[ref])
            status = max(status, quote_status(quote, full_cache[ref]), key=("no", "brackets", "ok").index)
            if status == "ok":
                break
        if cited:
            checks.append({"quote": quote[:200], "ref": cited[0], "ok": status == "ok", "status": status})
        return hold(_quote_html(quote, status if cited else "unsourced") + marks_html(group))

    def on_bare(m: re.Match) -> str:
        quote = m.group(2)
        if len(re.findall(r"[^\W\d_]{2,}|\d+", quote)) < 6:   # terme, titre ou requête : pas une citation
            return m.group(0)
        checks.append({"quote": quote[:200], "ref": None, "ok": False, "status": "unsourced"})
        return hold(_quote_html(quote, "unsourced"))

    text = (text or "").replace("\x00", "").strip()
    text = _QUOTE.sub(on_quote, text)
    text = _BARE.sub(on_bare, text)
    text = _MARK.sub(lambda m: hold(marks_html(m.group(1))), text)
    html_lines: list[str] = []
    in_list = False
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            continue
        s = s.lstrip("#").strip() if s.startswith("#") else s
        body = str(escape(s[2:] if s[:2] in ("- ", "* ", "• ") else s))
        body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", body)
        body = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", body)
        if s[:2] in ("- ", "* ", "• "):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            html_lines.append(f"<li>{body}</li>")
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<p>{body}</p>")
    if in_list:
        html_lines.append("</ul>")
    html = "\n".join(html_lines)
    html = re.sub(r"\x00(\d+)\x00", lambda m: holders[int(m.group(1))], html)
    return {"html": html, "order": order, "events": events, "checks": checks}


def _source_public(src: dict[str, Any], n: int) -> dict[str, Any]:
    """Source affichée. Pas de lien vers la fiche /doc (réservée aux comptes) ; le PDF est signé à l'affichage
    (sign_links), jamais stocké : un lien partagé ne garde pas d'accès à un document devenu interne."""
    return {"n": n, **{k: src.get(k) for k in ("ref", "title", "symbol", "level", "page", "para", "date", "actor",
                                                "url", "kind", "doc_id")},
            "text": (src.get("text") or "")[:700]}


def sign_links(conn, sources: list[dict[str, Any]], hours: int = 24) -> list[dict[str, Any]]:
    """Adresse signée (24 h, même origine) du PDF de chaque source encore publique ; vérifiée à chaque affichage."""
    from . import auth

    out = []
    for src in sources or []:
        src = {k: v for k, v in src.items() if k not in ("pdf", "page_url")}
        if src.get("doc_id"):
            row = conn.execute("SELECT access, raw_path FROM documents WHERE id=?", (src["doc_id"],)).fetchone()
            if not row or row["access"] != "public":
                src = {k: v for k, v in src.items() if k not in ("text", "url")} | {"withdrawn": True}
            elif (row["raw_path"] or "").lower().endswith(".pdf"):
                src["pdf"] = auth.signed_file_url(int(src["doc_id"]), hours=hours).replace(config.PUBLIC_BASE, "")
        out.append(src)
    return out


# --- boucle principale ----------------------------------------------------------------------------------------------

def _summary(args: dict[str, Any]) -> str:
    keys = ("requete", "slug", "sujet", "acteur", "dossier", "reference", "document_recent", "theme", "terme", "pays",
            "jour", "pavillon", "depuis", "auteur", "groupe")
    vals = [str(args[k]) for k in keys if args.get(k)]
    return " · ".join(vals)[:120]


def answer(question: str, lang: str, emit: Callable[[dict[str, Any]], None],
           follow: dict[str, Any] | None = None) -> dict[str, Any]:
    """Répond à une question ; `emit` reçoit les étapes au fil de l'eau. Renvoie le contenu final et le coût."""
    from openai import OpenAI

    from . import agenda

    conn = db.get()
    follow = follow or {}
    refs = Refs(follow.get("state"))
    today = datetime.now(agenda.ANTALYA).strftime("%A %d %B %Y, %H:%M")
    system = SYSTEM.format(today=today, lang_name="French" if lang == "fr" else "English")
    # API Responses : la seule qui accepte outils + raisonnement pour les modèles GPT-5.6 ; l'historique passe par
    # previous_response_id (le contenu envoyé est public).
    specs = [{"type": "function", "name": s["function"]["name"], "description": s["function"]["description"],
              "parameters": s["function"]["parameters"], "strict": False} for s in tool_specs()]
    client = OpenAI()
    trace: list[dict[str, str]] = []
    cost = 0.0
    final = ""
    pending: list[dict[str, Any]] = [{"role": "user", "content": question}]
    previous = follow.get("response_id")   # question de suivi : le modèle garde la conversation précédente
    n_calls = 0
    stop = False
    for rnd in range(MAX_ROUNDS):
        kwargs: dict[str, Any] = dict(model=MODEL, instructions=system, input=pending, tools=specs,
                                      max_output_tokens=6000)
        if previous:
            kwargs["previous_response_id"] = previous
        if stop or rnd == MAX_ROUNDS - 1:
            kwargs["tool_choice"] = "none"
        if EFFORT:
            kwargs["reasoning"] = {"effort": EFFORT}
        resp = client.responses.create(**kwargs)
        u = resp.usage
        cost += llm._record(conn, MODEL, "demo", SimpleNamespace(prompt_tokens=getattr(u, "input_tokens", 0),
                                                                  completion_tokens=getattr(u, "output_tokens", 0)))
        previous = resp.id
        calls = [o for o in resp.output if getattr(o, "type", "") == "function_call"]
        if not calls:
            final = resp.output_text or ""
            break
        # Coût borné : plafond rechecké entre deux tours, appels d'outils limités par tour et par question.
        stop = over_budget(conn)
        pending = []
        for i, c in enumerate(calls):
            if stop or i >= MAX_CALLS_PER_ROUND or n_calls >= MAX_CALLS:
                stop = stop or n_calls >= MAX_CALLS
                pending.append({"type": "function_call_output", "call_id": c.call_id,
                                "output": '{"erreur": "tool limit reached: answer now with what you have"}'})
                continue
            n_calls += 1
            try:
                args = json.loads(c.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            label = STEP_LABELS.get(c.name, (c.name, c.name))[0 if lang == "fr" else 1]
            detail = _summary(args)
            trace.append({"tool": c.name, "label": label, "detail": detail})
            emit({"type": "step", "label": label, "detail": detail})
            result = refs.tag(call_tool(c.name, args), c.name)
            content = json.dumps(result, ensure_ascii=False, default=str)
            if len(content) > MAX_TOOL_CHARS:
                content = content[:MAX_TOOL_CHARS] + ' …[truncated: ask a narrower question or read a passage]'
            pending.append({"type": "function_call_output", "call_id": c.call_id, "output": content})
    if not final.strip():
        raise RuntimeError(f"empty answer (status {getattr(resp, 'status', '?')})")
    emit({"type": "step", "label": "Vérification des citations" if lang == "fr" else "Checking quotes", "detail": ""})
    out = render(final, refs, conn)
    sources = [_source_public(refs.sources[r], i + 1) for i, r in enumerate(out["order"])]
    events = [refs.events[r] for r in out["events"]]
    return {"html": out["html"], "sources": sources, "events": events, "checks": out["checks"], "trace": trace,
            "cost": round(cost, 4), "model": MODEL, "response_id": previous, "state": refs.state()}


def save(conn, *, question: str, lang: str, session: str | None, ip: str, user: dict | None,
         payload: dict[str, Any] | None, error: str | None = None) -> str:
    ensure_schema(conn)
    aid = secrets.token_urlsafe(9)
    conn.execute("""INSERT INTO demo_answers(id,created_at,session,ip_hash,user_id,lang,question,payload,cost,error)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                 (aid, _now(), session, ip_hash(ip), user["id"] if user else None, lang, question[:MAX_QUESTION],
                  json.dumps(payload, ensure_ascii=False) if payload else None, (payload or {}).get("cost", 0), error))
    conn.commit()
    return aid


def load(conn, aid: str) -> dict[str, Any] | None:
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM demo_answers WHERE id=? AND error IS NULL", (aid,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["payload"] = json.loads(row["payload"] or "{}")
    out["payload"]["html"] = Markup(out["payload"].get("html") or "")
    return out


def follow_context(conn, aid: str, session: str) -> dict[str, Any] | None:
    """Contexte d'une réponse précédente du MÊME visiteur (jamais celle d'un autre, même avec son lien)."""
    ensure_schema(conn)
    row = conn.execute("SELECT payload FROM demo_answers WHERE id=? AND session=? AND error IS NULL",
                       (aid, session)).fetchone()
    if not row:
        return None
    p = json.loads(row["payload"] or "{}")
    return {"response_id": p.get("response_id"), "state": p.get("state")} if p.get("response_id") else None


def corpus_as_of(conn) -> str | None:
    return conn.execute("SELECT MAX(fetched_at) FROM documents").fetchone()[0]


EXAMPLES = {
    "delegations": [
        ("GGA : qu'est-ce qui a changé entre les deux dernières versions du projet de décision sur les indicateurs de l'objectif mondial d'adaptation ? Paragraphes ajoutés, supprimés, crochets.",
         "GGA: what changed between the last two versions of the draft decision on the Global Goal on Adaptation indicators? Paragraphs added, deleted, brackets."),
        ("Transition juste : dans le dernier projet de texte, quels paragraphes restent entre crochets, depuis quelle version, et quelles options sont encore ouvertes ?",
         "Just transition: in the latest draft text, which paragraphs are still bracketed, since which version, and which options remain open?"),
        ("Mesures commerciales unilatérales : quelles Parties et quels groupes ont soutenu ou rejeté une référence dans les textes de la COP31, d'après les comptes rendus et les déclarations ?",
         "Unilateral trade measures: which Parties and groups supported or opposed a reference in the COP31 texts, according to the daily reports and statements?"),
        ("Compare les positions écrites de l'AGN, de l'AOSIS et de l'UE sur le programme de travail d'atténuation (MWP) : points d'accord et lignes de fracture.",
         "Compare the written positions of the AGN, AOSIS and the EU on the Mitigation Work Programme (MWP): common ground and dividing lines."),
    ],
    "presse": [
        ("Le projet de décision mentionne-t-il la sortie des énergies fossiles ? Cite le passage exact.",
         "Does the draft decision mention transitioning away from fossil fuels? Quote the exact passage."),
        ("Vérifie : « l'Accord de Paris oblige les pays à atteindre leurs NDC ».",
         "Fact-check: \"the Paris Agreement obliges countries to achieve their NDCs\"."),
        ("Qui bloque sur l'article 6 et pourquoi, d'après les comptes rendus ?",
         "Who is blocking on Article 6 and why, according to the daily reports?"),
    ],
    "militants": [
        ("Quels pays soutiennent une feuille de route pour sortir des fossiles ? Avec sources.",
         "Which countries support a roadmap away from fossil fuels? With sources."),
        ("Quels événements demain sur les peuples autochtones ?",
         "Which events tomorrow on Indigenous Peoples?"),
        ("Que dit le texte sur les droits humains et l'espace civique ? Est-ce entre crochets ?",
         "What does the text say on human rights and civic space? Is it bracketed?"),
    ],
}
