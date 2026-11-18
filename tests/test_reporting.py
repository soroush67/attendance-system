"""Pure report-window regression checks, run inside the built Python image."""

import unittest

import jdatetime

from main import jalali_cycle_label, parse_jalali, range_jalali, report_headers


class JalaliReportTests(unittest.TestCase):
    def test_twentieth_to_nineteenth_boundary(self):
        prev_end = jdatetime.date(1405, 8, 19).togregorian()
        next_start = jdatetime.date(1405, 8, 20).togregorian()
        self.assertEqual(jalali_cycle_label(prev_end, 20), '1405/07/20 تا 1405/08/19')
        self.assertEqual(jalali_cycle_label(next_start, 20), '1405/08/20 تا 1405/09/19')

    def test_year_boundary(self):
        day = jdatetime.date(1406, 1, 1).togregorian()
        self.assertEqual(jalali_cycle_label(day, 20), '1405/12/20 تا 1406/01/19')

    def test_anchor_first_day_is_calendar_month(self):
        day = jdatetime.date(1405, 12, 5).togregorian()
        self.assertEqual(jalali_cycle_label(day, 1), '1405/12/01 تا 1405/12/29')

    def test_farsi_dates_and_arbitrary_range(self):
        self.assertEqual(parse_jalali('۱۴۰۵/۰۸/۲۰'), jdatetime.date(1405, 8, 20).togregorian())
        start, end = range_jalali('۱۴۰۰/۰۱/۰۱', '۱۴۰۵/۰۸/۲۰')
        self.assertLess(start, end)

    def test_headers_match_daily_and_aggregates(self):
        self.assertEqual(len(report_headers('daily')), 8)
        self.assertEqual(report_headers('daily')[5], 'تعداد ثبت دستگاه')
        self.assertEqual(len(report_headers('daily_raw')), 5)
        self.assertEqual(len(report_headers('daily_pairs')), 7)
        for mode in ('monthly', 'yearly', 'cycle', 'custom'):
            self.assertEqual(len(report_headers(mode)), 7)


if __name__ == '__main__':
    unittest.main()
