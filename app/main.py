import csv
import hashlib
import hmac
import io
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import jdatetime
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, UniqueConstraint, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from starlette.middleware.sessions import SessionMiddleware


def env(name, default=None):
    return os.environ.get(name, default)


DATABASE_URL = "postgresql+psycopg://{user}:{password}@db:5432/{db}".format(
    user=__import__('urllib.parse', fromlist=['quote']).quote(env('POSTGRES_USER', 'attendance'), safe=''),
    password=__import__('urllib.parse', fromlist=['quote']).quote(env('POSTGRES_PASSWORD', ''), safe=''),
    db=env('POSTGRES_DB', 'attendance'),
)
TZ = ZoneInfo(env('DEVICE_TIMEZONE', 'Asia/Tehran'))
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Employee(Base):
    __tablename__ = 'employees'
    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default='active')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(250))
    role: Mapped[str] = mapped_column(String(20), default='viewer')
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Import(Base):
    __tablename__ = 'imports'
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    uploader_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    accepted: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(String(500), default='')


class ReportSet(Base):
    __tablename__ = 'report_sets'
    id: Mapped[int] = mapped_column(primary_key=True)
    import_id: Mapped[int] = mapped_column(ForeignKey('imports.id'), unique=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)


class ReportDay(Base):
    __tablename__ = 'report_days'
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey('report_sets.id'), index=True)
    device_id: Mapped[str] = mapped_column(String(80), index=True)
    local_day: Mapped[str] = mapped_column(String(10), index=True)
    __table_args__ = (UniqueConstraint('report_id', 'device_id', 'local_day', name='unique_report_employee_day'),)


class ReportPunch(Base):
    __tablename__ = 'report_punches'
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey('report_sets.id'), index=True)
    device_id: Mapped[str] = mapped_column(String(80), index=True)
    local_day: Mapped[str] = mapped_column(String(10), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('report_id', 'device_id', 'occurred_at', name='unique_report_punch'),)


class Log(Base):
    __tablename__ = 'attendance_logs'
    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(80), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source_import_id: Mapped[int] = mapped_column(ForeignKey('imports.id'))
    __table_args__ = (UniqueConstraint('device_id', 'occurred_at', name='unique_device_punch'),)


class AttendanceRecord(Base):
    __tablename__ = 'attendance_records'
    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(80), index=True)
    local_day: Mapped[str] = mapped_column(String(10), index=True)
    first_punch: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_punch: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    punch_count: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint('device_id', 'local_day', name='unique_attendance_day'),)


