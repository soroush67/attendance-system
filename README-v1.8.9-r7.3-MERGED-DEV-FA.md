# کاندیدای Development v1.8.9-r7.3 — ادغام ایمن UI و محافظت Login

## زنجیرهٔ سورس و محدودهٔ تغییر

**مبنای واقعی:** سورس Development موجود در `attendance-r71-current-for-r72-merge.tar.gz` با SHA256 آرشیو:
`6f42022dd1518ad52ab9a870f58683d7bbb9889be960ace5247e32df58c4ee25`.

SHA256 فایل `app/main.py` قبل از Merge:
`921f94b734752841c412c278eaded6b7f7fea3da467aad16510f129f2ec0b97f`.

این پروژه یک Export بدون `.git` از سرور Development است؛ تا زمانی که commit دقیق GitLab تأیید نشده، ردیابی Git/ساخت Image تغییرناپذیر **UNKNOWN** است. هیچ دستوری روی سرور، PostgreSQL، Volumeها یا Production اجرا نشده است.

## تغییرات UI اضافه‌شده

1. منوی افقی بریده‌شدهٔ موبایل به `details/summary` با فهرست عمودی واکنش‌گرا تبدیل شد. کلید Escape منوی باز را می‌بندد و Focus را به عنوان منو برمی‌گرداند.
2. در عرض‌های حداکثر ۸۰۰ پیکسل، گزارش‌ها به کارت‌های برچسب‌دار تبدیل می‌شوند و اعمال تغییرات/اصلاح تردد در هر کارت قابل دسترسی است. انتخاب ستون و جهت مرتب‌سازی موبایل از همان داده‌ها و JS مشترک استفاده می‌کند.
3. دسکتاپ پیمایش افقی را داخل بخش گزارش محدود می‌کند، ستون عملیات در لبهٔ چپ Sticky است و متن راهنمای اسکرول/صفحه‌کلید دارد.
4. پس‌زمینهٔ Sidebar دسکتاپ تا انتهای محتوا ادامه دارد.
5. توضیحات کامل گزارش، بدون حذف اطلاعات، داخل راهنمای بازشونده قرار دارد؛ هشدار کارکرد تأییدنشدهٔ حقوق‌ودستمزد همواره دیده می‌شود.
6. عنوان نسخهٔ رابط کاربری به `1.8.9-r7.3-dev` تغییر کرد.

## مواردی که دقیقاً حفظ شدند

- AST ده تعریف Login/Throttling در `app/main.py` با سورس ارسال‌شده یکسان است (شامل کلاس `LoginRateLimit` و تابع `login`).
- SHA256 پنج فایل امنیتی و Release با مبنای ارسالی یکسان ماند: `0006_login_throttling.py`، `prune_login_throttle.py`، `test_login_throttling.py`، `Jenkinsfile` و `ci/remote_release.py`.
- تنظیمات Compose، مسیرهای استقرار و Volumeهای خارجی دست‌نخورده‌اند.
- `.env.example` ایمن بازسازی شده است؛ متغیرهای پیش‌فرض Login هم به آن اضافه شده‌اند. **فایل `.env` واقعی، دیتای کارکنان و گواهی داخلی در آرشیو نیستند.** Jenkins طبق روال موجود فایل CA را هنگام Build از Credentials تزریق می‌کند؛ برای Build دستی باید همان فرآیند امن رعایت شود.

## تست‌های اجراشده

| تست | نتیجه | محدودیت |
| --- | --- | --- |
| `python3 -m py_compile app/main.py tests/test_report_actions_visual.py tests/test_admin_direct_attendance.py` | PASS | Syntax فقط |
| `python3 -m unittest discover -s tests -p '*stdlib.py' -v` | ۱۰ PASS | بدون نیاز به DB |
| `python3 -m unittest discover -s tests -p 'test_ci_release.py' -v` | ۱۵ PASS | Mock، بدون Push/Deploy |
| `python3 -m unittest discover -s tests -p 'test_backup*.py' -v` | ۱۴ PASS | Reader/Publisher بدون اتصال واقعی |
| `python3 -m unittest discover -s tests -p 'test_manager_excel_parser.py' -v` | ۶ PASS | Excel مصنوعی، بدون DB |
| چهار تست JavaScript (`report_sort`, `presence_duration_sort`, `manager_bulk_select_all`, `manager_excel_preview`) | ۴ PASS | بدون مرورگر واقعی |
| `python3 scripts/check_project.py .` | PASS | Source-only safety guard |
| مقایسهٔ AST و Hash پنج فایل امنیتی | PASS | حفظ دقیق منطق و فایل‌های Login/CI |
| `tests/test_report_actions_visual.py` روی Chromium | NOT RUN | محیط فعلی اجرای مرورگر را محدود می‌کند |
| تست کامل FastAPI/SQLite/PostgreSQL و Migration واقعی | NOT RUN | Dependencyها و DB آزمایشی در این محیط موجود نیستند |
| Compose config و Build تصویر جدید | NOT RUN | Docker در این محیط در دسترس نیست |

