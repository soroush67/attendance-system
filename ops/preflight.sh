#!/usr/bin/env bash
set -euo pipefail

# Read-only preflight: never create/remove Docker networks, volumes or containers.
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

[[ -s .env ]] || { echo 'ERROR: Existing .env is missing' >&2; exit 1; }
[[ -s compose.app.yml ]] || { echo 'ERROR: App compose file missing' >&2; exit 1; }
command -v docker >/dev/null || { echo 'ERROR: docker command missing' >&2; exit 1; }
if docker compose version >/dev/null 2>&1; then
  compose=(docker compose)
elif command -v docker-compose >/dev/null; then
  compose=(docker-compose)
else
  echo 'ERROR: Docker Compose missing' >&2
  exit 1
fi

# The database is on its own PostgreSQL server. Never require DB/backups on the app host.
docker volume inspect attendance_uploads >/dev/null || { echo 'ERROR: app uploads volume not found' >&2; exit 1; }
docker network inspect attendance_internal >/dev/null || { echo 'ERROR: attendance_internal network not found' >&2; exit 1; }

"${compose[@]}" -f compose.app.yml config -q
echo 'PASS: existing uploads volume, app network, .env and app Compose are present.'