class Audit(Base):
    __tablename__ = 'audit_logs'
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(80))
    event: Mapped[str] = mapped_column(String(80))
    details: Mapped[str] = mapped_column(String(300), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


def password_hash(password: str):
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f'scrypt${salt.hex()}${derived.hex()}'


def password_ok(password: str, stored: str):
    try:
        _, salt, expected = stored.split('$')
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


app = FastAPI(title='سامانه حضور و غیاب', docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=env('SESSION_SECRET', ''), same_site='strict', https_only=env('SESSION_HTTPS_ONLY', 'false').lower() == 'true', max_age=28800)


@app.on_event('startup')
def setup():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if not db.scalar(select(User.id).limit(1)):
            admin_password = env('ADMIN_PASSWORD', '')
            if len(admin_password) < 12:
                raise RuntimeError('ADMIN_PASSWORD must be at least 12 characters')
            db.add(User(username=env('ADMIN_USERNAME', 'admin'), password_hash=password_hash(admin_password), role='admin'))
            db.commit()
        # Non-destructive compatibility with imports created before named reports.
        for previous in db.scalars(select(Import).outerjoin(ReportSet, ReportSet.import_id == Import.id).where(ReportSet.id.is_(None))).all():
            legacy_name = f'گزارش قدیمی {previous.id} - {previous.filename}'[:120]
            record = ReportSet(import_id=previous.id, name=legacy_name)
            db.add(record)
            db.flush()
            legacy_punches = list(db.scalars(select(Log).where(Log.source_import_id == previous.id)))
            days = {(item.device_id, item.occurred_at.astimezone(TZ).date().isoformat()) for item in legacy_punches}
            for code, day in days:
                db.add(ReportDay(report_id=record.id, device_id=code, local_day=day))
            for item in legacy_punches:
                db.add(ReportPunch(report_id=record.id, device_id=item.device_id, local_day=item.occurred_at.astimezone(TZ).date().isoformat(), occurred_at=item.occurred_at))
        # Upgrade reports created by v0.7, which already have ReportDay rows
        # but no ReportPunch rows. Historical punches are recoverable from Logs.
        existing_sets = db.scalars(select(ReportSet).outerjoin(ReportPunch, ReportPunch.report_id == ReportSet.id).where(ReportPunch.id.is_(None))).all()
        for existing_set in existing_sets:
            for item in db.scalars(select(Log).where(Log.source_import_id == existing_set.import_id)):
                db.add(ReportPunch(report_id=existing_set.id, device_id=item.device_id, local_day=item.occurred_at.astimezone(TZ).date().isoformat(), occurred_at=item.occurred_at))
        db.commit()


def get_db():
    with SessionLocal() as db:
        yield db


def actor(request: Request, db: Session):
    uid = request.session.get('uid')
    user = db.get(User, uid) if isinstance(uid, int) else None
    return user if user and user.active else None


def permit(request: Request, db: Session, *roles):
    user = actor(request, db)
    if not user:
        raise HTTPException(401, 'ابتدا وارد سامانه شوید')
    if roles and user.role not in roles:
        raise HTTPException(403, 'مجوز انجام این عملیات را ندارید')
    return user


def csrf(request: Request, token: str):
    if not hmac.compare_digest(str(request.session.get('csrf', '')), token) or not token:
        raise HTTPException(403, 'درخواست نامعتبر است؛ صفحه را تازه‌سازی کنید')


def csrf_token(request: Request):
    if 'csrf' not in request.session:
        request.session['csrf'] = secrets.token_urlsafe(24)
    return request.session['csrf']


def audit(db, user, event, details=''):
    db.add(Audit(actor=user.username, event=event, details=details[:300]))


STYLE = '''
:root{color-scheme:light;--bg:#f3f6fc;--bg-glow:#eaf0ff;--panel:#fff;--panel-2:#f8faff;--text:#17263b;--heading:#102039;--muted:#708098;--line:#e5eaf3;--line-strong:#d4ddea;--primary:#465de4;--primary-hover:#3449c6;--primary-soft:#eef0ff;--success:#15816b;--success-soft:#e7f8f1;--danger:#b44752;--shadow:0 14px 42px rgba(35,56,103,.065);--sidebar:#111c35;--sidebar-2:#1c2d51;--sidebar-text:#afbed9;--input:#fff;--focus:rgba(70,93,228,.17);font-family:Tahoma,"Segoe UI",sans-serif;background:var(--bg);color:var(--text)}
:root[data-theme="dark"]{color-scheme:dark;--bg:#0b1325;--bg-glow:#152341;--panel:#141f35;--panel-2:#18253e;--text:#dbe6fa;--heading:#f3f7ff;--muted:#95a8c4;--line:#283752;--line-strong:#354764;--primary:#8193ff;--primary-hover:#a3adff;--primary-soft:#243258;--success:#63d5b0;--success-soft:#193b37;--danger:#ff8894;--shadow:0 14px 42px rgba(0,0,0,.18);--sidebar:#0a1223;--sidebar-2:#1e2b48;--sidebar-text:#aabbd7;--input:#101b30;--focus:rgba(129,147,255,.22)}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;direction:rtl;font-size:14px;line-height:1.9;min-width:0;background:radial-gradient(ellipse 75% 40% at 75% -15%,var(--bg-glow),transparent 80%),var(--bg);color:var(--text);transition:background .24s,color .24s}
a{color:var(--primary)}button,input,select{font-family:inherit}button,a,input,select{transition:background-color .18s,border-color .18s,box-shadow .18s,transform .18s}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
.shell{display:grid;grid-template-columns:258px minmax(0,1fr);min-height:100vh}
aside{background:linear-gradient(165deg,var(--sidebar),#162847 70%,var(--sidebar));color:#fff;padding:27px 17px;position:sticky;top:0;align-self:start;height:100vh;display:flex;flex-direction:column;box-shadow:-4px 0 26px rgba(20,32,60,.08);z-index:2}
.brand{display:flex;align-items:center;gap:11px;margin:3px 9px 42px;font-size:17px;font-weight:800;line-height:1.6;color:#fff}
.brand-mark{width:42px;height:42px;border-radius:14px;display:grid;place-items:center;flex-shrink:0;background:linear-gradient(135deg,#8ea3ff,#5169f0);box-shadow:0 7px 20px #4255b944;font-size:22px;color:white}
.brand small{display:block;font-size:10px;letter-spacing:.05px;font-weight:400;color:var(--sidebar-text);margin-top:2px}
.company-logo-wrap{display:flex;align-items:center;justify-content:center;background:#fff;border-radius:16px;padding:8px 18px;margin:0 8px 18px;box-shadow:0 8px 28px #00000020;min-height:104px}
.company-logo{width:100%;max-width:152px;height:88px;object-fit:contain;display:block}
.nav-label{font-size:10px;color:#8497bb;padding:0 18px 12px;letter-spacing:.3px}
nav{display:flex;flex-direction:column;gap:5px}nav a{display:flex;align-items:center;gap:13px;text-decoration:none;color:var(--sidebar-text);padding:12px 15px;border-radius:12px;font-weight:500;position:relative}
nav a .nav-icon{font-size:18px;line-height:1;opacity:.88;width:22px;text-align:center}
nav a:hover{background:#ffffff0d;color:white}nav a.selected{background:linear-gradient(100deg,#5266cf55,#7c8dff22);color:white;box-shadow:inset 0 0 0 1px #ffffff18}
nav a.selected:before{content:"";position:absolute;right:0;top:23%;height:54%;width:3px;border-radius:6px;background:#a1b0ff}
.side-footer{margin-top:auto;padding:17px 13px 7px;border-top:1px solid #ffffff17;color:#8197b6;font-size:11px}
main{padding:38px clamp(20px,3.4vw,55px) 70px;width:100%;min-width:0;max-width:1560px;margin:auto}
.top{display:flex;justify-content:space-between;align-items:center;gap:24px;margin-bottom:32px}
.eyebrow{color:var(--primary);font-size:11px;font-weight:800;letter-spacing:.2px;margin-bottom:3px}
h1{font-size:clamp(22px,2.1vw,29px);font-weight:800;letter-spacing:-.5px;color:var(--heading);margin:0 0 4px}h2{font-size:17px;font-weight:800;letter-spacing:-.2px;color:var(--heading);margin:0 0 16px}
.muted{color:var(--muted)}.card{background:var(--panel);border:1px solid var(--line);border-radius:19px;padding:clamp(19px,2vw,28px);margin-bottom:21px;box-shadow:var(--shadow)}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px}.grid>.card{min-height:142px;position:relative;overflow:hidden}.grid>.card:before{content:"";position:absolute;top:0;right:0;width:4px;height:100%;background:linear-gradient(var(--primary),#77bcef)}
.stat{font-size:38px;line-height:1.5;font-weight:850;color:var(--heading);font-variant-numeric:tabular-nums;margin-top:8px}
.top-actions{display:flex;align-items:center;gap:9px;flex-wrap:wrap;justify-content:flex-end}.user-badge{display:inline-flex;align-items:center;gap:8px;padding:7px 11px;background:var(--panel);border:1px solid var(--line);border-radius:12px;color:var(--text);font-size:12px;font-weight:700}.avatar{width:27px;height:27px;border-radius:9px;display:grid;place-items:center;color:#fff;background:linear-gradient(140deg,#6b82ed,#4cb3c1);font-weight:800}
input,select{font-size:13px;padding:11px 13px;min-height:44px;border:1px solid var(--line-strong);border-radius:10px;min-width:0;width:100%;background:var(--input);color:var(--text)}input::placeholder{color:var(--muted);opacity:.85}
input:focus,select:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 4px var(--focus)}input[type=file]{padding:8px 10px}input[type=file]::file-selector-button{font-family:inherit;border:none;background:var(--primary-soft);color:var(--primary);border-radius:7px;padding:6px 10px;margin-left:12px;cursor:pointer}
label{display:block;font-size:12px;font-weight:700;color:var(--text);margin-bottom:7px}.fields{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;align-items:end}
button,.btn{font-size:12px;font-weight:700;background:var(--primary);color:white;padding:11px 18px;min-height:43px;border:1px solid transparent;border-radius:10px;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:7px;white-space:nowrap;text-align:center}
:root[data-theme="dark"] button:not(.outline):not(.theme-toggle),:root[data-theme="dark"] .btn:not(.outline){color:#0b1430}
button:hover,.btn:hover{background:var(--primary-hover);transform:translateY(-1px)}.outline{background:var(--panel);color:var(--primary);border:1px solid var(--line-strong)}.outline:hover{background:var(--primary-soft);color:var(--primary)}
.theme-toggle{display:inline-flex;align-items:center;gap:8px;background:var(--panel);color:var(--text);border:1px solid var(--line);min-height:43px;padding:9px 13px;border-radius:11px;box-shadow:0 2px 10px rgba(20,32,60,.035)}.theme-toggle:hover{background:var(--primary-soft);color:var(--primary)}.theme-toggle .theme-symbol{font-size:17px;line-height:1}.theme-toggle .theme-label{font-size:12px}
table{width:100%;border-collapse:separate;border-spacing:0;text-align:right}td,th{padding:14px 15px;border-bottom:1px solid var(--line);white-space:nowrap;font-size:12px}th{color:var(--muted);font-weight:800;background:var(--panel-2);font-size:11px}th:first-child{border-radius:0 10px 0 0}th:last-child{border-radius:10px 0 0 0}tbody tr:last-child td{border-bottom:0}tbody tr:hover td{background:var(--panel-2)}
.table-wrap{overflow-x:auto;scrollbar-color:var(--line-strong) transparent}.pill{display:inline-flex;align-items:center;border-radius:25px;background:var(--success-soft);color:var(--success);padding:3px 12px;font-size:11px;font-weight:800}.warn{background:#fff1dc;color:#9a6115}.error{background:#fbe9e8;color:#963e38}:root[data-theme="dark"] .warn{background:#4a381d;color:#ffd08e}:root[data-theme="dark"] .error{background:#44252c;color:#ff9ea5}
.msg{background:var(--success-soft);color:var(--success);border-right:4px solid var(--success);padding:13px 18px;border-radius:11px;margin-bottom:20px;font-weight:600}
.actions{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin:12px 0}.actions form{display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap}.actions select{width:auto}.actions input{width:auto;max-width:320px}details{border:1px solid var(--line);padding:5px 10px;border-radius:9px;margin-top:5px}summary{cursor:pointer;color:var(--primary);font-size:12px}
.login-screen{min-height:100vh;display:grid;place-items:center;padding:22px;position:relative}.login-screen:before{content:"";position:fixed;inset:0;z-index:-1;background:radial-gradient(circle at 20% 10%,var(--primary-soft),transparent 42%)}
.login{width:min(100%,440px);max-width:440px;padding:36px;margin:auto}.login h1{font-size:25px}.login input{margin-bottom:17px}.login-screen .theme-toggle{position:absolute;top:25px;left:28px}
.login-content{width:min(100%,480px);display:flex;flex-direction:column;align-items:center;gap:20px}.login-content .login{margin:0;width:100%}.login-logo{width:172px;min-height:143px;padding:12px 22px;border-radius:22px;background:#fff;box-shadow:var(--shadow);display:flex;justify-content:center;align-items:center}.login-logo img{display:block;width:100%;height:118px;object-fit:contain}
@media(max-width:1100px){.shell{grid-template-columns:220px minmax(0,1fr)}aside{padding-inline:11px}.fields{grid-template-columns:repeat(2,minmax(0,1fr))}.grid{gap:12px}}
@media(max-width:800px){.shell{display:block}aside{height:auto;position:relative;padding:12px 14px}.company-logo-wrap{width:118px;min-height:56px;margin:0 0 9px;padding:3px 8px;border-radius:10px}.company-logo{height:48px;max-width:92px}.brand{margin:0 0 8px;font-size:15px}.brand-mark{width:35px;height:35px}.brand small,.nav-label,.side-footer{display:none}nav{flex-direction:row;overflow-x:auto;gap:5px;padding-bottom:3px;scrollbar-width:thin}nav a{white-space:nowrap;padding:9px 12px;font-size:12px}nav a .nav-icon{font-size:15px;width:auto}nav a.selected:before{display:none}main{padding:23px 15px 45px}.top{align-items:flex-start;gap:14px;flex-wrap:wrap;margin-bottom:23px}.grid{grid-template-columns:1fr 1fr}.fields{grid-template-columns:1fr 1fr}.card{border-radius:15px;padding:18px}.top-actions{justify-content:flex-start}}
@media(max-width:520px){.grid,.fields{grid-template-columns:1fr}.top{display:block}.top-actions{justify-content:flex-start;margin-top:16px}.user-badge{display:none}.theme-toggle .theme-label{display:none}.stat{font-size:32px}td,th{padding:10px 12px}.login-screen .theme-toggle .theme-label{display:inline}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{transition:none!important;scroll-behavior:auto!important}}
'''

THEME_SCRIPT = '''
(function(){
  const root=document.documentElement;
  function getSaved(){try{return localStorage.getItem('attendance-theme')}catch(e){return null}}
  function save(theme){try{localStorage.setItem('attendance-theme',theme)}catch(e){}}
  function setTheme(theme){
    root.setAttribute('data-theme',theme);
    document.querySelectorAll('[data-theme-toggle]').forEach(function(btn){
      const dark=theme==='dark';
      btn.setAttribute('aria-label',dark?'فعال کردن حالت روز':'فعال کردن حالت شب');
      btn.setAttribute('aria-pressed',String(dark));
      btn.querySelector('.theme-symbol').textContent=dark?'☀':'☾';
      btn.querySelector('.theme-label').textContent=dark?'حالت روز':'حالت شب';
    });
  }
  document.addEventListener('DOMContentLoaded',function(){
    setTheme(root.getAttribute('data-theme')||'light');
    document.querySelectorAll('[data-theme-toggle]').forEach(function(btn){
      btn.addEventListener('click',function(){const next=root.getAttribute('data-theme')==='dark'?'light':'dark';save(next);setTheme(next)});
    });
  });
})();
'''

# The organization's logo is embedded in the HTML response. This keeps the
# existing Dockerfile and the safe installer's two-file update contract intact.
COMPANY_LOGO_BASE64 = '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'


def escape(value):
    import html
    return html.escape(str(value if value is not None else ''), quote=True)


def page(title, body, request, db, section=''):
    user = actor(request, db)
    token = csrf_token(request)
    nav = [('dashboard', '/', 'نمای کلی'), ('employees', '/employees', 'کارکنان'), ('imports', '/imports', 'آپلود فایل'), ('reports', '/reports', 'گزارش تردد')]
    if user and user.role == 'admin':
        nav += [('users', '/users', 'کاربران و دسترسی‌ها'), ('audit', '/audit', 'تاریخچه فعالیت')]
    icons = {'dashboard': '◈', 'employees': '♙', 'imports': '⇧', 'reports': '▤', 'users': '♧', 'audit': '◷'}
    links = ''.join(f'<a class="{"selected" if key == section else ""}" href="{url}" {"aria-current=page" if key == section else ""}><span class="nav-icon" aria-hidden="true">{icons[key]}</span><span>{label}</span></a>' for key, url, label in nav)
    toggle = '<button type="button" class="theme-toggle" data-theme-toggle aria-label="تغییر حالت روز و شب" aria-pressed="false"><span class="theme-symbol" aria-hidden="true">☾</span><span class="theme-label">حالت شب</span></button>'
    if user:
        layout = f'<div class="shell"><aside><div class="company-logo-wrap"><img class="company-logo" alt="بیمه سامان" src="data:image/png;base64,{COMPANY_LOGO_BASE64}"></div><div class="brand"><div class="brand-mark" aria-hidden="true">◈</div><div>سامانه حضور و غیاب<small>مدیریت هوشمند تردد · نسخه 1.3</small></div></div><div class="nav-label">منوی اصلی</div><nav aria-label="منوی اصلی">{links}</nav><div class="side-footer">سامانه مدیریت تردد کارکنان</div></aside><main><header class="top"><div><div class="eyebrow">پنل مدیریت / {escape(title)}</div><h1>{escape(title)}</h1><span class="muted">مدیریت یکپارچه حضور و غیاب کارکنان</span></div><div class="top-actions">{toggle}<span class="user-badge"><span class="avatar" aria-hidden="true">{escape(user.username[:1].upper())}</span>{escape(user.username)}</span><a class="btn outline" href="/logout">خروج</a></div></header>{body}</main></div>'
    else:
        layout = f'<div class="login-screen">{toggle}<div class="login-content"><div class="login-logo"><img alt="بیمه سامان" src="data:image/png;base64,{COMPANY_LOGO_BASE64}"></div>{body}</div></div>'
    return HTMLResponse(f'''<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#111c35"><title>{escape(title)} | سامانه حضور و غیاب</title><script>(function(){{try{{var t=localStorage.getItem('attendance-theme');document.documentElement.setAttribute('data-theme',t==='light'||t==='dark'?t:(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'))}}catch(e){{document.documentElement.setAttribute('data-theme','light')}}}})();</script><style>{STYLE}</style></head><body>{layout}<script>{THEME_SCRIPT}</script></body></html>''')


def msg(request):
    value = request.query_params.get('message', '')
    return f'<div class="msg">{escape(value)}</div>' if value else ''


def redirect(path, message=None):
    from urllib.parse import quote
    return RedirectResponse(path + ('?message=' + quote(message) if message else ''), status_code=303)


@app.get('/health')
def health():
    with SessionLocal() as db:
        db.execute(select(func.now()))
    return {'status': 'ok'}


@app.get('/login')
def login_page(request: Request, db: Session = Depends(get_db)):
    body = f'<div class="card login"><h1>ورود به سامانه</h1><p class="muted">لطفاً اطلاعات حساب کاربری خود را وارد کنید.</p>{msg(request)}<form method="post" action="/login"><label>نام کاربری</label><input name="username" autocomplete="username" required><label>گذرواژه</label><input type="password" name="password" autocomplete="current-password" required><button style="width:100%">ورود</button></form></div>'
    return page('ورود', body, request, db)


@app.post('/login')
def login(request: Request, username: str = Form(), password: str = Form(), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == username))
    if not user or not user.active or not password_ok(password, user.password_hash):
        return redirect('/login', 'نام کاربری یا گذرواژه اشتباه است')
    request.session.clear()
    request.session['uid'] = user.id
    request.session['csrf'] = secrets.token_urlsafe(24)
    audit(db, user, 'login')
    db.commit()
    return redirect('/')


