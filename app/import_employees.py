"""Safe one-time (repeatable) Excel -> PostgreSQL employee seed.

Usage:
  python import_employees.py /imports/employees.xlsx --dry-run
  python import_employees.py /imports/employees.xlsx --apply
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import select

from main import Employee, SessionLocal


def clean_name(value):
    return ' '.join(str(value or '').strip().split())


def code_text(value):
    if value is None or isinstance(value, bool):
        return ''
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else ''
    return str(value).strip()


def read_employees(path: Path):
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if len(workbook.sheetnames) != 1:
            raise ValueError('Expected exactly one worksheet')
        sheet = workbook.active
        iterator = sheet.iter_rows(values_only=True)
        headers = next(iterator, ())
        names = [str(x or '').strip() for x in headers[:4]]
        expected = ['ردیف', 'نام', 'نام خانوادگی', 'کد ملی']
        if names != expected:
            raise ValueError(f'Unexpected header layout: {names}. Expected {expected}')

        rows = []
        invalid = []
        for line, values in enumerate(iterator, 2):
            cells = list(values[:5])
            cells.extend([None] * (5 - len(cells)))
            if all(x is None or not str(x).strip() for x in cells):
                continue
            first, last, code = clean_name(cells[1]), clean_name(cells[2]), code_text(cells[3])
            if not first or not last or not code.isascii() or not code.isdigit() or len(code) > 80:
                invalid.append(line)
                continue
            # Never normalize or pad the device ID: it must match the device DAT exactly.
            rows.append((code, f'{first} {last}'))
        if invalid:
            raise ValueError(f'Invalid employee data on worksheet rows {invalid[:20]} (count={len(invalid)})')
        if not rows:
            raise ValueError('No employees found')
        repeated = [code for code, count in Counter(code for code, _ in rows).items() if count > 1]
        if repeated:
            raise ValueError(f'Duplicate device IDs in source file (count={len(repeated)})')
        return rows
    finally:
        workbook.close()


def run(path: Path, apply: bool):
    rows = read_employees(path)
    with SessionLocal() as db:
        ids = [code for code, _ in rows]
        existing = {employee.device_id: employee for employee in db.scalars(select(Employee).where(Employee.device_id.in_(ids)))}
        additions = [(code, name) for code, name in rows if code not in existing]
        unchanged = [(code, name) for code, name in rows if code in existing and existing[code].name == name]
        conflicts = [(code, name) for code, name in rows if code in existing and existing[code].name != name]
        print(f'Source employees: {len(rows)}')
        print(f'New employees: {len(additions)}')
        print(f'Already present, unchanged: {len(unchanged)}')
        print(f'ID/name conflicts requiring manual review: {len(conflicts)}')
        if conflicts:
            # Avoid leaking employee names / national codes into application logs.
            print('ABORT: conflicting employee records exist; no changes made', file=sys.stderr)
            return 2
        if not apply:
            print('DRY RUN: no changes were made. Use --apply after reviewing the counts.')
            return 0
        for code, name in additions:
            db.add(Employee(device_id=code, name=name, status='active'))
        db.commit()
        print(f'COMMITTED: {len(additions)} employee(s) added. Existing employees preserved.')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Import initial employee roster into PostgreSQL')
    parser.add_argument('xlsx', type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        sys.exit(run(args.xlsx, args.apply))
    except (ValueError, OSError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
