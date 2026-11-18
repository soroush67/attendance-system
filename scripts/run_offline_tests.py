#!/usr/bin/env python3
"""Run all non-browser Python tests inside the offline application image.

The synthetic Playwright fixture is deliberately not part of this suite: it
needs a separate browser-equipped QA runner and is NOT counted as passing.
"""

import sys
from pathlib import Path
import unittest


BROWSER_FIXTURE = 'test_report_actions_visual.py'


def discover_modules(directory: Path):
    return sorted(
        path.stem
        for path in directory.glob('test_*.py')
        if path.name != BROWSER_FIXTURE
    )


def main():
    if len(sys.argv) != 2:
        print('Usage: run_offline_tests.py TEST_DIRECTORY', file=sys.stderr)
        return 2

    directory = Path(sys.argv[1]).resolve()
    if not directory.is_dir():
        print('Missing test directory', file=sys.stderr)
        return 2

    modules = discover_modules(directory)
    if not modules:
        print('No offline test modules discovered', file=sys.stderr)
        return 2

    sys.path.insert(0, str(directory))
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in modules)
    result = unittest.TextTestRunner(verbosity=2).run(suite)

    print('OFFLINE_TESTS_RUN:', result.testsRun)
    print('OFFLINE_FAILURES:', len(result.failures))
    print('OFFLINE_ERRORS:', len(result.errors))
    print('BROWSER_VISUAL_QA: NOT RUN (separate Playwright runner required)')
    if result.testsRun < 100 or not result.wasSuccessful():
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
