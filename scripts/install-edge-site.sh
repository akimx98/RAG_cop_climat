#!/usr/bin/env bash
# Installe la route de CE corpus dans l'edge Caddy partagé du VPS.
#
# L'edge est une infrastructure commune : il vit hors des dépôts (/opt/mcp-edge)
# et importe un fichier par site depuis conf.d/. Ce script y dépose le nôtre, et
# uniquement le nôtre. Il ne crée pas l'edge et ne redémarre rien : il valide puis
# recharge à chaud, donc sans coupure pour les autres corpus.
#
#   DOMAIN="opencop.org, www.opencop.org, cop31.akimx.fr" sudo -E bash scripts/install-edge-site.sh
#   (toujours les trois noms : un nom omis n'est plus servi ; cop31.akimx.fr porte le certificat du courrier entrant)
#
# Variables : DOMAIN (défaut : celui de deploy/edge/.env), BACKEND (défaut
#             cop31-app:8000), SITE (nom du fichier, défaut cop31),
#             EDGE_DIR (défaut /opt/mcp-edge).
#
# Idempotent : relançable sans casse.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EDGE_DIR="${EDGE_DIR:-/opt/mcp-edge}"
SITE="${SITE:-cop31}"
BACKEND="${BACKEND:-cop31-app:8000}"
DOMAIN="${DOMAIN:-}"

# À défaut, on reprend le domaine déjà configuré pour ce corpus.
if [ -z "$DOMAIN" ] && [ -f "$REPO_DIR/deploy/edge/.env" ]; then
	DOMAIN="$(grep -E '^DOMAIN=' "$REPO_DIR/deploy/edge/.env" | cut -d= -f2- | head -1)"
fi

if [ "$(id -u)" -ne 0 ]; then
	echo "ERREUR : lancer en root (sudo -E, pour conserver les variables)." >&2
	exit 1
fi
if [ -z "$DOMAIN" ]; then
	echo "ERREUR : DOMAIN requis (ni fourni, ni trouvé dans deploy/edge/.env)." >&2
	exit 1
fi
if [ ! -d "$EDGE_DIR/conf.d" ]; then
	echo "ERREUR : edge partagé introuvable dans $EDGE_DIR." >&2
	echo "Il s'installe depuis le dépôt qui en porte la définition de base" >&2
	echo "(dépôt mcp-edge : scripts/install.sh)." >&2
	exit 1
fi

TEMPLATE="$REPO_DIR/deploy/edge/sites/$SITE.caddy.template"
[ -f "$TEMPLATE" ] || { echo "ERREUR : gabarit absent : $TEMPLATE" >&2; exit 1; }

echo "==> Route : $DOMAIN -> $BACKEND  (conf.d/$SITE.caddy)"
TMP="$(mktemp)"
sed -e "s|__DOMAIN__|$DOMAIN|g" -e "s|__BACKEND__|$BACKEND|g" "$TEMPLATE" >"$TMP"

# Validation AVANT d'écrire dans conf.d : une erreur de syntaxe ferait échouer le
# rechargement, et c'est TOUT l'edge — donc les autres corpus — qui resterait sur
# l'ancienne configuration sans qu'on sache pourquoi.
STAGE="$(mktemp -d)"
cp "$EDGE_DIR"/conf.d/*.caddy "$STAGE"/ 2>/dev/null || true
cp "$TMP" "$STAGE/$SITE.caddy"
echo "==> Validation"
docker run --rm \
	-v "$EDGE_DIR/Caddyfile:/etc/caddy/Caddyfile:ro" \
	-v "$STAGE:/etc/caddy/conf.d:ro" \
	caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile \
	2>&1 | tail -1
rm -rf "$STAGE"

install -m 644 "$TMP" "$EDGE_DIR/conf.d/$SITE.caddy"
rm -f "$TMP"

echo "==> Rechargement à chaud de l'edge (sans coupure)"
EDGE_CONTAINER="$(docker ps --filter "publish=443" --format '{{.Names}}' | head -1)"
docker exec "$EDGE_CONTAINER" caddy reload \
	--config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | tail -1

echo
echo "Sites routés par l'edge :"
ls "$EDGE_DIR/conf.d" | sed 's/^/  /'
echo
echo "Vérifier : curl -s https://$DOMAIN/healthz   # {"ok": true} attendu"
