"""Un écrivain par donnée (note docs/notes/2026-11-27-phase1-un-ecrivain.md), lots A (alertes) et D (registre)."""

import ast
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_parcours_question import admin_chat, env  # noqa: F401  (fixtures)

SRC = Path(__file__).resolve().parents[1] / "src" / "cop_corpus"
SITE = ("web.py", "web_live.py", "mcp_server.py", "server.py")


def _fmt(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


PKG = "cop_corpus"


def _aliases(tree):
    """Noms locaux venus d'un import du paquet → nom qualifié : `from . import alerts as al` (al → alerts),
    `import cop_corpus.alerts as al` (al → alerts), `from .alerts import run as r` (r → alerts.run)."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if not (node.level or mod == PKG or mod.startswith(PKG + ".")):
                continue
            base = mod.split(".")[-1] if mod and mod != PKG else ""
            for a in node.names:
                out[a.asname or a.name] = f"{base}.{a.name}" if base else a.name
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith(PKG + "."):
                    out[a.asname or a.name] = a.name.split(".")[-1]
    return out


def _calls(tree, mod=None):
    """(nom qualifié « module.fonction » ou « fonction », nœud, fonction englobante) de chaque appel, alias suivis.
    Dans le module `mod`, un appel direct à une fonction qu'il définit est qualifié « mod.fonction »."""
    aliases = _aliases(tree)
    own = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    out = []

    def name_of(f):
        if isinstance(f, ast.Attribute):
            if isinstance(f.value, ast.Name):
                return f"{aliases.get(f.value.id, f.value.id)}.{f.attr}"
            if isinstance(f.value, ast.Attribute) and isinstance(f.value.value, ast.Name) \
                    and f.value.value.id == PKG:   # cop_corpus.alerts.run(…)
                return f"{f.value.attr}.{f.attr}"
            return f".{f.attr}"
        if isinstance(f, ast.Name):
            if f.id in aliases:
                return aliases[f.id]
            return f"{mod}.{f.id}" if mod and f.id in own else f.id
        return ""

    def walk(node, fn):
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            if isinstance(child, ast.Call):
                out.append((name_of(child.func), child, fn))
            walk(child, inner)

    walk(tree, None)
    return out


def _modules(src=None):
    for path in sorted((src or SRC).glob("*.py")):
        yield path.stem, ast.parse(path.read_text(encoding="utf-8"))


def _parse(**files):
    """Modules factices pour les cas négatifs : {nom: source}."""
    return [(name, ast.parse(code)) for name, code in files.items()]


# --- Lot A, critère 1 : seul le fil `alertes` du worker appelle alerts.run / send_pending ---------------------------

ALERTS = {"alerts.run", "alerts.send_pending"}


def _alert_callers(mods):
    bad = []
    for mod, tree in mods:
        for name, node, fn in _calls(tree, mod):
            if name in ALERTS and not ((mod == "worker" and fn == "alerts_forever") or (mod == "alerts" and fn == "run")):
                bad.append(f"{mod}.py:{node.lineno} {fn}() → {name}")
    return bad


def test_alertes_appelees_seulement_par_le_fil_alertes():
    assert not _alert_callers(_modules())


def test_alertes_alias_detectes():
    """Cas négatifs : chaque forme d'import est suivie."""
    cases = {
        "a": "from . import alerts as al\ndef f(c):\n    al.run(c)\n",
        "b": "import cop_corpus.alerts as al2\ndef f(c):\n    al2.send_pending(c)\n",
        "c": "from .alerts import run as r\ndef f(c):\n    r(c)\n",
        "d": "from cop_corpus import alerts\ndef f(c):\n    alerts.run(c)\n",
        "e": "import cop_corpus\ndef f(c):\n    cop_corpus.alerts.run(c)\n",
        "worker": "from . import alerts\ndef watch_once(c):\n    alerts.run(c)\n",   # bon module, mauvais fil
    }
    for name, code in cases.items():
        assert _alert_callers(_parse(**{name: code})), name
    assert not _alert_callers(_parse(worker="from . import alerts\ndef alerts_forever(c):\n    alerts.run(c)\n"))


class _Stop(BaseException):
    pass


def test_worker_demarre_le_fil_alertes_et_l_attend(env, monkeypatch):  # noqa: F811
    from cop_corpus import watchdog, worker

    started, expected, garde, checks = [], {}, [], []

    class FakeThread:
        def __init__(self, target, args=(), daemon=None, name=None):
            self.name = name
            self.target = target
            self.args = args

        def start(self):
            started.append((self.name, self.target))
            if self.name == "garde":
                garde.append(self.args[0])

    monkeypatch.setattr(worker.threading, "Thread", FakeThread)
    monkeypatch.setattr(worker, "_install_stop_handler", lambda: None)
    monkeypatch.setattr(watchdog, "process_started", lambda *a: None)
    monkeypatch.setattr(watchdog, "expect", lambda proc, loops, conn=None: expected.update(loops))
    monkeypatch.setattr(watchdog, "check", lambda *a, **k: checks.append(a) or [])
    monkeypatch.setattr(worker, "digest", lambda conn: None)
    monkeypatch.setattr(worker.monitor, "check_answers", lambda conn: None)
    monkeypatch.setattr(worker, "time", SimpleNamespace(time=time.time, sleep=lambda s: (_ for _ in ()).throw(_Stop())))
    called = []
    monkeypatch.setattr(worker.alerts, "run", lambda *a, **k: called.append(1) or 0)
    with pytest.raises(_Stop):
        worker.run_forever(bot=False, watch=False)
    assert ("alertes", worker.alerts_forever) in started
    assert expected.get("alertes") and expected["alertes"] >= 1
    # Phase 2, critère 9 : fil `frises`, attendu à 90 min.
    assert ("frises", worker.frises_forever) in started and expected.get("frises") == 90
    assert called == []   # la boucle principale n'appelle plus les alertes
    # Lot B, critère 5 : chien de garde dans son fil `garde`, plus dans la boucle de veille.
    assert ("garde", watchdog.run_forever) in started and garde == ["worker"]
    assert checks == []


def test_fil_alertes_un_tour(env, monkeypatch):  # noqa: F811
    """Un tour : curseur initialisé (sans alerte sur l'existant), battement écrit ; le tour suivant crée les
    notifications (lues par « Mes sujets » et `nouveautes`) et les envoie ; un réveil le fait passer sans attendre."""
    from cop_corpus import alerts, auth, config, db, ingest, watchdog, worker

    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "test")
    sent = []
    monkeypatch.setattr(alerts.telegram, "send", lambda chat, text, **k: sent.append(chat) or True)
    watchdog._last_beat.clear()
    uid, _ = auth.create_user(env, "a@b.c", "A")
    env.execute("UPDATE users SET telegram_chat_id='9' WHERE id=?", (uid,))
    env.commit()
    alerts.add_subscription(env, uid, "fossiles", "fossil fuels")
    ingest.ingest_bytes(env, b"Old text on fossil fuels. " * 20, url="https://x/a.txt", filename="a.txt")

    stop, wake = threading.Event(), threading.Event()
    th = threading.Thread(target=worker.alerts_forever, args=(stop, 3600, wake), daemon=True)
    th.start()
    deadline = time.monotonic() + 5
    while db.meta_get(env, "hb:alertes") is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert db.meta_get(env, "alerts_last_doc_id") is not None and sent == []
    ingest.ingest_bytes(env, b"New text on fossil fuels. " * 20, url="https://x/b.txt", filename="b.txt")
    wake.set()
    deadline = time.monotonic() + 5
    while not sent and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    wake.set()
    th.join(5)
    assert sent == ["9"]
    assert env.execute("SELECT COUNT(*) FROM notifications WHERE telegram_sent_at IS NOT NULL").fetchone()[0] == 1
    assert "alerts_lag" not in {i["key"] for i in watchdog.issues(env, "worker", now=datetime.now(timezone.utc)
                                                               + timedelta(hours=3))}


