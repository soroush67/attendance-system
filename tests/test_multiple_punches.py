"""Regression: the new first/last daily view and historical observed pairs.

Run against a throwaway, in-memory SQLite database inside the application image.
No production PostgreSQL credentials, connection or migration are required.
"""

import asyncio
import io
import unittest
from datetime import date, datetime, timezone

from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from main import (Base, Employee, Import, ReportDay, ReportPunch, ReportSet,
                  User, download, observed_duration_minutes,
                  pair_observed_punches, report_rows, reports, session_auth_tag,
                  to_jalali)


class MultiplePunchTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite+pysqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.day = date(2026, 11, 1)
        self.local_day = self.day.isoformat()
        self.day_text = to_jalali(self.day)
        self.device_id = '987654321'
        employee = Employee(device_id=self.device_id, name='کارمند آزمایشی', status='active')
        self.admin = User(username='test-admin', password_hash='test-only', role='admin', active=True)
        self.db.add_all([employee, self.admin])
        self.db.flush()
        self.report_id = self.create_report('first', 'a' * 64)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def create_report(self, name, digest):
        imported = Import(filename=f'{name}.dat', digest=digest, uploader_id=self.admin.id, accepted=4, rejected=0)
        self.db.add(imported)
        self.db.flush()
        report_set = ReportSet(import_id=imported.id, name=name)
        self.db.add(report_set)
        self.db.flush()
        self.db.add(ReportDay(report_id=report_set.id, device_id=self.device_id, local_day=self.local_day))
        self.db.flush()
        return report_set.id

    def add_punches(self, hours, report_id=None):
        for hour in hours:
            hour_part, minute_part = (int(piece) for piece in hour.split(':'))
            stamp = datetime(2026, 11, 1, hour_part, minute_part, tzinfo=timezone.utc)
            self.db.add(ReportPunch(report_id=report_id or self.report_id,
                                    device_id=self.device_id, local_day=self.local_day, occurred_at=stamp))
        self.db.commit()

    def rows(self, period='daily', report_id=0):
        return report_rows(self.db, self.day_text, self.day_text, report_id, period)

    def test_four_punches_become_two_separate_diagnostic_pairs(self):
        self.add_punches(['07:30', '11:30', '12:30', '16:45'])
        rows = self.rows('daily_pairs')
        self.assertEqual(len(rows), 2)
        self.assertEqual([row[3] for row in rows], [1, 2])
        self.assertEqual([row[0] for row in rows], ['کارمند آزمایشی'] * 2)
        self.assertNotEqual(rows[0][4], rows[1][4])
        self.assertNotEqual(rows[0][5], rows[1][5])
        self.assertEqual([row[6] for row in rows], [240, 255])

    def test_ten_punches_show_all_five_sessions_and_minutes(self):
        self.add_punches([
            '06:00', '08:00', '09:00', '09:30', '10:00',
            '11:15', '12:00', '13:30', '14:00', '14:45',
        ])
        rows = self.rows('daily_pairs')
        self.assertEqual(len(rows), 5)
        self.assertEqual([row[3] for row in rows], [1, 2, 3, 4, 5])
        self.assertEqual([row[6] for row in rows], [120, 30, 75, 90, 45])

    def test_odd_number_preserves_unpaired_event(self):
        self.add_punches(['07:30', '11:30', '12:30'])
        rows = self.rows('daily_pairs')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][3], 2)
        self.assertEqual(rows[1][5], '—')
        self.assertEqual(rows[1][6], '—')

    def test_raw_mode_shows_each_timestamp(self):
        self.add_punches(['07:30', '11:30', '12:30', '16:45'])
        rows = self.rows('daily_raw')
        self.assertEqual(len(rows), 4)
        self.assertEqual([row[3] for row in rows], [1, 2, 3, 4])
        self.assertEqual(len({row[4] for row in rows}), 4)

    def test_different_report_sets_do_not_double_count_same_timestamp(self):
        hours = ['07:30', '11:30', '12:30', '16:45']
        self.add_punches(hours)
        second_id = self.create_report('second', 'b' * 64)
        self.add_punches(hours, report_id=second_id)
        self.assertEqual(len(self.rows('daily')), 1)
        self.assertEqual(len(self.rows('daily_pairs')), 2)
        self.assertEqual(len(self.rows('daily_raw')), 4)
        self.assertEqual(len(self.rows('daily', report_id=second_id)), 1)
        self.assertEqual(self.rows('daily')[0][-1], 555)

    def test_monthly_and_yearly_count_days_not_shifts(self):
        self.add_punches(['07:30', '11:30', '12:30', '16:45'])
        for period in ('monthly', 'yearly', 'cycle', 'custom'):
            with self.subTest(period=period):
                rows = self.rows(period)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][3], 1)
                self.assertEqual(rows[0][4], 0)
                self.assertEqual(rows[0][5], '09:15')
                self.assertEqual(rows[0][6], 555)

    def test_excel_download_includes_day_first_last_and_numeric_minutes(self):
        self.add_punches(['07:30', '11:30', '12:30', '16:45'])
        scope = {
            'type': 'http', 'method': 'GET', 'scheme': 'http',
            'path': '/reports/download', 'query_string': b'', 'headers': [],
            'client': ('127.0.0.1', 54545), 'server': ('localhost', 8000),
            'session': {'uid': self.admin.id, 'auth_tag': session_auth_tag(self.admin)},
        }
        response = download(Request(scope), self.day_text, self.day_text, period='daily', db=self.db)

        async def collect():
            return b''.join([part async for part in response.body_iterator])

        wb = load_workbook(io.BytesIO(asyncio.run(collect())), data_only=True)
        try:
            records = list(wb.active.values)
            self.assertEqual(len(records), 2)  # header + one observed day
            self.assertEqual(records[1][1], self.device_id)
            self.assertEqual(records[1][5], 4)
            self.assertEqual(records[1][6], '09:15')
            self.assertEqual(records[1][7], 555)
            self.assertEqual(records[0][5], 'تعداد ثبت دستگاه')
            self.assertEqual(wb.active.auto_filter.ref, 'A1:H2')
        finally:
            wb.close()

    def test_html_includes_all_rows_sort_buttons_and_nonce(self):
        self.add_punches(['07:30', '11:30', '12:30', '16:45'])
        scope = {
            'type': 'http', 'method': 'GET', 'scheme': 'http',
            'path': '/reports', 'query_string': b'', 'headers': [],
            'client': ('127.0.0.1', 54545), 'server': ('localhost', 8000),
            'session': {'uid': self.admin.id, 'auth_tag': session_auth_tag(self.admin)},
        }
        response = reports(Request(scope), start_jalali=self.day_text,
                           end_jalali=self.day_text, db=self.db)
        html = response.body.decode('utf-8')
        self.assertIn('id="attendance-report-table"', html)
        self.assertIn('مدت حضور ثبت‌شده', html)
        self.assertIn('تعداد ثبت دستگاه', html)
        self.assertIn('<th scope="col">عملیات</th>', html)
        self.assertIn('data-sort-type="number"', html)
        self.assertIn('data-sort-type="duration"', html)
        self.assertIn('Shift', html)
        self.assertIn('document.getElementById(\'attendance-report-table\')', html)
        self.assertIn('<script nonce="', html)
        table_body = html.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
        self.assertEqual(table_body.count('<tr>'), 1)
        employee_cell, actions_cell = table_body.split('</td>', 1)[0], table_body.split('class="report-actions-cell"', 1)[1]
        self.assertNotIn('/changes?', employee_cell)
        self.assertIn('/changes?', actions_cell)
        self.assertIn('/corrections?', actions_cell)
        self.assertEqual(table_body.count('<td'), 9)

    def test_viewer_cannot_see_administrative_row_actions(self):
        self.add_punches(['07:30', '11:30'])
        viewer = User(username='viewer-test', password_hash='test-only', role='viewer', active=True)
        self.db.add(viewer)
        self.db.commit()
        scope = {
            'type': 'http', 'method': 'GET', 'scheme': 'http',
            'path': '/reports', 'query_string': b'', 'headers': [],
            'client': ('127.0.0.1', 54545), 'server': ('localhost', 8000),
            'session': {'uid': viewer.id, 'auth_tag': session_auth_tag(viewer)},
        }
        response = reports(Request(scope), start_jalali=self.day_text,
                           end_jalali=self.day_text, db=self.db)
        html = response.body.decode('utf-8')
        self.assertNotIn('class="report-actions-cell"', html)
        self.assertNotIn('<th scope="col">عملیات</th>', html)
        self.assertNotIn('/changes?employee_id=', html)
        table_body = html.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
        self.assertEqual(table_body.count('<td'), 8)

    def test_first_last_does_not_pair_bounce_swipes(self):
        self.add_punches(['07:30', '07:31', '13:29', '13:30'])
        day = self.rows('daily')[0]
        self.assertEqual(day[5], 4)
        self.assertEqual(day[6], '06:00')
        self.assertEqual(day[7], 360)

    def test_fractional_minute_and_unpaired_duration(self):
        first = datetime(2026, 11, 1, 8, 0, 0, tzinfo=timezone.utc)
        second = datetime(2026, 11, 1, 8, 1, 30, tzinfo=timezone.utc)
        self.assertEqual(observed_duration_minutes(first, second), 1.5)
        self.assertEqual(observed_duration_minutes(first, None), '—')

    def test_pairing_helper_deduplicates_unsorted_times(self):
        stamps = [datetime(2026, 11, 1, 12), datetime(2026, 11, 1, 8),
                  datetime(2026, 11, 1, 8), datetime(2026, 11, 1, 16)]
        self.assertEqual(len(pair_observed_punches(stamps)), 2)
        self.assertIsNone(pair_observed_punches(stamps)[-1][1])


if __name__ == '__main__':
    unittest.main()
