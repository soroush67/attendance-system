# Attendance v1.6 — security hotfix for the already-running v1.5 application

Scope: backend HTTP hardening only. **No SQL migration, employee import or PostgreSQL restart is necessary.**

## Corrections

- Disable `/openapi.json` as well as `/docs` and `/redoc`.
- Respond to `/robots.txt` with `User-agent: *` and `Disallow: /`.
- Set `X-Robots-Tag: noindex, nofollow, noarchive, nosnippet`, `Cache-Control: private, no-store`, `Pragma: no-cache`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and `Permissions-Policy`.
- Set a Content Security Policy with a fresh nonce for the two inline theme scripts; retain bundled styles and embedded company logo without third-party network requests.
- Emit HSTS on HTTPS responses only, not on development HTTP.
- Add `<meta name="robots" ...>` to all HTML pages.
- Add regression tests in `tests/test_security.py`.

`robots.txt` and robots headers are **not** controls against malicious crawlers. Authentication, narrow network access, authorization, rate limiting and detection are also required.

## Safe update on `utl-18-atend`

**Never unzip this archive over the live directory.** The server already contains a `.env` and a host-specific `compose.app.yml`; preserve them unchanged. The archive is a full source snapshot for Git/versioning but the hotfix only needs `app/main.py` and the new test file.

1. Unzip the full archive into an **empty, separate directory** (for example `/tmp/attendance-v1.6-preview`). Inspect the extracted paths first.
2. In the running project's directory, back up `app/main.py` to a uniquely named file outside the Docker build context. Copy only the new `app/main.py` and `tests/test_security.py` from the extracted source. Do not copy the old Compose, `.env`, backup scripts or database configuration into the live directory.
3. Build a new, distinct image tag (`attendance-web:security-v1.6`) from the existing `app/Dockerfile` and test in a separate container on loopback port `18080` before switching the running service.

```bash
docker build --file app/Dockerfile --tag attendance-web:security-v1.6 app

docker run --rm --network none --entrypoint python \
  -v "$PWD/tests:/tests:ro" \
  attendance-web:security-v1.6 \
  -m unittest discover -s /tests -p 'test_*.py' -v

docker run -d --rm --name attendance-v16-smoke \
  --network db-net --env-file .env \
  -p 127.0.0.1:18080:8000 \
  attendance-web:security-v1.6

curl -fsS http://127.0.0.1:18080/health
for path in /login /employees /reports /openapi.json /docs /robots.txt; do
  printf '%-16s ' "$path"
  curl -sS -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:18080$path"
done
curl -sS -D - -o /dev/null http://127.0.0.1:18080/login

docker stop attendance-v16-smoke
```

Expected: `/login` 200, `/employees` and `/reports` 401, `/openapi.json` and `/docs` 404, `/robots.txt` 200, private/noindex/security headers present. **Test runner and smoke results must be recorded from the server; they were not executed against this production host during packaging.**

4. Once the smoke checks pass, edit the existing `compose.app.yml` image to `attendance-web:security-v1.6`; preserve its `db-net` network and its `192.168.11.10` binding for now. Run `docker-compose -f compose.app.yml config -q` and `docker-compose -f compose.app.yml up -d --no-build --no-deps web`, then repeat the endpoint tests on port 8080. The PostgreSQL container is not part of this operation.

## Production blockers still open

- Direct HTTP login on `192.168.11.10:8080` must be replaced by HTTPS (or restrict bind to loopback and access via SSH tunnel) before users enter credentials.
- Access to the exposed `5432` PostgreSQL and `9187` exporter ports must be restricted by firewall/ingress policy.
- Test login throttling, export rate limits, authorization, PII-safe audit logs, backups/restores, monitoring, dependency/image scanning, responsive UI and rollback before any production-ready claim.
- The repository's Jenkinsfile has an outdated/unverified deploy address/path; update and verify SSH credentials and protected deployment workflow before enabling CI deployments.
- Confirm that the first field in device `.dat` files matches the employee `device_id` imported from the Excel sheet. Otherwise, add an explicit mapping before importing punches.

Release status: **security hotfix candidate, NOT PRODUCTION-READY** until the mandatory gates are demonstrated.
