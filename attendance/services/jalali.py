"""Jalali (Persian calendar) conversions."""
import re
from datetime import date

import jdatetime

_JALALI_MONTH_DAYS = [0, 31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]

_DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')


def to_jalali(day: date) -> str:
    value = jdatetime.date.fromgregorian(date=day)
    return f'{value.year:04d}/{value.month:02d}/{value.day:02d}'


def parse_jalali(value: str) -> date:
    """'۱۴۰۵/۰۸/۰۱' or '1405-8-1' -> Gregorian date; ValueError when invalid
    (including days that do not exist, e.g. 30 Esfand in a common year)."""
    normalized = value.strip().translate(_DIGITS).replace('-', '/')
    match = re.fullmatch(r'(\d{4})/(\d{1,2})/(\d{1,2})', normalized)
    if not match:
        raise ValueError('فرمت تاریخ باید مانند ۱۴۰۵/۰۸/۰۱ باشد')
    return jdatetime.date(*(int(x) for x in match.groups())).togregorian()


def jalali_month_range(year: int, month: int) -> tuple[date, date]:
    """اول و آخر یک ماه شمسی به صورت تاریخ میلادی."""
    start = jdatetime.date(year, month, 1).togregorian()
    # اسفند سال کبیسه ۳۰ روز دارد
    last_day = jdatetime.date(year, month, 1)
    if month == 12:
        days_in_month = 30 if jdatetime.date(year, month, 1).isleap() else 29
    else:
        days_in_month = _JALALI_MONTH_DAYS[month]
    end = jdatetime.date(year, month, days_in_month).togregorian()
    return start, end


def current_jalali_ym() -> tuple[int, int]:
    """ماه و سال جاری شمسی."""
    today = jdatetime.date.today()
    return today.year, today.month


def jalali_month_name(month: int) -> str:
    names = ['فروردین', 'اردیبهشت', 'خرداد', 'تیر', 'مرداد', 'شهریور',
             'مهر', 'آبان', 'آذر', 'دی', 'بهمن', 'اسفند']
    return names[month - 1]
