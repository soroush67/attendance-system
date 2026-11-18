"""One-off retention task for expired, anonymized login counters.

Run only against an explicitly approved DATABASE_URL after its backup:
    python prune_login_throttle.py --dry-run
    python prune_login_throttle.py --apply
Do not run as part of app startup, request handling or database migrations.
"""

import argparse
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, or_, select

from main import LoginRateLimit, SessionLocal, positive_setting


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--dry-run', action='store_true')
    group.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    days = positive_setting('LOGIN_COUNTER_RETENTION_DAYS', 7, 365)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    stale = and_(
        LoginRateLimit.last_seen_at < cutoff,
        or_(LoginRateLimit.blocked_until.is_(None), LoginRateLimit.blocked_until < now),
    )
    with SessionLocal.begin() as db:
        if args.dry_run:
            count = db.scalar(select(func.count()).select_from(LoginRateLimit).where(stale))
            print(f'DRY_RUN: {count} expired login counters eligible for removal')
        else:
            count = db.execute(delete(LoginRateLimit).where(stale)).rowcount
            print(f'APPLIED: removed {count} expired login counters')


if __name__ == '__main__':
    main()
