"""Bounded, inert parser for secretary-submitted manager attendance Excel files.

This module has no database side effects. It returns typed candidate rows that
must be revalidated against live manager eligibility before one atomic commit.
"""

from dataclasses import dataclass
from io import BytesIO
import re
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook


MAX_XLSX_BYTES = 2 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 16 * 1024 * 1024
MAX_ROWS = 1000
MAX_SCANNED_ROWS = 2000
HEADERS = ('کد مدیر', 'نام مدیر', 'تاریخ شمسی', 'ساعت ورود', 'ساعت خروج', 'توضیح')
PERSIAN_DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')


class ManagerExcelValidationError(ValueError):
    """Safe, row-oriented error suitable for display to a secretary."""


@dataclass(frozen=True)
class ManagerExcelRow:
    excel_row: int
    device_id: str
    name: str
    day_jalali: str
    first_clock: str
    last_clock: str
    reason: str


def _cell_text(cell, row_number, label, *, optional=False, max_length=500):
    if cell.data_type == 'f':
        raise ManagerExcelValidationError(
            f'ردیف {row_number}: فرمول Excel در ستون «{label}» مجاز نیست')
    value = cell.value
    if value is None or value == '':
        if optional:
            return ''
        raise ManagerExcelValidationError(f'ردیف {row_number}: «{label}» خالی است')
    if not isinstance(value, str):
        raise ManagerExcelValidationError(
            f'ردیف {row_number}: «{label}» باید به صورت متن وارد شود؛ قالب سلول را Text تنظیم کنید')
    result = value.strip()
    if not result and not optional:
        raise ManagerExcelValidationError(f'ردیف {row_number}: «{label}» خالی است')
    if len(result) > max_length:
        raise ManagerExcelValidationError(
            f'ردیف {row_number}: «{label}» بیش از {max_length} نویسه دارد')
    if any(ord(char) < 32 and char not in ('\t', '\n') for char in result):
        raise ManagerExcelValidationError(f'ردیف {row_number}: کاراکتر نامعتبر در «{label}»')
    if result.startswith(('=', '+', '-', '@')):
        raise ManagerExcelValidationError(
            f'ردیف {row_number}: مقدار «{label}» نباید شبیه فرمول Excel باشد')
    return result