@app.get('/logout')
def logout(request: Request):
    request.session.clear()
    return redirect('/login')


@app.get('/')
def dashboard(request: Request, db: Session = Depends(get_db)):
    permit(request, db, 'admin', 'operator', 'viewer', 'exporter')
    employees = db.scalar(select(func.count()).select_from(Employee).where(Employee.status == 'active'))
    logs = db.scalar(select(func.count()).select_from(Log))
    imports = db.scalar(select(func.count()).select_from(Import))
    body = msg(request) + f'<div class="grid"><div class="card"><span class="muted">کارکنان فعال</span><div class="stat">{employees}</div></div><div class="card"><span class="muted">ترددهای ثبت‌شده</span><div class="stat">{logs}</div></div><div class="card"><span class="muted">فایل‌های پردازش‌شده</span><div class="stat">{imports}</div></div></div><div class="card"><h2>دسترسی سریع</h2><p class="muted">برای ثبت ترددهای جدید، فایل خروجی دستگاه را آپلود کنید؛ گزارش‌ها از اطلاعات ذخیره‌شده ساخته می‌شوند.</p><div class="actions"><a class="btn" href="/imports">آپلود فایل دستگاه</a><a class="btn outline" href="/reports">مشاهده گزارش‌ها</a></div></div>'
    return page('نمای کلی', body, request, db, 'dashboard')


