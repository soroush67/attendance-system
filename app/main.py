import csv
import hashlib
import hmac
import io
import math
import os
import re
import secrets
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import jdatetime
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from backup_status import read_public_backup_status
from manager_excel import MAX_ROWS, MAX_XLSX_BYTES, ManagerExcelRow, ManagerExcelValidationError, parse_manager_excel
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, LargeBinary, String, UniqueConstraint, create_engine, func, select, text as sql_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.middleware.sessions import SessionMiddleware


def env(name, default=None):
    return os.environ.get(name, default)


DATABASE_URL = env('DATABASE_URL') or "postgresql+psycopg://{user}:{password}@{host}:{port}/{db}".format(
    user=__import__('urllib.parse', fromlist=['quote']).quote(env('POSTGRES_USER', 'attendance'), safe=''),
    password=__import__('urllib.parse', fromlist=['quote']).quote(env('POSTGRES_PASSWORD', ''), safe=''),
    host=env('POSTGRES_HOST', 'db'),
    port=env('POSTGRES_PORT', '5432'),
    db=env('POSTGRES_DB', 'attendance'),
)
TZ = ZoneInfo(env('DEVICE_TIMEZONE', 'Asia/Tehran'))


def positive_setting(name: str, default: int, upper: int) -> int:
    raw = str(env(name, str(default)))
    if not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= upper:
        raise ValueError(f'{name} must be an integer between 1 and {upper}')
    return int(raw)


LOGIN_WINDOW_SECONDS = positive_setting('LOGIN_WINDOW_SECONDS', 900, 86400)
LOGIN_LOCK_SECONDS = positive_setting('LOGIN_LOCK_SECONDS', 900, 86400)
LOGIN_ACCOUNT_FAILURES = positive_setting('LOGIN_ACCOUNT_FAILURES', 5, 100)
LOGIN_IP_FAILURES = positive_setting('LOGIN_IP_FAILURES', 30, 1000)


def as_utc_instant(value: datetime) -> datetime:
    """Normalize DB timestamps for deduplication on PostgreSQL and SQLite.

    PostgreSQL returns aware timestamptz values, whereas SQLite drops the
    timezone marker on round-trip. Attendance timestamps are stored in UTC;
    a naive value read from SQLite therefore represents UTC, not host local
    time. Normalization also prevents silent duplicate acceptance in tests.
    """
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None
            else value.astimezone(timezone.utc))
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Employee(Base):
    __tablename__ = 'employees'
    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default='active')
    attendance_group: Mapped[str] = mapped_column(String(20), default='staff',
                                                  server_default='staff', nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    __table_args__ = (
        CheckConstraint("attendance_group IN ('staff', 'management')", name='ck_employees_attendance_group'),
        Index('ix_employees_attendance_group_status', 'attendance_group', 'status'),
    )


class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(250))
    role: Mapped[str] = mapped_column(String(20), default='viewer')
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Import(Base):
    __tablename__ = 'imports'
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    uploader_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    accepted: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(String(500), default='')


class PendingImport(Base):
    """Uploaded DAT is inert until an administrator resolves and commits it."""
    __tablename__ = 'pending_imports'
    id: Mapped[int] = mapped_column(primary_key=True)
    report_name: Mapped[str] = mapped_column(String(120), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    uploader_id: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default='pending', nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    committed_import_id: Mapped[int | None] = mapped_column(ForeignKey('imports.id', ondelete='SET NULL'), nullable=True)
    __table_args__ = (Index('ix_pending_imports_status_created', 'status', 'created_at'),)


class PendingImportRow(Base):
    """Original source line and every explicit operator resolution are retained."""
    __tablename__ = 'pending_import_rows'
    id: Mapped[int] = mapped_column(primary_key=True)
    pending_import_id: Mapped[int] = mapped_column(ForeignKey('pending_imports.id'), nullable=False)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_line: Mapped[str] = mapped_column(String(2048), nullable=False)
    source_device_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    issue: Mapped[str] = mapped_column(String(40), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    target_employee_id: Mapped[int | None] = mapped_column(ForeignKey('employees.id'), nullable=True)
    reason: Mapped[str] = mapped_column(String(500), default='', nullable=False)
    resolver_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint('pending_import_id', 'line_number', name='unique_pending_source_line'),
        Index('ix_pending_import_rows_batch_decision', 'pending_import_id', 'decision'),
    )


class ReportSet(Base):
    __tablename__ = 'report_sets'
    id: Mapped[int] = mapped_column(primary_key=True)
    import_id: Mapped[int] = mapped_column(ForeignKey('imports.id'), unique=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)


class ReportDay(Base):
    __tablename__ = 'report_days'
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey('report_sets.id'), index=True)
    device_id: Mapped[str] = mapped_column(String(80), index=True)
    local_day: Mapped[str] = mapped_column(String(10), index=True)
    __table_args__ = (UniqueConstraint('report_id', 'device_id', 'local_day', name='unique_report_employee_day'),)


class ReportPunch(Base):
    __tablename__ = 'report_punches'
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey('report_sets.id'), index=True)
    device_id: Mapped[str] = mapped_column(String(80), index=True)
    local_day: Mapped[str] = mapped_column(String(10), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('report_id', 'device_id', 'occurred_at', name='unique_report_punch'),)


class Log(Base):
    __tablename__ = 'attendance_logs'
    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(80), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source_import_id: Mapped[int] = mapped_column(ForeignKey('imports.id'))
    __table_args__ = (UniqueConstraint('device_id', 'occurred_at', name='unique_device_punch'),)


class AttendanceRecord(Base):
    __tablename__ = 'attendance_records'
    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(80), index=True)
    local_day: Mapped[str] = mapped_column(String(10), index=True)
    first_punch: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_punch: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    punch_count: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint('device_id', 'local_day', name='unique_attendance_day'),)


class AttendanceCorrection(Base):
    """Append-only administrative decisions; device punches remain immutable.

    The latest revision for an employee and local date is authoritative.
    A revoked revision resets the calculation to original device punches.
    """
    __tablename__ = 'attendance_corrections'
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey('employees.id'), nullable=False)
    local_day: Mapped[str] = mapped_column(String(10), nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False)
    override_first: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    override_last: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    origin: Mapped[str] = mapped_column(String(24), default='admin_correction',
                                        server_default='admin_correction', nullable=False)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    __table_args__ = (
        Index('ix_attendance_corrections_employee_day_revision', 'employee_id', 'local_day', 'id'),
        Index('ix_attendance_corrections_local_day', 'local_day'),
        CheckConstraint("action IN ('set', 'revoke')", name='ck_attendance_corrections_action'),
        CheckConstraint("origin IN ('admin_correction', 'manager_manual')", name='ck_attendance_corrections_origin'),
        CheckConstraint(
            "(action = 'set' AND (override_first IS NOT NULL OR override_last IS NOT NULL)) "
            "OR (action = 'revoke' AND override_first IS NULL AND override_last IS NULL)",
            name='ck_attendance_corrections_fields',
        ),
    )


class ManagerAttendanceRequest(Base):
    """Secretary proposal; canonical reports change only after admin approval."""
    __tablename__ = 'manager_attendance_requests'
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey('employees.id'), nullable=False)
    local_day: Mapped[str] = mapped_column(String(10), nullable=False)
    proposed_first: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    proposed_last: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    requested_by_id: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    expected_revision_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default='pending', server_default='pending', nullable=False)
    resolved_by_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_reason: Mapped[str] = mapped_column(String(500), default='', server_default='', nullable=False)
    correction_id: Mapped[int | None] = mapped_column(ForeignKey('attendance_corrections.id'), nullable=True)
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'approved', 'rejected', 'cancelled')",
                        name='ck_manager_requests_status'),
        Index('ix_manager_requests_status_created', 'status', 'requested_at'),
        Index('ix_manager_requests_employee_day', 'employee_id', 'local_day'),
        Index('uq_manager_requests_one_pending', 'employee_id', 'local_day', unique=True,
              postgresql_where=sql_text("status = 'pending'"),
              sqlite_where=sql_text("status = 'pending'")),
    )


class Audit(Base):
    __tablename__ = 'audit_logs'
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(80))
    event: Mapped[str] = mapped_column(String(80))
    details: Mapped[str] = mapped_column(String(300), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class LeaveDay(Base):
    """Admin-recorded leave for one employee on one calendar day."""
    __tablename__ = 'leave_days'
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey('employees.id'), nullable=False)
    local_day: Mapped[str] = mapped_column(String(10), nullable=False)
    leave_type: Mapped[str] = mapped_column(String(30), default='مرخصی', nullable=False)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    __table_args__ = (
        UniqueConstraint('employee_id', 'local_day', name='uq_leave_days_employee_day'),
        Index('ix_leave_days_local_day', 'local_day'),
    )


class LoginRateLimit(Base):
    """Persistent anonymized counters shared across Uvicorn workers.

    No username, password or client IP address is stored in this table.
    Expired rows are pruned by a separate one-off maintenance command.
    """
    __tablename__ = 'login_rate_limits'
    key_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        Index('ix_login_rate_limits_last_seen', 'last_seen_at'),
        CheckConstraint('failures >= 0', name='ck_login_rate_limits_failures'),
    )


def password_hash(password: str):
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f'scrypt${salt.hex()}${derived.hex()}'


# A dummy hash gives unknown/inactive accounts the same expensive verification
# path as existing accounts; neither response reveals account existence.
DUMMY_LOGIN_HASH = password_hash(secrets.token_urlsafe(24))


