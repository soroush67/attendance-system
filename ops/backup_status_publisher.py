#!/usr/bin/env python3
"""Publish ONLY non-sensitive backup health to a read-only web mount.

Runs as root on the PostgreSQL host, not inside Attendance web. Makes S3
read-only calls through the existing backup worker and does not touch PGDATA.
"""

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


BACKUP_ROOT = Path('/opt/attendance-rustfs-backup')
WORKER_PATH = BACKUP_ROOT / 'backup_worker.py'
PUBLIC_DIR = BACKUP_ROOT / 'public-status'
STATUS_PATH = PUBLIC_DIR / 'status.json'
TIMER = 'attendance-pg-backup.timer'
MAX_MANIFEST_BYTES = 8192


def utcnow():
    return datetime.now(timezone.utc)


def run_systemctl(*args):
    try:
        process = subprocess.run(['systemctl', *args], capture_output=True,
                                 text=True, timeout=10, check=False)
        return process.stdout.strip() if process.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def timer_state():
    active = run_systemctl('is-active', TIMER) == 'active'
    next_run = run_systemctl('show', TIMER, '--property=NextElapseUSecRealtime', '--value')
    if len(next_run) > 90 or any(ord(char) < 32 for char in next_run):
        next_run = ''
    return active, next_run or None


def load_worker():
    if not WORKER_PATH.is_file() or WORKER_PATH.is_symlink():
        raise RuntimeError('expected backup worker missing')
    spec = importlib.util.spec_from_file_location('attendance_installed_backup_worker', WORKER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError('backup worker could not be loaded')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verified_backup(s3, config, worker):
    prefix = config['BACKUP_PREFIX'] + '/'
    result = json.loads(s3.command('s3api', 'list-objects-v2', '--bucket',
                                   config['RUSTFS_BUCKET'], '--prefix', prefix, '--output', 'json'))
    if result.get('IsTruncated'):
        raise RuntimeError('S3 manifest listing is truncated')
    keys = sorted((entry['Key'] for entry in result.get('Contents', [])
                   if isinstance(entry, dict) and isinstance(entry.get('Key'), str)
                   and entry['Key'].startswith(prefix)
                   and worker.COMPLETE_MANIFEST.fullmatch(entry['Key'][len(prefix):])), reverse=True)
    if not keys:
        return None, 0, None
    manifest_key = keys[0]
    manifest_data = s3.command('s3', 'cp',
                               f"s3://{config['RUSTFS_BUCKET']}/{manifest_key}", '-',
                               '--no-progress', '--only-show-errors', timeout=60)
    if len(manifest_data) > MAX_MANIFEST_BYTES:
        raise RuntimeError('S3 manifest too large')
    manifest = json.loads(manifest_data)
    expected_key = manifest_key.removesuffix('.manifest.json')
    if (manifest.get('schema') != 1 or manifest.get('status') != 'verified'
            or manifest.get('object') != expected_key
            or manifest.get('database') != config['PG_DATABASE']
            or not isinstance(manifest.get('bytes'), int) or manifest['bytes'] < 1
            or not re.fullmatch(r'[a-f0-9]{64}', str(manifest.get('sha256', '')))
            or manifest.get('utc') != expected_key[len(prefix):len(prefix) + 16]):
        raise RuntimeError('latest S3 manifest failed consistency validation')
    metadata = json.loads(s3.command('s3api', 'head-object', '--bucket',
                                     config['RUSTFS_BUCKET'], '--key', expected_key))
    if metadata.get('ContentLength') != manifest['bytes']:
        raise RuntimeError('S3 dump size differs from verified manifest')
    completed = datetime.strptime(manifest['utc'], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
    if completed > utcnow():
        raise RuntimeError('backup timestamp is in the future')
    return completed, len(keys), manifest['bytes']


def publish(payload):
    if PUBLIC_DIR.is_symlink() or (STATUS_PATH.exists() and STATUS_PATH.is_symlink()):
        raise RuntimeError('public status path cannot be a symlink')
    PUBLIC_DIR.mkdir(mode=0o755, parents=False, exist_ok=True)
    os.chmod(PUBLIC_DIR, 0o755)
    encoded = (json.dumps(payload, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    descriptor, temp = tempfile.mkstemp(prefix='.backup-status-', dir=str(PUBLIC_DIR))
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, STATUS_PATH)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main():
    if os.geteuid() != 0:
        raise RuntimeError('publisher must run on host as root')
    active, next_run = timer_state()
    payload = {
        'schema': 1, 'state': 'unavailable',
        'checked_at_utc': utcnow().isoformat().replace('+00:00', 'Z'),
        'last_backup_utc': None, 'verified_count': None, 'size_bytes': None,
        'timer_active': active, 'next_run': next_run,
    }
    try:
        worker = load_worker()
        config = worker.read_config()
        s3 = worker.S3(config)
        try:
            s3.bucket_exists()
            completed, count, size = verified_backup(s3, config, worker)
        finally:
            s3.close()
        if completed is None:
            payload['state'] = 'warning'
            payload['verified_count'] = 0
        else:
            payload['last_backup_utc'] = completed.isoformat().replace('+00:00', 'Z')
            payload['verified_count'] = count
            payload['size_bytes'] = size
            age_seconds = (utcnow() - completed).total_seconds()
            payload['state'] = ('stale' if age_seconds > 36 * 3600 else
                                'healthy' if active else 'warning')
    except Exception:
        # Do not leak endpoint URLs, bucket/key names, secret-bearing subprocess
        # errors, or backend exceptions through logs or this JSON file.
        payload['state'] = 'unavailable'
        print('WARN: unable to verify current backup metadata (details suppressed)', file=sys.stderr)
    publish(payload)
    print('Backup status snapshot published (credential-free):', payload['state'])
    # A published degraded state is still a successful refresh operation.
    # Actual monitoring must inspect the state, not rely on systemd exit code.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