@app.get('/employees')
def employees(request: Request, search: str = '', show_deleted: int = 0, db: Session = Depends(get_db)):
    user = permit(request, db, 'admin', 'operator', 'viewer', 'exporter')
    stmt = select(Employee).order_by(Employee.id.desc()).limit(500)
    if show_deleted != 1:
        stmt = stmt.where(Employee.status != 'deleted')
    if search:
        stmt = stmt.where((Employee.name.ilike(f'%{search}%')) | (Employee.device_id.ilike(f'%{search}%')))
    records = db.scalars(stmt).all()
    token = csrf_token(request)
    form = ''
    if user.role == 'admin':
        form = f'<div class="card"><h2>تعریف کارمند جدید</h2><form class="fields" method="post" action="/employees"><div><label>نام و نام خانوادگی</label><input name="name" maxlength="200" required></div><div><label>شماره اختصاصی دستگاه</label><input name="device_id" maxlength="80" required></div><div><input type="hidden" name="csrf_token" value="{token}"><button>افزودن کارمند</button></div></form></div>'
    rows = ''
    status_map = {'active': 'فعال', 'suspended': 'معلق', 'deleted': 'حذف‌شده'}
    for employee in records:
        actions = ''
        if user.role == 'admin':
            actions = f'<form method="post" action="/employees/{employee.id}/status" class="actions"><input type="hidden" name="csrf_token" value="{token}"><select name="status"><option value="active">فعال</option><option value="suspended">تعلیق</option></select><button class="outline">ثبت وضعیت</button></form>'
            actions += f'<details><summary>ویرایش</summary><form class="actions" method="post" action="/employees/{employee.id}/edit"><input type="hidden" name="csrf_token" value="{token}"><input name="name" value="{escape(employee.name)}" required><input name="device_id" value="{escape(employee.device_id)}" required><button>ذخیره</button></form></details>'
            if employee.status != 'deleted':
                actions += f'<a href="/employees/{employee.id}/delete" class="btn outline" style="color:#a43d39;border-color:#e5b9b5">حذف کارمند</a>'
        rows += f'<tr><td>{escape(employee.name)}</td><td dir="ltr">{escape(employee.device_id)}</td><td><span class="pill {"warn" if employee.status == "suspended" else "error" if employee.status == "deleted" else ""}">{status_map[employee.status]}</span></td><td>{actions}</td></tr>'
    checkbox = 'checked' if show_deleted == 1 else ''
    body = msg(request) + form + f'<div class="card"><h2>فهرست کارکنان</h2><form method="get" class="actions"><input name="search" style="max-width:330px" placeholder="جست‌وجوی نام یا شماره دستگاه" value="{escape(search)}"><label style="display:flex;align-items:center;gap:6px"><input type="checkbox" name="show_deleted" value="1" {checkbox} style="width:auto">نمایش حذف‌شده‌ها</label><button>جست‌وجو</button></form><div class="table-wrap"><table><thead><tr><th>نام کارمند</th><th>شماره دستگاه</th><th>وضعیت</th><th>عملیات</th></tr></thead><tbody>{rows}</tbody></table></div></div>'
    return page('مدیریت کارکنان', body, request, db, 'employees')


