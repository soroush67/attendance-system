"""Passwords, login throttling and session rules.

- Passwords: argon2id. v1.3 scrypt hashes still verify and are re-hashed to
  argon2id on the next successful login.
- Brute force: per-account lockout after LOGIN_MAX_FAILURES and a per-IP limit
  of LOGIN_IP_MAX_FAILURES failures per window, both kept in PostgreSQL so they
  hold across workers and restarts. Unknown users cost the same hashing time.
- Sessions: signed HttpOnly SameSite=Strict cookie with an absolute lifetime,
  an idle timeout, and a per-user session_version that revokes every session
  of a user on password change, deactivation or "sign out everywhere".
"""
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from attendance.config import get_settings
from attendance.models import LoginAttempt, User

_hasher = PasswordHasher()  # argon2id, library defaults (RFC 9106 low-memory profile)
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))
COMMON_PASSWORDS = {'password', 'password123', '123456789012', 'qwertyuiop12', 'admin1234567', '1234567890ab'}


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def _verify_scrypt(password: str, stored: str) -> bool:
    try:
        _, salt, expected = stored.split('$')
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def verify_password(password: str, stored: str | None) -> tuple[bool, bool]:
    """-> (valid, needs_rehash). Always spends hashing time, even without a user."""
    if not stored:
        try:
            _hasher.verify(_DUMMY_HASH, password)
        except VerificationError:
            pass
        return False, False
    if stored.startswith('scrypt$'):
        return _verify_scrypt(password, stored), True
    try:
        _hasher.verify(stored, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, False
    return True, _hasher.check_needs_rehash(stored)


def password_problem(password: str, username: str) -> str | None:
    """Persian error message, or None when the password is acceptable."""
    minimum = get_settings().password_min_length
    if len(password) < minimum:
        return f'گذرواژه باید حداقل {minimum} نویسه باشد'
    if len(password) > 256:
        return 'گذرواژه بیش از حد طولانی است'
    if username and username.lower() in password.lower():
        return 'گذرواژه نباید شامل نام کاربری باشد'
    if password.lower() in COMMON_PASSWORDS or len(set(password)) < 5:
        return 'این گذرواژه بیش از حد ساده است'
    return None


# ---------------------------------------------------------------- throttling

def client_ip(request: Request) -> str:
    # uvicorn --proxy-headers only rewrites this for FORWARDED_ALLOW_IPS
    return (request.client.host if request.client else 'unknown')[:64]


def ip_blocked(db: Session, ip: str) -> bool:
    s = get_settings()
    since = datetime.now(timezone.utc) - timedelta(minutes=s.login_ip_window_minutes)
    failures = db.scalar(select(func.count()).select_from(LoginAttempt).where(
        LoginAttempt.ip == ip, LoginAttempt.success.is_(False), LoginAttempt.created_at >= since))
    return (failures or 0) >= s.login_ip_max_failures


def is_locked(user: User) -> bool:
    return bool(user.locked_until and user.locked_until > datetime.now(timezone.utc))


def record_failure(db: Session, user: User | None, username: str, ip: str) -> None:
    db.add(LoginAttempt(username=username[:80], ip=ip, success=False))
    if user:
        s = get_settings()
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= s.login_max_failures:
            user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=s.login_lock_minutes)
            user.failed_logins = 0


def record_success(db: Session, user: User, ip: str) -> None:
    db.add(LoginAttempt(username=user.username, ip=ip, success=True))
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = datetime.now(timezone.utc)


# ---------------------------------------------------------------- sessions

def start_session(request: Request, user: User) -> None:
    request.session.clear()  # new identity, new session: no fixation
    now = int(time.time())
    request.session.update(uid=user.id, sv=user.session_version, iat=now, seen=now,
                           csrf=secrets.token_urlsafe(32))


def session_user(request: Request, db: Session) -> User | None:
    """The signed-in user, or None (and the session is cleared) when the session
    is missing, expired, idle too long, revoked, or the user was deactivated."""
    data = request.session
    uid = data.get('uid')
    if not isinstance(uid, int):
        return None
    s = get_settings()
    now = int(time.time())
    if now - int(data.get('iat', 0)) > s.session_max_age_minutes * 60 or \
            now - int(data.get('seen', 0)) > s.session_idle_minutes * 60:
        data.clear()
        return None
    user = db.get(User, uid)
    if not user or not user.active or data.get('sv') != user.session_version:
        data.clear()
        return None
    data['seen'] = now
    return user


def csrf_token(request: Request) -> str:
    if 'csrf' not in request.session:
        request.session['csrf'] = secrets.token_urlsafe(32)
    return request.session['csrf']


def csrf_valid(request: Request, token: str | None) -> bool:
    expected = request.session.get('csrf')
    return bool(token and expected and hmac.compare_digest(str(expected), str(token)))
