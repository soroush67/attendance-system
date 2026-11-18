"""Offline, dependency-free checks of the real production presence math functions.

Loads the three actual function ASTs from main.py without importing the web app
or connecting to PostgreSQL. Full integration tests live in test_presence_hours.py.
"""

import ast
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SOURCE = Path(__file__).resolve().parent.parent / 'app' / 'main.py'
tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
functions = {'observed_day_seconds', 'duration_minutes', 'duration_label'}
items = [item for item in tree.body
         if isinstance(item, ast.FunctionDef) and item.name in functions]
assert len(items) == len(functions), 'Missing real application presence function'
namespace = {'timezone': timezone}
exec(compile(ast.fix_missing_locations(ast.Module(body=items, type_ignores=[])),
             str(SOURCE), 'exec'), namespace)


class PresenceMathTests(unittest.TestCase):
    def test_first_last_instead_of_consecutive_pairs(self):
        day = datetime(2026, 11, 16, tzinfo=timezone.utc)
        times = [day + timedelta(hours=10, minutes=49, seconds=16),
                 day + timedelta(hours=10, minutes=49, seconds=19),
                 day + timedelta(hours=16, minutes=48, seconds=9),
                 day + timedelta(hours=16, minutes=48, seconds=11)]
        seconds = namespace['observed_day_seconds'](times)
        self.assertEqual(seconds, 5 * 3600 + 58 * 60 + 55)
        self.assertEqual(namespace['duration_label'](seconds), '05:59')

    def test_empty_one_and_repeated_same_punch_are_incomplete(self):
        now = datetime.now(timezone.utc)
        calculate = namespace['observed_day_seconds']
        self.assertIsNone(calculate([]))
        self.assertIsNone(calculate([now]))
        self.assertIsNone(calculate([now, now]))

    def test_multi_day_seconds_round_once(self):
        fmt = namespace['duration_label']
        self.assertEqual(fmt(29 + 29), '00:01')
        self.assertEqual(fmt(25 * 3600 + 2 * 60), '25:02')
        self.assertEqual(namespace['duration_minutes'](0), 0)

    def test_equivalent_offset_timestamps_zero_elapsed(self):
        f = namespace['observed_day_seconds']
        iran = timezone(timedelta(hours=3, minutes=30))
        first = datetime(2026, 11, 16, 9, 0, tzinfo=iran)
        last = datetime(2026, 11, 16, 13, 0, tzinfo=iran)
        self.assertEqual(f([last, first, first]), 4 * 3600)


if __name__ == '__main__':
    unittest.main()