@app.get('/employees/{employee_id}/delete')
def confirm_delete_employee(employee_id: int, request: Request, db: Session = Depends(get_db)):
    permit(request, db, 'admin')
    employee = db.get(Employee, employee_id)
    if not employee:
        raise HTTPException(404, 'کارمند یافت نشد')
    token = csrf_token(request)
    body = f'<div class="card"><h2>حذف کارمند</h2><p>آیا می‌خواهید <strong>{escape(employee.name)}</strong> با شماره <strong dir="ltr">{escape(employee.device_id)}</strong> را حذف کنید؟</p><p class="muted">حذف به‌صورت منطقی انجام می‌شود: کارمند از فهرست اصلی پنهان می‌شود، اما اطلاعات حضور و غیاب او محفوظ می‌ماند. در صورت نیاز می‌توانید او را از فیلتر «نمایش حذف‌شده‌ها» دوباره فعال کنید.</p><form method="post" action="/employees/{employee.id}/delete" class="actions"><input type="hidden" name="csrf_token" value="{token}"><button style="background:#a43d39">بله، حذف شود</button><a href="/employees" class="btn outline">انصراف</a></form></div>'
    return page('تأیید حذف کارمند', body, request, db, 'employees')


@app.post('/employees/{employee_id}/delete')
def delete_employee(employee_id: int, request: Request, csrf_token: str = Form(), db: Session = Depends(get_db)):
    user = permit(request, db, 'admin')
    csrf(request, csrf_token)
    employee = db.get(Employee, employee_id)
    if not employee:
        raise HTTPException(404, 'کارمند یافت نشد')
    if employee.status == 'deleted':
        return redirect('/employees', 'این کارمند قبلاً حذف شده است')
    employee.status = 'deleted'
    audit(db, user, 'employee_delete', employee.device_id)
    db.commit()
    return redirect('/employees', 'کارمند حذف منطقی شد؛ سوابق تردد او محفوظ است')


@app.post('/employees')
def add_employee(request: Request, name: str = Form(), device_id: str = Form(), csrf_token: str = Form(), db: Session = Depends(get_db)):
    user = permit(request, db, 'admin'); csrf(request, csrf_token)
    name, device_id = name.strip(), device_id.strip()
    if not name or not device_id or len(name) > 200 or len(device_id) > 80:
        raise HTTPException(400, 'مشخصات نامعتبر است')
    if db.scalar(select(Employee.id).where(Employee.device_id == device_id)):
        return redirect('/employees', 'شماره دستگاه قبلاً ثبت شده است')
    db.add(Employee(name=name, device_id=device_id)); audit(db, user, 'employee_create', device_id); db.commit()
    return redirect('/employees', 'کارمند با موفقیت ثبت شد')


@app.post('/employees/{employee_id}/edit')
def edit_employee(employee_id: int, request: Request, name: str = Form(), device_id: str = Form(), csrf_token: str = Form(), db: Session = Depends(get_db)):
    user = permit(request, db, 'admin'); csrf(request, csrf_token)
    emp = db.get(Employee, employee_id)
    if not emp: raise HTTPException(404)
    name, device_id = name.strip(), device_id.strip()
    if not name or not device_id or len(name) > 200 or len(device_id) > 80: raise HTTPException(400)
    if db.scalar(select(Employee.id).where(Employee.device_id == device_id, Employee.id != employee_id)):
        return redirect('/employees', 'شماره دستگاه تکراری است')
    if emp.device_id != device_id and db.scalar(select(Log.id).where(Log.device_id == emp.device_id).limit(1)):
        return redirect('/employees', 'شماره دستگاه کارمند دارای سابقه قابل تغییر نیست؛ برای حفظ سوابق با مدیر فنی هماهنگ کنید')
    emp.name, emp.device_id = name, device_id
    audit(db, user, 'employee_edit', device_id); db.commit()
    return redirect('/employees', 'اطلاعات ویرایش شد')


@app.post('/employees/{employee_id}/status')
def status_employee(employee_id: int, request: Request, status: str = Form(), csrf_token: str = Form(), db: Session = Depends(get_db)):
    user = permit(request, db, 'admin'); csrf(request, csrf_token)
    if status not in ('active', 'suspended', 'deleted'): raise HTTPException(400)
    emp = db.get(Employee, employee_id)
    if not emp: raise HTTPException(404)
    emp.status = status; audit(db, user, 'employee_status', f'{emp.device_id}: {status}'); db.commit()
    return redirect('/employees', 'وضعیت کارمند ثبت شد')


def parse_device_file(data: bytes):
    """Parse the verified six-tab-column attendance device export.

    Device ID / timestamp / four device-specific fields. Only ID and timestamp
    are used for the daily presence report; the other fields are validated as
    part of the exact input layout and do not imply check-in/check-out.
    """
    for encoding in ('utf-8-sig', 'utf-16', 'cp1256'):
        try:
            content = data.decode(encoding)
            if '\x00' not in content: break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError('کدگذاری فایل شناخته نشد')
    result, failures = [], []
    for line_no, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line: continue
        columns = [item.strip() for item in line.split('\t')]
        if len(columns) != 6 or any(not item for item in columns):
            failures.append(line_no)
            continue
        identifier, timestamp_text = columns[:2]
        if not re.fullmatch(r'[0-9]{1,80}', identifier) or not all(re.fullmatch(r'\d+', x) for x in columns[2:]):
            failures.append(line_no)
            continue
        try:
            naive = datetime.strptime(timestamp_text, '%Y-%m-%d %H:%M:%S')
            timestamp = naive.replace(tzinfo=TZ).astimezone(timezone.utc)
            result.append((identifier, timestamp))
        except ValueError:
            failures.append(line_no)
    return result, failures


@app.get('/imports')
def imports_page(request: Request, db: Session = Depends(get_db)):
    user = permit(request, db, 'admin', 'operator', 'viewer', 'exporter')
    token = csrf_token(request)
    upload = f'<div class="card"><h2>بارگذاری فایل دستگاه</h2><p class="muted">هر بار یک نام گزارش جدید انتخاب کنید. حتی اگر همان DAT را با نام جدید آپلود کنید، یک گزارش جدا ساخته می‌شود؛ ترددهای خام تکراری نمی‌شوند.</p><form method="post" enctype="multipart/form-data" action="/imports"><div class="fields"><div><label>نام گزارش</label><input name="report_name" maxlength="120" placeholder="مثلاً حضور آبان ۱۴۰۵" required></div><div><label>فایل تردد دستگاه</label><input type="file" name="file" accept=".dat,.txt" required></div><div><input type="hidden" name="csrf_token" value="{token}"><button>پردازش و مشاهده گزارش</button></div></div></form></div>' if user.role in ('admin', 'operator') else ''
    records = db.scalars(select(Import).order_by(Import.id.desc()).limit(100)).all()
    rows = ''
    for item in records:
        delete_button = f'<a class="btn outline" href="/imports/{item.id}/delete">حذف گزارش</a>' if user.role == 'admin' else ''
        label = db.scalar(select(ReportSet.name).where(ReportSet.import_id == item.id)) or 'بدون نام'
        report_id = db.scalar(select(ReportSet.id).where(ReportSet.import_id == item.id))
        day_count = db.scalar(select(func.count()).select_from(ReportDay).where(ReportDay.report_id == report_id)) if report_id else 0
        rows += f'<tr><td>{escape(label)}</td><td>{escape(item.filename)}</td><td>{day_count}</td><td>{escape(item.uploaded_at.astimezone(TZ).strftime("%Y-%m-%d %H:%M"))}</td><td>{delete_button}</td></tr>'
    body = msg(request) + upload + f'<div class="card"><h2>تاریخچه فایل‌ها</h2><p class="muted">گزارش‌های نام‌گذاری‌شده، مستقل و قابل انتخاب هستند.</p><div class="table-wrap"><table><tr><th>نام گزارش</th><th>فایل دستگاه</th><th>روزهای حضور</th><th>زمان پردازش</th><th>مدیریت</th></tr>{rows}</table></div></div>'
    return page('ورود اطلاعات دستگاه', body, request, db, 'imports')