# --- Lot A, critères 2 à 4 : réservation atomique, pas de transaction pendant l'envoi, plafond d'âge ----------------

@pytest.fixture()
def notifs(env, monkeypatch):  # noqa: F811
    """Un utilisateur relié à Telegram, `n` notifications récentes ; renvoie une fabrique."""
    from cop_corpus import config

    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "test")
    uid = env.execute("INSERT INTO users(email, name, active, telegram_chat_id) VALUES('u@x.fr','U',1,'43')").lastrowid
    sub = env.execute("INSERT INTO subscriptions(user_id, label, query, active, notify_telegram) VALUES(?,?,?,1,1)",
                      (uid, "finance", "finance")).lastrowid
    env.commit()

    count = iter(range(10**6))

    def make(n, created):
        ids = []
        for _ in range(n):
            i = next(count)
            doc = env.execute("INSERT INTO documents(sha256, raw_path, status, title, access, fetched_at) "
                              "VALUES(?,'r','indexed',?,'public',?)", (f"s{i}", f"Doc-{i}", created)).lastrowid
            ids.append(env.execute("INSERT INTO notifications(user_id, subscription_id, doc_id, created_at) "
                                   "VALUES(?,?,?,?)", (uid, sub, doc, created)).lastrowid)
        env.commit()
        return ids

    return make


NOW = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)


