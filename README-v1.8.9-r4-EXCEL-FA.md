# نسخهٔ v1.8.9-r4 — بارگذاری Excel تردد مدیران

**وضعیت: DEV Candidate / NOT PRODUCTION-READY.** این بسته، سورس کامل توسعه‌ای روی مبنای v1.8.9-r3 است. هیچ سرویس Production یا دیتابیس واقعی تغییر نکرده است.

## نیازمندی

به‌جای ۱۵۰ بار ورود اطلاعات برای ۵ مدیر و ۳۰ روز، منشی قالب Excel را دریافت می‌کند، هر سطر را پر می‌کند و فایل را یک‌بار بارگذاری می‌کند. تمام سطرهای معتبر به درخواست‌های مستقل `pending` تبدیل می‌شوند. فقط ادمین دیگری می‌تواند هر درخواست را جداگانه تأیید کند.

## شیوه استفاده

۱. با حساب `secretary` به «ثبت گروهی مدیران» بروید و روی «دریافت قالب Excel» بزنید.

۲. در برگهٔ «تردد مدیران»، از ردیف دوم به بعد اطلاعات را وارد کنید. هر ردیف مربوط به یک مدیر در یک روز است؛ برای ۵ مدیر و ۳۰ روز، ۱۵۰ ردیف بسازید.

| ستون | راهنما |
|---|---|
| کد مدیر | کد اختصاصی نمایش‌داده‌شده کنار نام در صفحهٔ مدیران؛ شناسهٔ اصلی تطبیق |
| نام مدیر | اختیاری، ولی اگر پر شود باید دقیقاً با نام تعریف‌شده یکسان باشد |
| تاریخ شمسی | متن مانند `۱۴۰۵/۰۸/۲۵` |
| ساعت ورود | متن `08:00` یا `08:00:00` |
| ساعت خروج | متن `17:00` یا `17:00:00`؛ بعد از ساعت ورود و در همان روز |
| توضیح | دلیل ثبت، حداقل ۵ و حداکثر ۵۰۰ نویسه |

۳. همان فایل را از بخش «ورود گروهی با فایل Excel» بارگذاری کنید. سقف ۱۰۰۰ ردیف داده و ۲ مگابایت است؛ برگه‌ها و عنوان ستون‌ها نباید تغییر کنند. می‌توانید بیش از ۳۵۰ سطر آمادهٔ فرمت‌شدهٔ قالب، ردیف جدید اضافه کنید تا سقف ۱۰۰۰ سطر.

۴. اگر حتی یک سطر اشتباه، تکراری، آینده، متعلق به کارمند عادی، دارای تردد دستگاهی یا دارای درخواست Pending/اصلاح قبلی باشد، کل تراکنش رد می‌شود و هیچ ردیفی ثبت نخواهد شد. پیام خطا شمارهٔ سطر را اعلام می‌کند.

۵. سطرهای معتبر هم به گزارش نهایی نمی‌روند. ادمین در صفحهٔ «تأیید تردد مدیران» برای هر سطر مستقل تأیید یا رد می‌زند. تردد دستگاهی، بکاپ و درخواست‌های قبلی تغییر نمی‌کنند.

## پیاده‌سازی

- `app/main.py`: Endpoint دانلود محرمانهٔ قالب (`GET /managers/attendance/excel-template`) و بارگذاری تحت نقش منشی با بررسی CSRF (`POST /managers/attendance/excel-upload`)، اعتبارسنجی دیتابیس و تراکنش اتمیک.
- `app/manager_excel.py`: خواندن امن XLSX با محدودیت حجم فشرده و بازشده، سقف تعداد سطر/ستون، رد فرمول/لینک خارجی/ماکرو، تطبیق دقیق سطرها و جلوگیری از قطع خاموش داده.
- `app/templates/manager-attendance-template.xlsx`: قالب آفلاین فارسیِ خالی و فاقد اطلاعات واقعی کارکنان. فایل به‌کمک `scripts/build_manager_excel_template.mjs` ساخته شده است.
- `app/Dockerfile` و `app/.dockerignore`: اضافه شدن Parser و قالب به image بدون تغییر سرویس دیتابیس.
- `tests/test_manager_excel_parser.py` و Fixtureهای ساختگی، به همراه سناریوهای جدید در `tests/test_managers_manual_attendance.py`.
- **بدون Migration جدید**: درخواست‌ها در جدول نسخهٔ `0005` قرار می‌گیرند؛ تا پیش از تأیید، جدول `attendance_corrections` دست‌نخورده است.

