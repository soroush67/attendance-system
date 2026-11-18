# Attendance — معماری استقرار جداشده (مرحله ۱)

> **به‌روزرسانی برای PostgreSQL مستقل 18.4:** در استقرار جدید پایگاه داده روی
> `utl-18-atend` داخل پروژه مستقل `pg_stack` اجرا می‌شود و دیتای آن Bind Mount است.
> این سند درباره Volumeهای قدیمی و `db:5432` توضیح تاریخچه نسخه قبل است و
> دستورالعمل اجرایی استقرار جدید نیست. برای کار فعلی، راهنمای
> [DATABASE-INITIALIZATION.md](DATABASE-INITIALIZATION.md) مرجع اجرایی است.

## هدف

- ساخت Image فقط در Jenkins و نگهداری آن در Nexus.
- اجرای مستقل `web` از PostgreSQL و backup.
- حفظ فایل `.env` و تمام نام متغیرهای فعلی، گواهی CA، داده‌ها و Volumeهای Docker.
- عدم اجرای خودکار `down`، `down -v`، `prune`، Restore یا Migration در Pipeline.
- Image Tag تغییرناپذیر و امکان برگشت به Overlay نسخه قبلی در صورت خطای Healthcheck.

## فایل‌های جدید

| فایل | کارکرد |
| --- | --- |
| `compose.database.yml` | فقط `db` و `backup`؛ داده‌های موجود به‌صورت external volume |
| `compose.app.yml` | فقط `web`؛ بدون Build و بدون `depends_on` به DB |
| `compose.release.yml` | توسط Jenkins روی سرور ایجاد می‌شود و Tag دقیق Image را pin می‌کند |
| `Jenkinsfile` | Checkout، Validation، Build، Push و Deploy صرفاً Web |
| `ops/preflight.sh` | کنترل read-only شبکه، Volumeها، فایل‌های تنظیمات |
| `ops/deploy-web.sh` | Deploy Web همراه Healthcheck و تلاش برای Rollback |

## سازگاری با سرور فعلی

پروژه Compose همچنان `attendance` است؛ سرویس دیتابیس همچنان `db` است و کد FastAPI به `db:5432` متصل می‌شود. هیچ متغیر فعلی تغییر نکرده است.

شبکه و Volumeهای قبلی به‌صورت **external** استفاده می‌شوند:

- `attendance_internal`
- `attendance_postgres_data`
- `attendance_backups`
- `attendance_uploads`

Docker Compose به‌صورت مستقل این Volumeها را نمی‌سازد. برای محیط فعلی از Volumeهای موجود استفاده می‌شود؛ اگر وجود نداشته باشند، Preflight متوقف می‌شود تا از ساخت دیتابیس خالی و تصور اشتباهِ بازیابی داده جلوگیری شود.

## مرحله مهاجرت روی سرور موجود (بعد از بکاپ و بازبینی)

1. مشخص کنید که `attendance_postgres_data` و `attendance_backups` و `attendance_uploads` هنوز موجود هستند.
2. از PostgreSQL بکاپ جدید بگیرید و قابل بازیابی بودن آن را جداگانه بررسی کنید.
3. فایل‌های جدید را در کنار `docker-compose.yml` فعلی قرار دهید؛ فایل قبلی را حذف یا بازنویسی نکنید.
4. روی سرور دستورات read-only را اجرا کنید:

   ```bash
   cd /opt/delshad/attendance-system
   bash ops/preflight.sh
   docker-compose -f compose.database.yml ps
   docker-compose -f compose.app.yml ps
   ```

5. سرویس‌های `db` و `backup` در Deployment عادی **دست نمی‌خورند**. بعد از بازبینی، فقط Jenkins سرویس `web` را با Compose جدید اجرا می‌کند.
6. برای عملیات نگهداری دیتابیس (نه Deployment روزمره) از این Compose استفاده می‌شود:

   ```bash
   docker-compose -f compose.database.yml ps
   docker-compose -f compose.database.yml logs --tail=100 db
   ```

7. **هرگز** `docker-compose down -v` یا حذف Volumeها را اجرا نکنید.

## محدودیت‌ها و مراحل بعدی

این مرحله معماری Compose و Pipeline را جدا می‌کند، ولی وضعیت دیتابیس را **مهاجرت نمی‌دهد**. اصلاح بعدی باید شامل:

1. Alembic baseline، با تشخیص دیتابیس قدیمی و نسخه Migration؛ تا زمانی که Migration تأیید نشده، `create_all` از Startup حذف نمی‌شود.
2. جداسازی تست‌های واحد Parser و Report از کد HTTP و Session.
3. تست خودکار Build و اجرای Compose در محیط Stage و تست بازگرداندن داده از بکاپ.
4. انتقال فایل‌های Compose به Repository مستقل `devops/infra` پس از تأیید مسیر و دسترسی؛ فعلاً برای جلوگیری از وابستگی مجهول، همه فایل‌های لازم در همین Git repository نگهداری می‌شوند.
5. آزمون عملی Rollback با نسخه معتبر قبلی، قبل از فعال‌سازی Jenkins در محیط واقعی.

**وضعیت:** مرحله ۱ طراحی و آماده‌سازی کد است. تا وقتی آزمون‌های Stage، Backup/Restore و Migration کامل نشوند، نباید این معماری را Production-validated تلقی کرد.