def test_deux_envois_concurrents_un_seul_message(env, notifs, monkeypatch):  # noqa: F811
    from cop_corpus import alerts, db

    notifs(5, _fmt(NOW - timedelta(hours=1)))
    sent, lock = [], threading.Lock()

    def slow(chat, text, **k):
        time.sleep(0.05)
        with lock:
            sent.append(re.search(r"Doc-\d+|autres|more alerts", text).group(0))
        return True

    monkeypatch.setattr(alerts.telegram, "send", slow)
    conns = [db.connect(), db.connect()]
    ths = [threading.Thread(target=alerts.send_pending, args=(c,), kwargs={"now": NOW}) for c in conns]
    for t in ths:
        t.start()
    for t in ths:
        t.join(10)
    assert len(sent) == 5 and len(set(sent)) == 5
    assert env.execute("SELECT COUNT(*) FROM notifications WHERE telegram_sent_at IS NULL").fetchone()[0] == 0

    # Récapitulatif « … et N autres » : envoyé une seule fois lui aussi.
    sent.clear()
    notifs(5, _fmt(NOW - timedelta(minutes=30)))
    ths = [threading.Thread(target=alerts.send_pending, args=(c,), kwargs={"now": NOW, "max_per_user": 2})
           for c in conns]
    for t in ths:
        t.start()
    for t in ths:
        t.join(10)
    assert sum(s in ("autres", "more alerts") for s in sent) == 1 and len(sent) == 3
    assert env.execute("SELECT COUNT(*) FROM notifications WHERE telegram_sent_at IS NULL").fetchone()[0] == 0


def test_aucune_transaction_pendant_un_envoi(env, notifs, monkeypatch):  # noqa: F811
    from cop_corpus import alerts, db

    notifs(3, _fmt(NOW - timedelta(hours=1)))
    conn, other = db.connect(), db.connect()
    other.execute("PRAGMA busy_timeout=200")   # un verrou tenu ferait échouer l'écriture ci-dessous
    seen = []

    def check(chat, text, **k):
        seen.append(conn.in_transaction)
        db.meta_set(other, "test_ecriture", text[:20])   # autre connexion : écrit pendant l'envoi
        return True

    monkeypatch.setattr(alerts.telegram, "send", check)
    assert alerts.send_pending(conn, now=NOW, max_per_user=2) == 2
    assert seen == [False, False, False]   # 2 alertes + le récapitulatif


def test_echec_libere_puis_retente_jusqu_au_plafond(env, notifs, monkeypatch):  # noqa: F811
    from cop_corpus import alerts

    [nid] = notifs(1, _fmt(NOW - timedelta(hours=47)))
    calls = []
    monkeypatch.setattr(alerts.telegram, "send", lambda chat, text, **k: calls.append(1) and False)
    assert alerts.send_pending(env, now=NOW) == 0
    assert env.execute("SELECT telegram_sent_at FROM notifications WHERE id=?", (nid,)).fetchone()[0] is None
    monkeypatch.setattr(alerts.telegram, "send", lambda chat, text, **k: calls.append(2) or True)
    assert alerts.send_pending(env, now=NOW) == 1                       # retentée au tour suivant
    assert calls == [1, 2]
    # Au-delà du plafond de 48 h : plus retentée (reste en base, non marquée).
    [old] = notifs(1, _fmt(NOW - timedelta(hours=47)))
    monkeypatch.setattr(alerts.telegram, "send", lambda chat, text, **k: calls.append(3) and False)
    assert alerts.send_pending(env, now=NOW) == 0
    assert alerts.send_pending(env, now=NOW + timedelta(hours=2)) == 0
    assert calls == [1, 2, 3]
    assert env.execute("SELECT telegram_sent_at FROM notifications WHERE id=?", (old,)).fetchone()[0] is None

    # Exception pendant l'envoi : réservation libérée aussi.
    def boom(chat, text, **k):
        raise RuntimeError("réseau")

    monkeypatch.setattr(alerts.telegram, "send", boom)
    with pytest.raises(RuntimeError):
        alerts.send_pending(env, now=NOW)
    assert env.execute("SELECT telegram_sent_at FROM notifications WHERE id=?", (old,)).fetchone()[0] is None


# --- Lot D, critère 11 : registre des clés `meta` et fonctions réservées au worker -----------------------------------

_SQL = re.compile(r"\b(INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+meta\b", re.I)


def _consts(tree):
    """Constantes de module (NOM = "chaîne")."""
    return {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign)
            and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
            for t in n.targets if isinstance(t, ast.Name)}


def _key(node, consts):
    """(clé, est_un_préfixe) ou None si non résolue."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, False
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id], False
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        return node.values[0].value, True
    return None


def _strings(node, consts, assigned):
    """Morceaux de texte littéral d'une expression SQL (constante, f-string, concaténation, variable affectée)."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
        elif isinstance(n, ast.Name):
            out += ([consts[n.id]] if n.id in consts else []) + assigned.get(n.id, [])
    return out


