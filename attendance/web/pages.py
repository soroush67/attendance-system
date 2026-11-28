"""HTML routes. Every route except /login declares its required roles; the
check runs on the server before any data is read."""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from attendance.config import get_settings
from attendance.db import get_db
from attendance.models import (ROLES, AttendanceCorrection, Audit, Employee, Import, LeaveDay,
                               Log, ReportDay, ReportSet, User)
from attendance.security import (client_ip, hash_password, ip_blocked, is_locked, password_problem,
                                 record_failure, record_success, session_user, start_session, verify_password)
from attendance.services.importer import IngestError, delete_import, ingest, punches_of_import
from attendance.services.jalali import to_jalali
from attendance.services.jalali import current_jalali_ym, jalali_month_name
from attendance.services.reports import HEADERS, PERIODS, ReportError, ReportQuery, report_rows, to_xlsx
from attendance.services.work_hours import (STATUS_ABSENT, STATUS_DEFICIT, STATUS_HOLIDAY,
                                            STATUS_LEAVE, STATUS_OVERTIME, STATUS_PRESENT,
                                            month_grid)
from attendance.web.deps import (ALL_ROLES, CAN_EXPORT, CAN_UPLOAD, audit, check_csrf, current_user, redirect,
                                 render, require)

router = APIRouter()
LOGIN_FAILED = 'نام کاربری یا گذرواژه اشتباه است'


# ------------------------------------------------------------------ auth

@router.get('/login')
def login_page(request: Request, db: Session = Depends(get_db)):
    if session_user(request, db):
        return RedirectResponse('/', status_code=303)
    return render(request, 'login.html', 'ورود')


@router.post('/login')
def login(request: Request, username: str = Form('', max_length=80), password: str = Form('', max_length=256),
          csrf_token: str = Form(''), db: Session = Depends(get_db)):
    check_csrf(request, csrf_token)
    ip = client_ip(request)
    username = username.strip()
    if ip_blocked(db, ip):
        audit(db, username or '-', 'login_ip_blocked', ip)
        db.commit()
        return render(request, 'login.html', 'ورود', status_code=429,
                      error='تلاش‌های ناموفق زیادی از این نشانی انجام شده است؛ چند دقیقه بعد دوباره تلاش کنید')
    user = db.scalar(select(User).where(User.username == username)) if username else None
    valid, needs_rehash = verify_password(password, user.password_hash if user else None)
    if user and is_locked(user):
        record_failure(db, None, username, ip)
        audit(db, username, 'login_locked', ip)
        db.commit()
        # Same message whether the password was right or not: no oracle.
        return render(request, 'login.html', 'ورود', status_code=429,
                      error='حساب به‌دلیل تلاش‌های ناموفق موقتاً قفل شده است؛ بعداً دوباره تلاش کنید')
    if not user or not user.active or not valid:
        record_failure(db, user if user and user.active else None, username, ip)
        audit(db, username or '-', 'login_failed', ip)
        db.commit()
        return render(request, 'login.html', 'ورود', status_code=401, error=LOGIN_FAILED)
    if needs_rehash:
        user.password_hash = hash_password(password)
    record_success(db, user, ip)
    audit(db, user.username, 'login', ip)
    db.commit()
    start_session(request, user)
    return redirect('/account/password' if user.must_change_password else '/')


@router.post('/logout')
def logout(request: Request, csrf_token: str = Form(''), db: Session = Depends(get_db)):
    check_csrf(request, csrf_token)
    user = session_user(request, db)
    if user:
        audit(db, user.username, 'logout')
        db.commit()
    request.session.clear()
    return redirect('/login', 'از سامانه خارج شدید')


@router.get('/account/password')
def password_page(request: Request, user: User = Depends(current_user)):
    return render(request, 'password.html', 'تغییر گذرواژه', 'account', forced=user.must_change_password)


