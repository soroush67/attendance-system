#!/bin/sh
set -eu
if [ "$#" -ne 1 ]; then echo 'Usage: ./ops/restore.sh /absolute/path/attendance_YYYYMMDD.dump' >&2; exit 2; fi
case "$1" in /*) ;; *) echo 'Absolute dump path required' >&2; exit 2;; esac
[ -f "$1" ] || { echo 'Dump not found' >&2; exit 2; }
echo 'DANGER: This replaces attendance database contents. Stop web service and take an additional backup first.' >&2
printf 'Type RESTORE to proceed: ' >&2
read -r confirmation
[ "$confirmation" = RESTORE ] || exit 1
docker compose exec -T db sh -c 'pg_restore --clean --if-exists --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$1"