def _writes(mods=None):
    """[(module, ligne, clé résolue ou None)] pour chaque écriture dans `meta`. Clé None (le test échoue) : clé non
    résolue, ou SQL non littéral (variable, f-string, concaténation) qui touche `meta`."""
    out = []
    for mod, tree in (mods if mods is not None else _modules()):
        consts = _consts(tree)
        assigned = {}   # chaînes affectées à un nom, n'importe où dans le module (`sql = "…"`)
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign):
                for tg in n.targets:
                    if isinstance(tg, ast.Name):
                        assigned.setdefault(tg.id, []).extend(
                            s.value for s in ast.walk(n.value) if isinstance(s, ast.Constant) and isinstance(s.value, str))
        for name, node, fn in _calls(tree, mod):
            last = name.split(".")[-1]
            if last == "meta_set" and not (mod == "db" and fn == "meta_set"):
                out.append((mod, node.lineno, _key(node.args[1], consts) if len(node.args) > 1 else None))
            elif last in ("execute", "executemany") and node.args and not (mod == "db" and fn == "meta_set"):
                sql = node.args[0]
                if not (isinstance(sql, ast.Constant) and isinstance(sql.value, str)):
                    if any(_SQL.search(s) for s in _strings(sql, consts, assigned)) or \
                            _SQL.search(" ".join(_strings(sql, consts, assigned))):
                        out.append((mod, node.lineno, None))   # SQL non littéral sur `meta` : à écrire en littéral
                    continue
                m = _SQL.search(sql.value)
                if not m:
                    continue
                lit = re.search(r"VALUES\s*\(\s*'([^']+)'", sql.value) or re.search(r"key\s*=\s*'([^']+)'", sql.value)
                if lit:
                    out.append((mod, node.lineno, (lit.group(1), False)))
                    continue
                params = node.args[1] if len(node.args) > 1 else None
                elts = params.elts if isinstance(params, (ast.Tuple, ast.List)) else []
                pick = elts[0] if m.group(1).upper().startswith("INSERT") else (elts[-1] if elts else None)
                out.append((mod, node.lineno, _key(pick, consts) if pick is not None else None))
    return out


def _owner(key, prefix, owners):
    if not prefix and key in owners:
        return owners[key]
    for decl, mod in owners.items():
        if decl.endswith(":") and key.startswith(decl):
            return mod
    return None


def test_registre_des_cles_meta():
    from cop_corpus import db

    writes = _writes()
    assert len(writes) > 20   # le relevé voit bien les écritures (garde-fou du test lui-même)
    bad = []
    for mod, line, key in writes:
        if key is None:
            bad.append(f"{mod}.py:{line} clé non résolue (constante, f-string à préfixe fixe ou SQL littéral)")
            continue
        owner = _owner(*key, db.META_OWNERS)
        if owner is None:
            bad.append(f"{mod}.py:{line} clé {key[0]!r} absente de db.META_OWNERS")
        elif owner != mod:
            bad.append(f"{mod}.py:{line} clé {key[0]!r} appartient à {owner}.py")
    assert not bad, bad
    seen = [k[0] for _, _, k in writes if k]
    unused = [d for d in db.META_OWNERS if not any(s == d or (d.endswith(":") and s.startswith(d)) for s in seen)]
    assert not unused, f"déclarées mais jamais écrites : {unused}"


def _registry_errors(writes):
    from cop_corpus import db

    bad = []
    for mod, line, key in writes:
        owner = _owner(*key, db.META_OWNERS) if key else None
        if key is None or owner != mod:
            bad.append((mod, line))
    return bad


def test_registre_detecte_un_ecrivain_etranger():
    """Cas négatifs : chaque écriture ci-dessous doit faire échouer le registre (alias, clé étrangère, inconnue,
    non résolue, SQL non littéral)."""
    cases = {
        "web": "from . import db\ndef f(conn):\n    db.meta_set(conn, 'alerts_last_doc_id', '1')\n",
        "w2": "from . import db as d\ndef f(conn):\n    d.meta_set(conn, 'alerts_last_doc_id', '1')\n",
        "w3": "from .db import meta_set as ms\ndef f(conn):\n    ms(conn, 'cle_inconnue', '1')\n",
        "w4": "import cop_corpus.db as dd\ndef f(conn, k):\n    dd.meta_set(conn, k, '1')\n",
        "w5": "def f(conn, k):\n    conn.execute(\"INSERT INTO meta(key,value) VALUES(?,?)\", (f'zz:{k}', 1))\n",
        "w6": "def f(conn, k):\n    conn.execute(f\"UPDATE meta SET value=1 WHERE key='{k}'\")\n",
        "w7": "def f(conn):\n    sql = \"DELETE FROM meta WHERE key='hb:x'\"\n    conn.execute(sql)\n",
        "w8": "def f(conn, k):\n    conn.execute(\"DELETE FROM \" + \"meta WHERE key=?\", (k,))\n",
    }
    for name, code in cases.items():
        writes = _writes(_parse(**{name: code}))
        assert writes and _registry_errors(writes), name
    ok = _writes(_parse(alerts="from . import db as d\ndef f(conn):\n    d.meta_set(conn, 'alerts_last_doc_id', '1')\n"))
    assert ok and not _registry_errors(ok)


