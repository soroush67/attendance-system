"""Synthetic responsive browser regression (no FastAPI, DB, or employee data).

Run on a QA workstation with Playwright and Chromium installed:
    python3 tests/test_report_actions_visual.py

This checks the real CSS/JS against representative markup. It is not a live E2E pass.
"""

import ast
import os
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT / 'app/main.py').read_text(encoding='utf-8'))
style = next(
    node.value.value for node in tree.body
    if isinstance(node, ast.Assign)
    and any(isinstance(target, ast.Name) and target.id == 'STYLE' for target in node.targets)
)
scripts = {}
for node in tree.body:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in ('THEME_SCRIPT', 'REPORT_SORT_SCRIPT'):
                scripts[target.id] = node.value.value

fake_rows = [
    ('کارمند نمونه ب', '1006', '1405/08/22', '09:05:00', '17:30:00', '2', '08:25', '505'),
    ('کارمند نمونه الف', '1005', '1405/08/25', '09:00:00', '16:00:00', '0', '07:00', '420'),
]
headings = ('نام کارمند', 'شناسه دستگاه', 'تاریخ شمسی', 'اولین ثبت', 'آخرین ثبت',
            'تعداد ثبت دستگاه', 'مدت حضور ثبت‌شده', 'مدت (دقیقه)', 'عملیات')
table_head = ''.join(
    f'<th aria-sort="none"><button type="button" class="sort-head" data-sort-type="{type}">'
    f'{h}<span class="sort-indicator" aria-hidden="true">↕</span></button></th>'
    for h, type in zip(headings[:8], ('text', 'text', 'date', 'time', 'time', 'number', 'duration', 'number'))
) + '<th>عملیات</th>'
rows = ''.join(
    '<tr>'
    + f'<td data-label="نام کارمند">{row[0]}'
    + ('' if row[1] == '1006' else '<span class="correction-tag">✎ اصلاح مدیر</span>')
    + '</td>'
    + ''.join(f'<td data-label="{label}">{value}</td>' for label, value in zip(headings[1:8], row[1:]))
    + '<td class="report-actions-cell" data-label="عملیات"><div class="report-row-actions">'
    '<a class="correction-edit" href="#changes">اعمال تغییرات</a>'
    '<a class="correction-edit" href="#corrections">اصلاح تردد</a>'
    '</div></td></tr>'
    for row in fake_rows
)

html = (
    '<!doctype html><html lang="fa" dir="rtl"><head><meta charset="UTF-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    f'<style>{style}</style></head><body>'
    '<div class="shell"><aside><div class="company-logo-wrap">لوگو</div>'
    '<div class="brand">سامانه حضور و غیاب</div>'
    '<nav class="desktop-nav" aria-label="منوی اصلی">'
    + ''.join(f'<a href="#nav-{i}">بخش آزمایشی شماره {i}</a>' for i in range(12))
    + '</nav><details class="mobile-nav"><summary>☰ فهرست سامانه · گزارش تردد</summary>'
    '<nav aria-label="منوی موبایل">'
    + ''.join(f'<a href="#nav-{i}">بخش آزمایشی شماره {i}</a>' for i in range(12))
    + '</nav></details><div class="side-footer">تاریخچه فعالیت</div></aside><main>'
    '<div class="top"><h1>گزارش حضور</h1></div>'
    '<div class="card"><h2>گزارش روزانه</h2>'
    '<p class="report-caution">این مقدار کارکرد تأییدشده حقوق و دستمزد نیست.</p>'
    '<details class="report-methodology"><summary>راهنمای محاسبه و نکات مهم گزارش</summary>'
    '<p>اطلاعات کاملاً ساختگی برای آزمایش رابط کاربری؛ توضیح کامل بدون حذف.</p></details></div>'
    '<div class="report-mobile-sort"><div><label for="report-sort-field">مرتب‌سازی بر اساس</label>'
    '<select id="report-sort-field"><option value="-1">ترتیب اولیه</option><option value="0">نام کارمند</option>'
    '<option value="2">تاریخ شمسی</option></select></div><div>'
    '<label for="report-sort-direction">جهت</label><select id="report-sort-direction">'
    '<option value="1">صعودی</option><option value="-1">نزولی</option></select></div></div>'
    f'<div class="card report-table-panel"><p class="report-scroll-hint">جدول قابل پیمایش افقی است.</p>'
    f'<div class="report-table-scroll" role="region" tabindex="0"><table id="attendance-report-table" class="sortable-table">'
    f'<thead><tr>{table_head}</tr></thead><tbody>{rows}</tbody></table></div></div>'
    '</main></div>'
    f'<script>{scripts["REPORT_SORT_SCRIPT"]}</script><script>{scripts["THEME_SCRIPT"]}</script>'
    '</body></html>'
)


