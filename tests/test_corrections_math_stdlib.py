"""Run actual correction math/clock validators without PostgreSQL or packages."""

import ast
import re
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo


class BadInput(Exception):
    def __init__(self, code, message):
        self.status_code = code
        super().__init__(message)


SOURCE = Path(__file__).resolve().parents[1] / 'app' / 'main.py'
tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
wanted = {'effective_day_window', 'correction_clock', 'correction_reason'}
nodes = [node for node in tree.body
         if isinstance(node, ast.FunctionDef) and node.name in wanted]
assert {node.name for node in nodes} == wanted
runtime = {'timezone': timezone, 'time': time, 'datetime': datetime, 'TZ': ZoneInfo('Asia/Tehran'),
           're': re, 'HTTPException': BadInput}
exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
             str(SOURCE), 'exec'), runtime)


class CorrectionMathTests(unittest.TestCase):
    def test_one_swipe_missing_arrival(self):
        day = date(2026, 11, 16)
        last = datetime.combine(day, time(16, tzinfo=runtime['TZ']))
        first = runtime['correction_clock'](day, '۰۸:۰۰')
        revision = SimpleNamespace(action='set', override_first=first, override_last=None)
        f, l, seconds = runtime['effective_day_window']([last], revision)
        self.assertEqual((f.hour, l.hour, seconds), (8, 16, 28800))

    def test_one_swipe_missing_departure(self):
        day = date(2026, 11, 16)
        first = datetime.combine(day, time(8, tzinfo=runtime['TZ']))
        last = runtime['correction_clock'](day, '16:15:30')
        revision = SimpleNamespace(action='set', override_first=None, override_last=last)
        f, l, seconds = runtime['effective_day_window']([first], revision)
        self.assertEqual((f.hour, l.hour, seconds), (8, 16, 29730))

    def test_manual_only_day_and_revoke(self):
        day = date(2026, 11, 16)
        make = runtime['correction_clock']
        active = SimpleNamespace(action='set', override_first=make(day, '08:00'),
                                 override_last=make(day, '17:00'))
        self.assertEqual(runtime['effective_day_window']([], active)[2], 32400)
        active.action = 'revoke'
        self.assertIsNone(runtime['effective_day_window']([], active)[2])

    def test_raw_values_not_mutated(self):
        day = date(2026, 11, 16)
        original = [datetime.combine(day, time(h, tzinfo=runtime['TZ'])) for h in (8, 12, 17)]
        snapshot = list(original)
        revision = SimpleNamespace(action='set', override_first=None,
                                   override_last=runtime['correction_clock'](day, '16:00'))
        self.assertEqual(runtime['effective_day_window'](original, revision)[2], 28800)
        self.assertEqual(original, snapshot)

    def test_invalid_clock_and_reversed_range(self):
        day = date(2026, 11, 16)
        for value in ('8am', '25:00', '12:61', '08:00:99'):
            with self.subTest(value=value):
                with self.assertRaises(BadInput):
                    runtime['correction_clock'](day, value)
        revision = SimpleNamespace(action='set',
                                   override_first=runtime['correction_clock'](day, '16:00'),
                                   override_last=None)
        only = [datetime.combine(day, time(8, tzinfo=runtime['TZ']))]
        self.assertIsNone(runtime['effective_day_window'](only, revision)[2])

    def test_reason_validation(self):
        self.assertEqual(runtime['correction_reason']('  یادداشت آزمون  '), 'یادداشت آزمون')
        for reason in ('x', ' ', 'a' * 501):
            with self.assertRaises(BadInput):
                runtime['correction_reason'](reason)


if __name__ == '__main__':
    unittest.main()
