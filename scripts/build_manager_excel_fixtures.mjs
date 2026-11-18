/** Synthetic XLSX acceptance fixtures, built by the spreadsheet artifact tool. */
import fs from 'node:fs/promises';
import path from 'node:path';
import { SpreadsheetFile, Workbook } from '@oai/artifact-tool';

const dir = path.resolve(process.argv[2] || 'tests/fixtures');
await fs.mkdir(dir, { recursive: true });
const headers = ['کد مدیر', 'نام مدیر', 'تاریخ شمسی', 'ساعت ورود', 'ساعت خروج', 'توضیح'];
const managers = [
  ['MAN-001', 'مدیر نمونه'], ['MAN-002', 'مدیر دوم'], ['MAN-003', 'مدیر سوم'],
  ['MAN-004', 'مدیر چهارم'], ['MAN-005', 'مدیر پنجم'],
];
const rows = managers.flatMap(([code, name]) =>
  Array.from({length: 30}, (_, day) =>
    [code, name, `1405/07/${String(day + 1).padStart(2, '0')}`, '08:00', '16:30', 'ثبت بر اساس اعلام مدیر']));

async function exportFixture(name, records) {
  const wb = Workbook.create();
  const s = wb.worksheets.add('تردد مدیران');
  s.getRange('A1:F1').values = [headers];
  s.getRange('A2').write(records);
  if (name === 'manager-excel-formula.xlsx') {
    s.getRange('F2').formulas = [['=2+2']];
  }
  s.getRange(`A2:F${records.length + 1}`).setNumberFormat('@');
  const help = wb.worksheets.add('راهنما');
  help.getRange('A1').values = [['نمونهٔ ساختگی برای تست نرم‌افزار']];
  wb.recalculate();
  const out = await SpreadsheetFile.exportXlsx(wb);
  await out.save(path.join(dir, name));
}

await exportFixture('manager-excel-150-valid.xlsx', rows);
await exportFixture('manager-excel-duplicate.xlsx', [rows[0], rows[0]]);
await exportFixture('manager-excel-invalid-clock.xlsx', [rows[0],
  ['MAN-002', 'مدیر دوم', '1405/07/02', '19:00', '07:00', 'ثبت اشتباه آزمایشی']]);
await exportFixture('manager-excel-unknown.xlsx', [rows[0],
  ['MAN-404', 'مدیر ناموجود', '1405/07/02', '08:00', '16:00', 'کارمند ناشناس تست']]);
await exportFixture('manager-excel-formula.xlsx', [rows[0]]);
console.log('Created five synthetic Excel fixtures');
