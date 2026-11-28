"""Attendance reports. One query function feeds both the HTML table and the
Excel export, so what is downloaded is exactly what was shown."""
import io
from dataclasses import dataclass
from datetime import date, datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy import select
from sqlalchemy.orm import Session

from attendance.config import get_settings
from attendance.models import Employee, ReportDay, ReportPunch, ReportSet
from attendance.services.jalali import parse_jalali, to_jalali

PERIODS = {'daily': 'روزانه', 'monthly': 'ماهانه', 'yearly': 'سالانه'}
HEADERS = {
    'daily': ['نام کارمند', 'تاریخ شمسی', 'اولین ثبت', 'آخرین ثبت'],
    'monthly': ['نام کارمند', 'دوره شمسی', 'تعداد روز حضور'],
    'yearly': ['نام کارمند', 'دوره شمسی', 'تعداد روز حضور'],
}


class ReportError(ValueError):
    pass


@dataclass(frozen=True)
class ReportQuery:
    start: date        # inclusive (Gregorian)
    end: date          # inclusive (Gregorian)
    report_id: int     # 0 = all named reports
    period: str

    @classmethod
    def parse(cls, start_jalali: str, end_jalali: str, report_id: int, period: str) -> 'ReportQuery':
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
        return cls(start, end, report_id, period)


def report_rows(db: Session, q: ReportQuery) -> list[list]:
    tz = get_settings().timezone
    limit = get_settings().report_max_rows
    if q.report_id and not db.get(ReportSet, q.report_id):
        raise ReportError('گزارش انتخاب‌شده پیدا نشد')
    stmt = (select(ReportDay.device_id, ReportDay.local_day, Employee.name)
            .join(Employee, Employee.device_id == ReportDay.device_id)
            .where(ReportDay.local_day >= q.start.isoformat(), ReportDay.local_day <= q.end.isoformat()))
    if q.report_id:
        stmt = stmt.where(ReportDay.report_id == q.report_id)
    fetched = db.execute(stmt.limit(limit + 1)).all()
    if len(fetched) > limit:
        raise ReportError(f'نتیجه بیش از {limit} ردیف است؛ بازه یا گزارش را محدودتر کنید')
    # The same employee+day appearing in several named uploads counts once.
    unique_days = {(code, day, name) for code, day, name in fetched}
    if q.period == 'daily':
        punch_stmt = select(ReportPunch.device_id, ReportPunch.local_day, ReportPunch.occurred_at).where(
            ReportPunch.local_day >= q.start.isoformat(), ReportPunch.local_day <= q.end.isoformat())
        if q.report_id:
            punch_stmt = punch_stmt.where(ReportPunch.report_id == q.report_id)
        punches: dict[tuple[str, str], set] = {}
        for code, day, stamp in db.execute(punch_stmt.limit(limit * 4)):
            punches.setdefault((code, day), set()).add(stamp.astimezone(tz))
        rows = []
        for code, day, name in unique_days:
            times = sorted(punches.get((code, day), ()))
            first = times[0].strftime('%H:%M:%S') if times else '—'
            last = times[-1].strftime('%H:%M:%S') if len(times) > 1 else '—'
            rows.append([name, to_jalali(date.fromisoformat(day)), first, last])
        return sorted(rows, key=lambda r: (r[1], r[0]), reverse=True)
    counts: dict[tuple[str, str, str], int] = {}
    for code, day, name in unique_days:
        j = to_jalali(date.fromisoformat(day))
        key = (code, name, j[:7] if q.period == 'monthly' else j[:4])
        counts[key] = counts.get(key, 0) + 1
    return sorted([[name, period_key, count] for (_, name, period_key), count in counts.items()],
                  key=lambda r: (r[1], r[0]), reverse=True)


def _safe_cell(value):
    # Excel formula injection: a name like "=HYPERLINK(...)" must stay text.
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')):
        return "'" + value
    return value


def to_xlsx(rows: list[list], period: str) -> io.BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = 'گزارش حضور'
    ws.append(HEADERS[period])
    for row in rows:
        ws.append([_safe_cell(x) for x in row])
    for cell in ws[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='176B83')
        cell.alignment = Alignment(horizontal='right')
    for col, width in {'A': 36, 'B': 22, 'C': 21, 'D': 21}.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = 'A2'
    ws.sheet_view.rightToLeft = True
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


def now_local() -> datetime:
    return datetime.now(get_settings().timezone)
