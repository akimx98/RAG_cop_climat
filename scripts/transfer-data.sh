#!/usr/bin/env bash
# Transfert du corpus construit sur le Mac vers le VPS (rsync sur SSH, chiffré).
# La base contient les documents INTERNES (accès restreint) : ne transférer que vers le VPS prévu.
#   HOST=root@<vps> bash scripts/transfer-data.sh
#   DRY_RUN=1 HOST=… bash scripts/transfer-data.sh
set -euo pipefail
HOST="${HOST:?HOST=user@serveur requis}"
REMOTE_DIR="${REMOTE_DIR:-/home/debian/RAG_cop_climat}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
OPTS=(-az --delete-after)
[ "${DRY_RUN:-0}" = "1" ] && OPTS+=(-n -v)
# Copie cohérente de la base SQLite (WAL) avant envoi.
sqlite3 data/cop.sqlite3 ".backup data/cop.transfer.sqlite3"
ssh "$HOST" "mkdir -p $REMOTE_DIR/data"
rsync "${OPTS[@]}" data/raw/ "$HOST:$REMOTE_DIR/data/raw/"
rsync "${OPTS[@]}" data/cop.transfer.sqlite3 "$HOST:$REMOTE_DIR/data/cop.incoming.sqlite3"
rm -f data/cop.transfer.sqlite3
echo "Sur le VPS, service arrêté : mv data/cop.incoming.sqlite3 data/cop.sqlite3 (première installation)"