WORKER_ONLY = {"alerts.run", "alerts.send_pending", "ingest.index_document", "ingest.reindex_pending",
               "live.tidy_texts", "live.notify_new_texts", "embed.embed_pending", "digest.run",
               # Lot C : tout ce qui indexe (le site enregistre par ingest.register_bytes, mailin.ingest_newsletter
               # avec index=False : tests/test_indexation_worker.py).
               "ingest.ingest_bytes", "ingest.refetch_in_place", "ingest.fetch_one", "ingest.run_source",
               "ingest.run_many", "live.retrieve_pending", "live.add_note", "mailin.reread_newsletters",
               "soumissions.download_pending",
               # Phase 2 : frises des pages dossier calculées par le fil `frises` du worker seul (le site les lit).
               "frises.compute_all", "frises.compute"}


def _site_violations(mods):
    bad = []
    for name, tree in mods:
        for call, node, fn in _calls(tree, name):
            if call in WORKER_ONLY:
                bad.append(f"{name}.py:{node.lineno} {call}")
    return bad


def test_fonctions_du_worker_interdites_au_site():
    mods = [(m, t) for m, t in _modules() if f"{m}.py" in SITE]
    assert len(mods) == len(SITE)
    assert not _site_violations(mods)


# --- Phase 2, critère 2 : `dossier_frises` n'est écrite que par frises.py ------------------------------------------

_FRISES_SQL = re.compile(r"\b(INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+dossier_frises\b",
                         re.I)


def _frises_writers(mods):
    """Modules dont un texte littéral écrit dans `dossier_frises` (requête, morceau de requête)."""
    return sorted({mod for mod, tree in mods for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str) and _FRISES_SQL.search(n.value)})


def test_dossier_frises_ecrite_par_frises_seul():
    assert _frises_writers(_modules()) == ["frises"]
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    assert not _frises_writers([(p.stem, ast.parse(p.read_text())) for p in scripts.glob("*.py")])


def test_dossier_frises_ecrivain_etranger_detecte():
    cases = {
        "web_live": "def f(c):\n    c.execute(\"DELETE FROM dossier_frises WHERE slug=?\", ('x',))\n",
        "worker": "def f(c):\n    c.execute(\"INSERT OR REPLACE INTO dossier_frises VALUES(1)\")\n",
        "live": "def f(c):\n    c.execute(f\"UPDATE dossier_frises SET value='{1}'\")\n",
    }
    for name, code in cases.items():
        assert _frises_writers(_parse(**{name: code})) == [name], name
    assert not _frises_writers(_parse(web_live="def f(c):\n    c.execute('SELECT value FROM dossier_frises')\n"))


def test_frises_compute_all_reserve_au_fil_frises():
    """Seul `worker.frises_forever` appelle frises.compute_all (et seul frises.compute_all appelle frises.compute)."""
    bad = []
    for mod, tree in _modules():
        for name, node, fn in _calls(tree, mod):
            if name == "frises.compute_all" and not (mod == "worker" and fn == "frises_forever"):
                bad.append(f"{mod}.py:{node.lineno}")
            if name == "frises.compute" and not (mod == "frises" and fn == "compute_all"):
                bad.append(f"{mod}.py:{node.lineno}")
    assert not bad, bad


# --- Phase 2, critère 1 : liste fermée des fils ----------------------------------------------------------------------
# Site : seuls fils durables, le chien de garde et le battement de la boucle ; les autres sont liés à une requête
# (démo, envoi d'une alerte, pool sémantique). Toute création de fil du paquet est déclarée ici (liste fermée).

FILS_SITE = {
    ("watchdog", "start_thread"): "chien de garde (fil `watchdog`, durable)",
    ("server", "lifespan"): "battement de la boucle d'événements (tâche asyncio, durable)",
    ("web", "demo_ask"): "démo : un fil par question",
    ("monitor", "_spawn"): "envoi d'une alerte (Telegram, e-mail)",
    ("search", None): "pool sémantique (_SEM_POOL), une tâche par recherche",
}
FILS_HORS_SITE = {
    ("worker", "run_forever"): "fils du worker",
    ("server", "run"): "worker dans le même processus (cop serve --with-worker, poste de développement)",
    ("ingest", "run_many"): "collecte en parallèle (worker, CLI)",
    ("eval_ask", "run"): "évaluation (CLI)",
}
_THREAD_CALLS = {"Thread", "Timer", "ThreadPoolExecutor", "ProcessPoolExecutor", "create_task", "ensure_future",
                 "start_new_thread"}


