<div dir="rtl">

# یادداشت تحویل — سامانه حضور و غیاب (MVP در حال ساخت)

آخرین به‌روزرسانی: ۲۰۲۶-۱۱-۱۸ · شاخه: `feature/mvp-hardening` (هنوز commit نشده)

## مبنا
- کد موجود v1.3 از `https://gitlab.hamainsurance.net/opration/attendance-system.git` شاخه `develop` گرفته شد (تک‌فایل `app/main.py`).
- مشخصات فنی در اسکیل `.inputs/engineer-attendance-platform-skill.zip` (FastAPI/PostgreSQL، ۱۲ فاکتور، UI فارسی RTL، امنیت و ضدخزنده).
- قالب Jenkinsfile کاربر: `.inputs/Jenkinsfile.template` (Agent `inf-18-jenk-3`، GitLab/Nexus شرکت، infra repo، credentialها: `gitlab-http-clone`، `nexus-docker-credential`، `internal-ca-cert-file`، `stage-deploy-ssh`).
- ⚠️ در مخزن GitLab فعلی `.env` با رمزهای واقعی commit شده و `.env.example` هم همان رمزها را دارد؛ باید از git حذف و رمزهای Production عوض شوند.

## تصمیم‌های کاربر
- اول MVP ساده روی v1.3، بعد نسخه کامل.
- Jenkinsfile با الگوی قالب شرکت ساخته شود؛ اگر در محیط اصلی گیر کرد کاربر خبر می‌دهد.
- همه متغیرها در `.env` (کاربر نمی‌خواهد مدام سؤال شود).
- بدون اجازه به GitLab شرکت push نشود.

## سرور مقصد
- `192.168.11.10` (کاربر نوشته بود 19.168…؛ اشتباه تایپی)، hostname `utl-18-atend`، Ubuntu 24.04، کاربر `devops` رمز `123` (sudo با همان رمز).
- Docker نصب نیست؛ اینترنت دارد ولی به `nexus.hamainsurance.net` و `docker-hosted.hamainsurance.net` دسترسی ندارد. داده‌ای روی آن نیست.
- Production واقعی v1.3 جای دیگری است (`/opt/delshad/attendance-system`).

## انجام‌شده
- پکیج `attendance/`: `config.py` (همه تنظیم از env، بدون پیش‌فرض محرمانه)، `db.py` (`DATABASE_URL`، pool محدود)، `models.py` (جدول‌های v1.3 دقیقاً + ستون‌های امنیتی users + جدول `login_attempts`)، `security.py`، `cli.py` (bootstrap-admin، reset-password، import-employees)، `services/` (parser، jalali، reports، importer، roster)، `web/deps.py`، `web/pages.py`، `main.py`.
- امنیت لاگین: argon2id (هش scrypt قدیمی خودکار ارتقا)، قفل حساب بعد از ۵ خطا، محدودیت هر IP، نشست با عمر مطلق و idle و `session_version` برای ابطال، CSRF روی لاگین/خروج/همه فرم‌ها، logout با POST، تغییر اجباری گذرواژه در اولین ورود، هدرهای CSP/nosniff/DENY/no-store/X-Robots-Tag، `robots.txt`، `/docs` و OpenAPI خاموش.
- قالب‌های Jinja با autoescape در `attendance/templates/`، فایل‌های استاتیک (لوگو، CSS، theme) در `attendance/static/`.
- Alembic: `0001` = schema دقیق v1.3 (با pg_dump مقایسه و یکسان بود)، `0002` = سخت‌سازی لاگین. دیتابیس v1.3 موجود: `alembic stamp 0001` سپس `alembic upgrade head`.
- `requirements.txt`، `requirements-dev.txt`، `.venv` محلی.

## باقی‌مانده برای MVP
1. تست‌ها در `tests/` روی PostgreSQL واقعی (کانتینر موقت `att-test-pg` روی `127.0.0.1:55432`، رمز `test`، db `attendance_test`): parser، jalali (کبیسه)، دسترسی ناشناس، لاگین/قفل/IP، ابطال نشست، RBAC، آپلود و گزارش و Excel، حذف فایل.
2. `Dockerfile` در ریشه (ARG `CA_CERTS` و `BASE_IMAGE`، کاربر غیر root، HEALTHCHECK روی `/ready`)، `.dockerignore`.
3. `compose.database.yml` (db + backup) و `compose.app.yml` (migrate یک‌باره + app)، شبکه خارجی `attendance_internal`، volumeهای `attendance_postgres_data`/`attendance_backups`/`attendance_uploads`.
4. `.env` کامل با رمزهای تصادفی (gitignore) و `.env.example` بدون رمز.
5. `Jenkinsfile` بر اساس قالب: مرحله تست، build/push، به‌روزرسانی infra repo، deploy فقط app + migrate (هرگز DB)، health و smoke ناشناس.
6. README فارسی، اجرای محلی، بررسی UI در مرورگر.
7. deploy روی `192.168.11.10`: نصب Docker، انتقال image با `docker save | ssh docker load`، بالا آوردن DB و App، تست.

</div>
