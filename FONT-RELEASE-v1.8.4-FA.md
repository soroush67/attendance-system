# Attendance v1.8.4.2 — فونت فارسی آفلاین

این نسخه از v1.8.3 مشتق شده و فونت سراسری وب و اصلاح سازگاری Starlette را اضافه می‌کند. تغییرات:

1. `app/static/fonts/Vazirmatn-Variable-4e3fa217.woff2` و `OFL.txt` از پروژهٔ رسمی Vazirmatn.
2. Mount محدود `StaticFiles` فقط برای `/assets/fonts`؛ هیچ مسیر Upload، Database یا Secret به StaticFiles متصل نیست.
3. `@font-face` و `font-family: "Vazirmatn", Tahoma, "Segoe UI", sans-serif` برای تمام صفحات Login، Dashboard، فرم و گزارش؛ وزن متغیر 100 تا 900 و `font-display: swap`.
4. Preload فونت برای تمام صفحات HTML و Cache عمومی فقط برای Font URL با Hash ثابت؛ سایر پاسخ‌های شخصی بدون Cache باقی می‌مانند.
5. افزودن فایل‌های فونت به Dockerfile اپلیکیشن و گسترش تست‌های امنیتی.
6. Badge نسخهٔ داشبورد از `1.8.3` به `1.8.4.2` تغییر کرد.
7. برطرف‌شدن خطای نسخهٔ v1.8.4: `MutableHeaders` متد `pop` ندارد؛ حذف هدر `Pragma` از پاسخ موفق فونت با `del` انجام می‌شود.
8. رفع خطای واقعی v1.8.4.1 در کانتینر: فونت با `Content-Type: text/plain` تحویل داده می‌شد؛ نسخهٔ جدید فقط برای URL فونتِ Hashدار و موفق، `Content-Type: font/woff2` تنظیم می‌کند. یک Probe ایزولهٔ HTTP واقعی نیز قبل از Deploy باید پاس شود.

Font SHA256: `4e3fa217d38fdafc1fea4414ceb58ca5e662cf0ab5fa735a8c8c20e8b42cad92`، اندازه 111,152 بایت.

منبع: https://github.com/rastikerdar/vazirmatn (فایل `fonts/webfonts/Vazirmatn[wght].woff2`، GitHub blob SHA `a501289a85595158570b0c2badcb4608b042e748`). مجوز `OFL-1.1` در کنار فایل حفظ شده است.

## نکات استقرار

- در اجرای مستقیم از ZIP کامل، هرگز `compose.app.yml` موجود روی سرور را با نسخهٔ داخل آرشیو جایگزین نکنید؛ Production Config باید مطابق محیط واقعی باقی بماند.
- برای Hotfix بدون Jenkins فقط از بستهٔ اصلاحی جداگانهٔ `attendance-v1842-guarded-web-only-font.zip` استفاده شود؛ بسته‌های v1.8.4 و v1.8.4.1 را دوباره اجرا نکنید.
- در CI، Dockerfile جدید فایل‌های `static` را در Image کپی می‌کند. Jenkins باید Digest ثابت Image را منتشر کند و فقط Web را Recreate کند.
- این تغییر نیازی به Migration و هیچ عمل نوشتن روی PostgreSQL/RustFS ندارد.
- پس از استقرار، Smoke Test سرور + بررسی مرورگر موبایل و Light/Dark لازم است.

## معیارهای پذیرش

| معیار | انتظار |
|---|---|
| GET `/assets/fonts/Vazirmatn-Variable-4e3fa217.woff2` | 200 / font/woff2 / SHA256 صحیح |
| GET `/` بدون ورود | 303 -> `/login` |
| GET `/login` | 200 + Preload و CSS وزیرمتن |
| GET `/backups` بدون ورود | 401 |
| GET `/health` | 200 |
| Docker Web و DB | Healthy و بدون تغییر Mount/Network/Env |
| UI | مشاهدهٔ واقعی Login، Dashboard، گزارش‌ها در Light/Dark، عرض‌های 360/768/1440 |

وضعیت تست فعلی: بررسی Source و Compiler، تست‌های خالص Installer و CI و تست JavaScript باید پیش از تحویل پاس شوند. تست‌های نیازمند Dependencies کامل FastAPI و آزمون HTTP واقعی داخل Image روی سرور پیش از فعال‌سازی اجرا خواهند شد. هنوز ادعای تست UI مرورگر یا انتشار Production v1.8.4.2 نداریم.
