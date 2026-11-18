/* Regression test for the CSP-nonced, browser-side multi-column sorter.
   Run on a development machine with Node.js: node tests/test_report_sort.cjs
   Uses only built-in Node.js modules; no package install or production DB. */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '..', 'app', 'main.py'), 'utf8');
const match = source.match(/REPORT_SORT_SCRIPT = '''([\s\S]*?)'''/);
assert.ok(match, 'The HTML sort script must be present in main.py');

const data = [
  {id: 'A-day1-session1', cells: ['الف', '1405/08/10', 1, '08:00:00', '12:00:00', 240]},
  {id: 'B-day1-session1', cells: ['ب', '1405/08/10', 1, '09:00:00', '11:00:00', 120]},
  {id: 'A-day2-session1', cells: ['الف', '1405/08/11', 1, '10:00:00', '11:00:00', 60]},
  {id: 'A-day1-session2', cells: ['الف', '1405/08/10', 2, '13:00:00', '13:45:00', 45]},
  {id: 'A-day1-unpaired', cells: ['الف', '1405/08/10', 3, '16:00:00', '—', '—']},
].map(item => ({id: item.id, cells: item.cells.map(value => ({textContent: String(value)}))}));

const types = ['text', 'date', 'number', 'time', 'time', 'number'];
const headers = types.map(type => {
  const indicator = {textContent: ''};
  const button = {
    dataset: {sortType: type},
    addEventListener(event, callback) { assert.equal(event, 'click'); this.click = callback; },
  };
  return {
    button,
    indicator,
    attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    querySelector(selector) {
      if (selector === '.sort-head') return button;
      if (selector === '.sort-indicator') return indicator;
      throw new Error(`Unexpected selector: ${selector}`);
    },
  };
});
// The daily report now has a static, admin-only actions column.
// It must not register a sorter or break the existing eight data columns.
const actionsHeader = {
  attributes: {},
  setAttribute(name, value) { this.attributes[name] = value; },
  querySelector(selector) {
    if (selector === '.sort-head' || selector === '.sort-indicator') return null;
    throw new Error(`Unexpected selector: ${selector}`);
  },
};
headers.push(actionsHeader);

const tbody = {
  rows: data.slice(),
  appendChild(fragment) { this.rows = fragment.children; },
};
const table = {tBodies: [tbody], tHead: {querySelectorAll: () => headers}};
const mobileField = {value: '-1', addEventListener(event, callback) { assert.equal(event, 'change'); this.change = callback; }};
const mobileDirection = {value: '1', addEventListener(event, callback) { assert.equal(event, 'change'); this.change = callback; }};
const document = {
  getElementById(id) {
    if (id === 'attendance-report-table') return table;
    if (id === 'report-sort-field') return mobileField;
    if (id === 'report-sort-direction') return mobileDirection;
    return null;
  },
  createDocumentFragment() { return {children: [], appendChild(row) { this.children.push(row); }}; },
};

vm.runInNewContext(match[1], {document, Intl});
assert.equal(actionsHeader.attributes['aria-sort'], undefined);
const order = () => tbody.rows.map(item => item.id);
const click = (index, shiftKey = false) => headers[index].button.click({shiftKey});

// Numeric values must sort numerically, not as strings; unmatched entries last.
click(5);
assert.deepEqual(order(), [
  'A-day1-session2', 'A-day2-session1', 'B-day1-session1',
  'A-day1-session1', 'A-day1-unpaired',
]);
assert.equal(headers[5].attributes['aria-sort'], 'ascending');
click(5);
assert.deepEqual(order(), [
  'A-day1-session1', 'B-day1-session1', 'A-day2-session1',
  'A-day1-session2', 'A-day1-unpaired',
]);

// Plain click resets the order; Shift-click appends priority keys.
click(0);
click(1, true);
click(5, true);
assert.deepEqual(order(), [
  'A-day1-session2', 'A-day1-session1', 'A-day1-unpaired',
  'A-day2-session1', 'B-day1-session1',
]);
assert.equal(headers[0].indicator.textContent, '1↑');
assert.equal(headers[1].indicator.textContent, '2↑');
assert.equal(headers[5].indicator.textContent, '3↑');
click(5, true);
assert.deepEqual(order().slice(0, 3), [
  'A-day1-session1', 'A-day1-session2', 'A-day1-unpaired',
]);
assert.equal(headers[5].indicator.textContent, '3↓');

// The mobile select must use the same row sorter, including reverse and reset.
mobileField.value = '0'; mobileField.change();
assert.deepEqual(order(), ['A-day1-session1', 'A-day2-session1', 'A-day1-session2', 'A-day1-unpaired', 'B-day1-session1']);
mobileDirection.value = '-1'; mobileDirection.change();
assert.equal(order()[0], 'B-day1-session1');
mobileField.value = '-1'; mobileField.change();
assert.deepEqual(order(), data.map(item => item.id));

console.log('PASS: desktop multi-sort and mobile ascending/descending/reset');
