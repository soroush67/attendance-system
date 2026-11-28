#!/usr/bin/env python3
"""
seed_employees.py — بارگذاری یا به‌روزرسانی لیست کارکنان

فرمت‌های پشتیبانی‌شده:
  • Excel (.xlsx)  — ستون‌ها: ردیف | نام | نام خانوادگی | کد ملی  (هدر اجباری)
  • CSV  (.csv)    — device_id,name  (هدر اختیاری)

قوانین idempotency:
  • کارمند جدید (device_id ناشناخته)       → insert با status=active
  • کارمند موجود با status=active           → فقط name را به‌روز کن (در صورت تغییر)
  • کارمند موجود با status != active        → نادیده بگیر (حذف از طریق سامانه انجام شده)
  • status هیچوقت توسط این اسکریپت تغییر نمی‌کند

مثال اجرا:
  DATABASE_URL=postgresql+psycopg://att:pass@192.168.11.10:5432/attendance_wh \\
      python scripts/seed_employees.py personal_information.xlsx
"""

import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise SystemExit(
            "خطا: متغیر محیطی DATABASE_URL تنظیم نشده است.\n"
            "مثال:\n"
            "  DATABASE_URL=postgresql+psycopg://att:pass@192.168.11.10:5432/attendance_wh \\\n"
            "      python scripts/seed_employees.py personal_information.xlsx"
        )
    return url


# ── بارخوانی فایل ─────────────────────────────────────────────────────────

def load_xlsx(path: str) -> list[tuple[str, str]]:
    try:
        import openpyxl
    except ImportError:
        raise SystemExit("برای خواندن xlsx نیاز به openpyxl دارید:\n  pip install openpyxl")

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active

    # پیدا کردن ایندکس ستون‌ها از روی هدر
    headers = [str(c).strip() if c else "" for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]

    def col(candidates: list[str]) -> int:
        # اول مطابقت دقیق، سپس شامل بودن
        for name in candidates:
            for i, h in enumerate(headers):
                if h == name:
                    return i
        for name in candidates:
            for i, h in enumerate(headers):
                if name in h:
                    return i
        raise SystemExit(f"ستون مورد نظر ({candidates}) در فایل پیدا نشد.\nهدرهای موجود: {headers}")

    idx_first  = col(["نام"])             # نام کوچک
    idx_last   = col(["نام خانوادگی"])
    idx_device = col(["کد ملی", "کد دستگاه", "device_id"])

    rows: list[tuple[str, str]] = []
    for lineno, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        # توقف در اولین ردیف کاملاً خالی
        if not row or all(v is None for v in row):
            break
        device_id = str(row[idx_device]).strip() if row[idx_device] is not None else ""
        first     = str(row[idx_first]).strip()  if row[idx_first]  is not None else ""
        last      = str(row[idx_last]).strip()   if row[idx_last]   is not None else ""
        name      = f"{first} {last}".strip()
        if device_id and name:
            rows.append((device_id, name))
        elif any(v is not None for v in row):
            print(f"  ⚠  سطر {lineno} ناقص است، نادیده گرفته شد")

    wb.close()
    return rows


def load_csv(path: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(1024)
        f.seek(0)
        try:
            has_header = csv.Sniffer().has_header(sample)
        except csv.Error:
            has_header = False
        reader = csv.reader(f)
        if has_header:
            next(reader)
        for lineno, row in enumerate(reader, start=2 if has_header else 1):
            if not row or row[0].strip().startswith("#"):
                continue
            if len(row) < 2:
                print(f"  ⚠  سطر {lineno} ناقص است، نادیده گرفته شد: {row}")
                continue
            device_id, name = row[0].strip(), row[1].strip()
            if device_id and name:
                rows.append((device_id, name))
    return rows


def load_file(path: str) -> list[tuple[str, str]]:
    if path.lower().endswith(".xlsx"):
        return load_xlsx(path)
    return load_csv(path)


# ── بارگذاری در پایگاه داده ───────────────────────────────────────────────

def seed(file_path: str) -> None:
    rows = load_file(file_path)
    if not rows:
        print("فایل خالی است یا داده معتبری ندارد.")
        return

    print(f"تعداد رکورد در فایل: {len(rows)}\n")

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from attendance.models import Employee

    engine = create_engine(get_database_url())
    inserted = updated = skipped_inactive = skipped_unchanged = 0

    with Session(engine) as db:
        for device_id, name in rows:
            emp: Employee | None = db.scalar(
                select(Employee).where(Employee.device_id == device_id)
            )

            if emp is None:
                db.add(Employee(device_id=device_id, name=name, status="active"))
                print(f"  ✚  {device_id:>15}  {name}  [جدید]")
                inserted += 1

            elif emp.status != "active":
                print(f"  –  {device_id:>15}  {emp.name}  [غیرفعال، نادیده گرفته شد]")
                skipped_inactive += 1

            elif emp.name != name:
                print(f"  ✎  {device_id:>15}  {emp.name!r} → {name!r}  [به‌روزرسانی نام]")
                emp.name = name
                updated += 1

            else:
                skipped_unchanged += 1

        db.commit()

    print(
        f"\n{'─'*45}"
        f"\n  جدید اضافه‌شده    : {inserted}"
        f"\n  نام به‌روزشده     : {updated}"
        f"\n  غیرفعال (نادیده)  : {skipped_inactive}"
        f"\n  بدون تغییر        : {skipped_unchanged}"
        f"\n{'─'*45}"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(
            f"نحوه استفاده:\n"
            f"  DATABASE_URL=... python {sys.argv[0]} <employees.xlsx|employees.csv>"
        )
    seed(sys.argv[1])