def password_ok(password: str, stored: str):
    try:
        _, salt, expected = stored.split('$')
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def session_auth_tag(user: User):
    """Bind signed browser sessions to the current password without a DB migration.

    The tag is an HMAC, not the password hash itself. Changing the password
    invalidates every previously issued session at its next authenticated call.
    """
    return hmac.new(
        env('SESSION_SECRET', '').encode('utf-8'),
        user.password_hash.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()


app = FastAPI(title='سامانه تردد', docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=env('SESSION_SECRET', ''), same_site='strict', https_only=env('SESSION_HTTPS_ONLY', 'false').lower() == 'true', max_age=28800)
# Expose only bundled, public web fonts; never mount uploads or application data.
FONT_URL = '/assets/fonts/Vazirmatn-Variable-4e3fa217.woff2'
app.mount('/assets/fonts', StaticFiles(directory=str(Path(__file__).resolve().parent / 'static' / 'fonts')), name='fonts')


@app.middleware('http')
async def security_headers(request: Request, call_next):
    """Keep private application responses out of caches and search indexes.

    Robots directives are advisory; authorization remains enforced by the routes.
    A per-response script nonce keeps inline theme scripts working without
    allowing arbitrary inline JavaScript.
    """
    response = await call_next(request)
    response.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive, nosnippet'
    response.headers['Cache-Control'] = 'private, no-store, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    nonce = getattr(request.state, 'csp_nonce', None)
    scripts = f"script-src 'nonce-{nonce}'; " if nonce else "script-src 'none'; "
    response.headers['Content-Security-Policy'] = (
        "default-src 'none'; " + scripts
        + "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        + "font-src 'self' data:; connect-src 'self'; "
        + "base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    # The content-hashed font is not sensitive; HTML and API responses stay no-store.
    if request.url.path == FONT_URL and response.status_code == 200:
        # The slim container lacks a WOFF2 MIME mapping: StaticFiles may return
        # text/plain, which browsers reject with X-Content-Type-Options: nosniff.
        # Override the MIME type only for this exact, known public font URL.
        response.headers['Content-Type'] = 'font/woff2'
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        # Starlette MutableHeaders supports deletion, but not dict.pop().
        del response.headers['Pragma']
    if request.url.scheme == 'https':
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return response


@app.get('/robots.txt', response_class=PlainTextResponse, include_in_schema=False)
def robots_txt():
    return 'User-agent: *\nDisallow: /\n'


@app.on_event('startup')
def setup():
    # Migrations and bootstrap are explicit one-off tasks. Web startup is read-only.
    with engine.connect() as conn:
        from sqlalchemy import inspect
        required = set(Base.metadata.tables)
        missing = required.difference(inspect(conn).get_table_names())
        if missing:
            raise RuntimeError('Database schema is not initialized. Run: alembic upgrade head')


def upgrade_legacy_reports():
    """Legacy data backfill; run as an explicit maintenance task, never at startup."""
    with SessionLocal() as db:
        # Non-destructive compatibility with imports created before named reports.
        for previous in db.scalars(select(Import).outerjoin(ReportSet, ReportSet.import_id == Import.id).where(ReportSet.id.is_(None))).all():
            legacy_name = f'گزارش قدیمی {previous.id} - {previous.filename}'[:120]
            record = ReportSet(import_id=previous.id, name=legacy_name)
            db.add(record)
            db.flush()
            legacy_punches = list(db.scalars(select(Log).where(Log.source_import_id == previous.id)))
            days = {(item.device_id, item.occurred_at.astimezone(TZ).date().isoformat()) for item in legacy_punches}
            for code, day in days:
                db.add(ReportDay(report_id=record.id, device_id=code, local_day=day))
            for item in legacy_punches:
                db.add(ReportPunch(report_id=record.id, device_id=item.device_id, local_day=item.occurred_at.astimezone(TZ).date().isoformat(), occurred_at=item.occurred_at))
        # Upgrade reports created by v0.7, which already have ReportDay rows
        # but no ReportPunch rows. Historical punches are recoverable from Logs.
        existing_sets = db.scalars(select(ReportSet).outerjoin(ReportPunch, ReportPunch.report_id == ReportSet.id).where(ReportPunch.id.is_(None))).all()
        for existing_set in existing_sets:
            for item in db.scalars(select(Log).where(Log.source_import_id == existing_set.import_id)):
                db.add(ReportPunch(report_id=existing_set.id, device_id=item.device_id, local_day=item.occurred_at.astimezone(TZ).date().isoformat(), occurred_at=item.occurred_at))
        db.commit()


def get_db():
    with SessionLocal() as db:
        yield db


def actor(request: Request, db: Session):
    uid = request.session.get('uid')
    user = db.get(User, uid) if isinstance(uid, int) else None
    if not user or not user.active:
        return None
    tag = request.session.get('auth_tag')
    if not isinstance(tag, str) or not hmac.compare_digest(tag, session_auth_tag(user)):
        request.session.clear()
        return None
    return user


def permit(request: Request, db: Session, *roles):
    user = actor(request, db)
    if not user:
        raise HTTPException(401, 'ابتدا وارد سامانه شوید')
    if roles and user.role not in roles:
        raise HTTPException(403, 'مجوز انجام این عملیات را ندارید')
    return user


def csrf(request: Request, token: str):
    if not hmac.compare_digest(str(request.session.get('csrf', '')), token) or not token:
        raise HTTPException(403, 'درخواست نامعتبر است؛ صفحه را تازه‌سازی کنید')


def csrf_token(request: Request):
    if 'csrf' not in request.session:
        request.session['csrf'] = secrets.token_urlsafe(24)
    return request.session['csrf']


def audit(db, user, event, details=''):
    db.add(Audit(actor=user.username, event=event, details=details[:300]))


def login_limit_rows(db: Session, request: Request, username: str, now: datetime):
    """Upsert and lock counters in deterministic order (multi-worker safe on PG).

    Use only the trusted socket peer resolved by ASGI, never client-supplied
    X-Forwarded-For. A trusted reverse proxy must be configured explicitly at
    the ASGI server/ingress layer if the real client address is required.
    """
    peer = request.client.host if request.client else 'unknown'
    secret = env('SESSION_SECRET', '').encode('utf-8')
    identities = (
        ('account', username.strip().casefold(), LOGIN_ACCOUNT_FAILURES),
        ('ip', peer, LOGIN_IP_FAILURES),
    )
    subjects = sorted(
        (hmac.new(secret, f'login:{scope}:{value}'.encode('utf-8'), hashlib.sha256).hexdigest(),
         scope, limit)
        for scope, value, limit in identities
    )
    dialect = db.get_bind().dialect.name
    if dialect == 'postgresql':
        dialect_insert = pg_insert
    elif dialect == 'sqlite':
        dialect_insert = sqlite_insert
    else:
        raise RuntimeError(f'Login throttling requires PostgreSQL or SQLite, got {dialect}')

    rows = []
    for digest, scope, limit in subjects:
        statement = dialect_insert(LoginRateLimit).values(
            key_digest=digest, failures=0, window_started_at=now,
            last_seen_at=now, blocked_until=None,
        ).on_conflict_do_nothing(index_elements=['key_digest'])
        db.execute(statement)
        row = db.scalar(select(LoginRateLimit).where(
            LoginRateLimit.key_digest == digest).with_for_update())
        if row is None:
            raise RuntimeError('Login throttle counter could not be acquired')
        rows.append((scope, row, limit))
    return rows


def login_lock_remaining(rows, now: datetime) -> int:
    """Return remaining block time; reset counters only after expiry."""
    remaining = 0
    for _scope, row, _limit in rows:
        if row.blocked_until is not None:
            seconds = (as_utc_instant(row.blocked_until) - now).total_seconds()
            if seconds > 0:
                remaining = max(remaining, math.ceil(seconds))
                continue
            row.blocked_until = None
            row.failures = 0
            row.window_started_at = now
        elif (now - as_utc_instant(row.window_started_at)).total_seconds() >= LOGIN_WINDOW_SECONDS:
            row.failures = 0
            row.window_started_at = now
    return remaining


def record_login_failure(rows, now: datetime) -> int:
    """Count bad credentials; the threshold attempt itself receives 429."""
    remaining = 0
    for _scope, row, limit in rows:
        row.failures += 1
        row.last_seen_at = now
        if row.failures >= limit:
            row.blocked_until = now + timedelta(seconds=LOGIN_LOCK_SECONDS)
            remaining = LOGIN_LOCK_SECONDS
    return remaining


def reset_login_account_limit(rows, now: datetime):
    """A successful login clears the account counter, not the peer IP budget."""
    for scope, row, _limit in rows:
        if scope == 'account':
            row.failures = 0
            row.window_started_at = now
            row.blocked_until = None
            row.last_seen_at = now


STYLE = '''
.manager-callout{background:var(--primary-soft);border-inline-start:4px solid var(--primary);padding:14px 18px;border-radius:12px;margin-bottom:18px}
.manager-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;margin-bottom:17px}
.manager-list a{display:flex;justify-content:space-between;gap:9px;align-items:center;padding:12px 15px;background:var(--panel-2);border:1px solid var(--line);border-radius:12px;text-decoration:none;min-height:47px}
.manager-list a[aria-current="true"]{border-color:var(--primary);color:var(--primary);background:var(--primary-soft)}
.manager-preview{display:flex;gap:11px;flex-wrap:wrap;align-items:center;margin:14px 0 20px}
.manager-preview span{padding:7px 12px;background:var(--panel-2);border:1px solid var(--line);border-radius:10px}
.manager-bulk-table input[type=text]{min-width:105px;width:100%;max-width:190px}
.manager-bulk-table input[type=checkbox]{width:18px;height:18px;accent-color:var(--primary)}
.manager-bulk-table td{vertical-align:middle;white-space:normal}
.manager-bulk-actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:15px}
.manager-bulk-shared{display:flex;align-items:end;gap:12px;flex-wrap:wrap;margin:15px 0;padding:16px;border:1px solid var(--line);border-radius:12px;background:var(--panel-2)}
.manager-bulk-shared label{display:block;margin-bottom:6px}
.manager-bulk-shared input{width:100%;min-width:150px;max-width:210px}
.manager-bulk-feedback{min-height:1.5em;color:var(--muted);margin:10px 0}
.manager-bulk-feedback.error{color:var(--danger);font-weight:600}
#manager-excel-selection-error:empty{display:none}
.manager-bulk-table input[aria-invalid=true]{border-color:var(--danger)}
.manager-bulk-select-label{display:inline-flex;align-items:center;gap:8px;white-space:nowrap;cursor:pointer}
.manager-excel-scroll{max-height:520px;overflow:auto;overscroll-behavior:contain;border:1px solid var(--line);border-radius:12px;margin:16px 0;position:relative}
.manager-excel-scroll:focus-visible{outline:3px solid var(--primary);outline-offset:2px}
.manager-excel-table{width:100%;min-width:850px;border-collapse:separate;border-spacing:0}
.manager-excel-table thead th{position:sticky;top:0;z-index:2;background:var(--panel-2);box-shadow:0 1px 0 var(--line);white-space:nowrap}
.manager-excel-table td{vertical-align:middle;white-space:normal}
.manager-excel-table .excel-row-reason{max-width:280px;overflow-wrap:anywhere}
.manager-excel-table .excel-unavailable{opacity:.7;background:var(--panel-2)}
.manager-excel-count{font-weight:600;color:var(--heading)}
.manager-excel-controls{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin:14px 0}
.manager-excel-table input[type=checkbox]{width:18px;height:18px;accent-color:var(--primary)}
.manager-excel-status{color:var(--muted)}
.manager-excel-status.unavailable{color:var(--danger)}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.raw-line{display:block;max-width:28rem;white-space:pre-wrap;overflow-wrap:anywhere;direction:ltr;text-align:left}
.reconcile-form{display:grid;gap:.5rem;min-width:15rem;margin-top:.5rem}
.reconcile-form .actions{display:flex;flex-wrap:wrap;gap:.4rem}
@font-face{font-family:"Vazirmatn";src:url("/assets/fonts/Vazirmatn-Variable-4e3fa217.woff2") format("woff2");font-style:normal;font-weight:100 900;font-display:swap}
:root{color-scheme:light;--bg:#f3f6fc;--bg-glow:#eaf0ff;--panel:#fff;--panel-2:#f8faff;--text:#17263b;--heading:#102039;--muted:#708098;--line:#e5eaf3;--line-strong:#d4ddea;--primary:#465de4;--primary-hover:#3449c6;--primary-soft:#eef0ff;--success:#15816b;--success-soft:#e7f8f1;--danger:#b44752;--shadow:0 14px 42px rgba(35,56,103,.065);--sidebar:#111c35;--sidebar-2:#1c2d51;--sidebar-text:#afbed9;--input:#fff;--focus:rgba(70,93,228,.17);font-family:"Vazirmatn",Tahoma,"Segoe UI",sans-serif;background:var(--bg);color:var(--text)}
:root[data-theme="dark"]{color-scheme:dark;--bg:#0b1325;--bg-glow:#152341;--panel:#141f35;--panel-2:#18253e;--text:#dbe6fa;--heading:#f3f7ff;--muted:#95a8c4;--line:#283752;--line-strong:#354764;--primary:#8193ff;--primary-hover:#a3adff;--primary-soft:#243258;--success:#63d5b0;--success-soft:#193b37;--danger:#ff8894;--shadow:0 14px 42px rgba(0,0,0,.18);--sidebar:#0a1223;--sidebar-2:#1e2b48;--sidebar-text:#aabbd7;--input:#101b30;--focus:rgba(129,147,255,.22)}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;direction:rtl;font-size:14px;line-height:1.9;min-width:0;background:radial-gradient(ellipse 75% 40% at 75% -15%,var(--bg-glow),transparent 80%),var(--bg);color:var(--text);transition:background .24s,color .24s}
a{color:var(--primary)}button,input,select,textarea{font-family:inherit}button,a,input,select{transition:background-color .18s,border-color .18s,box-shadow .18s,transform .18s}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
.shell{display:grid;grid-template-columns:258px minmax(0,1fr);min-height:100vh}
aside{background:linear-gradient(165deg,var(--sidebar),#162847 70%,var(--sidebar));color:#fff;padding:27px 17px;position:sticky;top:0;align-self:start;height:100vh;display:flex;flex-direction:column;box-shadow:-4px 0 26px rgba(20,32,60,.08);z-index:2}
.brand{display:flex;align-items:center;gap:11px;margin:3px 9px 42px;font-size:17px;font-weight:800;line-height:1.6;color:#fff}
.brand-mark{width:42px;height:42px;border-radius:14px;display:grid;place-items:center;flex-shrink:0;background:linear-gradient(135deg,#8ea3ff,#5169f0);box-shadow:0 7px 20px #4255b944;font-size:22px;color:white}
.brand small{display:block;font-size:10px;letter-spacing:.05px;font-weight:400;color:var(--sidebar-text);margin-top:2px}
.company-logo-wrap{display:flex;align-items:center;justify-content:center;background:#fff;border-radius:16px;padding:8px 18px;margin:0 8px 18px;box-shadow:0 8px 28px #00000020;min-height:104px}
.company-logo{width:100%;max-width:152px;height:88px;object-fit:contain;display:block}
.nav-label{font-size:10px;color:#8497bb;padding:0 18px 12px;letter-spacing:.3px}
nav{display:flex;flex-direction:column;gap:5px}nav a{display:flex;align-items:center;gap:13px;text-decoration:none;color:var(--sidebar-text);padding:12px 15px;border-radius:12px;font-weight:500;position:relative}
nav a .nav-icon{font-size:18px;line-height:1;opacity:.88;width:22px;text-align:center}
nav a:hover{background:#ffffff0d;color:white}nav a.selected{background:linear-gradient(100deg,#5266cf55,#7c8dff22);color:white;box-shadow:inset 0 0 0 1px #ffffff18}
nav a.selected:before{content:"";position:absolute;right:0;top:23%;height:54%;width:3px;border-radius:6px;background:#a1b0ff}
.side-footer{margin-top:auto;padding:17px 13px 7px;border-top:1px solid #ffffff17;color:#8197b6;font-size:11px}
main{padding:38px clamp(20px,3.4vw,55px) 70px;width:100%;min-width:0;max-width:1560px;margin:auto}
.top{display:flex;justify-content:space-between;align-items:center;gap:24px;margin-bottom:32px}
.eyebrow{color:var(--primary);font-size:11px;font-weight:800;letter-spacing:.2px;margin-bottom:3px}
h1{font-size:clamp(22px,2.1vw,29px);font-weight:800;letter-spacing:-.5px;color:var(--heading);margin:0 0 4px}h2{font-size:17px;font-weight:800;letter-spacing:-.2px;color:var(--heading);margin:0 0 16px}
.muted{color:var(--muted)}.card{background:var(--panel);border:1px solid var(--line);border-radius:19px;padding:clamp(19px,2vw,28px);margin-bottom:21px;box-shadow:var(--shadow)}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px}.grid>.card{min-height:142px;position:relative;overflow:hidden}.grid>.card:before{content:"";position:absolute;top:0;right:0;width:4px;height:100%;background:linear-gradient(var(--primary),#77bcef)}
.stat{font-size:38px;line-height:1.5;font-weight:850;color:var(--heading);font-variant-numeric:tabular-nums;margin-top:8px}
.top-actions{display:flex;align-items:center;gap:9px;flex-wrap:wrap;justify-content:flex-end}.user-badge{display:inline-flex;align-items:center;gap:8px;padding:7px 11px;background:var(--panel);border:1px solid var(--line);border-radius:12px;color:var(--text);font-size:12px;font-weight:700}.avatar{width:27px;height:27px;border-radius:9px;display:grid;place-items:center;color:#fff;background:linear-gradient(140deg,#6b82ed,#4cb3c1);font-weight:800}
input,select{font-size:13px;padding:11px 13px;min-height:44px;border:1px solid var(--line-strong);border-radius:10px;min-width:0;width:100%;background:var(--input);color:var(--text)}input::placeholder{color:var(--muted);opacity:.85}
input:focus,select:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 4px var(--focus)}input[type=file]{padding:8px 10px}input[type=file]::file-selector-button{font-family:inherit;border:none;background:var(--primary-soft);color:var(--primary);border-radius:7px;padding:6px 10px;margin-left:12px;cursor:pointer}
textarea{font-size:13px;padding:11px 13px;border:1px solid var(--line-strong);border-radius:10px;min-height:80px;resize:vertical;width:100%;background:var(--input);color:var(--text)}textarea:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 4px var(--focus)}
.change-columns{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;align-items:start}.change-columns .card{min-width:0}.change-note{background:var(--primary-soft);border-right:4px solid var(--primary);padding:13px 16px;border-radius:10px;margin-bottom:20px}.change-note p{margin:4px 0}.change-form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px;align-items:end}.change-form .full{grid-column:1/-1}.change-form button{justify-self:start}.change-log{max-width:100%;overflow-x:auto}.change-locked{background:var(--panel-2);color:var(--muted)}
label{display:block;font-size:12px;font-weight:700;color:var(--text);margin-bottom:7px}.fields{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;align-items:end}
button,.btn{font-size:12px;font-weight:700;background:var(--primary);color:white;padding:11px 18px;min-height:43px;border:1px solid transparent;border-radius:10px;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:7px;white-space:nowrap;text-align:center}
:root[data-theme="dark"] button:not(.outline):not(.theme-toggle),:root[data-theme="dark"] .btn:not(.outline){color:#0b1430}
button:hover,.btn:hover{background:var(--primary-hover);transform:translateY(-1px)}.outline{background:var(--panel);color:var(--primary);border:1px solid var(--line-strong)}.outline:hover{background:var(--primary-soft);color:var(--primary)}
.theme-toggle{display:inline-flex;align-items:center;gap:8px;background:var(--panel);color:var(--text);border:1px solid var(--line);min-height:43px;padding:9px 13px;border-radius:11px;box-shadow:0 2px 10px rgba(20,32,60,.035)}.theme-toggle:hover{background:var(--primary-soft);color:var(--primary)}.theme-toggle .theme-symbol{font-size:17px;line-height:1}.theme-toggle .theme-label{font-size:12px}
table{width:100%;border-collapse:separate;border-spacing:0;text-align:right}td,th{padding:14px 15px;border-bottom:1px solid var(--line);white-space:nowrap;font-size:12px}th{color:var(--muted);font-weight:800;background:var(--panel-2);font-size:11px}th:first-child{border-radius:0 10px 0 0}th:last-child{border-radius:10px 0 0 0}tbody tr:last-child td{border-bottom:0}tbody tr:hover td{background:var(--panel-2)}
.table-wrap{overflow-x:auto;scrollbar-color:var(--line-strong) transparent}.pill{display:inline-flex;align-items:center;border-radius:25px;background:var(--success-soft);color:var(--success);padding:3px 12px;font-size:11px;font-weight:800}.warn{background:#fff1dc;color:#9a6115}.error{background:#fbe9e8;color:#963e38}:root[data-theme="dark"] .warn{background:#4a381d;color:#ffd08e}:root[data-theme="dark"] .error{background:#44252c;color:#ff9ea5}
.sortable-table th{padding:0}.sortable-table .sort-head{display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;min-height:46px;padding:12px 15px;border:none;border-radius:0;background:transparent;color:var(--text);text-align:right;box-shadow:none;transform:none}.sortable-table .sort-head:hover{background:var(--primary-soft);color:var(--primary);transform:none}.sortable-table .sort-head:focus-visible{outline:2px solid var(--primary);outline-offset:-3px}.sortable-table .sort-indicator{color:var(--primary);white-space:nowrap;font-size:12px}.sortable-table th[aria-sort="ascending"],.sortable-table th[aria-sort="descending"]{background:var(--primary-soft)}:root[data-theme="dark"] .sortable-table button.sort-head{color:var(--text)}
.msg{background:var(--success-soft);color:var(--success);border-right:4px solid var(--success);padding:13px 18px;border-radius:11px;margin-bottom:20px;font-weight:600}
.msg.error{background:var(--panel-2);color:var(--danger);border-right-color:var(--danger)}
.correction-tag{display:inline-block;margin-inline-start:7px;padding:1px 7px;border-radius:6px;font-size:11px;color:var(--success);background:var(--success-soft);white-space:nowrap}
.report-actions-cell{min-width:204px}
.report-row-actions{display:flex;flex-wrap:wrap;gap:7px;align-items:center}
.correction-edit{display:inline-flex;align-items:center;justify-content:center;min-height:36px;padding:6px 9px;border:1px solid var(--line-strong);border-radius:9px;background:var(--panel);font-size:11px;white-space:nowrap;text-decoration:none}
.correction-edit:hover{background:var(--primary-soft)}
.add-record-panel{margin-top:10px}
.add-record-panel>summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;gap:6px}
.add-record-panel>summary::-webkit-details-marker{display:none}
.add-record-panel[open]>summary{margin-bottom:10px}
.add-record-inline{display:flex;flex-wrap:nowrap;gap:8px;align-items:center;padding:12px;background:var(--panel-2);border:1px solid var(--line);border-radius:12px;overflow-x:auto}
.add-record-inline select{height:36px;border:1px solid var(--line-strong);border-radius:8px;padding:0 10px;background:var(--panel);color:inherit;font:inherit;font-size:13px;flex:1 1 180px;min-width:160px}
.add-record-inline input{height:36px;border:1px solid var(--line-strong);border-radius:8px;padding:0 10px;background:var(--panel);color:inherit;font:inherit;font-size:13px;flex:0 0 auto}
.add-record-inline button{height:36px;padding:0 16px;border-radius:8px;background:var(--primary);color:#fff;border:none;cursor:pointer;font:inherit;font-size:13px;flex:0 0 auto;white-space:nowrap}
.mobile-nav{display:none}
/* Keep the dark rail behind every link and the footer, including on long pages. */
.shell>aside{height:auto;min-height:100vh;position:relative;align-self:stretch}
.report-methodology{margin:16px 0;border-color:var(--line-strong);background:var(--panel-2);padding:0 14px}
.report-methodology summary{padding:10px 4px;font-weight:750;color:var(--primary);min-height:44px;display:list-item}
.report-methodology p{margin:8px 0 16px;line-height:2.05;overflow-wrap:anywhere}
.report-methodology strong{color:var(--heading)}
.report-caution{border-right:3px solid var(--primary);padding:8px 12px;margin:15px 0 4px;background:var(--primary-soft);border-radius:8px;font-size:12px}
.report-table-panel{min-width:0}
.report-scroll-hint{margin:0 0 10px;color:var(--muted);font-size:12px}
.report-table-scroll{max-width:100%;overflow-x:auto;overscroll-behavior-x:contain;scrollbar-color:var(--primary) var(--panel-2);scrollbar-width:thin}
.report-table-scroll:focus-visible{outline:3px solid var(--primary);outline-offset:3px}
.report-table-scroll table{min-width:1100px}
.report-table-scroll .report-actions-cell{position:sticky;left:0;background:var(--panel);z-index:1;box-shadow:3px 0 9px rgba(20,32,60,.12)}
.report-table-scroll .report-row-actions{flex-wrap:nowrap}
.report-table-scroll tbody tr:hover .report-actions-cell{background:var(--panel-2)}
.report-mobile-sort{display:none}
.actions{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin:12px 0}.actions form{display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap}.actions select{width:auto}.actions input{width:auto;max-width:320px}details{border:1px solid var(--line);padding:5px 10px;border-radius:9px;margin-top:5px}summary{cursor:pointer;color:var(--primary);font-size:12px}
/* Employee controls: one compact row; popovers are in the browser top layer. */
.employee-table{min-width:1030px}
.employee-actions-cell{width:560px;min-width:560px}
.employee-actions{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;align-items:center;direction:rtl}
.employee-actions>.btn,.employee-actions>button{width:100%;height:42px;min-height:42px;padding:7px 8px;margin:0;font-size:11px;line-height:1.5;border-radius:10px;white-space:nowrap;box-shadow:none}
.employee-actions>.btn:hover,.employee-actions>button:hover{transform:none}
.employee-actions .employee-delete{color:var(--danger);border-color:color-mix(in srgb,var(--danger) 40%,var(--line));background:var(--panel)}
.employee-actions .employee-delete:hover{background:color-mix(in srgb,var(--danger) 9%,var(--panel));color:var(--danger)}
.employee-popover{position:fixed;inset:auto;display:none;margin:0;z-index:1000;width:min(330px,calc(100vw - 24px));max-height:min(70vh,470px);overflow:auto;overscroll-behavior:contain;padding:18px;border:1px solid var(--line-strong);border-radius:15px;background:var(--panel);color:var(--text);box-shadow:0 20px 55px rgba(10,20,43,.2);direction:rtl;text-align:right}
.employee-popover:popover-open{display:grid;gap:11px}
.employee-popover label{margin:0 0 -7px;font-size:12px}
.employee-popover input,.employee-popover select{width:100%;max-width:none;min-height:42px}
.employee-popover button[type=submit]{width:100%;min-height:42px;margin-top:4px}
.employee-popover h3{margin:0 0 2px;font-size:14px;color:var(--heading)}
.employee-popover:focus-visible{outline:3px solid var(--primary);outline-offset:2px}
@media(max-width:800px){.employee-actions-cell{min-width:530px;width:530px}.employee-table{min-width:920px}}
.login-screen{min-height:100vh;display:grid;place-items:center;padding:22px;position:relative}.login-screen:before{content:"";position:fixed;inset:0;z-index:-1;background:radial-gradient(circle at 20% 10%,var(--primary-soft),transparent 42%)}
.login{width:min(100%,440px);max-width:440px;padding:36px;margin:auto}.login h1{font-size:25px}.login input{margin-bottom:17px}.login-screen .theme-toggle{position:absolute;top:25px;left:28px}
.login-content{width:min(100%,480px);display:flex;flex-direction:column;align-items:center;gap:20px}.login-content .login{margin:0;width:100%}.login-logo{width:172px;min-height:143px;padding:12px 22px;border-radius:22px;background:#fff;box-shadow:var(--shadow);display:flex;justify-content:center;align-items:center}.login-logo img{display:block;width:100%;height:118px;object-fit:contain}
@media(max-width:1100px){.shell{grid-template-columns:220px minmax(0,1fr)}aside{padding-inline:11px}.fields{grid-template-columns:repeat(2,minmax(0,1fr))}.grid{gap:12px}}
@media(max-width:1100px){.change-columns{grid-template-columns:1fr}}
@media(max-width:800px){.shell{display:block}aside{height:auto;position:relative;padding:12px 14px}.company-logo-wrap{width:118px;min-height:56px;margin:0 0 9px;padding:3px 8px;border-radius:10px}.company-logo{height:48px;max-width:92px}.brand{margin:0 0 8px;font-size:15px}.brand-mark{width:35px;height:35px}.brand small,.nav-label,.side-footer{display:none}nav{flex-direction:row;overflow-x:auto;gap:5px;padding-bottom:3px;scrollbar-width:thin}nav a{white-space:nowrap;padding:9px 12px;font-size:12px}nav a .nav-icon{font-size:15px;width:auto}nav a.selected:before{display:none}main{padding:23px 15px 45px}.top{align-items:flex-start;gap:14px;flex-wrap:wrap;margin-bottom:23px}.grid{grid-template-columns:1fr 1fr}.fields{grid-template-columns:1fr 1fr}.card{border-radius:15px;padding:18px}.top-actions{justify-content:flex-start}}
@media(max-width:520px){.grid,.fields,.change-form{grid-template-columns:1fr}.top{display:block}.top-actions{justify-content:flex-start;margin-top:16px}.user-badge{display:none}.theme-toggle .theme-label{display:none}.stat{font-size:32px}td,th{padding:10px 12px}.login-screen .theme-toggle .theme-label{display:inline}}
@media(max-width:800px){
 .shell>aside{min-height:0}
 .desktop-nav{display:none}
 .mobile-nav{display:block;background:transparent;border:0;padding:0;margin:3px 0 0}
 .mobile-nav summary{min-height:44px;padding:9px 13px;border:1px solid #ffffff50;border-radius:11px;color:#fff;background:#ffffff12;font-weight:750;font-size:13px}
 .mobile-nav[open] summary{background:#ffffff20}
 .mobile-nav nav{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;max-height:min(60vh,460px);overflow-y:auto;overflow-x:hidden;padding:11px 0 2px}
 .mobile-nav nav a{min-height:44px;white-space:normal;overflow-wrap:anywhere;padding:10px 11px;border:1px solid #ffffff1b;line-height:1.5;align-items:center}
 .mobile-nav nav a.selected{border-color:#a1b0ff80}
 .report-mobile-sort{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:15px}
 .report-mobile-sort label{margin:0 0 4px}
 .report-table-panel{padding:0;background:transparent;border:0;box-shadow:none}
 .report-scroll-hint{display:none}
 .report-table-scroll{overflow:visible}
 .report-table-scroll table,.report-table-scroll tbody{display:block;width:100%;min-width:0}
 .report-table-scroll thead{display:none}
 .report-table-scroll tbody{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:14px}
 .report-table-scroll tbody tr{display:block;min-width:0;border:1px solid var(--line);background:var(--panel);border-radius:15px;box-shadow:var(--shadow);padding:9px 14px}
 .report-table-scroll tbody td{display:grid;grid-template-columns:minmax(100px,43%) minmax(0,1fr);gap:9px;align-items:start;padding:11px 0;white-space:normal;min-width:0;overflow-wrap:anywhere;border-bottom:1px solid var(--line);font-size:12px}
 .report-table-scroll tbody td:before{content:attr(data-label);font-weight:750;color:var(--muted)}
 .report-table-scroll tbody tr td:last-child{border-bottom:0}
 .report-table-scroll .report-actions-cell{display:block;position:static;min-width:0;box-shadow:none;background:transparent}
 .report-table-scroll .report-actions-cell:before{display:block;margin-bottom:9px}
 .report-table-scroll .report-row-actions{flex-wrap:wrap}
 .report-table-scroll .correction-edit{min-height:44px;padding:9px 12px}
 .report-methodology{margin:12px 0}
 .report-caution{line-height:1.9}
}
@media(max-width:520px){.mobile-nav nav{grid-template-columns:1fr}.report-mobile-sort{grid-template-columns:1fr}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{transition:none!important;scroll-behavior:auto!important}}
'''

EMPLOYEE_ACTION_SCRIPT = '''
(function(){
  const edge=12, gap=8;
  function position(panel){
    const trigger=document.querySelector('[popovertarget="'+panel.id+'"]');
    if(!trigger)return;
    const box=trigger.getBoundingClientRect();
    const width=panel.getBoundingClientRect().width;
    const height=panel.getBoundingClientRect().height;
    const left=Math.max(edge,Math.min(window.innerWidth-width-edge,box.right-width));
    const below=box.bottom+gap;
    const above=box.top-height-gap;
    const top=(below+height<=window.innerHeight-edge || above<edge)?below:above;
    panel.style.left=left+'px';
    panel.style.top=Math.max(edge,Math.min(window.innerHeight-height-edge,top))+'px';
  }
  document.querySelectorAll('.employee-popover').forEach(function(panel){
    panel.addEventListener('toggle',function(event){
      if(event.newState==='open'){
        position(panel);
        const first=panel.querySelector('select,input:not([type=hidden])');
        if(first)first.focus({preventScroll:true});
      }
    });
  });
  function reposition(){
    document.querySelectorAll('.employee-popover:popover-open').forEach(position);
  }
  window.addEventListener('resize',reposition,{passive:true});
  window.addEventListener('scroll',reposition,{passive:true,capture:true});
})();
'''

THEME_SCRIPT = '''
(function(){
  const root=document.documentElement;
  function getSaved(){try{return localStorage.getItem('attendance-theme')}catch(e){return null}}
  function save(theme){try{localStorage.setItem('attendance-theme',theme)}catch(e){}}
  function setTheme(theme){
    root.setAttribute('data-theme',theme);
    document.querySelectorAll('[data-theme-toggle]').forEach(function(btn){
      const dark=theme==='dark';
      btn.setAttribute('aria-label',dark?'فعال کردن حالت روز':'فعال کردن حالت شب');
      btn.setAttribute('aria-pressed',String(dark));
      btn.querySelector('.theme-symbol').textContent=dark?'☀':'☾';
      btn.querySelector('.theme-label').textContent=dark?'حالت روز':'حالت شب';
    });
  }
  document.addEventListener('DOMContentLoaded',function(){
    setTheme(root.getAttribute('data-theme')||'light');
    document.querySelectorAll('[data-theme-toggle]').forEach(function(btn){
      btn.addEventListener('click',function(){const next=root.getAttribute('data-theme')==='dark'?'light':'dark';save(next);setTheme(next)});
    });
    document.addEventListener('keydown',function(event){
      if(event.key==='Escape'){
        const menu=document.querySelector('.mobile-nav[open]');
        if(menu){menu.open=false;menu.querySelector('summary').focus()}
      }
    });
  });
})();
'''

REPORT_SORT_SCRIPT = '''
(function(){
  const table = document.getElementById('attendance-report-table');
  if (!table || !table.tBodies.length) return;
  const tbody = table.tBodies[0];
  const headers = Array.from(table.tHead.querySelectorAll('th'));
  const originalRows = Array.from(tbody.rows).map((row, originalIndex) => ({row, originalIndex}));
  const collator = new Intl.Collator('fa', {numeric: true, sensitivity: 'base'});
  let keys = [];
  function asciiDigits(value){
    return String(value).trim()
      .replace(/[۰-۹]/g, c => String(c.charCodeAt(0) - 1776))
      .replace(/[٠-٩]/g, c => String(c.charCodeAt(0) - 1632));
  }
  function comparison(a, b){
    for (const key of keys){
      const left = asciiDigits(a.row.cells[key.index].textContent);
      const right = asciiDigits(b.row.cells[key.index].textContent);
      const aMissing = left === '' || left === '—';
      const bMissing = right === '' || right === '—';
      if (aMissing !== bMissing) return aMissing ? 1 : -1;
      if (aMissing) continue;
      let result;
      if (key.type === 'number'){
        const aNumber = Number(left);
        const bNumber = Number(right);
        result = aNumber < bNumber ? -1 : aNumber > bNumber ? 1 : 0;
      } else if (key.type === 'duration'){
        const duration = value => {
          const parts = value.split(':').map(Number);
          return parts[0] * 60 + parts[1];
        };
        const aMinutes = duration(left);
        const bMinutes = duration(right);
        result = aMinutes < bMinutes ? -1 : aMinutes > bMinutes ? 1 : 0;
      } else if (key.type === 'date' || key.type === 'time'){
        result = left < right ? -1 : left > right ? 1 : 0;
      } else {
        result = collator.compare(left, right);
      }
      if (result) return result * key.direction;
    }
    return a.originalIndex - b.originalIndex;
  }
  function draw(){
    const ordered = originalRows.slice().sort(comparison);
    const fragment = document.createDocumentFragment();
    ordered.forEach(item => fragment.appendChild(item.row));
    tbody.appendChild(fragment);
    headers.forEach((th, index) => {
      const priority = keys.findIndex(key => key.index === index);
      const button = th.querySelector('.sort-head');
      const indicator = th.querySelector('.sort-indicator');
      if (!button || !indicator) return; // Static action column is not sortable.
      th.setAttribute('aria-sort', priority === 0 ? (keys[0].direction === 1 ? 'ascending' : 'descending') : 'none');
      if (priority === -1){
        indicator.textContent = '↕';
        button.title = 'کلیک: مرتب‌سازی | Shift + کلیک: افزودن اولویت مرتب‌سازی';
      } else {
        const direction = keys[priority].direction === 1 ? 'صعودی' : 'نزولی';
        indicator.textContent = String(priority + 1) + (keys[priority].direction === 1 ? '↑' : '↓');
        button.title = 'اولویت ' + String(priority + 1) + '، ' + direction + ' | Shift + کلیک: تغییر جهت';
      }
    });
  }
  headers.forEach((header, index) => {
    const button = header.querySelector('.sort-head');
    if (!button) return;
    button.addEventListener('click', event => {
      const old = keys.find(key => key.index === index);
      const type = button.dataset.sortType;
      if (event.shiftKey){
        if (old) old.direction *= -1;
        else keys.push({index, type, direction: 1});
      } else {
        keys = [{index, type, direction: old && keys.length === 1 && old.direction === 1 ? -1 : 1}];
      }
      draw();
    });
  });
  const mobileField = document.getElementById('report-sort-field');
  const mobileDirection = document.getElementById('report-sort-direction');
  if (mobileField && mobileDirection){
    const mobileSort = () => {
      const index = Number(mobileField.value);
      const button = headers[index] && headers[index].querySelector('.sort-head');
      keys = button ? [{index, type:button.dataset.sortType, direction:Number(mobileDirection.value)}] : [];
      draw();
    };
    mobileField.addEventListener('change', mobileSort);
    mobileDirection.addEventListener('change', mobileSort);
  }
  draw();
})();
'''

MANAGER_BULK_SCRIPT = '''
(function(){
  const form = document.getElementById('manager-bulk-form');
  if (!form) return;
  const master = document.getElementById('manager-bulk-select-all');
  const boxes = Array.from(form.querySelectorAll('tbody input[name="employee_ids"]'));
  const counter = document.getElementById('manager-bulk-count');
  const feedback = document.getElementById('manager-bulk-feedback');
  const apply = document.getElementById('manager-bulk-apply-shared');
  const maxSelected = 25;
  function selected(){return boxes.filter(box => box.checked)}
  function message(value, error){
    feedback.textContent = value;
    feedback.classList.toggle('error', Boolean(error));
    feedback.setAttribute('role', error ? 'alert' : 'status');
  }
  function update(){
    const count = selected().length;
    master.checked = boxes.length > 0 && count === boxes.length;
    master.indeterminate = count > 0 && count < boxes.length;
    counter.textContent = count + ' نفر از ' + boxes.length + ' مدیر انتخاب شده‌اند (حداکثر ۲۵ نفر).';
    if (count > maxSelected) message('حداکثر ۲۵ مدیر در هر ثبت دستی مجاز است؛ تعداد انتخاب‌ها را کم کنید.', true);
  }
  master.disabled = boxes.length === 0 || boxes.length > maxSelected;
  if (boxes.length > maxSelected){
    master.title = 'انتخاب همه فقط وقتی تعداد نتایج ۲۵ مدیر یا کمتر باشد فعال است؛ از جستجو استفاده کنید.';
  }
  master.addEventListener('change', function(){
    boxes.forEach(box => {box.checked = master.checked});
    update();
    message('', false);
  });
  boxes.forEach(box => box.addEventListener('change', function(){
    update();
    if (selected().length <= maxSelected) message('', false);
  }));
  const sharedFields = [
    ['day', 'manager-bulk-common-day'],
    ['first', 'manager-bulk-common-first'],
    ['last', 'manager-bulk-common-last'],
    ['reason', 'manager-bulk-common-reason']
  ];
  apply.addEventListener('click', function(){
    const checked = selected();
    if (!checked.length){message('ابتدا مدیران موردنظر را تیک بزنید.', true);return}
    if (checked.length > maxSelected){message('بیش از ۲۵ مدیر انتخاب شده است.', true);return}
    const values = sharedFields.map(([prefix, id]) => [prefix, document.getElementById(id).value.trim()]);
    if (!values.some(([, value]) => value)){
      message('ابتدا دست‌کم یکی از فیلدهای مشترک را پر کنید.', true);return;
    }
    checked.forEach(box => {
      const row = box.closest('tr');
      values.forEach(([prefix, value]) => {
        if (value){
          const target = row.querySelector('[name="' + prefix + '_' + box.value + '"]');
          target.value = value;
          target.removeAttribute('aria-invalid');
        }
      });
    });
    message('مقادیر مشترک روی ' + checked.length + ' ردیف انتخاب‌شده اعمال شد؛ پیش از ثبت نهایی بررسی کنید.', false);
  });
  function asciiDigits(value){
    return value.replace(/[۰-۹]/g, ch => String(ch.charCodeAt(0) - 1776))
      .replace(/[٠-٩]/g, ch => String(ch.charCodeAt(0) - 1632));
  }
  function seconds(value){
    const normalized = asciiDigits(value.trim());
    if (!/^(?:[01][0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9])?$/.test(normalized)) return null;
    const parts = normalized.split(':').map(Number);
    return parts[0] * 3600 + parts[1] * 60 + (parts[2] || 0);
  }
  function invalid(input, text){
    input.setAttribute('aria-invalid', 'true');
    message(text, true);
    input.focus();
  }
  form.addEventListener('input', function(event){
    if (event.target && event.target.matches('.manager-bulk-table input[type="text"]')){
      event.target.removeAttribute('aria-invalid');
    }
  });
  form.addEventListener('submit', function(event){
    const checked = selected();
    if (checked.length < 1 || checked.length > maxSelected){
      event.preventDefault();
      message('برای ثبت دستی باید بین ۱ تا ۲۵ مدیر انتخاب شوند.', true);
      return;
    }
    for (const box of checked){
      const row = box.closest('tr');
      const fields = Object.fromEntries(['day', 'first', 'last', 'reason'].map(prefix =>
        [prefix, row.querySelector('[name="' + prefix + '_' + box.value + '"]')]));
      const managerName = row.querySelector('strong').textContent;
      const day = fields.day.value.trim();
      const first = seconds(fields.first.value);
      const last = seconds(fields.last.value);
      const reason = fields.reason.value.trim();
      if (!day){event.preventDefault();invalid(fields.day, 'تاریخ شمسی ' + managerName + ' را وارد کنید.');return}
      if (first === null){event.preventDefault();invalid(fields.first, 'ساعت ورود ' + managerName + ' را به شکل 08:00 وارد کنید.');return}
      if (last === null || last <= first){
        event.preventDefault();invalid(fields.last, 'ساعت خروج ' + managerName + ' باید معتبر و بعد از ورود باشد.');return;
      }
      if (reason.length < 5 || reason.length > 500){
        event.preventDefault();invalid(fields.reason, 'دلیل ثبت ' + managerName + ' باید بین ۵ تا ۵۰۰ نویسه باشد.');return;
      }
    }
    message('اطلاعات ' + checked.length + ' مدیر در حال ثبت است...', false);
  });
  update();
  if (master.disabled && boxes.length > maxSelected){
    message('برای «انتخاب همه» در ثبت دستی، نتایج را با جستجو به ۲۵ مدیر یا کمتر محدود کنید. فایل Excel به تیک‌زدن نیاز ندارد.', false);
  }
})();
'''

MANAGER_EXCEL_PREVIEW_SCRIPT = '''
(function(){
  const form = document.getElementById('manager-excel-confirm-form');
  if (!form) return;
  const master = document.getElementById('manager-excel-select-all');
  const boxes = Array.from(form.querySelectorAll('input[name="selected_rows"]:not(:disabled)'));
  const counter = document.getElementById('manager-excel-selection-count');
  const error = document.getElementById('manager-excel-selection-error');
  function update(){
    const chosen = boxes.filter(box => box.checked).length;
    master.checked = boxes.length > 0 && chosen === boxes.length;
    master.indeterminate = chosen > 0 && chosen < boxes.length;
    counter.textContent = chosen + ' ردیف از ' + boxes.length + ' ردیف قابل ثبت انتخاب شده است.';
    error.textContent = '';
  }
  master.disabled = boxes.length === 0;
  master.addEventListener('change', function(){
    boxes.forEach(box => {box.checked = master.checked});
    update();
  });
  boxes.forEach(box => box.addEventListener('change', update));
  form.addEventListener('submit', function(event){
    if (!boxes.some(box => box.checked)){
      event.preventDefault();
      error.textContent = 'ابتدا دست‌کم یک ردیف قابل ثبت را انتخاب کنید.';
      return;
    }
    counter.textContent = 'در حال بررسی و ثبت نهایی ردیف‌های انتخاب‌شده...';
  });
  update();
})();
'''


# The organization's logo is embedded in the HTML response. This keeps the
# existing Dockerfile and the safe installer's two-file update contract intact.
COMPANY_LOGO_BASE64 = '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'


def escape(value):
    import html
    return html.escape(str(value if value is not None else ''), quote=True)


def page(title, body, request, db, section=''):
    user = actor(request, db)
    token = csrf_token(request)
    nonce = secrets.token_urlsafe(24)
    request.state.csp_nonce = nonce
    nav = [('dashboard', '/', 'نمای کلی')]
    if user and user.role == 'secretary':
        nav += [('managers', '/managers/attendance', 'ثبت تردد مدیران'),
                ('managers_bulk', '/managers/attendance/bulk', 'ثبت گروهی مدیران')]
    if user and user.role == 'admin':
        nav += [('reports', '/reports', 'مدیریت تردد'),
                ('imports', '/imports', 'بارگذاری فایل'),
                ('managers_bulk', '/managers/attendance/bulk', 'ثبت گروهی'),
                ('employees', '/employees', 'مدیریت پرسنل'),
                ('users', '/users', 'مدیریت کاربران'),
                ('backups', '/backups', 'پشتیبان‌گیری'),
                ('audit', '/audit', 'تاریخچه تغییرات')]
    icons = {'dashboard': '◈', 'employees': '♙', 'imports': '⇧', 'reports': '▤', 'managers': '◷', 'managers_bulk': '▦', 'manager_approvals': '✓', 'changes': '✎', 'corrections': '◷', 'backups': '◴', 'users': '♧', 'audit': '▥'}
    links = ''.join(f'<a class="{"selected" if key == section else ""}" href="{url}" {"aria-current=page" if key == section else ""}><span class="nav-icon" aria-hidden="true">{icons[key]}</span><span>{label}</span></a>' for key, url, label in nav)
    current_nav_label = next((label for key, _, label in nav if key == section), title)
    navigation = (f'<div class="nav-label">منوی اصلی</div><nav class="desktop-nav" aria-label="منوی اصلی">{links}</nav>'
                  f'<details class="mobile-nav"><summary>☰ فهرست سامانه · {escape(current_nav_label)}</summary>'
                  f'<nav aria-label="منوی موبایل">{links}</nav></details>')
    toggle = '<button type="button" class="theme-toggle" data-theme-toggle aria-label="تغییر حالت روز و شب" aria-pressed="false"><span class="theme-symbol" aria-hidden="true">☾</span><span class="theme-label">حالت شب</span></button>'
    if user:
        layout = f'<div class="shell"><aside><div class="company-logo-wrap"><img class="company-logo" alt="بیمه سامان" src="data:image/png;base64,{COMPANY_LOGO_BASE64}"></div><div class="brand"><div class="brand-mark" aria-hidden="true">◈</div><div>سامانه تردد<small>مدیریت هوشمند تردد · نسخه 1.8.7</small></div></div>{navigation}<div class="side-footer">سامانه مدیریت تردد پرسنل</div></aside><main><header class="top"><div><div class="eyebrow">پنل مدیریت / {escape(current_nav_label)}</div></div><div class="top-actions">{toggle}<span class="user-badge"><span class="avatar" aria-hidden="true">{escape(user.username[:1].upper())}</span>{escape(user.username)}</span><a class="btn outline" href="/logout">خروج</a></div></header>{body}</main></div>'
    else:
        layout = f'<div class="login-screen">{toggle}<div class="login-content"><div class="login-logo"><img alt="بیمه سامان" src="data:image/png;base64,{COMPANY_LOGO_BASE64}"></div>{body}</div></div>'
    layout = layout.replace('نسخه 1.8.7', 'نسخه 1.8.9-r7.4.1')
    if section == 'reports':
        layout += f'<script nonce="{nonce}">{REPORT_SORT_SCRIPT}</script>'
    if section == 'employees':
        layout += f'<script nonce="{nonce}">{EMPLOYEE_ACTION_SCRIPT}</script>'
    if section == 'managers_bulk':
        layout += f'<script nonce="{nonce}">{MANAGER_BULK_SCRIPT}</script>'
        layout += f'<script nonce="{nonce}">{MANAGER_EXCEL_PREVIEW_SCRIPT}</script>'
    return HTMLResponse(f'''<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow,noarchive,nosnippet"><meta name="theme-color" content="#111c35"><title>{escape(title)} | سامانه تردد</title><link rel="preload" href="{FONT_URL}" as="font" type="font/woff2" crossorigin><script nonce="{nonce}">(function(){{try{{var t=localStorage.getItem('attendance-theme');document.documentElement.setAttribute('data-theme',t==='light'||t==='dark'?t:(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'))}}catch(e){{document.documentElement.setAttribute('data-theme','light')}}}})();</script><style>{STYLE}</style></head><body>{layout}<script nonce="{nonce}">{THEME_SCRIPT}</script></body></html>''')


def msg(request):
    value = request.query_params.get('message', '')
    return f'<div class="msg">{escape(value)}</div>' if value else ''


def redirect(path, message=None):
    from urllib.parse import quote
    return RedirectResponse(path + ('?message=' + quote(message) if message else ''), status_code=303)


def changes_redirect(employee_id, day_jalali='', message=''):
    """Return only to the fixed internal editor route (never a user URL)."""
    from urllib.parse import urlencode
    query = {'employee_id': employee_id}
    if day_jalali:
        query['day_jalali'] = day_jalali
    if message:
        query['message'] = message
    return RedirectResponse('/changes?' + urlencode(query), status_code=303)


@app.get('/health')
def health():
    with SessionLocal() as db:
        db.execute(select(func.now()))
    return {'status': 'ok'}


def render_login(request: Request, db: Session, warning: str = ''):
    alert = (f'<div class="msg error" role="alert">{escape(warning)}</div>'
             if warning else '')
    body = f'<div class="card login"><h1>ورود به سامانه</h1><p class="muted">لطفاً اطلاعات حساب کاربری خود را وارد کنید.</p>{alert}{msg(request)}<form method="post" action="/login"><label>نام کاربری</label><input name="username" autocomplete="username" required><label>گذرواژه</label><input type="password" name="password" autocomplete="current-password" required><button style="width:100%">ورود</button></form></div>'
    return page('ورود', body, request, db)


@app.get('/login')
def login_page(request: Request, db: Session = Depends(get_db)):
    return render_login(request, db)


def login_limited(request: Request, db: Session, retry_after: int):
    response = render_login(
        request, db,
        'تعداد تلاش‌های ورود بیش از حد مجاز است. لطفاً کمی بعد دوباره تلاش کنید.',
    )
    response.status_code = 429
    response.headers['Retry-After'] = str(retry_after)
    return response


@app.post('/login')
def login(request: Request, username: str = Form(), password: str = Form(), db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    rows = login_limit_rows(db, request, username, now)
    wait_seconds = login_lock_remaining(rows, now)
    if wait_seconds:
        # The locked counters are already committed from a previous failure.
        # No credential check, audit amplification, or new session is permitted.
        return login_limited(request, db, wait_seconds)

    user = db.scalar(select(User).where(User.username == username))
    verified = password_ok(password, user.password_hash if user and user.active else DUMMY_LOGIN_HASH)
    if not user or not user.active or not verified:
        locked = record_login_failure(rows, now)
        db.add(Audit(actor='anonymous', event='login_failed', details='invalid_credentials'))
        if locked:
            db.add(Audit(actor='anonymous', event='login_rate_limited', details='failure_limit_reached'))
        db.commit()
        if locked:
            return login_limited(request, db, locked)
        return redirect('/login', 'نام کاربری یا گذرواژه اشتباه است')

    reset_login_account_limit(rows, now)
    request.session.clear()
    request.session['uid'] = user.id
    request.session['auth_tag'] = session_auth_tag(user)
    request.session['csrf'] = secrets.token_urlsafe(24)
    audit(db, user, 'login')
    db.commit()
    return redirect('/')


@app.get('/logout')
def logout(request: Request):
    request.session.clear()
    return redirect('/login')


@app.get('/')
def dashboard(request: Request, db: Session = Depends(get_db)):
    # Human visitors arriving at the canonical hostname should see the login
    # form, not a raw JSON 401. Sensitive endpoints keep their 401/403 guards.
    if actor(request, db) is None:
        return redirect('/login')
    if actor(request, db).role == 'secretary':
        return redirect('/managers/attendance')
    permit(request, db, 'admin', 'operator', 'viewer', 'exporter')
    employees = db.scalar(select(func.count()).select_from(Employee).where(Employee.status == 'active'))
    logs = db.scalar(select(func.count()).select_from(Log))
    imports = db.scalar(select(func.count()).select_from(Import))
    body = msg(request) + f'<div class="grid"><div class="card"><span class="muted">پرسنل فعال</span><div class="stat">{employees}</div></div><div class="card"><span class="muted">ترددهای ثبت‌شده</span><div class="stat">{logs}</div></div><div class="card"><span class="muted">فایل‌های پردازش‌شده</span><div class="stat">{imports}</div></div></div><div class="card"><h2>دسترسی سریع</h2><p class="muted">برای ثبت ترددهای جدید، فایل خروجی دستگاه را آپلود کنید؛ گزارش‌ها از اطلاعات ذخیره‌شده ساخته می‌شوند.</p><div class="actions"><a class="btn" href="/imports">آپلود فایل دستگاه</a><a class="btn outline" href="/reports">مشاهده گزارش‌ها</a></div></div>'
    return page('نمای کلی', body, request, db, 'dashboard')


@app.get('/employees')
def employees(request: Request, search: str = '', show_deleted: int = 0, db: Session = Depends(get_db)):
    user = permit(request, db, 'admin', 'operator', 'viewer', 'exporter')
    stmt = select(Employee).order_by(Employee.id.desc()).limit(500)
    if show_deleted != 1:
        stmt = stmt.where(Employee.status != 'deleted')
    if search:
        stmt = stmt.where((Employee.name.ilike(f'%{search}%')) | (Employee.device_id.ilike(f'%{search}%')))
    records = db.scalars(stmt).all()
    token = csrf_token(request)
    form = ''
    if user.role == 'admin':
        form = f'<div class="card"><h2>تعریف کارمند جدید</h2><form class="fields" method="post" action="/employees"><div><label>نام و نام خانوادگی</label><input name="name" maxlength="200" required></div><div><label>شماره دستگاه / شناسه یکتا (برای مدیر بدون دستگاه اختیاری)</label><input name="device_id" maxlength="80" placeholder="برای مدیران می‌تواند خالی باشد"></div><div><label>گروه تردد</label><select name="attendance_group"><option value="staff">پرسنل عادی · دستگاه</option><option value="management">مدیران · ثبت دستی توسط ادمین</option></select></div><div><input type="hidden" name="csrf_token" value="{token}"><button>افزودن کارمند</button></div></form></div>'
    rows = ''
    status_map = {'active': 'فعال', 'suspended': 'معلق', 'deleted': 'حذف‌شده'}
    for employee in records:
        actions = ''
        if user.role == 'admin':
            status_id = f'employee-status-{employee.id}'
            edit_id = f'employee-edit-{employee.id}'
            group_id = f'employee-group-{employee.id}'
            actions = (
                '<div class="employee-actions" role="group" aria-label="عملیات کارمند">'
                f'<button type="button" class="outline" popovertarget="{status_id}" aria-label="ثبت وضعیت {escape(employee.name)}">ثبت وضعیت ▾</button>'
                f'<button type="button" class="outline" popovertarget="{edit_id}" aria-label="ویرایش {escape(employee.name)}">ویرایش ▾</button>'
                f'<button type="button" class="outline" popovertarget="{group_id}" aria-label="تغییر گروه تردد {escape(employee.name)}">تغییر گروه ▾</button>'
            )
            if employee.status != 'deleted':
                actions += f'<a href="/employees/{employee.id}/delete" class="btn outline employee-delete">حذف کارمند</a>'
            else:
                actions += '<span class="muted" aria-label="کارمند حذف‌شده">حذف‌شده</span>'
            actions += '</div>'
            actions += (
                f'<form id="{status_id}" class="employee-popover" popover="auto" method="post" action="/employees/{employee.id}/status">'
                f'<h3>ثبت وضعیت · {escape(employee.name)}</h3>'
                f'<input type="hidden" name="csrf_token" value="{token}">'
                '<label>وضعیت کارمند</label><select name="status">'
                f'<option value="active" {"selected" if employee.status == "active" else ""}>فعال</option>'
                f'<option value="suspended" {"selected" if employee.status == "suspended" else ""}>تعلیق</option>'
                '</select><button type="submit">ثبت وضعیت</button></form>'
                f'<form id="{edit_id}" class="employee-popover" popover="auto" method="post" action="/employees/{employee.id}/edit">'
                f'<h3>ویرایش · {escape(employee.name)}</h3>'
                f'<input type="hidden" name="csrf_token" value="{token}">'
                f'<label>نام و نام خانوادگی</label><input name="name" value="{escape(employee.name)}" required>'
                f'<label>شناسه دستگاه</label><input name="device_id" value="{escape(employee.device_id)}" required>'
                '<button type="submit">ذخیره تغییرات</button></form>'
            )
            actions += (
                f'<form id="{group_id}" class="employee-popover" popover="auto" method="post" action="/employees/{employee.id}/attendance-group">'
                f'<h3>تغییر گروه · {escape(employee.name)}</h3>'
                f'<input type="hidden" name="csrf_token" value="{token}">'
                f'<input type="hidden" name="expected_group" value="{escape(employee.attendance_group)}">'
                '<label>گروه</label><select name="attendance_group">'
                f'<option value="staff" {"selected" if employee.attendance_group == "staff" else ""}>پرسنل عادی · دستگاه</option>'
                f'<option value="management" {"selected" if employee.attendance_group == "management" else ""}>مدیران · ثبت دستی</option></select>'
                '<label>دلیل تغییر گروه</label><input name="reason" minlength="5" maxlength="500" required>'
                '<button type="submit">ثبت گروه با تاریخچه</button></form>'
            )
        group_label = 'مدیران · دستی' if employee.attendance_group == 'management' else 'پرسنل · دستگاه'
        rows += f'<tr><td>{escape(employee.name)}</td><td dir="ltr">{escape(employee.device_id)}</td><td>{escape(group_label)}</td><td><span class="pill {"warn" if employee.status == "suspended" else "error" if employee.status == "deleted" else ""}">{status_map[employee.status]}</span></td><td class="employee-actions-cell">{actions}</td></tr>'
    checkbox = 'checked' if show_deleted == 1 else ''
    bulk_form = ''
    if user.role == 'admin':
        bulk_form = (
            '<div class="card"><h2>بارگذاری گروهی پرسنل از Excel</h2>'
            '<p class="muted">فایل را بر اساس قالب استاندارد تهیه کنید. ستون‌ها: نام، نام خانوادگی، کد ملی، گروه تردد. '
            'فایل فعلی سازمانی (با ستون‌های ردیف، نام، نام خانوادگی، کد ملی) هم مستقیماً قابل بارگذاری است.</p>'
            '<p><a class="btn outline" href="/employees/template">دانلود قالب Excel</a></p>'
            '<form method="post" enctype="multipart/form-data" action="/employees/bulk" class="fields">'
            '<div><label>فایل Excel پرسنل (.xlsx)</label>'
            '<input type="file" name="file" accept=".xlsx" required></div>'
            f'<div><input type="hidden" name="csrf_token" value="{token}"><button>بارگذاری و ثبت پرسنل</button></div>'
            '</form></div>'
        )
    body = msg(request) + form + bulk_form + f'<div class="card"><h2>فهرست پرسنل</h2><form method="get" class="actions"><input name="search" style="max-width:330px" placeholder="جست‌وجوی نام یا شماره دستگاه" value="{escape(search)}"><label style="display:flex;align-items:center;gap:6px"><input type="checkbox" name="show_deleted" value="1" {checkbox} style="width:auto">نمایش حذف‌شده‌ها</label><button>جست‌وجو</button></form><div class="table-wrap" tabindex="0" aria-label="جدول پرسنل؛ در نمایشگر کوچک به طرفین پیمایش کنید"><table class="employee-table"><thead><tr><th>نام کارمند</th><th>شناسه یکتا</th><th>گروه تردد</th><th>وضعیت</th><th>عملیات</th></tr></thead><tbody>{rows}</tbody></table></div></div>'
    return page('مدیریت پرسنل', body, request, db, 'employees')


@app.get('/employees/{employee_id}/delete')
def confirm_delete_employee(employee_id: int, request: Request, db: Session = Depends(get_db)):
    permit(request, db, 'admin')
    employee = db.get(Employee, employee_id)
    if not employee:
        raise HTTPException(404, 'کارمند یافت نشد')
    token = csrf_token(request)
    body = f'<div class="card"><h2>حذف کارمند</h2><p>آیا می‌خواهید <strong>{escape(employee.name)}</strong> با شماره <strong dir="ltr">{escape(employee.device_id)}</strong> را حذف کنید؟</p><p class="muted">حذف به‌صورت منطقی انجام می‌شود: کارمند از فهرست اصلی پنهان می‌شود، اما اطلاعات حضور و غیاب او محفوظ می‌ماند. در صورت نیاز می‌توانید او را از فیلتر «نمایش حذف‌شده‌ها» دوباره فعال کنید.</p><form method="post" action="/employees/{employee.id}/delete" class="actions"><input type="hidden" name="csrf_token" value="{token}"><button style="background:#a43d39">بله، حذف شود</button><a href="/employees" class="btn outline">انصراف</a></form></div>'
    return page('تأیید حذف کارمند', body, request, db, 'employees')


@app.post('/employees/{employee_id}/delete')
def delete_employee(employee_id: int, request: Request, csrf_token: str = Form(), db: Session = Depends(get_db)):
    user = permit(request, db, 'admin')
    csrf(request, csrf_token)
    employee = db.get(Employee, employee_id)
    if not employee:
        raise HTTPException(404, 'کارمند یافت نشد')
    if employee.status == 'deleted':
        return redirect('/employees', 'این کارمند قبلاً حذف شده است')
    employee.status = 'deleted'
    audit(db, user, 'employee_delete', employee.device_id)
    db.commit()
    return redirect('/employees', 'کارمند حذف منطقی شد؛ سوابق تردد او محفوظ است')


# ─── Employee Excel template ────────────────────────────────────────────────

def _make_employee_template() -> bytes:
    """Generate a fresh xlsx template for bulk employee import."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = 'پرسنل'
    ws.sheet_view.rightToLeft = True

    headers = [('نام', 18), ('نام خانوادگی', 22), ('کد ملی', 18), ('گروه تردد', 20)]
    for col, (h, width) in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = Font(bold=True, color='FFFFFF', name='Calibri')
        cell.fill = PatternFill('solid', fgColor='1E3A5F')
        cell.alignment = Alignment(horizontal='center', vertical='center')
        ws.column_dimensions[chr(64 + col)].width = width
    ws.row_dimensions[1].height = 22

    dv = DataValidation(type='list', formula1='"staff,management"', allow_blank=True, showDropDown=False)
    ws.add_data_validation(dv)
    dv.sqref = 'D2:D10000'

    examples = [
        ('بهروز', 'آرمان‌مهر',         '19200364', 'staff'),
        ('مریم',  'کریمی',              '46000999', 'staff'),
        ('علی',   'مدیریت',             '',          'management'),
    ]
    for row_idx, row in enumerate(examples, 2):
        for col, val in enumerate(row, 1):
            ws.cell(row=row_idx, column=col, value=val)

    ns = wb.create_sheet('راهنما')
    ns.sheet_view.rightToLeft = True
    ns.column_dimensions['A'].width = 20
    ns.column_dimensions['B'].width = 50
    guide = [
        ('ستون',          'توضیح'),
        ('نام',           'الزامی · نام کوچک'),
        ('نام خانوادگی',  'الزامی · نام خانوادگی'),
        ('کد ملی',        'الزامی برای پرسنل عادی · به عنوان شناسه دستگاه استفاده می‌شود'),
        ('گروه تردد',     'staff یا management · پیش‌فرض: staff'),
        ('', ''),
        ('staff',         'پرسنل عادی · تردد از دستگاه ثبت می‌شود · کد ملی الزامی است'),
        ('management',    'مدیران · ثبت دستی توسط ادمین · کد ملی اختیاری است'),
    ]
    for r, (a, b) in enumerate(guide, 1):
        ns.cell(row=r, column=1, value=a)
        ns.cell(row=r, column=2, value=b)
        if r == 1:
            for c in (1, 2):
                ns.cell(row=1, column=c).font = Font(bold=True)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@app.get('/employees/template')
def employees_template(request: Request, db: Session = Depends(get_db)):
    permit(request, db, 'admin')
    data = _make_employee_template()
    return Response(
        content=data,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename="employees-template.xlsx"'},
    )


@app.post('/employees/bulk')
async def employees_bulk(
    request: Request,
    file: UploadFile = File(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    import io as _io
    from openpyxl import load_workbook as _load_wb

    admin = permit(request, db, 'admin')
    csrf(request, csrf_token)
    if not (file.filename or '').lower().endswith('.xlsx'):
        return redirect('/employees', 'فقط فایل xlsx مجاز است')
    data = await file.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        return redirect('/employees', 'حجم فایل بیش از ۵ مگابایت است')
    try:
        wb = _load_wb(_io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
    except Exception:
        return redirect('/employees', 'فایل xlsx نامعتبر یا خراب است')

    created = skipped = 0
    error_parts: list[str] = []

    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        # skip completely empty rows
        if not row or not any(c for c in row if c not in ('', None)):
            continue

        # support both 4-col template (نام, نام‌خانوادگی, کد ملی, گروه)
        # and 5-col legacy (ردیف, نام, نام‌خانوادگی, کد ملی, گروه)
        if len(row) >= 5 and str(row[0]).strip().isdigit():
            first, last, national_id = row[1], row[2], row[3]
            group_raw = row[4] if len(row) > 4 else None
        else:
            first, last, national_id = row[0], row[1], row[2]
            group_raw = row[3] if len(row) > 3 else None

        first  = str(first).strip()  if first  not in ('', None) else ''
        last   = str(last).strip()   if last   not in ('', None) else ''
        national_id = str(national_id).strip() if national_id not in ('', None) else ''
        group  = str(group_raw).strip().lower() if group_raw not in ('', None) else 'staff'
        if group not in ('staff', 'management'):
            group = 'staff'

        full_name = f'{first} {last}'.strip()
        if not full_name or len(full_name) > 200:
            error_parts.append(f'سطر {row_idx}: نام نامعتبر')
            continue

        if not national_id:
            if group != 'management':
                error_parts.append(f'سطر {row_idx}: کد ملی برای پرسنل عادی الزامی است')
                continue
            device_id = 'MANUAL-' + secrets.token_hex(8).upper()
        else:
            device_id = national_id[:80]

        if db.scalar(select(Employee.id).where(Employee.device_id == device_id)):
            skipped += 1
            continue

        db.add(Employee(name=full_name, device_id=device_id, attendance_group=group))
        audit(db, admin, 'employee_create', f'code={device_id} group={group} (bulk)')
        created += 1

    db.commit()
    parts = [f'{created} نفر ثبت شد']
    if skipped:
        parts.append(f'{skipped} تکراری نادیده گرفته شد')
    if error_parts:
        parts.append(f'{len(error_parts)} خطا: ' + ' | '.join(error_parts[:4]))
    return redirect('/employees', ' · '.join(parts))


@app.post('/employees')
def add_employee(request: Request, name: str = Form(), device_id: str = Form(), csrf_token: str = Form(), db: Session = Depends(get_db), attendance_group: str = Form('staff')):
    user = permit(request, db, 'admin'); csrf(request, csrf_token)
    name, device_id = name.strip(), device_id.strip()
    if not name or len(name) > 200 or len(device_id) > 80 or attendance_group not in ('staff', 'management'):
        raise HTTPException(400, 'مشخصات نامعتبر است')
    if not device_id:
        if attendance_group != 'management':
            raise HTTPException(400, 'برای پرسنل عادی شماره دستگاه لازم است')
        # The schema retains its stable unique device_id join key, but a
        # manager exempt from fingerprinting need not invent a device code.
        device_id = 'MANUAL-' + secrets.token_hex(8).upper()
    if db.scalar(select(Employee.id).where(Employee.device_id == device_id)):
        return redirect('/employees', 'شماره دستگاه قبلاً ثبت شده است')
    db.add(Employee(name=name, device_id=device_id, attendance_group=attendance_group)); audit(db, user, 'employee_create', f'code={device_id} group={attendance_group}'); db.commit()
    return redirect('/employees', 'کارمند با موفقیت ثبت شد')


@app.post('/employees/{employee_id}/attendance-group')
def change_attendance_group(employee_id: int, request: Request, attendance_group: str = Form(),
                            expected_group: str = Form(), reason: str = Form(),
                            csrf_token: str = Form(), db: Session = Depends(get_db)):
    user = permit(request, db, 'admin')
    csrf(request, csrf_token)
    if attendance_group not in ('staff', 'management'):
        raise HTTPException(400, 'گروه تردد نامعتبر است')
    reason = correction_reason(reason)
    employee = db.scalar(select(Employee).where(Employee.id == employee_id).with_for_update())
    if employee is None or employee.status == 'deleted':
        raise HTTPException(404, 'کارمند فعال یافت نشد')
    if employee.attendance_group != expected_group:
        raise HTTPException(409, 'گروه کارمند تغییر کرده است؛ صفحه را تازه‌سازی کنید')
    previous = employee.attendance_group
    if previous == attendance_group:
        return redirect('/employees', 'گروه کارمند تغییری نکرد')
    employee.attendance_group = attendance_group
    audit(db, user, 'employee_attendance_group_change',
          f'employee_id={employee.id} {previous}->{attendance_group} reason={reason}')
    db.commit()
    return redirect('/employees', 'گروه تردد به‌روزرسانی شد؛ ترددهای قبلی محفوظ‌اند')


@app.post('/employees/{employee_id}/edit')
def edit_employee(employee_id: int, request: Request, name: str = Form(), device_id: str = Form(),
                  csrf_token: str = Form(), db: Session = Depends(get_db),
                  from_changes: str = Form(''), day_jalali: str = Form(''),
                  change_reason: str = Form(''), expected_name: str = Form(''),
                  expected_device_id: str = Form('')):
    user = permit(request, db, 'admin'); csrf(request, csrf_token)
    emp = db.scalar(select(Employee).where(Employee.id == employee_id).with_for_update())
    if not emp: raise HTTPException(404)
    name, device_id = name.strip(), device_id.strip()
    if not name or not device_id or len(name) > 200 or len(device_id) > 80: raise HTTPException(400)
    if from_changes == '1':
        change_reason = correction_reason(change_reason)
        if (emp.name, emp.device_id) != (expected_name, expected_device_id):
            raise HTTPException(409, 'اطلاعات کارمند تغییر کرده است؛ صفحه را تازه‌سازی کنید')
    if db.scalar(select(Employee.id).where(Employee.device_id == device_id, Employee.id != employee_id)):
        return changes_redirect(emp.id, day_jalali, 'شماره دستگاه تکراری است') if from_changes == '1' else redirect('/employees', 'شماره دستگاه تکراری است')
    has_history = emp.device_id != device_id and any(
        db.scalar(select(model.id).where(model.device_id == emp.device_id).limit(1)) is not None
        for model in (Log, ReportDay, ReportPunch, AttendanceRecord)
    )
    if has_history:
        message = 'شناسه دستگاه دارای سابقه قابل تغییر نیست؛ ابتدا باید نگاشت شناسه‌های تاریخی طراحی شود'
        return changes_redirect(emp.id, day_jalali, message) if from_changes == '1' else redirect('/employees', message)
    changed_fields = ','.join(field for field, differs in (
        ('name', emp.name != name), ('device_id', emp.device_id != device_id)
    ) if differs) or 'none'
    emp.name, emp.device_id = name, device_id
    audit(db, user, 'employee_edit', f'employee_id={emp.id} fields={changed_fields} reason={change_reason}' if from_changes == '1' else device_id)
    db.commit()
    return changes_redirect(emp.id, day_jalali, 'مشخصات کارمند ذخیره و ثبت تاریخچه شد') if from_changes == '1' else redirect('/employees', 'اطلاعات ویرایش شد')


@app.post('/employees/{employee_id}/status')
def status_employee(employee_id: int, request: Request, status: str = Form(), csrf_token: str = Form(),
                    db: Session = Depends(get_db), from_changes: str = Form(''),
                    day_jalali: str = Form(''), change_reason: str = Form(''),
                    expected_status: str = Form('')):
    user = permit(request, db, 'admin'); csrf(request, csrf_token)
    if status not in ('active', 'suspended', 'deleted'): raise HTTPException(400)
    if from_changes == '1' and status == 'deleted':
        raise HTTPException(400, 'حذف کارمند فقط از مسیر تأیید اختصاصی مجاز است')
    emp = db.scalar(select(Employee).where(Employee.id == employee_id).with_for_update())
    if not emp: raise HTTPException(404)
    if from_changes == '1':
        change_reason = correction_reason(change_reason)
        if emp.status != expected_status:
            raise HTTPException(409, 'وضعیت کارمند تغییر کرده است؛ صفحه را تازه‌سازی کنید')
    previous_status = emp.status
    emp.status = status
    audit(db, user, 'employee_status',
          f'employee_id={emp.id} {previous_status}->{status} reason={change_reason}' if from_changes == '1' else f'{emp.device_id}: {status}')
    db.commit()
    return changes_redirect(emp.id, day_jalali, 'وضعیت کارمند به‌روزرسانی شد') if from_changes == '1' else redirect('/employees', 'وضعیت کارمند ثبت شد')


@app.get('/changes')
def changes_page(request: Request, employee_id: int = 0, day_jalali: str = '',
                 search: str = '', db: Session = Depends(get_db)):
    """One authenticated workbench for existing, audited edit operations.

    This version does not silently change raw device events or import flow.
    """
    user = permit(request, db, 'admin')
    search = search.strip()[:80]
    today_jalali = to_jalali(datetime.now(TZ).date())
    day_jalali = day_jalali or today_jalali
    day = correction_day(day_jalali)
    stmt = select(Employee).order_by(Employee.name, Employee.id).limit(100)
    if search:
        stmt = stmt.where(Employee.name.ilike(f'%{search}%') | Employee.device_id.ilike(f'%{search}%'))
    found = db.scalars(stmt).all()
    employee = db.get(Employee, employee_id) if employee_id else None
    if employee_id and employee is None:
        raise HTTPException(404, 'کارمند انتخاب‌شده وجود ندارد')
    if employee and all(x.id != employee.id for x in found):
        found.insert(0, employee)
    options = '<option value="0">ابتدا کارمند را انتخاب کنید</option>' + ''.join(
        f'<option value="{person.id}" {"selected" if employee and person.id == employee.id else ""}>'
        f'{escape(person.name)} — {escape(person.device_id)}</option>' for person in found
    )
    token = csrf_token(request)
    body = msg(request) + (
        '<div class="change-note"><strong>میزکار اعمال تغییرات</strong>'
        '<p>ویرایش مشخصات کارمند و اصلاح ورود/خروج از این صفحه انجام می‌شود. '
        'نام کاربری حساب ورود تغییر نمی‌کند؛ ثبت‌های خام دستگاه نیز بدون تغییر باقی می‌مانند.</p>'
        '<p class="muted">توجه: رفع مغایرت هنگام Import و انتقال گزارش‌ها به دیتابیس مرکزیِ واحد '
        'هنوز مرحلهٔ بعدی توسعه است؛ اصلاحات فعلی در گزارش‌های محاسباتی اعمال می‌شوند.</p></div>'
        '<div class="card"><h2>انتخاب رکورد برای ویرایش</h2>'
        '<form method="get" action="/changes" class="fields">'
        f'<div><label for="change-search">جست‌وجوی نام یا شناسه دستگاه</label><input id="change-search" name="search" maxlength="80" value="{escape(search)}" placeholder="نام / شماره دستگاه"></div>'
        f'<div><label for="change-employee">کارمند</label><select id="change-employee" name="employee_id" required>{options}</select></div>'
        f'<div><label for="change-day">تاریخ شمسی</label><input id="change-day" name="day_jalali" value="{escape(day_jalali)}" required placeholder="۱۴۰۵/۰۸/۲۵"></div>'
        '<div><button type="submit">نمایش پرونده و اعمال تغییرات</button></div>'
        '</form><p class="muted">حداکثر ۱۰۰ نتیجه نمایش داده می‌شود؛ برای یافتن سایر پرسنل جست‌وجو کنید.</p></div>'
    )
    if employee is None:
        rows = ''.join(
            '<tr>'
            f'<td>{escape(person.name)}</td><td><bdi>{escape(person.device_id)}</bdi></td>'
            f'<td>{escape({"active": "فعال", "suspended": "معلق", "deleted": "حذف‌شده"}.get(person.status, person.status))}</td>'
            f'<td><a class="btn outline" href="/changes?employee_id={person.id}&amp;day_jalali={escape(day_jalali)}">ویرایش</a></td>'
            '</tr>' for person in found[:30]
        ) or '<tr><td colspan="4">کارمندی یافت نشد.</td></tr>'
        body += ('<div class="card"><h2>انتخاب سریع کارمند</h2><div class="table-wrap"><table>'
                 '<thead><tr><th>نام</th><th>شناسه دستگاه</th><th>وضعیت</th><th>عملیات</th></tr></thead>'
                 f'<tbody>{rows}</tbody></table></div></div>')
        return page('اعمال تغییرات', body, request, db, 'changes')

    from urllib.parse import urlencode
    recent_days = db.scalars(
        select(ReportDay.local_day).where(ReportDay.device_id == employee.device_id)
        .distinct().order_by(ReportDay.local_day.desc()).limit(12)
    ).all()
    recent_links = ''.join(
        '<a class="btn outline" href="/changes?' + escape(urlencode({
            'employee_id': employee.id,
            'day_jalali': to_jalali(datetime.fromisoformat(item).date()),
        })) + '">' + escape(to_jalali(datetime.fromisoformat(item).date())) + '</a>'
        for item in recent_days
    )
    if recent_links:
        body += ('<div class="card"><h2>روزهای دارای تردد اخیر</h2>'
                 f'<div class="actions">{recent_links}</div></div>')

    # The existing schema still binds imported device punches to device_id.
    # Never offer to change this key after a device record exists: that would
    # orphan historical reporting until a future employee_id/alias migration.
    has_history = any((
        db.scalar(select(model.id).where(model.device_id == employee.device_id).limit(1)) is not None
        for model in (Log, ReportDay, ReportPunch, AttendanceRecord)
    ))
    locked = 'readonly class="change-locked"' if has_history else ''
    device_explanation = (
        '<p class="muted">این شناسه به ترددهای قبلی متصل است و در ساختار فعلی قابل تغییر نیست؛ '
        'پس از افزودن نگاشت شناسه‌های تاریخی می‌توان آن را با حفظ سوابق ویرایش کرد.</p>'
        if has_history else '<p class="muted">تا پیش از وجود تردد دستگاه، شناسه قابل اصلاح است.</p>'
    )
    status = {'active': 'فعال', 'suspended': 'معلق', 'deleted': 'حذف‌شده'}.get(employee.status, employee.status)
    name_form = (
        '<div class="card"><h2>۱. مشخصات کارمند</h2>'
        f'<p class="muted">شناسه داخلی ثابت: <bdi>#{employee.id}</bdi> · وضعیت فعلی: {escape(status)}</p>'
        f'<form method="post" action="/employees/{employee.id}/edit" class="change-form">'
        f'<input type="hidden" name="csrf_token" value="{escape(token)}">'
        '<input type="hidden" name="from_changes" value="1">'
        f'<input type="hidden" name="day_jalali" value="{escape(day_jalali)}">'
        f'<input type="hidden" name="expected_name" value="{escape(employee.name)}">'
        f'<input type="hidden" name="expected_device_id" value="{escape(employee.device_id)}">'
        f'<div><label for="change-name">نام و نام خانوادگی</label><input id="change-name" name="name" value="{escape(employee.name)}" maxlength="200" required></div>'
        f'<div><label for="change-device">شماره اختصاصی دستگاه</label><input id="change-device" name="device_id" value="{escape(employee.device_id)}" maxlength="80" {locked} required></div>'
        f'<div class="full">{device_explanation}</div>'
        '<div class="full"><label for="change-name-reason">دلیل ویرایش مشخصات</label><textarea id="change-name-reason" name="change_reason" minlength="5" maxlength="250" required placeholder="دلیل تغییر را ثبت کنید"></textarea></div>'
        '<div class="full"><button type="submit">ثبت تغییرات مشخصات</button></div>'
        '</form><hr style="border:0;border-top:1px solid var(--line);margin:23px 0">'
        f'<form method="post" action="/employees/{employee.id}/status" class="change-form">'
        f'<input type="hidden" name="csrf_token" value="{escape(token)}"><input type="hidden" name="from_changes" value="1">'
        f'<input type="hidden" name="day_jalali" value="{escape(day_jalali)}">'
        f'<input type="hidden" name="expected_status" value="{escape(employee.status)}">'
        f'<div><label for="change-status">وضعیت فعالیت</label><select id="change-status" name="status">'
        f'<option value="active" {"selected" if employee.status == "active" else ""}>فعال</option>'
        f'<option value="suspended" {"selected" if employee.status == "suspended" else ""}>معلق</option></select></div>'
        '<div><label for="change-status-reason">دلیل تغییر وضعیت</label><input id="change-status-reason" name="change_reason" minlength="5" maxlength="250" required></div>'
        '<div class="full"><button type="submit" class="outline">ثبت وضعیت</button></div>'
        '</form></div>'
    )
    day_iso = day.isoformat()
    original = source_day_punches(db, employee.device_id, day_iso)
    revisions = correction_revisions(db, employee.id, day_iso)
    latest = revisions[0] if revisions else None
    active = latest if latest is not None and latest.action == 'set' else None
    first, last, seconds = effective_day_window(original, active)
    first_value = active.override_first.astimezone(TZ).strftime('%H:%M:%S') if active and active.override_first else ''
    last_value = active.override_last.astimezone(TZ).strftime('%H:%M:%S') if active and active.override_last else ''
    punch_text = '، '.join(p.strftime('%H:%M:%S') for p in original) or 'ثبت خام وجود ندارد'
    effective_text = (
        f'<p>ورود مؤثر: <bdi>{escape(first.strftime("%H:%M:%S") if first else "—")}</bdi> · '
        f'خروج مؤثر: <bdi>{escape(last.strftime("%H:%M:%S") if last else "—")}</bdi> · '
        f'مدت حضور: <bdi>{escape(duration_label(seconds) if seconds is not None else "نامشخص")}</bdi></p>'
    )
    attendance_form = (
        '<div class="card"><h2>۲. اصلاح ورود و خروج</h2>'
        f'<p>روز انتخاب‌شده: <strong><bdi>{escape(day_jalali)}</bdi></strong></p>'
        f'<p>ثبت‌های واقعی دستگاه: <bdi>{escape(punch_text)}</bdi></p>{effective_text}'
        '<p class="muted">فیلد خالی یعنی استفاده از ثبت خام؛ برای روز فاقد تردد، هر دو ساعت لازم است. '
        'این ویرایش فقط ساعت ورود و خروج مؤثر را اصلاح می‌کند و فایل اصلی را دست‌کاری نمی‌کند.</p>'
        '<form method="post" action="/corrections/save" class="change-form">'
        f'<input type="hidden" name="csrf_token" value="{escape(token)}">'
        '<input type="hidden" name="from_changes" value="1">'
        f'<input type="hidden" name="employee_id" value="{employee.id}">'
        f'<input type="hidden" name="day_jalali" value="{escape(day_jalali)}">'
        f'<input type="hidden" name="expected_revision" value="{latest.id if latest else 0}">'
        f'<div><label for="change-first">ساعت ورود اصلاحی</label><input id="change-first" name="first_clock" inputmode="numeric" maxlength="8" placeholder="08:00" value="{escape(first_value)}"></div>'
        f'<div><label for="change-last">ساعت خروج اصلاحی</label><input id="change-last" name="last_clock" inputmode="numeric" maxlength="8" placeholder="16:00" value="{escape(last_value)}"></div>'
        '<div class="full"><label for="change-reason">دلیل اصلاح حضور و غیاب</label><textarea id="change-reason" name="reason" minlength="5" maxlength="500" required placeholder="مدرک و دلیل اصلاح"></textarea></div>'
        '<div class="full"><button type="submit">ثبت اصلاح و اعمال در گزارش‌ها</button></div>'
        '</form>'
    )
    if active:
        attendance_form += (
            '<hr style="border:0;border-top:1px solid var(--line);margin:23px 0">'
            '<form method="post" action="/corrections/revoke" class="change-form">'
            f'<input type="hidden" name="csrf_token" value="{escape(token)}">'
            '<input type="hidden" name="from_changes" value="1">'
            f'<input type="hidden" name="employee_id" value="{employee.id}">'
            f'<input type="hidden" name="day_jalali" value="{escape(day_jalali)}">'
            f'<input type="hidden" name="expected_revision" value="{latest.id}">'
            '<div class="full"><label for="change-revoke-reason">دلیل لغو اصلاح</label>'
            '<input id="change-revoke-reason" name="reason" minlength="5" maxlength="500" required></div>'
            '<div class="full"><button type="submit" class="outline">لغو اصلاح و بازگشت به ثبت خام</button></div>'
            '</form>'
        )
    attendance_form += '</div>'
    body += f'<div class="change-columns">{name_form}{attendance_form}</div>'
    history_rows = ''
    for revision in revisions[:30]:
        editor = db.get(User, revision.actor_id)
        history_rows += '<tr>' + ''.join(f'<td>{escape(value)}</td>' for value in (
            revision.id, ('ثبت دستی مدیران' if revision.action == 'set' else 'لغو ثبت دستی')
            if revision.origin == 'manager_manual' else ('اصلاح' if revision.action == 'set' else 'لغو'),
            revision.override_first.astimezone(TZ).strftime('%H:%M:%S') if revision.override_first else '—',
            revision.override_last.astimezone(TZ).strftime('%H:%M:%S') if revision.override_last else '—',
            revision.reason, editor.username if editor else f'#{revision.actor_id}',
            revision.created_at.astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S'),
        )) + '</tr>'
    history_rows = history_rows or '<tr><td colspan="7">هنوز اصلاحی برای این روز ثبت نشده است.</td></tr>'
    body += (
        '<div class="card"><h2>تاریخچه اصلاحات روز انتخابی</h2><div class="change-log"><table>'
        '<thead><tr><th>نسخه</th><th>عملیات</th><th>ورود</th><th>خروج</th><th>دلیل</th><th>ثبت‌کننده</th><th>زمان</th></tr></thead>'
        f'<tbody>{history_rows}</tbody></table></div></div>'
    )
    return page('اعمال تغییرات', body, request, db, 'changes')


def parse_device_file(data: bytes):
    """Parse the verified six-tab-column attendance device export.

    Device ID / timestamp / four device-specific fields. Only ID and timestamp
    are used for the daily presence report; the other fields are validated as
    part of the exact input layout and do not imply check-in/check-out.
    """
    for encoding in ('utf-8-sig', 'utf-16', 'cp1256'):
        try:
            content = data.decode(encoding)
            if '\x00' not in content: break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError('کدگذاری فایل شناخته نشد')
    result, failures = [], []
    for line_no, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line: continue
        columns = [item.strip() for item in line.split('\t')]
        if len(columns) != 6 or any(not item for item in columns):
            failures.append(line_no)
            continue
        identifier, timestamp_text = columns[:2]
        if not re.fullmatch(r'[0-9]{1,80}', identifier) or not all(re.fullmatch(r'\d+', x) for x in columns[2:]):
            failures.append(line_no)
            continue
        try:
            naive = datetime.strptime(timestamp_text, '%Y-%m-%d %H:%M:%S')
            timestamp = naive.replace(tzinfo=TZ).astimezone(timezone.utc)
            result.append((identifier, timestamp))
        except ValueError:
            failures.append(line_no)
    return result, failures


def stage_dat_file(db: Session, user: User, *, filename: str, data: bytes):
    """Stage, never import. Preserve line provenance and flag ALL discrepancies.

    No report, accepted punch or daily rollup changes before commit.
    Report name is auto-generated from filename + timestamp + random suffix.
    """
    if not filename.lower().endswith(('.dat', '.txt')):
        raise HTTPException(400, 'پسوند فایل تردد مجاز نیست')
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, 'حداکثر حجم فایل ۱۰ مگابایت است')
    stem = Path(filename).stem[:50]
    report_name = f'{stem}_{datetime.now(TZ).strftime("%Y%m%d_%H%M%S")}_{secrets.token_hex(3)}'
    for encoding in ('utf-8-sig', 'utf-16', 'cp1256'):
        try:
            content = data.decode(encoding)
            if '\x00' not in content:
                break
        except UnicodeDecodeError:
            pass
    else:
        raise HTTPException(400, 'کدگذاری فایل قابل شناسایی نیست')
    lines = [(number, line) for number, line in enumerate(content.splitlines(), 1) if line.strip()]
    if not lines:
        raise HTTPException(400, 'فایل تردد خالی است')
    if len(lines) > 20000 or any(len(line) > 2048 for _, line in lines):
        raise HTTPException(413, 'فایل برای بررسی امن بیش از حد سطر یا طول سطر دارد')

    employees = {employee.device_id: employee.id for employee in db.scalars(select(Employee).where(
        Employee.status != 'deleted'))}
    parsed = []
    timestamps = []
    seen = set()
    for number, raw in lines:
        fields = [cell.strip() for cell in raw.split('\t')]
        code, stamp, issue = None, None, 'none'
        if (len(fields) != 6 or any(not item for item in fields)
                or not re.fullmatch(r'[0-9]{1,80}', fields[0])
                or not all(re.fullmatch(r'[0-9]+', cell) for cell in fields[2:])):
            issue = 'invalid-format'
        else:
            code = fields[0]
            try:
                stamp = datetime.strptime(fields[1], '%Y-%m-%d %H:%M:%S').replace(
                    tzinfo=TZ).astimezone(timezone.utc)
            except ValueError:
                issue = 'invalid-format'
            if stamp is not None:
                if code not in employees:
                    issue = 'unknown-code'
                if (code, stamp) in seen:
                    issue = 'duplicate-file'
                seen.add((code, stamp))
                timestamps.append(stamp)
        parsed.append((number, raw, code, stamp, issue))

    existing = set()
    codes = {code for _, _, code, stamp, _ in parsed if code and stamp}
    if codes and timestamps:
        existing = {(device_id, as_utc_instant(occurred_at)) for device_id, occurred_at in db.execute(select(Log.device_id, Log.occurred_at).where(
            Log.device_id.in_(codes), Log.occurred_at >= min(timestamps),
            Log.occurred_at <= max(timestamps))).all()}
    batch = PendingImport(
        report_name=report_name, filename=Path(filename).name[:255],
        digest=hashlib.sha256(data).hexdigest(),
        uploader_id=user.id, status='pending',
    )
    db.add(batch)
    db.flush()
    for number, raw, code, stamp, issue in parsed:
        if issue == 'none' and (code, stamp) in existing:
            issue = 'duplicate-db'
        db.add(PendingImportRow(
            pending_import_id=batch.id, line_number=number, raw_line=raw,
            source_device_id=code, occurred_at=stamp, issue=issue,
            target_employee_id=employees.get(code),
            decision='ready' if issue == 'none' else 'pending',
        ))
    audit(db, user, 'file_stage', f'batch={batch.id} lines={len(parsed)}')
    db.commit()
    return batch


def resolve_staged_row(db: Session, user: User, batch_id: int, row_id: int,
                       decision: str, reason: str, target_employee_id: int = 0):
    """Resolve exactly one line. The audit log records each change of mind."""
    batch = db.scalar(select(PendingImport).where(PendingImport.id == batch_id).with_for_update())
    if not batch or batch.status != 'pending':
        raise HTTPException(409, 'این فایل دیگر در انتظار بررسی نیست')
    row = db.scalar(select(PendingImportRow).where(
        PendingImportRow.id == row_id, PendingImportRow.pending_import_id == batch_id).with_for_update())
    if not row:
        raise HTTPException(404, 'سطر انتخاب‌شده پیدا نشد')
    reason = ' '.join(reason.strip().split())
    if len(reason) < 5 or len(reason) > 500:
        raise HTTPException(400, 'دلیل تصمیم باید بین ۵ تا ۵۰۰ نویسه باشد')
    if decision == 'skip':
        row.decision = 'skip'
    elif decision == 'accept' and row.issue == 'unknown-code' and row.occurred_at:
        employee = db.get(Employee, target_employee_id)
        if not employee or employee.status == 'deleted':
            raise HTTPException(400, 'کارمند مقصد معتبر نیست')
        row.target_employee_id = employee.id
        row.decision = 'accept'
    elif decision == 'accept' and row.issue == 'none' and row.target_employee_id:
        row.decision = 'accept'
    else:
        raise HTTPException(400, 'برای این مغایرت فقط رد سطر یا انتخاب کارمند مجاز است')
    row.reason = reason
    row.resolver_id = user.id
    row.resolved_at = datetime.now(timezone.utc)
    audit(db, user, 'file_row_decision',
          f'batch={batch_id} line={row.line_number} decision={row.decision} '
          f'employee={row.target_employee_id} reason={reason}')
    db.commit()
    return row


def commit_staged_file(db: Session, user: User, batch_id: int):
    """Commit all approved rows atomically; recheck concurrent DB changes."""
    batch = db.scalar(select(PendingImport).where(PendingImport.id == batch_id).with_for_update())
    if not batch or batch.status != 'pending':
        raise HTTPException(409, 'فایل در انتظار تأیید پیدا نشد')
    rows = db.scalars(select(PendingImportRow).where(
        PendingImportRow.pending_import_id == batch.id).order_by(PendingImportRow.line_number)).all()
    if any(row.decision == 'pending' for row in rows):
        raise HTTPException(409, 'ابتدا همه مغایرت‌ها را تعیین تکلیف کنید')
    if db.scalar(select(ReportSet.id).where(ReportSet.name == batch.report_name)):
        raise HTTPException(409, 'نام گزارش در این فاصله استفاده شده است')
    selected = [row for row in rows if row.decision in ('ready', 'accept')]
    if not selected:
        raise HTTPException(400, 'هیچ تردد تأییدشده‌ای برای Import وجود ندارد')
    approved = []
    for row in selected:
        employee = db.get(Employee, row.target_employee_id)
        if (not employee or employee.status == 'deleted' or not row.occurred_at
                or (row.issue != 'unknown-code' and employee.device_id != row.source_device_id)):
            raise HTTPException(409, f'هویت سطر {row.line_number} تغییر کرده؛ فایل نیازمند بررسی مجدد است')
        approved.append((employee.device_id, as_utc_instant(row.occurred_at)))
    if len(set(approved)) != len(approved):
        raise HTTPException(409, 'پس از نگاشت پرسنل، تردد تکراری وجود دارد؛ تصمیم‌ها را اصلاح کنید')
    existing = {(device_id, as_utc_instant(occurred_at)) for device_id, occurred_at in db.execute(select(Log.device_id, Log.occurred_at).where(
        Log.device_id.in_({code for code, _ in approved}),
        Log.occurred_at >= min(stamp for _, stamp in approved),
        Log.occurred_at <= max(stamp for _, stamp in approved))).all()}
    if set(approved) & existing:
        raise HTTPException(409, 'در فاصله بررسی، تردد تکراری وارد دیتابیس مرکزی شده؛ Import متوقف شد')
    upload = Import(filename=batch.filename, digest=batch.digest, uploader_id=batch.uploader_id,
                    accepted=len(approved), rejected=len(rows)-len(approved), note='پس از رفع مغایرت تأیید شد')
    try:
        db.add(upload)
        db.flush()
        report = ReportSet(import_id=upload.id, name=batch.report_name)
        db.add(report)
        db.flush()
        affected = {(code, stamp.astimezone(TZ).date().isoformat()) for code, stamp in approved}
        for code, day in affected:
            db.add(ReportDay(report_id=report.id, device_id=code, local_day=day))
        for code, stamp in approved:
            db.add(ReportPunch(report_id=report.id, device_id=code,
                               local_day=stamp.astimezone(TZ).date().isoformat(), occurred_at=stamp))
            db.add(Log(device_id=code, occurred_at=stamp, source_import_id=upload.id))
        db.flush()
        for code, day in affected:
            start = datetime.fromisoformat(day).replace(tzinfo=TZ).astimezone(timezone.utc)
            end = (datetime.fromisoformat(day) + timedelta(days=1)).replace(tzinfo=TZ).astimezone(timezone.utc)
            times = db.scalars(select(Log.occurred_at).where(
                Log.device_id == code, Log.occurred_at >= start, Log.occurred_at < end
            ).order_by(Log.occurred_at)).all()
            summary = db.scalar(select(AttendanceRecord).where(
                AttendanceRecord.device_id == code, AttendanceRecord.local_day == day))
            if summary is None:
                summary = AttendanceRecord(device_id=code, local_day=day,
                                           first_punch=times[0], punch_count=len(times))
                db.add(summary)
            summary.first_punch = times[0]
            summary.last_punch = times[-1] if len(times) > 1 else None
            summary.punch_count = len(times)
        batch.committed_import_id = upload.id
        batch.status = 'committed'
        audit(db, user, 'file_commit', f'batch={batch.id} import={upload.id} accepted={len(approved)}')
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, 'Import هم‌زمان با تغییر دیگری تداخل کرد؛ دوباره بررسی کنید') from exc
    except Exception:
        db.rollback()
        raise
    return upload, report, [stamp.astimezone(TZ).date() for _, stamp in approved]


@app.get('/imports/staged/{batch_id}')
def staged_import_page(batch_id: int, request: Request, page_num: int = 1,
                       db: Session = Depends(get_db)):
    user = permit(request, db, 'admin', 'operator')
    batch = db.get(PendingImport, batch_id)
    if not batch:
        raise HTTPException(404, 'فایل در انتظار بررسی پیدا نشد')
    if page_num < 1:
        raise HTTPException(400, 'شماره صفحه نامعتبر است')
    token = csrf_token(request)
    total = db.scalar(select(func.count()).select_from(PendingImportRow).where(
        PendingImportRow.pending_import_id == batch_id)) or 0
    pending_count = db.scalar(select(func.count()).select_from(PendingImportRow).where(
        PendingImportRow.pending_import_id == batch_id, PendingImportRow.decision == 'pending')) or 0
    skipped_count = db.scalar(select(func.count()).select_from(PendingImportRow).where(
        PendingImportRow.pending_import_id == batch_id, PendingImportRow.decision == 'skip')) or 0
    rows = db.scalars(select(PendingImportRow).where(
        PendingImportRow.pending_import_id == batch_id
    ).order_by(PendingImportRow.line_number).offset((page_num - 1) * 50).limit(50)).all()
    employees = db.scalars(select(Employee).where(Employee.status != 'deleted').order_by(Employee.name)).all()
    choices = '<option value="0">کارمند مقصد را انتخاب کنید</option>' + ''.join(
        f'<option value="{emp.id}">{escape(emp.name)} — {escape(emp.device_id)}</option>'
        for emp in employees
    )
    issue_names = {
        'none': 'بدون مغایرت', 'invalid-format': 'فرمت یا تاریخ نامعتبر',
        'unknown-code': 'شمارهٔ دستگاه ناشناس', 'duplicate-file': 'تکراری در همین فایل',
        'duplicate-db': 'از قبل در دیتابیس ثبت شده',
    }
    decision_names = {'ready': 'آمادهٔ ثبت', 'pending': 'نیازمند تصمیم',
                      'accept': 'پذیرش با تعیین مقصد', 'skip': 'حذف از این Import'}
    table_rows = ''
    for row in rows:
        who = db.get(User, row.resolver_id) if row.resolver_id else None
        stamp_text = as_utc_instant(row.occurred_at).astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S') if row.occurred_at else '—'
        decision_form = ''
        if user.role == 'admin' and batch.status == 'pending':
            selector = f'<label>ثبت به نام کارمند<div><select name="target_employee_id">{choices}</select></div></label>' if row.issue == 'unknown-code' else ''
            allow_accept = row.issue in ('unknown-code', 'none')
            accept_button = '<button name="decision" value="accept">پذیرش</button>' if allow_accept else ''
            decision_form = (
                f'<form method="post" action="/imports/staged/{batch.id}/resolve" class="reconcile-form">'
                f'<input type="hidden" name="row_id" value="{row.id}">'
                f'<input type="hidden" name="csrf_token" value="{token}">{selector}'
                '<label>دلیل تصمیم<input name="reason" minlength="5" maxlength="500" '
                'placeholder="مثلاً تطبیق با پرونده پرسنلی" required></label>'
                f'<div class="actions">{accept_button}<button class="outline" name="decision" value="skip">رد این سطر</button></div>'
                '</form>'
            )
        table_rows += (
            f'<tr><td>{row.line_number}</td><td><bdi>{escape(row.source_device_id or "—")}</bdi></td>'
            f'<td><bdi>{escape(stamp_text)}</bdi></td><td>{escape(issue_names.get(row.issue, row.issue))}</td>'
            f'<td>{escape(decision_names.get(row.decision, row.decision))}</td>'
            f'<td><code class="raw-line">{escape(row.raw_line)}</code></td>'
            f'<td>{escape(who.username if who else "—")}<br>{escape(row.reason)}{decision_form}</td></tr>'
        )
    pages = (total + 49) // 50
    pagination = ' '.join(
        f'<a class="btn outline" href="/imports/staged/{batch.id}?page_num={number}">{number}</a>'
        for number in range(max(1, page_num - 3), min(pages, page_num + 3) + 1)
    )
    controls = ''
    if user.role == 'admin' and batch.status == 'pending':
        if pending_count == 0:
            controls += (
                f'<form method="post" action="/imports/staged/{batch.id}/commit">'
                f'<input type="hidden" name="csrf_token" value="{token}">'
                '<button>تأیید نهایی و Import به دیتابیس</button></form>'
            )
        else:
            controls += '<p class="muted">تا رفع همهٔ مغایرت‌ها، Import نهایی غیرفعال است.</p>'
        controls += (
            f'<form method="post" action="/imports/staged/{batch.id}/cancel">'
            f'<input type="hidden" name="csrf_token" value="{token}">'
            '<button class="outline">لغو این فایل در انتظار</button></form>'
        )
    body = (
        msg(request) + '<div class="card"><h2>صفحهٔ رفع مغایرت فایل ورودی</h2>'
        f'<p>نام گزارش: <strong>{escape(batch.report_name)}</strong> — {escape(batch.filename)}</p>'
        f'<p>وضعیت: {escape(batch.status)} | کل سطرها: {total} | نیازمند تصمیم: '
        f'<strong>{pending_count}</strong> | ردشده با دلیل: {skipped_count}</p>'
        '<p class="muted">تا زمان تأیید نهایی، هیچ سطری وارد گزارش‌ها و ترددهای مرجع نمی‌شود. '
        'سطرهای ناسالم یا تکراری فقط با دلیل قابل رد هستند؛ شمارهٔ ناشناس را می‌توان به کارمند موجود نگاشت کرد.</p>'
        f'<div class="actions">{controls}<a class="btn outline" href="/imports">بازگشت به فایل‌ها</a></div></div>'
        '<div class="card"><h2>جزئیات سطرهای فایل</h2>'
        '<div class="table-wrap"><table><thead><tr><th>سطر</th><th>شناسه ورودی</th><th>زمان</th>'
        '<th>مغایرت</th><th>تصمیم</th><th>متن اصلی</th><th>رفع مغایرت و تاریخچه</th>'
        f'</tr></thead><tbody>{table_rows}</tbody></table></div>'
        f'<div class="actions">صفحه {page_num} از {max(1,pages)} — {pagination}</div></div>'
    )
    return page('رفع مغایرت فایل', body, request, db, 'imports')


@app.post('/imports/staged/{batch_id}/resolve')
def resolve_staged_post(batch_id: int, request: Request, row_id: int = Form(),
                        decision: str = Form(), reason: str = Form(),
                        target_employee_id: int = Form(0), csrf_token: str = Form(),
                        db: Session = Depends(get_db)):
    user = permit(request, db, 'admin')
    csrf(request, csrf_token)
    resolve_staged_row(db, user, batch_id, row_id, decision, reason, target_employee_id)
    return redirect(f'/imports/staged/{batch_id}', 'تصمیم سطر ثبت و در Audit ذخیره شد')


@app.post('/imports/staged/{batch_id}/commit')
def commit_staged_post(batch_id: int, request: Request, csrf_token: str = Form(),
                       db: Session = Depends(get_db)):
    user = permit(request, db, 'admin')
    csrf(request, csrf_token)
    _, report, dates = commit_staged_file(db, user, batch_id)
    from urllib.parse import urlencode
    query = urlencode({'start_jalali': to_jalali(min(dates)),
                       'end_jalali': to_jalali(max(dates)),
                       'report_id': report.id, 'period': 'daily',
                       'message': 'فایل پس از رفع مغایرت به دیتابیس اضافه شد'})
    return RedirectResponse('/reports?' + query, status_code=303)


@app.post('/imports/staged/{batch_id}/cancel')
def cancel_staged_post(batch_id: int, request: Request, csrf_token: str = Form(),
                       db: Session = Depends(get_db)):
    user = permit(request, db, 'admin')
    csrf(request, csrf_token)
    batch = db.scalar(select(PendingImport).where(PendingImport.id == batch_id).with_for_update())
    if not batch or batch.status != 'pending':
        raise HTTPException(409, 'فایل منتظر بررسی پیدا نشد')
    batch.status = 'cancelled'
    audit(db, user, 'file_stage_cancel', f'batch={batch.id}')
    db.commit()
    return redirect('/imports', 'فایل آزمایشی از صف Import خارج شد؛ تاریخچه بررسی حفظ شده است')


@app.get('/imports')
def imports_page(request: Request, db: Session = Depends(get_db)):
    user = permit(request, db, 'admin', 'operator', 'viewer', 'exporter')
    token = csrf_token(request)
    upload = f'<div class="card"><h2>بارگذاری فایل دستگاه</h2><p class="muted">فایل تردد دستگاه را انتخاب کنید. ترددهای معتبر مستقیماً وارد دیتابیس می‌شوند. کدهای ناشناس یا تکراری نادیده گرفته می‌شوند.</p><form method="post" enctype="multipart/form-data" action="/imports/direct"><div class="fields"><div><label>فایل تردد دستگاه (.dat یا .txt)</label><input type="file" name="file" accept=".dat,.txt" required></div><div><input type="hidden" name="csrf_token" value="{token}"><button>بارگذاری و ثبت در دیتابیس</button></div></div></form></div>' if user.role in ('admin', 'operator') else ''
    pending_html = ''
    records = db.scalars(select(Import).where(Import.note != 'hidden').order_by(Import.id.desc()).limit(100)).all()
    rows = ''
    for item in records:
        delete_button = f'<a class="btn outline" href="/imports/{item.id}/delete">حذف گزارش</a>' if user.role == 'admin' else ''
        label = db.scalar(select(ReportSet.name).where(ReportSet.import_id == item.id)) or 'بدون نام'
        report_id = db.scalar(select(ReportSet.id).where(ReportSet.import_id == item.id))
        day_count = db.scalar(select(func.count()).select_from(ReportDay).where(ReportDay.report_id == report_id)) if report_id else 0
        rows += f'<tr><td>{escape(label)}</td><td>{escape(item.filename)}</td><td>{day_count}</td><td>{escape(item.uploaded_at.astimezone(TZ).strftime("%Y-%m-%d %H:%M"))}</td><td>{delete_button}</td></tr>'
    body = msg(request) + upload + pending_html + f'<div class="card"><h2>فایل‌های تأییدشده</h2><p class="muted">گزارش‌های نام‌گذاری‌شده، مستقل و قابل انتخاب هستند.</p><div class="table-wrap"><table><tr><th>نام گزارش</th><th>فایل دستگاه</th><th>روزهای حضور</th><th>زمان پردازش</th><th>مدیریت</th></tr>{rows}</table></div></div>'
    return page('ورود اطلاعات دستگاه', body, request, db, 'imports')


@app.get('/imports/{import_id}/delete')
def delete_import_confirm(import_id: int, request: Request, db: Session = Depends(get_db)):
    permit(request, db, 'admin')
    item = db.get(Import, import_id)
    if not item:
        raise HTTPException(404, 'فایل موردنظر پیدا نشد')
    token = csrf_token(request)
    count = db.scalar(select(func.count()).select_from(Log).where(Log.source_import_id == import_id)) or 0
    content = f'<div class="card"><h2>پنهان‌کردن گزارش از لیست</h2><p>فایل: <strong>{escape(item.filename)}</strong></p><p>تعداد ترددهای ثبت‌شده از این فایل: <strong>{count}</strong></p><p class="muted">فایل فقط از لیست پنهان می‌شود. تمام ترددهای این فایل در دیتابیس مرکزی باقی می‌مانند و در گزارش‌ها همچنان نمایش داده می‌شوند.</p><form method="post" action="/imports/{item.id}/delete" class="actions"><input type="hidden" name="csrf_token" value="{token}"><button>بله، از لیست پنهان شود</button><a href="/imports" class="btn outline">انصراف</a></form></div>'
    return page('حذف گزارش', content, request, db, 'imports')


@app.post('/imports/{import_id}/delete')
def delete_import(import_id: int, request: Request, csrf_token: str = Form(), db: Session = Depends(get_db)):
    """Hide the import from the list without touching any punch data in the DB."""
    user = permit(request, db, 'admin')
    csrf(request, csrf_token)
    item = db.get(Import, import_id)
    if not item:
        raise HTTPException(404, 'فایل موردنظر پیدا نشد')
    filename = item.filename
    item.note = 'hidden'
    audit(db, user, 'file_hide', f'{filename} hidden from list (data preserved)')
    db.commit()
    return redirect('/imports', f'فایل «{filename}» از لیست پنهان شد؛ همه ترددها در دیتابیس محفوظ هستند')


@app.post('/imports')
async def upload(request: Request, file: UploadFile = File(), csrf_token: str = Form(), db: Session = Depends(get_db)):
    """Legacy staged upload — kept for backward compatibility."""
    user = permit(request, db, 'admin', 'operator')
    csrf(request, csrf_token)
    filename = Path(file.filename or '').name[:255]
    data = await file.read(10 * 1024 * 1024 + 1)
    batch = stage_dat_file(db, user, filename=filename, data=data)
    return RedirectResponse(f'/imports/staged/{batch.id}', status_code=303)


@app.post('/imports/direct')
async def import_direct(
    request: Request,
    file: UploadFile = File(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    """Direct import: parse DAT and write to DB in one step, no staging/review."""
    user = permit(request, db, 'admin', 'operator')
    csrf(request, csrf_token)

    filename = Path(file.filename or '').name[:255]
    data = await file.read(10 * 1024 * 1024 + 1)

    if not filename.lower().endswith(('.dat', '.txt')):
        return redirect('/imports', 'پسوند فایل تردد مجاز نیست (.dat یا .txt)')
    if len(data) > 10 * 1024 * 1024:
        return redirect('/imports', 'حداکثر حجم فایل ۱۰ مگابایت است')

    digest = hashlib.sha256(data).hexdigest()
    if db.scalar(select(Import.id).where(Import.digest == digest)):
        return redirect('/imports', 'این فایل قبلاً وارد سیستم شده است')

    # decode
    for encoding in ('utf-8-sig', 'utf-16', 'cp1256'):
        try:
            content = data.decode(encoding)
            if '\x00' not in content:
                break
        except UnicodeDecodeError:
            pass
    else:
        return redirect('/imports', 'کدگذاری فایل قابل شناسایی نیست')

    lines = [(n, ln) for n, ln in enumerate(content.splitlines(), 1) if ln.strip()]
    if not lines:
        return redirect('/imports', 'فایل تردد خالی است')
    if len(lines) > 20000 or any(len(ln) > 2048 for _, ln in lines):
        return redirect('/imports', 'فایل بیش از حد بزرگ است')

    employees = {e.device_id: e for e in db.scalars(
        select(Employee).where(Employee.status != 'deleted')
    )}

    skipped_format = skipped_unknown = skipped_dup_file = auto_created = 0
    valid: list[tuple[str, datetime]] = []
    seen: set[tuple[str, datetime]] = set()

    for _n, raw in lines:
        fields = [c.strip() for c in raw.split('\t')]
        if (len(fields) != 6 or any(not f for f in fields)
                or not re.fullmatch(r'[0-9]{1,80}', fields[0])
                or not all(re.fullmatch(r'[0-9]+', c) for c in fields[2:])):
            skipped_format += 1
            continue
        code = fields[0]
        try:
            stamp = datetime.strptime(fields[1], '%Y-%m-%d %H:%M:%S').replace(
                tzinfo=TZ).astimezone(timezone.utc)
        except ValueError:
            skipped_format += 1
            continue
        if code not in employees:
            placeholder = Employee(
                name=f'ناشناس — {code}',
                device_id=code,
                attendance_group='staff',
            )
            db.add(placeholder)
            db.flush()
            employees[code] = placeholder
            auto_created += 1
        if (code, stamp) in seen:
            skipped_dup_file += 1
            continue
        seen.add((code, stamp))
        valid.append((code, stamp))

    if not valid:
        return redirect('/imports', 'هیچ تردد معتبری در فایل یافت نشد')

    # check DB duplicates
    codes = {c for c, _ in valid}
    min_t, max_t = min(s for _, s in valid), max(s for _, s in valid)
    existing = {
        (d, as_utc_instant(o))
        for d, o in db.execute(
            select(Log.device_id, Log.occurred_at).where(
                Log.device_id.in_(codes),
                Log.occurred_at >= min_t,
                Log.occurred_at <= max_t,
            )
        ).all()
    }

    # create Import record
    upload_rec = Import(
        filename=filename,
        digest=digest,
        uploader_id=user.id,
        note='بارگذاری مستقیم',
    )
    db.add(upload_rec)
    db.flush()

    stem = Path(filename).stem[:50]
    report_name = f'{stem}_{datetime.now(TZ).strftime("%Y%m%d_%H%M%S")}_{secrets.token_hex(3)}'
    report = ReportSet(import_id=upload_rec.id, name=report_name)
    db.add(report)
    db.flush()

    accepted = skipped_dup_db = 0
    days_seen: set[tuple[str, str]] = set()

    for code, stamp in valid:
        if (code, stamp) in existing:
            skipped_dup_db += 1
            continue
        local_day = stamp.astimezone(TZ).date().isoformat()
        db.add(Log(device_id=code, occurred_at=stamp, source_import_id=upload_rec.id))
        db.add(ReportPunch(
            report_id=report.id, device_id=code,
            local_day=local_day, occurred_at=stamp,
        ))
        if (code, local_day) not in days_seen:
            db.add(ReportDay(report_id=report.id, device_id=code, local_day=local_day))
            days_seen.add((code, local_day))
        accepted += 1

    upload_rec.accepted = accepted
    upload_rec.rejected = skipped_format
    audit(db, user, 'file_import_direct', f'{filename} accepted={accepted} auto_created={auto_created}')
    db.commit()

    parts = [f'{accepted} تردد وارد شد']
    if skipped_dup_db + skipped_dup_file:
        parts.append(f'{skipped_dup_db + skipped_dup_file} تکراری نادیده گرفته شد')
    if auto_created:
        parts.append(f'{auto_created} پرسنل موقت ایجاد شد')
    if skipped_format:
        parts.append(f'{skipped_format} سطر نامعتبر')
    return redirect('/imports', ' · '.join(parts))


def jalali(value):
    local = value.astimezone(TZ)
    j = jdatetime.date.fromgregorian(date=local.date())
    return f'{j.year:04d}/{j.month:02d}/{j.day:02d}', local.strftime('%H:%M:%S')


def to_jalali(day):
    value = jdatetime.date.fromgregorian(date=day)
    return f'{value.year:04d}/{value.month:02d}/{value.day:02d}'


def parse_jalali(value):
    normalized = value.translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')).replace('-', '/')
    match = re.fullmatch(r'(\d{4})/(\d{1,2})/(\d{1,2})', normalized)
    if not match:
        raise ValueError('فرمت تاریخ باید ۱۴۰۵/۰۸/۰۱ باشد')
    return jdatetime.date(*(int(x) for x in match.groups())).togregorian()


def range_jalali(start_jalali, end_jalali):
    try:
        start_day = parse_jalali(start_jalali)
        end_day = parse_jalali(end_jalali)
        if end_day < start_day:
            raise ValueError()
        return start_day, end_day
    except ValueError:
        raise HTTPException(400, 'بازه تاریخ شمسی نامعتبر است؛ تاریخ پایان نباید قبل از شروع باشد')


def jalali_cycle_label(day, anchor_day):
    """Non-overlapping Jalali cycles: [previous month day N, current month day N)."""
    j = jdatetime.date.fromgregorian(date=day)
    start_year, start_month = j.year, j.month
    if j.day < anchor_day:
        start_month -= 1
        if start_month == 0:
            start_month, start_year = 12, start_year - 1
    end_year = start_year + (start_month == 12)
    end_month = 1 if start_month == 12 else start_month + 1
    end_exclusive = jdatetime.date(end_year, end_month, anchor_day).togregorian()
    end_inclusive = end_exclusive - timedelta(days=1)
    return f'{start_year:04d}/{start_month:02d}/{anchor_day:02d} تا {to_jalali(end_inclusive)}'


def report_headers(period):
    if period == 'daily':
        return ['نام کارمند', 'شناسه دستگاه', 'تاریخ شمسی', 'اولین ثبت', 'آخرین ثبت', 'تعداد ثبت دستگاه', 'مدت حضور ثبت‌شده', 'مدت (دقیقه)']
    if period == 'daily_raw':
        return ['نام کارمند', 'شناسه دستگاه', 'تاریخ شمسی', 'شماره ثبت', 'ساعت ثبت']
    if period == 'daily_pairs':
        return ['نام کارمند', 'شناسه دستگاه', 'تاریخ شمسی', 'نوبت مشاهده‌ای', 'ثبت اول', 'ثبت دوم', 'فاصله (دقیقه)']
    return ['نام کارمند', 'شناسه دستگاه', 'دوره شمسی', 'روزهای ثبت‌شده', 'روزهای ناقص', 'مجموع حضور ثبت‌شده', 'مجموع (دقیقه)']


def pair_observed_punches(times):
    """Group consecutive observed events without claiming confirmed IN/OUT status.

    Deduplicate timestamps which may be present in multiple named report sets.
    An odd last event remains visible with a missing second timestamp.
    """
    unique_times = sorted(set(times))
    return [
        (unique_times[i], unique_times[i + 1] if i + 1 < len(unique_times) else None)
        for i in range(0, len(unique_times), 2)
    ]


def observed_duration_minutes(first, second):
    """Elapsed minutes between two observed punches; never infer IN/OUT status."""
    if second is None:
        return '—'
    elapsed = (second.astimezone(timezone.utc) - first.astimezone(timezone.utc)).total_seconds()
    return round(elapsed / 60, 2)


def observed_day_seconds(times):
    """First-to-last observed punches of one LOCAL day; no IN/OUT or breaks inferred.

    None means there are not two distinct observed timestamps, not zero work.
    Seconds are retained so aggregate rounding occurs once after summing days.
    """
    unique_times = sorted(set(times))
    if len(unique_times) < 2:
        return None
    return max(0, int((unique_times[-1].astimezone(timezone.utc) -
                       unique_times[0].astimezone(timezone.utc)).total_seconds()))


def duration_minutes(seconds):
    """Round accumulated nonnegative seconds to the nearest whole minute."""
    return (seconds + 30) // 60


def duration_label(seconds):
    minutes = duration_minutes(seconds)
    hours, remainder = divmod(minutes, 60)
    return f'{hours:02d}:{remainder:02d}'


def effective_day_window(times, correction=None):
    """Effective first/last for totals, without modifying observed punches.

    A one-punch day can be completed by overriding just the missing side.
    Diagnostic raw/pair views must never call this function.
    """
    observed = sorted(set(times))
    first = observed[0] if observed else None
    last = observed[-1] if len(observed) >= 2 else None
    if correction is not None and correction.action == 'set':
        # One recorded swipe is a potential IN or OUT, not automatically both.
        if len(observed) == 1:
            if correction.override_first is not None and correction.override_last is None:
                last = observed[0]
            if correction.override_last is not None and correction.override_first is None:
                first = observed[0]
        if correction.override_first is not None:
            first = correction.override_first.astimezone(TZ)
        if correction.override_last is not None:
            last = correction.override_last.astimezone(TZ)
    seconds = None
    if first is not None and last is not None:
        elapsed = int((last.astimezone(timezone.utc) - first.astimezone(timezone.utc)).total_seconds())
        if elapsed > 0:
            seconds = elapsed
    return first, last, seconds


def latest_corrections(db, start_iso, end_iso, employee_id=0):
    """One latest (possibly revoked) revision for each employee/local date."""
    stmt = select(AttendanceCorrection).where(
        AttendanceCorrection.local_day >= start_iso,
        AttendanceCorrection.local_day <= end_iso,
    ).order_by(AttendanceCorrection.id)
    if employee_id:
        stmt = stmt.where(AttendanceCorrection.employee_id == employee_id)
    result = {}
    for revision in db.scalars(stmt):
        result[(revision.employee_id, revision.local_day)] = revision
    return result


def absent_rows(db, start_jalali, end_jalali):
    """Employees who have no punch at all in the given range, per day."""
    start, end = range_jalali(start_jalali, end_jalali)
    # days that DO have a punch
    punched = {(r[0], r[1]) for r in db.execute(
        select(ReportDay.device_id, ReportDay.local_day).where(
            ReportDay.local_day >= start.isoformat(),
            ReportDay.local_day <= end.isoformat(),
        )
    ).all()}
    employees = db.scalars(select(Employee).where(Employee.status == 'active').order_by(Employee.name)).all()
    rows = []
    cur = start
    while cur <= end:
        day_iso = cur.isoformat()
        day_j = to_jalali(cur)
        for emp in employees:
            if (emp.device_id, day_iso) not in punched:
                rows.append((emp.name, emp.device_id, day_j))
        cur += timedelta(days=1)
    return rows


def on_leave_rows(db, start_jalali, end_jalali):
    """Rows for employees with a recorded leave in the given range."""
    start, end = range_jalali(start_jalali, end_jalali)
    leave_data = db.execute(
        select(LeaveDay.employee_id, LeaveDay.local_day, Employee.name, Employee.device_id)
        .join(Employee, Employee.id == LeaveDay.employee_id)
        .where(LeaveDay.local_day >= start.isoformat(), LeaveDay.local_day <= end.isoformat())
        .order_by(LeaveDay.local_day, Employee.name)
    ).all()
    return [(r[2], r[3], to_jalali(datetime.fromisoformat(r[1]).date())) for r in leave_data]


def incomplete_rows(db, start_jalali, end_jalali):
    """Employees who have only one punch (no exit or no entry) in a day."""
    start, end = range_jalali(start_jalali, end_jalali)
    # days with exactly 1 distinct punch time
    stmt = (
        select(ReportPunch.device_id, ReportPunch.local_day, Employee.name,
               func.count(ReportPunch.id).label('cnt'))
        .join(Employee, Employee.device_id == ReportPunch.device_id)
        .where(ReportPunch.local_day >= start.isoformat(), ReportPunch.local_day <= end.isoformat())
        .group_by(ReportPunch.device_id, ReportPunch.local_day, Employee.name)
        .having(func.count(ReportPunch.id) == 1)
        .order_by(ReportPunch.local_day, Employee.name)
    )
    return [(r[2], r[0], to_jalali(datetime.fromisoformat(r[1]).date())) for r in db.execute(stmt).all()]


def report_rows(db, start_jalali, end_jalali, report_id, period, employee_id=0, anchor_day=20):
    start, end = range_jalali(start_jalali, end_jalali)
    if period not in ('daily', 'daily_raw', 'daily_pairs', 'monthly', 'yearly', 'cycle', 'custom'):
        raise HTTPException(400, 'نوع گزارش نامعتبر است')
    if not 1 <= anchor_day <= 28:
        raise HTTPException(400, 'روز شروع دوره باید بین ۱ تا ۲۸ باشد')
    stmt = select(ReportDay.device_id, ReportDay.local_day, Employee.name).join(Employee, Employee.device_id == ReportDay.device_id).join(ReportSet, ReportSet.id == ReportDay.report_id).where(ReportDay.local_day >= start.isoformat(), ReportDay.local_day <= end.isoformat())
    if employee_id:
        employee = db.get(Employee, employee_id)
        if not employee:
            raise HTTPException(404, 'کارمند انتخاب‌شده پیدا نشد')
        stmt = stmt.where(ReportDay.device_id == employee.device_id)
    if report_id:
        if not db.get(ReportSet, report_id):
            raise HTTPException(404, 'گزارش انتخاب‌شده پیدا نشد')
        stmt = stmt.where(ReportDay.report_id == report_id)
    # Consolidate the same employee+day across multiple named uploads.
    unique_days = {(code, local_day, name) for code, local_day, name in db.execute(stmt)}
    corrections = latest_corrections(db, start.isoformat(), end.isoformat(), employee_id)
    # Explicit manual-only days are visible in the all-reports view. They do
    # not silently appear inside an unrelated named device upload.
    employee_by_id = {}
    if corrections and not report_id:
        employee_by_id = {emp.id: emp for emp in db.scalars(select(Employee).where(
            Employee.id.in_({emp_id for emp_id, _ in corrections})
        ))}
        for (emp_id, day), revision in corrections.items():
            if revision.action == 'set' and emp_id in employee_by_id:
                employee = employee_by_id[emp_id]
                unique_days.add((employee.device_id, day, employee.name))
    if not unique_days:
        return []
    # Use the same source-filtered punch set for daily, month, year, cycle and
    # arbitrary window. Set union removes duplicate uploads without removing
    # different swipes (which may be just seconds apart).
    punch_stmt = select(ReportPunch.device_id, ReportPunch.local_day, ReportPunch.occurred_at).where(
        ReportPunch.local_day >= start.isoformat(), ReportPunch.local_day <= end.isoformat()
    )
    if employee_id:
        punch_stmt = punch_stmt.where(ReportPunch.device_id == employee.device_id)
    if report_id:
        punch_stmt = punch_stmt.where(ReportPunch.report_id == report_id)
    day_keys = {(code, day) for code, day, _ in unique_days}
    punches = {}
    for code, day, stamp in db.execute(punch_stmt):
        if (code, day) in day_keys:
            punches.setdefault((code, day), set()).add(stamp.astimezone(TZ))
    # Correction identity is the employee primary key, never a display name.
    employee_ids = {emp.device_id: emp.id for emp in db.scalars(select(Employee).where(
        Employee.device_id.in_({code for code, _, _ in unique_days})
    ))}

    def window(code, day):
        revision = corrections.get((employee_ids[code], day))
        return effective_day_window(punches.get((code, day), ()), revision)

    if period in ('daily', 'daily_raw', 'daily_pairs'):
        rows = []
        for code, day, name in unique_days:
            times = sorted(punches.get((code, day), ()))
            day_text = to_jalali(datetime.fromisoformat(day).date())
            if period == 'daily_raw':
                for ordinal, stamp in enumerate(times, start=1):
                    rows.append([name, code, day_text, ordinal, stamp.strftime('%H:%M:%S')])
                if not times:
                    rows.append([name, code, day_text, 1, '—'])
            elif period == 'daily_pairs':
                observed_pairs = pair_observed_punches(times)
                for ordinal, (first, second) in enumerate(observed_pairs, start=1):
                    rows.append([name, code, day_text, ordinal, first.strftime('%H:%M:%S'), second.strftime('%H:%M:%S') if second else '—', observed_duration_minutes(first, second)])
                if not observed_pairs:
                    rows.append([name, code, day_text, 1, '—', '—', '—'])
            else:
                first, last, seconds = window(code, day)
                rows.append([name, code, day_text,
                             first.strftime('%H:%M:%S') if first is not None else '—',
                             last.strftime('%H:%M:%S') if last is not None else '—',
                             len(times), duration_label(seconds) if seconds is not None else '—',
                             duration_minutes(seconds) if seconds is not None else '—'])
        # Keep recent days first, but display sessions/events chronologically.
        if period in ('daily_raw', 'daily_pairs'):
            rows.sort(key=lambda row: row[3])
        rows.sort(key=lambda row: (row[2], row[0], row[1]), reverse=True)
        return rows
    counts = {}
    for code, day, name in unique_days:
        date_value = datetime.fromisoformat(day).date()
        j = to_jalali(date_value)
        if period == 'monthly':
            key = j[:7]
        elif period == 'yearly':
            key = j[:4]
        elif period == 'cycle':
            key = jalali_cycle_label(date_value, anchor_day)
        else:
            key = f'{to_jalali(start)} تا {to_jalali(end)}'
        summary = counts.setdefault((code, name, key), {'days': 0, 'incomplete': 0, 'seconds': 0})
        summary['days'] += 1
        _, _, seconds = window(code, day)
        if seconds is None:
            summary['incomplete'] += 1
        else:
            summary['seconds'] += seconds
    return sorted([
        [name, code, key, value['days'], value['incomplete'],
         duration_label(value['seconds']), duration_minutes(value['seconds'])]
        for (code, name, key), value in counts.items()
    ], key=lambda row: (row[2], row[0], row[1]), reverse=True)


def report_filters(db):
    return db.execute(select(ReportSet.id, ReportSet.name).order_by(ReportSet.id.desc())).all()


def correction_day(day_jalali):
    try:
        day = parse_jalali(day_jalali)
    except (ValueError, TypeError):
        raise HTTPException(400, 'تاریخ شمسی معتبر نیست')
    if day > datetime.now(TZ).date():
        raise HTTPException(400, 'اصلاح تردد برای روزهای آینده مجاز نیست')
    return day


def correction_clock(day, value):
    value = value.strip().translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'))
    if not value:
        return None
    if not re.fullmatch(r'\d{2}:\d{2}(?::\d{2})?', value):
        raise HTTPException(400, 'ساعت باید به صورت HH:MM یا HH:MM:SS باشد')
    try:
        fields = [int(part) for part in value.split(':')]
        clock = time(*fields, tzinfo=TZ)
    except ValueError:
        raise HTTPException(400, 'ساعت انتخابی معتبر نیست')
    return datetime.combine(day, clock).astimezone(timezone.utc)


def correction_reason(value):
    result = value.strip()
    if not 5 <= len(result) <= 500:
        raise HTTPException(400, 'ثبت دلیل اصلاح با حداقل ۵ و حداکثر ۵۰۰ نویسه الزامی است')
    return result


def correction_revisions(db, employee_id, day):
    return db.scalars(select(AttendanceCorrection).where(
        AttendanceCorrection.employee_id == employee_id,
        AttendanceCorrection.local_day == day,
    ).order_by(AttendanceCorrection.id.desc()).limit(100)).all()


def source_day_punches(db, device_id, day):
    return sorted({stamp.astimezone(TZ) for stamp in db.scalars(
        select(ReportPunch.occurred_at).where(
            ReportPunch.device_id == device_id, ReportPunch.local_day == day,
        )
    )})


def correction_redirect(employee_id, day_jalali, message):
    from urllib.parse import urlencode
    return RedirectResponse('/corrections?' + urlencode({
        'employee_id': employee_id, 'day_jalali': day_jalali, 'message': message,
    }), status_code=303)


def manager_attendance_redirect(employee_id, day_jalali, message):
    from urllib.parse import urlencode
    return RedirectResponse('/managers/attendance?' + urlencode({
        'employee_id': employee_id, 'day_jalali': day_jalali, 'message': message,
    }), status_code=303)


def eligible_manual_manager(db: Session, employee_id: int, *, lock: bool = False):
    stmt = select(Employee).where(Employee.id == employee_id)
    if lock:
        stmt = stmt.with_for_update()
    employee = db.scalar(stmt)
    if employee is None or employee.status != 'active' or employee.attendance_group != 'management':
        raise HTTPException(404, 'این کارمند در گروه مدیرانِ فعال قرار ندارد')
    return employee


@app.get('/managers/attendance')
def managers_attendance_page(request: Request, employee_id: int = 0, day_jalali: str = '',
                             db: Session = Depends(get_db)):
    """Manager-only attendance workbench; admins commit directly, secretaries use legacy approvals."""
    user = permit(request, db, 'admin', 'secretary')
    is_admin = user.role == 'admin'
    day_jalali = day_jalali or to_jalali(datetime.now(TZ).date())
    day = correction_day(day_jalali)
    managers = db.scalars(select(Employee).where(
        Employee.attendance_group == 'management', Employee.status == 'active',
    ).order_by(Employee.name, Employee.id).limit(500)).all()
    token = csrf_token(request)
    options = '<option value="">مدیر موردنظر را انتخاب کنید</option>' + ''.join(
        f'<option value="{emp.id}" {"selected" if emp.id == employee_id else ""}>'
        f'{escape(emp.name)} · {escape(emp.device_id)}</option>' for emp in managers
    )
    body = msg(request) + (
        '<div class="card"><h2>ثبت ورود و خروج مدیران</h2>'
        '<div class="manager-callout">این بخش مخصوص مدیرانی است که موظف به ثبت اثر انگشت نیستند. '
        + ('ادمین تاریخ شمسی و ساعات ورود و خروج را ثبت می‌کند؛ '
           '<strong>اطلاعات بلافاصله در گزارش‌های نهایی اعمال می‌شوند و تأیید دیگری لازم نیست.</strong> '
           if is_admin else
           'منشی برای افراد تعیین‌شده توسط ادمین، تاریخ شمسی و دو ساعت ورود و خروج را پیشنهاد می‌کند. '
           '<strong>تا تأیید ادمین، هیچ تغییری در گزارش‌های نهایی ایجاد نمی‌شود.</strong> ')
        + 'ثبت خام دستگاه و سوابق قبلی حذف یا تغییر نمی‌شوند؛ همهٔ نسخه‌ها و نام ثبت‌کننده در تاریخچه می‌مانند.</div>'
        '<p><a class="btn outline" href="/managers/attendance/bulk">ثبت چند مدیر به‌صورت گروهی ←</a></p>'
        '<p><a class="btn outline" href="/managers/attendance/bulk#manager-excel-import">ثبت تعداد زیاد با Excel ←</a></p>'
        '<form method="get" action="/managers/attendance" class="fields">'
        f'<div><label for="manager-employee">نام مدیر</label><select id="manager-employee" name="employee_id" required>{options}</select></div>'
        f'<div><label for="manager-day">تاریخ شمسی</label><input id="manager-day" name="day_jalali" '
        f'value="{escape(day_jalali)}" inputmode="numeric" placeholder="۱۴۰۵/۰۸/۲۵" required></div>'
        '<div><button>نمایش و ثبت روز انتخابی</button></div></form></div>'
    )
    if not managers:
        body += '<div class="card"><p class="muted">هنوز مدیر فعالی تعریف نشده است. ادمین باید در بخش پرسنل، گروه تردد فرد را «مدیران · ثبت دستی» قرار دهد.</p></div>'
    if employee_id == 0:
        return page('ثبت تردد مدیران', body, request, db, 'managers')
    employee = eligible_manual_manager(db, employee_id)
    revisions = correction_revisions(db, employee.id, day.isoformat())
    current = revisions[0] if revisions else None
    active = current if current and current.action == 'set' else None
    original = source_day_punches(db, employee.device_id, day.isoformat())
    locked = bool(original or (current and current.origin != 'manager_manual'))
    pending = db.scalar(select(ManagerAttendanceRequest).where(
        ManagerAttendanceRequest.employee_id == employee.id,
        ManagerAttendanceRequest.local_day == day.isoformat(),
        ManagerAttendanceRequest.status == 'pending',
    ))
    first = active.override_first.astimezone(TZ).strftime('%H:%M:%S') if active and active.override_first else ''
    last = active.override_last.astimezone(TZ).strftime('%H:%M:%S') if active and active.override_last else ''
    status = ('در انتظار تأیید ادمین' if pending else
              ('ثبت مستقیم فعال' if is_admin else 'ثبت دستی تأییدشده فعال') if active and active.origin == 'manager_manual' else
              'ثبت دستی لغو شده' if current and current.origin == 'manager_manual' else
              'این تاریخ اصلاح اداری دارد' if current else 'هنوز ترددی ثبت نشده')
    body += (
        '<div class="card"><h2>اطلاعات روز انتخابی</h2>'
        f'<p><strong>{escape(employee.name)}</strong> · <bdi>{escape(day_jalali)}</bdi></p>'
        f'<div class="manager-preview"><span>وضعیت: {escape(status)}</span>'
        f'<span>ورود: <bdi>{escape(first or "—")}</bdi></span>'
        f'<span>خروج: <bdi>{escape(last or "—")}</bdi></span></div>'
    )
    if locked:
        body += ('<p class="muted">این تاریخ دارای تردد دستگاهی یا اصلاح اداری دیگری است. '
                 'برای جلوگیری از تداخل، ثبت مدیران در این بخش مجاز نیست؛ ادمین از «اصلاح تردد» پیگیری کند.</p>')
    elif pending:
        body += (
            f'<p class="muted">درخواست شماره <bdi>#{pending.id}</bdi> با ورود '
            f'<bdi>{pending.proposed_first.astimezone(TZ).strftime("%H:%M:%S")}</bdi> و خروج '
            f'<bdi>{pending.proposed_last.astimezone(TZ).strftime("%H:%M:%S")}</bdi> '
            'در صف بررسی ادمین است. ثبت دوباره برای همین روز تا تعیین تکلیف مجاز نیست.</p>'
        )
        if pending.requested_by_id == user.id:
            body += (
                '<form method="post" action="/managers/attendance/cancel" class="fields">'
                f'<input type="hidden" name="csrf_token" value="{escape(token)}">'
                f'<input type="hidden" name="request_id" value="{pending.id}">'
                f'<div><label for="cancel-reason">دلیل پس‌گرفتن درخواست</label><input id="cancel-reason" name="reason" minlength="5" maxlength="500" required></div>'
                '<div><button type="submit" class="outline">انصراف از درخواستِ در انتظار</button></div></form>'
            )
    else:
        body += (
            '<p class="muted">ورود و خروج هر دو الزامی‌اند؛ ساعت خروج باید بعد از ورود و مربوط به همین روز باشد. '
            + ('برای اصلاح ثبت قبلی، رکورد جدید با تاریخچه و نام ادمین ذخیره می‌شود.</p>'
               if is_admin else
               'برای اصلاح ثبت تأییدشده قبلی، درخواست جدید بفرستید. فقط تأیید ادمین نتیجهٔ گزارش را عوض می‌کند.</p>')
            + '<form method="post" action="/managers/attendance/submit" class="fields">'
            f'<input type="hidden" name="csrf_token" value="{escape(token)}">'
            f'<input type="hidden" name="employee_id" value="{employee.id}">'
            f'<input type="hidden" name="day_jalali" value="{escape(day_jalali)}">'
            f'<input type="hidden" name="expected_revision" value="{current.id if current else 0}">'
            f'<div><label for="manager-first">ساعت ورود</label><input id="manager-first" name="first_clock" '
            f'value="{escape(first)}" maxlength="8" placeholder="08:00" inputmode="numeric" required></div>'
            f'<div><label for="manager-last">ساعت خروج</label><input id="manager-last" name="last_clock" '
            f'value="{escape(last)}" maxlength="8" placeholder="16:00" inputmode="numeric" required></div>'
            '<div><label for="manager-reason">توضیح ثبت</label><input id="manager-reason" name="reason" '
            'minlength="5" maxlength="500" placeholder="ثبت بر اساس اعلام مدیر" required></div>'
            + ('<div><button>ثبت مستقیم در گزارش</button></div></form>' if is_admin else
               '<div><button>ارسال برای تأیید ادمین</button></div></form>')
        )
        if is_admin and active and active.origin == 'manager_manual':
            body += (
                '<form method="post" action="/managers/attendance/revoke" class="fields">'
                f'<input type="hidden" name="csrf_token" value="{escape(token)}">'
                f'<input type="hidden" name="employee_id" value="{employee.id}">'
                f'<input type="hidden" name="day_jalali" value="{escape(day_jalali)}">'
                f'<input type="hidden" name="expected_revision" value="{current.id}">'
                '<div><label>دلیل لغو ثبت</label><input name="reason" minlength="5" maxlength="500" required></div>'
                '<div><button class="outline">لغو ثبت مستقیم (با حفظ تاریخچه)</button></div></form>'
            )
    body += '</div>'
    history = ''
    for revision in revisions:
        editor = db.get(User, revision.actor_id)
        moment = revision.created_at.astimezone(TZ)
        history += '<tr>' + ''.join(f'<td>{escape(value)}</td>' for value in (
            revision.id,
            'لغو' if revision.action == 'revoke' else 'ثبت یا اصلاح',
            revision.override_first.astimezone(TZ).strftime('%H:%M:%S') if revision.override_first else '—',
            revision.override_last.astimezone(TZ).strftime('%H:%M:%S') if revision.override_last else '—',
            revision.reason, editor.username if editor else f'#{revision.actor_id}',
            to_jalali(moment.date()) + ' ' + moment.strftime('%H:%M:%S'),
        )) + '</tr>'
    body += (
        '<div class="card table-wrap"><h2>تاریخچه این مدیر در روز انتخابی</h2>'
        '<table><thead><tr><th>نسخه</th><th>عملیات</th><th>ورود</th><th>خروج</th>'
        '<th>توضیح</th><th>ثبت‌کننده</th><th>زمان ثبت</th></tr></thead>'
        f'<tbody>{history or "<tr><td colspan=7>هنوز سابقه‌ای وجود ندارد.</td></tr>"}</tbody></table></div>'
    )
    requests = db.scalars(select(ManagerAttendanceRequest).where(
        ManagerAttendanceRequest.employee_id == employee.id,
        ManagerAttendanceRequest.local_day == day.isoformat(),
    ).order_by(ManagerAttendanceRequest.id.desc()).limit(100)).all()
    request_rows = ''
    labels = {'pending': 'در انتظار تأیید', 'approved': 'تأییدشده',
              'rejected': 'ردشده', 'cancelled': 'پس‌گرفته‌شده'}
    for item in requests:
        requester = db.get(User, item.requested_by_id)
        reviewer = db.get(User, item.resolved_by_id) if item.resolved_by_id else None
        request_rows += '<tr>' + ''.join(f'<td>{escape(value)}</td>' for value in (
            item.id, labels.get(item.status, item.status),
            item.proposed_first.astimezone(TZ).strftime('%H:%M:%S'),
            item.proposed_last.astimezone(TZ).strftime('%H:%M:%S'),
            requester.username if requester else f'#{item.requested_by_id}',
            item.reason, reviewer.username if reviewer else '—', item.resolution_reason or '—',
        )) + '</tr>'
    body += (
        ('<div class="card table-wrap"><h2>درخواست‌های دومرحله‌ای قدیمی</h2>' if is_admin else
           '<div class="card table-wrap"><h2>تاریخچه درخواست‌ها و تصمیم‌های ادمین</h2>')
        + '<table><thead><tr><th>شناسه</th><th>وضعیت</th><th>ورود</th><th>خروج</th>'
        '<th>منشی</th><th>دلیل</th><th>تصمیم‌گیرنده</th><th>دلیل تصمیم</th></tr></thead>'
        f'<tbody>{request_rows or "<tr><td colspan=8>درخواستی ثبت نشده است.</td></tr>"}</tbody></table></div>'
    )
    return page('ثبت تردد مدیران', body, request, db, 'managers')


@app.get('/managers/attendance/bulk')
def managers_bulk_attendance_page(request: Request, q: str = '', db: Session = Depends(get_db)):
    """Manager-day batch form; admin writes immediately, secretary submits legacy requests."""
    user = permit(request, db, 'admin', 'secretary')
    is_admin = user.role == 'admin'
    q = q.strip()
    if len(q) > 80:
        raise HTTPException(400, 'متن جستجو نباید بیشتر از ۸۰ نویسه باشد')
    stmt = select(Employee).where(Employee.attendance_group == 'management',
                                  Employee.status == 'active')
    if q:
        stmt = stmt.where(Employee.name.ilike('%' + q + '%') |
                          Employee.device_id.ilike('%' + q + '%'))
    managers = db.scalars(stmt.order_by(Employee.name, Employee.id).limit(100)).all()
    today = to_jalali(datetime.now(TZ).date())
    token = csrf_token(request)
    bulk_note = ('<strong>ثبت گروهی توسط ادمین بلافاصله در گزارش‌ها اعمال می‌شود.</strong> '
                 if is_admin else
                 '<strong>ارسال گروهی به معنی تأیید گروهی نیست:</strong> همهٔ درخواست‌ها جداگانه در صف ادمین می‌مانند '
                 'و تا تأیید او در گزارش نهایی محاسبه نمی‌شوند. ')
    excel_note = ('<strong>در مرحلهٔ پیش‌نمایش هیچ اطلاعاتی ذخیره نمی‌شود.</strong> '
                  'پس از انتخاب ردیف‌ها و تأیید نهایی، ادمین آن‌ها را مستقیم در گزارش ثبت می‌کند. '
                  if is_admin else
                  '<strong>در مرحلهٔ پیش‌نمایش هیچ اطلاعاتی ذخیره نمی‌شود.</strong> '
                  'پس از تأیید، ردیف‌های انتخاب‌شده به صف بررسی ادمین می‌روند. ')
    excel_button = 'بررسی فایل و نمایش پیش‌نمایش Excel'
    bulk_button = ('ثبت مستقیم انتخاب‌شده‌ها' if is_admin else 'ارسال انتخاب‌شده‌ها به ادمین')
    bulk_status = ('ثبت نهایی بلافاصله انجام می‌شود؛ هر رکورد در تاریخچه قابل پیگیری است.'
                   if is_admin else 'ثبت نهایی صرفاً پس از بررسی و تأیید ادمین انجام می‌شود.')
    select_all_disabled = ' disabled' if len(managers) > 25 or not managers else ''
    select_all_help = ('برای انتخاب همه، جستجو را به حداکثر ۲۵ مدیر محدود کنید.'
                       if len(managers) > 25 else 'با این گزینه همهٔ مدیران نمایش‌داده‌شده انتخاب می‌شوند.')
    rows = ''
    for emp in managers:
        rowid = str(emp.id)
        rows += (
            '<tr>'
            f'<td><label><input type="checkbox" name="employee_ids" value="{rowid}" '
            f'aria-label="انتخاب {escape(emp.name)}"></label></td>'
            f'<td><strong>{escape(emp.name)}</strong><br><small><bdi>{escape(emp.device_id)}</bdi></small></td>'
            f'<td><label class="sr-only" for="bulk-day-{rowid}">تاریخ شمسی {escape(emp.name)}</label>'
            f'<input id="bulk-day-{rowid}" name="day_{rowid}" type="text" '
            f'value="{escape(today)}" inputmode="numeric" maxlength="10" placeholder="۱۴۰۵/۰۸/۲۵"></td>'
            f'<td><label class="sr-only" for="bulk-first-{rowid}">ورود {escape(emp.name)}</label>'
            f'<input id="bulk-first-{rowid}" name="first_{rowid}" type="text" '
            'maxlength="8" inputmode="numeric" placeholder="08:00"></td>'
            f'<td><label class="sr-only" for="bulk-last-{rowid}">خروج {escape(emp.name)}</label>'
            f'<input id="bulk-last-{rowid}" name="last_{rowid}" type="text" '
            'maxlength="8" inputmode="numeric" placeholder="17:00"></td>'
            f'<td><label class="sr-only" for="bulk-reason-{rowid}">توضیح {escape(emp.name)}</label>'
            f'<input id="bulk-reason-{rowid}" name="reason_{rowid}" type="text" '
            'maxlength="500" placeholder="بر اساس اعلام مدیر"></td>'
            '</tr>'
        )
    body = msg(request) + (
        '<div class="card"><h2>ثبت گروهی تردد مدیران: Excel یا دستی</h2>'
        '<div class="manager-callout">مدیران موردنظر را انتخاب کنید و برای <strong>هر نفر</strong> تاریخ شمسی، '
        'ساعت ورود، ساعت خروج و دلیل مستقل بنویسید. حداکثر ۲۵ مدیر در یک نوبت ثبت می‌شوند. '
        + bulk_note
        + 'اگر اطلاعات حتی یک نفر نامعتبر یا متداخل باشد، هیچ‌کدام از درخواست‌های همان نوبت ثبت نمی‌شود.</div>'
        '<p><a class="btn outline" href="/managers/attendance">بازگشت به ثبت تکی</a></p>'
        '<form method="get" action="/managers/attendance/bulk" class="fields">'
        f'<div><label for="bulk-search">جستجوی مدیر با نام یا کد</label><input '
        f'id="bulk-search" type="search" name="q" maxlength="80" value="{escape(q)}" '
        'placeholder="نام یا شمارهٔ اختصاصی"></div>'
        '<div><button class="outline">جستجو</button></div></form>'
        '<p class="muted">تا ۱۰۰ نتیجهٔ جستجو نمایش داده می‌شود. فقط سطرهای تیک‌خورده ارسال می‌شوند؛ '
        'ثبت گروهی فقط برای تاریخ‌های بدون تردد دستگاه یا اصلاح قبلی است.</p></div>'
        '<div class="card" id="manager-excel-import"><h2>ورود گروهی با فایل Excel</h2>'
        '<div class="manager-callout">برای چند مدیر در چند روز (مثلاً ۵ مدیر در ۳۰ روز)، '
        'قالب Excel را دانلود و برای هر مدیر و هر تاریخ، یک ردیف کامل کنید. '
        'شناسهٔ مدیر باید همان کد اختصاصی کنار نام او در سامانه باشد. '
        + excel_note
        + 'در پیش‌نمایش، ردیف‌های ازقبل‌ثبت‌شده یا متداخل مشخص می‌شوند. '
        'در مرحلهٔ تأیید نهایی، اگر حتی یکی از ردیف‌های انتخابی متداخل شود، هیچ ردیف انتخابی ذخیره نخواهد شد. '
        '<strong>برای ۱۵۰ ردیف Excel، نیازی به تیک‌زدن در جدول پایین نیست.</strong> '
        'بعد از انتخاب فایل، دکمهٔ پیش‌نمایش را بزنید؛ سپس در جدول اسکرول‌دار ردیف‌های موردنظر را انتخاب کنید. '
        'جدول پایین برای ثبت دستیِ جداگانه است.</div>'
        '<p><a class="btn outline" href="/managers/attendance/excel-template">دریافت قالب Excel</a></p>'
        '<form method="post" action="/managers/attendance/excel-upload" '
        'enctype="multipart/form-data" class="fields">'
        f'<input type="hidden" name="csrf_token" value="{escape(token)}">'
        '<div><label for="manager-excel-file">فایل تکمیل‌شده (xlsx)</label>'
        '<input id="manager-excel-file" type="file" name="file" accept=".xlsx" required></div>'
        f'<div><button type="submit">{excel_button}</button></div>'
        '</form><p class="muted">حداکثر ۱۰۰۰ ردیف و ۲ مگابایت در هر فایل. '
        'از Excel بدون فرمول یا ماکرو استفاده کنید؛ تاریخ شمسی و ساعت‌ها به شکل متن باشند.</p></div>'
        '<div class="card table-wrap"><h2>ثبت دستی یک روز برای چند مدیر (حداکثر ۲۵ نفر)</h2>'
        '<p class="muted">این جدول مربوط به فایل Excel بارگذاری‌شده نیست. '
        'متن کم‌رنگ داخل فیلدها مثال است؛ برای ثبت باید ساعت ورود، خروج و دلیل واقعی وارد کنید.</p>'
        '<form id="manager-bulk-form" method="post" action="/managers/attendance/bulk">'
        f'<input type="hidden" name="csrf_token" value="{escape(token)}">'
        '<div class="manager-bulk-shared">'
        '<div><label for="manager-bulk-common-day">تاریخ شمسی مشترک (اختیاری)</label>'
        '<input id="manager-bulk-common-day" type="text" maxlength="10" inputmode="numeric" placeholder="1405/08/25"></div>'
        '<div><label for="manager-bulk-common-first">ورود مشترک</label>'
        '<input id="manager-bulk-common-first" type="text" maxlength="8" inputmode="numeric" placeholder="08:00"></div>'
        '<div><label for="manager-bulk-common-last">خروج مشترک</label>'
        '<input id="manager-bulk-common-last" type="text" maxlength="8" inputmode="numeric" placeholder="17:00"></div>'
        '<div><label for="manager-bulk-common-reason">دلیل مشترک</label>'
        '<input id="manager-bulk-common-reason" type="text" maxlength="500" placeholder="براساس اعلام مدیر"></div>'
        '<div><button type="button" id="manager-bulk-apply-shared" class="outline">اعمال روی انتخاب‌شده‌ها</button></div>'
        '</div>'
        '<div class="table-wrap"><table class="manager-bulk-table"><thead><tr>'
        f'<th><label class="manager-bulk-select-label"><input type="checkbox" id="manager-bulk-select-all" '
        f'aria-label="انتخاب همه مدیران نمایش‌داده‌شده" title="{escape(select_all_help)}"{select_all_disabled}>'
        '<span>انتخاب همه</span></label></th><th>نام مدیر</th><th>تاریخ شمسی</th><th>ساعت ورود</th>'
        '<th>ساعت خروج</th><th>دلیل ثبت</th></tr></thead>'
        f'<tbody>{rows or "<tr><td colspan=6>مدیر فعالی با این مشخصات پیدا نشد.</td></tr>"}</tbody>'
        '</table></div><p id="manager-bulk-count" class="muted">'
        f'۰ نفر از {len(managers)} مدیر انتخاب شده‌اند (حداکثر ۲۵ نفر).</p>'
        '<p id="manager-bulk-feedback" class="manager-bulk-feedback" role="status" aria-live="polite"></p>'
        f'<div class="manager-bulk-actions"><button type="submit">{bulk_button}</button>'
        f'<span class="muted">{bulk_status}</span>'
        '</div></form></div>'
    )
    return page('ثبت گروهی مدیران', body, request, db, 'managers_bulk')


@app.get('/managers/attendance/excel-template')
def download_manager_excel_template(request: Request, db: Session = Depends(get_db)):
    """Serve an employee-free, static template only to permitted operators."""
    permit(request, db, 'admin', 'secretary')
    return FileResponse(
        Path(__file__).resolve().parent / 'templates' / 'manager-attendance-template.xlsx',
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        filename='manager-attendance-template.xlsx',
    )


EXCEL_PREVIEW_TTL_SECONDS = 20 * 60
EXCEL_PREVIEW_MAX_TOKEN_CHARS = 1_000_000


def manager_excel_preview_signer():
    """Stateless, session-bound preview: no server-side temporary file or cookie payload."""
    return URLSafeTimedSerializer(
        env('SESSION_SECRET', ''), salt='attendance-manager-excel-preview-r7',
        signer_kwargs={'digest_method': hashlib.sha256},
    )


def prepare_manager_excel_entries(db: Session, entries: list[ManagerExcelRow]):
    """Validate exact manager identity and all day/time/reason fields without writing."""
    codes = {item.device_id for item in entries}
    matches = db.scalars(select(Employee).where(Employee.device_id.in_(codes))).all()
    by_code = {emp.device_id: emp for emp in matches}
    prepared = []
    for item in entries:
        employee = by_code.get(item.device_id)
        if employee is None or employee.status != 'active' or employee.attendance_group != 'management':
            raise HTTPException(400, f'ردیف {item.excel_row}: کد متعلق به مدیر فعال نیست')
        if item.name and item.name != employee.name:
            raise HTTPException(400, f'ردیف {item.excel_row}: نام مدیر با کد اختصاصی او مطابقت ندارد')
        try:
            day = correction_day(item.day_jalali)
            first = correction_clock(day, item.first_clock)
            last = correction_clock(day, item.last_clock)
            reason = correction_reason(item.reason)
        except HTTPException as exc:
            raise HTTPException(400, f'ردیف {item.excel_row}: {exc.detail}') from None
        if first is None or last is None or first >= last:
            raise HTTPException(400, f'ردیف {item.excel_row}: ساعت خروج باید بعد از ساعت ورود باشد')
        prepared.append((employee, day.isoformat(), first, last, reason, item.excel_row))
    return prepared


def manager_excel_preview_conflicts(db: Session, prepared):
    """Set-based conflict hints; commit still rechecks everything with row locks."""
    if not prepared:
        return {}
    ids = {emp.id for emp, *_ in prepared}
    codes = {emp.device_id for emp, *_ in prepared}
    days = {day for _, day, *_ in prepared}
    corrected = set(db.execute(select(
        AttendanceCorrection.employee_id, AttendanceCorrection.local_day,
    ).where(AttendanceCorrection.employee_id.in_(ids),
            AttendanceCorrection.local_day.in_(days))).all())
    pending = set(db.execute(select(
        ManagerAttendanceRequest.employee_id, ManagerAttendanceRequest.local_day,
    ).where(ManagerAttendanceRequest.employee_id.in_(ids),
            ManagerAttendanceRequest.local_day.in_(days),
            ManagerAttendanceRequest.status == 'pending')).all())
    punched = set(db.execute(select(
        ReportPunch.device_id, ReportPunch.local_day,
    ).where(ReportPunch.device_id.in_(codes), ReportPunch.local_day.in_(days))).all())
    statuses = {}
    for emp, day, _, _, _, row_number in prepared:
        if (emp.device_id, day) in punched:
            statuses[row_number] = 'دارای ثبت دستگاهی'
        elif (emp.id, day) in corrected:
            statuses[row_number] = 'قبلاً ثبت یا اصلاح شده'
        elif (emp.id, day) in pending:
            statuses[row_number] = 'در انتظار تأیید ادمین'
    return statuses


def render_manager_excel_preview(request, db, user, entries, prepared, digest):
    """Show ALL parsed rows in a contained scroll pane; no database mutation."""
    conflicts = manager_excel_preview_conflicts(db, prepared)
    preview_rows = [
        [item.excel_row, item.device_id, item.name, item.day_jalali,
         item.first_clock, item.last_clock, item.reason]
        for item in entries
    ]
    payload = {
        'uid': user.id, 'auth_tag': session_auth_tag(user),
        'csrf': csrf_token(request), 'digest': digest,
        'rows': preview_rows,
    }
    signed = manager_excel_preview_signer().dumps(payload)
    if len(signed) > EXCEL_PREVIEW_MAX_TOKEN_CHARS:
        raise HTTPException(413, 'حجم اطلاعات پیش‌نمایش بیش از حد مجاز است')
    html_rows = []
    for item, (employee, *_prepared) in zip(entries, prepared):
        blocked = conflicts.get(item.excel_row)
        check = (f'<input type="checkbox" name="selected_rows" value="{item.excel_row}" '
                 f'aria-label="انتخاب ردیف {item.excel_row}، {escape(employee.name)}"'
                 + (' disabled' if blocked else '') + '>')
        status = (f'<span class="manager-excel-status unavailable">{escape(blocked)}</span>'
                  if blocked else '<span class="manager-excel-status">آمادهٔ ثبت</span>')
        html_rows.append(
            f'<tr{(" class=excel-unavailable" if blocked else "")}>'
            f'<td>{check}</td><td><bdi>{item.excel_row}</bdi></td>'
            f'<td>{escape(employee.name)}<br><small><bdi>{escape(employee.device_id)}</bdi></small></td>'
            f'<td><bdi>{escape(item.day_jalali)}</bdi></td>'
            f'<td><bdi>{escape(item.first_clock)}</bdi></td>'
            f'<td><bdi>{escape(item.last_clock)}</bdi></td>'
            f'<td class="excel-row-reason">{escape(item.reason)}</td><td>{status}</td></tr>'
        )
    available = len(entries) - len(conflicts)
    action = ('ثبت مستقیم ردیف‌های انتخاب‌شده در گزارش‌ها' if user.role == 'admin'
              else 'ارسال ردیف‌های انتخاب‌شده به صف ادمین')
    body = (
        '<div class="card"><h2>پیش‌نمایش Excel مدیران</h2>'
        '<div class="manager-callout"><strong>هنوز هیچ ردیفی ثبت نشده است.</strong> '
        f'این فایل {len(entries)} ردیف دارد؛ {available} ردیف قابل ثبت است و '
        f'{len(conflicts)} ردیف از قبل ثبت شده یا تداخل دارد. '
        'فقط ردیف‌های قابل ثبت را انتخاب کنید. در صورت تداخل هنگام ثبت، کل انتخاب‌ها رد می‌شود.</div>'
        '<p><a class="btn outline" href="/managers/attendance/bulk#manager-excel-import">'
        'بازگشت و انتخاب فایل جدید</a></p>'
        '<form id="manager-excel-confirm-form" method="post" action="/managers/attendance/excel-commit">'
        f'<input type="hidden" name="csrf_token" value="{escape(csrf_token(request))}">'
        f'<input type="hidden" name="preview_token" value="{escape(signed)}">'
        '<div class="manager-excel-controls">'
        f'<label class="manager-bulk-select-label"><input id="manager-excel-select-all" type="checkbox"'
        f'{" disabled" if not available else ""}> انتخاب همهٔ ردیف‌های قابل ثبت</label>'
        f'<span class="manager-excel-count" id="manager-excel-selection-count">'
        f'۰ ردیف از {available} ردیف قابل ثبت انتخاب شده است.</span></div>'
        '<div class="manager-excel-scroll" tabindex="0" role="region" '
        'aria-label="پیش‌نمایش اسکرول‌دار ردیف‌های فایل Excel">'
        '<table class="manager-excel-table"><thead><tr>'
        '<th>انتخاب</th><th>ردیف Excel</th><th>نام و کد مدیر</th>'
        '<th>تاریخ شمسی</th><th>ورود</th><th>خروج</th><th>دلیل</th><th>وضعیت</th>'
        '</tr></thead><tbody>' + ''.join(html_rows) + '</tbody></table></div>'
        '<p class="manager-bulk-feedback error" id="manager-excel-selection-error" '
        'role="alert" aria-live="polite"></p>'
        f'<div class="manager-bulk-actions"><button type="submit"'
        f'{" disabled" if not available else ""}>{action}</button>'
        '<span class="muted">لطفاً اطلاعات را قبل از تأیید نهایی بررسی کنید. '
        'پیش‌نمایش پس از ۲۰ دقیقه منقضی می‌شود.</span></div>'
        '</form></div>'
    )
    return page('پیش‌نمایش Excel مدیران', body, request, db, 'managers_bulk')


@app.post('/managers/attendance/excel-upload')
async def upload_manager_excel(request: Request, db: Session = Depends(get_db)):
    """Parse and preview a bounded workbook. This endpoint NEVER commits rows."""
    # Authorize before multipart parsing/spooling any untrusted file bytes.
    user = permit(request, db, 'admin', 'secretary')
    content_length = request.headers.get('content-length')
    if content_length and content_length.isdecimal() and int(content_length) > MAX_XLSX_BYTES + 16384:
        raise HTTPException(413, 'حجم درخواست بارگذاری بیش از حد مجاز است')
    try:
        async with request.form(max_files=1, max_fields=1, max_part_size=MAX_XLSX_BYTES + 4096) as form:
            token = form.get('csrf_token')
            csrf(request, token if isinstance(token, str) else '')
            file = form.get('file')
            if not isinstance(file, StarletteUploadFile):
                raise HTTPException(400, 'فایل Excel انتخاب نشده است')
            if not file.filename or not file.filename.lower().endswith('.xlsx'):
                raise HTTPException(400, 'فقط فایل با پسوند xlsx مجاز است')
            raw = await file.read(MAX_XLSX_BYTES + 1)
    except ValueError:
        raise HTTPException(400, 'ساختار بارگذاری فایل معتبر نیست') from None
    try:
        entries = parse_manager_excel(raw)
    except ManagerExcelValidationError as exc:
        raise HTTPException(400, str(exc)) from None
    prepared = prepare_manager_excel_entries(db, entries)
    return render_manager_excel_preview(
        request, db, user, entries, prepared, hashlib.sha256(raw).hexdigest()[:16])


def commit_manager_excel_prepared(db, user, prepared, digest):
    """All-or-nothing commit of a selected subset, with fresh locks and conflict checks."""
    is_admin = user.role == 'admin'
    # One lock per manager, in stable order; every manager-day must be available.
    for employee_id in sorted({emp.id for emp, *_ in prepared}):
        eligible_manual_manager(db, employee_id, lock=True)
    for employee, day, _, _, _, row_number in prepared:
        if source_day_punches(db, employee.device_id, day):
            raise HTTPException(409, f'ردیف {row_number}: این روز تردد دستگاهی دارد؛ هیچ ردیفی ثبت نشد')
        if correction_revisions(db, employee.id, day):
            raise HTTPException(409, f'ردیف {row_number}: سابقهٔ اصلاح قبلی دارد؛ هیچ ردیفی ثبت نشد')
        if db.scalar(select(ManagerAttendanceRequest.id).where(
            ManagerAttendanceRequest.employee_id == employee.id,
            ManagerAttendanceRequest.local_day == day,
            ManagerAttendanceRequest.status == 'pending',
        )):
            raise HTTPException(409, f'ردیف {row_number}: درخواست در انتظار تأیید دارد؛ هیچ ردیفی ثبت نشد')

    marker = secrets.token_hex(6)
    for employee, day, first, last, reason, _ in prepared:
        if is_admin:
            db.add(AttendanceCorrection(
                employee_id=employee.id, local_day=day, action='set',
                override_first=first, override_last=last, reason=reason,
                actor_id=user.id, origin='manager_manual',
            ))
        else:
            db.add(ManagerAttendanceRequest(
                employee_id=employee.id, local_day=day,
                proposed_first=first, proposed_last=last, reason=reason,
                requested_by_id=user.id, expected_revision_id=0, status='pending',
            ))
    try:
        db.flush()
        audit(db, user, 'manager_attendance_excel_direct' if is_admin else 'manager_attendance_excel_import',
              f'batch={marker} file_sha256_prefix={digest} count={len(prepared)}')
        for employee, day, _, _, _, row_number in prepared:
            audit(db, user, 'manager_attendance_direct_row' if is_admin else 'manager_attendance_excel_row',
                  f'batch={marker} row={row_number} employee_id={employee.id} day={day}')
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'در حین ثبت اطلاعات همزمان تغییر کرد؛ هیچ ردیفی از فایل ذخیره نشد')
    return redirect('/managers/attendance/bulk',
                    f'{len(prepared)} ردیف Excel مستقیماً در گزارش‌ها ثبت شد' if is_admin else
                    f'{len(prepared)} ردیف Excel برای تأیید جداگانهٔ ادمین به صف ارسال شد')


@app.post('/managers/attendance/excel-commit')
async def commit_manager_excel_selection(request: Request, db: Session = Depends(get_db)):
    """Commit only explicitly checked, signed preview rows (20-minute TTL)."""
    user = permit(request, db, 'admin', 'secretary')
    content_length = request.headers.get('content-length')
    if content_length and content_length.isdecimal() and int(content_length) > 1_600_000:
        raise HTTPException(413, 'حجم درخواست تأیید بیش از حد مجاز است')
    try:
        async with request.form(max_files=0, max_fields=MAX_ROWS + 2,
                                max_part_size=1_500_000) as form:
            raw_csrf = form.get('csrf_token')
            csrf(request, raw_csrf if isinstance(raw_csrf, str) else '')
            token = form.get('preview_token')
            submitted = form.getlist('selected_rows')
    except ValueError:
        raise HTTPException(400, 'ساختار درخواست تأیید معتبر نیست') from None
    if not isinstance(token, str) or not 1 <= len(token) <= EXCEL_PREVIEW_MAX_TOKEN_CHARS:
        raise HTTPException(400, 'توکن پیش‌نمایش معتبر نیست')
    if not 1 <= len(submitted) <= MAX_ROWS:
        raise HTTPException(400, 'بین ۱ تا ۱۰۰۰ ردیف باید انتخاب شود')
    if any(not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,5}', value)
           for value in submitted):
        raise HTTPException(400, 'شمارهٔ ردیف انتخاب‌شده معتبر نیست')
    selected_rows = {int(value) for value in submitted}
    if len(selected_rows) != len(submitted):
        raise HTTPException(400, 'یک ردیف بیش از یک بار انتخاب شده است')
    try:
        payload = manager_excel_preview_signer().loads(
            token, max_age=EXCEL_PREVIEW_TTL_SECONDS)
    except SignatureExpired:
        raise HTTPException(400, 'زمان پیش‌نمایش تمام شده است؛ فایل را دوباره بارگذاری کنید') from None
    except BadSignature:
        raise HTTPException(403, 'پیش‌نمایش تغییر کرده یا امضای آن معتبر نیست') from None
    if (not isinstance(payload, dict) or payload.get('uid') != user.id
            or payload.get('auth_tag') != session_auth_tag(user)
            or payload.get('csrf') != request.session.get('csrf')):
        raise HTTPException(403, 'پیش‌نمایش متعلق به این نشست نیست')
    raw_rows = payload.get('rows')
    if (not isinstance(raw_rows, list) or not 1 <= len(raw_rows) <= MAX_ROWS
            or not isinstance(payload.get('digest'), str)
            or not re.fullmatch(r'[a-f0-9]{16}', payload['digest'])):
        raise HTTPException(400, 'اطلاعات پیش‌نمایش ناقص است')
    entries = []
    seen = set()
    for item in raw_rows:
        if (not isinstance(item, list) or len(item) != 7
                or not isinstance(item[0], int) or item[0] <= 1
                or any(not isinstance(value, str) for value in item[1:])):
            raise HTTPException(400, 'ساختار ردیف پیش‌نمایش نامعتبر است')
        if item[0] in seen:
            raise HTTPException(400, 'ردیف تکراری در پیش‌نمایش وجود دارد')
        seen.add(item[0])
        if item[0] in selected_rows:
            entries.append(ManagerExcelRow(*item))
    if len(entries) != len(selected_rows):
        raise HTTPException(400, 'ردیف انتخابی در فایل وجود ندارد')
    prepared = prepare_manager_excel_entries(db, entries)
    return commit_manager_excel_prepared(db, user, prepared, payload['digest'])


@app.post('/managers/attendance/bulk')
async def submit_managers_bulk_attendance(request: Request, db: Session = Depends(get_db)):
    """Atomic all-or-nothing manager-day creation for admin or legacy secretary."""
    user = permit(request, db, 'admin', 'secretary')
    is_admin = user.role == 'admin'
    form = await request.form()
    csrf(request, str(form.get('csrf_token', '')))
    raw_ids = form.getlist('employee_ids')
    if not 1 <= len(raw_ids) <= 25:
        raise HTTPException(400, 'بین ۱ تا ۲۵ مدیر باید انتخاب شود')
    if any(not isinstance(raw, str) or not re.fullmatch(r'[1-9][0-9]*', raw)
           for raw in raw_ids):
        raise HTTPException(400, 'شناسهٔ مدیر نامعتبر است')
    ids = [int(raw) for raw in raw_ids]
    if len(set(ids)) != len(ids):
        raise HTTPException(400, 'یک مدیر را نمی‌توان در یک نوبت دوبار انتخاب کرد')

    prepared = []
    for employee_id in sorted(ids):
        def field(prefix):
            value = form.get(f'{prefix}_{employee_id}', '')
            if not isinstance(value, str):
                raise HTTPException(400, 'مقدار فرم نامعتبر است')
            return value

        day = correction_day(field('day'))
        first = correction_clock(day, field('first'))
        last = correction_clock(day, field('last'))
        reason = correction_reason(field('reason'))
        if first is None or last is None or last <= first:
            raise HTTPException(400, 'برای هر مدیر ورود و خروج صحیح و به ترتیب زمانی لازم است')
        prepared.append((employee_id, day.isoformat(), first, last, reason))

    # Serialise batches touching the same managers with a stable lock order.
    for employee_id, day, first, last, reason in prepared:
        employee = eligible_manual_manager(db, employee_id, lock=True)
        if source_day_punches(db, employee.device_id, day):
            raise HTTPException(409, 'برای یک مدیر در این روز تردد دستگاهی وجود دارد؛ هیچ درخواست گروهی ذخیره نشد')
        if correction_revisions(db, employee_id, day):
            raise HTTPException(409, 'برای یک مدیر در این روز اصلاح قبلی وجود دارد؛ از ثبت تکی استفاده کنید')
        if db.scalar(select(ManagerAttendanceRequest.id).where(
            ManagerAttendanceRequest.employee_id == employee_id,
            ManagerAttendanceRequest.local_day == day,
            ManagerAttendanceRequest.status == 'pending',
        )):
            raise HTTPException(409, 'یک درخواست دیگر در صف تأیید است؛ هیچ درخواست گروهی ذخیره نشد')

    marker = secrets.token_hex(6)
    for employee_id, day, first, last, reason in prepared:
        if is_admin:
            db.add(AttendanceCorrection(
                employee_id=employee_id, local_day=day, action='set',
                override_first=first, override_last=last, reason=reason,
                actor_id=user.id, origin='manager_manual',
            ))
        else:
            db.add(ManagerAttendanceRequest(
                employee_id=employee_id, local_day=day,
                proposed_first=first, proposed_last=last, reason=reason,
                requested_by_id=user.id, expected_revision_id=0, status='pending',
            ))
    try:
        db.flush()
        audit(db, user, 'manager_attendance_bulk_direct' if is_admin else 'manager_attendance_bulk_request',
              f'batch={marker} count={len(prepared)}')
        for employee_id, day, _, _, _ in prepared:
            audit(db, user, 'manager_attendance_direct_set' if is_admin else 'manager_attendance_request',
                  f'batch={marker} employee_id={employee_id} day={day}')
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'یکی از درخواست‌ها همزمان تغییر کرد؛ هیچ نوبت گروهی ثبت نشد')
    return redirect('/managers/attendance/bulk',
                    f'{len(prepared)} تردد مدیران مستقیماً در گزارش‌ها ثبت شد' if is_admin else
                    f'{len(prepared)} درخواست مدیران به صورت جداگانه برای تأیید ادمین ارسال شد')


@app.post('/managers/attendance/save')
def save_manager_attendance(request: Request, employee_id: int = Form(), day_jalali: str = Form(),
                            first_clock: str = Form(), last_clock: str = Form(),
                            reason: str = Form(), expected_revision: int = Form(),
                            csrf_token: str = Form(), db: Session = Depends(get_db)):
    # Retain a safe response for links/forms from the previous prerelease.
    permit(request, db, 'admin', 'secretary')
    raise HTTPException(410, 'ثبت مستقیم منسوخ شده است؛ درخواست باید ابتدا به ادمین ارسال شود')


@app.post('/managers/attendance/revoke')
def revoke_manager_attendance(request: Request, employee_id: int = Form(),
                              day_jalali: str = Form(), reason: str = Form(),
                              expected_revision: int = Form(), csrf_token: str = Form(),
                              db: Session = Depends(get_db)):
    user = permit(request, db, 'admin', 'secretary')
    if user.role != 'admin':
        raise HTTPException(410, 'لغو مستقیم توسط منشی منسوخ شده است؛ از درخواست و بررسی ادمین استفاده کنید')
    csrf(request, csrf_token)
    day = correction_day(day_jalali)
    reason = correction_reason(reason)
    employee = eligible_manual_manager(db, employee_id, lock=True)
    if source_day_punches(db, employee.device_id, day.isoformat()):
        raise HTTPException(409, 'روز دارای تردد دستگاهی را از این بخش تغییر ندهید')
    previous = correction_revisions(db, employee.id, day.isoformat())
    current = previous[0] if previous else None
    if expected_revision != (current.id if current else 0):
        raise HTTPException(409, 'سابقهٔ این روز تغییر کرده؛ صفحه را تازه‌سازی کنید')
    if current is None or current.action != 'set' or current.origin != 'manager_manual':
        raise HTTPException(409, 'ثبت مستقیم فعالی برای لغو وجود ندارد')
    if db.scalar(select(ManagerAttendanceRequest.id).where(
        ManagerAttendanceRequest.employee_id == employee.id,
        ManagerAttendanceRequest.local_day == day.isoformat(),
        ManagerAttendanceRequest.status == 'pending',
    )):
        raise HTTPException(409, 'درخواست قدیمی این روز هنوز در انتظار بررسی است')
    revoke = AttendanceCorrection(
        employee_id=employee.id, local_day=day.isoformat(), action='revoke',
        reason=reason, actor_id=user.id, origin='manager_manual',
    )
    db.add(revoke)
    db.flush()
    audit(db, user, 'manager_attendance_direct_revoke',
          f'revision={revoke.id} previous={current.id} employee_id={employee.id} day={day.isoformat()}')
    db.commit()
    return manager_attendance_redirect(employee.id, day_jalali,
                                       'ثبت مستقیم لغو شد؛ تاریخچه حفظ و گزارش‌ها به‌روز شدند')


@app.post('/managers/attendance/submit')
def submit_manager_attendance(request: Request, employee_id: int = Form(),
                              day_jalali: str = Form(), first_clock: str = Form(),
                              last_clock: str = Form(), reason: str = Form(),
                              expected_revision: int = Form(), csrf_token: str = Form(),
                              db: Session = Depends(get_db)):
    user = permit(request, db, 'admin', 'secretary')
    csrf(request, csrf_token)
    day = correction_day(day_jalali)
    first = correction_clock(day, first_clock)
    last = correction_clock(day, last_clock)
    reason = correction_reason(reason)
    if first is None or last is None or last <= first:
        raise HTTPException(400, 'ورود و خروج معتبر و به ترتیب صحیح لازم است')
    employee = eligible_manual_manager(db, employee_id, lock=True)
    if source_day_punches(db, employee.device_id, day.isoformat()):
        raise HTTPException(409, 'این روز تردد دستگاهی دارد؛ بررسی فقط از مسیر ادمین مجاز است')
    previous = correction_revisions(db, employee.id, day.isoformat())
    latest = previous[0] if previous else None
    if expected_revision != (latest.id if latest else 0):
        raise HTTPException(409, 'سابقه این روز تغییر کرده است؛ صفحه را تازه‌سازی کنید')
    if latest and latest.origin != 'manager_manual':
        raise HTTPException(409, 'این روز اصلاح اداری دیگری دارد')
    if db.scalar(select(ManagerAttendanceRequest.id).where(
        ManagerAttendanceRequest.employee_id == employee.id,
        ManagerAttendanceRequest.local_day == day.isoformat(),
        ManagerAttendanceRequest.status == 'pending',
    )):
        raise HTTPException(409, 'درخواستی برای این روز در انتظار تأیید است')
    if user.role == 'admin':
        correction = AttendanceCorrection(
            employee_id=employee.id, local_day=day.isoformat(), action='set',
            override_first=first, override_last=last, reason=reason,
            actor_id=user.id, origin='manager_manual',
        )
        db.add(correction)
        db.flush()
        audit(db, user, 'manager_attendance_direct_set',
              f'revision={correction.id} employee_id={employee.id} day={day.isoformat()}')
        db.commit()
        return manager_attendance_redirect(employee.id, day_jalali,
                                           'تردد مدیر مستقیماً در گزارش ثبت شد؛ تأیید دیگری لازم نیست')
    proposal = ManagerAttendanceRequest(
        employee_id=employee.id, local_day=day.isoformat(),
        proposed_first=first, proposed_last=last, reason=reason,
        requested_by_id=user.id, expected_revision_id=expected_revision,
        status='pending',
    )
    db.add(proposal)
    db.flush()
    audit(db, user, 'manager_attendance_request',
          f'request={proposal.id} employee_id={employee.id} day={day.isoformat()}')
    db.commit()
    return manager_attendance_redirect(employee.id, day_jalali,
                                       'درخواست ثبت شد و در انتظار تأیید ادمین است؛ گزارش نهایی تغییری نکرد')


@app.post('/managers/attendance/cancel')
def cancel_manager_attendance(request: Request, request_id: int = Form(),
                              reason: str = Form(), csrf_token: str = Form(),
                              db: Session = Depends(get_db)):
    user = permit(request, db, 'secretary')
    csrf(request, csrf_token)
    reason = correction_reason(reason)
    proposal = db.get(ManagerAttendanceRequest, request_id)
    if proposal is None:
        raise HTTPException(404, 'درخواست پیدا نشد')
    db.scalar(select(Employee).where(Employee.id == proposal.employee_id).with_for_update())
    proposal = db.scalar(select(ManagerAttendanceRequest).where(
        ManagerAttendanceRequest.id == request_id).with_for_update())
    if proposal.status != 'pending' or proposal.requested_by_id != user.id:
        raise HTTPException(403, 'فقط ثبت‌کننده می‌تواند درخواستِ در انتظار خود را پس بگیرد')
    proposal.status = 'cancelled'
    proposal.resolved_by_id = user.id
    proposal.resolved_at = datetime.now(timezone.utc)
    proposal.resolution_reason = reason
    audit(db, user, 'manager_attendance_cancel', f'request={proposal.id} employee_id={proposal.employee_id}')
    db.commit()
    return manager_attendance_redirect(proposal.employee_id,
                                       to_jalali(datetime.fromisoformat(proposal.local_day).date()),
                                       'درخواست پس گرفته شد؛ هیچ گزارش نهایی تغییر نکرد')


@app.get('/managers/approvals')
def manager_approvals_page(request: Request, page_num: int = 1, db: Session = Depends(get_db)):
    permit(request, db, 'admin')
    if page_num < 1 or page_num > 10000:
        raise HTTPException(400, 'شماره صفحه نامعتبر است')
    total = db.scalar(select(func.count()).select_from(ManagerAttendanceRequest).where(
        ManagerAttendanceRequest.status == 'pending')) or 0
    proposals = db.scalars(select(ManagerAttendanceRequest).where(
        ManagerAttendanceRequest.status == 'pending',
    ).order_by(ManagerAttendanceRequest.id).limit(25).offset((page_num - 1) * 25)).all()
    token = csrf_token(request)
    body = msg(request) + (
        '<div class="card"><h2>صندوق تأیید تردد مدیران</h2>'
        '<div class="manager-callout">تا زمانی که ادمین یک درخواست را تأیید نکند، ساعت‌های پیشنهادی '
        'در گزارش و Excel نهایی ظاهر نمی‌شوند. رد درخواست نیز چیزی از گزارش حذف نمی‌کند. '
        'دلیل تصمیم اجباری و نام تأییدکننده قابل پیگیری است.</div>'
        f'<p>در انتظار بررسی: <strong>{total}</strong> درخواست · صفحه <bdi>{page_num}</bdi></p></div>'
    )
    for proposal in proposals:
        employee = db.get(Employee, proposal.employee_id)
        requester = db.get(User, proposal.requested_by_id)
        status = ('فعال · مدیر' if employee and employee.status == 'active'
                  and employee.attendance_group == 'management' else 'نیازمند بررسی هویت/وضعیت')
        body += (
            '<div class="card"><h2>درخواست ' + str(proposal.id) + '</h2>'
            f'<p><strong>{escape(employee.name if employee else "کارمند حذف‌شده")}</strong> '
            f'· <bdi>{escape(to_jalali(datetime.fromisoformat(proposal.local_day).date()))}</bdi> '
            f'· وضعیت کارمند: {escape(status)}</p>'
            f'<div class="manager-preview"><span>ورود: <bdi>{proposal.proposed_first.astimezone(TZ).strftime("%H:%M:%S")}</bdi></span>'
            f'<span>خروج: <bdi>{proposal.proposed_last.astimezone(TZ).strftime("%H:%M:%S")}</bdi></span>'
            f'<span>منشی: {escape(requester.username if requester else "حساب نامشخص")}</span></div>'
            f'<p class="muted">دلیل ثبت: {escape(proposal.reason)}</p>'
            f'<form method="post" action="/managers/approvals/{proposal.id}/resolve" class="fields">'
            f'<input type="hidden" name="csrf_token" value="{escape(token)}">'
            '<div><label>دلیل تصمیم ادمین</label><input name="reason" minlength="5" maxlength="500" required '
            'placeholder="مثلاً تطبیق با اعلام مدیر"></div>'
            '<div><button type="submit" name="decision" value="approve">تأیید و اعمال در گزارش</button></div>'
            '<div><button type="submit" name="decision" value="reject" class="outline">رد درخواست</button></div>'
            '</form></div>'
        )
    if not proposals:
        body += '<div class="card"><p class="muted">در این صفحه درخواست در انتظاری وجود ندارد.</p></div>'
    links = ''
    if page_num > 1:
        links += f'<a class="btn outline" href="/managers/approvals?page_num={page_num - 1}">صفحه قبل</a>'
    if page_num * 25 < total:
        links += f'<a class="btn outline" href="/managers/approvals?page_num={page_num + 1}">صفحه بعد</a>'
    body += f'<div class="actions">{links}</div>'
    recent = db.scalars(select(ManagerAttendanceRequest).where(
        ManagerAttendanceRequest.status != 'pending',
    ).order_by(ManagerAttendanceRequest.id.desc()).limit(30)).all()
    review_rows = ''
    for item in recent:
        reviewer = db.get(User, item.resolved_by_id) if item.resolved_by_id else None
        review_rows += '<tr>' + ''.join(f'<td>{escape(value)}</td>' for value in (
            item.id, {'approved': 'تأیید', 'rejected': 'رد', 'cancelled': 'انصراف'}.get(item.status, item.status),
            item.employee_id, to_jalali(datetime.fromisoformat(item.local_day).date()),
            reviewer.username if reviewer else '—', item.resolution_reason,
        )) + '</tr>'
    body += (
        '<div class="card table-wrap"><h2>۳۰ تصمیم اخیر</h2><table><thead>'
        '<tr><th>درخواست</th><th>نتیجه</th><th>شناسه کارمند</th><th>تاریخ</th><th>تصمیم‌گیرنده</th><th>دلیل</th></tr>'
        f'</thead><tbody>{review_rows or "<tr><td colspan=6>هنوز تصمیمی ثبت نشده است.</td></tr>"}</tbody></table></div>'
    )
    return page('تأیید تردد مدیران', body, request, db, 'manager_approvals')


@app.post('/managers/approvals/{request_id}/resolve')
def resolve_manager_attendance(request_id: int, request: Request, decision: str = Form(),
                               reason: str = Form(), csrf_token: str = Form(),
                               db: Session = Depends(get_db)):
    admin = permit(request, db, 'admin')
    csrf(request, csrf_token)
    reason = correction_reason(reason)
    if decision not in ('approve', 'reject'):
        raise HTTPException(400, 'تصمیم نامعتبر است')
    lookup = db.get(ManagerAttendanceRequest, request_id)
    if lookup is None:
        raise HTTPException(404, 'درخواست پیدا نشد')
    employee = db.scalar(select(Employee).where(Employee.id == lookup.employee_id).with_for_update())
    proposal = db.scalar(select(ManagerAttendanceRequest).where(
        ManagerAttendanceRequest.id == request_id).with_for_update())
    if proposal.status != 'pending':
        raise HTTPException(409, 'این درخواست قبلاً تعیین تکلیف شده است')
    if proposal.requested_by_id == admin.id:
        raise HTTPException(403, 'ثبت‌کننده نمی‌تواند درخواست خودش را تأیید یا رد کند')
    if decision == 'approve':
        if employee is None or employee.status != 'active' or employee.attendance_group != 'management':
            raise HTTPException(409, 'کارمند دیگر عضو فعال گروه مدیران نیست')
        if source_day_punches(db, employee.device_id, proposal.local_day):
            raise HTTPException(409, 'ثبت خام دستگاه وجود دارد؛ تأیید خودکار ممنوع است')
        latest = correction_revisions(db, employee.id, proposal.local_day)
        current = latest[0] if latest else None
        if proposal.expected_revision_id != (current.id if current else 0):
            raise HTTPException(409, 'سابقه روز پس از ارسال درخواست تغییر کرده؛ درخواست را رد کنید')
        if current and current.origin != 'manager_manual':
            raise HTTPException(409, 'اصلاح اداری جدید با این درخواست تداخل دارد')
        if proposal.proposed_last <= proposal.proposed_first:
            raise HTTPException(409, 'ساعت درخواست نامعتبر است')
        correction = AttendanceCorrection(
            employee_id=employee.id, local_day=proposal.local_day, action='set',
            override_first=proposal.proposed_first, override_last=proposal.proposed_last,
            reason=f'تأیید درخواست #{proposal.id}: {proposal.reason}'[:500],
            actor_id=admin.id, origin='manager_manual',
        )
        db.add(correction)
        db.flush()
        proposal.correction_id = correction.id
    proposal.status = 'approved' if decision == 'approve' else 'rejected'
    proposal.resolved_by_id = admin.id
    proposal.resolved_at = datetime.now(timezone.utc)
    proposal.resolution_reason = reason
    audit(db, admin, 'manager_attendance_' + proposal.status,
          f'request={proposal.id} employee_id={proposal.employee_id} revision={proposal.correction_id or 0}')
    db.commit()
    return redirect('/managers/approvals',
                    'درخواست تأیید و در گزارش‌های نهایی اعمال شد' if decision == 'approve'
                    else 'درخواست رد شد و هیچ تردد نهایی تغییر نکرد')


@app.get('/corrections')
def corrections_page(request: Request, employee_id: int = 0, day_jalali: str = '', db: Session = Depends(get_db)):
    permit(request, db, 'admin')
    day_jalali = day_jalali or to_jalali(datetime.now(TZ).date())
    day = correction_day(day_jalali)
    employees = db.scalars(select(Employee).order_by(Employee.name, Employee.device_id)).all()
    options = '<option value="0">کارمند را انتخاب کنید</option>' + ''.join(
        f'<option value="{emp.id}" {"selected" if employee_id == emp.id else ""}>'
        f'{escape(emp.name)} — {escape(emp.device_id)}</option>' for emp in employees
    )
    token = csrf_token(request)
    body = msg(request) + (
        '<div class="card"><h2>اصلاح دستی ورود و خروج</h2>'
        '<p class="muted">فقط مدیر مجاز به ثبت اصلاح است. ثبت‌های واقعی دستگاه و فایل‌های DAT تغییر نمی‌کنند. '
        'هر اصلاح با دلیل، نام مدیر و زمان ثبت در تاریخچه محفوظ می‌ماند و در محاسبات روزانه، '
        'ماهانه، سالانه و دوره‌های دلخواه اعمال می‌شود؛ نمایش ثبت‌های خام تغییر نمی‌کند.</p>'
        '<form method="get" action="/corrections"><div class="fields">'
        f'<div><label for="employee_id">کارمند</label><select id="employee_id" name="employee_id" required>{options}</select></div>'
        f'<div><label for="day_jalali">تاریخ شمسی</label><input id="day_jalali" name="day_jalali" value="{escape(day_jalali)}" required placeholder="۱۴۰۵/۰۸/۲۵"></div>'
        '<div><button type="submit">نمایش تردد این روز</button></div>'
        '</div></form></div>'
    )
    if not employee_id:
        return page('اصلاح تردد', body, request, db, 'corrections')
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(404, 'کارمند پیدا نشد')
    revisions = correction_revisions(db, employee.id, day.isoformat())
    current = revisions[0] if revisions else None
    active = current if current is not None and current.action == 'set' else None
    original = source_day_punches(db, employee.device_id, day.isoformat())
    first, last, seconds = effective_day_window(original, active)
    original_text = '، '.join(stamp.strftime('%H:%M:%S') for stamp in original) or 'هیچ ثبت دستگاهی وجود ندارد'
    status = ('اصلاح مدیر فعال است' if active else
              'آخرین اصلاح لغو شده است' if current else 'بدون اصلاح دستی')
    first_override = active.override_first.astimezone(TZ).strftime('%H:%M:%S') if active and active.override_first else ''
    last_override = active.override_last.astimezone(TZ).strftime('%H:%M:%S') if active and active.override_last else ''
    history_rows = ''
    for revision in revisions:
        person = db.get(User, revision.actor_id)
        history_rows += '<tr>' + ''.join(f'<td>{escape(item)}</td>' for item in (
            revision.id,
            ('ثبت دستی مدیران' if revision.action == 'set' else 'لغو ثبت دستی')
            if revision.origin == 'manager_manual' else ('اصلاح' if revision.action == 'set' else 'لغو اصلاح'),
            revision.override_first.astimezone(TZ).strftime('%H:%M:%S') if revision.override_first else '—',
            revision.override_last.astimezone(TZ).strftime('%H:%M:%S') if revision.override_last else '—',
            revision.reason,
            person.username if person else f'#{revision.actor_id}',
            to_jalali(revision.created_at.astimezone(TZ).date()) + ' ' +
            revision.created_at.astimezone(TZ).strftime('%H:%M:%S'),
        )) + '</tr>'
    body += (
        '<div class="card"><h2>وضعیت روز انتخابی</h2>'
        f'<p><strong>{escape(employee.name)} ({escape(employee.device_id)})</strong> — {escape(day_jalali)}</p>'
        f'<p>ثبت‌های خام دستگاه: <span dir="ltr">{escape(original_text)}</span></p>'
        f'<p>اولین زمان مؤثر: <strong dir="ltr">{escape(first.strftime("%H:%M:%S") if first else "—")}</strong> '
        f'| آخرین زمان مؤثر: <strong dir="ltr">{escape(last.strftime("%H:%M:%S") if last else "—")}</strong> '
        f'| مدت حضور: <strong dir="ltr">{escape(duration_label(seconds) if seconds is not None else "نامشخص")}</strong></p>'
        f'<p class="muted">وضعیت: {escape(status)}. مبنا اختلاف اولین تا آخرین زمان مؤثر است، بدون کسر استراحت یا تفسیر شیفت شب.</p>'
        '</div>'
        '<div class="card"><h2>ثبت نسخهٔ جدید اصلاح</h2>'
        '<p class="muted">فقط ساعتِ اشتباه یا جامانده را بنویسید؛ فیلد خالی از ثبت‌های اصلی دستگاه استفاده می‌کند. '
        'اگر هیچ ثبت دستگاهی وجود ندارد، هر دو ساعت ورود و خروج لازم است. '
        'برای پاک‌کردن تمام اصلاحات قبلی از «لغو اصلاح» استفاده کنید.</p>'
        '<form method="post" action="/corrections/save"><div class="fields">'
        f'<input type="hidden" name="csrf_token" value="{escape(token)}">'
        f'<input type="hidden" name="employee_id" value="{employee.id}">'
        f'<input type="hidden" name="day_jalali" value="{escape(day_jalali)}">'
        f'<input type="hidden" name="expected_revision" value="{current.id if current else 0}">'
        f'<div><label for="first_clock">ورود اصلاحی (اختیاری)</label><input id="first_clock" name="first_clock" value="{escape(first_override)}" placeholder="08:00:00" inputmode="numeric" maxlength="8"></div>'
        f'<div><label for="last_clock">خروج اصلاحی (اختیاری)</label><input id="last_clock" name="last_clock" value="{escape(last_override)}" placeholder="16:00:00" inputmode="numeric" maxlength="8"></div>'
        '<div><label for="reason">دلیل اصلاح</label><textarea id="reason" name="reason" rows="2" maxlength="500" minlength="5" required placeholder="مثلاً ثبت ورود توسط کارمند فراموش شده است"></textarea></div>'
        '<div><button type="submit">ثبت اصلاح با تاریخچه</button></div>'
        '</div></form>'
    )
    if active:
        body += (
            '<form method="post" action="/corrections/revoke"><div class="fields">'
            f'<input type="hidden" name="csrf_token" value="{escape(token)}">'
            f'<input type="hidden" name="employee_id" value="{employee.id}">'
            f'<input type="hidden" name="day_jalali" value="{escape(day_jalali)}">'
            f'<input type="hidden" name="expected_revision" value="{current.id}">'
            '<div><label>دلیل لغو</label><input name="reason" maxlength="500" minlength="5" required placeholder="دلیل بازگردانی ثبت‌های اصلی دستگاه"></div>'
            '<div><button type="submit" class="outline">لغو اصلاح و بازگشت به ثبت‌های خام</button></div>'
            '</div></form>'
        )
    body += '</div>'
    history_table = history_rows or '<tr><td colspan="7">هنوز تغییری ثبت نشده است.</td></tr>'
    body += (
        '<div class="card table-wrap"><h2>تاریخچهٔ تغییرات این کارمند و روز (۱۰۰ نسخهٔ اخیر)</h2>'
        '<table><thead><tr><th>نسخه</th><th>عملیات</th><th>ورود اصلاحی</th><th>خروج اصلاحی</th>'
        '<th>دلیل</th><th>ثبت‌کننده</th><th>زمان ثبت</th></tr></thead>'
        f'<tbody>{history_table}</tbody></table></div>'
    )
    return page('اصلاح تردد', body, request, db, 'corrections')


@app.post('/corrections/save')
def save_correction(request: Request, employee_id: int = Form(), day_jalali: str = Form(),
                    first_clock: str = Form(''), last_clock: str = Form(''),
                    reason: str = Form(), expected_revision: int = Form(),
                    csrf_token: str = Form(), db: Session = Depends(get_db),
                    from_changes: str = Form('')):
    user = permit(request, db, 'admin')
    csrf(request, csrf_token)
    day = correction_day(day_jalali)
    reason = correction_reason(reason)
    first_override = correction_clock(day, first_clock)
    last_override = correction_clock(day, last_clock)
    if first_override is None and last_override is None:
        raise HTTPException(400, 'حداقل یک ساعت اصلاحی لازم است؛ برای حذف اصلاح از دکمهٔ لغو استفاده کنید')
    # Serialize edits for the same employee; compare version to prevent stale tabs.
    employee = db.scalar(select(Employee).where(Employee.id == employee_id).with_for_update())
    if employee is None:
        raise HTTPException(404, 'کارمند پیدا نشد')
    revisions = correction_revisions(db, employee.id, day.isoformat())
    current = revisions[0] if revisions else None
    if expected_revision != (current.id if current else 0):
        raise HTTPException(409, 'این روز هم‌زمان توسط مدیر دیگری تغییر کرده؛ صفحه را تازه‌سازی کنید')
    candidate = AttendanceCorrection(employee_id=employee.id, local_day=day.isoformat(),
                                     action='set', override_first=first_override,
                                     override_last=last_override, reason=reason,
                                     actor_id=user.id)
    source = source_day_punches(db, employee.device_id, day.isoformat())
    _, _, seconds = effective_day_window(source, candidate)
    if seconds is None:
        raise HTTPException(400, 'زمان اصلاحی باید یک ورود و خروج معتبر با ترتیب زمانی صحیح بسازد؛ برای روز بدون ثبت، هر دو ساعت را وارد کنید')
    db.add(candidate)
    db.flush()
    audit(db, user, 'attendance_correction_set', f'revision={candidate.id} employee_id={employee.id} day={day.isoformat()}')
    db.commit()
    message = 'اصلاح ثبت شد؛ مبنای محاسبهٔ گزارش‌ها به‌روزرسانی شد'
    return changes_redirect(employee.id, day_jalali, message) if from_changes == '1' else correction_redirect(employee.id, day_jalali, message)


@app.post('/corrections/revoke')
def revoke_correction(request: Request, employee_id: int = Form(), day_jalali: str = Form(),
                      reason: str = Form(), expected_revision: int = Form(),
                      csrf_token: str = Form(), db: Session = Depends(get_db),
                      from_changes: str = Form('')):
    user = permit(request, db, 'admin')
    csrf(request, csrf_token)
    day = correction_day(day_jalali)
    reason = correction_reason(reason)
    employee = db.scalar(select(Employee).where(Employee.id == employee_id).with_for_update())
    if employee is None:
        raise HTTPException(404, 'کارمند پیدا نشد')
    revisions = correction_revisions(db, employee.id, day.isoformat())
    current = revisions[0] if revisions else None
    if expected_revision != (current.id if current else 0):
        raise HTTPException(409, 'این روز تغییر کرده؛ صفحه را تازه‌سازی کنید')
    if current is None or current.action != 'set':
        raise HTTPException(409, 'هیچ اصلاح فعالی برای لغو وجود ندارد')
    revoked = AttendanceCorrection(employee_id=employee.id, local_day=day.isoformat(),
                                   action='revoke', reason=reason, actor_id=user.id)
    db.add(revoked)
    db.flush()
    audit(db, user, 'attendance_correction_revoke', f'revision={revoked.id} previous={current.id} employee_id={employee.id} day={day.isoformat()}')
    db.commit()
    message = 'اصلاح لغو شد؛ گزارش‌ها دوباره بر مبنای ثبت‌های اصلی هستند'
    return changes_redirect(employee.id, day_jalali, message) if from_changes == '1' else correction_redirect(employee.id, day_jalali, message)


@app.get('/reports/add-record')
def add_record_page(request: Request, db: Session = Depends(get_db)):
    permit(request, db, 'admin')
    token = csrf_token(request)
    today = to_jalali(datetime.now(TZ).date())
    employees = db.scalars(select(Employee).where(Employee.status == 'active').order_by(Employee.name)).all()
    options = '<option value="0">کارمند را انتخاب کنید</option>' + ''.join(
        f'<option value="{emp.id}">{escape(emp.name)} — {escape(emp.device_id)}</option>'
        for emp in employees
    )
    body = msg(request) + (
        '<div class="card"><h2>افزودن رکورد تردد</h2>'
        '<p class="muted">برای روزی که هیچ تردد دستگاهی ثبت نشده، می‌توانید ورود و خروج را مستقیماً وارد کنید. '
        'ثبت‌ها در دیتابیس مرکزی ذخیره می‌شوند.</p>'
        '<form method="post" action="/reports/add-record" class="fields">'
        f'<div><label>کارمند</label><select name="employee_id" required>{options}</select></div>'
        f'<div><label>تاریخ شمسی</label><input name="day_jalali" value="{escape(today)}" placeholder="۱۴۰۵/۰۸/۲۵" required></div>'
        '<div><label>ساعت ورود (HH:MM)</label><input name="first_punch" placeholder="۰۸:۳۰" required></div>'
        '<div><label>ساعت خروج (HH:MM) — اختیاری</label><input name="last_punch" placeholder="۱۷:۰۰"></div>'
        f'<div><input type="hidden" name="csrf_token" value="{token}"><button>ثبت رکورد</button>'
        '<a class="btn outline" href="/reports" style="margin-inline-start:10px">انصراف</a></div>'
        '</form></div>'
    )
    return page('افزودن رکورد', body, request, db, 'reports')


@app.post('/reports/add-record')
def add_record(
    request: Request,
    employee_id: int = Form(),
    day_jalali: str = Form(),
    first_punch: str = Form(),
    last_punch: str = Form(''),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    user = permit(request, db, 'admin')
    csrf(request, csrf_token)
    employee = db.get(Employee, employee_id)
    if not employee or employee.status == 'deleted':
        raise HTTPException(404, 'کارمند یافت نشد')
    day = correction_day(day_jalali)
    day_str = day.isoformat()

    def parse_clock(val: str):
        val = val.strip().translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'))
        if not val:
            return None
        if not re.fullmatch(r'\d{1,2}:\d{2}(?::\d{2})?', val):
            raise HTTPException(400, f'ساعت نامعتبر: {val}')
        parts = [int(p) for p in val.split(':')]
        t = time(*parts)
        return datetime.combine(day, t, tzinfo=TZ).astimezone(timezone.utc)

    t_first = parse_clock(first_punch)
    t_last  = parse_clock(last_punch) if last_punch.strip() else None

    if t_last and t_last <= t_first:
        raise HTTPException(400, 'ساعت خروج باید بعد از ورود باشد')

    punches_to_add = [t_first]
    if t_last:
        punches_to_add.append(t_last)

    # check duplicates in DB
    existing = {as_utc_instant(o) for o in db.scalars(
        select(Log.occurred_at).where(
            Log.device_id == employee.device_id,
            Log.occurred_at.in_(punches_to_add),
        )
    )}
    added = 0
    for stamp in punches_to_add:
        if stamp in existing:
            continue
        db.add(Log(device_id=employee.device_id, occurred_at=stamp, source_import_id=None))
        db.add(ReportPunch(
            report_id=db.scalar(
                select(ReportSet.id).join(Import, Import.id == ReportSet.import_id)
                .where(Import.note != 'hidden').order_by(ReportSet.id.desc()).limit(1)
            ) or 1,
            device_id=employee.device_id, local_day=day_str, occurred_at=stamp,
        ))
        added += 1

    if added:
        # ensure ReportDay exists
        if not db.scalar(select(ReportDay.id).where(
            ReportDay.device_id == employee.device_id, ReportDay.local_day == day_str,
        )):
            last_report = db.scalar(
                select(ReportSet.id).join(Import, Import.id == ReportSet.import_id)
                .where(Import.note != 'hidden').order_by(ReportSet.id.desc()).limit(1)
            )
            if last_report:
                db.add(ReportDay(report_id=last_report, device_id=employee.device_id, local_day=day_str))

    audit(db, user, 'punch_add_manual', f'employee={employee.device_id} day={day_str} punches={added}')
    db.commit()
    return redirect('/reports', f'رکورد {employee.name} در {day_jalali} ثبت شد ({added} ثبت اضافه شد)')


@app.post('/reports/delete-day')
def delete_report_day(
    request: Request,
    employee_id: int = Form(),
    day_jalali: str = Form(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    """Delete all punch data for one employee on one day from the central DB."""
    user = permit(request, db, 'admin')
    csrf(request, csrf_token)
    employee = db.get(Employee, employee_id)
    if not employee:
        raise HTTPException(404, 'کارمند یافت نشد')
    day = correction_day(day_jalali)
    day_str = day.isoformat()
    day_start = datetime.combine(day, time.min).replace(tzinfo=TZ).astimezone(timezone.utc)
    day_end   = datetime.combine(day + timedelta(days=1), time.min).replace(tzinfo=TZ).astimezone(timezone.utc)

    logs = db.scalars(select(Log).where(
        Log.device_id == employee.device_id,
        Log.occurred_at >= day_start, Log.occurred_at < day_end,
    )).all()
    for lg in logs:
        db.delete(lg)
    for rp in db.scalars(select(ReportPunch).where(
        ReportPunch.device_id == employee.device_id, ReportPunch.local_day == day_str,
    )).all():
        db.delete(rp)
    for rd in db.scalars(select(ReportDay).where(
        ReportDay.device_id == employee.device_id, ReportDay.local_day == day_str,
    )).all():
        db.delete(rd)
    record = db.scalar(select(AttendanceRecord).where(
        AttendanceRecord.device_id == employee.device_id, AttendanceRecord.local_day == day_str,
    ))
    if record:
        db.delete(record)
    audit(db, user, 'punch_day_delete', f'employee={employee.device_id} day={day_str} punches={len(logs)}')
    db.commit()
    return redirect('/reports', f'ترددهای {employee.name} در {day_jalali} حذف شد')


@app.post('/reports/leave-day')
def set_leave_day(
    request: Request,
    employee_id: int = Form(),
    day_jalali: str = Form(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    user = permit(request, db, 'admin')
    csrf(request, csrf_token)
    employee = db.get(Employee, employee_id)
    if not employee:
        raise HTTPException(404, 'کارمند یافت نشد')
    day = correction_day(day_jalali)
    day_str = day.isoformat()
    existing = db.scalar(select(LeaveDay).where(
        LeaveDay.employee_id == employee_id,
        LeaveDay.local_day == day_str,
    ))
    if existing:
        return redirect('/reports', f'{employee.name} در {day_jalali} قبلاً مرخصی دارد')
    db.add(LeaveDay(employee_id=employee_id, local_day=day_str, actor_id=user.id))
    audit(db, user, 'leave_set', f'employee={employee.device_id} day={day_str}')
    db.commit()
    return redirect('/reports', f'مرخصی {employee.name} در {day_jalali} ثبت شد')


@app.post('/reports/leave-day/remove')
def remove_leave_day(
    request: Request,
    employee_id: int = Form(),
    day_jalali: str = Form(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    user = permit(request, db, 'admin')
    csrf(request, csrf_token)
    employee = db.get(Employee, employee_id)
    if not employee:
        raise HTTPException(404, 'کارمند یافت نشد')
    day_str = correction_day(day_jalali).isoformat()
    leave = db.scalar(select(LeaveDay).where(
        LeaveDay.employee_id == employee_id,
        LeaveDay.local_day == day_str,
    ))
    if leave:
        db.delete(leave)
        audit(db, user, 'leave_remove', f'employee={employee.device_id} day={day_str}')
        db.commit()
    return redirect('/reports', f'مرخصی {employee.name} در {day_jalali} لغو شد')


@app.get('/reports')
def reports(request: Request, start_jalali: str = '', end_jalali: str = '', report_id: int = 0, period: str = 'daily', employee_id: int = 0, anchor_day: int = 20, view_filter: str = '', db: Session = Depends(get_db)):
    user = permit(request, db, 'admin', 'operator', 'viewer', 'exporter')
    today = datetime.now(TZ).date()
    start_jalali = start_jalali or to_jalali(today - timedelta(days=30))
    end_jalali = end_jalali or to_jalali(today)
    # Special view filters bypass normal report_rows
    if view_filter == 'absent':
        special_rows = absent_rows(db, start_jalali, end_jalali)
        special_headers = ['نام کارمند', 'شناسه دستگاه', 'تاریخ']
    elif view_filter == 'on_leave':
        special_rows = on_leave_rows(db, start_jalali, end_jalali)
        special_headers = ['نام کارمند', 'شناسه دستگاه', 'تاریخ']
    elif view_filter == 'incomplete':
        special_rows = incomplete_rows(db, start_jalali, end_jalali)
        special_headers = ['نام کارمند', 'شناسه دستگاه', 'تاریخ']
    else:
        special_rows = None
        special_headers = None
    rows = report_rows(db, start_jalali, end_jalali, report_id, period, employee_id, anchor_day) if special_rows is None else []
    token = csrf_token(request)
    from urllib.parse import urlencode
    daily_employee_ids = {}
    active_corrections = {}
    active_leaves: set[tuple[int, str]] = set()
    if period == 'daily' and rows:
        start, end = range_jalali(start_jalali, end_jalali)
        daily_employee_ids = {emp.device_id: emp.id for emp in db.scalars(select(Employee).where(
            Employee.device_id.in_({row[1] for row in rows})
        ))}
        active_corrections = latest_corrections(db, start.isoformat(), end.isoformat(), employee_id)
        leave_rows = db.execute(select(LeaveDay.employee_id, LeaveDay.local_day).where(
            LeaveDay.local_day >= start.isoformat(),
            LeaveDay.local_day <= end.isoformat(),
        )).all()
        active_leaves = {(r[0], r[1]) for r in leave_rows}
    column_labels = report_headers(period)
    rendered_rows = []
    for row in rows:
        cells = [f'<td data-label="{escape(label)}">{escape(value)}</td>'
                 for label, value in zip(column_labels, row)]
        if period == 'daily':
            emp_id = daily_employee_ids[row[1]]
            day = parse_jalali(row[2]).isoformat()
            correction = active_corrections.get((emp_id, day))
            has_leave = (emp_id, day) in active_leaves
            badge = ''
            if has_leave:
                badge = '<span class="correction-tag" style="color:var(--warning,#b87d00);background:var(--warning-soft,#fef3cd)" title="مرخصی ثبت‌شده">✦ مرخصی</span>'
            elif correction and correction.action == 'set':
                badge = ('<span class="correction-tag" title="تردد با ثبت دستی منشی">◷ ثبت دستی مدیران</span>'
                         if correction.origin == 'manager_manual' else
                         '<span class="correction-tag" title="ساعت مؤثر توسط مدیر اصلاح شده">✎ اصلاح مدیر</span>')
            if user.role == 'admin':
                leave_btn = (
                    f'<form method="post" action="/reports/leave-day/remove" style="display:inline" onsubmit="return confirm(\'مرخصی این روز لغو شود؟\')">'
                    + f'<input type="hidden" name="employee_id" value="{emp_id}">'
                    + f'<input type="hidden" name="day_jalali" value="{escape(row[2])}">'
                    + f'<input type="hidden" name="csrf_token" value="{token}">'
                    + '<button class="correction-edit" style="color:#b87d00" type="submit" title="لغو مرخصی">✕ مرخصی</button>'
                    + '</form>'
                ) if has_leave else (
                    f'<form method="post" action="/reports/leave-day" style="display:inline" onsubmit="return confirm(\'مرخصی این روز ثبت شود؟\')">'
                    + f'<input type="hidden" name="employee_id" value="{emp_id}">'
                    + f'<input type="hidden" name="day_jalali" value="{escape(row[2])}">'
                    + f'<input type="hidden" name="csrf_token" value="{token}">'
                    + '<button class="correction-edit" style="color:var(--primary,#1a6fa8)" type="submit" title="ثبت مرخصی">✦ مرخصی</button>'
                    + '</form>'
                )
                edit = (
                    '<a class="correction-edit" href="/corrections?' + urlencode({
                        'employee_id': emp_id, 'day_jalali': row[2],
                    }) + '">اصلاح</a>'
                    + leave_btn
                    + f'<form method="post" action="/reports/delete-day" style="display:inline" onsubmit="return confirm(\'ترددهای این روز حذف شود؟\')">'
                    + f'<input type="hidden" name="employee_id" value="{emp_id}">'
                    + f'<input type="hidden" name="day_jalali" value="{escape(row[2])}">'
                    + f'<input type="hidden" name="csrf_token" value="{token}">'
                    + '<button class="correction-edit" style="color:#c0392b" type="submit">حذف</button>'
                    + '</form>'
                )
            else:
                edit = ''
            cells[0] = f'<td data-label="نام کارمند">{escape(row[0])}{badge}</td>'
            if user.role == 'admin':
                cells.append(f'<td data-label="عملیات" class="report-actions-cell"><div class="report-row-actions">{edit}</div></td>')
        rendered_rows.append('<tr>' + ''.join(cells) + '</tr>')
    # Build special-filter table if applicable
    if special_rows is not None:
        sp_header = ''.join(f'<th>{escape(h)}</th>' for h in special_headers)
        sp_body = ''.join(
            '<tr>' + ''.join(f'<td>{escape(str(c))}</td>' for c in r) + '</tr>'
            for r in special_rows
        )
        filter_labels = {'absent': 'غایبین', 'on_leave': 'مرخصی‌ها', 'incomplete': 'تردد ناقص'}
        sp_title = filter_labels.get(view_filter, '')
        special_table_html = (
            f'<div class="card"><h2>{sp_title} ({len(special_rows)} ردیف)</h2>'
            '<div class="table-wrap" tabindex="0"><table>'
            f'<thead><tr>{sp_header}</tr></thead><tbody>{sp_body}</tbody>'
            '</table></div></div>'
        )
    else:
        special_table_html = ''
    table = ''.join(rendered_rows)
    query = urlencode({'start_jalali': start_jalali, 'end_jalali': end_jalali, 'period': period, 'employee_id': employee_id, 'anchor_day': anchor_day})
    download = f'<a class="btn" href="/reports/download?{query}">دانلود Excel</a>' if user.role in ('admin', 'operator', 'exporter') else ''
    # Build employee options for the inline add-record sub-form
    all_employees = db.scalars(select(Employee).where(Employee.status == 'active').order_by(Employee.name)).all()
    add_emp_opts = '<option value="0">کارمند را انتخاب کنید</option>' + ''.join(
        f'<option value="{emp.id}">{escape(emp.name)} — {escape(emp.device_id)}</option>'
        for emp in all_employees
    )
    today_j = to_jalali(datetime.now(TZ).date())
    add_record_section = (
        f'<details class="add-record-panel"><summary class="btn outline">افزودن رکورد</summary>'
        f'<form method="post" action="/reports/add-record" class="add-record-inline">'
        f'<select name="employee_id" required>{add_emp_opts}</select>'
        f'<input name="day_jalali" value="{escape(today_j)}" placeholder="تاریخ شمسی" required style="width:120px">'
        f'<input name="first_punch" placeholder="ورود HH:MM" required style="width:100px">'
        f'<input name="last_punch" placeholder="خروج HH:MM" style="width:100px">'
        f'<input type="hidden" name="csrf_token" value="{token}">'
        f'<button type="submit">ثبت</button>'
        f'</form></details>'
    ) if user.role == 'admin' else ''
    corrections_link = ''
    daily_query = urlencode({'start_jalali': start_jalali, 'end_jalali': end_jalali,
                             'period': 'daily',
                             'employee_id': employee_id, 'anchor_day': anchor_day})
    daily_link = (f'<a class="btn outline" href="/reports?{daily_query}">ریز روزانه همین بازه</a>'
                  if period in ('monthly', 'yearly', 'cycle', 'custom') else '')
    options = '<option value="0">تمام گزارش‌های نام‌گذاری‌شده</option>' + ''.join(f'<option value="{id}" {"selected" if id == report_id else ""}>{escape(name)}</option>' for id, name in report_filters(db))
    employee_options = '<option value="0">تمام پرسنل</option>' + ''.join(
        f'<option value="{emp.id}" {"selected" if emp.id == employee_id else ""}>{escape(emp.name)} — {escape(emp.device_id)}</option>'
        for emp in db.scalars(select(Employee).order_by(Employee.name, Employee.id)).all()
    )
    periods = ''.join(f'<option value="{key}" {"selected" if key == period else ""}>{label}</option>' for key, label in [
        ('daily','روزانه · اولین تا آخرین ثبت'), ('monthly','ماهانه شمسی · مجموع حضور'),
        ('yearly','سالانه شمسی · مجموع حضور'), ('cycle','دوره ماهانه شناور · مجموع حضور'),
        ('custom','مجموع کل بازه انتخابی'), ('daily_raw','تمام ثبت‌های خام روز'),
        ('daily_pairs','جفت ثبت‌های متوالی (برای بررسی)')
    ])
    sort_types = (
        ['text', 'text', 'date', 'time', 'time', 'number', 'duration', 'number'] if period == 'daily' else
        ['text', 'text', 'date', 'number', 'time'] if period == 'daily_raw' else
        ['text', 'text', 'date', 'number', 'time', 'time', 'number'] if period == 'daily_pairs' else
        ['text', 'text', 'text', 'number', 'number', 'duration', 'number']
    )
    header = ''.join(
        f'<th aria-sort="none"><button type="button" class="sort-head" '
        f'data-sort-type="{sort_types[index]}" aria-label="مرتب‌سازی بر اساس {escape(label)}">'
        f'{escape(label)}<span class="sort-indicator" aria-hidden="true">↕</span></button></th>'
        for index, label in enumerate(report_headers(period))
    )
    if period == 'daily' and user.role == 'admin':
        header += '<th scope="col">عملیات</th>'
    mobile_sort_options = '<option value="-1">ترتیب اولیه</option>' + ''.join(
        f'<option value="{index}">{escape(label)}</option>' for index, label in enumerate(column_labels)
    )
    body = msg(request) + (
        '<div class="card"><h2>گزارش‌گیری پویا</h2><form method="get"><div class="fields">'
        f'<div><label>از تاریخ شمسی</label><input name="start_jalali" value="{escape(start_jalali)}" placeholder="۱۴۰۵/۰۸/۰۱" required></div>'
        f'<div><label>تا تاریخ شمسی</label><input name="end_jalali" value="{escape(end_jalali)}" placeholder="۱۴۰۵/۰۸/۳۰" required></div>'
        f'<div><label>نام کارمند</label><select name="employee_id">{employee_options}</select></div>'
        f'<div><label>شیوه محاسبه</label><select name="period">{periods}</select></div>'
        f'<div><label>روز شروع دوره شناور (۱ تا ۲۸)</label><input type="number" min="1" max="28" name="anchor_day" value="{anchor_day}" required></div>'
        '<div><button>نمایش گزارش</button></div></div></form>'
        '<p class="report-caution">زمان‌های این گزارش فاصلهٔ ثبت‌های مشاهده‌شده یا اصلاح‌شده هستند؛ '
        'به‌تنهایی کارکرد تأییدشدهٔ حقوق‌ودستمزد محسوب نمی‌شوند.</p>'
        '<details class="report-methodology"><summary>راهنمای محاسبه و نکات مهم گزارش</summary>'
        '<p class="muted">فیلتر تاریخ، کارمند و شیوه محاسبه مستقل‌اند و هم‌زمان اعمال می‌شوند. '
        'در دوره شناور با روز شروع ۲۰، هر دوره از روز ۲۰ ماه قبل تا پایان روز ۱۹ ماه جاری محاسبه می‌شود تا هیچ روزی در دو دوره نیفتد. '
        'در حالت «تمام گزارش‌ها»، روز و ساعتِ ثبت‌شدهٔ تکراری در فایل‌های مختلف فقط یک بار محاسبه می‌شود. '
        'در محاسبه روزانه، فاصله اولین تا آخرین ثبت همان روز در ساعت محلی تهران محاسبه می‌شود؛ '
        'اگر مدیر برای تاریخ و کارمند اصلاح فعالی ثبت کرده باشد، ساعت اصلاحی در این محاسبه جایگزین ساعت متناظر می‌شود. '
        'ثبت‌های اصلی در حالت «تمام ثبت‌های خام» و «جفت ثبت‌ها» بدون تغییر قابل مشاهده‌اند. '
        'روزهایی که هر دو ساعت آن‌ها دستی وارد شده و فاقد ثبت دستگاهی هستند، در حالت «تمام گزارش‌ها» دیده می‌شوند، نه در گزارش یک فایل خاص. '
        'ماهانه، سالانه، دوره شناور و بازه دلخواه مجموع این فاصله‌ها در روزهای داخل بازه انتخابی را نشان می‌دهند. '
        '«روز ناقص» یعنی کمتر از دو ساعت ثبت‌شدهٔ متمایز وجود دارد؛ برای آن زمان حضور حدس زده نمی‌شود. '
        'مجموع‌ها ابتدا با دقت ثانیه جمع و سپس به نزدیک‌ترین دقیقه گرد می‌شوند. '
        '<strong>این رقم فاصلهٔ ثبت‌های خام یا ساعت‌های اصلاحی مدیر است، نه کارکرد تأییدشده حقوق‌ودستمزد:</strong> '
        'استراحت، خروج موقت، شیفت شب، مرخصی و اضافه‌کاری خودکار کسر/محاسبه نمی‌شوند. '
        'حالت خام و جفت‌های متوالی صرفاً برای بررسی بیشتر در دسترس‌اند.</p>'
        '<p class="muted">با کلیک روی عنوان هر ستون، مرتب‌سازی صعودی/نزولی انجام می‌شود. '
        'برای مرتب‌سازی چندسطحی (مثلاً ابتدا نام، سپس تاریخ و بعد نوبت)، Shift را نگه دارید و روی ستون‌های بعدی کلیک کنید. '
        'عدد کنار فلش اولویت مرتب‌سازی را نشان می‌دهد.</p>'
        '</details>'
        + (
            '<div class="actions" style="flex-wrap:wrap;gap:8px">'
            + f'<a class="btn{" outline" if view_filter != "absent" else ""}" href="/reports?' + urlencode({"start_jalali": start_jalali, "end_jalali": end_jalali, "view_filter": "absent"}) + '">غایبین</a>'
            + f'<a class="btn{" outline" if view_filter != "on_leave" else ""}" href="/reports?' + urlencode({"start_jalali": start_jalali, "end_jalali": end_jalali, "view_filter": "on_leave"}) + '">مرخصی‌ها</a>'
            + f'<a class="btn{" outline" if view_filter != "incomplete" else ""}" href="/reports?' + urlencode({"start_jalali": start_jalali, "end_jalali": end_jalali, "view_filter": "incomplete"}) + '">تردد ناقص</a>'
            + (f'<a class="btn outline" href="/reports?{urlencode({"start_jalali": start_jalali, "end_jalali": end_jalali})}">× بستن فیلتر</a>' if view_filter else '')
            + '</div>'
        )
        + f'<div class="actions">{download}{daily_link}<span class="muted">{len(special_rows) if special_rows is not None else len(rows)} ردیف</span></div>'
        f'{add_record_section}</div>'
        + special_table_html
        + (
            '<div class="report-mobile-sort">'
            f'<div><label for="report-sort-field">مرتب‌سازی گزارش بر اساس</label><select id="report-sort-field">{mobile_sort_options}</select></div>'
            '<div><label for="report-sort-direction">جهت مرتب‌سازی</label><select id="report-sort-direction">'
            '<option value="1">صعودی</option><option value="-1">نزولی</option></select></div></div>'
            '<section class="card report-table-panel" aria-label="نتایج گزارش">'
            '<p class="report-scroll-hint">↔ جدول قابل پیمایش افقی است؛ ستون عملیات در دسترس می‌ماند. '
            'برای حرکت با صفحه‌کلید، داخل جدول کلیک کنید یا با Tab روی آن بروید و از کلیدهای جهت‌دار استفاده کنید.</p>'
            f'<div class="report-table-scroll" role="region" aria-label="جدول نتایج حضور" tabindex="0">'
            f'<table id="attendance-report-table" class="sortable-table">'
            f'<thead><tr>{header}</tr></thead><tbody>{table}</tbody></table></div>'
            + ('<p class="muted" role="status">در بازه و فیلتر انتخاب‌شده ردیفی یافت نشد.</p>' if not rows else '')
            + '</section>'
            if not view_filter else ''
        )
    )
    return page('گزارش حضور', body, request, db, 'reports')


@app.get('/reports/download')
def download(request: Request, start_jalali: str, end_jalali: str, report_id: int = 0, period: str = 'daily', employee_id: int = 0, anchor_day: int = 20, db: Session = Depends(get_db)):
    user = permit(request, db, 'admin', 'operator', 'exporter')
    rows = report_rows(db, start_jalali, end_jalali, report_id, period, employee_id, anchor_day)
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = Workbook(); ws = wb.active; ws.title = 'گزارش حضور'
    ws.append(['ردیف'] + report_headers(period))
    for i, row in enumerate(rows, start=1):
        ws.append([i] + [f"'{x}" if isinstance(x, str) and x.lstrip().startswith(('=', '+', '-', '@')) else x for x in row])
    for cell in ws[1]:
        cell.font = Font(bold=True, color='FFFFFF'); cell.fill = PatternFill('solid', fgColor='254D59'); cell.alignment = Alignment(horizontal='right')
    for col, width in {'A': 7, 'B': 36, 'C': 22, 'D': 34, 'E': 20, 'F': 20, 'G': 25, 'H': 28, 'I': 20}.items(): ws.column_dimensions[col].width = width
    ws.auto_filter.ref = ws.dimensions
    ws.freeze_panes = 'A2'; ws.sheet_view.rightToLeft = True
    buffer = io.BytesIO(); wb.save(buffer); buffer.seek(0)
    audit(db, user, 'report_download', f'{start_jalali}..{end_jalali} period={period} report={report_id} employee={employee_id} anchor={anchor_day}'); db.commit()
    now_j = jdatetime.datetime.now(TZ)
    fname = f'attendance_{now_j.strftime("%Y-%m-%d_%H-%M")}.xlsx'
    return StreamingResponse(buffer, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={'Content-Disposition': f'attachment; filename="{fname}"'})


@app.get('/backups')
def backup_status_page(request: Request, db: Session = Depends(get_db)):
    """Admin-only overview from a safe host-published status snapshot."""
    permit(request, db, 'admin')
    data = read_public_backup_status()
    labels = {
        'healthy': ('سالم', 'آخرین بکاپ تأییدشده موجود است و زمان‌بند فعال است.'),
        'stale': ('قدیمی', 'از زمان آخرین بکاپ تأییدشده بیش از حد انتظار گذشته است.'),
        'warning': ('نیازمند بررسی', 'زمان‌بند یا اطلاعات بکاپ نیازمند بررسی است.'),
        'unavailable': ('نامشخص', 'امکان دریافت وضعیت معتبر بکاپ وجود ندارد.'),
    }
    label, description = labels[data['state']]

    def stamp(dt):
        if dt is None:
            return '—'
        # Display local time explicitly; underlying status remains UTC.
        local = dt.astimezone(ZoneInfo('Asia/Tehran'))
        return f"{to_jalali(local.date())} - {local:%H:%M:%S}"

    bytes_count = data['size_bytes']
    backup_size = f'{bytes_count / 1024:,.1f} KiB' if bytes_count is not None else '—'
    count = str(data['verified_count']) if data['verified_count'] is not None else '—'
    next_run = data['next_run'] or '—'
    rows = (
        ('وضعیت', label),
        ('آخرین بکاپ تأییدشده (تهران)', stamp(data['last_backup_at'])),
        ('حجم آخرین فایل بکاپ', backup_size),
        ('تعداد بکاپ‌های دارای Manifest معتبر', count),
        ('فعال بودن زمان‌بند', 'بله' if data['timer_active'] else 'خیر'),
        ('اجرای بعدی زمان‌بند', next_run),
        ('زمان آخرین بررسی (تهران)', stamp(data['checked_at'])),
    )
    table = ''.join(f'<tr><th scope="row">{escape(key)}</th><td><bdi>{escape(str(value))}</bdi></td></tr>' for key, value in rows)
    body = (
        '<div class="card"><h2>سلامت نسخه‌های پشتیبان</h2>'
        f'<p>{escape(description)}</p>'
        '<p class="muted">این صفحه صرفاً اطلاعات بدون رمز را از وضعیت منتشرشده روی میزبان می‌خواند؛ '
        'هیچ دسترسی مستقیم به S3/RustFS، عملیات حذف یا بازیابی در برنامه وجود ندارد.</p></div>'
        f'<div class="card table-wrap"><table><tbody>{table}</tbody></table></div>'
        '<div class="card"><h2>محدودهٔ حفاظت</h2>'
        '<p class="muted">بکاپ فعلی از نوع Logical Dump است؛ بازیابی لحظه‌ای (PITR) فعال نیست. '
        'نگهداری RustFS و PostgreSQL روی یک میزبان، جایگزین نسخهٔ خارج از میزبان نمی‌شود.</p></div>'
    )
    return page('پشتیبان‌گیری', body, request, db, 'backups')


@app.get('/users')
def users_page(request: Request, db: Session = Depends(get_db)):
    permit(request, db, 'admin')
    token = csrf_token(request)
    form = (
        '<div class="card"><h2>ایجاد کاربر سامانه</h2>'
        '<form method="post" action="/users" autocomplete="off" class="fields">'
        '<div><label for="create-username">نام کاربری</label>'
        '<input id="create-username" name="username" autocomplete="off" autocapitalize="none" spellcheck="false" required maxlength="80"></div>'
        '<div><label for="create-password">گذرواژه (حداقل ۱۲ نویسه)</label>'
        '<input id="create-password" name="password" type="password" autocomplete="new-password" minlength="12" required></div>'
        '<div><label for="create-role">نقش</label><select id="create-role" name="role">'
        '<option value="viewer">فقط مشاهده</option><option value="exporter">مشاهده و دانلود</option>'
        '<option value="operator">آپلود و دانلود</option><option value="secretary">منشی · فرایند قدیمی دومرحله‌ای</option>'
        '<option value="admin">مدیر سیستم / ادمین</option></select></div>'
        f'<div><input type="hidden" name="csrf_token" value="{token}"><button>ایجاد حساب</button></div>'
        '</form></div>'
    )
    accounts = list(db.scalars(select(User).order_by(User.id)).all())
    reset_options = (
        '<option value="" selected disabled>لطفاً کاربر را انتخاب کنید</option>'
        + ''.join(f'<option value="{account.id}">{escape(account.username)} ({escape(account.role)})</option>' for account in accounts)
    )
    reset_form = (
        '<div class="card"><h2>بازنشانی رمز عبور کاربر</h2>'
        '<p class="muted">فقط مدیر می‌تواند رمز را بازنشانی کند. نشست‌های قبلی کاربر پس از تغییر رمز باطل می‌شوند.</p>'
        '<form method="post" action="/users/reset-password" class="fields">'
        f'<div><label for="reset-target">حساب کاربری</label><select id="reset-target" name="target_user_id" required>{reset_options}</select></div>'
        '<div><label for="reset-admin-password">رمز فعلی مدیر برای تأیید</label>'
        '<input id="reset-admin-password" name="admin_password" type="password" autocomplete="current-password" required></div>'
        '<div><label for="reset-new-password">رمز جدید (حداقل ۱۲ نویسه)</label>'
        '<input id="reset-new-password" name="new_password" type="password" autocomplete="new-password" minlength="12" maxlength="128" required></div>'
        '<div><label for="reset-confirm-password">تکرار رمز جدید</label>'
        '<input id="reset-confirm-password" name="confirm_password" type="password" autocomplete="new-password" minlength="12" maxlength="128" required></div>'
        f'<div><input type="hidden" name="csrf_token" value="{token}"><button>بازنشانی رمز عبور</button></div>'
        '</form></div>'
    )
    rows = ''
    for user in accounts:
        actions = f'<form method="post" action="/users/{user.id}/toggle"><input type="hidden" name="csrf_token" value="{token}"><button class="outline">{"غیرفعال‌سازی" if user.active else "فعال‌سازی"}</button></form>'
        rows += f'<tr><td>{escape(user.username)}</td><td>{escape(user.role)}</td><td>{"فعال" if user.active else "غیرفعال"}</td><td>{actions}</td></tr>'
    bulk_form = (
        '<div class="card"><h2>بارگذاری گروهی کاربران از Excel</h2>'
        '<p class="muted">فایل را بر اساس قالب استاندارد تهیه کنید. ستون‌های الزامی: نام کاربری، گذرواژه (حداقل ۱۲ نویسه)، نقش.</p>'
        '<p><a class="btn outline" href="/users/template">دانلود قالب Excel</a></p>'
        '<form method="post" enctype="multipart/form-data" action="/users/bulk" class="fields">'
        '<div><label>فایل Excel کاربران (.xlsx)</label>'
        '<input type="file" name="file" accept=".xlsx" required></div>'
        f'<div><input type="hidden" name="csrf_token" value="{token}"><button>بارگذاری و ایجاد کاربران</button></div>'
        '</form></div>'
    )
    return page('مدیریت کاربران', msg(request) + form + bulk_form + reset_form + f'<div class="card table-wrap"><table><tr><th>کاربر</th><th>نقش</th><th>وضعیت</th><th>عملیات</th></tr>{rows}</table></div>', request, db, 'users')


@app.post('/users')
def create_user(request: Request, username: str = Form(), password: str = Form(), role: str = Form(), csrf_token: str = Form(), db: Session = Depends(get_db)):
    user = permit(request, db, 'admin'); csrf(request, csrf_token)
    if role not in ('admin', 'operator', 'viewer', 'exporter', 'secretary') or len(password) < 12 or not username.strip() or len(username) > 80:
        raise HTTPException(400, 'اطلاعات کاربر نامعتبر است')
    if db.scalar(select(User.id).where(User.username == username.strip())):
        return redirect('/users', 'نام کاربری قبلاً ثبت شده است')
    db.add(User(username=username.strip(), password_hash=password_hash(password), role=role)); audit(db, user, 'user_create', username); db.commit()
    return redirect('/users', 'کاربر ایجاد شد')


@app.post('/users/reset-password')
def reset_user_password(
    request: Request,
    target_user_id: int = Form(),
    admin_password: str = Form(),
    new_password: str = Form(),
    confirm_password: str = Form(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    admin = permit(request, db, 'admin')
    csrf(request, csrf_token)
    if not password_ok(admin_password, admin.password_hash):
        return redirect('/users', 'رمز فعلی مدیر صحیح نیست')
    if new_password != confirm_password:
        return redirect('/users', 'رمز جدید و تکرار آن یکسان نیستند')
    if not 12 <= len(new_password) <= 128 or not new_password.strip():
        raise HTTPException(400, 'رمز جدید باید بین ۱۲ تا ۱۲۸ نویسه باشد')
    target = db.get(User, target_user_id)
    if target is None:
        raise HTTPException(404, 'حساب کاربری پیدا نشد')
    if password_ok(new_password, target.password_hash):
        return redirect('/users', 'رمز جدید نباید با رمز فعلی کاربر یکسان باشد')
    target.password_hash = password_hash(new_password)
    audit(db, admin, 'user_password_reset', f'user={target.username} id={target.id}')
    db.commit()
    if target.id == admin.id:
        request.session.clear()
        return redirect('/login', 'رمز حساب شما تغییر کرد؛ دوباره وارد شوید')
    return redirect('/users', 'رمز کاربر بازنشانی شد؛ نشست‌های قبلی او منقضی شدند')


@app.post('/users/{user_id}/toggle')
def toggle_user(user_id: int, request: Request, csrf_token: str = Form(), db: Session = Depends(get_db)):
    actor_user = permit(request, db, 'admin'); csrf(request, csrf_token)
    target = db.get(User, user_id)
    if not target: raise HTTPException(404)
    if target.id == actor_user.id: return redirect('/users', 'امکان غیرفعال‌سازی حساب خودتان وجود ندارد')
    target.active = not target.active; audit(db, actor_user, 'user_toggle', target.username); db.commit()
    return redirect('/users', 'وضعیت حساب تغییر کرد')


# ─── Excel template generator ──────────────────────────────────────────────

def _make_user_template() -> bytes:
    """Generate a fresh xlsx template for bulk user import."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = 'کاربران'
    ws.sheet_view.rightToLeft = True

    headers = [('نام کاربری', 25), ('گذرواژه', 25), ('نقش', 20)]
    for col, (h, width) in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = Font(bold=True, color='FFFFFF', name='Calibri')
        cell.fill = PatternFill('solid', fgColor='1E3A5F')
        cell.alignment = Alignment(horizontal='center', vertical='center')
        ws.column_dimensions[chr(64 + col)].width = width

    dv = DataValidation(type='list',
                        formula1='"viewer,exporter,operator,secretary,admin"',
                        allow_blank=False, showDropDown=False)
    ws.add_data_validation(dv)
    dv.sqref = 'C2:C10000'

    examples = [
        ('ahmad.karimi',    'P@ss1234567890', 'viewer'),
        ('sara.mohammadi',  'SecurePass!123', 'operator'),
    ]
    for row_idx, (u, p, r) in enumerate(examples, 2):
        ws.cell(row=row_idx, column=1, value=u)
        ws.cell(row=row_idx, column=2, value=p)
        ws.cell(row=row_idx, column=3, value=r)

    ns = wb.create_sheet('راهنما')
    ns.sheet_view.rightToLeft = True
    ns.column_dimensions['A'].width = 20
    ns.column_dimensions['B'].width = 45
    guide = [
        ('ستون',      'توضیح'),
        ('نام کاربری', 'الزامی · حداکثر ۸۰ نویسه · بدون فاصله'),
        ('گذرواژه',   'الزامی · حداقل ۱۲ نویسه'),
        ('نقش',       'مقدار از لیست زیر انتخاب شود'),
        ('', ''),
        ('viewer',    'فقط مشاهده'),
        ('exporter',  'مشاهده و دانلود'),
        ('operator',  'آپلود و دانلود'),
        ('secretary', 'منشی · فرایند ثبت مدیران'),
        ('admin',     'مدیر سیستم / ادمین'),
    ]
    for r, (a, b) in enumerate(guide, 1):
        ns.cell(row=r, column=1, value=a)
        ns.cell(row=r, column=2, value=b)
        if r == 1:
            for c in (1, 2):
                cell = ns.cell(row=1, column=c)
                cell.font = Font(bold=True)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@app.get('/users/template')
def users_template(request: Request, db: Session = Depends(get_db)):
    permit(request, db, 'admin')
    data = _make_user_template()
    return Response(
        content=data,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename="users-template.xlsx"'},
    )


@app.post('/users/bulk')
async def users_bulk(
    request: Request,
    file: UploadFile = File(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    import io as _io
    from openpyxl import load_workbook as _load_workbook

    admin = permit(request, db, 'admin')
    csrf(request, csrf_token)
    if not (file.filename or '').lower().endswith('.xlsx'):
        return redirect('/users', 'فقط فایل xlsx مجاز است')
    data = await file.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        return redirect('/users', 'حجم فایل بیش از ۲ مگابایت است')
    try:
        wb = _load_workbook(_io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
    except Exception:
        return redirect('/users', 'فایل xlsx نامعتبر یا خراب است')

    VALID_ROLES = {'admin', 'operator', 'viewer', 'exporter', 'secretary'}
    created = skipped = 0
    error_parts: list[str] = []

    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if not row or not any(c for c in row if c not in ('', None)):
            continue
        username = str(row[0]).strip() if row[0] not in ('', None) else ''
        password = str(row[1]).strip() if len(row) > 1 and row[1] not in ('', None) else ''
        role = str(row[2]).strip().lower() if len(row) > 2 and row[2] not in ('', None) else 'viewer'

        if not username or len(username) > 80 or ' ' in username:
            error_parts.append(f'سطر {row_idx}: نام کاربری نامعتبر')
            continue
        if len(password) < 12:
            error_parts.append(f'سطر {row_idx}: گذرواژه کمتر از ۱۲ نویسه')
            continue
        if role not in VALID_ROLES:
            error_parts.append(f'سطر {row_idx}: نقش «{role}» نامعتبر')
            continue
        if db.scalar(select(User.id).where(User.username == username)):
            skipped += 1
            continue
        db.add(User(username=username, password_hash=password_hash(password), role=role))
        audit(db, admin, 'user_create', f'{username} (bulk)')
        created += 1

    db.commit()
    parts = [f'{created} کاربر ایجاد شد']
    if skipped:
        parts.append(f'{skipped} تکراری نادیده گرفته شد')
    if error_parts:
        parts.append(f'{len(error_parts)} خطا: ' + ' | '.join(error_parts[:4]))
    return redirect('/users', ' · '.join(parts))


@app.get('/audit')
def audit_page(request: Request, db: Session = Depends(get_db)):
    permit(request, db, 'admin')
    rows = ''.join(f'<tr><td>{escape(x.actor)}</td><td>{escape(x.event)}</td><td>{escape(x.details)}</td><td>{escape(x.created_at.astimezone(TZ).strftime("%Y-%m-%d %H:%M"))}</td></tr>' for x in db.scalars(select(Audit).order_by(Audit.id.desc()).limit(300)).all())
    return page('تاریخچه فعالیت', f'<div class="card table-wrap"><table><tr><th>کاربر</th><th>رویداد</th><th>جزئیات</th><th>زمان</th></tr>{rows}</table></div>', request, db, 'audit')
