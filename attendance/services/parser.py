"""Parser for the attendance device export (six tab-separated columns).

    61852775<TAB>2026-11-01 06:36:47<TAB>1<TAB>4<TAB>15<TAB>0

Column 1 is the device ID, column 2 the local timestamp; the other four are
device fields that are validated as part of the layout but not interpreted
(they do not mean check-in/check-out).
"""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


class DeviceFileError(ValueError):
    pass


def decode(data: bytes) -> str:
    for encoding in ('utf-8-sig', 'utf-16', 'cp1256'):
        try:
            content = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if '\x00' not in content:
            return content
    raise DeviceFileError('کدگذاری فایل شناخته نشد')


def parse_device_file(data: bytes, tz: ZoneInfo) -> tuple[list[tuple[str, datetime]], list[int]]:
    """-> ([(device_id, utc timestamp)], [line numbers that failed])."""
    content = decode(data)
    result, failures = [], []
    for line_no, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
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
        except ValueError:
            failures.append(line_no)
            continue
        result.append((identifier, naive.replace(tzinfo=tz).astimezone(timezone.utc)))
    return result, failures
