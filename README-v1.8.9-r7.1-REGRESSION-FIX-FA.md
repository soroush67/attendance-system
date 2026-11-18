# نسخهٔ v1.8.9-r7.1 — اصلاح دو Assertion تست Excel

این بسته، نسخهٔ کامل منبع r7 به‌همراه اصلاح **صرفاً فایل آزمون** `tests/test_admin_direct_attendance.py` است. هیچ تغییری در `app/`، مسیرهای HTTP، پایگاه داده، Compose یا فایل‌های Production انجام نشده است.

## علت دو FAIL در خروجی r7

1. آزمون، رشتهٔ `name="selected_rows"` را در کل HTML می‌شمرد. این عبارت ۱۵۰ بار در ۱۵۰ checkbox واقعی و یک بار نیز در کد JavaScript داخل همان صفحه وجود دارد. در r7.1 آزمون فقط markup واقعی `<input type="checkbox" ...>` را می‌شمارد.
2. HTML شمارندهٔ تداخل را به‌صورت `3` و `150` می‌سازد؛ آزمون قبلی به‌اشتباه رقم‌های فارسی `۳` و `۱۵۰` را انتظار داشت. اکنون Assertion با متن واقعی سازگار است. این تغییر فقط مربوط به تست است و به معنای تأیید تصویری UI نیست.

## اجرای آزمون بدون اتصال به دیتابیس

از داخل پوشهٔ این بسته، با `ATTENDANCE_QA_IMAGE` مربوط به محیط آزمایش:

```bash
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user 10001:10001 \
  --memory 512m --pids-limit 64 \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --workdir /qa/app \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/qa/app \
  -e DATABASE_URL='sqlite+pysqlite:///:memory:' \
  -e SESSION_SECRET='synthetic-r7-tests-only' \
  -v "$PWD/app:/qa/app:ro" -v "$PWD/tests:/qa/tests:ro" \
  --entrypoint sh "$ATTENDANCE_QA_IMAGE" \
  -ec 'set -e; for suite in test_admin_direct_attendance.py test_managers_manual_attendance.py test_manager_excel_parser.py; do python -m unittest discover -s /qa/tests -p "$suite" -v; done'
```

پیش از PASS کامل این آزمون‌ها و بررسی واقعی مرورگر، نسخه **Production-ready محسوب نمی‌شود**. در QA فعلی ۱۵۰ رکورد Excel قبلاً ثبت شده‌اند؛ همان فایل را مجدداً نمی‌توان ثبت کرد. برای تست انتخاب همهٔ ۱۵۰ ردیف مجاز، fixture مصنوعی با تاریخ‌های جدید یا دیتابیس آزمایشی تازه لازم است. دیتابیس موجود و تولیدی نباید پاک یا بازنویسی شوند.
