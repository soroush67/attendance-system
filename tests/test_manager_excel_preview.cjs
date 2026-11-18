// Offline UI regression using exactly the JavaScript shipped inside app/main.py.
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../app/main.py'), 'utf8');
const found = source.match(/MANAGER_EXCEL_PREVIEW_SCRIPT = '''\n([\s\S]*?)\n'''/);
assert.ok(found, 'excel preview JavaScript exists');
const script = new vm.Script(found[1], {filename: 'manager-excel-preview.js'});

function element(){
  return {
    textContent: '', checked: false, disabled: false, indeterminate: false,
    handlers: {}, addEventListener(event, callback){this.handlers[event] = callback},
  };
}

function page(available){
  const master = element();
  const counter = element();
  const error = element();
  const boxes = Array.from({length: available}, element);
  const form = {
    handlers: {},
    querySelectorAll(selector){
      assert.equal(selector, 'input[name="selected_rows"]:not(:disabled)');
      return boxes;
    },
    addEventListener(event, callback){this.handlers[event] = callback},
  };
  const ids = {
    'manager-excel-confirm-form': form,
    'manager-excel-select-all': master,
    'manager-excel-selection-count': counter,
    'manager-excel-selection-error': error,
  };
  script.runInNewContext({document: {getElementById: id => ids[id]}}, {timeout: 2000});
  function submit(){
    let blocked = false;
    form.handlers.submit({preventDefault(){blocked = true}});
    return blocked;
  }
  return {master, boxes, counter, error, submit};
}

{
  const ui = page(150);
  assert.equal(ui.submit(), true, 'empty selection is blocked');
  assert.match(ui.error.textContent, /ابتدا/);
  ui.master.checked = true;
  ui.master.handlers.change();
  assert.equal(ui.boxes.filter(box => box.checked).length, 150, 'select all 150');
  assert.match(ui.counter.textContent, /^150 ردیف از 150/);
  assert.equal(ui.submit(), false, 'selected rows can proceed to server validation');

  ui.boxes[149].checked = false;
  ui.boxes[149].handlers.change();
  assert.equal(ui.master.indeterminate, true, 'partial checkboxes visible');
  assert.match(ui.counter.textContent, /^149 ردیف از 150/);
  ui.master.checked = false;
  ui.master.handlers.change();
  assert.equal(ui.boxes.filter(box => box.checked).length, 0, 'deselect all');
}

{
  const ui = page(0);
  assert.equal(ui.master.disabled, true, 'already imported rows are not reselectable');
  assert.equal(ui.submit(), true, 'zero available rows cannot be submitted');
}

console.log('PASS excel preview: 150-row select all, partial, clear, empty and unavailable');
