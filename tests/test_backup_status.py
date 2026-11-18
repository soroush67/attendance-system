"""Self-contained backup status tests: no DB, Docker, S3, network or secrets."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone


SOURCE = Path(__file__).resolve().parent.parent / 'app/backup_status.py'
SPEC = importlib.util.spec_from_file_location('backup_status_unit', SOURCE)
status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(status)
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


class BackupStatusReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'status.json'

    def write(self, **changes):
        payload = {
            'schema': 1, 'state': 'healthy', 'checked_at_utc': NOW.isoformat(),
            'last_backup_utc': (NOW - timedelta(hours=1)).isoformat(),
            'size_bytes': 32995, 'verified_count': 1,
            'next_run': 'Mon 2026-11-16 02:30:00', 'timer_active': True,
        }
        payload.update(changes)
        self.path.write_text(json.dumps(payload), encoding='utf-8')
        return status.read_public_backup_status(path=str(self.path), now=NOW)

    def test_missing_status_is_unavailable(self):
        self.assertEqual(status.read_public_backup_status(str(self.path), NOW)['state'], 'unavailable')

    def test_verified_status(self):
        result = self.write()
        self.assertEqual(result['state'], 'healthy')
        self.assertEqual(result['size_bytes'], 32995)
        self.assertEqual(result['verified_count'], 1)

    def test_stale_publisher_is_unavailable(self):
        result = self.write(checked_at_utc=(NOW - timedelta(hours=2)).isoformat())
        self.assertEqual(result['state'], 'unavailable')

    def test_future_publisher_is_unavailable(self):
        result = self.write(checked_at_utc=(NOW + timedelta(minutes=7)).isoformat())
        self.assertEqual(result['state'], 'unavailable')

    def test_invalid_schema_fails_closed(self):
        self.assertEqual(self.write(schema=2)['state'], 'unavailable')

    def test_healthy_state_requires_active_timer(self):
        self.assertEqual(self.write(timer_active=False)['state'], 'warning')

    def test_healthy_state_requires_last_backup(self):
        self.assertEqual(self.write(last_backup_utc=None)['state'], 'warning')

    def test_unknown_state_fails_closed(self):
        self.assertEqual(self.write(state='superhealthy')['state'], 'unavailable')

    def test_oversized_payload_fails_closed(self):
        self.path.write_text('x' * 8193, encoding='utf-8')
        self.assertEqual(status.read_public_backup_status(str(self.path), NOW)['state'], 'unavailable')

    def test_sensitive_unexpected_keys_never_returned(self):
        result = self.write(RUSTFS_SECRET_KEY='never-include-me', database_url='db-secret')
        self.assertNotIn('RUSTFS_SECRET_KEY', result)
        self.assertNotIn('database_url', result)
        self.assertNotIn('never-include-me', repr(result))

    def test_next_run_controls_fails_closed(self):
        self.assertIsNone(self.write(next_run='hi\nmalicious')['next_run'])


if __name__ == '__main__':
    unittest.main()
