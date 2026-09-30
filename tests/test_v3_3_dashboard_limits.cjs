// Dependency-free state/transport checks. Actual DOM/timers are exercised by
// test_dashboard_browser.cjs against Flask and Chromium.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {test}=require('node:test');
const html=fs.readFileSync(path.join(__dirname,'../dashboard/index.html'),'utf8');
const extract=(start,end)=>html.slice(html.indexOf(start),html.indexOf(end));
const code=extract('async function api(','function showLogin(')+
  extract('function realLimitControls(','function showV330Dialog(')+
  extract('async function fetchTickerExecution(','async function applyTickerExecution(');
function setup(){
  const elements={};
  for(const id of ['realShareLimit','realTickerLimit','saveRealShareLimit','saveRealTickerLimit','realShareLimitStatus','realTickerLimitStatus','realCapacityState','execEditStatus','exec-mode-META','exec-shares-META']){
    let value='';elements[id]={dataset:{},disabled:false,style:{},get value(){return value;},set value(v){value=String(v);}};
  }
  const state={botRunning:false,tickerExecution:[],tickerExecutionMeta:{},execDrafts:{},execNotices:{}};
  const meta={status:'ok',bot_running:false,max_real_tickers:2,max_real_shares:3,max_paper_shares:1000,tickers:[{ticker:'META',mode:'REAL',shares:3,updated_utc:'original'},{ticker:'NVDA',mode:'PAPER',shares:20,updated_utc:'original'}]};
  const calls=[],notices=[];
  const context=vm.createContext({S:state,Headers,csrfToken:'test-csrf',showLogin:()=>{},document:{activeElement:null},el:id=>elements[id],
    escHtml:v=>String(v??''),toast:(...args)=>notices.push(args),
    fetch:async(url,options={})=>{
      if(options.method==='POST'){
        assert.equal(options.headers.get('Content-Type'),'application/json');assert.equal(options.headers.get('X-CSRF-Token'),'test-csrf');
        const body=JSON.parse(options.body);calls.push({url,body});
        if('max_shares_per_order' in body)meta.max_real_shares=body.max_shares_per_order;
        if('max_real_tickers' in body)meta.max_real_tickers=body.max_real_tickers;
        return {ok:true,json:async()=>({status:'ok',max_real_tickers:meta.max_real_tickers,max_shares_per_order:meta.max_real_shares})};
      }
      return {ok:true,json:async()=>structuredClone(meta)};
    }});
  vm.runInContext(code,context);
  context.renderTickerExecution=()=>{}; // DOM coverage is in the real-browser suite.
  return {context,elements,state,meta,calls,notices};
}
test('share maximum saves independently without changing saved ticker quantities',async()=>{
  const {context,elements,meta,calls}=setup();await context.fetchTickerExecution();
  elements.realShareLimit.value='20';context.markRealLimitDraft('max_shares_per_order');
  await context.fetchTickerExecution();assert.equal(elements.realShareLimit.value,'20');
  await context.saveRealLimit('max_shares_per_order');
  assert.deepEqual(calls,[{url:'/api/live-settings',body:{max_shares_per_order:20}}]);
  assert.equal(meta.max_real_tickers,2);assert.equal(meta.tickers[0].shares,3);
  assert.equal(elements.realShareLimit.dataset.dirty,undefined);
  assert.match(elements.realShareLimitStatus.textContent,/Guardado: 20/);
});
test('running state locks both maximum controls and blocks submissions',async()=>{
  const {context,elements,meta,calls}=setup();meta.bot_running=true;await context.fetchTickerExecution();
  for(const id of ['realShareLimit','realTickerLimit','saveRealShareLimit','saveRealTickerLimit'])assert.equal(elements[id].disabled,true);
  await context.saveRealLimit('max_shares_per_order');assert.equal(calls.length,0);
});
test('ticker maximum is independent; invalid quantities are not submitted',async()=>{
  const {context,elements,meta,calls}=setup();await context.fetchTickerExecution();
  elements.realTickerLimit.value='2';await context.saveRealLimit();
  assert.deepEqual(calls[0].body,{max_real_tickers:2});assert.equal(meta.max_real_shares,3);
  elements.realShareLimit.value='2.5';await context.saveRealLimit('max_shares_per_order');assert.equal(calls.length,1);
  assert.equal(elements.realShareLimitStatus.dataset.state,'error');
});
test('typing above the cap is retained as a draft instead of clamped',async()=>{
  const {context,elements,state}=setup();await context.fetchTickerExecution();
  elements['exec-mode-META'].value='REAL';elements['exec-shares-META'].value='12';context.updateExecShareLimit('META');
  assert.equal(elements['exec-shares-META'].value,'12');assert.equal(state.execDrafts.META.shares,'12');
  assert.match(context.execRowIssue('META',state.execDrafts.META),/máximo REAL guardado \(3\)/);
  assert.equal(state.tickerExecution[0].shares,3);
  elements['exec-shares-META'].value='';context.updateExecShareLimit('META');assert.equal(state.execDrafts.META.shares,'');
});
test('older GET responses cannot overwrite newer configuration',async()=>{
  const {context,state}=setup();let resolveOld;
  context.fetch=()=>new Promise(resolve=>resolveOld=resolve);
  const pending=context.fetchTickerExecution();
  state.execRevision=(state.execRevision||0)+1;state.tickerExecutionMeta={max_real_shares:20};
  resolveOld({ok:true,json:async()=>({status:'ok',max_real_shares:3,tickers:[]})});await pending;
  assert.equal(state.tickerExecutionMeta.max_real_shares,20);
});

test('partial-exit previews match engine rounding for half-share boundaries',async()=>{
  const {context}=setup();await context.fetchTickerExecution();
  for(const [qty,partial] of [[3,2],[5,4],[15,10],[25,18],[35,24]]){
    assert.equal(context.partialPlanForShares(qty).partial,partial);
    assert.equal(context.partialPlanForShares(qty).runner,qty-partial);
  }
});
