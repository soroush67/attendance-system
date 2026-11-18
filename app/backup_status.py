"""Read an allowlisted, credential-free backup status snapshot.

The web app never imports the host backup worker, contacts S3, or sees AWS
credentials. A root-owned host publisher prepares this small JSON file.
"""

import json
import os
from pathlib import Path
import stat
from datetime import datetime, timezone


MAX_STATUS_BYTES = 8192
DEFAULT_FILE = '/run/attendance-backup/status.json'


def _parse_utc(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def read_public_backup_status(path=None, now=None):
    """Return a controlled data shape; failure is UNAVAILABLE, never fatal."""
    empty = {
        'state': 'unavailable', 'last_backup_at': None, 'checked_at': None,
        'size_bytes': None, 'verified_count': None, 'next_run': None,
        'timer_active': False,
    }
    selected = path or os.environ.get('ATTENDANCE_BACKUP_STATUS_FILE', DEFAULT_FILE)
    try:
        target = Path(selected)
        if not target.is_absolute():
            return empty
        details = target.stat()
        if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_STATUS_BYTES:
            return empty
        with target.open('r', encoding='utf-8') as stream:
            content = stream.read(MAX_STATUS_BYTES + 1)
        if len(content.encode('utf-8')) > MAX_STATUS_BYTES:
            return empty
        data = json.loads(content)
        if not isinstance(data, dict) or data.get('schema') != 1:
            return empty
        checked = _parse_utc(data.get('checked_at_utc'))
        last = _parse_utc(data.get('last_backup_utc'))
        current = now or datetime.now(timezone.utc)
        if not checked or current.tzinfo is None:
            return empty
        age = (current.astimezone(timezone.utc) - checked).total_seconds()
        if age < -300 or age > 3600:
            return empty  # Stale/future publisher output must not imply health.
        state = data.get('state')
        if state not in {'healthy', 'stale', 'warning', 'unavailable'}:
            state = 'unavailable'
        size = data.get('size_bytes')
        if type(size) is not int or not 0 <= size <= 10**14:
            size = None
        count = data.get('verified_count')
        if type(count) is not int or not 0 <= count <= 365:
            count = None
        next_run = data.get('next_run')
        if not isinstance(next_run, str) or len(next_run) > 90 or any(ord(ch) < 32 for ch in next_run):
            next_run = None
        timer_active = data.get('timer_active') is True
        if state == 'healthy' and (not timer_active or not last):
            state = 'warning'
        return {
            'state': state,
            'last_backup_at': last,
            'checked_at': checked,
            'size_bytes': size,
            'verified_count': count,
            'next_run': next_run,
            'timer_active': timer_active,
        }
    except (OSError, ValueError, UnicodeError, TypeError):
        return empty
