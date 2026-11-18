"""Two-person management attendance approval; isolated in-memory SQLite."""

import unittest
import asyncio
from html import unescape
from pathlib import Path
from datetime import datetime, time, timezone
import re
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from main import (
    AttendanceCorrection, Audit, Base, Employee, Import, ManagerAttendanceRequest,
    ReportDay, ReportPunch, ReportSet, TZ, User, cancel_manager_attendance,
    dashboard, manager_approvals_page, managers_attendance_page,
    managers_bulk_attendance_page, submit_managers_bulk_attendance, report_rows,
    commit_manager_excel_selection, download_manager_excel_template, upload_manager_excel,
    resolve_manager_attendance, revoke_manager_attendance,
    save_manager_attendance, session_auth_tag, submit_manager_attendance,
)


class ManagerApprovalTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite+pysqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.admin = User(username='admin-synthetic', password_hash='hash', role='admin', active=True)
        self.secretary = User(username='secretary-synthetic', password_hash='hash', role='secretary', active=True)
        self.other_secretary = User(username='other-secretary', password_hash='hash', role='secretary', active=True)
        self.viewer = User(username='viewer-synthetic', password_hash='hash', role='viewer', active=True)
        self.manager = Employee(name='مدیر نمونه', device_id='MAN-001', attendance_group='management')
        self.manager_two = Employee(name='مدیر دوم', device_id='MAN-002', attendance_group='management')
        self.manager_three = Employee(name='مدیر سوم', device_id='MAN-003', attendance_group='management')
        self.manager_four = Employee(name='مدیر چهارم', device_id='MAN-004', attendance_group='management')
        self.manager_five = Employee(name='مدیر پنجم', device_id='MAN-005', attendance_group='management')
        self.staff = Employee(name='کارمند معمولی', device_id='101', attendance_group='staff')
        self.db.add_all([self.admin, self.secretary, self.other_secretary,
                         self.viewer, self.manager, self.manager_two, self.manager_three,
                         self.manager_four, self.manager_five,
                         self.staff])
        self.db.commit()
        self.day = '1405/08/24'
        self.iso_day = '2026-11-15'

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def request(self, who=None, *, csrf='valid-csrf', path='/managers/attendance/submit'):
        who = who or self.secretary
        return Request({
            'type': 'http', 'method': 'POST', 'scheme': 'https',
            'path': path, 'query_string': b'', 'headers': [],
            'client': ('127.0.0.1', 1234), 'server': ('localhost', 8000),
            'session': {'uid': who.id, 'auth_tag': session_auth_tag(who), 'csrf': csrf},
        })

    def submit(self, *, who=None, employee=None, day=None, first='08:30', last='17:00',
               reason='ثبت بر اساس اعلام مدیر', revision=0, request=None):
        return submit_manager_attendance(
            request or self.request(who), employee_id=(employee or self.manager).id,
            day_jalali=day or self.day, first_clock=first, last_clock=last,
            reason=reason, expected_revision=revision, csrf_token='valid-csrf', db=self.db,
        )

    def resolve(self, proposal_id, decision='approve', *, who=None,
                reason='بررسی ادمین و تأیید سند'):
        return resolve_manager_attendance(
            proposal_id, self.request(who or self.admin, path='/managers/approvals/1/resolve'),
            decision=decision, reason=reason, csrf_token='valid-csrf', db=self.db,
        )

    def cancel(self, proposal_id, *, who=None, reason='اطلاعات نیاز به اصلاح دارد'):
        return cancel_manager_attendance(
            self.request(who, path='/managers/attendance/cancel'),
            request_id=proposal_id, reason=reason, csrf_token='valid-csrf', db=self.db,
        )

    def rows(self, period='daily'):
        return report_rows(self.db, self.day, self.day, 0, period)

    def reject(self, code, action):
        with self.assertRaises(HTTPException) as caught:
            action()
        self.assertEqual(caught.exception.status_code, code)
        self.db.rollback()

    def current_request(self):
        return self.db.scalar(select(ManagerAttendanceRequest).order_by(ManagerAttendanceRequest.id.desc()))

    def bulk_submit(self, employee_specs, *, who=None, csrf='valid-csrf'):
        """Exercise the real urlencoded HTML form parser with a Starlette request."""
        user = who or self.secretary
        fields = [('csrf_token', csrf)]
        for employee, day, first, last, reason in employee_specs:
            fields += [('employee_ids', str(employee.id)),
                       (f'day_{employee.id}', day),
                       (f'first_{employee.id}', first),
                       (f'last_{employee.id}', last),
                       (f'reason_{employee.id}', reason)]
        payload = urlencode(fields).encode('utf-8')
        async def receive():
            return {'type': 'http.request', 'body': payload, 'more_body': False}
        request = Request({
            'type': 'http', 'method': 'POST', 'scheme': 'https',
            'path': '/managers/attendance/bulk', 'query_string': b'',
            'headers': [(b'content-type', b'application/x-www-form-urlencoded')],
            'client': ('127.0.0.1', 1234), 'server': ('localhost', 8000),
            'session': {'uid': user.id, 'auth_tag': session_auth_tag(user),
                        'csrf': 'valid-csrf'},
        }, receive)
        return asyncio.run(submit_managers_bulk_attendance(request, db=self.db))

    def bulk_specs(self):
        return [(self.manager, self.day, '08:00', '16:00', 'ثبت طبق اعلام مدیر اول'),
                (self.manager_two, self.day, '09:30', '18:15', 'ثبت طبق اعلام مدیر دوم')]

    def upload_excel(self, fixture='manager-excel-150-valid.xlsx', *, who=None,
                     token='valid-csrf'):
        content = (Path(__file__).resolve().parent / 'fixtures' / fixture).read_bytes()
        boundary = 'soroush-attendance-synthetic-test-boundary'
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\n'
                f'{token}\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{fixture}"\r\nContent-Type: '
                f'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n').encode()
        body += content + f'\r\n--{boundary}--\r\n'.encode()
        async def receive():
            return {'type': 'http.request', 'body': body, 'more_body': False}
        user = who or self.secretary
        request = Request({
            'type': 'http', 'method': 'POST', 'scheme': 'https',
            'path': '/managers/attendance/excel-upload', 'query_string': b'',
            'headers': [(b'content-type', f'multipart/form-data; boundary={boundary}'.encode()),
                        (b'content-length', str(len(body)).encode())],
            'client': ('127.0.0.1', 1234), 'server': ('localhost', 8000),
            'session': {'uid': user.id, 'auth_tag': session_auth_tag(user),
                        'csrf': 'valid-csrf'},
        }, receive)
        return asyncio.run(upload_manager_excel(request, db=self.db))

    def confirm_uploaded_excel(self, preview, selected, *, who=None):
        user = who or self.secretary
        found = re.search(r'name="preview_token" value="([^"]+)"', preview.body.decode())
        self.assertIsNotNone(found)
        fields = [('csrf_token', 'valid-csrf'), ('preview_token', unescape(found.group(1)))]
        fields.extend(('selected_rows', str(row)) for row in selected)
        body = urlencode(fields).encode('utf-8')

        async def receive():
            return {'type': 'http.request', 'body': body, 'more_body': False}

        request = Request({
            'type': 'http', 'method': 'POST', 'scheme': 'https',
            'path': '/managers/attendance/excel-commit', 'query_string': b'',
            'headers': [(b'content-type', b'application/x-www-form-urlencoded'),
                        (b'content-length', str(len(body)).encode())],
            'client': ('127.0.0.1', 1234), 'server': ('localhost', 8000),
            'session': {'uid': user.id, 'auth_tag': session_auth_tag(user),
                        'csrf': 'valid-csrf'},
        }, receive)
        return asyncio.run(commit_manager_excel_selection(request, db=self.db))

    def test_submit_is_inert_until_distinct_admin_approval(self):
        response = self.submit()
        self.assertEqual(response.status_code, 303)
        self.assertIn('managers/attendance', response.headers['location'])
        proposal = self.current_request()
        self.assertEqual(proposal.status, 'pending')
        self.assertEqual(proposal.requested_by_id, self.secretary.id)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 0)
        self.assertEqual(self.db.query(ReportPunch).count(), 0)
        self.assertIsNotNone(self.db.scalar(select(Audit).where(Audit.event == 'manager_attendance_request')))

        reviewed = self.resolve(proposal.id)
        self.assertEqual(reviewed.status_code, 303)
        self.db.refresh(proposal)
        self.assertEqual(proposal.status, 'approved')
        self.assertEqual(proposal.resolved_by_id, self.admin.id)
        self.assertIsNotNone(proposal.correction_id)
        self.assertEqual(self.rows()[0][3:8], ['08:30:00', '17:00:00', 0, '08:30', 510])
        self.assertEqual(self.rows('monthly')[0][3:], [1, 0, '08:30', 510])
        self.assertEqual(self.rows('yearly')[0][3:], [1, 0, '08:30', 510])
        self.assertEqual(self.rows('daily_raw')[0][4], '—')
        self.assertEqual(self.db.query(ReportPunch).count(), 0)
        correction = self.db.get(AttendanceCorrection, proposal.correction_id)
        self.assertEqual((correction.origin, correction.actor_id), ('manager_manual', self.admin.id))
        self.assertIsNotNone(self.db.scalar(select(Audit).where(Audit.event == 'manager_attendance_approved')))

    def test_rejected_request_never_changes_report_and_can_be_resubmitted(self):
        self.submit()
        first = self.current_request()
        self.resolve(first.id, 'reject', reason='ورود اعلام شده صحیح نیست')
        self.db.refresh(first)
        self.assertEqual(first.status, 'rejected')
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 0)
        self.submit(first='09:00', last='18:00', reason='ساعت‌های اعلام‌شده اصلاح شدند')
        next_one = self.current_request()
        self.assertNotEqual(next_one.id, first.id)
        self.resolve(next_one.id)
        self.assertEqual(self.rows()[0][3:5], ['09:00:00', '18:00:00'])
        self.reject(409, lambda: self.resolve(first.id))

    def test_changes_to_approved_record_require_new_approval(self):
        self.submit()
        first = self.current_request()
        self.resolve(first.id)
        original = self.rows()[0]
        self.submit(first='07:00', last='19:00', revision=first.correction_id)
        pending = self.current_request()
        self.assertEqual(self.rows()[0], original)
        self.resolve(pending.id)
        self.assertEqual(self.rows()[0][3:5], ['07:00:00', '19:00:00'])
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 2)

    def test_only_owner_may_cancel_pending_request_without_report_changes(self):
        self.submit()
        proposal = self.current_request()
        self.reject(403, lambda: self.cancel(proposal.id, who=self.other_secretary))
        self.assertEqual(self.cancel(proposal.id).status_code, 303)
        self.db.refresh(proposal)
        self.assertEqual(proposal.status, 'cancelled')
        self.assertEqual(proposal.resolved_by_id, self.secretary.id)
        self.assertEqual(self.rows(), [])
        self.reject(409, lambda: self.resolve(proposal.id))
        self.submit()
        self.assertNotEqual(self.current_request().id, proposal.id)

    def test_duplicate_pending_and_concurrent_review_are_blocked(self):
        self.submit()
        proposal = self.current_request()
        self.reject(409, lambda: self.submit())
        self.assertEqual(self.db.query(ManagerAttendanceRequest).count(), 1)
        self.resolve(proposal.id)
        self.reject(409, lambda: self.resolve(proposal.id))
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 1)

    def test_secretary_role_csrf_and_legacy_direct_endpoints_remain_guarded(self):
        self.reject(403, lambda: self.submit(who=self.viewer))
        self.reject(403, lambda: self.submit(request=self.request(csrf='bad')))
        self.reject(403, lambda: manager_approvals_page(self.request(), db=self.db))
        self.assertEqual(managers_attendance_page(self.request(self.admin), db=self.db).status_code, 200)
        self.reject(410, lambda: save_manager_attendance(
            self.request(), employee_id=self.manager.id, day_jalali=self.day,
            first_clock='08:00', last_clock='16:00', reason='دلیل کاملاً معتبر',
            expected_revision=0, csrf_token='valid-csrf', db=self.db))
        self.reject(410, lambda: revoke_manager_attendance(
            self.request(), employee_id=self.manager.id, day_jalali=self.day,
            reason='دلیل معتبر برای لغو', expected_revision=0,
            csrf_token='valid-csrf', db=self.db))
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 0)

    def test_invalid_dates_times_manager_membership_and_stale_revision(self):
        self.reject(404, lambda: self.submit(employee=self.staff))
        self.reject(400, lambda: self.submit(first='17:00', last='08:00'))
        self.reject(400, lambda: self.submit(first='08:00', last='08:00'))
        self.reject(400, lambda: self.submit(first='abc'))
        self.reject(400, lambda: self.submit(reason='کم'))
        self.reject(400, lambda: self.submit(day='1409/01/01'))
        self.manager.status = 'suspended'
        self.db.commit()
        self.reject(404, lambda: self.submit())
        self.manager.status = 'active'
        self.db.commit()
        self.reject(409, lambda: self.submit(revision=999))

    def test_admin_rejects_stale_request_after_correction_change(self):
        self.submit()
        proposal = self.current_request()
        self.db.add(AttendanceCorrection(employee_id=self.manager.id,
            local_day=self.iso_day, action='set',
            override_first=datetime(2026, 11, 15, 4, tzinfo=timezone.utc),
            override_last=datetime(2026, 11, 15, 12, tzinfo=timezone.utc),
            reason='اصلاح مستقیم ادمین', actor_id=self.admin.id))
        self.db.commit()
        self.reject(409, lambda: self.resolve(proposal.id))
        self.db.refresh(proposal)
        self.assertEqual(proposal.status, 'pending')
        self.resolve(proposal.id, decision='reject')
        self.assertEqual(proposal.status, 'rejected')

    def test_approval_refuses_new_device_punch_without_mutating_it(self):
        self.submit()
        proposal = self.current_request()
        upload = Import(filename='synthetic.dat', digest='a'*64, uploader_id=self.admin.id)
        self.db.add(upload)
        self.db.flush()
        report = ReportSet(import_id=upload.id, name='synthetic-source')
        self.db.add(report)
        self.db.flush()
        stamp = datetime.combine(datetime(2026, 11, 15).date(),
                                 time(11, tzinfo=TZ)).astimezone(timezone.utc)
        self.db.add(ReportDay(report_id=report.id, device_id=self.manager.device_id,
                              local_day=self.iso_day))
        self.db.add(ReportPunch(report_id=report.id, device_id=self.manager.device_id,
                                local_day=self.iso_day, occurred_at=stamp))
        self.db.commit()
        self.reject(409, lambda: self.resolve(proposal.id))
        self.assertEqual(self.db.query(ReportPunch).count(), 1)
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 0)

    def test_approval_is_two_different_accounts_even_after_role_change(self):
        self.submit()
        proposal = self.current_request()
        self.secretary.role = 'admin'
        self.db.commit()
        self.reject(403, lambda: self.resolve(proposal.id, who=self.secretary))

    def test_secretary_and_admin_pages_show_correct_queue_without_public_write(self):
        self.submit()
        secretary_html = managers_attendance_page(self.request(),
                                                  employee_id=self.manager.id,
                                                  day_jalali=self.day, db=self.db).body.decode()
        self.assertIn('در انتظار تأیید ادمین', secretary_html)
        self.assertIn('action="/managers/attendance/cancel"', secretary_html)
        self.assertNotIn('action="/managers/attendance/save"', secretary_html)
        self.assertNotIn('کارمند معمولی', secretary_html)
        admin_html = manager_approvals_page(self.request(self.admin), db=self.db).body.decode()
        self.assertIn('صندوق تأیید', admin_html)
        self.assertIn('value="approve"', admin_html)
        self.assertIn('value="reject"', admin_html)
        self.assertIn('مدیر نمونه', admin_html)
        self.assertEqual(dashboard(self.request(), db=self.db).headers['location'],
                         '/managers/attendance')

    def test_bulk_creates_independent_pending_requests_without_report_mutation(self):
        response = self.bulk_submit(self.bulk_specs())
        self.assertEqual(response.status_code, 303)
        self.assertIn('/managers/attendance/bulk', response.headers['location'])
        requests = self.db.scalars(select(ManagerAttendanceRequest).order_by(
            ManagerAttendanceRequest.employee_id)).all()
        self.assertEqual(len(requests), 2)
        self.assertEqual({p.status for p in requests}, {'pending'})
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 0)
        self.assertEqual(self.rows(), [])
        self.assertEqual({p.requested_by_id for p in requests}, {self.secretary.id})
        self.resolve(requests[0].id)
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.db.get(ManagerAttendanceRequest, requests[1].id).status, 'pending')

    def test_bulk_rejects_any_bad_row_without_partial_requests(self):
        specs = self.bulk_specs()
        specs[1] = (self.manager_two, self.day, '17:00', '08:00', 'ساعت اشتباه')
        self.reject(400, lambda: self.bulk_submit(specs))
        self.assertEqual(self.db.query(ManagerAttendanceRequest).count(), 0)
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 0)
        specs = self.bulk_specs()
        specs[1] = (self.staff, self.day, '08:00', '16:00', 'کارمند خارج از گروه')
        self.reject(404, lambda: self.bulk_submit(specs))
        self.assertEqual(self.db.query(ManagerAttendanceRequest).count(), 0)

    def test_bulk_conflicts_and_role_csrf_and_duplicate_selection(self):
        self.submit(employee=self.manager_two)
        self.reject(409, lambda: self.bulk_submit(self.bulk_specs()))
        self.assertEqual(self.db.query(ManagerAttendanceRequest).count(), 1)
        self.reject(409, lambda: self.bulk_submit(self.bulk_specs(), who=self.admin))
        self.reject(403, lambda: self.bulk_submit(self.bulk_specs(), csrf='invalid'))
        duplicated = [self.bulk_specs()[0], self.bulk_specs()[0]]
        self.reject(400, lambda: self.bulk_submit(duplicated))
        self.assertEqual(self.db.query(ManagerAttendanceRequest).count(), 1)

    def test_bulk_page_allows_secretary_and_admin_but_excludes_normal_staff(self):
        html = managers_bulk_attendance_page(self.request(), db=self.db).body.decode('utf-8')
        self.assertIn('action="/managers/attendance/bulk"', html)
        self.assertIn('مدیر نمونه', html)
        self.assertIn('مدیر دوم', html)
        self.assertNotIn('کارمند معمولی', html)
        self.assertIn('name="employee_ids"', html)
        admin_html = managers_bulk_attendance_page(self.request(self.admin), db=self.db).body.decode()
        self.assertIn('ثبت مستقیم', admin_html)
        self.assertNotIn('کارمند معمولی', admin_html)

    def test_excel_150_manager_days_all_pending_until_separate_admin_approval(self):
        preview = self.upload_excel()
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(self.db.query(ManagerAttendanceRequest).count(), 0)
        response = self.confirm_uploaded_excel(preview, range(2, 152))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.db.query(ManagerAttendanceRequest).count(), 150)
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 0)
        self.assertEqual(self.db.query(ReportPunch).count(), 0)
        self.assertEqual({p.status for p in self.db.query(ManagerAttendanceRequest).all()}, {'pending'})
        self.assertEqual(len({p.employee_id for p in self.db.query(ManagerAttendanceRequest).all()}), 5)
        self.assertEqual(self.db.query(Audit).filter(Audit.event == 'manager_attendance_excel_row').count(), 150)
        sample = self.current_request()
        self.resolve(sample.id)
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 1)
        self.assertEqual(self.db.query(ManagerAttendanceRequest).filter_by(status='pending').count(), 149)

    def test_excel_duplicate_unknown_and_clock_conflict_leave_database_unchanged(self):
        self.reject(400, lambda: self.upload_excel('manager-excel-duplicate.xlsx'))
        self.reject(400, lambda: self.upload_excel('manager-excel-unknown.xlsx'))
        self.reject(400, lambda: self.upload_excel('manager-excel-invalid-clock.xlsx'))
        self.assertEqual(self.db.query(ManagerAttendanceRequest).count(), 0)
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 0)

    def test_excel_upload_rejects_role_csrf_and_conflicts_atomically(self):
        self.reject(403, lambda: self.upload_excel(who=self.viewer))
        self.reject(403, lambda: self.upload_excel(token='invalid'))
        self.submit(employee=self.manager_two, day='1405/07/01')
        secretary_preview = self.upload_excel()
        admin_preview = self.upload_excel(who=self.admin)
        self.assertEqual(secretary_preview.status_code, 200)
        self.assertIn('در انتظار تأیید ادمین', secretary_preview.body.decode())
        self.reject(409, lambda: self.confirm_uploaded_excel(
            secretary_preview, range(2, 152)))
        self.reject(409, lambda: self.confirm_uploaded_excel(
            admin_preview, range(2, 152), who=self.admin))
        self.assertEqual(self.db.query(ManagerAttendanceRequest).count(), 1)
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 0)

    def test_excel_template_is_restricted_to_secretary_and_admin(self):
        response = download_manager_excel_template(self.request(), db=self.db)
        self.assertEqual(response.filename, 'manager-attendance-template.xlsx')
        self.assertEqual(download_manager_excel_template(
            self.request(self.admin), db=self.db).status_code, 200)
        self.reject(403, lambda: download_manager_excel_template(
            self.request(self.viewer), db=self.db))
        html = managers_bulk_attendance_page(self.request(), db=self.db).body.decode()
        self.assertIn('action="/managers/attendance/excel-upload"', html)
        self.assertIn('href="/managers/attendance/excel-template"', html)


if __name__ == '__main__':
    unittest.main()
