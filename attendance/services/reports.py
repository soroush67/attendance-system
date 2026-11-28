"""Attendance reports. One query function feeds both the HTML table and the
Excel export, so what is downloaded is exactly what was shown."""
import io
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy import select
from sqlalchemy.orm import Session

from attendance.config import get_settings
from attendance.models import Employee, LeaveDay, Log, ReportDay, ReportPunch, ReportSet
from attendance.services.jalali import parse_jalali, to_jalali

PERIODS = {
    'daily': 'روزانه · اولین تا آخرین ثبت',
    'monthly': 'ماهانه شمسی · مجموع روزهای حضور',
    'yearly': 'سالانه شمسی · مجموع روزهای حضور',
    'cycle': 'دوره شناور ماهانه',
}
HEADERS = {
    'daily': ['نام کارمند', 'شناسه دستگاه', 'تاریخ شمسی', 'اولین ثبت', 'آخرین ثبت'],
    'monthly': ['نام کارمند', 'شناسه دستگاه', 'دوره شمسی', 'تعداد روز حضور'],
    'yearly': ['نام کارمند', 'شناسه دستگاه', 'دوره شمسی', 'تعداد روز حضور'],
    'cycle': ['نام کارمند', 'شناسه دستگاه', 'دوره شناور', 'تعداد روز حضور'],
}


class ReportError(ValueError):
    pass


@dataclass(frozen=True)
class ReportQuery:
    start: date
    end: date
    report_id: int
    period: str
    employee_id: int = 0
    anchor_day: int = 20

    @classmethod
    def parse(cls, start_jalali: str, end_jalali: str, report_id: int, period: str,
              employee_id: int = 0, anchor_day: int = 20) -> 'ReportQuery':
        if period not in PERIODS:
            raise ReportError('نوع گزارش نامعتبر است')
        try:
            start, end = parse_jalali(start_jalali), parse_jalali(end_jalali)
        except ValueError as exc:
            raise ReportError(str(exc)) from None
        if end < start:
            raise ReportError('تاریخ پایان نباید پیش از تاریخ شروع باشد')
        if (end - start).days > get_settings().report_max_days:
            raise ReportError(f'بازه گزارش حداکثر {get_settings().report_max_days} روز است')
        if report_id < 0:
            raise ReportError('گزارش نامعتبر است')
        return cls(start, end, report_id, period, employee_id, max(1, min(28, anchor_day)))


def _cycle_key(d: date, anchor: int) -> str:
    """Return 'YYYY/MM' label for the cycle period that date d falls in."""
    if d.day >= anchor:
        # from anchor of this month
        import jdatetime
        jd = jdatetime.date.fromgregorian(date=d)
        return f'{jd.year}/{jd.month:02d}'
    else:
        import jdatetime
        jd = jdatetime.date.fromgregorian(date=d)
        # belongs to previous month's cycle
        if jd.month == 1:
            return f'{jd.year - 1}/12'
        return f'{jd.year}/{jd.month - 1:02d}'


def report_rows(db: Session, q: ReportQuery) -> list[list]:
    tz = get_settings().timezone
    limit = get_settings().report_max_rows
    if q.report_id and not db.get(ReportSet, q.report_id):
        raise ReportError('گزارش انتخاب‌شده پیدا نشد')

    stmt = (select(ReportDay.device_id, ReportDay.local_day, Employee.name, Employee.id)
            .join(Employee, Employee.device_id == ReportDay.device_id)
            .where(ReportDay.local_day >= q.start.isoformat(),
                   ReportDay.local_day <= q.end.isoformat()))
    if q.report_id:
        stmt = stmt.where(ReportDay.report_id == q.report_id)
    if q.employee_id:
        stmt = stmt.where(Employee.id == q.employee_id)

    fetched = db.execute(stmt.limit(limit + 1)).all()
    if len(fetched) > limit:
        raise ReportError(f'نتیجه بیش از {limit} ردیف است؛ بازه یا گزارش را محدودتر کنید')

    unique_days = {(code, day, name, emp_id) for code, day, name, emp_id in fetched}

    if q.period == 'daily':
        punch_stmt = select(ReportPunch.device_id, ReportPunch.local_day,
                            ReportPunch.occurred_at).where(
            ReportPunch.local_day >= q.start.isoformat(),
            ReportPunch.local_day <= q.end.isoformat())
        if q.report_id:
            punch_stmt = punch_stmt.where(ReportPunch.report_id == q.report_id)
        punches: dict[tuple[str, str], set] = {}
        for code, day, stamp in db.execute(punch_stmt.limit(limit * 4)):
            punches.setdefault((code, day), set()).add(stamp.astimezone(tz))

        # leave days index
        leave_set: set[tuple[int, str]] = set()
        leave_rows_q = db.execute(
            select(LeaveDay.employee_id, LeaveDay.local_day).where(
                LeaveDay.local_day >= q.start.isoformat(),
                LeaveDay.local_day <= q.end.isoformat()
            )
        ).all()
        for eid, lday in leave_rows_q:
            leave_set.add((eid, lday))

        rows = []
        for code, day, name, emp_id in unique_days:
            times = sorted(punches.get((code, day), ()))
            first = times[0].strftime('%H:%M') if times else '—'
            last = times[-1].strftime('%H:%M') if len(times) > 1 else '—'
            on_leave = (emp_id, day) in leave_set
            rows.append([name, code, to_jalali(date.fromisoformat(day)), first, last, emp_id, day, on_leave])
        return sorted(rows, key=lambda r: (r[2], r[0]), reverse=True)

    # monthly / yearly / cycle
    counts: dict[tuple[str, str, str], int] = {}
    for code, day, name, emp_id in unique_days:
        j = to_jalali(date.fromisoformat(day))
        if q.period == 'monthly':
            key = (code, name, j[:7])
        elif q.period == 'yearly':
            key = (code, name, j[:4])
        else:  # cycle
            key = (code, name, _cycle_key(date.fromisoformat(day), q.anchor_day))
        counts[key] = counts.get(key, 0) + 1
    return sorted([[name, code, period_key, count]
                   for (code, name, period_key), count in counts.items()],
                  key=lambda r: (r[2], r[0]), reverse=True)


