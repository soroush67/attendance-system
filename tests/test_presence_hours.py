"""Daily/monthly/yearly/custom first-to-last presence; isolated SQLite, no production access."""

import asyncio
import io
import unittest
from datetime import datetime, time, timezone

import jdatetime
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from main import (Base, Employee, Import, ReportDay, ReportPunch, ReportSet,
                  User, download, duration_label, observed_day_seconds,
                  report_rows, session_auth_tag, to_jalali)


class PresenceHoursTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite+pysqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.admin = User(username='test-admin', password_hash='test', role='admin', active=True)
        self.emp = Employee(device_id='441094831', name='کارمند همنام')
        self.other = Employee(device_id='222', name='کارمند همنام')
        self.db.add_all([self.admin, self.emp, self.other])
        self.db.flush()
        self.report_a = self.report('report-a', 'a' * 64)
        self.report_b = self.report('report-b', 'b' * 64)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def report(self, name, digest):
        imported = Import(filename=name + '.dat', digest=digest, uploader_id=self.admin.id)
        self.db.add(imported)
        self.db.flush()
        record = ReportSet(name=name, import_id=imported.id)
        self.db.add(record)
        self.db.flush()
        return record.id

    def add_day(self, jalali_day, times, code='441094831', report=None):
        gregorian = jdatetime.date(*map(int, jalali_day.split('/'))).togregorian()
        report_id = report or self.report_a
        self.db.add(ReportDay(report_id=report_id, device_id=code,
                              local_day=gregorian.isoformat()))
        for clock in times:
            hours, minutes, seconds = map(int, clock.split(':'))
            # UTC test events are after 05:00, so their Tehran local date agrees.
            stamp = datetime.combine(gregorian, time(hours, minutes, seconds, tzinfo=timezone.utc))
            self.db.add(ReportPunch(report_id=report_id, device_id=code,
                                    local_day=gregorian.isoformat(), occurred_at=stamp))
        self.db.commit()

    def rows(self, start, end, period, report=0, employee=0, anchor=20):
        return report_rows(self.db, start, end, report, period, employee, anchor)

    def test_realistic_four_swipes_count_six_hours_not_three_seconds(self):
        self.add_day('1405/08/24', ['07:19:16', '07:19:19', '13:18:09', '13:18:11'])
        row = self.rows('1405/08/24', '1405/08/24', 'daily')[0]
        self.assertEqual(row[1], '441094831')
        self.assertEqual(row[5], 4)
        self.assertEqual(row[6], '05:59')
        self.assertEqual(row[7], 359)

    def test_incomplete_day_not_invented_and_aggregates_by_calendar_month(self):
        self.add_day('1405/08/24', ['07:00:00', '15:00:00'])
        self.add_day('1405/08/25', ['07:00:00'])
        self.add_day('1405/09/01', ['08:00:00', '10:30:00'])
        november = self.rows('1405/08/01', '1405/08/30', 'monthly')[0]
        self.assertEqual(november[2:], ['1405/08', 2, 1, '08:00', 480])
        all_months = self.rows('1405/08/01', '1405/09/30', 'monthly')
        self.assertEqual(len(all_months), 2)
        self.assertEqual(sum(row[-1] for row in all_months), 630)
        self.assertEqual(self.rows('1405/08/24', '1405/08/25', 'custom')[0][3:],
                         [2, 1, '08:00', 480])
        self.assertEqual(self.rows('1405/08/25', '1405/08/25', 'daily')[0][6:], ['—', '—'])

    def test_jalali_year_and_anchor_cycle_with_custom_filter(self):
        self.add_day('1405/12/29', ['07:00:00', '15:00:00'])
        self.add_day('1406/01/01', ['07:00:00', '12:00:00'])
        start, end = '1405/12/29', '1406/01/01'
        yearly = self.rows(start, end, 'yearly')
        self.assertEqual({r[2]: r[-1] for r in yearly}, {'1405': 480, '1406': 300})
        monthly = self.rows(start, end, 'monthly')
        self.assertEqual({r[2]: r[-1] for r in monthly}, {'1405/12': 480, '1406/01': 300})
        cycle = self.rows(start, end, 'cycle')
        self.assertEqual(len(cycle), 1)
        self.assertEqual(cycle[0][2], '1405/12/20 تا 1406/01/19')
        self.assertEqual(cycle[0][3:], [2, 0, '13:00', 780])
        self.assertEqual(self.rows('1406/01/01', end, 'custom')[0][-1], 300)

    def test_duplicate_named_uploads_and_same_names_do_not_conflate_people(self):
        times = ['07:00:00', '12:00:00']
        self.add_day('1405/08/24', times, report=self.report_a)
        self.add_day('1405/08/24', times, report=self.report_b)
        self.add_day('1405/08/24', ['08:00:00', '16:00:00'], code='222')
        merged = self.rows('1405/08/24', '1405/08/24', 'monthly')
        self.assertEqual({r[1]: r[-1] for r in merged}, {'441094831': 300, '222': 480})
        chosen = self.rows('1405/08/24', '1405/08/24', 'monthly', employee=self.emp.id)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0][-1], 300)
        only_a = self.rows('1405/08/24', '1405/08/24', 'monthly', report=self.report_a)
        self.assertEqual(len(only_a), 2)

    def test_seconds_sum_then_round_once_and_excel_matches(self):
        self.add_day('1405/08/24', ['07:00:00', '07:00:29'])
        self.add_day('1405/08/25', ['07:00:00', '07:00:29'])
        monthly = self.rows('1405/08/24', '1405/08/25', 'monthly')
        self.assertEqual(monthly[0][3:], [2, 0, '00:01', 1])
        scope = {
            'type': 'http', 'method': 'GET', 'scheme': 'http',
            'path': '/reports/download', 'query_string': b'', 'headers': [],
            'client': ('127.0.0.1', 54545), 'server': ('localhost', 8000),
            'session': {'uid': self.admin.id, 'auth_tag': session_auth_tag(self.admin)},
        }
        response = download(Request(scope), '1405/08/24', '1405/08/25',
                            period='monthly', db=self.db)

        async def collect():
            return b''.join([part async for part in response.body_iterator])

        workbook = load_workbook(io.BytesIO(asyncio.run(collect())), data_only=True)
        try:
            lines = list(workbook.active.values)
            self.assertEqual(lines[1], tuple(monthly[0]))
            self.assertEqual(lines[1][-1], 1)
            self.assertEqual(workbook.active.auto_filter.ref, 'A1:G2')
        finally:
            workbook.close()

    def test_empty_report_day_is_marked_incomplete(self):
        self.add_day('1405/08/24', [])
        value = self.rows('1405/08/24', '1405/08/24', 'monthly')[0]
        self.assertEqual(value[3:], [1, 1, '00:00', 0])

    def test_observed_duration_works_with_non_utc_offsets(self):
        self.assertEqual(observed_day_seconds([]), None)
        self.assertEqual(observed_day_seconds([datetime.now(timezone.utc)]), None)
        self.assertEqual(duration_label(60 * 75 + 35), '01:16')


if __name__ == '__main__':
    unittest.main()