def _thread_sites(mods):
    return {(mod, fn) for mod, tree in mods for name, _node, fn in _calls(tree, mod)
            if name.split(".")[-1] in _THREAD_CALLS}


def test_liste_fermee_des_fils():
    assert _thread_sites(_modules()) == set(FILS_SITE) | set(FILS_HORS_SITE)


def test_liste_fermee_des_fils_detecte_un_nouveau_fil():
    cases = {
        "web_live": "import threading\ndef start_warming():\n    threading.Thread(target=print).start()\n",
        "server": "import asyncio\nasync def lifespan(app):\n    asyncio.create_task(x())\ndef run():\n    pass\n"
                  "def autre():\n    asyncio.ensure_future(x())\n",
        "web": "from concurrent.futures import ThreadPoolExecutor\nPOOL = ThreadPoolExecutor(2)\n",
    }
    for name, code in cases.items():
        assert _thread_sites(_parse(**{name: code})) - set(FILS_SITE) - set(FILS_HORS_SITE), name


def test_fonctions_du_worker_alias_detectes():
    cases = {
        "web": "from . import ingest as ig\ndef f(c):\n    ig.index_document(c, 1)\n",
        "web_live": "import cop_corpus.live as lv\ndef f(c):\n    lv.tidy_texts(c)\n",
        "mcp_server": "from .alerts import send_pending as sp\ndef f(c):\n    sp(c)\n",
        "server": "from cop_corpus import digest\ndef f(c):\n    digest.run(c, None)\n",
    }
    for name, code in cases.items():
        assert _site_violations(_parse(**{name: code})), name


# --- Lot A, suite de revue : une panne Telegram ne retient pas le fil `alertes` --------------------------------------

def test_au_plus_cinq_echecs_par_tour(env, notifs, monkeypatch):  # noqa: F811
    from cop_corpus import alerts

    notifs(8, _fmt(NOW - timedelta(hours=1)))
    calls = []
    monkeypatch.setattr(alerts.telegram, "send", lambda chat, text, **k: calls.append(k) and False)
    assert alerts.send_pending(env, now=NOW) == 0
    assert len(calls) == alerts.MAX_FAILURES == 5 and all(k == {"quick": True} for k in calls)
    assert env.execute("SELECT COUNT(*) FROM notifications WHERE telegram_sent_at IS NULL").fetchone()[0] == 8


def test_envoi_court_un_seul_essai(monkeypatch):
    from cop_corpus import config, telegram

    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "test")
    posts, sleeps = [], []

    def post(url, json=None, timeout=None):
        posts.append(timeout)
        raise OSError("injoignable")

    monkeypatch.setattr(telegram._http, "post", post)
    monkeypatch.setattr(telegram.time, "sleep", lambda s: sleeps.append(s))
    assert telegram.send("1", "x", quick=True) is False
    assert len(posts) == 1 and sleeps == [] and posts[0].read <= telegram.QUICK_TIMEOUT
    posts.clear()
    assert telegram.send("1", "x") is False                    # autres appelants : inchangés (3 essais)
    assert len(posts) == 3


def test_reveil_pendant_un_tour_non_perdu(env, monkeypatch):  # noqa: F811
    from cop_corpus import worker

    stop, wake, tours = threading.Event(), threading.Event(), []

    def run(conn):
        tours.append(1)
        if len(tours) == 1:
            wake.set()          # réveil arrivé pendant le tour : le tour suivant part sans attendre l'heure
        return 0

    monkeypatch.setattr(worker.alerts, "run", run)
    th = threading.Thread(target=worker.alerts_forever, args=(stop, 3600, wake), daemon=True)
    th.start()
    deadline = time.monotonic() + 5
    while len(tours) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    assert len(tours) == 2      # puis il attend (réveil consommé)
    stop.set()
    wake.set()
    th.join(5)
    assert not th.is_alive()


# --- Lot B : chien de garde, un état par processus -------------------------------------------------------------------

@pytest.fixture()
def wd(env, admin_chat, monkeypatch):  # noqa: F811
    import importlib

    from cop_corpus import config, monitor, watchdog

    importlib.reload(watchdog)
    monkeypatch.setattr(monitor, "_spawn", lambda fn, *a: fn(*a))
    monkeypatch.setattr(config, "DIGEST_HOURS", [])
    monkeypatch.setattr(watchdog, "_cgroup_memory", lambda: None)
    return watchdog


def _meta(conn):
    return {k: v for k, v in conn.execute("SELECT key, value FROM meta")}


