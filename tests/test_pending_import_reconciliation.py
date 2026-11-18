"""v1.8.8 isolated, in-memory staged DAT reconciliation checks."""

import os
import unittest
from datetime import date, datetime, timezone

os.environ['DATABASE_URL'] = 'sqlite+pysqlite:///:memory:'
os.environ['SESSION_SECRET'] = 'v188-in-memory-tests-only'

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from main import (
    Audit, Base, Employee, Import, Log, PendingImport, PendingImportRow, ReportDay,
    ReportPunch, ReportSet, TZ, User, cancel_staged_post, commit_staged_file,
    commit_staged_post, report_rows, resolve_staged_post, resolve_staged_row,
    session_auth_tag, stage_dat_file, staged_import_page, to_jalali,
)


def line(code, time_text):
    return f'{code}\t{time_text}\t1\t4\t15\t0'


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite+pysqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.admin = User(username='admin-v188', password_hash='test-hash', role='admin', active=True)
        self.operator = User(username='operator-v188', password_hash='test-hash', role='operator', active=True)
        self.viewer = User(username='viewer-v188', password_hash='test-hash', role='viewer', active=True)
        self.employee = Employee(device_id='1005', name='کارمند آزمایشی')
        self.db.add_all((self.admin, self.operator, self.viewer, self.employee))
        self.db.commit()
        self.jdate = to_jalali(date(2026, 11, 1))

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def request(self, user=None, token='csrf-v188', method='POST'):
        user = self.admin if user is None else user
        return Request({
            'type': 'http', 'path': '/imports/staged/1', 'method': method,
            'scheme': 'https', 'headers': [], 'query_string': b'',
            'client': ('127.0.0.1', 4444), 'server': ('localhost', 8000),
            'session': {'uid': user.id, 'auth_tag': session_auth_tag(user),
                        'csrf': 'csrf-v188'},
        })

    def stage(self, contents, report_name='گزارش آزمایشی اول'):
        return stage_dat_file(self.db, self.operator, report_name=report_name,
                              filename='device.dat', data=contents.encode('utf-8'))

    def sample(self):
        return '\n'.join([
            line('1005', '2026-11-01 08:00:00'),
            line('1005', '2026-11-01 16:00:00'),
            line('9999', '2026-11-01 12:00:00'),  # unknown device
            'broken line',                        # malformed row
            line('1005', '2026-11-01 08:00:00'),   # duplicate in the file
        ])

    def test_staging_does_not_change_canonical_records_or_reports(self):
        batch = self.stage(self.sample())
        rows = self.db.scalars(select(PendingImportRow).order_by(PendingImportRow.line_number)).all()
        self.assertEqual([row.issue for row in rows],
                         ['none', 'none', 'unknown-code', 'invalid-format', 'duplicate-file'])
        self.assertEqual([row.decision for row in rows],
                         ['ready', 'ready', 'pending', 'pending', 'pending'])
        self.assertIn('9999\t', rows[2].raw_line)
        self.assertEqual(rows[2].source_device_id, '9999')
        self.assertEqual(batch.status, 'pending')
        for model in (Import, Log, ReportSet, ReportDay, ReportPunch):
            self.assertEqual(self.db.query(model).count(), 0)
        with self.assertRaises(HTTPException) as denied:
            commit_staged_file(self.db, self.admin, batch.id)
        self.assertEqual(denied.exception.status_code, 409)
        self.assertEqual(self.db.query(ReportPunch).count(), 0)

    def test_full_manual_reconciliation_then_atomic_commit_and_report(self):
        batch = self.stage(self.sample())
        rows = self.db.scalars(select(PendingImportRow).order_by(PendingImportRow.line_number)).all()
        resolve_staged_row(self.db, self.admin, batch.id, rows[2].id,
                           'accept', 'بررسی پرونده دستگاه ناشناس', self.employee.id)
        resolve_staged_row(self.db, self.admin, batch.id, rows[3].id,
                           'skip', 'این سطر فرمت مناسبی ندارد')
        resolve_staged_row(self.db, self.admin, batch.id, rows[4].id,
                           'skip', 'سطر تکراری در همین فایل')
        upload, report, dates = commit_staged_file(self.db, self.admin, batch.id)
        self.assertEqual(upload.accepted, 3)
        self.assertEqual(upload.rejected, 2)
        self.assertEqual(len(dates), 3)
        self.assertEqual(self.db.query(Log).count(), 3)
        self.assertEqual(self.db.query(ReportPunch).count(), 3)
        self.assertEqual(self.db.get(PendingImport, batch.id).status, 'committed')
        self.assertEqual(self.db.get(PendingImport, batch.id).committed_import_id, upload.id)
        raw = report_rows(self.db, self.jdate, self.jdate, report.id, 'daily_raw')
        self.assertEqual(len(raw), 3)
        daily = report_rows(self.db, self.jdate, self.jdate, 0, 'daily')
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0][3:5], ['08:00:00', '16:00:00'])
        self.assertEqual(self.db.get(User, self.admin.id).username, 'admin-v188')
        events = self.db.scalars(select(Audit.event)).all()
        self.assertEqual(events.count('file_row_decision'), 3)
        self.assertIn('file_stage', events)
        self.assertIn('file_commit', events)
        with self.assertRaises(HTTPException) as denied:
            commit_staged_file(self.db, self.admin, batch.id)
        self.assertEqual(denied.exception.status_code, 409)

    def test_duplicate_existing_and_invalid_can_only_be_skipped(self):
        prior = Import(filename='previous.dat', digest='a' * 64,
                       uploader_id=self.admin.id, accepted=1, rejected=0)
        self.db.add(prior)
        self.db.flush()
        stamp = datetime(2026, 11, 1, 8, 0, tzinfo=TZ).astimezone(timezone.utc)
        self.db.add(Log(device_id='1005', occurred_at=stamp, source_import_id=prior.id))
        self.db.commit()
        batch = self.stage(line('1005', '2026-11-01 08:00:00'), 'گزارش تکراری')
        row = self.db.scalar(select(PendingImportRow).where(PendingImportRow.pending_import_id == batch.id))
        self.assertEqual(row.issue, 'duplicate-db')
        with self.assertRaises(HTTPException) as denied:
            resolve_staged_row(self.db, self.admin, batch.id, row.id,
                               'accept', 'تأیید این رکورد تکراری', self.employee.id)
        self.assertEqual(denied.exception.status_code, 400)
        self.db.rollback()
        resolve_staged_row(self.db, self.admin, batch.id, row.id,
                           'skip', 'این تردد قبلاً ثبت شده')
        with self.assertRaises(HTTPException) as denied:
            commit_staged_file(self.db, self.admin, batch.id)
        self.assertEqual(denied.exception.status_code, 400)

    def test_detects_concurrent_duplicate_before_commit(self):
        batch = self.stage(line('1005', '2026-11-01 08:00:00'))
        other = Import(filename='other.dat', digest='b' * 64, uploader_id=self.admin.id)
        self.db.add(other)
        self.db.flush()
        stamp = datetime(2026, 11, 1, 8, 0, tzinfo=TZ).astimezone(timezone.utc)
        self.db.add(Log(device_id='1005', occurred_at=stamp, source_import_id=other.id))
        self.db.commit()
        with self.assertRaises(HTTPException) as denied:
            commit_staged_file(self.db, self.admin, batch.id)
        self.assertEqual(denied.exception.status_code, 409)
        self.assertEqual(self.db.get(PendingImport, batch.id).status, 'pending')

    def test_authorization_csrf_and_cancel_are_server_side(self):
        batch = self.stage(self.sample())
        html = staged_import_page(batch.id, self.request(method='GET'), db=self.db).body.decode('utf-8')
        self.assertIn('رفع مغایرت', html)
        self.assertIn('نام گزارش', html)
        self.assertIn('csrf_token', html)
        self.assertIn('9999', html)
        row_id = self.db.scalar(select(PendingImportRow.id).where(PendingImportRow.issue == 'unknown-code'))
        with self.assertRaises(HTTPException) as denied:
            resolve_staged_post(batch.id, self.request(self.viewer), row_id,
                                'skip', 'رد سطر در تست', 0, 'csrf-v188', self.db)
        self.assertEqual(denied.exception.status_code, 403)
        with self.assertRaises(HTTPException) as denied:
            commit_staged_post(batch.id, self.request(), 'wrong-csrf', self.db)
        self.assertEqual(denied.exception.status_code, 403)
        response = cancel_staged_post(batch.id, self.request(), 'csrf-v188', self.db)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.db.get(PendingImport, batch.id).status, 'cancelled')
        self.assertEqual(self.db.query(ReportSet).count(), 0)


if __name__ == '__main__':
    unittest.main()
