"""One-off and idempotent initial admin creation. Run after Alembic upgrade."""
import os
import sys

from sqlalchemy import select

from main import SessionLocal, User, password_hash


def main():
    username = os.environ.get('ADMIN_USERNAME', 'admin').strip()
    password = os.environ.get('ADMIN_PASSWORD', '')
    if not username or len(password) < 12:
        print('ERROR: ADMIN_USERNAME and a strong ADMIN_PASSWORD (12+ characters) are required.', file=sys.stderr)
        return 2
    with SessionLocal.begin() as db:
        current = db.scalar(select(User.id).where(User.username == username))
        if current is not None:
            print('Admin username already exists; no changes made.')
            return 0
        existing = db.scalar(select(User.id).limit(1))
        if existing is not None:
            print('ERROR: users already exist; manual administrator review required.', file=sys.stderr)
            return 3
        db.add(User(username=username, password_hash=password_hash(password), role='admin', active=True))
    print('Initial administrator created successfully.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
