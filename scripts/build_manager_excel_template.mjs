/** Generate the offline, employee-free manager attendance XLSX template. */
import fs from 'node:fs/promises';
import path from 'node:path';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const outputDir = path.resolve(process.argv[2] || 'outputs/attendance-v189r4');
const workbook = Workbook.create();
const records = workbook.worksheets.add('تردد مدیران');
const help = workbook.worksheets.add('راهنما');
records.showGridLines = false;
help.showGridLines = false;
records.tabColor = '#176B83';
help.tabColor = '#718096';

const headings = ['کد مدیر', 'نام مدیر', 'تاریخ شمسی', 'ساعت ورود', 'ساعت خروج', 'توضیح'];
records.getRange('A1:F1').values = [headings];
records.getRange('A1:F1').format = {
  fill: '#176B83',
  font: { name: 'Arial', size: 11, color: '#FFFFFF', bold: true },
  horizontalAlignment: 'center',
  verticalAlignment: 'center',
};
records.getRange('A1:F1').format.rowHeight = 34;
records.getRange('A2:F351').format = {
  font: { name: 'Arial', size: 11, color: '#162235' },
  verticalAlignment: 'center',
};
records.getRange('A2:F351').format.rowHeight = 25;
records.getRange('A2:F351').setNumberFormat('@');
records.getRange('A2:A351').format.fill = '#FFF9E8';
records.getRange('C2:F351').format.fill = '#FFFDF7';
records.getRange('A:A').format.columnWidth = 19;
records.getRange('B:B').format.columnWidth = 30;
records.getRange('C:C').format.columnWidth = 18;
records.getRange('D:E').format.columnWidth = 17;
records.getRange('F:F').format.columnWidth = 49;
records.freezePanes.freezeRows(1);

help.getRange('A2').values = [['راهنمای ثبت گروهی ورود و خروج مدیران']];
help.getRange('A2').format.font = { name: 'Arial', size: 15, bold: true, color: '#162235' };
help.getRange('A4:B10').values = [
  ['۱', 'فقط در برگه «تردد مدیران» اطلاعات را وارد کنید. هر ردیف: یک مدیر در یک روز.'],
  ['۲', '«کد مدیر» همان کد اختصاصی نمایش‌داده‌شده کنار نام مدیر در سامانه است؛ آن را دقیقاً کپی کنید.'],
  ['۳', '«نام مدیر» اختیاری است؛ اگر نوشته شود، باید با نام ثبت‌شده در سامانه مطابقت کند.'],
  ['۴', 'تاریخ شمسی را به‌صورت متن، مانند ۱۴۰۵/۰۸/۲۵، بنویسید.'],
  ['۵', 'ساعت ورود و خروج را به‌صورت متن HH:MM، مانند 08:00 و 17:30، وارد کنید.'],
  ['۶', 'توضیح برای هر ردیف الزامی است و باید بین ۵ تا ۵۰۰ نویسه باشد.'],
  ['۷', 'تا ۱۰۰۰ ردیف داده در یک فایل پذیرفته می‌شود. همه سطرها با هم اعتبارسنجی می‌شوند.'],
];
help.getRange('A12').values = [['پس از بارگذاری، همهٔ ردیف‌های معتبر «در انتظار تأیید ادمین» می‌مانند؛ ثبت در گزارش نهایی خودکار نیست.']];
help.getRange('A13').values = [['اگر حتی یک ردیف نامعتبر یا دارای تداخل باشد، هیچ ردیفی ثبت نمی‌شود.']];
help.getRange('A4:A10').format = { fill: '#DCEEF3', font: { name: 'Arial', color: '#176B83', bold: true, size: 10 } };
help.getRange('B4:B10').format = { font: { name: 'Arial', color: '#162235', size: 11 }, verticalAlignment: 'center' };
help.getRange('A12:A13').format = { font: { name: 'Arial', color: '#8B4229', bold: true, size: 11 } };
help.getRange('A:A').format.columnWidth = 9;
help.getRange('B:B').format.columnWidth = 105;
help.getRange('A4:B10').format.rowHeight = 30;

workbook.recalculate();
const inspection = await workbook.inspect({
  kind: 'table', range: 'تردد مدیران!A1:F3', include: 'values,formulas',
  tableMaxRows: 3, tableMaxCols: 6, maxChars: 2500,
});
console.log(inspection.ndjson);
await fs.mkdir(outputDir, { recursive: true });
const preview = await workbook.render({ sheetName: 'تردد مدیران', range: 'A1:F9', scale: 1.4, format: 'png' });
await fs.writeFile(path.join(outputDir, 'manager-attendance-template-preview.png'),
                   new Uint8Array(await preview.arrayBuffer()));
const blob = await SpreadsheetFile.exportXlsx(workbook);
await blob.save(path.join(outputDir, 'manager-attendance-template.xlsx'));
console.log('Exported manager-attendance-template.xlsx');
