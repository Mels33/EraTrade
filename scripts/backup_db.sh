#!/usr/bin/env bash
# Резервная копия БД EraTrade в backups/<db>_<UTC-время>.dump (формат pg_dump -Fc).
# Использование: scripts/backup_db.sh [eratrade_testnet|eratrade_mainnet]
set -euo pipefail

DB="${1:-eratrade_testnet}"
CONTAINER="${ERATRADE_PG_CONTAINER:-eratrade-pg}"
DIR="$(cd "$(dirname "$0")/.." && pwd)/backups"
mkdir -p "$DIR"

OUT="$DIR/${DB}_$(date -u +%Y%m%dT%H%M%SZ).dump"
docker exec "$CONTAINER" pg_dump -U eratrade -Fc "$DB" > "$OUT.tmp"
mv "$OUT.tmp" "$OUT"
echo "$OUT"