def _troubles(env, wd, now):
    """Problèmes que seul le worker doit voir : boucles arrêtées (dont celles du site), document bloqué, alertes en
    retard, source morte."""
    from cop_corpus import db

    old = _fmt(now - timedelta(hours=3))
    wd.expect("worker", {"veille": 90}, env)
    wd.expect("app", {"app": 15, "app-boucle": 10}, env)
    for k in ("hb:veille", "hb:app", "hb:app-boucle"):
        db.meta_set(env, k, old)
    db.meta_set(env, "alerts_last_doc_id", "0")
    env.execute("INSERT INTO documents(sha256, raw_path, status, fetched_at) VALUES('p','r','pending',?)", (old,))
    env.execute("INSERT INTO documents(sha256, raw_path, status, fetched_at) VALUES('i','r','indexed',?)", (old,))
    env.execute("INSERT INTO source_state(source_id,last_run,last_result,failing_since) VALUES(?,?,?,?)",
                ("morte", _fmt(now - timedelta(hours=1)), "{'error': 1}", _fmt(now - timedelta(hours=30))))
    env.commit()
    db.meta_set(env, "watchdog_pending", json.dumps({"1": old}))


def test_cles_disjointes_site_et_worker(env, admin_chat, wd, monkeypatch):  # noqa: F811
    """Critère 5 : le site n'écrit que watchdog_state:app et watchdog_check:app et ne connaît que `worker` et
    `mem:app` ; le worker n'écrit que watchdog_state:worker, watchdog_pending, watchdog_check:worker et ne lève
    jamais `worker`."""
    from cop_corpus import db

    now = wd._now()
    _troubles(env, wd, now)
    db.meta_set(env, "watchdog_check:worker", _fmt(now - timedelta(minutes=16)))
    monkeypatch.setattr(wd, "_cgroup_memory", lambda: (95, 100))
    before = _meta(env)
    site = {i["key"] for i in wd.check(env, "app", now=now)}
    after = _meta(env)
    assert site == {"worker", "mem:app"}
    assert {k for k in after if after.get(k) != before.get(k)} == {"watchdog_state:app", "watchdog_check:app"}
    before = after
    worker = {i["key"] for i in wd.check(env, "worker", now=now)}
    after = _meta(env)
    assert {"hb:veille", "hb:app", "hb:app-boucle", "pending", "alerts_lag", "sources", "mem:worker"} <= worker
    assert "worker" not in worker and "mem:app" not in worker
    assert {k for k in after if after.get(k) != before.get(k)} <= {"watchdog_state:worker", "watchdog_pending",
                                                                     "watchdog_check:worker"}
    assert set(json.loads(after["watchdog_state:app"])) == {"worker", "mem:app"}
    assert "worker" not in json.loads(after["watchdog_state:worker"])


def test_fil_garde_du_worker(env, wd, monkeypatch):  # noqa: F811
    """Le fil `garde` vérifie sans redéclarer démarrage ni battements (faits par worker.run_forever)."""
    stop, calls = threading.Event(), []
    monkeypatch.setattr(wd, "process_started", lambda *a: calls.append("started"))
    monkeypatch.setattr(wd, "expect", lambda *a, **k: calls.append("expect"))
    monkeypatch.setattr(wd, "check", lambda conn, proc: calls.append(proc) or stop.set())
    wd.run_forever("worker", stop, every=0)
    assert calls == ["worker"]


def test_site_alerte_worker_puis_retabli(env, admin_chat, wd, monkeypatch):  # noqa: F811
    """Critère 6 : `watchdog_check:worker` vieux de 16 min → alerte `worker` par le site, puis « Rétabli » par le
    site ; le site n'envoie jamais `pending` ni `alerts_lag`, le worker jamais `worker`."""
    from cop_corpus import config, db

    now = wd._now()
    _troubles(env, wd, now)
    db.meta_set(env, "watchdog_check:worker", _fmt(now - timedelta(minutes=16)))
    assert [i["key"] for i in wd.check(env, "app", now=now)] == ["worker"]
    assert len(admin_chat) == 1 and "le worker ne vérifie plus" in admin_chat[0][1]
    db.meta_set(env, "watchdog_check:worker", _fmt(now - timedelta(minutes=14)))
    assert wd.check(env, "app", now=now) == []
    assert len(admin_chat) == 2 and "Rétabli" in admin_chat[1][1] and "worker" in admin_chat[1][1]
    site = [t for _, t in admin_chat]
    assert not any(w in t for t in site for w in ("attente d'indexation", "alertes en retard", "veille"))
    admin_chat.clear()
    wd.check(env, "worker", now=now)
    assert admin_chat and not any("le worker ne vérifie plus" in t for _, t in admin_chat)
    # Hors session : `worker` est un blocage, il part quand même.
    admin_chat.clear()
    monkeypatch.setattr(config, "SESSION", False)
    env.execute("DELETE FROM error_events")
    env.commit()
    assert [i["key"] for i in wd.check(env, "app", now=now + timedelta(minutes=20))] == ["worker"]
    assert len(admin_chat) == 1 and "le worker ne vérifie plus" in admin_chat[0][1]


