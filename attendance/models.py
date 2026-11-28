"""ORM models.

Tables, columns and constraint names up to `Audit` are identical to v1.3 (which
created them with create_all), so an existing production database is adopted
by `alembic stamp 0001` without any change. Everything newer comes from
versioned migrations in migrations/versions.
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Employee(Base):
    __tablename__ = 'employees'
    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default='active')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


ROLES = {
    'viewer': 'فقط مشاهده',
    'exporter': 'مشاهده و دانلود',
    'operator': 'آپلود و دانلود',
    'admin': 'مدیر سامانه',
}


class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(250))
    role: Mapped[str] = mapped_column(String(20), default='viewer')
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 0002: login hardening
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True, server_default='false')
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Import(Base):
    __tablename__ = 'imports'
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    uploader_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    accepted: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(String(500), default='')


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


class Audit(Base):
    __tablename__ = 'audit_logs'
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(80))
    event: Mapped[str] = mapped_column(String(80))
    details: Mapped[str] = mapped_column(String(300), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LoginAttempt(Base):
    """0002: every login attempt, for per-IP throttling and the security audit."""
    __tablename__ = 'login_attempts'
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80))
    ip: Mapped[str] = mapped_column(String(64), index=True)
    success: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# ── 0003: work-hours & leave ──────────────────────────────────────────────────

WORK_DAYS_DEFAULT = 'sat,sun,mon,tue,wed,thu'   # شنبه تا پنجشنبه
OVERTIME_DAYS_DEFAULT = 'fri'                   # جمعه = اضافه‌کاری


class WorkSettings(Base):
    """تنظیمات ساعت کاری سیستم — همیشه یک ردیف با id=1."""
    __tablename__ = 'work_settings'
    id: Mapped[int] = mapped_column(primary_key=True)
    standard_hours: Mapped[int] = mapped_column(Integer, default=8)
    # comma-separated day names: sat sun mon tue wed thu fri
    work_days: Mapped[str] = mapped_column(String(60), default=WORK_DAYS_DEFAULT)
    overtime_days: Mapped[str] = mapped_column(String(60), default=OVERTIME_DAYS_DEFAULT)


class LeaveDay(Base):
    """مرخصی ثبت‌شده برای یک کارمند در یک روز.
    start_time/end_time=None → تمام روز؛ مقداردار → مرخصی ساعتی (HH:MM:SS)."""
    __tablename__ = 'leave_days'
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey('employees.id'), nullable=False)
    local_day: Mapped[str] = mapped_column(String(10), nullable=False)   # YYYY-MM-DD gregorian
    leave_type: Mapped[str] = mapped_column(String(30), default='مرخصی')
    start_time: Mapped[str | None] = mapped_column(String(8), nullable=True)  # HH:MM:SS یا null
    end_time: Mapped[str | None] = mapped_column(String(8), nullable=True)    # HH:MM:SS یا null
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        Index('ix_leave_days_local_day', 'local_day'),
        Index('ix_leave_days_employee_day', 'employee_id', 'local_day'),
    )


class AttendanceCorrection(Base):
    """اصلاح دستی ساعت ورود یا خروج توسط اپراتور."""
    __tablename__ = 'attendance_corrections'
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey('employees.id'), nullable=False)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False)
    local_day: Mapped[str] = mapped_column(String(10), nullable=False)   # YYYY-MM-DD شمسی
    punch_time: Mapped[str] = mapped_column(String(8), nullable=False)   # HH:MM:SS
    punch_type: Mapped[str] = mapped_column(String(10), nullable=False)  # 'in' | 'out'
    reason: Mapped[str] = mapped_column(String(300), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index('ix_corrections_employee_day', 'employee_id', 'local_day'),)
