# بستهٔ کاندیدای Attendance 1.8.3 — GitLab / Jenkins / Nexus

## وضعیت و محدوده

این یک **بستهٔ سورس و Pipeline برای بازبینی و استقرار کنترل‌شده** است؛
خودِ بسته هیچ کاری روی Production انجام نمی‌دهد. آخرین سرویس مشاهده‌شده
در سرور `192.168.11.10:8080`، ایمیج `attendance-web:ui-v1.8.2` بود.
دیتابیس PostgreSQL 18.4، `pg-exporter`، RustFS و Timerهای بکاپ باید
مستقل باقی بمانند. داده‌های واقعی موجودند.

تغییرات این کاندیدا:

1. کاربر بدون نشست معتبر که آدرس اصلی `/` را باز می‌کند، به `/login`
   هدایت می‌شود (`303`). باز شدن JSON با پیام «ابتدا وارد سامانه شوید»
   در صفحهٔ اصلی برطرف می‌شود.
2. `GET /backups` در سمت سرور **فقط برای `admin` دارای نشست معتبر** است.
   مهمان `401`، نقش غیرادمین `403` دریافت می‌کند؛ تست‌های Regression برای
   هر دو، دسترسی ادمین و Logout افزوده شده‌اند.
3. برچسب نسخه در Sidebar از مقدار تاریخی `1.4` به `1.8.3` تغییر کرد.
4. Jenkinsfile کاندیدا Build/Test/Push تصویر را به‌شکل جدا از استقرار اجرا
   می‌کند؛ انتشار Production پیش‌فرض خاموش است و یک تأیید دستی جداگانه دارد.
5. انتشار فقط با **Digest ثابت** رجیستری Nexus و Compose Overlay مستقل
   `compose.ci-release.yml` انجام می‌شود؛ DB، صادرکنندهٔ متریک، RustFS،
   Volumes و فایل `.env` نباید جایگزین یا Restart شوند.

## دلیل نمایش صفحهٔ بکاپ «بدون ورود» در عکس

در اسکرین‌شات، Badge کاربر `admin` و دکمهٔ «خروج» دیده می‌شود؛ این شاهد
وجود Session قبلی در مرورگر است، نه شاهد باز بودن عمومی مسیر. طبق کد،
Cookie امضاشده حداکثر هشت ساعت عمر دارد (`max_age=28800`).
ورود از IP و از `attendance.hamainsurance.net` دو Origin/Host متفاوت است؛
Cookie ورود یکی لزوماً در دیگری وجود ندارد. عکس دامنه که JSON `401`
نشان می‌دهد، برعکس ثابت می‌کند درخواست آن مرورگر احراز هویت نشده است.

برای تست مستقل در مرورگر **پنجرهٔ ناشناس** باز کنید. قبل از ورود:

- `https://attendance.hamainsurance.net/` باید بعد از انتشار 1.8.3 به
  `https://attendance.hamainsurance.net/login` هدایت شود.
- `https://attendance.hamainsurance.net/backups` نباید محتوای وضعیت بکاپ
  را برگرداند؛ در این انتشار همچنان `401` دریافت می‌شود.
- پس از ورود با `admin`، `/backups` باید باز شود؛ با نقش `viewer` باید
  `403` باشد.
- پس از فشردن «خروج» و مراجعهٔ مجدد به `/backups` باید `401` باشد.

برای تست HTTP بدون Cookie از خود میزبان، از `GET` استفاده کنید (نه `HEAD`):

```bash
curl -sS -D - -o /dev/null http://192.168.11.10:8080/
curl -sS -D - -o /dev/null http://192.168.11.10:8080/login
curl -sS -D - -o /dev/null http://192.168.11.10:8080/backups
```

انتظار **بعد از استقرار کاندیدا**: به‌ترتیب `303` با `Location: /login`،
`200` و `401`؛ و هدر `Cache-Control: private, no-store`.

## پیش‌نیازها — قبل از اجرای Jenkins

- URL/Branch واقعی GitLab را تعیین کنید. مقدار تاریخیِ قرارگرفته در
  Jenkinsfile، `https://gitlab.hamainsurance.net/opration/attendance-system.git`
  و `develop` است و باید در سامانهٔ واقعی بررسی شود؛ **تأیید نشده است**.
