'use strict';
// Execute production control functions in a VM; no browser, broker, or HTTP.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../dashboard/index.html'), 'utf8');
const control = html.slice(html.indexOf('let botControlPending = false;'),
  html.indexOf('// FETCH STATUS', html.indexOf('let botControlPending = false;')));
const reconciliation = html.slice(html.indexOf('function updateBrokerReconciliation('),
  html.indexOf('async function refreshBrokerReconciliation('));
const pause = html.slice(html.indexOf('function setSchwabRetryPause('),
  html.indexOf('async function checkSchwabStatus('));

function setup() {
  const elements = new Map();
  const el = id => {
    if (!elements.has(id)) elements.set(id, {
      checked:false, disabled:false, hidden:false, textContent:'', style:{},
      classList:{add(){}, remove(){}, toggle(){}},
    });
    return elements.get(id);
  };
  const calls = [], notices = [];
  const context = vm.createContext({
    S:{botRunning:true}, Date, Promise, console, el,
    document:{getElementById:el},
    toast:(...args)=>notices.push(args),
    api:async (url, options)=>{
      calls.push({url, options});
      return {ok:false, json:async ()=>({running:false, message:'Broker exposure unresolved'})};
    },
    fetchStatus:async ()=>{}, fetchTrades:async ()=>{},
  });
  vm.runInContext('let schwabRetryUntil = 0;', context);
  vm.runInContext(control + reconciliation + pause, context);
  return {context, calls, notices, el};
}

(async ()=>{
  let count = 0;
  {
    const s=setup(), toggle=s.el('botToggle');
    toggle.checked=false;
    await s.context.toggleBot(toggle);
    assert.equal(s.calls[0].url, '/api/bot/stop');
    assert.equal(toggle.checked, false, 'failed flatten must not flip engine back on');
    assert.equal(s.context.S.botRunning, false);
    assert.equal(s.el('botStatus').textContent, 'PARADO');
    assert.equal(toggle.disabled, false);
    assert.equal(s.notices[0][0], 'err');
    count++;
  }
  {
    const s=setup(), toggle=s.el('botToggle');
    toggle.checked=true;
    s.context.api=async ()=>({ok:false, json:async ()=>({message:'Start blocked'})});
    let statusReads=0;
    s.context.fetchStatus=async ()=>{statusReads++;s.context.updateBotUI(false);};
    await s.context.toggleBot(toggle);
    assert.equal(statusReads, 1);
    assert.equal(toggle.checked, false);
    count++;
  }
  {
    const s=setup(), toggle=s.el('botToggle');
    s.context.api=async ()=>{throw new Error('timeout');};
    s.context.fetchStatus=async ()=>s.context.updateBotUI(false);
    await s.context.toggleBot(toggle);
    assert.equal(toggle.checked, false, 'unknown outcome must read server state');
    assert.equal(toggle.disabled, false);
    count++;
  }
  {
    const s=setup(), toggle=s.el('botToggle');
    let release;
    s.context.api=()=>{
      s.calls.push('one request');
      return new Promise(resolve=>{release=resolve;});
    };
    const pending=s.context.toggleBot(toggle);
    await s.context.toggleBot(toggle);
    assert.equal(s.calls.length, 1, 'duplicate controls must not send a second request');
    assert.equal(toggle.disabled, true);
    release({ok:false,json:async ()=>({running:false})});
    await pending;
    count++;
  }
  {
    const s=setup();
    s.context.S.botRunning=false;
    s.context.updateBrokerReconciliation({ok:false,flat:false,message:'HTTP 401'});
    assert.match(s.el('brokerSyncTitle').textContent, /cierre REAL no confirmado/);
    assert.equal(s.el('brokerCloseRetry').hidden, false);
    assert.equal(s.calls.length, 0, 'rendering exposure must never send a close');
    count++;
  }
  {
    const s=setup();
    s.context.S.botRunning=false;
    s.context.updateBrokerReconciliation({ok:true,flat:true});
    assert.equal(s.el('brokerCloseRetry').hidden, true);
    assert.equal(s.calls.length, 0);
    count++;
  }
  {
    const s=setup();
    s.context.S.botRunning=false;
    s.context.setSchwabRetryPause(600);
    assert.equal(s.el('brokerCloseRetry').disabled, true);
    assert.equal(s.el('brokerSyncRefresh').disabled, true);
    await s.context.retryStoppedRealClose();
    assert.equal(s.calls.length, 0, 'cooldown must block explicit recovery locally');
    count++;
  }
  {
    const s=setup();
    s.context.S.botRunning=false;
    await s.context.retryStoppedRealClose();
    assert.equal(s.calls.length, 1);
    assert.equal(s.calls[0].url, '/api/bot/stop');
    assert.equal(s.context.S.botRunning, false);
    assert.equal(s.notices[0][0], 'err');
    count++;
  }
  console.log(`${count} dashboard control scenarios passed`);
})().catch(err=>{console.error(err);process.exitCode=1;});
