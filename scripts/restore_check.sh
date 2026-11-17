#!/usr/bin/env bash
# Проверка, что бэкап восстанавливается: заливает его во временную БД и удаляет её.
# Использование: scripts/restore_check.sh backups/<file>.dump
set -euo pipefail

DUMP="${1:?путь к .dump}"
CONTAINER="${ERATRADE_PG_CONTAINER:-eratrade-pg}"
TMP_DB="restore_check_$(date -u +%s)"

docker exec "$CONTAINER" createdb -U eratrade "$TMP_DB"
trap 'docker exec "$CONTAINER" dropdb -U eratrade --if-exists "$TMP_DB"' EXIT

docker exec -i "$CONTAINER" pg_restore -U eratrade --exit-on-error -d "$TMP_DB" < "$DUMP"
TABLES=$(docker exec "$CONTAINER" psql -U eratrade -d "$TMP_DB" -tAc \
  "select count(*) from information_schema.tables where table_schema='public'")
echo "restore OK: $TABLES таблиц в public"
# Журнал для `eratrade live-check` (проверка восстановления не старше N дней).
LOG_DIR="$(cd "$(dirname "$DUMP")" && pwd)"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) OK $(basename "$DUMP") tables=$TABLES" >>"$LOG_DIR/restore_checks.log"