- پوشهٔ Production فعلی اصلاً مخزن Git نیست. در آن `git init`،
  `git push -f`، `git pull` یا `git clone` روی فایل‌های موجود نزنید.
  ابتدا یک Working Tree تازه از مخزن تأییدشده بگیرید، فایل‌های همین ZIP
  را با سورس مخزن Compare کنید و تغییرات پذیرفته‌شده را Commit کنید.
- Credentials مورد نیاز Jenkins: `gitlab-http-clone`،
  `internal-ca-cert-file`، `nexus-docker-credential` و
  `stage-deploy-ssh`؛ Agent دارای Label `inf-18-jenk-3`، Docker CLI،
  Python 3، CA داخلی، DNS/Trust صحیح GitLab/Nexus/سرور استقرار.
- دسترسی SSH به `192.168.11.10` باید با User مشخص و کنترل‌شده برقرار
  باشد. سرویس‌های Docker و مسیر Production نیازمند مجوزهای مناسب‌اند.
  Jenkins از `StrictHostKeyChecking=yes` استفاده می‌کند، پس `known_hosts`
  باید **از قبل و به‌صورت معتبر** تنظیم شده باشد.
- چک‌های Preflight بر پایهٔ وضعیت مشاهده‌شده ساخته شده‌اند و با کوچک‌ترین
  اختلاف مالکیت Container، Mount، Port، شبکه یا هش Compose **متوقف می‌شوند**.
  این رفتار عمدی است. هش `compose.app.yml` Production ثبت‌شده:
  `1c6b1f091ed73e15d7e54275c7a1e409edb6c2446bf82a4ea46550ba08d2310e`
  و هش `compose.backup-status.yml`:
  `422144d7a261ddbc3314300c6afd7a6178d964f8db005d8734c8b640f3745eb3`.

## جریان انتشار پیشنهادی

1. ZIP را در محیط کار بازبینی توسعه باز کنید. فایل‌های `.env`، Root CA
   خصوصی میزبان، کلیدها، فایل حضور و غیاب و بکاپ هرگز نباید وارد Git شوند.
   `.gitignore` و `scripts/check_project.py` کنترل کمکی‌اند؛ بررسی
   دستی `git diff --cached` و `git status` نیز ضروری است.
2. پس از Commit در Repo صحیح، Pipeline Jenkins را ابتدا با
   `DEPLOY_PRODUCTION=false` اجرا کنید: Checkout → Code Check → Build →
   Offline Tests → Push to Nexus → Archive immutable `release-ref.txt`.
3. پس از بررسی تست‌ها و Digest و دسترسی‌ها، در یک نوبت تغییر مجاز،
   `DEPLOY_PRODUCTION=true` را با تأیید دستی اجرا کنید. تنها سرویس `web`
   با `up -d --no-deps --no-build --force-recreate web` بازساخته می‌شود.
   قطعی کوتاه UI ممکن است؛ برای آن زمان مناسب تعیین کنید.
4. اسکریپت میزبان `ci/remote_release.py` ابتدا وضعیت واقعی Production را
   به‌شکل Read-only بررسی می‌کند، سپس ایمیج دقیق Digest را دریافت و Overlay
   کوچک `compose.ci-release.yml` را ایجاد می‌کند. اگر Smoke Test شکست
   بخورد، Pointer قبلی را برمی‌گرداند و فقط Web را Rollback می‌کند.
5. از آن پس **هر دستور Compose مربوط به وب** باید سه فایل را به همین ترتیب
   اعمال کند: `compose.app.yml`، `compose.backup-status.yml`،
   `compose.ci-release.yml`. راه‌اندازی وب با Compose پایهٔ تنها ممنوع
   است چون می‌تواند نسخهٔ تصویر یا Mount وضعیت بکاپ را از بین ببرد.

**توجه:** اجرای Pipeline برای بازسازی/Push و به‌خصوص Deploy هنوز روی محیط
واقعی آزمایش نشده است؛ این بسته معادل تأیید نهایی Production نیست.

## نکات امنیتی و کارهای موکول‌شده به پایان

- تصویر اولیهٔ IP با عبارت `Not secure` نشان می‌دهد HTTP بدون TLS هم در
  دسترس بوده است. گذرواژه و Session روی HTTP امنیت انتقال ندارند.
  برای FQDN با Certificate معتبر، TLS Reverse Proxy و Redirect از HTTP
  به HTTPS را فعال کنید و پس از تأیید HTTPS، `SESSION_HTTPS_ONLY=true`
  را در تنظیمات واقعی App اعمال کنید. مقدار `.env` را این بسته تغییر نمی‌دهد.