## پیش‌شرط‌های Deploy صرفاً در Development

1. قبل از هر جایگزینی، SHA256 `app/main.py` فعلی سرور را با Hash مبنای این سند مقایسه کنید. اگر متفاوت است، فایل جدید را مستقیم Copy نکنید؛ تغییرات جدید را Review/Merge کنید.
2. سورس R7.3 را در مسیر و Git branch جداگانه بررسی کنید، `.env` و CA واقعی را با Workflow موجود سرور/CI تأمین کنید، سپس در یک PostgreSQL آزمایشی مستقل تست کنید.
3. اجرای `alembic upgrade head` با Revision `0006_login_throttling` فقط روی DB آزمایشی تازه و پس از بررسی بکاپ مجاز است. این نسخه هیچ Migration جدیدی نمی‌سازد. Production نیازمند مجوز و بکاپ آزمودهٔ جداگانه است.
4. تست کامل `python scripts/run_offline_tests.py tests` را در Container دارای Dependencyها اجرا کنید. همچنین مرورگر واقعی باید Login، Dashboard و Reports را در عرض‌های ۳۶۰، ۷۶۸ و ۱۴۴۰ و هر دو تم بررسی کند؛ Login throttling با 429/Retry-After و خروجی گزارش نیز تست شود.
5. Rollback باید به Digest دقیق نسخهٔ قبلی WEB برگردد. دیتابیس و Volumeها را حذف/جایگزین نکنید. از `docker compose down -v` استفاده نکنید.

## وضعیت Twelve-Factor و امنیت

| عامل | وضعیت | شواهد / کار باقی‌مانده |
| --- | --- | --- |
| ۱ Codebase | PARTIAL | فایل‌های پروژه و Pipeline موجود، ولی Export فاقد `.git`/commit قابل تأیید است |
| ۲ Dependencies | PARTIAL | نسخه‌های requirements پین شده، Build بازتولیدپذیر اجرا نشده |
| ۳ Config | PARTIAL | `.env.example` بدون Secret واقعی؛ Runtime secret store آزموده نشده |
| ۴ Backing services | PARTIAL | دو Compose مستقل و Volumeهای خارجی در سورس؛ Persistence عملی تست نشده |
| ۵ Build/release/run | PARTIAL | Guardهای CI تست Mock شدند؛ Image digest/rollback عملی نداریم |
| ۶ Processes | UNKNOWN | Stateless و استقرار چند Worker آزمایش نشده |
| ۷ Port binding | PARTIAL | Uvicorn و health endpoint در سورس؛ readiness واقعی تست نشده |
| ۸ Concurrency | UNKNOWN | بار و همزمانی واقعی Login/import/report تست نشده |
| ۹ Disposability | UNKNOWN | SIGTERM/restart و shutdown آزموده نشده |
| ۱۰ Dev/prod parity | UNKNOWN | محیط Stage و DB واقعی تطبیق داده نشده |
| ۱۱ Logs | UNKNOWN | Audit/Log redaction در Runtime تأیید نشده |
| ۱۲ Admin processes | PARTIAL | Migration `0006` و prune one-off محفوظ؛ اجرای آزمایشی و Restore Drill باقی است |

**Security/anti-crawler checklist:** نقش‌ها، Login limiter، CSP/noindex و گاردهای export باید در HTTP واقعی بررسی شوند. خروجی API/Excel ناشناس باید رد شود؛ `/robots.txt` صرفاً راهنمای ربات‌هاست و کنترل دسترسی نیست. HTTPS ingress، Rate-limit گزارش‌ها، ذخیرهٔ امن Secretها، Read-only HTTP و تست بازیابی بکاپ هنوز بررسی نشده‌اند.

**حکم انتشار: NOT PRODUCTION-READY.** نسخه R7.3 یک Candidate یکپارچهٔ Development است و قبل از Deploy نیاز به Browser QA، Integration Test و تأیید Commit/Build دارد.
