"""Chien de garde : aucune panne bloquante ne doit passer inaperçue.

Le 19/11/2026, un PDF a fait planter le worker (segfault de PyMuPDF) ; Docker l'a relancé 50 fois en trois jours,
les alertes et les récapitulatifs se sont arrêtés, et rien ne l'a signalé : un plantage natif ne passe pas par
`monitor.report()`, et un processus mort ne peut pas dire qu'il est mort. D'où trois mécanismes complémentaires.

1. Battements de cœur (`beat`) : chaque boucle longue (veille, textes en direct, soumissions, courrier, bot Telegram,
   site) écrit l'heure de son dernier tour dans `meta`. Chaque processus déclare au démarrage les boucles qu'il fait
   tourner et leur délai maximal (`expect`).
2. Arrêts anormaux (`process_started` / `process_stopping`, worker et site) : chaque processus note qu'il tourne ;
   seul un arrêt demandé (SIGTERM, arrêt normal d'uvicorn) le marque propre. Redémarré sans arrêt propre : il a
   planté (alerte, avec les documents en attente, suspects) ; trois arrêts anormaux en deux heures : « en boucle ».
3. Invariants vérifiés (`check`) par le fil `garde` du worker : battements en retard (y compris ceux du site),
   documents bloqués en 'pending' (ils arrêtent les alertes), curseur des alertes en retard, récapitulatif non
   envoyé, sources en échec continu, mémoire du conteneur ou disque presque pleins. Le site, lui, ne vérifie que
   deux choses : le worker vérifie-t-il encore (`worker`), et sa propre mémoire (`mem:app`). Chaque processus a son
   état (`watchdog_state:<processus>`) : clés disjointes, un seul écrivain par clé (note 2026-11-27-phase1).

Problème bloquant → `monitor.report` (Telegram, à défaut e-mail ; rappel toutes les 30 min tant qu'il dure).
Problème dégradé (sources en échec, ressources) → au plus un message par jour et par problème.
Problème résolu → message « rétabli ». `status()` sert aussi /healthz/deep (sonde externe : 503 si bloquant)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config, db

CHECK_EVERY = 300                 # secondes entre deux vérifications (site et worker)
PENDING_MINUTES = 30              # document 'pending' depuis plus longtemps : indexation bloquée
ALERT_LAG_MINUTES = 120           # document indexé depuis plus longtemps sans passage des alertes (≥ délai « veille »)
DIGEST_GRACE_MINUTES = 60         # récapitulatif de 7 h / 19 h toujours pas parti une heure après
SOURCE_FAIL_HOURS = 24            # source en échec sans aucun succès depuis
RESTART_WINDOW_HOURS, RESTART_MAX = 2, 3
MEM_RATIO, DISK_MIN_RATIO = 0.92, 0.08
WAL_STUCK_BYTES = 128 * 2**20   # pages du WAL en attente de report dans la base (lecteur coincé)
DEGRADED_REPEAT_HOURS = 24
WORKER_LATE_FACTOR = 3            # site : `watchdog_check:worker` plus vieux que 3 vérifications → worker en panne
APP_GRACE_MINUTES = 10            # site : clé `worker` ignorée juste après son démarrage (valeur d'avant le déploiement)
_app_started: datetime | None = None   # démarrage du fil de surveillance du site (None : tests, scripts)

_last_beat: dict[str, float] = {}
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _jget(conn, key: str, default):
    try:
        return json.loads(db.meta_get(conn, key) or "null") or default
    except ValueError:
        return default


# --- battements de cœur -----------------------------------------------------------------------------------------------

def beat(name: str, conn=None, every: int = 60) -> None:
    """Un tour de la boucle `name` vient de se terminer. Écrit au plus une fois par `every` secondes. Ne lève jamais."""
    t = time.monotonic()
    with _lock:
        if t - _last_beat.get(name, -every - 1) < every:
            return
        _last_beat[name] = t
    try:
        db.meta_set(conn or db.get(), f"hb:{name}", _fmt(_now()))
    except Exception as exc:  # noqa: BLE001  (base verrouillée…) : réessayé au tour suivant
        with _lock:
            _last_beat.pop(name, None)
        print(f"[watchdog] battement {name} non écrit : {exc}", file=sys.stderr, flush=True)


def expect(process: str, loops: dict[str, int], conn=None) -> None:
    """Le processus `process` (worker, app) fait tourner ces boucles ; délai maximal entre deux tours, en minutes.
    Remplace la déclaration précédente du même processus (une boucle retirée n'est plus attendue)."""
    conn = conn or db.get()
    db.meta_set(conn, f"hb_expect:{process}", json.dumps(loops))
    for name in loops:   # délai compté depuis le démarrage, pas depuis un battement d'une vie antérieure
        db.meta_set(conn, f"hb:{name}", _fmt(_now()))


# --- arrêts anormaux (worker et site) --------------------------------------------------------------------------------
# « Propre » n'est noté QUE sur un arrêt demandé (SIGTERM du worker, arrêt normal d'uvicorn pour le site) : une
# exception non rattrapée, un segfault ou un SIGKILL (mémoire) laissent clean=False, donc une alerte au démarrage suivant.

def _run_file(process: str) -> Path:
    return config.DATA_DIR / "run" / f"{process}.json"


def _read_run(process: str) -> dict[str, Any]:
    try:
        return json.loads(_run_file(process).read_text())
    except (OSError, ValueError):
        return {}


def _write_run(process: str, state: dict[str, Any]) -> None:
    """Fichier (écriture atomique), pas la base : le gestionnaire de SIGTERM ne doit jamais attendre un verrou."""
    f = _run_file(process)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(f)


def process_started(conn, process: str) -> bool:
    """Au démarrage : signale un arrêt anormal précédent et les arrêts anormaux en boucle. Vrai si anormal."""
    from . import monitor

    prev = _read_run(process)
    now = _now()
    crashes = [s for s in prev.get("crashes", []) if (_parse(s) or now) > now - timedelta(hours=24)]
    abnormal = bool(prev.get("started")) and not prev.get("clean")
    if abnormal:
        crashes.append(_fmt(now))
    _write_run(process, {"started": _fmt(now), "clean": False, "crashes": crashes[-50:]})
    if not abnormal:
        return False
    pending = [r[0] for r in conn.execute("SELECT id FROM documents WHERE status='pending' ORDER BY id LIMIT 10")]
    suspects = f" Documents en attente d'indexation (suspects, repris à part) : {', '.join(map(str, pending))}." \
        if pending else ""
    monitor.report(f"{process} : arrêt anormal",
                   f"Crash: {process} arrêté sans arrêt demandé (plantage natif, exception, mémoire ?) ; démarré le "
                   f"{prev['started']}, relancé par Docker.{suspects}", conn=conn, background=False)
    recent = [s for s in crashes if (_parse(s) or now) > now - timedelta(hours=RESTART_WINDOW_HOURS)]
    if len(recent) >= RESTART_MAX:
        monitor.report(f"{process} : redémarrages en boucle",
                       f"CrashLoop: {len(recent)} arrêts anormaux en {RESTART_WINDOW_HOURS} h. "
                       "Voir sudo journalctl -u docker | grep exitCode", conn=conn, background=False,
                       urgent=True)   # part même hors session (un arrêt isolé, lui, est seulement enregistré)
    return True


def process_stopping(process: str, conn=None) -> None:
    """Arrêt demandé : ce n'est pas un plantage. À appeler seulement sur un arrêt volontaire. Ne lève jamais."""
    try:
        state = _read_run(process)
        state["clean"] = True
        _write_run(process, state)
    except Exception as exc:  # noqa: BLE001
        print(f"[watchdog] arrêt propre de {process} non noté : {exc}", file=sys.stderr, flush=True)


# --- invariants -------------------------------------------------------------------------------------------------------

def _cgroup_memory() -> tuple[int, int] | None:
    """(utilisée hors cache récupérable, limite) du conteneur ; cgroup v2, sinon v1 ; None sans limite connue."""
    for cur_f, max_f, stat_f, inactive in (
            ("memory.current", "memory.max", "memory.stat", "inactive_file"),
            ("memory/memory.usage_in_bytes", "memory/memory.limit_in_bytes", "memory/memory.stat", "total_inactive_file")):
        base = Path("/sys/fs/cgroup")
        try:
            cur = int((base / cur_f).read_text())
            mx = (base / max_f).read_text().strip()
        except (OSError, ValueError):
            continue
        if mx == "max" or int(mx) >= 1 << 60:
            return None
        try:
            stat = dict(line.split() for line in (base / stat_f).read_text().splitlines())
            cur -= int(stat.get(inactive, 0))   # cache de fichiers récupérable : pas une pression mémoire
        except (OSError, ValueError):
            pass
        return cur, int(mx)
    return None


def _failed_result(text: str | None) -> bool:
    """Dernier passage d'une source fait uniquement d'échecs (même règle que ingest.run_source)."""
    import ast

    from .ingest import FAIL_KEYS

    try:
        counts = ast.literal_eval(text or "{}")
    except (ValueError, SyntaxError):
        return False
    return isinstance(counts, dict) and bool(counts) and all(k in FAIL_KEYS for k in counts)


def _antibot_only(text: str | None) -> bool:
    import ast

    try:
        counts = ast.literal_eval(text or "{}")
    except (ValueError, SyntaxError):
        return False
    return isinstance(counts, dict) and set(counts) == {"antibot"}


def issues(conn, process: str | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Problèmes constatés maintenant : [{key, level (bloquant|degrade), message}]. N'écrit rien, hormis le
    checkpoint passif du WAL (report de pages déjà validées dans la base)."""
    now = now or _now()
    out: list[dict[str, Any]] = []

    def add(key: str, level: str, message: str) -> None:
        out.append({"key": key, "level": level, "message": message})

    # 1. battements en retard
    for (k,) in conn.execute("SELECT key FROM meta WHERE key LIKE 'hb_expect:%'").fetchall():
        for name, minutes in _jget(conn, k, {}).items():
            last = _parse(db.meta_get(conn, f"hb:{name}"))
            if last is None or now - last > timedelta(minutes=int(minutes)):
                age = f"{int((now - last).total_seconds() // 60)} min" if last else "jamais"
                add(f"hb:{name}", "bloquant",
                    f"boucle « {name} » ({k.split(':', 1)[1]}) arrêtée : dernier tour il y a {age} "
                    f"(maximum {minutes} min)")

    # 2. documents bloqués en attente d'indexation (ils arrêtent aussi le curseur des alertes). Durée mesurée par le
    #    chien de garde lui-même (un document peut repasser en 'pending' avec son ancienne date de collecte).
    seen = _jget(conn, "watchdog_pending", {})
    limit = now - timedelta(minutes=PENDING_MINUTES)
    stuck = sorted(int(i) for (i,) in conn.execute("SELECT id FROM documents WHERE status='pending'").fetchall()
                   if (_parse(seen.get(str(i))) or now) <= limit)
    if stuck:
        add("pending", "bloquant", f"documents en attente d'indexation depuis plus de {PENDING_MINUTES} min "
                                   f"(alertes bloquées derrière eux) : {', '.join(map(str, stuck[:10]))}")

    # 3. alertes en retard sur le corpus
    cursor = db.meta_get(conn, "alerts_last_doc_id")
    if cursor is not None:
        limit = _fmt(now - timedelta(minutes=ALERT_LAG_MINUTES))
        row = conn.execute("SELECT COUNT(*), MIN(fetched_at) FROM documents WHERE id>? AND status<>'pending' "
                           "AND COALESCE(fetched_at,'')<>'' AND fetched_at<?", (int(cursor), limit)).fetchone()
        if row[0]:
            add("alerts_lag", "bloquant", f"alertes en retard : {row[0]} document(s) indexé(s) depuis plus de "
                                          f"{ALERT_LAG_MINUTES} min sans comparaison aux abonnements "
                                          f"(le plus ancien : {row[1]})")

    # 4. récapitulatif Telegram non parti (aucun hors session)
    if config.TELEGRAM_TOKEN and config.DIGEST_HOURS and config.SESSION:
        from zoneinfo import ZoneInfo

        from . import digest

        local = now.astimezone(ZoneInfo(config.TIMEZONE))
        slot = digest.due_slot(conn, now)
        if slot:
            hour = int(slot[-2:])
            late = local.replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(minutes=DIGEST_GRACE_MINUTES)
            if local >= late and db.meta_get(conn, "digest_slot"):
                add("digest", "bloquant", f"récapitulatif de {hour} h non envoyé (dernier : "
                                          f"{db.meta_get(conn, 'digest_slot')})")

    # 5. sources en échec continu : tous les passages des dernières 24 h en échec, source encore relevée. Les
    #    sources bloquées par un anti-bot (connu, jamais contourné) sont comptées à part, sans être listées.
    since = _fmt(now - timedelta(hours=SOURCE_FAIL_HOURS))
    rows = conn.execute("SELECT source_id, last_result FROM source_state WHERE failing_since IS NOT NULL "
                        "AND failing_since<? AND last_run>=? ORDER BY source_id",
                        (since, _fmt(now - timedelta(hours=2 * SOURCE_FAIL_HOURS)))).fetchall()
    failing = [r[0] for r in rows if _failed_result(r[1]) and not _antibot_only(r[1])]
    blocked = sum(1 for r in rows if _antibot_only(r[1]))
    if failing:
        more = f" et {len(failing) - 15} autres" if len(failing) > 15 else ""
        anti = f" (+ {blocked} bloquée(s) par un anti-bot)" if blocked else ""
        add("sources", "degrade", f"{len(failing)} source(s) sans aucun succès depuis plus de {SOURCE_FAIL_HOURS} h : "
                                  f"{', '.join(failing[:15])}{more}{anti}")

    # 6. reprise du WAL arrêtée : un lecteur tient une ancienne version de la base (connexion coincée, 26/11) ; les
    #    pages écrites s'accumulent dans le WAL sans pouvoir être reportées dans la base. Mesure directe : pages en
    #    attente après un checkpoint passif (ni la taille du fichier WAL, qui ne rétrécit pas toujours, ni la date de
    #    la base, qui ne change qu'aux checkpoints). Signal plus précoce que les battements (#21).
    try:
        if conn.in_transaction:   # checkpoint impossible dans une transaction (et l'annulerait) : l'autre processus voit
            raise sqlite3.Error("connexion en transaction")
        busy, log_pages, done = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        page = conn.execute("PRAGMA page_size").fetchone()[0]
        waiting = max(0, log_pages - done) * page
        if not busy and log_pages > 0 and waiting > WAL_STUCK_BYTES:
            add("wal", "bloquant", f"reprise du WAL arrêtée : {waiting >> 20} Mo écrits mais non reportés dans la "
                                   f"base (une connexion tient une ancienne version ; redémarrer le conteneur "
                                   f"concerné)")
    except sqlite3.Error:
        pass

    # 7. ressources du processus courant
    out += _mem_issue(process)
    try:
        du = shutil.disk_usage(config.DATA_DIR)
        if du.free < DISK_MIN_RATIO * du.total:
            add("disk", "degrade", f"disque presque plein : {du.free >> 30} Go libres sur {du.total >> 30} Go")
    except OSError:
        pass
    return out


def _mem_issue(process: str | None) -> list[dict[str, Any]]:
    mem = _cgroup_memory()
    if not (mem and mem[0] > MEM_RATIO * mem[1]):
        return []
    return [{"key": f"mem:{process or 'proc'}", "level": "degrade",
             "message": f"mémoire du conteneur {process or ''} à {100 * mem[0] // mem[1]} % de sa limite "
                        f"({mem[0] >> 20} / {mem[1] >> 20} Mo) : risque d'arrêt brutal"}]


def app_issues(conn, now: datetime | None = None) -> list[dict[str, Any]]:
    """Contrôle du site : le worker vérifie-t-il encore (sinon tout le reste est aveugle) ? Mémoire du site. Rien
    d'autre : le reste est vérifié par le worker seul (un écrivain par clé de problème)."""
    now = now or _now()
    out = []
    last = _parse(db.meta_get(conn, "watchdog_check:worker"))
    # Juste après un déploiement, la clé date encore de l'ancien worker : le temps que le nouveau fil `garde` la
    # réécrive, pas d'alerte `worker`.
    fresh = _app_started is not None and now - _app_started < timedelta(minutes=APP_GRACE_MINUTES)
    if last and not fresh and now - last > timedelta(seconds=WORKER_LATE_FACTOR * CHECK_EVERY):
        out.append({"key": "worker", "level": "bloquant",
                    "message": f"le worker ne vérifie plus : dernier contrôle il y a "
                               f"{int((now - last).total_seconds() // 60)} min (arrêté ou bloqué ; voir "
                               f"sudo journalctl -u docker | grep exitCode)"})
    return out + _mem_issue("app")


def check(conn=None, process: str | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Vérifie, alerte, annonce les rétablissements. Ne lève jamais. Site (`app`) : `app_issues` ; worker : `issues`. Chacun n'écrit que son état `watchdog_state:<processus>` et
    `watchdog_check:<processus>` (le worker aussi `watchdog_pending`). L'état est écrit AVANT toute annonce : si
    l'écriture échoue, rien ne part (#29, #23) ; le contrôle suivant annonce une seule fois."""
    from . import monitor

    try:
        conn = conn or db.get()
        now = now or _now()
        proc = process or "proc"
        monitor.admin_chats(conn), monitor.admin_emails(conn)   # destinataires du repli d'alerte, tenus à jour
        if proc == "app":
            found = app_issues(conn, now)
        else:
            if proc == "worker":   # ancien état commun au site et au worker (avant la phase 1) : abandonné
                conn.execute("DELETE FROM meta WHERE key='watchdog_state'")
                conn.commit()
            seen = _jget(conn, "watchdog_pending", {})
            ids = {str(r[0]) for r in conn.execute("SELECT id FROM documents WHERE status='pending'")}
            db.meta_set(conn, "watchdog_pending", json.dumps({i: seen.get(i, _fmt(now)) for i in ids}))
            found = issues(conn, proc, now)
        state = _jget(conn, f"watchdog_state:{proc}", {})   # {key: {"since", "sent", "noted", "level", "message"}}
        current = {i["key"]: i for i in found}
        todo = []   # annonces, faites après l'écriture de l'état
        for key, i in current.items():
            st = state.get(key) or {"since": _fmt(now)}
            if i["level"] == "bloquant":   # rappel toutes les RESEND_MINUTES, par error_events
                todo.append(("bloquant", key, i["message"]))
                st["sent"] = st.get("sent") or _fmt(now)
            elif config.SESSION:
                sent = _parse(st.get("sent"))
                if sent is None or now - sent > timedelta(hours=DEGRADED_REPEAT_HOURS):
                    todo.append(("degrade", key, i["message"]))
                    st["sent"] = _fmt(now)
            else:   # hors session : dégradé noté dans error_events seulement (ni message, ni « rétabli »)
                noted = _parse(st.get("noted"))
                if noted is None or now - noted > timedelta(hours=DEGRADED_REPEAT_HOURS):
                    todo.append(("note", key, i["message"]))
                    st["noted"] = _fmt(now)
            st.update(level=i["level"], message=i["message"][:300])
            state[key] = st
        gone = [state.pop(k) | {"key": k} for k in [k for k in state if k not in current]]
        db.meta_set(conn, f"watchdog_state:{proc}", json.dumps(state))
        db.meta_set(conn, f"watchdog_check:{proc}", _fmt(now))
        for kind, key, message in todo:
            if kind == "bloquant":
                monitor.report(f"chien de garde : {key}", f"Blocage: {message}", conn=conn, background=False,
                               urgent=True)
            elif kind == "degrade":
                monitor.notify_admins(f"🟠 <b>Dégradé</b> : {_esc(message)}", conn=conn)
            else:
                monitor.report(f"chien de garde : {key}", f"Degrade: {message}", conn=conn, background=False)
        for st in gone:
            if st.get("sent"):
                monitor.notify_admins(f"✅ <b>Rétabli</b> : {_esc(st.get('message', st['key']))} "
                                      f"(problème constaté depuis {st.get('since')})", conn=conn)
        return found
    except Exception as exc:  # noqa: BLE001
        monitor.report("chien de garde", exc, conn=conn)
        return []


def _esc(text: str) -> str:
    import html

    return html.escape(text)


_status_cache: tuple[float, dict[str, Any]] | None = None


def status(conn=None) -> dict[str, Any]:
    """État pour une sonde externe : seulement des noms de problèmes. Mis en cache 60 s (route publique)."""
    global _status_cache
    if _status_cache and time.monotonic() - _status_cache[0] < 60:
        return _status_cache[1]
    try:
        found = issues(conn or db.get(), "app")
        st = {"ok": not any(i["level"] == "bloquant" for i in found),
              "bloquant": sorted(i["key"] for i in found if i["level"] == "bloquant"),
              "degrade": sorted(i["key"] for i in found if i["level"] == "degrade")}
    except Exception as exc:  # noqa: BLE001  (base illisible…) : c'est un échec de la sonde
        st = {"ok": False, "bloquant": [f"sonde : {type(exc).__name__}"], "degrade": []}
    _status_cache = (time.monotonic(), st)
    return st


def run_forever(process: str, stop: threading.Event | None = None, every: int = CHECK_EVERY) -> None:
    """Fil de surveillance : `app` (site : démarrage, battement « app », mini-contrôle) ou `worker` (fil `garde` :
    contrôle complet, indépendant de la veille ; démarrage et battements déclarés par worker.run_forever).
    Ne s'arrête jamais sur une erreur (un fil figé se voit de l'autre processus)."""
    global _app_started
    from . import monitor

    conn = None
    ready = process != "app"
    while not (stop and stop.is_set()):
        try:
            conn = conn or db.connect()
            if not ready:
                _app_started = _now()
                process_started(conn, process)
                expect(process, {process: max(15, 3 * every // 60), f"{process}-boucle": 10}, conn)
                ready = True
            if process == "app":
                beat(process, conn, every=0)
            check(conn, process)
        except Exception as exc:  # noqa: BLE001
            monitor.report(f"chien de garde ({process})", exc)
        if stop:
            stop.wait(every)
        else:
            time.sleep(every)


ACTIVE: str | None = None   # nom du processus surveillé (site lancé par `cop serve`) ; None : tests, scripts


async def loop_heartbeat(process: str, every: int = 60) -> None:
    """Battement écrit DEPUIS la boucle d'événements du site : il s'arrête si la boucle est bloquée (outil MCP
    synchrone interminable…), alors que le fil de surveillance, lui, continuerait de battre."""
    import asyncio

    from starlette.concurrency import run_in_threadpool

    while True:
        await run_in_threadpool(beat, f"{process}-boucle", None, 0)
        await asyncio.sleep(every)


def start_thread(process: str) -> None:
    global ACTIVE
    if not config.WATCHDOG:
        return
    ACTIVE = process
    threading.Thread(target=run_forever, args=(process,), daemon=True, name="watchdog").start()