- حفظ Cookie برای هشت ساعت بخشی از رفتار فعلی Session است؛ بسته‌شدن
  تب به معنای Logout نیست. در صورت نیاز سازمان به احراز هویت مجدد برای
  صفحهٔ بکاپ، این باید به‌صورت سیاست جداگانه (Step-up Auth) طراحی شود.
- اولین اجرای خودکار Nightly Backup در لاگ‌های ارائه‌شده برای
  ۱۶ نوامبر ۲۰۲۶ تنظیم شده بود؛ موفقیت Timer واقعی، دومین Restore Drill،
  نگهداری خارج از میزبان، تعریف RPO/RTO و PITR/WAL هنوز در فهرست بررسی نهایی‌اند.
- این تغییر **هیچ Migration دیتابیس** ندارد. PostgreSQL و RustFS باید
  در جریان CI و استقرار وب دست‌نخورده بمانند.

## آزمون‌هایی که این بسته باید عبور کند

```bash
python3 scripts/check_project.py .
python3 -m unittest discover -s tests -p 'test_backup*.py' -v
python3 -m unittest discover -s tests -p 'test_ci_release.py' -v
python3 -m unittest discover -s tests -p 'test_security.py' -v
```

آزمون امنیتی آخری به Dependencies اصلی برنامه (از جمله SQLAlchemy و
Psycopg) نیاز دارد و در مرحلهٔ Offline Tests روی Image ساخته‌شده توسط
Jenkins اجرا خواهد شد. آزمون حقیقی HTTP در FQDN باید بعد از Deploy تکرار شود.

## ارزیابی Twelve-Factor تا قبل از انتشار کاندیدا

این جدول ارزیابی **کاندیدای 1.8.3** است؛ وضعیت موفق 1.8.2 روی سرور
به‌تنهایی اثبات‌کنندهٔ تست‌های نسخهٔ جدید نیست.

| اصل | وضعیت | شواهد / کار باقی‌مانده |
|---|---|---|
| Codebase | PARTIAL | فایل‌های کامل در ZIP؛ GitLab URL/Branch و Commit نهایی هنوز تأیید نشده‌اند. |
| Dependencies | PARTIAL | Dockerfile و Requirements موجود؛ Build از Nexus روی Jenkins هنوز اجرا نشده است. |
| Config | PARTIAL | `.env` از ZIP حذف و Credentials برای Jenkins تعریف شده‌اند؛ تنظیمات واقعی Secret/TLS نیازمند بازبینی است. |
| Backing Services | PARTIAL | DB/RustFS مستقل در گزارش عملیاتی فعلی؛ پیش‌بررسی جدید CI روی سرور اجرا نشده است. |
| Build/Release/Run | PARTIAL | Jenkinsfile و Digest Guard آماده؛ Pipeline و Release واقعی هنوز تست نشده است. |
| Processes | PARTIAL | وب با Volume آپلود مستقل طراحی شده؛ Stateless/افزایش تعداد Replica هنوز آزمایش نشده است. |
| Port Binding | PARTIAL | سرویس فعلی روی 8080 پاسخ Health=200 داده؛ نسخهٔ جدید روی همین ورودی آزمایش نشده است. |
| Concurrency | UNKNOWN | تست هم‌زمانی آپلود، گزارش و Session برای کاندیدا اجرا نشده است. |
| Disposability | PARTIAL | Healthcheck و Rollback در کد هستند؛ Drill انتشار/بازگشت جدید هنوز اجرا نشده است. |
| Dev/Prod Parity | UNKNOWN | Stage با PostgreSQL 18.4 و داده‌های مشابه تأیید نشده است. |
| Logs | PARTIAL | Timerهای فعلی ثبت‌شده‌اند؛ ساختار، حریم خصوصی و Audit همهٔ لاگ‌های جدید آزموده نشده‌اند. |
| Admin Processes | PARTIAL | Migrationهای Alembic در سورس هستند؛ CI جدید هیچ Migration اجرا نمی‌کند. |

**نتیجه:** این ZIP یک Release Candidate است و تا عبور از تست‌های Jenkins،
تست پذیرش امنیتی/HTTP، Preflight واقعی و تمرین Rollback، برچسب
`PRODUCTION-READY` دریافت نمی‌کند.
