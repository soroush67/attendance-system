"""Device file ingestion and deletion (same rules as v1.3).

A file is accepted only as a whole: one unknown device ID or unreadable line
rejects it, so no partial report is ever created. Raw punches are stored once
(`attendance_logs`), while every named report keeps its own day/punch rows.
"""
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from attendance.config import get_settings
from attendance.models import AttendanceRecord, Employee, Import, Log, ReportDay, ReportPunch, ReportSet, User
from attendance.services.parser import DeviceFileError, parse_device_file

ALLOWED_SUFFIXES = ('.dat', '.txt')


class IngestError(ValueError):
    pass


@dataclass
class IngestResult:
    report_id: int
    report_name: str
    first_day: date
    last_day: date
    accepted: int
    duplicates: int


def normalize_report_name(name: str) -> str:
    return ' '.join(name.strip().split())


def _day_bounds(day: str):
    tz = get_settings().timezone
    start = datetime.fromisoformat(day).replace(tzinfo=tz).astimezone(timezone.utc)
    end = (datetime.fromisoformat(day) + timedelta(days=1)).replace(tzinfo=tz).astimezone(timezone.utc)
    return start, end


def _rebuild_day(db: Session, device_id: str, day: str) -> None:
    start, end = _day_bounds(day)
    times = list(db.scalars(select(Log.occurred_at).where(
        Log.device_id == device_id, Log.occurred_at >= start, Log.occurred_at < end).order_by(Log.occurred_at)))
    record = db.scalar(select(AttendanceRecord).where(
        AttendanceRecord.device_id == device_id, AttendanceRecord.local_day == day))
    if not times:
        if record:
            db.delete(record)
        return
    if not record:
        record = AttendanceRecord(device_id=device_id, local_day=day, first_punch=times[0], punch_count=len(times))
        db.add(record)
    record.first_punch = times[0]
    record.last_punch = times[-1] if len(times) > 1 else None
    record.punch_count = len(times)


def ingest(db: Session, user: User, filename: str, data: bytes, report_name: str) -> IngestResult:
    tz = get_settings().timezone
    report_name = normalize_report_name(report_name)
    if not report_name or len(report_name) > 120:
        raise IngestError('نام گزارش باید بین ۱ تا ۱۲۰ نویسه باشد')
    filename = Path(filename or '').name[:255]
    if not filename.lower().endswith(ALLOWED_SUFFIXES):
        raise IngestError('فقط فایل‌های ‎.dat‎ یا ‎.txt‎ دستگاه مجاز هستند')
    if db.scalar(select(ReportSet.id).where(ReportSet.name == report_name)):
        raise IngestError('این نام گزارش قبلاً ثبت شده؛ نام دیگری انتخاب کنید')
    try:
        parsed, failures = parse_device_file(data, tz)
    except DeviceFileError as exc:
        raise IngestError(str(exc)) from None
    if failures:
        raise IngestError(f'ثبت متوقف شد: {len(failures)} سطر قابل خواندن نیست (اولین سطر: {failures[0]})')
    if not parsed:
        raise IngestError('هیچ رکورد معتبری در فایل پیدا نشد')
    codes = {code for code, _ in parsed}
    known = set(db.scalars(select(Employee.device_id).where(Employee.device_id.in_(codes))))
    unknown = sorted(codes - known)
    if unknown:
        raise IngestError(f'ثبت متوقف شد: {len(unknown)} شماره در فهرست کارکنان تعریف نشده است. نمونه: {", ".join(unknown[:5])}')

    # Uniqueness belongs to a named report, not the raw file alone.
    digest = hashlib.sha256(data + b'\x00' + report_name.encode('utf-8')).hexdigest()
    unique = set(parsed)
    existing = set(db.execute(select(Log.device_id, Log.occurred_at).where(
        Log.device_id.in_(codes),
        Log.occurred_at.between(min(t for _, t in unique), max(t for _, t in unique)))).all())
    pending = unique - existing

    imp = Import(filename=filename, digest=digest, uploader_id=user.id, accepted=len(pending),
                 rejected=len(unique) - len(pending), note='پردازش موفق')
    db.add(imp)
    db.flush()
    report = ReportSet(import_id=imp.id, name=report_name)
    db.add(report)
    db.flush()
    local = {(code, stamp, stamp.astimezone(tz).date().isoformat()) for code, stamp in unique}
    for code, day in {(code, day) for code, _, day in local}:
        db.add(ReportDay(report_id=report.id, device_id=code, local_day=day))
    for code, stamp, day in local:
        db.add(ReportPunch(report_id=report.id, device_id=code, local_day=day, occurred_at=stamp))
    for code, stamp in sorted(pending):
        db.add(Log(device_id=code, occurred_at=stamp, source_import_id=imp.id))
    db.flush()
    for code, day in {(code, stamp.astimezone(tz).date().isoformat()) for code, stamp in pending}:
        _rebuild_day(db, code, day)
    days = sorted(date.fromisoformat(day) for _, _, day in local)
    return IngestResult(report.id, report_name, days[0], days[-1], len(pending), len(unique) - len(pending))


def punches_of_import(db: Session, import_id: int) -> int:
    return db.scalar(select(func.count()).select_from(Log).where(Log.source_import_id == import_id)) or 0


def delete_import(db: Session, item: Import) -> int:
    """Remove an upload, its named report and its raw punches; rebuild the
    affected days from the punches of other uploads. Returns punches removed."""
    tz = get_settings().timezone
    punches = list(db.scalars(select(Log).where(Log.source_import_id == item.id)))
    affected = {(p.device_id, p.occurred_at.astimezone(tz).date().isoformat()) for p in punches}
    report = db.scalar(select(ReportSet).where(ReportSet.import_id == item.id))
    if report:
        for row in db.scalars(select(ReportPunch).where(ReportPunch.report_id == report.id)):
            db.delete(row)
        for row in db.scalars(select(ReportDay).where(ReportDay.report_id == report.id)):
            db.delete(row)
        db.flush()
        db.delete(report)
        db.flush()
    for punch in punches:
        db.delete(punch)
    db.flush()
    for code, day in affected:
        _rebuild_day(db, code, day)
    db.delete(item)
    return len(punches)
