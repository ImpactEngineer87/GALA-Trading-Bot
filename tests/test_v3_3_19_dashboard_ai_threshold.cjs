'use strict';
// Execute the production AI-threshold dialog helpers in a VM; no browser or HTTP.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../dashboard/index.html'), 'utf8');
const start = html.indexOf('function aiThresholdSection(');
const code = html.slice(start, html.indexOf('async function saveModelAuthorization(', start));
const escStart = html.indexOf('function escHtml(');
const escHtml = html.slice(escStart, html.indexOf('\n}', escStart) + 2);

function setup(responses) {
  const values = {aiThrValue: {value: '60'}, aiThrReason: {value: 'client'}};
  const calls = [], notices = [], reloads = [];
  const context = vm.createContext({
    el: id => values[id], Number, String, Math, JSON,
    toast: (...args) => notices.push(args),
    fetchAIStatus: () => reloads.push('status'),
    showModelDetails: async ticker => reloads.push(ticker),
    api: async (url, options) => { calls.push({url, options}); return {json: async () => responses.shift()}; },
  });
  vm.runInContext(escHtml + code, context);
  return {context, calls, notices, reloads};
}

(async () => {
  const s = setup([{status: 'ok', applied_threshold: 0.6}]);
  const model = s.context.aiThresholdSection('META', {status: 'ok', model_threshold: 0.69,
    applied_threshold: 0.69, override: null, min_threshold: 0.3, max_threshold: 0.95, model_loaded: true});
  assert.match(model, /Umbral del modelo: 69% · Umbral aplicado: <b>69%<\/b> \(del modelo\)/);
  assert.match(model, /min="30" max="95"/);
  assert.match(model, /type="button" onclick="clearAIThreshold\('META'\)" disabled/);
  const manual = s.context.aiThresholdSection('META', {status: 'ok', model_threshold: 0.69,
    applied_threshold: 0.6, min_threshold: 0.3, max_threshold: 0.95, model_loaded: true,
    override: {threshold: 0.6, updated_by: 'admin', updated_utc: '2026-10-09T13:00:00+00:00', reason: '<b>x</b>'}});
  assert.match(manual, /Umbral aplicado: <b>60%<\/b> \(manual · admin/);
  assert.doesNotMatch(manual, /<b>x<\/b>/, 'operator text is escaped');
  assert.match(manual, /value="60"/);
  await s.context.saveAIThreshold('META');
  assert.equal(s.calls[0].url, '/api/ai/threshold/META');
  assert.deepEqual(JSON.parse(s.calls[0].options.body), {threshold: 0.6, reason: 'client'});
  assert.equal(s.notices[0][0], 'ok');
  assert.deepEqual(s.reloads, ['status', 'META']);
  const failing = setup([{status: 'error', message: 'Detenga el motor antes de cambiar el umbral de IA.'}]);
  await failing.context.clearAIThreshold('META');
  assert.equal(failing.calls[0].options.method, 'DELETE');
  assert.deepEqual(failing.notices[0].slice(0, 2), ['err', 'Umbral IA']);
  assert.match(failing.notices[0][2], /Detenga el motor/);
  console.log('Dashboard AI threshold dialog scenarios passed.');
})().catch(error => { console.error(error); process.exit(1); });
