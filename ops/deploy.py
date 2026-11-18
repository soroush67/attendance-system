#!/usr/bin/env python3
"""Guarded web-only v1.8.2 upgrade; production DB and RustFS are not altered.

Run on the production host after extracting ZIP to a STAGING folder:
    python3 <staging>/ops/deploy.py preflight
    python3 <staging>/ops/deploy.py deploy

Writes only the new status publisher/service/timer, a new Compose overlay, and
one Docker image. The existing app source, .env and base Compose stay intact.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, ProxyHandler


BUNDLE = Path(__file__).resolve().parent.parent
PROJECT = Path('/home/devops/containerd/devops/attendance-backend/attendance-system')
BACKUP_ROOT = Path('/opt/attendance-rustfs-backup')
BASE_COMPOSE_SHA = '1c6b1f091ed73e15d7e54275c7a1e409edb6c2446bf82a4ea46550ba08d2310e'
BASE_MAIN_SHA = 'b2ec187d90243a59523e5a836fdfc3d66f5659babb6ea1fd1a744bd4a301b58d'
OLD_IMAGE = 'attendance-web:ui-v1.8.1'
NEW_IMAGE = 'attendance-web:ui-v1.8.2'
WEB, PG, EXPORTER, RUSTFS = 'attendance-web-1', 'postgresql', 'pg-exporter', 'attendance-rustfs'
OVERLAY = PROJECT / 'compose.backup-status.yml'
HEALTH = 'http://192.168.11.10:8080'
SENSITIVE_KEYS = ('DATABASE_URL', 'POSTGRES_HOST', 'POSTGRES_PORT', 'POSTGRES_DB',
                  'POSTGRES_USER', 'POSTGRES_PASSWORD', 'SESSION_SECRET',
                  'ADMIN_USERNAME', 'ADMIN_PASSWORD', 'DEVICE_FORMAT',
                  'DEVICE_TIMEZONE', 'SESSION_HTTPS_ONLY')


def fail(message):
    raise RuntimeError(message)


def run(*args, timeout=60, cwd=PROJECT, allow_failure=False):
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        fail(f'command unavailable or timed out: {args[0]}')
    if not allow_failure and result.returncode != 0:
        # stderr may contain user credentials or interpolated Compose data.
        fail(f'command failed ({result.returncode}): {args[0]}')
    return result


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect(name):
    data = json.loads(run('docker', 'inspect', name, timeout=15).stdout)
    if len(data) != 1:
        fail('Unexpected Docker inspect result')
    return data[0]


def image_id(tag):
    return run('docker', 'image', 'inspect', tag, '--format', '{{.Id}}', timeout=15).stdout.strip()


def environment(info):
    entries = dict(item.split('=', 1) for item in info['Config'].get('Env', []) if '=' in item)
    return {key: entries.get(key) for key in SENSITIVE_KEYS}


def network_names(info):
    return set((info.get('NetworkSettings') or {}).get('Networks') or {})


def ports(info):
    return sorted((entry['HostIp'], str(entry['HostPort']), key)
                  for key, bindings in ((info.get('NetworkSettings') or {}).get('Ports') or {}).items()
                  for entry in bindings or [])


def mounts(info):
    return sorted((m['Type'], m.get('Name') or '', m['Destination'], bool(m['RW']))
                  for m in info.get('Mounts', []))


def healthy(info):
    return info.get('State', {}).get('Running') is True and (
        info.get('State', {}).get('Health') or {}).get('Status') == 'healthy'


def running_without_bad_health(info):
    state = info.get('State', {})
    return state.get('Running') is True and (state.get('Health') or {}).get('Status') != 'unhealthy'


def same_unchanged(now, before):
    return (now['Id'] == before['Id'] and mounts(now) == mounts(before)
            and network_names(now) == network_names(before) and running_without_bad_health(now))


def compose_args(overlay=False):
    binary = 'docker-compose' if shutil.which('docker-compose') else 'docker'
    prefix = (binary,) if binary == 'docker-compose' else ('docker', 'compose')
    return prefix + ('-f', 'compose.app.yml') + (('-f', OVERLAY.name) if overlay else ())


def http_code(path):
    try:
        with build_opener(ProxyHandler({})).open(HEALTH + path, timeout=5) as response:
            return response.status
    except HTTPError as error:
        return error.code
    except (URLError, OSError):
        return -1


def snapshot():
    return {name: inspect(name) for name in (WEB, PG, EXPORTER, RUSTFS)}


def preflight():
    if os.geteuid() != 0:
        fail('Run as root on the intended host')
    if not (PROJECT / '.env').is_file():
        fail('Production .env missing')
    if sha(PROJECT / 'compose.app.yml') != BASE_COMPOSE_SHA:
        fail('Production Compose SHA changed: STOP and review, no overwrite')
    if sha(PROJECT / 'app/main.py') != BASE_MAIN_SHA:
        fail('Production source SHA changed: STOP and review, no overwrite')
    if sha(BUNDLE / 'app/main.py') == BASE_MAIN_SHA:
        fail('Upgrade source is identical to old source')
    if not (BACKUP_ROOT / 'backup_worker.py').is_file() or not (BACKUP_ROOT / 'config.env').is_file():
        fail('Installed backup service is missing')
    state = snapshot()
    if not healthy(state[WEB]) or not healthy(state[PG]):
        fail('Production web/PostgreSQL missing or unhealthy')
    if not running_without_bad_health(state[EXPORTER]) or not running_without_bad_health(state[RUSTFS]):
        fail('Production exporter or RustFS missing or unhealthy')
    if state[WEB]['Image'] != image_id(OLD_IMAGE):
        fail('Live web does not match expected old image ID')
    labels = state[WEB]['Config'].get('Labels') or {}
    if labels.get('com.docker.compose.project') != 'attendance' or labels.get('com.docker.compose.service') != 'web':
        fail('Unexpected production web Compose owner')
    if (state[PG]['Config'].get('Labels') or {}).get('com.docker.compose.project') != 'pg_stack':
        fail('Unexpected production database owner')
    if any(e.startswith('ATTENDANCE_BACKUP_STATUS_FILE=') for e in state[WEB]['Config'].get('Env', [])):
        fail('Unexpected existing backup status integration')
    if not mounts(state[WEB]) or len(mounts(state[WEB])) != 1:
        fail('Unexpected production web mounts; review required')
    if network_names(state[WEB]) != {'db-net'}:
        fail('Unexpected production web network topology')
    if ports(state[WEB]) != [('192.168.11.10', '8080', '8000/tcp')]:
        fail('Unexpected production HTTP port binding')
    if [mounts(state[WEB])[0][0], mounts(state[WEB])[0][2], mounts(state[WEB])[0][3]] != ['volume', '/data/uploads', True]:
        fail('Unexpected production uploads volume')
    run(*compose_args(), 'config', '--quiet', timeout=20)
    if run(*compose_args(), 'config', '--services', timeout=20).stdout.splitlines() != ['web']:
        fail('Unexpected application Compose services')
    if OVERLAY.exists():
        if OVERLAY.is_symlink() or sha(OVERLAY) != sha(BUNDLE / OVERLAY.name):
            fail('Conflicting existing overlay; STOP for review')
    for name in ('attendance-backup-status.service', 'attendance-backup-status.timer'):
        target = Path('/etc/systemd/system') / name
        if target.exists() and sha(target) != sha(BUNDLE / 'ops' / name):
            fail(f'Conflicting systemd unit: {name}')
    for target, source in ((BACKUP_ROOT / 'backup_status_publisher.py', BUNDLE / 'ops/backup_status_publisher.py'),):
        if target.exists() and (target.is_symlink() or sha(target) != sha(source)):
            fail('Conflicting backup status publisher')
    for item in (BACKUP_ROOT / 'public-status', BACKUP_ROOT / 'public-status/status.json'):
        if item.is_symlink():
            fail('Unexpected symlink in status path')
    if http_code('/health') != 200 or http_code('/users') != 401:
        fail('Current application HTTP preflight failed')
    print('PASS preflight: exact app source/Compose, production ownership, health, network, mounts')
    print('PASS preflight: DB/RustFS identities and S3 credentials remain out of web')
    print('No production changes made by preflight')
    return state


def install_file(src, dst, mode):
    if dst.exists():
        if dst.is_symlink() or sha(src) != sha(dst):
            fail('Existing installation file differs; abort')
        return
    descriptor = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(src.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        dst.unlink(missing_ok=True)
        raise
    os.chmod(dst, mode)


def deploy():
    print('Attendance v1.8.2 — guarded web-only release; no DB migration, RustFS writes or data deletion')
    old = preflight()
    print('Will add a root-only status publisher and recreate ONLY attendance-web-1.')
    print('A brief web interruption is possible. To proceed type: DEPLOY')
    if input().strip() != 'DEPLOY':
        fail('Cancelled')

    # An offline Dockerfile overlay prevents pip/registry network dependencies
    # and preserves every byte of the already tested v1.8.1 base image.
    run('docker', 'build', '--pull=false', '--network=none', '-f', str(BUNDLE / 'Dockerfile.upgrade'),
        '-t', NEW_IMAGE, str(BUNDLE), timeout=360, cwd=BUNDLE)
    print('PASS offline upgrade image built:', NEW_IMAGE)

    install_file(BUNDLE / 'ops/backup_status_publisher.py', BACKUP_ROOT / 'backup_status_publisher.py', 0o700)
    for unit in ('attendance-backup-status.service', 'attendance-backup-status.timer'):
        install_file(BUNDLE / 'ops' / unit, Path('/etc/systemd/system') / unit, 0o644)
    run('systemctl', 'daemon-reload', timeout=20)
    run('systemctl', 'enable', '--now', 'attendance-backup-status.timer', timeout=30)
    run('systemctl', 'start', 'attendance-backup-status.service', timeout=120)
    safe_status = json.loads((BACKUP_ROOT / 'public-status/status.json').read_text(encoding='utf-8'))
    if safe_status.get('state') != 'healthy':
        fail('Published backup status is not healthy. Web untouched; investigate status timer')
    print('PASS credential-free status published from verified RustFS manifest')

    install_file(BUNDLE / 'compose.backup-status.yml', OVERLAY, 0o644)
    run(*compose_args(overlay=True), 'config', '--quiet', timeout=20)
    if run(*compose_args(overlay=True), 'config', '--services', timeout=20).stdout.splitlines() != ['web']:
        fail('Merged Compose has unexpected services; web untouched')

    print('Recreating only the Attendance web service ...')
    activated = False
    try:
        activated = True
        run(*compose_args(overlay=True), 'up', '-d', '--no-deps', '--no-build', '--force-recreate', 'web', timeout=120)
        limit = time.monotonic() + 150
        while time.monotonic() < limit:
            now = inspect(WEB)
            if now['Image'] != image_id(NEW_IMAGE):
                fail('Running image does not match new release')
            if environment(now) != environment(old[WEB]):
                fail('Sensitive web environment changed')
            if network_names(now) != network_names(old[WEB]) or ports(now) != ports(old[WEB]):
                fail('Web network or port changed')
            added = ('bind', '', '/run/attendance-backup', False)
            if mounts(now) != sorted(mounts(old[WEB]) + [added]):
                fail('Web mounts changed beyond read-only status directory')
            if healthy(now) and all(http_code(path) == code for path, code in (
                ('/health', 200), ('/login', 200), ('/backups', 401),
                ('/users', 401), ('/openapi.json', 404))):
                if not all(same_unchanged(inspect(name), old[name]) for name in (PG, EXPORTER, RUSTFS)):
                    fail('Production non-web service changed identity: investigate immediately')
                print('SUCCESS: v1.8.2 live; admin-only /backups available')
                print('PASS PostgreSQL, exporter, RustFS unchanged; uploads and sensitive env preserved')
                print('IMPORTANT: Future Compose up commands must include compose.backup-status.yml')
                return
            time.sleep(3)
        fail('Web health or HTTP verification timed out')
    except Exception:
        if activated:
            print('ERROR: activation failed. Restoring original Compose web configuration ...')
            rollback = run(*compose_args(), 'up', '-d', '--no-deps', '--no-build', '--force-recreate', 'web',
                           timeout=120, allow_failure=True)
            if rollback.returncode == 0:
                print('Rollback recreation issued; verify web health manually')
            else:
                print('CRITICAL: rollback command failed; manual response needed')
        raise


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('preflight', 'deploy'):
        print('Usage: python3 ops/deploy.py preflight|deploy', file=sys.stderr)
        return 2
    try:
        if sys.argv[1] == 'preflight':
            preflight()
        else:
            deploy()
        return 0
    except Exception as exc:
        print('STOP:', str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
