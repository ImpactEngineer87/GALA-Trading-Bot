const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('dashboard/index.html', 'utf8');
function section(start, end){
  const first = html.indexOf(start);
  const last = html.indexOf(end, first);
  assert(first >= 0 && last > first);
  return html.slice(first, last);
}

const buttons = Object.fromEntries(
  ['schwabAuthOpenBtn','schwabConnectBtn','schwabTestBtn','schwabNetworkDiagBtn','schwabRetryNotice']
    .map(id => [id, {disabled: false, hidden: true, textContent: ''}])
);
const opened = [];
const notices = [];
let now = 1000;
let result = {ok: false, json: async () => ({retry_after_seconds: 600, message: 'Wait'})};
const context = vm.createContext({
  Date: {now: () => now},
  el: id => buttons[id],
  api: async () => result,
  window: {open: (...args) => opened.push(args)},
  toast: (...args) => notices.push(args),
});
vm.runInContext(
  section('let schwabRetryUntil = 0;', 'async function checkSchwabStatus(){') +
  section('async function openSchwabAuth(){', 'function renderSchwabOAuthDiagnostic('),
  context
);

(async () => {
  // A paused response must not open an undefined authorization URL.
  await vm.runInContext('openSchwabAuth()', context);
  assert.equal(opened.length, 0);
  for(const id of Object.keys(buttons).filter(id => id !== 'schwabRetryNotice')){
    assert.equal(buttons[id].disabled, true);
  }
  assert.equal(buttons.schwabRetryNotice.hidden, false);
  assert.match(buttons.schwabRetryNotice.textContent, /10 min/);
  assert.equal(vm.runInContext('schwabRetryUntil', context), 601000);

  // Operator-visible storage failure explains why a restart is unsafe.
  vm.runInContext("setSchwabRetryPause(300, 'La espera no pudo guardarse')", context);
  assert.match(buttons.schwabRetryNotice.textContent, /no pudo guardarse/);

  now = 601000;
  vm.runInContext('setSchwabRetryPause(0)', context);
  for(const id of Object.keys(buttons).filter(id => id !== 'schwabRetryNotice')){
    assert.equal(buttons[id].disabled, false);
  }
  assert.equal(buttons.schwabRetryNotice.hidden, true);
  result = {ok: true, json: async () => ({auth_url: 'https://example.test/authorize'})};
  await vm.runInContext('openSchwabAuth()', context);
  assert.deepEqual(opened, [['https://example.test/authorize', '_blank']]);
  console.log('Schwab dashboard pause interactions passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