## شواهد اجرا در محیط ساخت

- خواندن فایل تست ۱۵۰ ردیفی با ۵ مدیر و ۳۰ روز: **PASS** (Parser).
- `python3 -m unittest discover -s tests -p 'test_manager_excel_parser.py' -v`: **۶ PASS**.
- `node tests/test_report_sort.cjs` و `node tests/test_presence_duration_sort.cjs`: **PASS**.
- `python3 -m compileall -q app tests`: **PASS**.
- `python3 scripts/check_project.py .`: **PASS**.
- ساخت و رندر قالب با Artifact Tool، خواندن مجدد با `openpyxl`: **PASS**.
- تست‌های یکپارچهٔ FastAPI + SQLAlchemy + PostgreSQL، احراز هویت HTTP، مرورگر و Migration rehearsal برای **r4 هنوز اجرا نشده‌اند**؛ وابستگی‌های SQLAlchemy و `jdatetime` در محیط ساخت موجود نبود. سناریوهای DB در Test Suite نوشته شده‌اند اما **PASS ادعا نمی‌شود**.

## امنیت و استقرار

- تنها نقش منشی می‌تواند قالب را دانلود یا Excel را بارگذاری کند. ادمین این مسیر را اجرا نمی‌کند و تأییدِ خودش را از ثبت منشی جدا نگه می‌دارد.
- کنترل سرور برای ۲ مگابایت فایل، ۱۶ مگابایت اندازهٔ بازشده، ۱۵۰ عضو ZIP، ۲۰۰۰ ردیف اسکن، فرمول/ماکرو و ۱۰۰۰ سطر داده. در Reverse Proxy نیز محدودیت درخواست مثلاً ۳ مگابایت اعمال شود؛ حد کد جایگزین محدودیت Ingress نیست.
- هیچ شناسهٔ واقعی، تاریخ حضور واقعی یا دادهٔ کارمند در Template و Fixtureها وجود ندارد؛ تنها دادهٔ ساختگی تستی همراه سورس است.
- شناسهٔ batch و خلاصهٔ SHA256 فایل در Audit Event ثبت می‌شود؛ هر سطر نیز Audit مجزا دارد. خود فایل بارگذاری‌شده در دیتابیس/دیسک نگهداری نمی‌شود.
- Composeهای App و Database مستقل باقی مانده‌اند؛ از اجرای `down -v`، جایگزینی volume، اصلاح مستقیم Production و اجرای Migration بدون تمرین/Backup خودداری شود.
- پیش از استقرار: تست PostgreSQL 18، HTTP multipart، Role/CSRF، بررسی بصری RTL روی موبایل/دسکتاپ، اسکن امنیت، بررسی محدودیت Ingress، تست Restore و Rollback و مجوز تغییر Production ضروری است.

## Twelve-Factor status

| Factor | وضعیت در r4 | دلیل |
|---|---|---|
| Codebase | UNKNOWN | وضعیت commit در GitLab بررسی نشده |
| Dependencies | PARTIAL | Requirements پین شده؛ Container Build آزمایش نشده |
| Config | PARTIAL | تنظیمات env موجود؛ secrets/Ingress محیط هدف بررسی نشده |
| Backing services | UNKNOWN | اتصال واقعی DB و دوام Volume این نسخه تست نشده |
| Build/release/run | UNKNOWN | CI، Digest Release و Rollback r4 اجرا نشده |
| Processes | UNKNOWN | چند Worker و restart تست نشده |
| Port binding | UNKNOWN | HTTP Smoke Test اجرا نشده |
| Concurrency | PARTIAL | Transaction/Lock/Unique Index در کد وجود دارد؛ رقابت واقعی آزموده نشده |
| Disposability | UNKNOWN | Graceful Shutdown روی Container تست نشده |
| Dev/prod parity | UNKNOWN | Postgres staging مخصوص r4 اجرا نشده |
| Logs | PARTIAL | Audit جمعی/سطرها در کد؛ Redaction و Forwarding آزمایش نشده |
| Admin processes | PARTIAL | Migration موجود؛ Rehearsal اجرا نشده |

این جدول نشان‌دهندهٔ **Production-Readiness نیست** و تا اجرای Gateهای باقیمانده نباید به Production انتشار یابد.
