"""Isolated v1.8.7 changes-workbench tests; all database writes are in-memory."""

import os
import unittest
from datetime import datetime, timedelta, time, timezone

# Never let a test-runner process build a real Production DB connection.
os.environ['DATABASE_URL'] = 'sqlite+pysqlite:///:memory:'
os.environ['SESSION_SECRET'] = 'v187-tests-only-not-a-deployment-secret'

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from main import (
    Audit, AttendanceCorrection, Base, Employee, Import, Log, ReportDay,
    ReportPunch, ReportSet, TZ, User, changes_page, edit_employee,
    report_rows, revoke_correction, save_correction, session_auth_tag,
    status_employee, to_jalali,
)


class ChangesWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite+pysqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.admin = User(username='admin-stable', role='admin', password_hash='fake-test-hash', active=True)
        self.viewer = User(username='readonly-user', role='viewer', password_hash='fake-test-hash', active=True)
        self.employee = Employee(name='کارمند اولیه', device_id='1005', status='active')
        self.db.add_all([self.admin, self.viewer, self.employee])
        self.db.flush()
        self.day = (datetime.now(TZ).date() - timedelta(days=2))
        self.day_jalali = to_jalali(self.day)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def request(self, who=None, method='GET', path='/changes', token='token-187'):
        who = who or self.admin
        return Request({
            'type': 'http', 'method': method, 'scheme': 'https', 'path': path,
            'query_string': b'', 'headers': [],
            'client': ('127.0.0.1', 4567), 'server': ('localhost', 8000),
            'session': {'uid': who.id, 'auth_tag': session_auth_tag(who), 'csrf': token},
        })

    def seed_device_history(self):
        upload = Import(filename='synthetic.dat', digest='c' * 64, uploader_id=self.admin.id)
        self.db.add(upload)
        self.db.flush()
        report = ReportSet(name='تست مصنوعی', import_id=upload.id)
        self.db.add(report)
        self.db.flush()
        first = datetime.combine(self.day, time(8, 0, tzinfo=TZ)).astimezone(timezone.utc)
        last = datetime.combine(self.day, time(16, 0, tzinfo=TZ)).astimezone(timezone.utc)
        self.db.add(ReportDay(report_id=report.id, device_id=self.employee.device_id,
                              local_day=self.day.isoformat()))
        self.db.add_all([
            ReportPunch(report_id=report.id, device_id=self.employee.device_id,
                        local_day=self.day.isoformat(), occurred_at=stamp)
            for stamp in (first, last)
        ])
        self.db.commit()

    def test_admin_only_page_and_no_writes_on_get(self):
        preview = changes_page(self.request(), employee_id=self.employee.id,
                               day_jalali=self.day_jalali, db=self.db)
        html = preview.body.decode('utf-8')
        for label in ('اعمال تغییرات', 'ثبت تغییرات مشخصات', 'ثبت اصلاح و اعمال در گزارش‌ها',
                      'نام کاربری حساب ورود تغییر نمی‌کند', 'expected_revision',
                      'expected_device_id', 'expected_status', 'csrf_token'):
            self.assertIn(label, html)
        self.assertIn('href="/changes"', html)
        self.assertEqual(self.db.query(Audit).count(), 0)
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 0)
        with self.assertRaises(HTTPException) as denied:
            changes_page(self.request(who=self.viewer), db=self.db)
        self.assertEqual(denied.exception.status_code, 403)
        anon = self.request()
        anon.scope['session'] = {}
        with self.assertRaises(HTTPException) as denied:
            changes_page(anon, db=self.db)
        self.assertEqual(denied.exception.status_code, 401)

    def test_edits_profile_status_with_audited_reason_but_not_username(self):
        response = edit_employee(
            self.employee.id, self.request(method='POST'), name='کارمند ویرایش‌شده',
            device_id='1005', csrf_token='token-187', db=self.db,
            from_changes='1', day_jalali=self.day_jalali,
            change_reason='اصلاح نام طبق پرونده', expected_name='کارمند اولیه',
            expected_device_id='1005',
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn('/changes?', response.headers['location'])
        self.assertEqual(self.db.get(Employee, self.employee.id).name, 'کارمند ویرایش‌شده')
        response = status_employee(
            self.employee.id, self.request(method='POST'), status='suspended',
            csrf_token='token-187', db=self.db, from_changes='1',
            day_jalali=self.day_jalali, change_reason='مرخصی طولانی',
            expected_status='active',
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.db.get(Employee, self.employee.id).status, 'suspended')
        self.assertEqual(self.db.get(User, self.admin.id).username, 'admin-stable')
        events = self.db.scalars(select(Audit.event).order_by(Audit.id)).all()
        self.assertEqual(events, ['employee_edit', 'employee_status'])
        details = self.db.scalars(select(Audit.details).order_by(Audit.id)).all()
        self.assertIn('اصلاح نام طبق پرونده', details[0])
        self.assertIn('مرخصی طولانی', details[1])

    def test_stale_change_csrf_bad_reason_and_direct_delete_are_rejected(self):
        kw = dict(
            employee_id=self.employee.id, request=self.request(method='POST'),
            name='نام جدید', device_id='1005', csrf_token='token-187', db=self.db,
            from_changes='1', change_reason='ویرایش معتبر',
            expected_name='نامی که قبلاً بوده', expected_device_id='1005',
        )
        with self.assertRaises(HTTPException) as denied:
            edit_employee(**kw)
        self.assertEqual(denied.exception.status_code, 409)
        self.db.rollback()
        kw.update(expected_name='کارمند اولیه', csrf_token='wrong')
        with self.assertRaises(HTTPException) as denied:
            edit_employee(**kw)
        self.assertEqual(denied.exception.status_code, 403)
        self.db.rollback()
        kw.update(csrf_token='token-187', change_reason='bad')
        with self.assertRaises(HTTPException) as denied:
            edit_employee(**kw)
        self.assertEqual(denied.exception.status_code, 400)
        self.db.rollback()
        with self.assertRaises(HTTPException) as denied:
            status_employee(self.employee.id, self.request(method='POST'), 'deleted',
                            'token-187', self.db, from_changes='1',
                            change_reason='درخواست حذف', expected_status='active')
        self.assertEqual(denied.exception.status_code, 400)
        self.assertEqual(self.db.get(Employee, self.employee.id).name, 'کارمند اولیه')

    def test_device_with_existing_report_cannot_be_renumbered(self):
        self.seed_device_history()
        page_html = changes_page(self.request(), employee_id=self.employee.id,
                                 day_jalali=self.day_jalali, db=self.db).body.decode('utf-8')
        self.assertIn('readonly class="change-locked"', page_html)
        response = edit_employee(
            self.employee.id, self.request(method='POST'), name='کارمند اولیه',
            device_id='9999', csrf_token='token-187', db=self.db,
            from_changes='1', change_reason='تغییر شماره دستگاه',
            expected_name='کارمند اولیه', expected_device_id='1005',
            day_jalali=self.day_jalali,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.db.get(Employee, self.employee.id).device_id, '1005')
        self.assertEqual(self.db.query(ReportPunch).count(), 2)
        self.assertEqual(self.db.query(Audit).count(), 0)

    def test_correction_from_workbench_redirects_back_and_keeps_raw_events(self):
        self.seed_device_history()
        response = save_correction(
            self.request(method='POST', path='/corrections/save'),
            employee_id=self.employee.id, day_jalali=self.day_jalali,
            first_clock='07:30', last_clock='', reason='اصلاح ورود با تایید مدیر',
            expected_revision=0, csrf_token='token-187', db=self.db,
            from_changes='1',
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn('/changes?', response.headers['location'])
        self.assertEqual(self.db.query(ReportPunch).count(), 2)
        self.assertEqual(report_rows(self.db, self.day_jalali, self.day_jalali,
                                     0, 'daily', self.employee.id)[0][3], '07:30:00')
        revision = self.db.scalar(select(AttendanceCorrection))
        html = changes_page(self.request(), employee_id=self.employee.id,
                            day_jalali=self.day_jalali, db=self.db).body.decode('utf-8')
        self.assertIn('لغو اصلاح و بازگشت به ثبت خام', html)
        self.assertIn('اصلاح ورود با تایید مدیر', html)
        response = revoke_correction(
            self.request(method='POST', path='/corrections/revoke'),
            employee_id=self.employee.id, day_jalali=self.day_jalali,
            reason='لغو اصلاح پس از بررسی', expected_revision=revision.id,
            csrf_token='token-187', db=self.db, from_changes='1',
        )
        self.assertIn('/changes?', response.headers['location'])
        self.assertEqual(report_rows(self.db, self.day_jalali, self.day_jalali,
                                     0, 'daily', self.employee.id)[0][3], '08:00:00')
        self.assertEqual(self.db.query(AttendanceCorrection).count(), 2)


if __name__ == '__main__':
    unittest.main()
