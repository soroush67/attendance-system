"""End-to-end correction logic against throwaway SQLite; no Production access."""

import unittest
from datetime import datetime, time, timezone

import jdatetime
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from main import (
    Audit, AttendanceCorrection, Base, Employee, Import, Log, ReportDay,
    ReportPunch, ReportSet, TZ, User, corrections_page, report_rows,
    revoke_correction, save_correction, reports, session_auth_tag,
)


class AttendanceCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite+pysqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.admin = User(username='admin-one', password_hash='testing', role='admin', active=True)
        self.viewer = User(username='viewer-one', password_hash='testing', role='viewer', active=True)
        self.employee = Employee(device_id='101', name='نام یکسان')
        self.other = Employee(device_id='102', name='نام یکسان')
        self.db.add_all([self.admin, self.viewer, self.employee, self.other])
        self.db.flush()
        imported = Import(filename='example.dat', digest='d' * 64, uploader_id=self.admin.id)
        self.db.add(imported)
        self.db.flush()
        report = ReportSet(name='نمونه اول', import_id=imported.id)
        self.db.add(report)
        self.db.flush()
        self.report_id = report.id
        self.day = '1405/08/25'
        self.other_day = '1405/08/24'
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def iso(self, day):
        return jdatetime.date(*[int(x) for x in day.split('/')]).togregorian().isoformat()

    def punch(self, local_day, clock):
        y, m, d = map(int, self.iso(local_day).split('-'))
        h, minute = map(int, clock.split(':'))
        return datetime.combine(datetime(y, m, d).date(),
                                time(h, minute, tzinfo=TZ)).astimezone(timezone.utc)

    def observed(self, day, times, employee=None):
        employee = employee or self.employee
        iso = self.iso(day)
        self.db.add(ReportDay(report_id=self.report_id, device_id=employee.device_id, local_day=iso))
        for value in times:
            stamp = self.punch(day, value)
            self.db.add(ReportPunch(report_id=self.report_id, device_id=employee.device_id,
                                    local_day=iso, occurred_at=stamp))
        self.db.commit()

    def request(self, who=None, csrf_value='token123', path='/corrections/save'):
        who = self.admin if who is None else who
        return Request({
            'type': 'http', 'method': 'POST', 'scheme': 'https',
            'path': path, 'query_string': b'', 'headers': [],
            'client': ('127.0.0.1', 22222), 'server': ('localhost', 8000),
            'session': {'uid': who.id, 'auth_tag': session_auth_tag(who), 'csrf': csrf_value},
        })

    def save(self, first='', last='', reason='فراموشی ثبت ورود', day=None,
             employee=None, revision=0, request=None):
        return save_correction(
            request or self.request(), employee_id=(employee or self.employee).id,
            day_jalali=day or self.day, first_clock=first, last_clock=last,
            reason=reason, expected_revision=revision,
            csrf_token='token123', db=self.db,
        )

    def rows(self, day=None, period='daily', report=0, employee=0, end=None):
        return report_rows(self.db, day or self.day, end or day or self.day,
                           report, period, employee)

    def test_missing_entry_corrects_daily_monthly_yearly_and_excel_source(self):
        self.observed(self.day, ['16:00'])
        self.assertEqual(self.rows()[0][7], '—')
        before_raw = self.rows(period='daily_raw')
        response = self.save(first='08:00')
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.rows()[0][3:8], ['08:00:00', '16:00:00', 1, '08:00', 480])
        for period in ('monthly', 'yearly', 'cycle', 'custom'):
            self.assertEqual(self.rows(period=period)[0][3:], [1, 0, '08:00', 480])
        self.assertEqual(self.rows(period='daily_raw'), before_raw)
        self.assertEqual(self.rows(period='daily_pairs')[0][5:], ['—', '—'])
        self.assertEqual(self.db.scalar(select(ReportPunch).where(ReportPunch.device_id == '101')).occurred_at,
                         self.punch(self.day, '16:00').replace(tzinfo=None))
        revision = self.db.scalar(select(AttendanceCorrection))
        self.assertEqual(revision.action, 'set')
        self.assertEqual(revision.actor_id, self.admin.id)
        self.assertEqual(revision.reason, 'فراموشی ثبت ورود')
        self.assertEqual(self.db.scalar(select(Audit).where(Audit.event == 'attendance_correction_set')).actor,
                         self.admin.username)

    def test_missing_exit_and_repeat_edits_keep_every_revision_then_revoke(self):
        self.observed(self.day, ['08:00'])
        self.save(last='16:00')
        self.assertEqual(self.rows()[0][6:], ['08:00', 480])
        revision = self.db.scalar(select(AttendanceCorrection).order_by(AttendanceCorrection.id.desc()))
        self.save(last='17:30', revision=revision.id, reason='خروج واقعی بعدتر ثبت شده')
        self.assertEqual(self.rows()[0][6:], ['09:30', 570])
        revision = self.db.scalar(select(AttendanceCorrection).order_by(AttendanceCorrection.id.desc()))
        response = revoke_correction(self.request(path='/corrections/revoke'),
                                     self.employee.id, self.day, 'لغو بر اساس بررسی جدید',
                                     revision.id, 'token123', self.db)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.rows()[0][6:], ['—', '—'])
        revisions = self.db.scalars(select(AttendanceCorrection).order_by(AttendanceCorrection.id)).all()
        self.assertEqual([r.action for r in revisions], ['set', 'set', 'revoke'])

    def test_manual_only_day_is_visible_only_in_all_reports_and_reversible(self):
        self.save(first='08:00', last='16:30', day=self.other_day)
        self.assertEqual(self.rows(day=self.other_day)[0][3:8],
                         ['08:00:00', '16:30:00', 0, '08:30', 510])
        self.assertEqual(self.rows(day=self.other_day, report=self.report_id), [])
        self.assertEqual(self.rows(day=self.other_day, period='monthly')[0][3:],
                         [1, 0, '08:30', 510])
        revision = self.db.scalar(select(AttendanceCorrection))
        revoke_correction(self.request(path='/corrections/revoke'),
                          self.employee.id, self.other_day, 'اصلاح اشتباه ثبت شده بود',
                          revision.id, 'token123', self.db)
        self.assertEqual(self.rows(day=self.other_day), [])

    def test_filtered_reports_only_count_selected_employee_and_source(self):
        self.observed(self.day, ['16:00'])
        self.observed(self.day, ['08:00', '15:00'], employee=self.other)
        self.save(first='08:00')
        self.assertEqual(self.rows(period='monthly', employee=self.employee.id)[0][-1], 480)
        self.assertEqual(self.rows(period='monthly', employee=self.other.id)[0][-1], 420)
        self.assertEqual({r[1]: r[7] for r in self.rows(report=self.report_id)},
                         {'101': 480, '102': 420})

    def test_rejects_invalid_ranges_unbounded_or_missing_times(self):
        self.observed(self.day, ['16:00'])
        for first, last in [('17:00', ''), ('25:00', ''), ('16:00', ''), ('', ''), ('8am', '')]:
            with self.subTest(first=first, last=last):
                with self.assertRaises(HTTPException) as raised:
                    self.save(first=first, last=last)
                self.assertEqual(raised.exception.status_code, 400)
                self.db.rollback()
        with self.assertRaises(HTTPException):
            self.save(first='08:00', reason='abc')
        with self.assertRaises(HTTPException):
            self.save(first='08:00', day='1409/01/01')
        with self.assertRaises(HTTPException):
            self.save(first='08:00', day=self.other_day)

    def test_csrf_rbac_and_optimistic_version_gate(self):
        self.observed(self.day, ['16:00'])
        with self.assertRaises(HTTPException) as denied:
            self.save(first='08:00', request=self.request(who=self.viewer))
        self.assertEqual(denied.exception.status_code, 403)
        with self.assertRaises(HTTPException) as denied:
            self.save(first='08:00', request=self.request(csrf_value='wrong'))
        self.assertEqual(denied.exception.status_code, 403)
        self.save(first='08:00')
        with self.assertRaises(HTTPException) as denied:
            self.save(first='07:00', revision=0)
        self.assertEqual(denied.exception.status_code, 409)
        revision = self.db.scalar(select(AttendanceCorrection))
        with self.assertRaises(HTTPException) as denied:
            revoke_correction(self.request(who=self.viewer), self.employee.id, self.day,
                              'درخواست غیرمجاز برای لغو', revision.id, 'token123', self.db)
        self.assertEqual(denied.exception.status_code, 403)
        with self.assertRaises(HTTPException) as denied:
            revoke_correction(self.request(csrf_value='wrong'), self.employee.id, self.day,
                              'درخواست بدون توکن معتبر', revision.id, 'token123', self.db)
        self.assertEqual(denied.exception.status_code, 403)
        with self.assertRaises(HTTPException) as denied:
            corrections_page(self.request(who=self.viewer), employee_id=self.employee.id,
                             day_jalali=self.day, db=self.db)
        self.assertEqual(denied.exception.status_code, 403)
        self.assertEqual(self.db.scalar(select(AttendanceCorrection).where(
            AttendanceCorrection.action == 'set')).reason, 'فراموشی ثبت ورود')

    def test_html_displays_original_and_corrected_punches_and_actor(self):
        self.observed(self.day, ['16:00'])
        self.save(first='08:00')
        response = corrections_page(self.request(path='/corrections'),
                                    employee_id=self.employee.id, day_jalali=self.day, db=self.db)
        html = response.body.decode('utf-8')
        for fragment in ('اصلاح تردد', '16:00:00', '08:00:00', 'فراموشی ثبت ورود',
                         'admin-one', 'csrf_token', 'expected_revision', 'تاریخچهٔ تغییرات'):
            self.assertIn(fragment, html)
        self.assertNotIn('viewer-one</td>', html)

    def test_daily_report_badge_and_admin_edit_link(self):
        self.observed(self.day, ['16:00'])
        self.save(first='08:00')
        response = reports(self.request(path='/reports'), start_jalali=self.day,
                           end_jalali=self.day, db=self.db)
        html = response.body.decode('utf-8')
        self.assertIn('✎ اصلاح مدیر', html)
        self.assertIn('اصلاح تردد</a>', html)
        self.assertIn('employee_id=' + str(self.employee.id), html)
        self.assertIn('08:00:00', html)


if __name__ == '__main__':
    unittest.main()