def test_etat_ecrit_avant_toute_annonce(env, admin_chat, wd, monkeypatch):  # noqa: F811
    """Critère 7, #29 : si l'état ne peut pas être écrit, ni blocage ni « Dégradé » ne partent ; au retour de la
    base, un seul message par problème."""
    import sqlite3

    from cop_corpus import db

    now = wd._now()
    wd.expect("worker", {"veille": 90}, env)
    db.meta_set(env, "hb:veille", _fmt(now - timedelta(hours=3)))
    env.execute("INSERT INTO source_state(source_id,last_run,last_result,failing_since) VALUES(?,?,?,?)",
                ("morte", _fmt(now - timedelta(hours=1)), "{'error': 1}", _fmt(now - timedelta(hours=30))))
    env.commit()
    real = db.meta_set

    def failing(conn, key, value):
        if key == "watchdog_state:worker":
            raise sqlite3.OperationalError("database is locked")
        return real(conn, key, value)

    monkeypatch.setattr(wd.db, "meta_set", failing)
    wd.check(env, "worker", now=now)
    annonces = lambda: [t for _, t in admin_chat if "veille" in t or "Dégradé" in t]  # noqa: E731
    assert annonces() == []
    monkeypatch.setattr(wd.db, "meta_set", real)
    wd.check(env, "worker", now=now)
    wd.check(env, "worker", now=now)
    assert len(annonces()) == 2                                       # un blocage, un « Dégradé »
    assert sum("Dégradé" in t for t in annonces()) == 1


def test_repli_pas_renvoye_au_retour_de_la_base(env, admin_chat, wd):  # noqa: F811
    """Critère 7, #28 : une alerte partie par le repli (base indisponible) n'est pas renvoyée quand la base revient ;
    `_mem_sent` est purgé au-delà de RESEND_MINUTES."""
    import sqlite3

    from cop_corpus import monitor

    class Locked:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

    monitor.admin_chats(env)                                          # destinataires connus du repli
    monitor._ensure(env)
    place, err = "chien de garde : hb:veille", "Blocage: boucle arrêtée"
    assert monitor.report(place, err, conn=Locked(), background=False, urgent=True)
    assert len(admin_chat) == 1 and "base indisponible" in admin_chat[0][1]
    assert monitor.report(place, err, conn=env, background=False, urgent=True) is False
    assert len(admin_chat) == 1                                       # pas de second message
    row = env.execute("SELECT sent_at, unsent FROM error_events").fetchone()
    assert row["sent_at"] and row["unsent"] == 0
    # Purge : une entrée plus vieille que RESEND_MINUTES disparaît ; l'alerte repart normalement.
    key = next(iter(monitor._mem_sent))
    monitor._mem_sent[key] -= timedelta(minutes=monitor.RESEND_MINUTES + 1)
    monitor._mem_sent["autre"] = monitor._mem_sent[key]
    env.execute("UPDATE error_events SET sent_at=?", (_fmt(wd._now() - timedelta(hours=1)),))
    env.commit()
    assert monitor.report(place, err, conn=env, background=False, urgent=True)
    assert monitor._mem_sent == {} and len(admin_chat) == 2


def test_ancien_etat_supprime_par_le_worker(env, wd):  # noqa: F811
    """Critère 8."""
    from cop_corpus import db

    db.meta_set(env, "watchdog_state", json.dumps({"hb:veille": {"since": "2026-11-26T00:00:00Z"}}))
    wd.check(env, "app")
    assert db.meta_get(env, "watchdog_state") is not None             # le site n'y touche pas
    wd.check(env, "worker")
    assert db.meta_get(env, "watchdog_state") is None


def test_site_ignore_worker_juste_apres_son_demarrage(env, admin_chat, wd, monkeypatch):  # noqa: F811
    """Retouche de revue : au démarrage du site, `watchdog_check:worker` date encore de l'ancien worker ; pas
    d'alerte `worker` pendant APP_GRACE_MINUTES (10 min), le temps que le fil `garde` la réécrive."""
    from cop_corpus import db

    now = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)
    db.meta_set(env, "watchdog_check:worker", _fmt(now - timedelta(minutes=40)))
    monkeypatch.setattr(wd, "_app_started", now - timedelta(minutes=9))
    assert wd.check(env, "app", now=now) == [] and admin_chat == []
    monkeypatch.setattr(wd, "_app_started", now - timedelta(minutes=11))
    assert [i["key"] for i in wd.check(env, "app", now=now)] == ["worker"]
    assert len(admin_chat) == 1


def test_fil_du_site_note_son_demarrage(env, wd, monkeypatch):  # noqa: F811
    stop = threading.Event()
    monkeypatch.setattr(wd, "process_started", lambda *a: None)
    monkeypatch.setattr(wd, "check", lambda conn, proc: stop.set())
    assert wd._app_started is None
    wd.run_forever("app", stop, every=0)
    assert wd._app_started is not None
