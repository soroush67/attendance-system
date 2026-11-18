#!/usr/bin/env python3
"""Guarded Attendance web-only deployment from a DIGEST pinned Nexus image.

This script runs on the ACTUAL application host, not on Jenkins. `preflight`
does not write any state. `deploy` reads registry password from stdin, creates
one CI-only Compose overlay and recreates ONLY web. No DB/backup operations.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener


PROJECT = Path('/home/devops/containerd/devops/attendance-backend/attendance-system')
BASE_SHA256 = '1c6b1f091ed73e15d7e54275c7a1e409edb6c2446bf82a4ea46550ba08d2310e'
BACKUP_OVERLAY_SHA256 = '422144d7a261ddbc3314300c6afd7a6178d964f8db005d8734c8b640f3745eb3'
IMAGE_RE = re.compile(r'^docker-hosted\.hamainsurance\.net/attendance-web@sha256:[a-f0-9]{64}$')
ROLLBACK_IMAGE_RE = re.compile(
    r'^(?:attendance-web:[A-Za-z0-9][A-Za-z0-9_.-]*|'
    r'docker-hosted\.hamainsurance\.net/attendance-web'
    r'(?:@[Ss][Hh][Aa]256:[a-f0-9]{64}|:[A-Za-z0-9][A-Za-z0-9_.-]*))$'
)
REGISTRY = 'docker-hosted.hamainsurance.net'
RELEASE = PROJECT / 'compose.ci-release.yml'
BASE = PROJECT / 'compose.app.yml'
STATUS = PROJECT / 'compose.backup-status.yml'
WEB, DB, EXPORTER, RUSTFS = 'attendance-web-1', 'postgresql', 'pg-exporter', 'attendance-rustfs'
WEB_URL = 'http://192.168.11.10:8080'
ENV_KEYS = ('DATABASE_URL', 'POSTGRES_HOST', 'POSTGRES_PORT', 'POSTGRES_DB',
            'POSTGRES_USER', 'POSTGRES_PASSWORD', 'SESSION_SECRET', 'ADMIN_USERNAME',
            'ADMIN_PASSWORD', 'DEVICE_FORMAT', 'DEVICE_TIMEZONE', 'SESSION_HTTPS_ONLY',
            'ATTENDANCE_BACKUP_STATUS_FILE')


def stop(reason):
    raise RuntimeError(reason)


def command(*argv, timeout=80, cwd=PROJECT, stdin=None, allow_failure=False, env=None):
    try:
        result = subprocess.run(argv, cwd=cwd, input=stdin, capture_output=True,
                                timeout=timeout, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired):
        stop('Tool unavailable/timeout: ' + argv[0])
    if result.returncode and not allow_failure:
        # Never emit stdout/stderr: Compose output can contain secrets.
        stop('Command failed: ' + argv[0] + ' (' + str(result.returncode) + ')')
    return result


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect(name):
    return json.loads(command('docker', 'inspect', name, timeout=20).stdout)[0]


def env_subset(info):
    all_env = dict(item.split('=', 1) for item in info['Config'].get('Env', []) if '=' in item)
    return {key: all_env.get(key) for key in ENV_KEYS}


def mounts(info):
    return sorted((m['Type'], m.get('Name') or '', m['Destination'], bool(m['RW']))
                  for m in info.get('Mounts', []))


def networks(info):
    return sorted(((info.get('NetworkSettings') or {}).get('Networks') or {}).keys())


def ports(info):
    return sorted((entry['HostIp'], entry['HostPort'], dest)
                  for dest, entries in ((info.get('NetworkSettings') or {}).get('Ports') or {}).items()
                  for entry in entries or [])


def health(info):
    return (info.get('State', {}).get('Health') or {}).get('Status', 'none')


def alive(info):
    return info.get('State', {}).get('Running') is True and health(info) != 'unhealthy'


def compose(extra=None):
    if shutil.which('docker-compose'):
        cmd = ['docker-compose']
    else:
        cmd = ['docker', 'compose']
    cmd += ['-f', BASE.name, '-f', STATUS.name]
    if extra:
        cmd += ['-f', str(extra)]
    return cmd


def previous_web_image(info):
    """Fail closed unless the *running* web image can be restored locally.

    The Compose release pointer may be absent or stale. It must never be
    treated as evidence of which image was running before the new release.
    """
    reference = (info.get('Config') or {}).get('Image')
    if not isinstance(reference, str) or not ROLLBACK_IMAGE_RE.fullmatch(reference):
        stop('Running web has no approved rollback image reference')
    expected_id = info.get('Image')
    found = command('docker', 'image', 'inspect', reference, '--format', '{{.Id}}',
                    timeout=15, allow_failure=True)
    if (found.returncode or not expected_id
            or found.stdout.decode().strip() != expected_id):
        stop('Running web image is not locally available for exact rollback')
    return reference


def image_overlay(reference):
    if not ROLLBACK_IMAGE_RE.fullmatch(reference):
        stop('Invalid web image for Compose rollback overlay')
    return ('services:\n  web:\n    image: ' + reference + '\n').encode()


def http_status(path):
    try:
        with build_opener(ProxyHandler({})).open(WEB_URL + path, timeout=5) as resp:
            return resp.status
    except HTTPError as err:
        return err.code
    except (URLError, OSError):
        return -1


def release_schema_preflight():
    """Read-only DB guard: reject web upgrades before the approved migration.

    Run using the CURRENT healthy web image and its existing DB connection.
    Never echo connection strings or a failing command's stderr.
    """
    check = '''from main import engine
from sqlalchemy import inspect, text
with engine.connect() as conn:
    tables = set(inspect(conn).get_table_names())
    assert 'attendance_corrections' in tables, 'missing correction table'
    cols = {col['name'] for col in inspect(conn).get_columns('attendance_corrections')}
    assert {'employee_id', 'local_day', 'action', 'override_first', 'override_last', 'actor_id', 'reason', 'created_at'} <= cols
    inspector = inspect(conn)

    required_columns = {
        'employees': {'attendance_group'},
        'attendance_corrections': {'origin'},
        'pending_imports': {
            'id', 'report_name', 'filename',
            'status', 'uploader_id', 'created_at',
        },
        'pending_import_rows': {
            'id', 'pending_import_id',
            'line_number', 'issue', 'decision', 'raw_line',
        },
        'manager_attendance_requests': {
            'id', 'employee_id', 'local_day',
            'proposed_first', 'proposed_last',
            'requested_by_id', 'expected_revision_id',
            'status', 'resolved_by_id', 'correction_id',
        },
        'login_rate_limits': {
            'key_digest', 'failures', 'window_started_at',
            'blocked_until', 'last_seen_at',
        },
    }

    assert set(required_columns) <= tables, 'R7.1 tables missing'

    for table, needed in required_columns.items():
        actual = {
            column['name']
            for column in inspector.get_columns(table)
        }
        assert needed <= actual, f'{table}: missing columns'

    required_checks = {
        'employees': 'ck_employees_attendance_group',
        'attendance_corrections': 'ck_attendance_corrections_origin',
        'manager_attendance_requests': 'ck_manager_requests_status',
        'login_rate_limits': 'ck_login_rate_limits_failures',
    }

    for table, name in required_checks.items():
        actual_checks = {
            item['name']
            for item in inspector.get_check_constraints(table)
        }
        assert name in actual_checks, f'{table}: missing constraint'

    indexes = {
        item['name']: item
        for item in inspector.get_indexes(
            'manager_attendance_requests'
        )
    }

    unique_index = indexes.get(
        'uq_manager_requests_one_pending'
    )

    assert unique_index, 'approval index missing'
    assert unique_index.get('unique') is True, 'approval index not unique'

    login_indexes = {item['name'] for item in inspector.get_indexes('login_rate_limits')}
    assert 'ix_login_rate_limits_last_seen' in login_indexes, 'login retention index missing'

    assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == '0006_login_throttling'
'''
    checked = command('docker', 'exec', WEB, 'python', '-c', check, timeout=30, allow_failure=True)
    if checked.returncode:
        stop('Migration 0006_login_throttling is not verified; refuse to recreate web. Take a verified backup and migrate separately.')


def preflight(ref):
    if not IMAGE_RE.fullmatch(ref):
        stop('Release image MUST be a pinned digest for approved Nexus repository')
    if not BASE.is_file() or sha256(BASE) != BASE_SHA256:
        stop('Production Compose checksum changed: stop and review')
    if not STATUS.is_file() or sha256(STATUS) != BACKUP_OVERLAY_SHA256:
        stop('Production backup-status overlay checksum changed: stop and review')
    if not (PROJECT / '.env').is_file():
        stop('Production .env missing')
    if RELEASE.is_symlink():
        stop('CI release pointer is a symlink')
    if not (Path('/opt/attendance-rustfs-backup/public-status/status.json').is_file()):
        stop('Backup status mount source missing')
    states = {name: inspect(name) for name in (WEB, DB, EXPORTER, RUSTFS)}
    if not all(alive(item) for item in states.values()) or health(states[WEB]) != 'healthy' or health(states[DB]) != 'healthy':
        stop('A required production service is not healthy/running')
    labels = states[WEB]['Config'].get('Labels') or {}
    if labels.get('com.docker.compose.project') != 'attendance' or labels.get('com.docker.compose.service') != 'web':
        stop('Unexpected Compose ownership of production web')
    if (states[DB]['Config'].get('Labels') or {}).get('com.docker.compose.project') != 'pg_stack':
        stop('Unexpected production PostgreSQL owner')
    if networks(states[WEB]) != ['db-net'] or ports(states[WEB]) != [('192.168.11.10', '8080', '8000/tcp')]:
        stop('Production network or port changed')
    expected_mounts = sorted([('volume', 'attendance_uploads', '/data/uploads', True),
                              ('bind', '', '/run/attendance-backup', False)])
    if mounts(states[WEB]) != expected_mounts:
        stop('Unexpected web mounts; no risk to uploads/backup will be accepted')
    previous_web_image(states[WEB])
    if env_subset(states[WEB])['ATTENDANCE_BACKUP_STATUS_FILE'] != '/run/attendance-backup/status.json':
        stop('Backup status environment missing')
    if http_status('/health') != 200 or http_status('/backups') != 401:
        stop('Production HTTP security/health preflight failed')
    release_schema_preflight()
    command(*compose(), 'config', '--quiet', timeout=20)
    services = command(*compose(), 'config', '--services', timeout=20).stdout.decode().splitlines()
    if services != ['web']:
        stop('Compose has unexpected services')
    print('PASS read-only: production web/db/exporter/rustfs healthy, correct mounts/network/security')
    print('PASS read-only: existing Compose and RustFS overlay are unchanged')
    print('PASS read-only: R7.1 schema and Alembic revision 0006_login_throttling verified')
    return states


def safe_write(path, data):
    descriptor, temp = tempfile.mkstemp(prefix='.attendance-ci-', dir=str(path.parent))
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def verify_after(before, ref, *, rollback=False):
    target = command('docker', 'image', 'inspect', ref, '--format', '{{.Id}}', timeout=15).stdout.decode().strip()
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        now = inspect(WEB)
        if now['Image'] != target:
            stop('Web image ID mismatch')
        if env_subset(now) != env_subset(before[WEB]):
            stop('Sensitive/expected web environment changed')
        if mounts(now) != mounts(before[WEB]) or networks(now) != networks(before[WEB]) or ports(now) != ports(before[WEB]):
            stop('Web mounts, network or port unexpectedly changed')
        security_ok = http_status('/backups') == 401
        if not rollback:
            security_ok = security_ok and http_status('/corrections') == 401
        if health(now) == 'healthy' and http_status('/health') == 200 and security_ok:
            for name in (DB, EXPORTER, RUSTFS):
                current = inspect(name)
                if current['Id'] != before[name]['Id'] or mounts(current) != mounts(before[name]) or not alive(current):
                    stop('Non-web production resource changed during release')
            message = 'PASS rollback smoke' if rollback else 'PASS release smoke'
            print(message + ': web healthy, guest /backups 401; DB/exporter/RustFS unchanged')
            return
        if health(now) == 'unhealthy':
            break
        time.sleep(3)
    stop('Web readiness verification failed')


def deploy(ref, user):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{1,79}', user):
        stop('Invalid registry username')
    before = preflight(ref)
    old_ref = previous_web_image(before[WEB])
    secret = sys.stdin.buffer.readline().rstrip(b'\r\n')
    if not secret or len(secret) > 512:
        stop('No registry credential received over stdin')
    print('Preflight passed. Pulling immutable Nexus image; DB/RustFS will not be touched.')
    with tempfile.TemporaryDirectory(prefix='attendance-docker-credentials-') as config_dir:
        env = os.environ.copy()
        env['DOCKER_CONFIG'] = config_dir
        command('docker', 'login', REGISTRY, '--username', user, '--password-stdin', stdin=secret + b'\n', env=env)
        secret = b''
        command('docker', 'pull', ref, timeout=900, env=env)
        # Remove registry credentials before activation (local image is cached).
        command('docker', 'logout', REGISTRY, timeout=20, env=env, allow_failure=True)
    new_content = ('services:\n  web:\n    image: ' + ref + '\n').encode()
    stage = PROJECT / ('.attendance-ci-candidate-' + str(os.getpid()) + '.yml')
    try:
        safe_write(stage, new_content)
        command(*compose(stage), 'config', '--quiet', timeout=20)
        if command(*compose(stage), 'config', '--services', timeout=20).stdout.decode().splitlines() != ['web']:
            stop('Candidate Compose unexpectedly targets other services')
        safe_write(RELEASE, new_content)
        try:
            command(*compose(RELEASE), 'up', '-d', '--no-deps', '--no-build', '--force-recreate', 'web', timeout=150)
            verify_after(before, ref)
            print('SUCCESS commit-bound image deployed via web-only Compose overlay')
        except Exception:
            print('Release failed: restoring the exact previously running web image')
            try:
                # The old pointer may be absent or stale. Always pin to the
                # image inspected from the healthy running container instead.
                safe_write(RELEASE, image_overlay(old_ref))
                rollback_result = command(*compose(RELEASE), 'up', '-d', '--no-deps',
                                          '--no-build', '--force-recreate', 'web',
                                          timeout=150, allow_failure=True)
                if rollback_result.returncode:
                    print('CRITICAL: rollback recreate failed; manual response required')
                else:
                    try:
                        verify_after(before, old_ref, rollback=True)
                    except Exception:
                        print('CRITICAL: rollback health/identity verification failed; manual response required')
            except Exception:
                print('CRITICAL: could not restore the previous image pointer; manual response required')
            raise
    finally:
        stage.unlink(missing_ok=True)


def main():
    if len(sys.argv) not in (3, 4) or sys.argv[1] not in ('preflight', 'deploy'):
        print('Usage: remote_release.py preflight IMAGE_DIGEST | deploy IMAGE_DIGEST REGISTRY_USERNAME', file=sys.stderr)
        return 2
    if (sys.argv[1] == 'preflight' and len(sys.argv) != 3) or (sys.argv[1] == 'deploy' and len(sys.argv) != 4):
        return 2
    try:
        if sys.argv[1] == 'preflight':
            preflight(sys.argv[2])
        else:
            deploy(sys.argv[2], sys.argv[3])
        return 0
    except Exception as error:
        print('STOP:', str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
