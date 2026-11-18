# راه‌اندازی مخزن کامل سامانه حضور و غیاب در GitLab

این آرشیو نسخه کامل **1.3** کد سامانه است؛ شامل FastAPI، قالب روز/شب، لوگوی بیمه سامان در صفحه ورود و منوی کناری، ابزار ورود اطلاعات پرسنل، Dockerfile، Compose و ابزارهای backup/restore.

## ۱. آماده‌سازی و Push اولیه

پس از استخراج ZIP، وارد پوشه `attendance-system` شوید و آدرس مخزن خالی GitLab را جایگزین کنید:

```bash
cd attendance-system
git init
git branch -M main
git add .
git status --short
git commit -m "Initial attendance system v1.3"
git remote add origin https://gitlab.example.com/your-group/attendance-system.git
git push -u origin main
```

اگر شاخه اصلی مخزن GitLab از قبل commit دارد (مثلاً README پیش‌فرض)، ابتدا تاریخچه remote را بررسی و ادغام کنید؛ از `git push --force` استفاده نکنید.

## ۲. فایل‌هایی که عمداً داخل Git نیستند

این موارد خصوصی یا وابسته به سرور هستند و نباید به GitLab Push شوند:

- `.env` — گذرواژه PostgreSQL، کلید Session و حساب مدیر.
- `app/certs/company-root-ca.crt` — گواهی داخلی سازمان. **این فایل برای Build لازم است** و باید از نسخه معتبر سازمانی روی سرور تأمین شود.
- `data/initial-import/personal_information.xlsx` — اطلاعات واقعی پرسنل.
- داده‌های Docker Volume و بکاپ‌های دیتابیس.

فایل `.env.example` فقط قالب نام متغیرهاست؛ مقادیر واقعی `.env` باید طبق تنظیمات سرور شما تکمیل شوند. هیچ نام متغیر محیطی یا تنظیم موجود در `docker-compose.yml` تغییر نکرده است.

## ۳. اجرای پروژه روی سرور

اگر پروژه را از GitLab روی سرور clone می‌کنید:

```bash
git clone https://gitlab.example.com/your-group/attendance-system.git
cd attendance-system
cp .env.example .env
```

سپس `.env` را با **مقادیر واقعی خودتان** کامل کنید و فایل Root CA معتبر سازمان را در `app/certs/company-root-ca.crt` قرار دهید. فایل پرسنل را فقط در صورت نیاز به Import اولیه در `data/initial-import/` قرار دهید.

```bash
docker-compose config --quiet
docker-compose build web
docker-compose up -d
docker-compose ps
curl -i http://127.0.0.1:8080/health
```

آدرس نمونه بالا برای مقدار پیش‌فرض `BIND_IP=127.0.0.1` و `WEB_PORT=8080` است؛ در سرور شما آدرس مطابق `.env` فعلی خواهد بود.

## ۴. داده‌های قبلی PostgreSQL

**ZIP یا Git شامل داده‌های قبلی حضور و غیاب نیست.** اگر فقط پوشه پروژه حذف شده و Docker Volumeهای `attendance_postgres_data`، `attendance_uploads` و `attendance_backups` هنوز روی همان Docker Host موجود باشند، می‌توان داده‌ها را با همین نام پروژه `attendance` دوباره متصل کرد. پیش از هر تغییر:

```bash
docker volume ls --filter name=attendance
```

اگر Volume حذف شده، برای بازیابی اطلاعات کارکنان و گزارش‌ها به بکاپ PostgreSQL نیاز دارید. ساخت دوباره سرویس به‌تنهایی اطلاعات حذف‌شده دیتابیس را بازنمی‌گرداند.

## ۵. کنترل قبل از Push

```bash
git status --short
git ls-files
```

مطمئن شوید `.env`، `.crt`، `.xlsx`، فایل‌های `.dump` و هرگونه کلید خصوصی در خروجی `git ls-files` حضور ندارند.