@app.get('/imports/{import_id}/delete')
def delete_import_confirm(import_id: int, request: Request, db: Session = Depends(get_db)):
    permit(request, db, 'admin')
    item = db.get(Import, import_id)
    if not item:
        raise HTTPException(404, 'فایل موردنظر پیدا نشد')
    token = csrf_token(request)
    count = db.scalar(select(func.count()).select_from(Log).where(Log.source_import_id == import_id)) or 0
    content = f'<div class="card"><h2>تأیید حذف گزارش</h2><p>فایل: <strong>{escape(item.filename)}</strong></p><p>تعداد ترددهای ثبت‌شده از این فایل: <strong>{count}</strong></p><p class="muted">با حذف، رکوردهای همین فایل و گزارش‌های روزانه وابسته به آن بازسازی می‌شوند. کارکنان و ترددهای سایر فایل‌ها محفوظ می‌مانند. این عملیات برگشت خودکار ندارد؛ قبل از حذف از دیتابیس بکاپ داشته باشید.</p><form method="post" action="/imports/{item.id}/delete" class="actions"><input type="hidden" name="csrf_token" value="{token}"><button style="background:#9b3d38">بله، حذف شود</button><a href="/imports" class="btn outline">انصراف</a></form></div>'
    return page('حذف گزارش', content, request, db, 'imports')


@app.post('/imports/{import_id}/delete')
def delete_import(import_id: int, request: Request, csrf_token: str = Form(), db: Session = Depends(get_db)):
    user = permit(request, db, 'admin')
    csrf(request, csrf_token)
    item = db.get(Import, import_id)
    if not item:
        raise HTTPException(404, 'فایل موردنظر پیدا نشد')
    punches = list(db.scalars(select(Log).where(Log.source_import_id == import_id)))
    affected = {(x.device_id, x.occurred_at.astimezone(TZ).date().isoformat()) for x in punches}
    report_set = db.scalar(select(ReportSet).where(ReportSet.import_id == import_id))
    if report_set:
        for punch in db.scalars(select(ReportPunch).where(ReportPunch.report_id == report_set.id)).all():
            db.delete(punch)
        for day in db.scalars(select(ReportDay).where(ReportDay.report_id == report_set.id)).all():
            db.delete(day)
        db.flush()
        db.delete(report_set)
        db.flush()
    for punch in punches:
        db.delete(punch)
    db.flush()
    for device_id, day in affected:
        record = db.scalar(select(AttendanceRecord).where(AttendanceRecord.device_id == device_id, AttendanceRecord.local_day == day))
        if not record:
            continue
        day_start = datetime.fromisoformat(day).replace(tzinfo=TZ).astimezone(timezone.utc)
        day_end = (datetime.fromisoformat(day) + timedelta(days=1)).replace(tzinfo=TZ).astimezone(timezone.utc)
        remaining = list(db.scalars(select(Log.occurred_at).where(Log.device_id == device_id, Log.occurred_at >= day_start, Log.occurred_at < day_end).order_by(Log.occurred_at)))
        if not remaining:
            db.delete(record)
        else:
            record.first_punch = remaining[0]
            record.last_punch = remaining[-1] if len(remaining) > 1 else None
            record.punch_count = len(remaining)
    filename = item.filename
    db.delete(item)
    audit(db, user, 'file_delete', f'{filename} removed_punches={len(punches)}')
    db.commit()
    return redirect('/imports', f'گزارش «{filename}» حذف شد و روزهای حضور باقیمانده بازسازی شدند')


@app.post('/imports')
async def upload(request: Request, file: UploadFile = File(), report_name: str = Form(), csrf_token: str = Form(), db: Session = Depends(get_db)):
    user = permit(request, db, 'admin', 'operator'); csrf(request, csrf_token)
    report_name = ' '.join(report_name.strip().split())
    if not report_name or len(report_name) > 120:
        return redirect('/imports', 'نام گزارش باید بین ۱ تا ۱۲۰ نویسه باشد')
    if db.scalar(select(ReportSet.id).where(ReportSet.name == report_name)):
        return redirect('/imports', 'این نام گزارش قبلاً ثبت شده؛ نام دیگری انتخاب کنید')
    filename = Path(file.filename or '').name
    if not filename.lower().endswith(('.dat', '.txt')): raise HTTPException(400, 'پسوند فایل مجاز نیست')
    data = await file.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024: raise HTTPException(413, 'حجم فایل بیش از ۱۰ مگابایت است')
    # Uniqueness belongs to a named report, not the raw file alone: identical
    # DAT contents may intentionally be filed under different report names.
    digest = hashlib.sha256(data + b'\x00' + report_name.encode('utf-8')).hexdigest()
    try:
        parsed, failures = parse_device_file(data)
    except ValueError as ex:
        return redirect('/imports', str(ex))
    if not parsed:
        return redirect('/imports', 'هیچ رکورد معتبری شناسایی نشد؛ فرمت فایل دستگاه باید بررسی شود')
    unknown = sorted({code for code, _ in parsed if not db.scalar(select(Employee.id).where(Employee.device_id == code))})
    if unknown:
        return redirect('/imports', f'ثبت متوقف شد: {len(unknown)} شماره در فهرست کارکنان تعریف نشده است. نمونه: {", ".join(unknown[:5])}')
    if failures:
        return redirect('/imports', f'ثبت متوقف شد: {len(failures)} سطر قابل خواندن نیست. سطر اول: {failures[0]}')
    existing = set(db.execute(select(Log.device_id, Log.occurred_at).where(Log.device_id.in_([x[0] for x in parsed]), Log.occurred_at.between(min(t for _, t in parsed), max(t for _, t in parsed)))).all())
    unique = {(identifier, stamp) for identifier, stamp in parsed}
    pending = unique - existing
    imp = Import(filename=filename[:255], digest=digest, uploader_id=user.id, accepted=len(pending), rejected=len(parsed)-len(pending), note='پردازش موفق')
    db.add(imp); db.flush()
    report_set = ReportSet(import_id=imp.id, name=report_name)
    db.add(report_set); db.flush()
    for identifier, stamp_day in {(identifier, timestamp.astimezone(TZ).date().isoformat()) for identifier, timestamp in parsed}:
        db.add(ReportDay(report_id=report_set.id, device_id=identifier, local_day=stamp_day))
    for identifier, timestamp in set(parsed):
        db.add(ReportPunch(report_id=report_set.id, device_id=identifier, local_day=timestamp.astimezone(TZ).date().isoformat(), occurred_at=timestamp))
    for identifier, stamp in sorted(pending):
        db.add(Log(device_id=identifier, occurred_at=stamp, source_import_id=imp.id))
    db.flush()
    for identifier, local_day in {(code, stamp.astimezone(TZ).date().isoformat()) for code, stamp in pending}:
        day_start = datetime.fromisoformat(local_day).replace(tzinfo=TZ).astimezone(timezone.utc)
        day_end = (datetime.fromisoformat(local_day) + timedelta(days=1)).replace(tzinfo=TZ).astimezone(timezone.utc)
        times = list(db.scalars(select(Log.occurred_at).where(Log.device_id == identifier, Log.occurred_at >= day_start, Log.occurred_at < day_end).order_by(Log.occurred_at)))
        record = db.scalar(select(AttendanceRecord).where(AttendanceRecord.device_id == identifier, AttendanceRecord.local_day == local_day))
        if not record:
            record = AttendanceRecord(device_id=identifier, local_day=local_day, first_punch=times[0], punch_count=len(times))
            db.add(record)
        record.first_punch, record.last_punch, record.punch_count = times[0], times[-1] if len(times) > 1 else None, len(times)
    audit(db, user, 'file_import', f'{filename} accepted={len(pending)}'); db.commit()
    from urllib.parse import urlencode
    local_dates = [timestamp.astimezone(TZ).date() for _, timestamp in parsed]
    query = urlencode({'start_jalali': to_jalali(min(local_dates)), 'end_jalali': to_jalali(max(local_dates)), 'report_id': report_set.id, 'period': 'daily', 'message': f'گزارش «{report_name}» آماده شد'})
    return RedirectResponse('/reports?' + query, status_code=303)


