'use strict';
// Execute the production network-diagnostic renderer in a VM; no browser or HTTP.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../dashboard/index.html'), 'utf8');
const start = html.indexOf('async function diagnoseSchwabNetwork(');
const diagnose = html.slice(start, html.indexOf('async function connectSchwab(', start));

function setup(payload) {
  const elements = new Map();
  const el = id => {
    if (!elements.has(id)) elements.set(id, {disabled:false, textContent:'', style:{}});
    return elements.get(id);
  };
  const notices = [], pauses = [];
  const context = vm.createContext({
    el, Date, Number, String, Math,
    toast:(...args)=>notices.push(args),
    setSchwabRetryPause:seconds=>pauses.push(seconds),
    api:async ()=>({ok:true, json:async ()=>payload}),
  });
  vm.runInContext('let schwabRetryUntil = 0;', context);
  vm.runInContext(diagnose, context);
  return {context, el, notices, pauses};
}

(async ()=>{
  const deferred = {request_sent:false, deferred:true, local_retry_after_seconds:16, http_status:0};
  const s = setup({
    status:'ok', conclusion:'LOCAL_BUDGET_BUSY', request_origin:'bot_server', execution_host:'vps',
    retry_after_seconds:0, local_retry_after_seconds:16, dns_addresses:['192.0.2.1'],
    probes:{authorize:deferred, token_endpoint:{http_status:401, oauth_response:true}, trader:deferred},
  });
  await s.context.diagnoseSchwabNetwork();
  const box = s.el('schwabNetworkDiagnostic');
  assert.match(box.textContent, /Sondas no enviadas: límite local de solicitudes ocupado \(LOCAL_BUDGET_BUSY\)/);
  assert.match(box.textContent, /Authorize: no enviada · límite local ocupado \(16 s\)/);
  assert.match(box.textContent, /Trader: no enviada · límite local ocupado \(16 s\)/);
  assert.match(box.textContent, /No es un fallo de red, DNS ni credenciales/);
  assert.doesNotMatch(box.textContent, /SchwabRequestDeferred/);
  assert.equal(box.style.borderColor, 'rgba(255,176,32,.45)', 'amber, not the red failure border');
  assert.equal(s.notices[0][0], 'info');
  assert.deepEqual(s.pauses, [], 'a local wait is not a Schwab HTTP pause');
  assert.equal(s.el('schwabNetworkDiagBtn').disabled, false);
  console.log('Dashboard local-budget diagnostic scenario passed.');
})().catch(error=>{ console.error(error); process.exit(1); });
