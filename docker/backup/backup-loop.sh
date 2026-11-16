#!/bin/sh
# Ежедневный pg_dump всех БД EraTrade в /backups, хранение BACKUP_RETENTION_DAYS дней.
set -eu
INTERVAL="${BACKUP_INTERVAL_SECONDS:-86400}"
RETENTION="${BACKUP_RETENTION_DAYS:-14}"

while true; do
  for db in eratrade_testnet eratrade_mainnet; do
    out="/backups/${db}_$(date -u +%Y%m%dT%H%M%SZ).dump"
    if pg_dump -h postgres -U eratrade -Fc "$db" > "$out.tmp"; then
      mv "$out.tmp" "$out"
      echo "$(date -u +%FT%TZ) backup OK: $out"
    else
      rm -f "$out.tmp"
      echo "$(date -u +%FT%TZ) backup FAILED: $db" >&2
    fi
  done
  find /backups -name 'eratrade_*.dump' -mtime +"$RETENTION" -delete
  sleep "$INTERVAL"
done