def parse_manager_excel(data: bytes) -> list[ManagerExcelRow]:
    if not data or len(data) > MAX_XLSX_BYTES:
        raise ManagerExcelValidationError('فایل Excel خالی است یا بیش از ۲ مگابایت حجم دارد')
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= 150:
                raise ManagerExcelValidationError('ساختار فایل Excel بیش از حد پیچیده است')
            if sum(entry.file_size for entry in entries) > MAX_UNCOMPRESSED_BYTES:
                raise ManagerExcelValidationError('حجم بازشدهٔ فایل بیش از حد مجاز است')
            if any(entry.flag_bits & 1 for entry in entries):
                raise ManagerExcelValidationError('فایل رمزگذاری‌شده پذیرفته نمی‌شود')
            if any('externallinks/' in entry.filename.lower() or
                   'vbaproject' in entry.filename.lower() for entry in entries):
                raise ManagerExcelValidationError('فرمول خارجی و ماکرو در فایل مجاز نیست')
            if '[Content_Types].xml' not in archive.namelist():
                raise ManagerExcelValidationError('فایل Excel معتبر نیست')
            # Bound read-only worksheet expansion even for a hostile far-right
            # formatted cell (XFD); only A..F are part of this import contract.
            for entry in entries:
                if re.fullmatch(r'xl/worksheets/sheet[0-9]+\.xml', entry.filename):
                    xml = archive.read(entry)
                    addresses = re.findall(
                        rb'<(?:[A-Za-z0-9_]+:)?c\b[^>]{0,320}\br="([A-Z]+)[0-9]{1,7}"', xml)
                    if any(column not in (b'A', b'B', b'C', b'D', b'E', b'F')
                           for column in addresses):
                        raise ManagerExcelValidationError('در فایل سلول خارج از شش ستون مجاز وجود دارد')
    except (BadZipFile, OSError):
        raise ManagerExcelValidationError('فایل باید Excel واقعی با پسوند xlsx باشد') from None

    try:
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=False,
                                 keep_links=False)
    except (ValueError, OSError, KeyError, BadZipFile) as exc:
        raise ManagerExcelValidationError('فایل Excel خوانده نمی‌شود یا ساختار نامعتبری دارد') from exc
    try:
        if workbook.sheetnames not in (['تردد مدیران'], ['تردد مدیران', 'راهنما']):
            raise ManagerExcelValidationError('نام و ترتیب برگه‌ها نباید تغییر کند؛ قالب اصلی را دریافت کنید')
        if any(sheet.sheet_state != 'visible' for sheet in workbook.worksheets):
            raise ManagerExcelValidationError('برگهٔ مخفی در فایل پذیرفته نمی‌شود')
        sheet = workbook.worksheets[0]
        rows = []
        seen = set()
        for row_number, cells in enumerate(sheet.iter_rows(max_col=7), start=1):
            if row_number > MAX_SCANNED_ROWS + 1:
                raise ManagerExcelValidationError(
                    f'فایل بیش از {MAX_SCANNED_ROWS} ردیف دارد؛ ردیف‌های اضافی را پاک کنید')
            if row_number == 1:
                labels = tuple(cell.value for cell in cells[:6])
                if labels != HEADERS or any(cell.value is not None for cell in cells[6:]):
                    raise ManagerExcelValidationError('عنوان ستون‌ها تغییر کرده است؛ از قالب اصلی استفاده کنید')
                continue
            if any(cell.value is not None for cell in cells[6:]):
                raise ManagerExcelValidationError(f'ردیف {row_number}: فقط شش ستون اول مجاز است')
            if all(cell.value in (None, '') for cell in cells[:6]):
                continue
            if len(rows) >= MAX_ROWS:
                raise ManagerExcelValidationError(f'حداکثر {MAX_ROWS} ردیف داده در هر فایل مجاز است')
            labels = HEADERS
            # Empty trailing cells in sparse XLSX documents are represented as None.
            def cell_at(position):
                if position < len(cells):
                    return cells[position]
                from openpyxl.cell.read_only import EmptyCell
                return EmptyCell

            code = _cell_text(cell_at(0), row_number, labels[0], max_length=80)
            name = _cell_text(cell_at(1), row_number, labels[1], optional=True, max_length=200)
            day = _cell_text(cell_at(2), row_number, labels[2], max_length=10)
            first = _cell_text(cell_at(3), row_number, labels[3], max_length=8)
            last = _cell_text(cell_at(4), row_number, labels[4], max_length=8)
            reason = _cell_text(cell_at(5), row_number, labels[5], max_length=500)
            day_normalized = day.translate(PERSIAN_DIGITS)
            if not re.fullmatch(r'[0-9]{4}/[0-9]{2}/[0-9]{2}', day_normalized):
                raise ManagerExcelValidationError(
                    f'ردیف {row_number}: تاریخ شمسی باید به شکل ۱۴۰۵/۰۸/۲۵ باشد')
            for value, label in ((first, labels[3]), (last, labels[4])):
                if not re.fullmatch(r'[0-9]{2}:[0-9]{2}(?::[0-9]{2})?',
                                    value.translate(PERSIAN_DIGITS)):
                    raise ManagerExcelValidationError(
                        f'ردیف {row_number}: «{label}» باید HH:MM یا HH:MM:SS باشد')
            if len(reason) < 5:
                raise ManagerExcelValidationError(
                    f'ردیف {row_number}: توضیح باید دست‌کم پنج نویسه داشته باشد')
            key = (code, day_normalized)
            if key in seen:
                raise ManagerExcelValidationError(
                    f'ردیف {row_number}: برای همین مدیر و تاریخ، یک ردیف دیگر وجود دارد')
            seen.add(key)
            rows.append(ManagerExcelRow(row_number, code, name, day, first, last, reason))
        if not rows:
            raise ManagerExcelValidationError('فایل هیچ ردیف تکمیل‌شده‌ای ندارد')
        return rows
    finally:
        workbook.close()