@router.post('/account/password')
def change_password(request: Request, current: str = Form('', max_length=256), new: str = Form('', max_length=256),
                    confirm: str = Form('', max_length=256), csrf_token: str = Form(''),
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    check_csrf(request, csrf_token)
    valid, _ = verify_password(current, user.password_hash)
    if not valid:
        return redirect('/account/password', 'گذرواژه فعلی اشتباه است', error=True)
    if new != confirm:
        return redirect('/account/password', 'تکرار گذرواژه جدید یکسان نیست', error=True)
    if new == current:
        return redirect('/account/password', 'گذرواژه جدید باید با گذرواژه فعلی متفاوت باشد', error=True)
    problem = password_problem(new, user.username)
    if problem:
        return redirect('/account/password', problem, error=True)
    user.password_hash = hash_password(new)
    user.must_change_password = False
    user.password_changed_at = datetime.now(timezone.utc)
    user.session_version += 1  # sign out every other session of this user
    audit(db, user.username, 'password_change')
    db.commit()
    start_session(request, user)
    return redirect('/', 'گذرواژه تغییر کرد')


@router.post('/account/logout-all')
def logout_all(request: Request, csrf_token: str = Form(''), user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    check_csrf(request, csrf_token)
    user.session_version += 1
    audit(db, user.username, 'logout_all')
    db.commit()
    request.session.clear()
    return redirect('/login', 'از همه دستگاه‌ها خارج شدید')


# ------------------------------------------------------------------ dashboard

@router.get('/')
def dashboard(request: Request, user: User = Depends(require(*ALL_ROLES)), db: Session = Depends(get_db)):
    stats = {
        'employees': db.scalar(select(func.count()).select_from(Employee).where(Employee.status == 'active')),
        'punches': db.scalar(select(func.count()).select_from(Log)),
        'imports': db.scalar(select(func.count()).select_from(Import)),
    }
    return render(request, 'dashboard.html', 'نمای کلی', 'dashboard', stats=stats)


# ------------------------------------------------------------------ employees

STATUS = {'active': 'فعال', 'suspended': 'معلق', 'deleted': 'حذف‌شده'}


@router.get('/employees')
def employees(request: Request, search: str = '', show_deleted: int = 0,
              user: User = Depends(require(*ALL_ROLES)), db: Session = Depends(get_db)):
    search = search.strip()[:100]
    stmt = select(Employee).order_by(Employee.id.desc()).limit(500)
    if show_deleted != 1:
        stmt = stmt.where(Employee.status != 'deleted')
    if search:
        pattern = '%' + search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        stmt = stmt.where(Employee.name.ilike(pattern, escape='\\') | Employee.device_id.ilike(pattern, escape='\\'))
    return render(request, 'employees.html', 'مدیریت کارکنان', 'employees', employees=db.scalars(stmt).all(),
                  search=search, show_deleted=show_deleted == 1, status_labels=STATUS)


def _employee_fields(name: str, device_id: str) -> tuple[str, str]:
    name, device_id = ' '.join(name.split()), device_id.strip()
    if not name or not device_id or len(name) > 200 or len(device_id) > 80 or not device_id.isdigit():
        raise HTTPException(400, 'نام و شماره دستگاه (فقط رقم) را درست وارد کنید')
    return name, device_id


@router.post('/employees')
def add_employee(request: Request, name: str = Form(''), device_id: str = Form(''), csrf_token: str = Form(''),
                 user: User = Depends(require('admin')), db: Session = Depends(get_db)):
    check_csrf(request, csrf_token)
    name, device_id = _employee_fields(name, device_id)
    if db.scalar(select(Employee.id).where(Employee.device_id == device_id)):
        return redirect('/employees', 'شماره دستگاه قبلاً ثبت شده است', error=True)
    db.add(Employee(name=name, device_id=device_id))
    audit(db, user.username, 'employee_create', device_id)
    db.commit()
    return redirect('/employees', 'کارمند با موفقیت ثبت شد')


@router.post('/employees/{employee_id}/edit')
def edit_employee(employee_id: int, request: Request, name: str = Form(''), device_id: str = Form(''),
                  csrf_token: str = Form(''), user: User = Depends(require('admin')), db: Session = Depends(get_db)):
    check_csrf(request, csrf_token)
    emp = db.get(Employee, employee_id)
    if not emp:
        raise HTTPException(404, 'کارمند یافت نشد')
    name, device_id = _employee_fields(name, device_id)
    if db.scalar(select(Employee.id).where(Employee.device_id == device_id, Employee.id != employee_id)):
        return redirect('/employees', 'شماره دستگاه تکراری است', error=True)
    if emp.device_id != device_id and db.scalar(select(Log.id).where(Log.device_id == emp.device_id).limit(1)):
        return redirect('/employees', 'شماره دستگاه کارمندی که سابقه تردد دارد قابل تغییر نیست', error=True)
    emp.name, emp.device_id = name, device_id
    audit(db, user.username, 'employee_edit', device_id)
    db.commit()
    return redirect('/employees', 'اطلاعات ویرایش شد')


@router.post('/employees/{employee_id}/status')
def employee_status(employee_id: int, request: Request, status: str = Form(''), csrf_token: str = Form(''),
                    user: User = Depends(require('admin')), db: Session = Depends(get_db)):
    check_csrf(request, csrf_token)
    if status not in STATUS:
        raise HTTPException(400, 'وضعیت نامعتبر است')
    emp = db.get(Employee, employee_id)
    if not emp:
        raise HTTPException(404, 'کارمند یافت نشد')
    emp.status = status
    audit(db, user.username, 'employee_status', f'{emp.device_id}: {status}')
    db.commit()
    return redirect('/employees', 'وضعیت کارمند ثبت شد')


@router.get('/employees/{employee_id}/delete')
def employee_delete_confirm(employee_id: int, request: Request, user: User = Depends(require('admin')),
                            db: Session = Depends(get_db)):
    emp = db.get(Employee, employee_id)
    if not emp:
        raise HTTPException(404, 'کارمند یافت نشد')
    return render(request, 'employee_delete.html', 'تأیید حذف کارمند', 'employees', employee=emp)


@router.post('/employees/{employee_id}/delete')
def employee_delete(employee_id: int, request: Request, csrf_token: str = Form(''),
                    user: User = Depends(require('admin')), db: Session = Depends(get_db)):
    check_csrf(request, csrf_token)
    emp = db.get(Employee, employee_id)
    if not emp:
        raise HTTPException(404, 'کارمند یافت نشد')
    if emp.status != 'deleted':
        emp.status = 'deleted'
        audit(db, user.username, 'employee_delete', emp.device_id)
        db.commit()
    return redirect('/employees', 'کارمند حذف منطقی شد؛ سوابق تردد او محفوظ است')


# ------------------------------------------------------------------ imports

@router.get('/imports')
def imports_page(request: Request, user: User = Depends(require(*ALL_ROLES)), db: Session = Depends(get_db)):
    rows = db.execute(
        select(Import, ReportSet.id, ReportSet.name, func.count(ReportDay.id))
        .outerjoin(ReportSet, ReportSet.import_id == Import.id)
        .outerjoin(ReportDay, ReportDay.report_id == ReportSet.id)
        .group_by(Import.id, ReportSet.id).order_by(Import.id.desc()).limit(100)).all()
    tz = get_settings().timezone
    items = [{'imp': imp, 'report_id': rid, 'name': name or 'بدون نام', 'days': days,
              'uploaded': imp.uploaded_at.astimezone(tz).strftime('%Y-%m-%d %H:%M')} for imp, rid, name, days in rows]
    return render(request, 'imports.html', 'ورود اطلاعات دستگاه', 'imports', items=items,
                  max_mb=get_settings().upload_max_bytes // (1024 * 1024))


@router.post('/imports')
async def upload(request: Request, file: UploadFile = File(...), report_name: str = Form(''),
                 csrf_token: str = Form(''), user: User = Depends(require(*CAN_UPLOAD)),
                 db: Session = Depends(get_db)):
    check_csrf(request, csrf_token)
    limit = get_settings().upload_max_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        return redirect('/imports', f'حجم فایل بیش از {limit // (1024 * 1024)} مگابایت است', error=True)
    try:
        result = ingest(db, user, file.filename or '', data, report_name)
    except IngestError as exc:
        db.rollback()
        return redirect('/imports', str(exc), error=True)
    audit(db, user.username, 'file_import', f'{result.report_name} accepted={result.accepted}')
    db.commit()
    query = urlencode({'start_jalali': to_jalali(result.first_day), 'end_jalali': to_jalali(result.last_day),
                       'report_id': result.report_id, 'period': 'daily',
                       'message': f'گزارش «{result.report_name}» آماده شد'})
    return RedirectResponse('/reports?' + query, status_code=303)


@router.get('/imports/{import_id}/delete')
def import_delete_confirm(import_id: int, request: Request, user: User = Depends(require('admin')),
                          db: Session = Depends(get_db)):
    item = db.get(Import, import_id)
    if not item:
        raise HTTPException(404, 'فایل موردنظر پیدا نشد')
    return render(request, 'import_delete.html', 'حذف گزارش', 'imports', item=item, count=punches_of_import(db, item.id))


@router.post('/imports/{import_id}/delete')
def import_delete(import_id: int, request: Request, csrf_token: str = Form(''),
                  user: User = Depends(require('admin')), db: Session = Depends(get_db)):
    check_csrf(request, csrf_token)
    item = db.get(Import, import_id)
    if not item:
        raise HTTPException(404, 'فایل موردنظر پیدا نشد')
    filename = item.filename
    removed = delete_import(db, item)
    audit(db, user.username, 'file_delete', f'{filename} removed_punches={removed}')
    db.commit()
    return redirect('/imports', f'گزارش «{filename}» حذف شد و روزهای حضور باقیمانده بازسازی شدند')


# ------------------------------------------------------------------ reports

def _report_query(start_jalali: str, end_jalali: str, report_id: int, period: str) -> ReportQuery:
    today = datetime.now(get_settings().timezone).date()
    return ReportQuery.parse(start_jalali or to_jalali(today - timedelta(days=30)), end_jalali or to_jalali(today),
                             report_id, period)


@router.get('/reports')
def reports(request: Request, start_jalali: str = '', end_jalali: str = '', report_id: int = 0, period: str = 'daily',
            user: User = Depends(require(*ALL_ROLES)), db: Session = Depends(get_db)):
    error, rows, q = '', [], None
    try:
        q = _report_query(start_jalali, end_jalali, report_id, period)
        rows = report_rows(db, q)
    except ReportError as exc:
        error = str(exc)
    today = datetime.now(get_settings().timezone).date()
    form = {'start_jalali': start_jalali or to_jalali(today - timedelta(days=30)),
            'end_jalali': end_jalali or to_jalali(today), 'report_id': report_id,
            'period': period if period in PERIODS else 'daily'}
    reports_list = db.execute(select(ReportSet.id, ReportSet.name).order_by(ReportSet.id.desc())).all()
    return render(request, 'reports.html', 'گزارش حضور', 'reports', rows=rows, form=form, periods=PERIODS,
                  headers=HEADERS[form['period']], reports_list=reports_list, report_error=error,
                  download_query=urlencode(form) if q else '')


@router.get('/reports/download')
def download(request: Request, start_jalali: str = '', end_jalali: str = '', report_id: int = 0,
             period: str = 'daily', user: User = Depends(require(*CAN_EXPORT)), db: Session = Depends(get_db)):
    try:
        q = _report_query(start_jalali, end_jalali, report_id, period)
        rows = report_rows(db, q)
    except ReportError as exc:
        raise HTTPException(400, str(exc)) from None
    audit(db, user.username, 'report_download', f'{to_jalali(q.start)}..{to_jalali(q.end)} period={q.period} '
                                                 f'report={q.report_id} rows={len(rows)}')
    db.commit()
    return StreamingResponse(to_xlsx(rows, q.period),
                             media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                             headers={'Content-Disposition': 'attachment; filename="attendance.xlsx"'})


# ------------------------------------------------------------------ users (admin)

@router.get('/users')
def users_page(request: Request, user: User = Depends(require('admin')), db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    return render(request, 'users.html', 'کاربران و دسترسی‌ها', 'users', users=db.scalars(select(User).order_by(User.id)).all(),
                  now=now, min_length=get_settings().password_min_length)


@router.post('/users')
def create_user(request: Request, username: str = Form(''), password: str = Form('', max_length=256),
                role: str = Form(''), csrf_token: str = Form(''), user: User = Depends(require('admin')),
                db: Session = Depends(get_db)):
    check_csrf(request, csrf_token)
    username = username.strip()
    if role not in ROLES or not username or len(username) > 80 or not username.replace('.', '').replace('_', '').replace('-', '').isalnum():
        return redirect('/users', 'نام کاربری (حروف لاتین، رقم، . _ -) یا نقش نامعتبر است', error=True)
    problem = password_problem(password, username)
    if problem:
        return redirect('/users', problem, error=True)
    if db.scalar(select(User.id).where(func.lower(User.username) == username.lower())):
        return redirect('/users', 'نام کاربری قبلاً ثبت شده است', error=True)
    db.add(User(username=username, password_hash=hash_password(password), role=role, active=True,
                must_change_password=True, password_changed_at=datetime.now(timezone.utc)))
    audit(db, user.username, 'user_create', f'{username} role={role}')
    db.commit()
    return redirect('/users', 'کاربر ایجاد شد؛ در اولین ورود باید گذرواژه را تغییر دهد')


def _target(db: Session, user_id: int, actor: User) -> User:
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, 'کاربر یافت نشد')
    if target.id == actor.id:
        raise HTTPException(400, 'این عملیات روی حساب خودتان مجاز نیست')
    return target


@router.post('/users/{user_id}/toggle')
def toggle_user(user_id: int, request: Request, csrf_token: str = Form(''), user: User = Depends(require('admin')),
                db: Session = Depends(get_db)):
    check_csrf(request, csrf_token)
    target = _target(db, user_id, user)
    target.active = not target.active
    target.session_version += 1
    audit(db, user.username, 'user_toggle', f'{target.username} active={target.active}')
    db.commit()
    return redirect('/users', 'وضعیت حساب تغییر کرد')


@router.post('/users/{user_id}/role')
def change_role(user_id: int, request: Request, role: str = Form(''), csrf_token: str = Form(''),
                user: User = Depends(require('admin')), db: Session = Depends(get_db)):
    check_csrf(request, csrf_token)
    if role not in ROLES:
        raise HTTPException(400, 'نقش نامعتبر است')
    target = _target(db, user_id, user)
    target.role = role
    target.session_version += 1
    audit(db, user.username, 'user_role', f'{target.username} role={role}')
    db.commit()
    return redirect('/users', 'نقش کاربر تغییر کرد')


@router.post('/users/{user_id}/reset')
def reset_user(user_id: int, request: Request, password: str = Form('', max_length=256), csrf_token: str = Form(''),
               user: User = Depends(require('admin')), db: Session = Depends(get_db)):
    check_csrf(request, csrf_token)
    target = _target(db, user_id, user)
    problem = password_problem(password, target.username)
    if problem:
        return redirect('/users', problem, error=True)
    target.password_hash = hash_password(password)
    target.must_change_password = True
    target.failed_logins, target.locked_until = 0, None
    target.session_version += 1
    audit(db, user.username, 'user_password_reset', target.username)
    db.commit()
    return redirect('/users', f'گذرواژه «{target.username}» بازنشانی شد و حساب از قفل خارج شد')


@router.get('/employees/{emp_id}/monthly')
def employee_monthly(
    emp_id: int, request: Request,
    year: int = 0, month: int = 0,
    user: User = Depends(require('operator')),
    db: Session = Depends(get_db),
):
    employee = db.get(Employee, emp_id)
    if not employee:
        raise HTTPException(404, 'کارمند یافت نشد')
    if not year or not month:
        year, month = current_jalali_ym()
    entries, summary, ws = month_grid(db, employee, year, month)
    month_name = jalali_month_name(month)
    return render(request, 'monthly.html', f'کارکرد ماهانه — {employee.name}',
                  'employees',
                  employee=employee, entries=entries, summary=summary,
                  ws=ws, year=year, month=month, month_name=month_name,
                  STATUS_PRESENT=STATUS_PRESENT, STATUS_DEFICIT=STATUS_DEFICIT,
                  STATUS_ABSENT=STATUS_ABSENT, STATUS_LEAVE=STATUS_LEAVE,
                  STATUS_OVERTIME=STATUS_OVERTIME, STATUS_HOLIDAY=STATUS_HOLIDAY)


@router.post('/employees/{emp_id}/leave')
def add_leave(
    emp_id: int, request: Request,
    local_day: str = Form(''), leave_type: str = Form('مرخصی'),
    year: int = Form(0), month: int = Form(0),
    csrf_token: str = Form(''),
    user: User = Depends(require('operator')),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token)
    employee = db.get(Employee, emp_id)
    if not employee:
        raise HTTPException(404, 'کارمند یافت نشد')
    existing = db.scalar(
        select(LeaveDay).where(LeaveDay.employee_id == emp_id, LeaveDay.local_day == local_day)
    )
    if existing:
        db.delete(existing)
        audit(db, user.username, 'leave_remove', f'emp={emp_id} day={local_day}')
        msg = 'مرخصی حذف شد'
    else:
        db.add(LeaveDay(employee_id=emp_id, local_day=local_day,
                        leave_type=leave_type, actor_id=user.id))
        audit(db, user.username, 'leave_add', f'emp={emp_id} day={local_day}')
        msg = 'مرخصی ثبت شد'
    db.commit()
    qs = urlencode({'year': year, 'month': month}) if year else ''
    return redirect(f'/employees/{emp_id}/monthly?{qs}', msg)


@router.post('/employees/{emp_id}/correction')
def add_correction(
    emp_id: int, request: Request,
    local_day: str = Form(''), punch_time: str = Form(''),
    punch_type: str = Form('in'), reason: str = Form(''),
    year: int = Form(0), month: int = Form(0),
    csrf_token: str = Form(''),
    user: User = Depends(require('operator')),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token)
    employee = db.get(Employee, emp_id)
    if not employee:
        raise HTTPException(404, 'کارمند یافت نشد')
    # اعتبارسنجی فرمت زمان
    import re as _re
    if not _re.fullmatch(r'\d{2}:\d{2}(:\d{2})?', punch_time.strip()):
        return redirect(f'/employees/{emp_id}/monthly', 'فرمت ساعت نامعتبر است', error=True)
    pt = punch_time.strip()
    if len(pt) == 5:
        pt += ':00'
    db.add(AttendanceCorrection(employee_id=emp_id, actor_id=user.id,
                                local_day=local_day, punch_time=pt,
                                punch_type=punch_type, reason=reason))
    audit(db, user.username, 'correction_add',
          f'emp={emp_id} day={local_day} {punch_type}={pt}')
    db.commit()
    qs = urlencode({'year': year, 'month': month}) if year else ''
    return redirect(f'/employees/{emp_id}/monthly?{qs}', 'اصلاح ثبت شد')


@router.get('/audit')
def audit_page(request: Request, user: User = Depends(require('admin')), db: Session = Depends(get_db)):
    tz = get_settings().timezone
    rows = [(a.actor, a.event, a.details, a.created_at.astimezone(tz).strftime('%Y-%m-%d %H:%M'))
            for a in db.scalars(select(Audit).order_by(Audit.id.desc()).limit(300))]
    return render(request, 'audit.html', 'تاریخچه فعالیت', 'audit', rows=rows)
