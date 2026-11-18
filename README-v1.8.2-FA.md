# بسته ارتقای امن Attendance v1.8.2 — داشبورد بکاپ RustFS

این ZIP برای همان میزبان `utl-18-atend` ساخته شده است. بر اساس گزارش‌هایی که از
Production دریافت کرده‌ایم، برنامهٔ فعال `attendance-web:ui-v1.8.1` است و
بازیابی نسخهٔ واقعی PostgreSQL از RustFS در یک دیتابیس موقت با موفقیت انجام
شده است (۱۰ جدول، ۱۵۲ ردیف در زمان آزمون). **نسخهٔ وب هنوز با این ZIP روی
سرور ارتقا داده نشده است.**

## تغییر جدید

در منوی مدیر، صفحهٔ **وضعیت بکاپ** در مسیر `/backups` اضافه می‌شود. مدیر
می‌تواند آخرین زمان بکاپ تأییدشده، حجم، تعداد Manifestهای تأییدشده، فعال بودن
Timer و زمان آخرین بررسی را ببیند. کاربر بدون Login و همه نقش‌های غیرمدیر
اجازه دسترسی ندارند. این صفحه فقط یک فایل JSON بدون هیچ رمز یا اطلاعات
کارکنان را می‌خواند.

یک سرویس مستقل روی میزبان هر ۱۵ دقیقه Manifestهای بکاپ را از RustFS/S3
می‌خواند، آخرین Object را با `head-object` بررسی می‌کند و تنها **خلاصهٔ
بدون رمز** را به `public-status/status.json` منتشر می‌کند. اطلاعات اتصال
RustFS صرفاً در همان فایل root-only موجود `/opt/attendance-rustfs-backup/config.env`
می‌ماند. در صورت جابه‌جایی RustFS به میزبان دیگر، تنظیمات قبلی
`RUSTFS_ENDPOINT` و گواهی/اطلاعات دسترسی را در همان فایل تغییر دهید؛ نیازی
به تنظیم AWS در برنامه وب نیست.

## نکات حیاتی درباره داده واقعی

- این ارتقا **هیچ Migration، هیچ تغییر SQL، هیچ Restore و هیچ تغییر
  PGDATA** ندارد؛ PostgreSQL و RustFS نباید Restart شوند.
- `compose.app.yml` واقعی سرور با نسخهٔ درون Source متفاوت است. برنامه
  نصب **هرگز فایل اصلی سرور، `.env` یا `app/main.py` را بازنویسی نمی‌کند**.
- فقط Docker image جدید از روی **ایمیج محلی v1.8.1** ساخته می‌شود، بدون
  نصب پکیج و بدون Pull. فقط سرویس وب با Compose overlay بازسازی می‌شود.
- ارتقا ممکن است چند ثانیه وب را قطع کند؛ اجرا بعد از تایپ `DEPLOY` است.
- پایگاه داده فعلی واقعی است. نگهداری بکاپ روی همان دیسک، Disaster Recovery
  محسوب نمی‌شود. این بکاپ Logical است و **PITR نیست**. انتقال RustFS یا
  نسخهٔ ثانویه به میزبان دیگری باید بعداً عملیاتی شود.
- تعداد Manifest و حجم، «بررسی سریع metadata» هستند؛ اثبات بازیابی، همان
  Restore Drill مستقل قبلی است. مانیتور دوره‌ای جایگزین اجرای مجدد Drill نیست.

## اجرای مرحله‌ای روی سرور

ZIP را در **پوشهٔ staging جداگانه** استخراج کنید، نه داخل پروژه اصلی:

```bash
mkdir -p /tmp/attendance-v182-stage
unzip -o /tmp/attendance-v182-backup-status.zip -d /tmp/attendance-v182-stage
python3 /tmp/attendance-v182-stage/attendance-v182-backup-status/ops/deploy.py preflight
```

پیش‌نیازها با مقدارهای دقیق قبلی چک می‌شوند:

