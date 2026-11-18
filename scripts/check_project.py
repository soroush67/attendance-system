#!/usr/bin/env python3
"""Offline source-only release guard (no Docker, database or network calls)."""

import hashlib
from pathlib import Path
import re
import sys


def run(project):
    project = Path(project).resolve()
    must_exist = (
        'Jenkinsfile', 'app/main.py', 'app/backup_status.py', 'app/Dockerfile',
        'app/requirements.txt', 'app/alembic.ini',
        'compose.app.yml', 'compose.database.yml', 'compose.backup-status.yml',
        'ci/remote_release.py', 'ci/digest_ref.py',
        'tests/test_backup_status.py', 'tests/test_ci_release.py', '.gitignore',
        'app/migrations/versions/0006_login_throttling.py',
        'app/prune_login_throttle.py', 'tests/test_login_throttling.py',
    )
    missing = [f for f in must_exist if not (project / f).is_file()]
    if missing:
        raise ValueError('Required source files missing: ' + ', '.join(missing))
    forbidden = ('.env', 'app/certs/company-root-ca.crt', 'data/initial-import/personal_information.xlsx')
    exposed = [name for name in forbidden if (project / name).exists()]
    if exposed:
        raise ValueError('Sensitive production files present in release tree (names only): ' + ', '.join(exposed))
    for pattern in ('*.dump', '*.key', '*.p12', '*.pfx'):
        if any(project.rglob(pattern)):
            raise ValueError('Forbidden credential/backup extension in source')
    ci = (project / 'Jenkinsfile').read_text(encoding='utf-8')
    if not all(s in ci for s in ('DEPLOY_PRODUCTION', 'Manual Production Approval',
                                'compose.backup-status.yml', 'release-ref.txt')):
        raise ValueError('CI manual release safety guard missing')
    if '/opt/delshad/attendance-system' in ci or '192.168.120.128' in ci:
        raise ValueError('Old production deployment destination detected')
    image_script = (project / 'ci/remote_release.py').read_text(encoding='utf-8')
    if not re.search(r"IMAGE_RE\s*=", image_script) or 'compose.ci-release.yml' not in image_script:
        raise ValueError('Immutable digest deployment safeguards missing')
    main = (project / 'app/main.py').read_text(encoding='utf-8')
    if "permit(request, db, 'admin')" not in main or "@app.get('/backups')" not in main:
        raise ValueError('Admin-only backup page guard missing')
    if not all(marker in main for marker in ('class LoginRateLimit', 'record_login_failure',
                                            'login_lock_remaining', 'Retry-After')):
        raise ValueError('Login throttling guards missing')
    if '0006_login_throttling' not in image_script or "'login_rate_limits'" not in image_script:
        raise ValueError('Login migration is not protected by the immutable release preflight')
    print('PASS source-only safety: required files, no sensitive artifacts, admin and CI manual gates')
    print('INFO app/main.py SHA256:', hashlib.sha256((project / 'app/main.py').read_bytes()).hexdigest())


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise ValueError('Usage: check_project.py PROJECT_ROOT')
        run(sys.argv[1])
    except ValueError as error:
        print('FAIL', error, file=sys.stderr)
        raise SystemExit(1)
