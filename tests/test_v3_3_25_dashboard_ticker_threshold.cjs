'use strict';
// Execute the production per-ticker AI threshold controls (v3.3.25) in a VM.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../dashboard/index.html'), 'utf8');
const start = html.indexOf('// v3.3.25: per-ticker AI threshold');
const code = html.slice(start, html.indexOf('function updateExecShareLimit(', start));
assert.ok(start > 0 && code.includes('function renderTickerThreshold('));
const msgStart = html.indexOf('function executionMessage(');
const executionMessage = html.slice(msgStart, html.indexOf('\n}', msgStart) + 2);
assert.match(html, /id="exec-thr-input-\$\{escaped\}" type="number" min="30" max="95"/);
assert.match(html, /renderTickerThreshold\(ticker\);\n    const save=el\('exec-save-'\+ticker\)/);

function setup(aiRow, responses = []) {
  const nodes = {}, calls = [], notices = [];
  const node = id => (nodes[id] ??= {id, value: '', disabled: false, dataset: {}, textContent: '',
    attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }});
  const document = {activeElement: null};
  const context = vm.createContext({
    el: node, Number, String, Math, JSON, document,
    S: {aiStatus: [aiRow]},
    aiRowForTicker: () => aiRow,
    toast: (...args) => notices.push(args),
    fetchAIStatus: async () => { calls.push({url: 'status'}); },
    api: async (url, options) => { calls.push({url, options}); return {json: async () => responses.shift()}; },
  });
  vm.runInContext(executionMessage + code, context);
  return {context, nodes, calls, notices, document, node};
}

(async () => {
  const meta = {model_threshold: 0.79, operator_threshold: null, production_threshold: 0.79, veto_threshold: 12};
  const s = setup(meta, [{status: 'ok', applied_threshold: 0.6}]);
  s.context.renderTickerThreshold('META');
  assert.equal(s.nodes['exec-thr-input-META'].value, '79');
  assert.equal(s.nodes['exec-thr-save-META'].disabled, true, 'nothing to apply yet');
  assert.equal(s.nodes['exec-thr-reset-META'].disabled, true, 'no manual threshold to clear');
  assert.equal(s.nodes['exec-thr-note-META'].textContent, 'Modelo 79% · 12 veto(s) por umbral');

  // Typing keeps the draft through background polling.
  s.context.S.thrDrafts = {META: '60'};
  s.nodes['exec-thr-input-META'].value = '60';
  s.context.renderTickerThreshold('META');
  assert.equal(s.nodes['exec-thr-input-META'].value, '60');
  assert.equal(s.nodes['exec-thr-save-META'].disabled, false);
  await s.context.applyTickerThreshold('META');
  assert.equal(s.calls[0].url, '/api/ai/threshold/META');
  assert.deepEqual(JSON.parse(s.calls[0].options.body), {threshold: 0.6});
  assert.equal(s.calls[1].url, 'status');
  assert.equal(s.notices[0][0], 'ok');
  assert.match(s.notices[0][2], /umbral 60% aplicado a las próximas señales/);
  assert.equal(s.context.S.thrDrafts.META, undefined);

  // Invalid values never reach the server.
  for (const bad of ['29', '96', '60.5', '']) {
    s.context.S.thrDrafts = {META: bad};
    s.context.renderTickerThreshold('META');
    assert.equal(s.nodes['exec-thr-save-META'].disabled, true, bad);
    assert.equal(s.nodes['exec-thr-note-META'].dataset.state, 'error', bad);
    await s.context.applyTickerThreshold('META');
  }
  assert.equal(s.calls.length, 2);

  // Reset to the model's threshold; a server refusal is surfaced.
  const manual = {model_threshold: 0.79, operator_threshold: 0.6, production_threshold: 0.6, veto_threshold: 3};
  const r = setup(manual, [{status: 'ok', applied_threshold: 0.79}, {status: 'error', message: 'disk full'}]);
  r.context.renderTickerThreshold('META');
  assert.equal(r.nodes['exec-thr-reset-META'].disabled, false);
  assert.equal(r.nodes['exec-thr-note-META'].textContent, 'Modelo 79% · manual 60% · 3 veto(s) por umbral');
  await r.context.resetTickerThreshold('META');
  assert.equal(r.calls[0].options.method, 'DELETE');
  assert.match(r.notices[0][2], /vuelve al umbral del modelo \(79%\)/);
  await r.context.resetTickerThreshold('META');
  assert.deepEqual(r.notices[1].slice(0, 2), ['err', 'Umbral IA']);

  // Without a PRODUCTION model the operator value is still shown and kept.
  const none = setup({model_threshold: null, operator_threshold: 0.55, production_threshold: null});
  none.context.renderTickerThreshold('BA');
  assert.equal(none.nodes['exec-thr-input-BA'].value, '55');
  assert.match(none.nodes['exec-thr-note-BA'].textContent, /^Sin modelo PRODUCTION/);
  console.log('Dashboard per-ticker AI threshold scenarios passed.');
})().catch(error => { console.error(error); process.exit(1); });
