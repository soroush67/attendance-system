#!/bin/sh
set -eu

# Run from /opt/delshad/attendance-system.
# This installer NEVER overwrites existing configuration, CA, roster or volumes.
target="$(pwd)"
source_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if [ "$target" = "$source_dir" ]; then
  echo 'ERROR: Extract the update into a temporary folder and run from the deployed project.' >&2
  exit 1
fi
if [ ! -f "$target/docker-compose.yml" ] || [ ! -f "$target/app/Dockerfile" ] || [ ! -f "$target/.env" ]; then
  echo 'ERROR: Not in the existing attendance-system project (Compose/Dockerfile/.env missing).' >&2
  exit 1
fi
if [ ! -f "$target/app/certs/company-root-ca.crt" ]; then
  echo 'ERROR: Existing organizational CA missing; stopping without changes.' >&2
  exit 1
fi
if [ ! -f "$source_dir/app/main.py" ] || [ ! -f "$source_dir/app/import_employees.py" ]; then
  echo 'ERROR: New application source missing.' >&2
  exit 1
fi
if ! command -v docker-compose >/dev/null 2>&1; then
  echo 'ERROR: docker-compose is not available.' >&2
  exit 1
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
if [ -f "$target/app/main.py" ]; then
  cp -p "$target/app/main.py" "$target/app/main.py.before-v1.3.$stamp"
fi
if [ -f "$target/app/import_employees.py" ]; then
  cp -p "$target/app/import_employees.py" "$target/app/import_employees.py.before-v1.3.$stamp"
fi

cp "$source_dir/app/main.py" "$target/app/main.py"
cp "$source_dir/app/import_employees.py" "$target/app/import_employees.py"

echo 'Only application Python code changed. .env, Compose, Dockerfile, certificates, Excel and DB were preserved.'
docker-compose build web
docker-compose up -d --no-deps --force-recreate web
docker-compose ps
echo 'Finished. If build failed, restore app/main.py.before-v1.3.* and rebuild web.'