def jalali(value):
    local = value.astimezone(TZ)
    j = jdatetime.date.fromgregorian(date=local.date())
    return f'{j.year:04d}/{j.month:02d}/{j.day:02d}', local.strftime('%H:%M:%S')


def to_jalali(day):
    value = jdatetime.date.fromgregorian(date=day)
    return f'{value.year:04d}/{value.month:02d}/{value.day:02d}'


def parse_jalali(value):
    normalized = value.translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')).replace('-', '/')
    match = re.fullmatch(r'(\d{4})/(\d{1,2})/(\d{1,2})', normalized)
    if not match:
        raise ValueError('فرمت تاریخ باید ۱۴۰۵/۰۸/۰۱ باشد')
    return jdatetime.date(*(int(x) for x in match.groups())).togregorian()


def range_jalali(start_jalali, end_jalali):
    try:
        start_day = parse_jalali(start_jalali)
        end_day = parse_jalali(end_jalali)
        if end_day < start_day or (end_day-start_day).days > 3660:
            raise ValueError()
        return start_day, end_day
    except ValueError:
        raise HTTPException(400, 'تاریخ شمسی نامعتبر است؛ بازه حداکثر ده سال باشد')


def report_rows(db, start_jalali, end_jalali, report_id, period):
    start, end = range_jalali(start_jalali, end_jalali)
    if period not in ('daily', 'monthly', 'yearly'):
        raise HTTPException(400, 'نوع گزارش نامعتبر است')
    stmt = select(ReportDay.device_id, ReportDay.local_day, Employee.name).join(Employee, Employee.device_id == ReportDay.device_id).join(ReportSet, ReportSet.id == ReportDay.report_id).where(ReportDay.local_day >= start.isoformat(), ReportDay.local_day <= end.isoformat())
    if report_id:
        if not db.get(ReportSet, report_id):
            raise HTTPException(404, 'گزارش انتخاب‌شده پیدا نشد')
        stmt = stmt.where(ReportDay.report_id == report_id)
    stmt = stmt.limit(100000)
    # Consolidate the same employee+day across multiple named uploads.
    unique_days = {(code, local_day, name) for code, local_day, name in db.execute(stmt)}
    if period == 'daily':
        punch_stmt = select(ReportPunch.device_id, ReportPunch.local_day, ReportPunch.occurred_at).where(ReportPunch.local_day >= start.isoformat(), ReportPunch.local_day <= end.isoformat())
        if report_id:
            punch_stmt = punch_stmt.where(ReportPunch.report_id == report_id)
        punches = {}
        for code, day, stamp in db.execute(punch_stmt.limit(200000)):
            punches.setdefault((code, day), set()).add(stamp.astimezone(TZ))
        rows = []
        for code, day, name in unique_days:
            times = sorted(punches.get((code, day), ()))
            first = times[0].strftime('%H:%M:%S') if times else '—'
            last = times[-1].strftime('%H:%M:%S') if len(times) > 1 else '—'
            rows.append([name, to_jalali(datetime.fromisoformat(day).date()), first, last])
        return sorted(rows, key=lambda row: (row[1], row[0]), reverse=True)
    counts = {}
    for code, day, name in unique_days:
        j = to_jalali(datetime.fromisoformat(day).date())
        key = j[:7] if period == 'monthly' else j[:4]
        counts[(code, name, key)] = counts.get((code, name, key), 0) + 1
    return sorted([[name, key, count] for (_, name, key), count in counts.items()], key=lambda row: (row[1], row[0]), reverse=True)


def report_filters(db):
    return db.execute(select(ReportSet.id, ReportSet.name).order_by(ReportSet.id.desc())).all()


