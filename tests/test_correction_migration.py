"""Execute the *real* additive Alembic migration against isolated SQLite."""

import importlib.util
import unittest
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from main import Base


MIGRATION_PATH = Path(__file__).resolve().parents[1] / 'app/migrations/versions/0002_attendance_corrections.py'
spec = importlib.util.spec_from_file_location('attendance_corrections_migration', MIGRATION_PATH)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class CorrectionMigrationTests(unittest.TestCase):
    def test_adds_only_correction_table_and_two_indexes(self):
        engine = create_engine('sqlite+pysqlite:///:memory:')
        try:
            old_tables = [table for table in Base.metadata.sorted_tables
                          if table.name != 'attendance_corrections']
            Base.metadata.create_all(engine, tables=old_tables)
            with engine.begin() as db:
                db.execute(text("INSERT INTO employees (id, device_id, name, status, created_at) "
                                "VALUES (1, '101', 'existing employee', 'active', '2026-11-16')"))
            original_tables = set(inspect(engine).get_table_names())
            with engine.begin() as db:
                operations = Operations(MigrationContext.configure(db))
                previous = migration.op
                try:
                    migration.op = operations
                    migration.upgrade()
                finally:
                    migration.op = previous
            inspector = inspect(engine)
            self.assertEqual(set(inspector.get_table_names()) - original_tables,
                             {'attendance_corrections'})
            self.assertEqual({item['name'] for item in inspector.get_indexes('attendance_corrections')},
                             {'ix_attendance_corrections_employee_day_revision',
                              'ix_attendance_corrections_local_day'})
            with engine.connect() as db:
                self.assertEqual(db.execute(text('SELECT count(*) FROM employees')).scalar_one(), 1)
                self.assertEqual(db.execute(text('SELECT count(*) FROM attendance_corrections')).scalar_one(), 0)
            with self.assertRaisesRegex(RuntimeError, 'audit trail'):
                migration.downgrade()
        finally:
            engine.dispose()


if __name__ == '__main__':
    unittest.main()
