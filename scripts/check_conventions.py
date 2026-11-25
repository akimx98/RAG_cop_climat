#!/usr/bin/env python3
# COPIE de mcp-edge/scripts/check_conventions.py (référence) : ne pas modifier ici, lancer mcp-edge/scripts/sync-check.sh
"""Contrôle automatique des conventions du VPS partagé (mcp-edge/CONVENTIONS.md) avant un déploiement.

Référence : mcp-edge/scripts/check_conventions.py. Les dépôts des services en embarquent une COPIE
(scripts/check_conventions.py), mise à jour par mcp-edge/scripts/sync-check.sh : ne pas la modifier sur place.

  check_conventions.py deploy/docker-compose.yml            contrôle statique (CI, poste, VPS)
  check_conventions.py deploy/docker-compose.yml --vps      + noms et alias des AUTRES projets en marche
  check_conventions.py snapshot FICHIER                     conteneurs en marche avant déploiement
  check_conventions.py neighbors FICHIER                    après : tous tournent encore, aucun « unhealthy »

Code de sortie : 0 conforme (avertissements possibles), 1 erreur de convention, 2 usage ou lecture impossible.
Variable DOCKER : commande docker à utiliser (ex. « sudo docker ») ; défaut « docker ».
Bibliothèque standard seulement ; PyYAML s'il est installé, sinon `docker compose config`."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

# Inventaire du VPS (CONVENTIONS.md, « Inventaire ») : nom de projet Compose → alias sur le réseau « web ».
INVENTORY = {
    "maths-corpus": {"mcp-maths"},
    "programmes-scolaires": {"mcp-histoire"},
    "cop31": {"cop31-app"},
    "rag-embed": set(),
    "rag-articles": set(),
    "mcp-edge": set(),
}
GENERIC_NAMES = {"deploy", "docker", "app", "compose", "default"}
GENERIC_ALIASES = {"mcp", "app", "web", "api", "server", "service", "backend"}
AUTO_RESTART = {"always", "unless-stopped", "on-failure"}
# Exceptions TEMPORAIRES, datées : avertissement au lieu d'erreur. À retirer dès que la condition est vérifiée.
TRANSITIONAL_ALIASES: dict[str, set[str]] = {
    # ex. "projet": {"alias"} — 25/11/2026 : plus aucune exception (alias « mcp » d'histoire-géo retiré).
}


def _docker() -> list[str]:
    return shlex.split(os.environ.get("DOCKER", "docker"))


def load(path: Path) -> dict:
    """Compose → dict. PyYAML si présent (variables laissées telles quelles), sinon `docker compose config`."""
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None
    if yaml is not None:
        data = yaml.safe_load(path.read_text())
        return data or {}
    res = subprocess.run([*_docker(), "compose", "-f", str(path), "config", "--format", "json"],
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"lecture du compose impossible (installer PyYAML ?) : {res.stderr.strip()[:300]}")
    return json.loads(res.stdout)


def _networks(svc: dict) -> dict:
    nets = svc.get("networks") or {}
    if isinstance(nets, list):
        return {n: {} for n in nets}
    return {k: (v or {}) for k, v in nets.items()}


def _has_mem_limit(svc: dict) -> bool:
    if svc.get("mem_limit"):
        return True
    limits = ((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {}
    return bool(limits.get("memory"))


def _dockerfile_healthcheck(base: Path, svc: dict) -> bool:
    build = svc.get("build")
    if not build:
        return False
    if isinstance(build, str):
        ctx, dockerfile = build, "Dockerfile"
    else:
        ctx, dockerfile = build.get("context", "."), build.get("dockerfile", "Dockerfile")
    for candidate in ((base / ctx / dockerfile), (base / dockerfile)):
        try:
            if re.search(r"^\s*HEALTHCHECK\s+(?!NONE)", candidate.read_text(), re.M | re.I):
                return True
        except OSError:
            continue
    return False


def _build_contexts(base: Path, services: dict) -> set[Path]:
    out = set()
    for svc in services.values():
        build = svc.get("build")
        if build:
            ctx = build if isinstance(build, str) else build.get("context", ".")
            out.add((base / ctx).resolve())
    return out


def check_static(path: Path) -> tuple[list[str], list[str], dict]:
    """(erreurs, avertissements, compose) pour un fichier compose."""
    data = load(path)
    base = path.parent
    errors: list[str] = []
    warnings: list[str] = []
    name = data.get("name")
    services = data.get("services") or {}

    # §1 nom de projet explicite et distinct
    if not name:
        errors.append("§1 pas de `name:` en tête du compose (le nom serait déduit du dossier, ex. « deploy »)")
    elif name in GENERIC_NAMES:
        errors.append(f"§1 nom de projet générique « {name} »")
    mine = INVENTORY.get(name, set()) if name else set()

    for sname, svc in services.items():
        svc = svc or {}
        # §4 plafond mémoire
        if not _has_mem_limit(svc):
            msg = f"§4 service « {sname} » sans plafond mémoire (`mem_limit`)"
            if svc.get("profiles"):   # outil lancé à la main (`--profile …`), pas par `up` : à plafonner une fois mesuré
                warnings.append(msg + " — outil hors `up` (profiles)")
            else:
                errors.append(msg)
        # §2 alias propre sur le réseau web
        web = _networks(svc).get("web")
        if web is not None:
            aliases = set((web or {}).get("aliases") or [])
            if not aliases:
                errors.append(f"§2 service « {sname} » sur le réseau web sans alias propre")
            for a in sorted(aliases):
                if a in GENERIC_ALIASES:
                    if a in TRANSITIONAL_ALIASES.get(name, set()):
                        warnings.append(f"§2 alias générique « {a} » toléré temporairement (transition, à retirer)")
                    else:
                        errors.append(f"§2 alias générique « {a} » (service « {sname} »)")
                for other, other_aliases in INVENTORY.items():
                    if other != name and a in other_aliases:
                        errors.append(f"§2 alias « {a} » déjà pris par le projet « {other} »")
                if name in INVENTORY and a not in mine and a not in TRANSITIONAL_ALIASES.get(name, set()):
                    warnings.append(f"§2 alias « {a} » absent de l'inventaire (CONVENTIONS.md) pour « {name} »")
            # §7 healthcheck applicatif
            hc = svc.get("healthcheck")
            if not (hc and not hc.get("disable")) and not _dockerfile_healthcheck(base, svc):
                warnings.append(f"§7 service web « {sname} » sans healthcheck (compose ou HEALTHCHECK du Dockerfile)")
        # §13 arrêt propre
        if svc.get("restart") in AUTO_RESTART and not svc.get("stop_grace_period"):
            warnings.append(f"§13 service « {sname} » relancé automatiquement sans `stop_grace_period`")

    # §6 .dockerignore à la racine de chaque contexte de build
    for ctx in sorted(_build_contexts(base, services)):
        if not (ctx / ".dockerignore").is_file():
            errors.append(f"§6 contexte de build {ctx} sans .dockerignore")
    if name and name not in INVENTORY:
        warnings.append(f"projet « {name} » absent de l'inventaire : l'ajouter à CONVENTIONS.md et à ce script")
    return errors, warnings, data


def _run_json_lines(args: list[str]) -> list[dict]:
    res = subprocess.run([*_docker(), *args], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[:300])
    out = res.stdout.strip()
    if not out:
        return []
    if out.startswith("["):
        return json.loads(out)
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def check_vps(path: Path, data: dict) -> tuple[list[str], list[str]]:
    """Noms de projet et alias réellement pris par les AUTRES projets en marche sur cet hôte Docker."""
    errors: list[str] = []
    warnings: list[str] = []
    name = data.get("name")
    me = os.path.realpath(path)
    try:
        projects = _run_json_lines(["compose", "ls", "--format", "json"])   # en marche seulement
    except (RuntimeError, OSError, ValueError) as exc:
        return [], [f"--vps : projets en marche illisibles ({exc}) : contrôle sauté"]
    for p in projects:
        files = str(p.get("ConfigFiles", ""))
        theirs = {os.path.realpath(f.strip()) for f in files.split(",") if f.strip()}
        if p.get("Name") == name and me not in theirs:   # chemins réels : un lien (/opt/…) n'est pas un autre projet
            errors.append(f"§1 un autre projet « {name} » tourne déjà ({files})")
    my_aliases = set()
    for svc in (data.get("services") or {}).values():
        web = _networks(svc or {}).get("web")
        if web is not None:
            my_aliases |= set((web or {}).get("aliases") or [])
    if not my_aliases:
        return errors, warnings
    try:
        ids = subprocess.run([*_docker(), "network", "inspect", "web", "--format",
                              "{{range $k, $v := .Containers}}{{$k}} {{end}}"],
                             capture_output=True, text=True, check=True).stdout.split()
    except (subprocess.CalledProcessError, OSError):
        return errors, warnings + ["--vps : réseau web invisible depuis ce Docker (rootless ?) : alias non vérifiés"]
    for cid in ids:
        info = json.loads(subprocess.run([*_docker(), "inspect", cid], capture_output=True, text=True,
                                         check=True).stdout)[0]
        project = (info.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project")
        if project == name:
            continue
        taken = set(((info.get("NetworkSettings", {}).get("Networks") or {}).get("web") or {}).get("Aliases") or [])
        for a in sorted(my_aliases & taken):
            errors.append(f"§2 alias « {a} » déjà porté sur web par {info.get('Name', cid).lstrip('/')}")
    return errors, warnings


def _ps() -> dict[str, str]:
    res = subprocess.run([*_docker(), "ps", "--format",
                          '{{.Names}}\t{{.Status}}\t{{.Label "com.docker.compose.oneoff"}}'],
                         capture_output=True, text=True, check=True)
    out = {}
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and (len(parts) < 3 or parts[2] != "True"):   # `run --rm` ponctuels : pas des voisins
            out[parts[0]] = parts[1]
    return out


def snapshot(path: Path) -> int:
    path.write_text(json.dumps(_ps()))
    return 0


def neighbors(path: Path) -> int:
    """§9 : tout conteneur qui tournait avant le déploiement tourne encore et n'est pas « unhealthy »."""
    before = json.loads(path.read_text())
    now = _ps()
    bad = []
    for cname in sorted(before):
        status = now.get(cname)
        if status is None:
            bad.append(f"{cname} : ne tourne plus")
        elif "unhealthy" in status:
            bad.append(f"{cname} : {status}")
    for line in bad:
        print(f"ERREUR §9 voisin : {line}")
    if not bad:
        print(f"§9 voisins : {len(before)} conteneur(s) toujours en marche")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] in ("snapshot", "neighbors"):
        return (snapshot if argv[0] == "snapshot" else neighbors)(Path(argv[1]))
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    path = Path(argv[0])
    try:
        errors, warnings, data = check_static(path)
        if "--vps" in argv[1:]:
            e2, w2 = check_vps(path, data)
            errors, warnings = errors + e2, warnings + w2
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERREUR : {exc}")
        return 2
    for w in warnings:
        print(f"avertissement {w}")
    for e in errors:
        print(f"ERREUR {e}")
    status = "NON CONFORME" if errors else "conforme"
    print(f"conventions du VPS ({path}) : {status} — {len(errors)} erreur(s), {len(warnings)} avertissement(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
