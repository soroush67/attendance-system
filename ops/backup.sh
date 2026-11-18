#!/bin/sh
set -eu
mkdir -p /backups
case "${BACKUP_INTERVAL_HOURS:-24}" in *[!0-9]*|'') echo 'invalid BACKUP_INTERVAL_HOURS' >&2; exit 1;; esac
case "${BACKUP_RETENTION_DAYS:-30}" in *[!0-9]*|'') echo 'invalid BACKUP_RETENTION_DAYS' >&2; exit 1;; esac
[ "$BACKUP_INTERVAL_HOURS" -gt 0 ] && [ "$BACKUP_RETENTION_DAYS" -gt 0 ] || exit 1
while :; do
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  partial="/backups/attendance_${stamp}.dump.partial"
  final="/backups/attendance_${stamp}.dump"
  if pg_dump -Fc --no-owner --no-acl -f "$partial" && pg_restore -l "$partial" >/dev/null; then
    mv "$partial" "$final"
    sha256sum "$final" > "${final}.sha256"
    echo "Backup verified: $final"
    find /backups -type f \( -name '*.dump' -o -name '*.sha256' \) -mtime "+${BACKUP_RETENTION_DAYS}" -delete
  else
    echo "Backup failed: $stamp" >&2
    rm -f "$partial"
  fi
  sleep "${BACKUP_INTERVAL_HOURS}h"
done
