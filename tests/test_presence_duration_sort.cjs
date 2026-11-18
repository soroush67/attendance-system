/* Offline browser duration sorting regression, including totals > 24 hours. */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const src = fs.readFileSync(path.join(__dirname, '..', 'app', 'main.py'), 'utf8');
const script = src.match(/REPORT_SORT_SCRIPT = '''([\s\S]*?)'''/);
assert.ok(script);

const rows = [
  {value: '100:02'},
  {value: '09:30'},
  {value: '10:05'},
  {value: '—'},
].map(item => ({cells: [{textContent: item.value}]}));
const indicator = {textContent: ''};
const button = {dataset: {sortType: 'duration'}, addEventListener: (_type, cb) => {button.click = cb;}};
const header = {
  attributes: {}, setAttribute(key, value) {this.attributes[key] = value;},
  querySelector(selector) {return selector === '.sort-head' ? button : indicator;},
};
const tbody = {rows, appendChild(fragment) {this.rows = fragment.children;}};
const table = {tBodies: [tbody], tHead: {querySelectorAll: () => [header]}};
const document = {
  getElementById: id => id === 'attendance-report-table' ? table : null,
  createDocumentFragment: () => ({children: [], appendChild(row) {this.children.push(row);}}),
};
vm.runInNewContext(script[1], {document, Intl});
button.click({shiftKey: false});
assert.deepEqual(tbody.rows.map(row => row.cells[0].textContent), ['09:30', '10:05', '100:02', '—']);
button.click({shiftKey: false});
assert.deepEqual(tbody.rows.map(row => row.cells[0].textContent), ['100:02', '10:05', '09:30', '—']);
console.log('PASS: total duration sorting over 24 hours and incomplete rows');
