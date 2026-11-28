"""Runtime configuration, read once from the environment (Twelve-Factor III).

Nothing here has a secret default: the app refuses to start when a required
value is missing or too weak, instead of running with something guessable.
"""
import os
from dataclasses import dataclass
from functools import lru_cache
from zoneinfo import ZoneInfo


class ConfigError(RuntimeError):
    pass


def _str(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == '':
        raise ConfigError(f'environment variable {name} is required')
    return value


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f'{name} must be an integer, got {raw!r}') from None
    if value < minimum:
        raise ConfigError(f'{name} must be >= {minimum}')
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, 'true' if default else 'false').strip().lower()
    if raw not in ('true', 'false', '1', '0', 'yes', 'no'):
        raise ConfigError(f'{name} must be true or false, got {raw!r}')
    return raw in ('true', '1', 'yes')


@dataclass(frozen=True)
class Settings:
    database_url: str
    session_secret: str
    session_https_only: bool
    session_max_age_minutes: int
    session_idle_minutes: int
    login_max_failures: int
    login_lock_minutes: int
    login_ip_max_failures: int
    login_ip_window_minutes: int
    password_min_length: int
    timezone: ZoneInfo
    upload_max_bytes: int
    report_max_days: int
    report_max_rows: int
    db_pool_size: int
    db_max_overflow: int
    app_version: str
    environment: str

    @property
    def hsts(self) -> bool:
        return self.session_https_only


@lru_cache
def get_settings() -> Settings:
    secret = _str('SESSION_SECRET')
    if len(secret) < 32:
        raise ConfigError('SESSION_SECRET must be at least 32 characters')
    try:
        tz = ZoneInfo(os.environ.get('DEVICE_TIMEZONE', 'Asia/Tehran'))
    except Exception:
        raise ConfigError('DEVICE_TIMEZONE is not a valid IANA time zone') from None
    return Settings(
        database_url=_str('DATABASE_URL'),
        session_secret=secret,
        session_https_only=_bool('SESSION_HTTPS_ONLY', False),
        session_max_age_minutes=_int('SESSION_MAX_AGE_MINUTES', 480, 5),
        session_idle_minutes=_int('SESSION_IDLE_MINUTES', 30, 1),
        login_max_failures=_int('LOGIN_MAX_FAILURES', 5, 1),
        login_lock_minutes=_int('LOGIN_LOCK_MINUTES', 15, 1),
        login_ip_max_failures=_int('LOGIN_IP_MAX_FAILURES', 20, 1),
        login_ip_window_minutes=_int('LOGIN_IP_WINDOW_MINUTES', 15, 1),
        password_min_length=_int('PASSWORD_MIN_LENGTH', 12, 8),
        timezone=tz,
        upload_max_bytes=_int('UPLOAD_MAX_MB', 10, 1) * 1024 * 1024,
        report_max_days=_int('REPORT_MAX_DAYS', 3660, 1),
        report_max_rows=_int('REPORT_MAX_ROWS', 100000, 100),
        db_pool_size=_int('DB_POOL_SIZE', 5, 1),
        db_max_overflow=_int('DB_MAX_OVERFLOW', 5, 0),
        app_version=os.environ.get('APP_VERSION', 'dev'),
        environment=os.environ.get('APP_ENV', 'production'),
    )