def run():
    with sync_playwright() as browser_runtime:
        browser = browser_runtime.chromium.launch(headless=True, 
                                                  args=['--no-sandbox'])
        try:
            for width in (360, 768, 1440):
                for theme in ('light', 'dark'):
                    page = browser.new_page(viewport={'width': width, 'height': 900}, device_scale_factor=1)
                    page.set_content(html)
                    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                    widths = page.evaluate('''() => ({
                      document: document.documentElement.scrollWidth,
                      window: window.innerWidth,
                      table: document.querySelector('table').scrollWidth,
                      scrollBox: document.querySelector('.report-table-scroll').clientWidth
                    })''')
                    assert widths['document'] <= widths['window'] + 1, (width, theme, widths)
                    assert page.locator('.report-row-actions a').count() == 4
                    assert page.locator('.report-row-actions a').first.is_visible()
                    assert page.locator('th').count() == 9
                    assert not page.locator('.report-methodology').get_attribute('open')
                    page.locator('.report-methodology summary').click()
                    assert page.locator('.report-methodology p').is_visible()
                    if width <= 800:
                        assert page.locator('.mobile-nav summary').is_visible()
                        assert not page.locator('.mobile-nav').get_attribute('open')
                        page.locator('.mobile-nav summary').click()
                        assert page.locator('.mobile-nav nav a').count() == 12
                        last = page.locator('.mobile-nav nav a').last
                        last.scroll_into_view_if_needed()
                        assert last.is_visible()
                        bounds = last.bounding_box()
                        assert bounds and bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width + 1, bounds
                        page.keyboard.press('Escape')
                        assert not page.locator('.mobile-nav').get_attribute('open')
                        assert page.locator('.mobile-nav summary').evaluate('el => document.activeElement === el')
                        assert page.locator('.report-table-scroll tbody tr').first.locator('td').count() == 9
                        for cell in page.locator('.report-table-scroll tbody tr').first.locator('td').all():
                            assert cell.is_visible()
                        page.locator('#report-sort-field').select_option('0')
                        assert 'نمونه الف' in page.locator('#attendance-report-table tbody tr').first.locator('td').first.inner_text()
                    else:
                        metrics = page.evaluate('''() => {
                          const a=document.querySelector('.report-row-actions a').getBoundingClientRect();
                          const box=document.querySelector('.report-table-scroll').getBoundingClientRect();
                          const sidebar=document.querySelector('aside').getBoundingClientRect();
                          const main=document.querySelector('main').getBoundingClientRect();
                          return {a,box,sidebar,main};
                        }''')
                        assert metrics['a']['left'] >= metrics['box']['left'] - 2, metrics
                        assert metrics['a']['right'] <= metrics['box']['right'] + 2, metrics
                        assert metrics['sidebar']['bottom'] >= metrics['main']['bottom'] - 1, metrics
                    page.close()
                    print(f'PASS synthetic visual DOM: {width}px {theme}')
        finally:
            browser.close()


if __name__ == '__main__':
    fixture_path = os.environ.get('ATTENDANCE_VISUAL_FIXTURE_PATH')
    if fixture_path:
        Path(fixture_path).write_text(html, encoding='utf-8')
        print(f'Wrote synthetic HTML fixture: {fixture_path}')
        raise SystemExit(0)
    run()