# ── فیلترهای سریع ──────────────────────────────────────────────────────────

def absent_rows(db: Session, start_jalali: str, end_jalali: str,
                employee_id: int = 0) -> list[list]:
    """Employees with no punches at all in the date range."""
    start, end = parse_jalali(start_jalali), parse_jalali(end_jalali)
    stmt = select(Employee).where(Employee.status == 'active')
    if employee_id:
        stmt = stmt.where(Employee.id == employee_id)
    all_emps = {e.device_id: (e.id, e.name) for e in db.scalars(stmt)}
    present_devices = {code for code, in db.execute(
        select(ReportDay.device_id).where(
            ReportDay.local_day >= start.isoformat(),
            ReportDay.local_day <= end.isoformat()
        ).distinct()
    )}
    rows = []
    for d in _date_range(start, end):
        day_str = d.isoformat()
        for device, (eid, name) in all_emps.items():
            if device not in present_devices:
                rows.append([name, device, to_jalali(d)])
    return rows


def on_leave_rows(db: Session, start_jalali: str, end_jalali: str,
                  employee_id: int = 0) -> list[list]:
    start, end = parse_jalali(start_jalali), parse_jalali(end_jalali)
    stmt = (select(LeaveDay, Employee.name, Employee.device_id)
            .join(Employee, Employee.id == LeaveDay.employee_id)
            .where(LeaveDay.local_day >= start.isoformat(),
                   LeaveDay.local_day <= end.isoformat()))
    if employee_id:
        stmt = stmt.where(LeaveDay.employee_id == employee_id)
    rows = []
    for leave, name, device in db.execute(stmt):
        rows.append([name, device, to_jalali(date.fromisoformat(leave.local_day)), leave.leave_type or 'مرخصی'])
    return sorted(rows, key=lambda r: (r[2], r[0]))


def incomplete_rows(db: Session, start_jalali: str, end_jalali: str,
                    employee_id: int = 0) -> list[list]:
    """Days where employee has punches but only one (no in+out pair)."""
    start, end = parse_jalali(start_jalali), parse_jalali(end_jalali)
    tz = get_settings().timezone
    punch_stmt = select(ReportPunch.device_id, ReportPunch.local_day, ReportPunch.occurred_at).where(
        ReportPunch.local_day >= start.isoformat(),
        ReportPunch.local_day <= end.isoformat())
    punches: dict[tuple[str, str], set] = {}
    for code, day, stamp in db.execute(punch_stmt):
        punches.setdefault((code, day), set()).add(stamp)

    emp_map = {e.device_id: (e.id, e.name) for e in db.scalars(
        select(Employee).where(Employee.status == 'active'))}

    rows = []
    for (code, day), times in punches.items():
        if len(times) == 1 and code in emp_map:
            eid, name = emp_map[code]
            if employee_id and eid != employee_id:
                continue
            rows.append([name, code, to_jalali(date.fromisoformat(day))])
    return sorted(rows, key=lambda r: (r[2], r[0]))


def _date_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _safe_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')):
        return "'" + value
    return value


def to_xlsx(rows: list[list], period: str) -> io.BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = 'گزارش حضور'
    headers = HEADERS[period]
    ws.append(headers)
    # strip internal cols (emp_id, day, on_leave) from daily rows before export
    for row in rows:
        export_row = row[:len(headers)]
        ws.append([_safe_cell(x) for x in export_row])
    for cell in ws[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='176B83')
        cell.alignment = Alignment(horizontal='right')
    for col, width in {'A': 32, 'B': 18, 'C': 22, 'D': 21, 'E': 21}.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = 'A2'
    ws.sheet_view.rightToLeft = True
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


def now_local() -> datetime:
    return datetime.now(get_settings().timezone)
