# نسخهٔ آزمایشی v1.8.9-r3 — ثبت گروهی تردد مدیران با تأیید مستقل

**وضعیت: DEV Candidate / NOT PRODUCTION-READY**. این بسته سورس کامل است و هنوز روی Production اجرا نشده است. مبنا نسخهٔ v1.8.9-r2 با ثبت دومرحله‌ای بوده و چیزی از کد قبلی حذف نشده است.

## نیازمندی پیاده‌شده

- منشی همچنان صفحهٔ ثبت تکی `/managers/attendance` را دارد.
- صفحهٔ جدید `/managers/attendance/bulk` در منوی منشی ظاهر شده و فقط مدیران فعالِ گروه `management` را نشان می‌دهد.
- جستجو با نام یا شناسهٔ دستگاه؛ تا ۱۰۰ مدیر در صفحه و حداکثر ۲۵ مدیر انتخاب‌شده برای هر ارسال.
- هر مدیر انتخاب‌شده می‌تواند تاریخ شمسی، ساعت ورود، ساعت خروج و توضیح **مستقل** داشته باشد؛ حتی اگر همه برای یک تاریخ باشند.
- هر مدیر یک `ManagerAttendanceRequest` مجزا با وضعیت `pending` می‌گیرد. تصمیم ادمین دربارهٔ هر مدیر مستقل است؛ تأیید یک مدیر سایر درخواست‌های گروه را خودکار تأیید نمی‌کند.
- ارسال گروهی **اتمیک** است: اگر یک سطر تاریخ، ساعت، نقش مدیر، تداخل تردد خام یا درخواست Pending نامعتبر داشته باشد، هیچ‌یک از سطرهای آن نوبت نوشته نمی‌شود. در تعارض همزمان، ایندکس یکتای Pending و تراکنش DB جلوی ثبت دوگانه را می‌گیرند.
- ثبت گروهی برای تاریخ‌های **فاقد هر اصلاح قبلی** طراحی شده است. اصلاح روزی که قبلاً تأیید شده از مسیر ثبت تکی و بازبینی نسخه انجام می‌شود.
- نه تردد خام دستگاه و نه اصلاح نهایی تا مرحلهٔ تأیید ادمین تغییر می‌کند. درخواست ردشده هیچ تغییری در گزارش نمی‌دهد. تایید توسط حساب ادمین متفاوت الزامی است.
- Audit Event جمعی `manager_attendance_bulk_request` با شناسهٔ همبستگی تصادفی و Audit Event هر رکورد ثبت می‌شود. هیچ کاربر واقعی یا دادهٔ سازمانی داخل Source ZIP نیست.

## تغییرات کد

- `app/main.py`: منو، استایل جدول واکنش‌گرا، صفحهٔ جدید و Endpoint ثبت اتمیک گروهی.
- `tests/test_managers_manual_attendance.py`: سناریوهای چندنفرهٔ Pending، تأیید مستقل، لغو کامل تراکنش برای سطر نامعتبر، RBAC، CSRF، تکرار و تداخل.
- این README.
- **Migration جدید نیاز نیست**: جدول `manager_attendance_requests` موجود در Migration افزودنی `0005` به‌ازای هر مدیر یک ردیف جداگانه دارد.

## شواهد تست

- `python3 -m py_compile app/main.py tests/test_managers_manual_attendance.py`: PASS
- `python3 scripts/check_project.py .`: PASS
- `node tests/test_report_sort.cjs`: PASS
- `node tests/test_presence_duration_sort.cjs`: PASS
- تست عملی FastAPI + SQLAlchemy، اجرای Migration روی PostgreSQL 18، تست HTTP و مرورگر برای **این نسخهٔ r3** هنوز اجرا نشده‌اند؛ از روی کد نمی‌توان ادعای PASS کرد.

## حفاظت Production و مراحل باقیمانده

- هیچ دستور `docker compose` یا Migration روی محیط واقعی اجرا نشده و هیچ داده‌ای از Production در این بسته موجود نیست.
- نسخهٔ موجود Preview-r1 برای **r2** به SHA256 سورس قبلی قفل شده و این Source جدید r3 را عمداً رد می‌کند. قبل از تست r3 باید ابزار Preview جداگانهٔ SHA256-pinned مختص r3 آماده و با دادهٔ ساختگی اجرا شود.
- پیش از هر استقرار: تست کامل Python/PG/HTTP و UI، Backup قابل Restore، Migration rehearsal، امنیت و Rollback مستقل و تأیید کاربر لازم است.
- Twelve-Factor: Codebase `UNKNOWN` (GitLab commit بررسی نشده)، Dependencies `PARTIAL` (فایل Requirements وجود دارد، Build یکسان آزمایش نشده)، Config `PARTIAL`، Backing Services `UNKNOWN` برای r3، Build/Release/Run `UNKNOWN`، Processes `UNKNOWN`، Port `UNKNOWN`، Concurrency `PARTIAL` (قفل و ایندکس بررسی کدی)، Disposability `UNKNOWN`، Dev/Prod parity `UNKNOWN`، Logs `PARTIAL` (Audit دستی)، Admin processes `PARTIAL` (Migrationهای نسخه‌دار، اجرای واقعی r3 باقی‌مانده). این مقادیر جایگزین Release Gate نیستند.
