// Offline browser-behavior regression: no third-party npm modules required.
// Exercises the exact nonce-injected JS shipped in app/main.py.
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../app/main.py'), 'utf8');
const match = source.match(/MANAGER_BULK_SCRIPT = '''\n([\s\S]*?)\n'''/);
assert.ok(match, 'manager bulk client script is defined');
const script = new vm.Script(match[1], {filename: 'manager-bulk-embedded.js'});

function input(value = '') {
  return {
    value, attributes: {}, handlers: {}, focusCount: 0,
    addEventListener(event, fn){this.handlers[event] = fn},
    setAttribute(name, value){this.attributes[name] = value},
    removeAttribute(name){delete this.attributes[name]},
    focus(){this.focusCount += 1},
  };
}

function render(count) {
  const fields = ['day', 'first', 'last', 'reason'];
  const rows = [];
  const boxes = [];
  for (let i = 1; i <= count; i++) {
    const rowFields = Object.fromEntries(fields.map(field => [field, input(field === 'day' ? '1405/08/25' : '')]));
    const row = {
      fields: rowFields,
      querySelector(query){
        if (query === 'strong') return {textContent: `مدیر ${i}`};
        const found = query.match(/^\[name="(day|first|last|reason)_(\d+)"\]$/);
        assert.ok(found && Number(found[2]) === i, `valid row field selector ${query}`);
        return rowFields[found[1]];
      },
    };
    const box = input();
    box.value = String(i);
    box.checked = false;
    box.closest = query => {
      assert.equal(query, 'tr');
      return row;
    };
    rows.push(row);
    boxes.push(box);
  }
  const master = input();
  master.checked = false;
  const counter = input();
  const feedback = input();
  feedback.classList = {
    error: false,
    toggle(name, value){assert.equal(name, 'error');this.error = value},
  };
  const apply = input();
  const common = Object.fromEntries(fields.map(field => [field, input()]));
  const form = {
    handlers: {},
    querySelectorAll(query){
      assert.equal(query, 'tbody input[name="employee_ids"]');
      return boxes;
    },
    addEventListener(event, fn){this.handlers[event] = fn},
  };
  const byId = {
    'manager-bulk-form': form,
    'manager-bulk-select-all': master,
    'manager-bulk-count': counter,
    'manager-bulk-feedback': feedback,
    'manager-bulk-apply-shared': apply,
    ...Object.fromEntries(fields.map(field => [`manager-bulk-common-${field}`, common[field]])),
  };
  script.runInNewContext({document: {getElementById: id => byId[id]}}, {timeout: 2000});
  function submit(){
    let prevented = false;
    form.handlers.submit({preventDefault(){prevented = true}});
    return prevented;
  }
  return {boxes, rows, master, counter, feedback, apply, common, submit};
}

{
  const ui = render(6);
  assert.equal(ui.master.disabled, false);
  ui.master.checked = true;
  ui.master.handlers.change();
  assert.equal(ui.boxes.filter(box => box.checked).length, 6, 'select all selects six managers');
  assert.match(ui.counter.textContent, /^6 نفر از 6/);
  assert.equal(ui.submit(), true, 'placeholders cannot pass validation');
  assert.match(ui.feedback.textContent, /ساعت ورود/);
  assert.equal(ui.rows[0].fields.first.focusCount, 1);

  ui.common.first.value = '08:00';
  ui.common.last.value = '17:00';
  ui.apply.handlers.click();
  assert.equal(ui.submit(), true, 'reason remains mandatory after filling both hours');
  assert.match(ui.feedback.textContent, /دلیل ثبت/);
  ui.common.reason.value = 'تردد اعلامی مدیر';
  ui.apply.handlers.click();
  assert.equal(ui.rows[5].fields.reason.value, 'تردد اعلامی مدیر');
  assert.equal(ui.rows[5].fields.last.value, '17:00');
  assert.equal(ui.submit(), false, 'all populated rows can submit');

  ui.rows[2].fields.last.value = '07:00';
  assert.equal(ui.submit(), true, 'exit earlier than entry is rejected');
  assert.match(ui.feedback.textContent, /بعد از ورود/);
  ui.rows[2].fields.last.value = '17:00';

  ui.boxes[0].checked = false;
  ui.boxes[0].handlers.change();
  assert.equal(ui.master.indeterminate, true, 'partial selection is visible');
  ui.master.checked = false;
  ui.master.handlers.change();
  assert.equal(ui.boxes.filter(box => box.checked).length, 0, 'select all can clear selection');
  assert.equal(ui.submit(), true, 'empty selection is blocked');
}

{
  const ui = render(30);
  assert.equal(ui.master.disabled, true, 'select all disabled above server limit');
  assert.match(ui.feedback.textContent, /Excel/);
  ui.boxes.slice(0, 26).forEach(box => {box.checked = true;box.handlers.change()});
  assert.equal(ui.submit(), true, 'server limit enforced on client');
  assert.match(ui.feedback.textContent, /۲۵/);
}

console.log('PASS manager bulk select-all, common-field fill, reason check, and 25-manager cap');
