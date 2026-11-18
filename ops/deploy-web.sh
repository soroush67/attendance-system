#!/usr/bin/env bash
set -euo pipefail

# Usage: deploy-web.sh DEPLOY_PATH IMAGE REGISTRY REGISTRY_USER
# Registry password is read from stdin, never from argv or a tracked file.
[[ "$#" -eq 4 ]] || { echo 'ERROR: invalid deploy arguments' >&2; exit 2; }
DEPLOY_PATH="$1"
IMAGE="$2"
REGISTRY="$3"
REGISTRY_USER="$4"

[[ "$IMAGE" == "$REGISTRY/"*:* ]] || { echo 'ERROR: invalid release image' >&2; exit 1; }
IFS= read -r REGISTRY_PASSWORD || [[ -n "${REGISTRY_PASSWORD:-}" ]]
[[ -n "${REGISTRY_PASSWORD:-}" ]] || { echo 'ERROR: missing registry password' >&2; exit 1; }

cd "$DEPLOY_PATH"
test -s .env
test -s compose.app.yml
test -s compose.release.yml.new

# Data/backup storage belongs to the separately managed PostgreSQL server.
docker volume inspect attendance_uploads >/dev/null
docker network inspect attendance_internal >/dev/null

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose -f compose.app.yml -f compose.release.yml)
  VALIDATE_COMPOSE=(docker compose)
else
  COMPOSE=(docker-compose -f compose.app.yml -f compose.release.yml)
  VALIDATE_COMPOSE=(docker-compose)
fi
OLD_OVERLAY=0
if [[ -s compose.release.yml ]]; then
  cp -p compose.release.yml compose.release.yml.rollback
  OLD_OVERLAY=1
fi

DOCKER_CONFIG="$(mktemp -d)"
export DOCKER_CONFIG
cleanup() {
  docker logout "$REGISTRY" >/dev/null 2>&1 || true
  rm -f "$DOCKER_CONFIG/config.json"
  rmdir "$DOCKER_CONFIG" 2>/dev/null || true
}
trap cleanup EXIT

printf '%s' "$REGISTRY_PASSWORD" | docker login "$REGISTRY" --username "$REGISTRY_USER" --password-stdin >/dev/null
unset REGISTRY_PASSWORD

# First validate both Compose files before switching the active release pointer.
"${VALIDATE_COMPOSE[@]}" -f compose.app.yml -f compose.release.yml.new config -q
docker pull "$IMAGE"

mv compose.release.yml.new compose.release.yml
"${COMPOSE[@]}" config -q
"${COMPOSE[@]}" up -d --no-deps --no-build --force-recreate web

healthy=0
for attempt in $(seq 1 24); do
  cid="$("${COMPOSE[@]}" ps -q web)"
  [[ -n "$cid" ]] || break
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid")"
  echo "Health check $attempt/24: $status"
  if [[ "$status" == healthy ]]; then healthy=1; break; fi
  if [[ "$status" == unhealthy || "$status" == exited || "$status" == dead ]]; then break; fi
  sleep 5
done
if [[ "$healthy" -eq 1 ]]; then
  echo "DEPLOY SUCCESS: $IMAGE"
  exit 0
fi

echo 'ERROR: New web image is not healthy. Attempting rollback.' >&2
"${COMPOSE[@]}" logs --tail=100 web || true
if [[ "$OLD_OVERLAY" -eq 1 ]]; then
  mv compose.release.yml.rollback compose.release.yml
  "${COMPOSE[@]}" up -d --no-deps --no-build --force-recreate web || true
  echo 'Previous release configuration restored. Verify the web container.' >&2
else
  echo 'No previous release overlay: automatic rollback unavailable.' >&2
fi
exit 1
