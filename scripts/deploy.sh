#!/usr/bin/env bash
# Déploiement du code sur le VPS (à lancer sur le VPS, dans /home/debian/RAG_cop_climat). Depuis le Mac, préférer scripts/ship.sh (tests, contrôles, retour arrière).
#   sudo bash scripts/deploy.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[ -f deploy/.env ] || { echo "deploy/.env manquant (copier deploy/.env.example)"; exit 1; }
grep -q '^COP_SECRET=.\+' deploy/.env || { echo "COP_SECRET vide dans deploy/.env"; exit 1; }
docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml ps
