"""v1.8.9-r5: administrator commits manager attendance directly.

All tests use fictional people and a private in-memory SQLite database.  The
legacy secretary/two-person path is covered separately by the r4 regression
suite and must not be silently rewritten during this upgrade.
"""

import asyncio
from datetime import datetime
from html import unescape
from pathlib import Path
import re
import unittest
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from main import (
    AttendanceCorrection, Audit, Base, Employee, ManagerAttendanceRequest,
    ReportPunch, User, commit_manager_excel_selection,
    download_manager_excel_template, manager_approvals_page,
    managers_attendance_page, managers_bulk_attendance_page, password_hash,
    password_ok, report_rows, revoke_manager_attendance, session_auth_tag,
    submit_manager_attendance, submit_managers_bulk_attendance,
    upload_manager_excel,
)


class AdminDirectTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite+pysqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.admin = User(username='r5-admin', password_hash=password_hash('synthetic-password!'),
                          role='admin', active=True)
        self.secretary = User(username='r5-secretary', password_hash='synthetic',
                              role='secretary', active=True)
        self.viewer = User(username='r5-viewer', password_hash='synthetic',
                           role='viewer', active=True)
        self.managers = [Employee(name=name, device_id=code, attendance_group='management')
                         for code, name in (
                             ('MAN-001', 'مدیر نمونه'), ('MAN-002', 'مدیر دوم'),
                             ('MAN-003', 'مدیر سوم'), ('MAN-004', 'مدیر چهارم'),
                             ('MAN-005', 'مدیر پنجم'))]
        self.staff = Employee(name='کارمند معمولی', device_id='101',
                              attendance_group='staff')
        self.db.add_all([self.admin, self.secretary, self.viewer, *self.managers, self.staff])
        self.db.commit()
        self.day = '1405/08/24'

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def request(self, user=None, *, csrf='r5-test-csrf', path='/managers/attendance/submit'):
        user = self.admin if user is None else user
        return Request({
            'type': 'http', 'method': 'POST', 'scheme': 'https',
            'path': path, 'query_string': b'', 'headers': [],
            'client': ('127.0.0.1', 1234), 'server': ('localhost', 8000),
            'session': {'uid': user.id, 'auth_tag': session_auth_tag(user),
                        'csrf': csrf},
        })

    def submit(self, employee=None, user=None, day=None, *, revision=0,
               first='08:00', last='17:00', reason='اطلاعات اعلامی مدیر',
               token='r5-test-csrf'):
        return submit_manager_attendance(
            self.request(user), employee_id=(employee or self.managers[0]).id,
            day_jalali=day or self.day, first_clock=first, last_clock=last,
            reason=reason, expected_revision=revision, csrf_token=token, db=self.db,
        )

    def reject(self, status_code, func):
        with self.assertRaises(HTTPException) as captured:
            func()
        self.assertEqual(captured.exception.status_code, status_code)
        self.db.rollback()

    def bulk(self, specs, *, user=None, token='r5-test-csrf'):
        user = self.admin if user is None else user
        fields = [('csrf_token', token)]
        for employee, day, first, last, reason in specs:
            fields += [('employee_ids', str(employee.id)),
                       (f'day_{employee.id}', day),
                       (f'first_{employee.id}', first),
                       (f'last_{employee.id}', last),
                       (f'reason_{employee.id}', reason)]
        data = urlencode(fields).encode()

        async def receive():
            return {'type': 'http.request', 'body': data, 'more_body': False}

        request = Request({
            'type': 'http', 'method': 'POST', 'scheme': 'https',
            'path': '/managers/attendance/bulk', 'query_string': b'',
            'headers': [(b'content-type', b'application/x-www-form-urlencoded')],
            'client': ('127.0.0.1', 1234), 'server': ('localhost', 8000),
            'session': {'uid': user.id, 'auth_tag': session_auth_tag(user),
                        'csrf': 'r5-test-csrf'},
        }, receive)
        return asyncio.run(submit_managers_bulk_attendance(request, db=self.db))

    def excel(self, fixture='manager-excel-150-valid.xlsx', *, user=None,
              token='r5-test-csrf'):
        user = self.admin if user is None else user
        content = (Path(__file__).resolve().parent / 'fixtures' / fixture).read_bytes()
        boundary = 'r5-synthetic-boundary'
        data = (f'--{boundary}\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\n'
                f'{token}\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{fixture}"\r\nContent-Type: '
                f'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n').encode()
        data += content + f'\r\n--{boundary}--\r\n'.encode()

        async def receive():
            return {'type': 'http.request', 'body': data, 'more_body': False}

        request = Request({
            'type': 'http', 'method': 'POST', 'scheme': 'https',
            'path': '/managers/attendance/excel-upload', 'query_string': b'',
            'headers': [(b'content-type', f'multipart/form-data; boundary={boundary}'.encode()),
                        (b'content-length', str(len(data)).encode())],
            'client': ('127.0.0.1', 1234), 'server': ('localhost', 8000),
            'session': {'uid': user.id, 'auth_tag': session_auth_tag(user),
                        'csrf': 'r5-test-csrf'},
        }, receive)
        return asyncio.run(upload_manager_excel(request, db=self.db))

    def confirm_excel(self, preview, selected, *, user=None, token='r5-test-csrf',
                      preview_token=None):
        user = self.admin if user is None else user
        if preview_token is None:
            found = re.search(r'name="preview_token" value="([^"]+)"', preview.body.decode())
            self.assertIsNotNone(found, 'preview includes a signed confirmation token')
            preview_token = unescape(found.group(1))
        fields = [('csrf_token', token), ('preview_token', preview_token)]
        fields.extend(('selected_rows', str(row)) for row in selected)
        data = urlencode(fields).encode()

        async def receive():
            return {'type': 'http.request', 'body': data, 'more_body': False}

        request = Request({
            'type': 'http', 'method': 'POST', 'scheme': 'https',
            'path': '/managers/attendance/excel-commit', 'query_string': b'',
            'headers': [(b'content-type', b'application/x-www-form-urlencoded'),
                        (b'content-length', str(len(data)).encode())],
            'client': ('127.0.0.1', 1234), 'server': ('localhost', 8000),
            'session': {'uid': user.id, 'auth_tag': session_auth_tag(user),
                        'csrf': 'r5-test-csrf'},
        }, receive)
        return asyncio.run(commit_manager_excel_selection(request, db=self.db))

    def test_admin_direct_single_entry_updates_reports_without_queue(self):
        self.assertTrue(password_ok('synthetic-password!', self.admin.password_hash))
        response = self.submit()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.db.query(ManagerAttendanceRequest).count(), 0)
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 1)
        correction = self.db.scalar(select(AttendanceCorrection))
        self.assertEqual((correction.origin, correction.actor_id, correction.action),
                         ('manager_manual', self.admin.id, 'set'))
        self.assertEqual(len(report_rows(self.db, self.day, self.day, 0, 'daily')), 1)
        self.assertEqual(self.db.query(ReportPunch).count(), 0)
        self.assertIsNotNone(self.db.scalar(select(Audit).where(
            Audit.event == 'manager_attendance_direct_set')))

    def test_direct_revision_and_revoke_keep_audit_history(self):
        self.submit()
        first = self.db.scalar(select(AttendanceCorrection))
        self.reject(409, lambda: self.submit(first='09:00'))
        self.submit(first='09:00', last='18:00', revision=first.id)
        latest = self.db.scalar(select(AttendanceCorrection).order_by(
            AttendanceCorrection.id.desc()))
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 2)
        self.reject(409, lambda: revoke_manager_attendance(
            self.request(), self.managers[0].id, self.day,
            'لغو اشتباه', first.id, 'r5-test-csrf', self.db))
        response = revoke_manager_attendance(
            self.request(), self.managers[0].id, self.day,
            'لغو ثبت اشتباه', latest.id, 'r5-test-csrf', self.db)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 3)
        self.assertEqual(self.db.query(AttendanceCorrection).filter_by(action='revoke').count(), 1)
        self.assertEqual(report_rows(self.db, self.day, self.day, 0, 'daily'), [])

    def test_admin_bulk_is_atomic_and_commits_immediately(self):
        specs = [(self.managers[0], self.day, '08:00', '16:00', 'ثبت اول مدیر'),
                 (self.managers[1], self.day, '09:00', '18:00', 'ثبت دوم مدیر')]
        self.assertEqual(self.bulk(specs).status_code, 303)
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 2)
        self.assertEqual(self.db.query(ManagerAttendanceRequest).count(), 0)
        self.reject(409, lambda: self.bulk([
            (self.managers[0], self.day, '08:00', '16:00', 'دوباره اول'),
            (self.managers[2], self.day, '08:00', '16:00', 'ثبت سوم مدیر'),
        ]))
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 2)

    def test_excel_150_manager_days_preview_then_apply_without_approval(self):
        preview = self.excel()
        self.assertEqual(preview.status_code, 200)
        html = preview.body.decode()
        self.assertIn('پیش‌نمایش Excel مدیران', html)
        self.assertIn('manager-excel-scroll', html)
        self.assertIn('manager-excel-select-all', html)
        # Count rendered checkboxes only; the shipped JavaScript also contains
        # the literal name="selected_rows" in its querySelectorAll selector.
        self.assertEqual(html.count('<input type="checkbox" name="selected_rows"'), 150)
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 0)
        self.assertEqual(self.db.query(Audit).count(), 0)
        response = self.confirm_excel(preview, range(2, 152))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 150)
        self.assertEqual(self.db.query(ManagerAttendanceRequest).count(), 0)
        self.assertEqual(len(report_rows(self.db, '1405/07/01', '1405/07/30', 0, 'daily')), 150)
        self.assertEqual(self.db.query(ReportPunch).count(), 0)
        self.reject(409, lambda: self.confirm_excel(preview, range(2, 152)))
        second_preview = self.excel()
        self.assertEqual(second_preview.status_code, 200)
        self.assertIn('150 ردیف از قبل ثبت شده یا تداخل دارد', second_preview.body.decode())
        self.assertEqual(len(re.findall(r'name="selected_rows"[^>]* disabled>',
                                        second_preview.body.decode())), 150)
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 150)

    def test_excel_preview_can_commit_only_selected_rows(self):
        preview = self.excel()
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 0)
        self.assertEqual(self.confirm_excel(preview, [2, 3, 4]).status_code, 303)
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 3)
        second_preview = self.excel()
        self.assertIn('3 ردیف از قبل ثبت شده یا تداخل دارد', second_preview.body.decode())
        self.assertEqual(self.confirm_excel(second_preview, [5, 6]).status_code, 303)
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 5)

    def test_excel_preview_rejects_tamper_empty_duplicate_wrong_user_and_csrf(self):
        preview = self.excel()
        self.reject(400, lambda: self.confirm_excel(preview, []))
        self.reject(400, lambda: self.confirm_excel(preview, [2, 2]))
        self.reject(400, lambda: self.confirm_excel(preview, [999]))
        self.reject(403, lambda: self.confirm_excel(preview, [2], token='wrong'))
        self.reject(403, lambda: self.confirm_excel(preview, [2], user=self.secretary))
        self.reject(403, lambda: self.confirm_excel(preview, [2], preview_token='tampered'))
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 0)

    def test_excel_preview_selection_is_atomic_if_record_changes_after_preview(self):
        preview = self.excel()
        self.assertEqual(self.confirm_excel(preview, [2]).status_code, 303)
        self.reject(409, lambda: self.confirm_excel(preview, [2, 3]))
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 1)

    def test_secretary_excel_preview_enqueues_only_selected_rows(self):
        preview = self.excel(user=self.secretary)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(self.db.query(ManagerAttendanceRequest).count(), 0)
        self.assertEqual(self.confirm_excel(preview, [2, 3], user=self.secretary).status_code, 303)
        self.assertEqual(self.db.query(ManagerAttendanceRequest).count(), 2)
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 0)

    def test_excel_invalid_row_never_partially_applies(self):
        self.reject(400, lambda: self.excel('manager-excel-unknown.xlsx'))
        self.reject(400, lambda: self.excel('manager-excel-duplicate.xlsx'))
        self.reject(400, lambda: self.excel('manager-excel-invalid-clock.xlsx'))
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 0)

    def test_roles_csrf_device_and_existing_pending_block_direct_edits(self):
        self.reject(403, lambda: self.submit(user=self.viewer))
        self.reject(403, lambda: self.submit(token='incorrect'))
        self.reject(404, lambda: self.submit(employee=self.staff))
        self.reject(400, lambda: self.submit(first='17:00', last='08:00'))
        self.reject(403, lambda: self.excel(user=self.viewer))
        self.reject(403, lambda: self.excel(token='incorrect'))
        self.reject(410, lambda: revoke_manager_attendance(
            self.request(self.secretary), self.managers[0].id, self.day,
            'لغو بدون مجوز', 0, 'r5-test-csrf', self.db))
        self.db.add(ManagerAttendanceRequest(
            employee_id=self.managers[0].id, local_day='2026-11-15',
            proposed_first=datetime.fromisoformat('2026-11-15T08:00:00+00:00'),
            proposed_last=datetime.fromisoformat('2026-11-15T16:00:00+00:00'),
            reason='درخواست قدیمی', requested_by_id=self.secretary.id, status='pending',
        ))
        self.db.commit()
        self.reject(409, lambda: self.submit())
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 0)

    def test_admin_ui_exposes_manager_definition_and_direct_excel(self):
        response = managers_attendance_page(self.request(), db=self.db)
        self.assertEqual(response.status_code, 200)
        self.assertIn('ثبت مستقیم', response.body.decode())
        response = managers_bulk_attendance_page(self.request(), db=self.db)
        self.assertEqual(response.status_code, 200)
        bulk_html = response.body.decode()
        self.assertIn('نمایش پیش‌نمایش Excel', bulk_html)
        self.assertIn('/managers/attendance/excel-template', bulk_html)
        self.assertIn('برای ۱۵۰ ردیف Excel، نیازی به تیک‌زدن', bulk_html)
        self.assertIn('id="manager-bulk-select-all"', bulk_html)
        self.assertIn('id="manager-bulk-form"', bulk_html)
        self.assertIn('id="manager-bulk-apply-shared"', bulk_html)
        self.assertIn('id="manager-bulk-feedback"', bulk_html)
        self.assertIn('در حال ثبت است', bulk_html)
        self.assertIn('script nonce=', bulk_html)
        self.assertIn('نسخه 1.8.9-r7.3-dev', bulk_html)
        self.assertEqual(download_manager_excel_template(self.request(), db=self.db).status_code, 200)
        self.assertEqual(manager_approvals_page(self.request(), db=self.db).status_code, 200)


if __name__ == '__main__':
    unittest.main()
