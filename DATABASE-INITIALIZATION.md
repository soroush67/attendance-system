# PostgreSQL 18 — independent attendance database

## Confirmed on 2026-11-14 (user-supplied evidence)

- PostgreSQL container `postgresql` on `utl-18-atend`, image `postgres:18.4-alpine3.22`, healthy.
- Persistent bind mount `/data/pg_stack/data => /var/lib/postgresql`, PGDATA `/var/lib/postgresql/18/docker`.
- `attendance` database owned by **non-superuser** `attendance_app`.
- `attendance_app` logged in and has `CREATE` privilege on schema `public`.
- Application schema has not yet been migrated or verified. No user password/IP is recorded here.

## Important boundaries

Never deploy the repository's **legacy** `docker-compose.yml` or `compose.database.yml` to the new PostgreSQL server. The PostgreSQL 18 instance is managed separately by its own `pg_stack` Compose project. App deploy uses **only** `compose.app.yml` on its separate app host; app host does not need DB/backups Docker volumes. Preserve PostgreSQL's existing bind mount and backups.

## Prerequisites before any write

1. Confirm new `attendance` database has no user tables:

   `docker exec postgresql sh -c 'psql -U "$POSTGRES_USER" -d attendance -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema = '\''public'\'' AND table_type = '\''BASE TABLE'\'';"'`

2. Confirm a **restorable** backup exists, even if initially empty; retain password outside source control.
3. Confirm DNS/IP/port access between future app host and PostgreSQL 18. Restrict TCP 5432 to allowed app host; configure `pg_hba.conf` and firewall, ideally with TLS. No wide-open PostgreSQL Internet exposure.
4. Configure application secrets on the app host in `.env` (never commit or overwrite this file):

   ```text
   POSTGRES_HOST=<INTERNAL_DB_IP_OR_DNS>
   POSTGRES_PORT=5432
   POSTGRES_DB=attendance
   POSTGRES_USER=attendance_app
   POSTGRES_PASSWORD=<EXISTING_PASSWORD_FROM_SECURE_STORE>
   SESSION_SECRET=<LONG_UNIQUE_VALUE>
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD=<LONG_UNIQUE_INITIAL_PASSWORD>
   ```

   Or use `DATABASE_URL=postgresql+psycopg://attendance_app:<URL_ENCODED_PASSWORD>@<DB_HOST>:5432/attendance`. URL-encode special characters in password. For production set `SESSION_HTTPS_ONLY=true` behind HTTPS.

## Schema and admin operations (after image built and reviewed)

Use the **same app image** and runtime configuration as the planned release, with no `web` startup. From the app host, in the deploy directory, with `compose.app.yml`, correct `.env`, external network and app upload volume pre-created:

```bash
docker compose -f compose.app.yml run --rm --no-deps web python -m alembic upgrade head
docker compose -f compose.app.yml run --rm --no-deps web python -m alembic current
docker compose -f compose.app.yml run --rm --no-deps web python bootstrap_admin.py
```

This is an explicit, **authorized one-off operation**, never a Jenkins web deployment side effect. Do not run the first baseline on a database containing legacy tables: evaluate `alembic stamp` and migration compatibility separately, never blindly recreate data. This baseline intentionally refuses destructive downgrade.

Then verify from the database container:

```bash
docker exec postgresql sh -c 'psql -U "$POSTGRES_USER" -d attendance -c "SELECT version_num FROM alembic_version;"'
docker exec postgresql sh -c 'psql -U "$POSTGRES_USER" -d attendance -c "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = '\''public'\'' ORDER BY tablename;"'
```

Expected nine domain tables plus `alembic_version`, migration revision `0001_initial`. A newly initialized database has **one admin user** and zero employees until import.

## Initial employee import (after source Excel is verified)

The Excel needs a **single sheet**, with first four headers exactly `ردیف`, `نام`, `نام خانوادگی`, `کد ملی`. National code becomes `device_id` unchanged. Inspect counts first; never log sensitive rows.

```bash
# Replace /ABS/PATH with actual path on the APP host; read-only mount.
docker compose -f compose.app.yml run --rm --no-deps \
  -v /ABS/PATH/personal_information.xlsx:/imports/employees.xlsx:ro \
  web python import_employees.py /imports/employees.xlsx --dry-run

# After human review of dry-run counts and explicit approval:
docker compose -f compose.app.yml run --rm --no-deps \
  -v /ABS/PATH/personal_information.xlsx:/imports/employees.xlsx:ro \
  web python import_employees.py /imports/employees.xlsx --apply
```

Verify row counts without printing national codes/names:

```bash
docker exec postgresql sh -c 'psql -U "$POSTGRES_USER" -d attendance -c "SELECT count(*) AS employees FROM public.employees; SELECT count(*) AS users FROM public.users;"'
```

## CI/CD gate

Application-only Jenkins pipeline starts **after** schema/admin/data readiness is verified; it must never run `compose.database.yml`, destroy DB volumes or silently mutate `.env`. Verify deployment destination/SSH credentials against actual app host (older Jenkinsfile contains an unconfirmed address). Test readiness and rollback before production.
