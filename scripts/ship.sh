#!/usr/bin/env bash
# Mise en production depuis le Mac, en une commande, avec garde-fous.
#
#   scripts/ship.sh              tests → envoi du code → reconstruction de tous les conteneurs → contrôles
#   scripts/ship.sh --app        idem, mais ne reconstruit que le site (app) : n'interrompt pas le worker
#   scripts/ship.sh --rollback   remet les images d'avant le dernier déploiement (sans reconstruire)
#
# Refuse de partir si des modifications ne sont pas commitées ou si un test échoue. Après le déploiement :
# /healthz doit répondre, puis une vraie question doit recevoir une réponse sourcée (`cop smoke`, ~0,02 $).
# Les images précédentes sont gardées sous le tag :previous pour revenir en arrière en une commande.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

VPS="${COP_VPS:-debian@152.228.141.68}"
DIR="/home/debian/RAG_cop_climat"
URL="${COP_URL:-https://opencop.org}"
# Ancienne adresse, toujours servie (connecteurs MCP, QR codes, courrier entrant) : contrôlée aussi. Vide = ignorée.
OLD_URL="${COP_OLD_URL-https://cop31.akimx.fr}"
COMPOSE="sudo docker compose -f deploy/docker-compose.yml"
SERVICES="app worker mail"

red() { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
step() { printf '\n\033[1m▶ %s\033[0m\n' "$*"; }

remote() { ssh -o ConnectTimeout=15 "$VPS" "cd $DIR && $*"; }

check_prod() {
  step "Contrôle : /healthz"
  for i in $(seq 1 30); do
    if curl -fsS -m 5 "$URL/healthz" >/dev/null 2>&1; then green "  le site répond"; break; fi
    [ "$i" = 30 ] && { red "  le site ne répond pas après 60 s"; return 1; }
    sleep 2
  done
  if [ -n "$OLD_URL" ] && [ "$OLD_URL" != "$URL" ]; then
    curl -fsS -m 10 "$OLD_URL/healthz" >/dev/null 2>&1 && green "  l'ancienne adresse répond ($OLD_URL)" \
      || { red "  l'ancienne adresse ne répond pas ($OLD_URL)"; return 1; }
  fi
  step "Contrôle : une vraie question de bout en bout"
  remote "$COMPOSE exec -T app cop smoke" || return 1
}

fail() {
  red ""
  red "✖ ÉCHEC DU DÉPLOIEMENT : $*"
  red "  Revenir à la version précédente :  scripts/ship.sh --rollback"
  red "  Journaux :  ssh $VPS 'sudo docker logs --tail 100 cop31-app-1'"
  exit 1
}

if [ "${1:-}" = "--rollback" ]; then
  step "Retour aux images précédentes"
  remote "for s in $SERVICES; do sudo docker image inspect cop31-\$s:previous >/dev/null 2>&1 && sudo docker tag cop31-\$s:previous cop31-\$s:latest; done; $COMPOSE up -d --no-build"
  check_prod || fail "même la version précédente ne passe pas les contrôles"
  green "✔ Version précédente remise en service (le code sur le VPS reste le nouveau : redéployer après correction)."
  exit 0
fi

TARGET="$SERVICES"
[ "${1:-}" = "--app" ] && TARGET="app"

step "Modifications non commitées ?"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  git status --short --untracked-files=no
  fail "commitez d'abord (on déploie ce qui est dans git, pas un état intermédiaire)"
fi
SHA="$(git rev-parse --short HEAD)"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "main" ] || red "  attention : vous déployez la branche $BRANCH, pas main"
green "  $SHA ($BRANCH) : $(git log -1 --format=%s)"

step "Conventions du VPS partagé (mcp-edge/CONVENTIONS.md)"
.venv/bin/python scripts/check_conventions.py deploy/docker-compose.yml || fail "compose non conforme aux conventions du VPS"

step "Tests"
.venv/bin/python -m pytest -q -p no:cacheprovider || fail "des tests échouent : rien n'a été envoyé"

step "Sauvegarde des images actuelles (:previous)"
remote "for s in $TARGET; do sudo docker image inspect cop31-\$s:latest >/dev/null 2>&1 && sudo docker tag cop31-\$s:latest cop31-\$s:previous; done; true"

step "Envoi du code"
# Sans --delete : les fichiers propres au VPS (données, .env, fichiers générés) ne sont jamais supprimés.
rsync -az \
  --exclude data --exclude .venv --exclude .git --exclude deploy/.env --exclude .claude \
  --exclude __pycache__ --exclude '*.pyc' --exclude .pytest_cache --exclude .DS_Store \
  ./ "$VPS:$DIR/"
remote "echo '$SHA $(date -u +%Y-%m-%dT%H:%M:%SZ)' > .deployed"

step "Conventions sur le VPS (noms et alias des voisins) ; état des voisins avant"
remote "DOCKER='sudo docker' python3 scripts/check_conventions.py deploy/docker-compose.yml --vps && DOCKER='sudo docker' python3 scripts/check_conventions.py snapshot /tmp/cop31-voisins-avant.json" \
  || fail "conventions non respectées sur le VPS (rien n'a été reconstruit)"

step "Reconstruction : $TARGET"
remote "$COMPOSE up -d --build $TARGET" || fail "la construction a échoué (les anciens conteneurs tournent encore)"

check_prod || fail "les contrôles après déploiement ne passent pas"
step "Voisins du VPS (CONVENTIONS §9)"
remote "DOCKER='sudo docker' python3 scripts/check_conventions.py neighbors /tmp/cop31-voisins-avant.json" \
  || fail "un autre service du VPS ne tourne plus (ou est unhealthy) après ce déploiement"
green ""
green "✔ $SHA en production ($URL)"