| فایل Production | SHA256 مورد انتظار |
|---|---|
| `app/main.py` | `b2ec187d90243a59523e5a836fdfc3d66f5659babb6ea1fd1a744bd4a301b58d` |
| `compose.app.yml` | `1c6b1f091ed73e15d7e54275c7a1e409edb6c2446bf82a4ea46550ba08d2310e` |

اگر حتی یکی تغییر کرده باشد، برنامه متوقف می‌شود؛ **هش را به‌صورت دستی دور
نزنید**. ابتدا نسخهٔ جدید را بررسی کنید.

بعد از PASS در preflight، برای اجرای تأییدشده:

```bash
python3 /tmp/attendance-v182-stage/attendance-v182-backup-status/ops/deploy.py deploy
```

درخواست `DEPLOY` را فقط زمانی وارد کنید که آمادهٔ قطع کوتاه سرویس وب هستید.
اسکریپت پیش از بازسازی وب باید وضعیت بکاپ را سالم منتشر کرده باشد.

## بررسی پس از نصب

```bash
docker ps --filter name=attendance-web-1 --format '{{.Names}} | {{.Image}} | {{.Status}}'
systemctl list-timers attendance-pg-backup.timer attendance-backup-status.timer
systemctl status attendance-backup-status.service --no-pager
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.11.10:8080/health
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.11.10:8080/backups
```

برای درخواست بدون نشست (Guest)، انتظار `401` در `/backups` و `200` در
`/health` داریم. پس از Login با حساب مدیر، مسیر `/backups` را در مرورگر
باز کنید. نقش‌های Viewer/Exporter/Operator نباید به صفحه دسترسی داشته باشند.

## برگشت در صورت مشکل

اگر بخش فعال‌سازی شکست بخورد، اسکریپت تلاش می‌کند سرویس وب را با همان
`compose.app.yml` اصلی به ایمیج قبل بازگرداند. اگر پس از موفقیت نیاز به
برگشت دستی بود (از پوشه اصلی Production):

```bash
cd /home/devops/containerd/devops/attendance-backend/attendance-system
docker-compose -f compose.app.yml up -d --no-deps --no-build --force-recreate web
```

این عملیات فقط وب را بازسازی می‌کند. برای نگه داشتن قابلیت v1.8.2 در
استقرارهای بعدی، حتماً Compose را با **هر دو فایل** صدا بزنید:

```bash
docker-compose -f compose.app.yml -f compose.backup-status.yml up -d --no-deps web
```

**هشدار:** مسیر `compose.backup-status.yml` باید در Pipeline انتشار بعدی هم
حفظ شود، وگرنه انتشار بعدی می‌تواند Mount وضعیت را حذف کند.

## تست بسته قبل از نصب

تست‌های مستقل خواندن JSON و پردازش Metadata بدون اتصال به PostgreSQL و S3:

```bash
python3 -m unittest discover -s /tmp/attendance-v182-stage/attendance-v182-backup-status/tests -p 'test_backup*.py' -v
```

تست‌های جامع قبلی v1.8.1 و Restore Drill با موفقیت انجام شده‌اند؛ اما تست
Production v1.8.2 **فقط پس از اجرای واقعی روی سرور** قابل تأیید است.

## نام فایل‌های مهم

- `ops/deploy.py`: نصب کنترل‌شدهٔ یک‌مرحله‌ای (دو حالت `preflight` و `deploy`).
- `Dockerfile.upgrade`: ایمیج Overlay آفلاین از پایهٔ دقیق v1.8.1.
- `app/main.py` و `app/backup_status.py`: منو/صفحه و خوانندهٔ امن وضعیت.
- `ops/backup_status_publisher.py`: سرویس میزبان برای انتشار JSON بدون رمز.
- `ops/attendance-backup-status.{service,timer}`: انتشار دوره‌ای مستقل.
- `compose.backup-status.yml`: فقط Image و Mount جدید وب، بدون تعریف دیتابیس.

فایل‌های قدیمی Source برای مرجع نگه داشته شده‌اند؛ **برای این ارتقا فقط
`ops/deploy.py` را اجرا کنید و از `install-safe.sh` قدیمی استفاده نکنید.**
