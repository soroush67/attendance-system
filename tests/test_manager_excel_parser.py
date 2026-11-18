"""Offline, database-free tests of Excel ingestion bounds and validation."""

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from manager_excel import (MAX_XLSX_BYTES, ManagerExcelValidationError,
                           parse_manager_excel)


FIXTURES = Path(__file__).resolve().parent / 'fixtures'
TEMPLATE = Path(__file__).resolve().parents[1] / 'app' / 'templates' / 'manager-attendance-template.xlsx'


class ExcelParserTests(unittest.TestCase):
    def test_one_file_accepts_150_synthetic_manager_days(self):
        entries = parse_manager_excel((FIXTURES / 'manager-excel-150-valid.xlsx').read_bytes())
        self.assertEqual(len(entries), 150)
        self.assertEqual(len({(row.device_id, row.day_jalali) for row in entries}), 150)
        self.assertEqual(len({row.device_id for row in entries}), 5)
        self.assertEqual(entries[0].excel_row, 2)
        self.assertEqual(entries[-1].excel_row, 151)

    def test_rejects_duplicate_manager_day(self):
        with self.assertRaisesRegex(ManagerExcelValidationError, 'ردیف 3'):
            parse_manager_excel((FIXTURES / 'manager-excel-duplicate.xlsx').read_bytes())

    def test_rejects_excel_formula(self):
        with self.assertRaisesRegex(ManagerExcelValidationError, 'فرمول'):
            parse_manager_excel((FIXTURES / 'manager-excel-formula.xlsx').read_bytes())

    def test_empty_template_is_not_an_import(self):
        with self.assertRaisesRegex(ManagerExcelValidationError, 'هیچ ردیف'):
            parse_manager_excel(TEMPLATE.read_bytes())

    def test_rejects_non_excel_and_excessive_size(self):
        for payload in (b'not-a-spreadsheet', b'', b'X' * (MAX_XLSX_BYTES + 1)):
            with self.subTest(length=len(payload)):
                with self.assertRaises(ManagerExcelValidationError):
                    parse_manager_excel(payload)

    def test_template_headers_match_expected_schema(self):
        from openpyxl import load_workbook
        from manager_excel import HEADERS
        with TEMPLATE.open('rb') as handle:
            book = load_workbook(handle, read_only=True)
            self.assertEqual(tuple(cell.value for cell in next(book.worksheets[0].iter_rows())), HEADERS)
            self.assertEqual(book.sheetnames, ['تردد مدیران', 'راهنما'])
            book.close()


if __name__ == '__main__':
    unittest.main()