@app.get('/reports')
def reports(request: Request, start_jalali: str = '', end_jalali: str = '', report_id: int = 0, period: str = 'daily', db: Session = Depends(get_db)):
    user = permit(request, db, 'admin', 'operator', 'viewer', 'exporter')
    today = datetime.now(TZ).date()
    start_jalali = start_jalali or to_jalali(today - timedelta(days=30))
    end_jalali = end_jalali or to_jalali(today)
    rows = report_rows(db, start_jalali, end_jalali, report_id, period)
    table = ''.join('<tr>' + ''.join(f'<td>{escape(x)}</td>' for x in row) + '</tr>' for row in rows)
    from urllib.parse import urlencode
    query = urlencode({'start_jalali': start_jalali, 'end_jalali': end_jalali, 'report_id': report_id, 'period': period})
    download = f'<a class="btn" href="/reports/download?{query}">دانلود Excel</a>' if user.role in ('admin', 'operator', 'exporter') else ''
    options = '<option value="0">تمام گزارش‌های نام‌گذاری‌شده</option>' + ''.join(f'<option value="{id}" {"selected" if id == report_id else ""}>{escape(name)}</option>' for id, name in report_filters(db))
    periods = ''.join(f'<option value="{key}" {"selected" if key == period else ""}>{label}</option>' for key, label in [('daily','روزانه'),('monthly','ماهانه'),('yearly','سالانه')])
    header = '<th>نام کارمند</th><th>تاریخ شمسی</th><th>اولین ثبت</th><th>آخرین ثبت</th>' if period == 'daily' else '<th>نام کارمند</th><th>دوره شمسی</th><th>تعداد روز حضور</th>'
    body = msg(request) + f'<div class="card"><h2>گزارش‌گیری دلخواه حضور</h2><form method="get"><div class="fields"><div><label>از تاریخ شمسی</label><input name="start_jalali" value="{escape(start_jalali)}" placeholder="۱۴۰۵/۰۸/۰۱" required></div><div><label>تا تاریخ شمسی</label><input name="end_jalali" value="{escape(end_jalali)}" placeholder="۱۴۰۵/۰۸/۳۰" required></div><div><label>گزارش</label><select name="report_id">{options}</select></div><div><label>نوع گزارش</label><select name="period">{periods}</select></div><div><button>نمایش گزارش</button></div></div></form><p class="muted">در حالت «تمام گزارش‌ها»، روزهای تکراری یک کارمند فقط یک بار شمرده می‌شوند. ساعت‌ها مربوط به اولین و آخرین ثبت اثر انگشت در همان روز هستند؛ این مقادیر لزوماً ورود و خروج قطعی نیستند.</p><div class="actions">{download}<span class="muted">{len(rows)} ردیف</span></div></div><div class="card table-wrap"><table><tr>{header}</tr>{table}</table></div>'
    return page('گزارش حضور', body, request, db, 'reports')


@app.get('/reports/download')
def download(request: Request, start_jalali: str, end_jalali: str, report_id: int = 0, period: str = 'daily', db: Session = Depends(get_db)):
    user = permit(request, db, 'admin', 'operator', 'exporter')
    rows = report_rows(db, start_jalali, end_jalali, report_id, period)
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = Workbook(); ws = wb.active; ws.title = 'گزارش حضور'
    ws.append(['نام کارمند', 'تاریخ شمسی', 'اولین ثبت', 'آخرین ثبت'] if period == 'daily' else ['نام کارمند', 'دوره شمسی', 'تعداد روز حضور'])
    for row in rows:
        ws.append([f"'{x}" if isinstance(x, str) and x.lstrip().startswith(('=', '+', '-', '@')) else x for x in row])
    for cell in ws[1]:
        cell.font = Font(bold=True, color='FFFFFF'); cell.fill = PatternFill('solid', fgColor='254D59'); cell.alignment = Alignment(horizontal='right')
    for col, width in {'A': 36, 'B': 22, 'C': 21, 'D': 21}.items(): ws.column_dimensions[col].width = width
    ws.freeze_panes = 'A2'; ws.sheet_view.rightToLeft = True
    buffer = io.BytesIO(); wb.save(buffer); buffer.seek(0)
    audit(db, user, 'report_download', f'{start_jalali}..{end_jalali} period={period} report={report_id}'); db.commit()
    return StreamingResponse(buffer, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={'Content-Disposition': 'attachment; filename="attendance.xlsx"'})


@app.get('/users')
def users_page(request: Request, db: Session = Depends(get_db)):
    permit(request, db, 'admin')
    token = csrf_token(request)
    form = f'<div class="card"><h2>ایجاد کاربر سامانه</h2><form method="post" action="/users" class="fields"><div><label>نام کاربری</label><input name="username" required maxlength="80"></div><div><label>گذرواژه (حداقل ۱۲ نویسه)</label><input name="password" type="password" minlength="12" required></div><div><label>نقش</label><select name="role"><option value="viewer">فقط مشاهده</option><option value="exporter">مشاهده و دانلود</option><option value="operator">آپلود و دانلود</option><option value="admin">مدیر</option></select></div><div><input type="hidden" name="csrf_token" value="{token}"><button>ایجاد حساب</button></div></form></div>'
    rows = ''
    for user in db.scalars(select(User).order_by(User.id)).all():
        actions = f'<form method="post" action="/users/{user.id}/toggle"><input type="hidden" name="csrf_token" value="{token}"><button class="outline">{"غیرفعال‌سازی" if user.active else "فعال‌سازی"}</button></form>'
        rows += f'<tr><td>{escape(user.username)}</td><td>{escape(user.role)}</td><td>{"فعال" if user.active else "غیرفعال"}</td><td>{actions}</td></tr>'
    return page('مدیریت کاربران', msg(request) + form + f'<div class="card table-wrap"><table><tr><th>کاربر</th><th>نقش</th><th>وضعیت</th><th>عملیات</th></tr>{rows}</table></div>', request, db, 'users')


@app.post('/users')
def create_user(request: Request, username: str = Form(), password: str = Form(), role: str = Form(), csrf_token: str = Form(), db: Session = Depends(get_db)):
    user = permit(request, db, 'admin'); csrf(request, csrf_token)
    if role not in ('admin', 'operator', 'viewer', 'exporter') or len(password) < 12 or not username.strip() or len(username) > 80:
        raise HTTPException(400, 'اطلاعات کاربر نامعتبر است')
    if db.scalar(select(User.id).where(User.username == username.strip())):
        return redirect('/users', 'نام کاربری قبلاً ثبت شده است')
    db.add(User(username=username.strip(), password_hash=password_hash(password), role=role)); audit(db, user, 'user_create', username); db.commit()
    return redirect('/users', 'کاربر ایجاد شد')


@app.post('/users/{user_id}/toggle')
def toggle_user(user_id: int, request: Request, csrf_token: str = Form(), db: Session = Depends(get_db)):
    actor_user = permit(request, db, 'admin'); csrf(request, csrf_token)
    target = db.get(User, user_id)
    if not target: raise HTTPException(404)
    if target.id == actor_user.id: return redirect('/users', 'امکان غیرفعال‌سازی حساب خودتان وجود ندارد')
    target.active = not target.active; audit(db, actor_user, 'user_toggle', target.username); db.commit()
    return redirect('/users', 'وضعیت حساب تغییر کرد')


@app.get('/audit')
def audit_page(request: Request, db: Session = Depends(get_db)):
    permit(request, db, 'admin')
    rows = ''.join(f'<tr><td>{escape(x.actor)}</td><td>{escape(x.event)}</td><td>{escape(x.details)}</td><td>{escape(x.created_at.astimezone(TZ).strftime("%Y-%m-%d %H:%M"))}</td></tr>' for x in db.scalars(select(Audit).order_by(Audit.id.desc()).limit(300)).all())
    return page('تاریخچه فعالیت', f'<div class="card table-wrap"><table><tr><th>کاربر</th><th>رویداد</th><th>جزئیات</th><th>زمان</th></tr>{rows}</table></div>', request, db, 'audit')
