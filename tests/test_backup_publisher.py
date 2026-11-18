"""Pure mock S3 tests for host-only publisher, without touching production."""

import importlib.util
import json
from pathlib import Path
import unittest
from datetime import datetime, timezone
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parent.parent / 'ops/backup_status_publisher.py'
SPEC = importlib.util.spec_from_file_location('publisher_unit', SOURCE)
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)

CONFIG = {'BACKUP_PREFIX': 'attendance-prod-abcd', 'RUSTFS_BUCKET': 'attendance-backups',
          'PG_DATABASE': 'attendance'}
KEY = 'attendance-prod-abcd/20261115T114627Z-76b4ac33fb74.dump'
MANIFEST = {
    'schema': 1, 'status': 'verified', 'utc': '20261115T114627Z',
    'database': 'attendance', 'object': KEY, 'sha256': 'a' * 64, 'bytes': 32995,
}


class FakeWorker:
    import re
    COMPLETE_MANIFEST = re.compile(r'^\d{8}T\d{6}Z-[a-f0-9]{12}\.dump\.manifest\.json$')


class FakeS3:
    def __init__(self, manifest=None, length=32995):
        self.manifest = manifest or MANIFEST
        self.length = length

    def command(self, *args, **kwargs):
        if args[1] == 'list-objects-v2':
            return json.dumps({'IsTruncated': False, 'Contents': [{'Key': KEY + '.manifest.json'}]}).encode()
        if args[1] == 'head-object':
            return json.dumps({'ContentLength': self.length}).encode()
        if args[1] == 'cp':
            return json.dumps(self.manifest).encode()
        raise AssertionError('unexpected S3 call')


class BackupPublisherTests(unittest.TestCase):
    def test_verified_metadata(self):
        with patch.object(publisher, 'utcnow', return_value=datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)):
            moment, count, size = publisher.verified_backup(FakeS3(), CONFIG, FakeWorker)
        self.assertEqual(moment.isoformat(), '2026-11-15T11:46:27+00:00')
        self.assertEqual(count, 1)
        self.assertEqual(size, 32995)

    def test_bad_manifest_rejected(self):
        broken = dict(MANIFEST, object='other/dump')
        with self.assertRaises(RuntimeError):
            publisher.verified_backup(FakeS3(manifest=broken), CONFIG, FakeWorker)

    def test_mismatched_s3_size_rejected(self):
        with self.assertRaises(RuntimeError):
            publisher.verified_backup(FakeS3(length=32996), CONFIG, FakeWorker)


if __name__ == '__main__':
    unittest.main()
