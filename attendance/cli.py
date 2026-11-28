"""One-off admin processes (Twelve-Factor XII), run in the release image:

  python -m attendance.cli bootstrap-admin              # first admin from ADMIN_USERNAME/ADMIN_PASSWORD, only if no user exists
  python -m attendance.cli reset-password <username>    # new password from RESET_PASSWORD env, unlocks, revokes sessions
  python -m attendance.cli import-employees FILE.xlsx --dry-run|--apply
"""
import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from attendance.db import session_factory
from attendance.models import Audit, User
from attendance.security import hash_password, password_problem


def bootstrap_admin() -> int:
    username = os.environ.get('ADMIN_USERNAME', 'admin').strip()
    password = os.environ.get('ADMIN_PASSWORD', '')
    with session_factory()() as db:
        if db.scalar(select(User.id).limit(1)):
            print('users already exist: bootstrap skipped')
            return 0
        problem = password_problem(password, username)
        if problem:
            print(f'ERROR: ADMIN_PASSWORD rejected ({problem})', file=sys.stderr)
            return 1
        db.add(User(username=username, password_hash=hash_password(password), role='admin', active=True,
                    must_change_password=True, password_changed_at=datetime.now(timezone.utc)))
        db.add(Audit(actor='system', event='admin_bootstrap', details=username))
        db.commit()
        print(f'admin user {username!r} created; the password must be changed at first login')
    return 0


def reset_password(username: str) -> int:
    password = os.environ.get('RESET_PASSWORD', '')
    with session_factory()() as db:
        user = db.scalar(select(User).where(User.username == username))
        if not user:
            print('ERROR: user not found', file=sys.stderr)
            return 1
        problem = password_problem(password, username)
        if problem:
            print(f'ERROR: RESET_PASSWORD rejected ({problem})', file=sys.stderr)
            return 1
        user.password_hash = hash_password(password)
        user.failed_logins, user.locked_until, user.active = 0, None, True
        user.must_change_password = True
        user.session_version += 1
        db.add(Audit(actor='system', event='password_reset_cli', details=username))
        db.commit()
        print(f'password of {username!r} reset; it must be changed at next login')
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='attendance.cli')
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('bootstrap-admin')
    r = sub.add_parser('reset-password')
    r.add_argument('username')
    imp = sub.add_parser('import-employees')
    imp.add_argument('xlsx', type=Path)
    mode = imp.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    a = parser.parse_args(argv)
    if a.cmd == 'bootstrap-admin':
        return bootstrap_admin()
    if a.cmd == 'reset-password':
        return reset_password(a.username)
    from attendance.services.roster import run
    try:
        return run(a.xlsx, a.apply)
    except (ValueError, OSError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
