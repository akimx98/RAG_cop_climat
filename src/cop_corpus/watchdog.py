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
3. Invariants vérifiés de l'extérieur (`check`), par le site ET par le worker, qui se surveillent donc l'un l'autre :
   battements en retard, documents bloqués en 'pending' (ils arrêtent les alertes), curseur des alertes en retard,
   récapitulatif non envoyé, sources en échec continu, mémoire du conteneur ou disque presque pleins.

Problème bloquant → `monitor.report` (Telegram, à défaut e-mail ; rappel toutes les 30 min tant qu'il dure).
Problème dégradé (sources en échec, ressources) → au plus un message par jour et par problème.
Problème résolu → message « rétabli ». `status()` sert aussi /healthz/deep (sonde externe : 503 si bloquant)."""

from __future__ import annotations

import json
import shutil
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
DEGRADED_REPEAT_HOURS = 24

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
                       "Voir sudo journalctl -u docker | grep exitCode", conn=conn, background=False)
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
    """Problèmes constatés maintenant : [{key, level (bloquant|degrade), message}]. Lecture seule."""
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

    # 4. récapitulatif Telegram non parti
    if config.TELEGRAM_TOKEN and config.DIGEST_HOURS:
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

    # 6. ressources du processus courant
    mem = _cgroup_memory()
    if mem and mem[0] > MEM_RATIO * mem[1]:
        add(f"mem:{process or 'proc'}", "degrade",
            f"mémoire du conteneur {process or ''} à {100 * mem[0] // mem[1]} % de sa limite "
            f"({mem[0] >> 20} / {mem[1] >> 20} Mo) : risque d'arrêt brutal")
    try:
        du = shutil.disk_usage(config.DATA_DIR)
        if du.free < DISK_MIN_RATIO * du.total:
            add("disk", "degrade", f"disque presque plein : {du.free >> 30} Go libres sur {du.total >> 30} Go")
    except OSError:
        pass
    return out


def check(conn=None, process: str | None = None) -> list[dict[str, Any]]:
    """Vérifie, alerte, annonce les rétablissements. Appelé par le site et par le worker. Ne lève jamais."""
    from . import monitor

    try:
        conn = conn or db.get()
        now = _now()
        # Un seul écrivain de l'état (alertes, « rétabli ») : le worker. Le site ne prend le relais que si le worker
        # ne vérifie plus (arrêté, bloqué) ; il le signale alors lui-même par le battement en retard.
        if process == "app":
            last = _parse(db.meta_get(conn, "watchdog_check:worker"))
            if last and now - last < timedelta(seconds=3 * CHECK_EVERY):
                return issues(conn, process, now)
        seen = _jget(conn, "watchdog_pending", {})
        ids = {str(r[0]) for r in conn.execute("SELECT id FROM documents WHERE status='pending'")}
        db.meta_set(conn, "watchdog_pending", json.dumps({i: seen.get(i, _fmt(now)) for i in ids}))
        found = issues(conn, process, now)
        state = _jget(conn, "watchdog_state", {})       # {key: {"since":…, "sent":…, "level":…, "message":…}}
        current = {i["key"]: i for i in found}
        for key, i in current.items():
            st = state.get(key) or {"since": _fmt(now)}
            if i["level"] == "bloquant":
                if monitor.report(f"chien de garde : {key}", f"Blocage: {i['message']}", conn=conn, background=False):
                    st["sent"] = _fmt(now)
            else:
                sent = _parse(st.get("sent"))
                if sent is None or now - sent > timedelta(hours=DEGRADED_REPEAT_HOURS):
                    monitor.notify_admins(f"🟠 <b>Dégradé</b> : {_esc(i['message'])}", conn=conn)
                    st["sent"] = _fmt(now)
            st.update(level=i["level"], message=i["message"][:300])
            state[key] = st
        for key in [k for k in state if k not in current and _owned(k, process)]:
            st = state.pop(key)
            if st.get("sent"):
                monitor.notify_admins(f"✅ <b>Rétabli</b> : {_esc(st.get('message', key))} "
                                      f"(problème constaté depuis {st.get('since')})", conn=conn)
        db.meta_set(conn, "watchdog_state", json.dumps(state))
        db.meta_set(conn, f"watchdog_check:{process or 'proc'}", _fmt(now))
        return found
    except Exception as exc:  # noqa: BLE001
        monitor.report("chien de garde", exc, conn=conn)
        return []


def _owned(key: str, process: str | None) -> bool:
    """Une alerte mémoire n'est levée (et donc rétablie) que par le processus qui la mesure."""
    return not key.startswith("mem:") or key == f"mem:{process or 'proc'}"


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
    """Fil de surveillance du site : démarrage (arrêt anormal précédent ?), battement « app », vérifications.
    Ne s'arrête jamais sur une erreur (le worker voit de toute façon le battement « app » vieillir)."""
    from . import monitor

    conn = None
    ready = False
    while not (stop and stop.is_set()):
        try:
            conn = conn or db.connect()
            if not ready:
                process_started(conn, process)
                expect(process, {process: max(15, 3 * every // 60), f"{process}-boucle": 10}, conn)
                ready = True
            beat(process, conn, every=0)
            check(conn, process)
        except Exception as exc:  # noqa: BLE001
            monitor.report("chien de garde du site", exc)
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
