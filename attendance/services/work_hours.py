"""Work-hours calculation helpers.

paired_duration_minutes  — جفت‌بندی پانچ‌ها و محاسبه کارکرد واقعی
day_status               — وضعیت روز (حاضر / کسری / غایب / مرخصی / اضافه‌کاری / تعطیل)
month_grid               — شبکه ۳۰ روزه ماهانه برای یک کارمند
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from attendance.models import AttendanceCorrection, Employee, LeaveDay, Log, WorkSettings
from attendance.services.jalali import to_jalali, parse_jalali

TZ = timezone(timedelta(hours=3, minutes=30))   # Asia/Tehran

# روزهای هفته به فرمت پایتون isoweekday(): شنبه=6, یکشنبه=7, ... جمعه=5
_DAY_NAME = {6: 'sat', 7: 'sun', 1: 'mon', 2: 'tue', 3: 'wed', 4: 'thu', 5: 'fri'}
_FA_WEEKDAY = {6: 'شنبه', 7: 'یکشنبه', 1: 'دوشنبه', 2: 'سه‌شنبه',
               3: 'چهارشنبه', 4: 'پنجشنبه', 5: 'جمعه'}


def get_settings(db: Session) -> WorkSettings:
    ws = db.get(WorkSettings, 1)
    if ws is None:
        ws = WorkSettings(id=1)
        db.add(ws)
        db.flush()
    return ws


def paired_duration_minutes(punches: Sequence[datetime]) -> int:
    """کارکرد واقعی بر اساس جفت‌بندی پانچ‌ها (دقیقه).

    پانچ فرد = ورود، پانچ زوج = خروج.
    اگر تعداد پانچ‌ها فرد باشد آخرین پانچ بدون جفت رها می‌شود.
    """
    if not punches:
        return 0
    sorted_p = sorted(punches)
    total = 0
    for i in range(0, len(sorted_p) - 1, 2):
        delta = sorted_p[i + 1] - sorted_p[i]
        total += max(0, int(delta.total_seconds() // 60))
    return total


STATUS_PRESENT  = 'حاضر'
STATUS_DEFICIT  = 'کسری'
STATUS_ABSENT   = 'غایب'
STATUS_LEAVE    = 'مرخصی'
STATUS_OVERTIME = 'اضافه‌کاری'
STATUS_HOLIDAY  = 'تعطیل'


@dataclass
class DayEntry:
    local_day: str          # YYYY-MM-DD شمسی
    weekday_fa: str         # نام روز فارسی
    is_workday: bool
    is_overtime_day: bool
    on_leave: bool
    punches: list[datetime] = field(default_factory=list)
    corrections: list[AttendanceCorrection] = field(default_factory=list)

    @property
    def all_times(self) -> list[datetime]:
        """پانچ‌های اصلی + اصلاح‌شده ترکیب‌شده و مرتب."""
        base = list(self.punches)
        for c in self.corrections:
            # زمان اصلاح را به datetime تبدیل کن
            day_greg = parse_jalali(self.local_day)
            h, m, s = (int(x) for x in c.punch_time.split(':'))
            dt = datetime(day_greg.year, day_greg.month, day_greg.day,
                          h, m, s, tzinfo=TZ)
            base.append(dt)
        return sorted(base)

    @property
    def worked_minutes(self) -> int:
        return paired_duration_minutes(self.all_times)

    @property
    def first_punch(self) -> datetime | None:
        t = self.all_times
        return t[0] if t else None

    @property
    def last_punch(self) -> datetime | None:
        t = self.all_times
        return t[-1] if t else None

    def status(self, standard_hours: int) -> str:
        if self.on_leave:
            return STATUS_LEAVE
        if self.is_overtime_day:
            return STATUS_OVERTIME if self.worked_minutes > 0 else STATUS_HOLIDAY
        if not self.is_workday:
            return STATUS_HOLIDAY
        if self.worked_minutes == 0:
            return STATUS_ABSENT
        threshold = standard_hours * 60
        return STATUS_PRESENT if self.worked_minutes >= threshold else STATUS_DEFICIT

    def deficit_minutes(self, standard_hours: int) -> int:
        """کسری دقیقه — صفر برای مرخصی / تعطیل / اضافه‌کاری."""
        st = self.status(standard_hours)
        if st == STATUS_DEFICIT:
            return standard_hours * 60 - self.worked_minutes
        return 0

    def overtime_minutes(self, standard_hours: int) -> int:
        if self.status(standard_hours) == STATUS_OVERTIME:
            return self.worked_minutes
        if self.status(standard_hours) == STATUS_PRESENT:
            return max(0, self.worked_minutes - standard_hours * 60)
        return 0


@dataclass
class MonthSummary:
    total_worked_min: int = 0
    total_deficit_min: int = 0
    total_overtime_min: int = 0
    leave_days: int = 0
    absent_days: int = 0
    present_days: int = 0
    workday_count: int = 0


def month_grid(
    db: Session,
    employee: Employee,
    year_j: int,
    month_j: int,
) -> tuple[list[DayEntry], MonthSummary, WorkSettings]:
    """شبکه روزانه یک ماه شمسی برای کارمند.

    برمی‌گرداند: (لیست DayEntry، خلاصه ماه، تنظیمات کار)
    """
    ws = get_settings(db)
    work_day_set = set(ws.work_days.split(','))
    overtime_day_set = set(ws.overtime_days.split(','))

    # محدوده شمسی → میلادی
    from attendance.services.jalali import jalali_month_range
    start_greg, end_greg = jalali_month_range(year_j, month_j)

    # پانچ‌های خام کارمند در این بازه
    logs = db.scalars(
        select(Log)
        .where(
            Log.device_id == employee.device_id,
            Log.occurred_at >= datetime(start_greg.year, start_greg.month,
                                        start_greg.day, tzinfo=TZ),
            Log.occurred_at < datetime(end_greg.year, end_greg.month,
                                       end_greg.day, tzinfo=TZ) + timedelta(days=1),
        )
        .order_by(Log.occurred_at)
    ).all()

    # گروه‌بندی پانچ‌ها بر اساس روز شمسی
    punches_by_day: dict[str, list[datetime]] = {}
    for log in logs:
        local_dt = log.occurred_at.astimezone(TZ)
        day_key = to_jalali(local_dt.date())
        punches_by_day.setdefault(day_key, []).append(log.occurred_at)

    # مرخصی‌های این ماه
    leave_prefix = f'{year_j:04d}-{month_j:02d}-'
    leave_rows = db.scalars(
        select(LeaveDay)
        .where(
            LeaveDay.employee_id == employee.id,
            LeaveDay.local_day.like(leave_prefix + '%'),
        )
    ).all()
    leave_set = {r.local_day for r in leave_rows}

    # اصلاح‌های این ماه
    corrections = db.scalars(
        select(AttendanceCorrection)
        .where(
            AttendanceCorrection.employee_id == employee.id,
            AttendanceCorrection.local_day.like(leave_prefix + '%'),
        )
        .order_by(AttendanceCorrection.local_day, AttendanceCorrection.punch_time)
    ).all()
    corrections_by_day: dict[str, list[AttendanceCorrection]] = {}
    for c in corrections:
        corrections_by_day.setdefault(c.local_day, []).append(c)

    # ساخت شبکه روزانه
    entries: list[DayEntry] = []
    cur = start_greg
    while cur <= end_greg:
        day_key = to_jalali(cur)
        iso = cur.isoweekday()
        day_name = _DAY_NAME[iso]
        entry = DayEntry(
            local_day=day_key,
            weekday_fa=_FA_WEEKDAY[iso],
            is_workday=day_name in work_day_set,
            is_overtime_day=day_name in overtime_day_set,
            on_leave=day_key in leave_set,
            punches=punches_by_day.get(day_key, []),
            corrections=corrections_by_day.get(day_key, []),
        )
        entries.append(entry)
        cur += timedelta(days=1)

    # خلاصه ماه
    summary = MonthSummary()
    for e in entries:
        st = e.status(ws.standard_hours)
        if e.is_workday:
            summary.workday_count += 1
        summary.total_worked_min += e.worked_minutes
        summary.total_deficit_min += e.deficit_minutes(ws.standard_hours)
        summary.total_overtime_min += e.overtime_minutes(ws.standard_hours)
        if st == STATUS_LEAVE:
            summary.leave_days += 1
        elif st == STATUS_ABSENT:
            summary.absent_days += 1
        elif st in (STATUS_PRESENT, STATUS_DEFICIT):
            summary.present_days += 1

    return entries, summary, ws
