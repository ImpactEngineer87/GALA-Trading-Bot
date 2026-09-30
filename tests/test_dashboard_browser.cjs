// Real Chromium interactions against browser_fixture.py; no broker network.
// npm install --no-save playwright && npx playwright install chromium
// node --test tests/test_dashboard_browser.cjs
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),readline=require('node:readline');
const {spawn}=require('node:child_process');
const {chromium}=require(process.env.UI_PLAYWRIGHT_PATH||'playwright');
const root=path.resolve(__dirname,'..');
const fixture=()=>{
  const child=spawn(process.env.UI_PYTHON_BINARY||'python',[path.join(__dirname,'browser_fixture.py')],{cwd:root,stdio:['ignore','pipe','pipe']});
  let errors='';child.stderr.on('data',chunk=>errors+=chunk);
  const ready=new Promise((resolve,reject)=>{
    const timeout=setTimeout(()=>reject(new Error('Fixture did not start: '+errors)),15000);
    readline.createInterface({input:child.stdout}).on('line',line=>{try{const data=JSON.parse(line);if(data.url){clearTimeout(timeout);resolve(data.url);}}catch{}});
    child.once('exit',code=>{clearTimeout(timeout);reject(new Error('Fixture exited '+code+': '+errors));});
  });
  return {child,ready};
};

test('Editor 3 browser acceptance', {timeout:120000},async t=>{
  const f=fixture();let browser,page;const responses=[];
  try{
    const url=await f.ready;
    browser=await chromium.launch({headless:true,executablePath:process.env.UI_CHROMIUM_EXECUTABLE||undefined,
      args:['--no-sandbox','--disable-gpu','--disable-dev-shm-usage']});
    page=await browser.newPage({viewport:{width:1560,height:1080}});
    page.setDefaultTimeout(12000);
    page.on('response',async r=>{try{if(/\/api\/(login|ticker-execution)$/.test(r.url()))responses.push({url:r.url(),status:r.status(),body:await r.text()});}catch{}});
    const errors=[];page.on('pageerror',error=>errors.push(String(error)));
    page.on('dialog',dialog=>dialog.accept()); // fixture-only REAL activation confirmation
    await page.route('**/*',route=>route.request().url().startsWith(url)?route.continue():route.abort());
    const expectValue=async(id,value)=>page.waitForFunction(([id,value])=>document.getElementById(id)?.value===value,[id,value]);
    const api=async endpoint=>page.evaluate(async path=>(await (await api(path)).json()),endpoint);
    const state=async body=>page.evaluate(async data=>(await (await api('/api/test-fixture-state',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})).json()),body);
    const postAndClick=async(endpoint,selector)=>{
      const response=page.waitForResponse(r=>r.url()===url+endpoint&&r.request().method()==='POST');
      await page.locator(selector).click();return response;
    };
    await page.goto(url);
    await page.locator('#loginUser').fill('ui-test');await page.locator('#loginPass').fill('ui-test-password');
    await page.locator('.login-btn').click();
    await page.waitForFunction(()=>document.querySelectorAll('#tickerExecutionRows tr[data-ticker]').length===17);
    await expectValue('realShareLimit','3');

    await t.test('typing survives the actual 7-second and 10-second refresh timers',async()=>{
      await page.locator('#exec-shares-META').fill('');
      await page.evaluate(()=>window.originalEditNode=document.getElementById('exec-shares-META'));
      await page.waitForTimeout(11000);
      assert.equal(await page.locator('#exec-shares-META').inputValue(),'');
      assert.equal(await page.evaluate(()=>document.activeElement===window.originalEditNode&&document.getElementById('exec-shares-META')===window.originalEditNode),true);
      await page.locator('#exec-shares-META').pressSequentially('12');
      assert.equal(await page.locator('#exec-shares-META').inputValue(),'12');
      assert.equal(await page.locator('#exec-save-META').isDisabled(),true);
      assert.equal(await page.locator('#exec-raise-META').isVisible(),true);
      assert.match(await page.locator('#exec-feedback-META').textContent(),/máximo REAL guardado \(3\)/);
    });
    await t.test('raise the maximum without clamping or changing ticker quantities',async()=>{
      await page.locator('#exec-raise-META').click();await expectValue('realShareLimit','12');
      await page.locator('#realShareLimit').fill('20');await page.locator('#realShareLimit').blur();
      await page.evaluate(()=>fetchTickerExecution());await expectValue('realShareLimit','20');
      const response=await postAndClick('/api/live-settings','#saveRealShareLimit');assert.equal(response.status(),200);
      await page.waitForFunction(()=>!document.getElementById('exec-save-META').disabled);
      assert.equal((await api('/api/live-settings')).max_shares_per_order,20);
      assert.equal((await api('/api/ticker-execution')).tickers.find(t=>t.ticker==='META').shares,3);
      assert.equal(await page.locator('#exec-shares-META').inputValue(),'12');
      assert.equal(await page.locator('#exec-raise-META').isVisible(),false);
    });
    await t.test('save a REAL quantity and keep it after a full page reload',async()=>{
      const response=await postAndClick('/api/ticker-execution/META','#exec-save-META');assert.equal(response.status(),200);
      await page.waitForFunction(()=>document.getElementById('exec-feedback-META').textContent.includes('Guardado: REAL · 12'));
      const reload=await page.reload();assert.equal(reload.headers()['cache-control'],'no-store');
      await expectValue('exec-shares-META','12');await expectValue('realShareLimit','20');
      assert.equal(await page.locator('#exec-save-META').isDisabled(),true);
    });
    await t.test('ticker maximum saves independently with visible confirmation',async()=>{
      await page.locator('#realTickerLimit').fill('13');
      assert.equal((await postAndClick('/api/live-settings','#saveRealTickerLimit')).status(),200);
      await page.waitForFunction(()=>document.getElementById('realTickerLimitStatus').textContent.includes('Guardado: 13'));
      assert.match(await page.locator('#tickerExecutionSummary').textContent(),/5\/13/);
      assert.equal((await api('/api/live-settings')).max_shares_per_order,20);
    });
    await t.test('save errors retain the draft and show the actual blocking reason',async()=>{
      await state({flat:false});await page.locator('#exec-shares-META').fill('11');
      assert.equal((await postAndClick('/api/ticker-execution/META','#exec-save-META')).status(),409);
      await page.waitForFunction(()=>document.getElementById('exec-feedback-META').textContent.includes('posición REAL abierta'));
      await page.evaluate(()=>fetchTickerExecution());
      assert.equal(await page.locator('#exec-shares-META').inputValue(),'11');
      assert.equal((await api('/api/ticker-execution')).tickers.find(t=>t.ticker==='META').shares,12);
      await page.locator('#exec-discard-META').click();await expectValue('exec-shares-META','12');await state({flat:true});
      await state({reject_limits:true});await page.locator('#realShareLimit').fill('25');
      assert.equal((await postAndClick('/api/live-settings','#saveRealShareLimit')).status(),400);
      await page.waitForFunction(()=>document.getElementById('realShareLimitStatus').dataset.state==='error');
      assert.equal(await page.locator('#realShareLimit').inputValue(),'25');
      await state({reject_limits:false});await page.locator('#realShareLimit').fill('20');
      assert.equal((await postAndClick('/api/live-settings','#saveRealShareLimit')).status(),200);
    });
    await t.test('invalid quantities stay editable and cannot be submitted',async()=>{
      for(const value of ['2.5','0','']){
        await page.locator('#exec-shares-META').fill(value);
        assert.equal(await page.locator('#exec-shares-META').inputValue(),value);
        assert.equal(await page.locator('#exec-save-META').isDisabled(),true);
      }
      await page.locator('#exec-discard-META').click();await expectValue('exec-shares-META','12');
    });
    await t.test('one pending save cannot submit twice or lose its draft to polling',async()=>{
      let release,started;const waiting=new Promise(r=>started=r),gate=new Promise(r=>release=r);let posts=0;
      await page.route('**/api/ticker-execution/META',async route=>{
        if(route.request().method()==='POST'){posts++;started();await gate;}await route.continue();
      });
      await page.locator('#exec-shares-META').fill('11');
      const response=postAndClick('/api/ticker-execution/META','#exec-save-META');await waiting;
      assert.equal(await page.locator('#exec-save-META').isDisabled(),true);
      await page.evaluate(()=>applyTickerExecution('META'));await page.evaluate(()=>fetchTickerExecution());
      assert.equal(await page.locator('#exec-shares-META').inputValue(),'11');assert.equal(posts,1);
      release();assert.equal((await response).status(),200);
      await page.waitForFunction(()=>document.getElementById('exec-feedback-META').textContent.includes('Guardado: REAL · 11'));
      await page.unroute('**/api/ticker-execution/META');
    });
    await t.test('an old poll cannot replace a freshly saved maximum',async()=>{
      let release,captured;const gate=new Promise(r=>release=r),ready=new Promise(r=>captured=r);let held=false;
      await page.route('**/api/ticker-execution',async route=>{
        if(!held){held=true;const response=await route.fetch();captured();await gate;await route.fulfill({response});}
        else await route.continue();
      });
      const oldPoll=page.evaluate(()=>fetchTickerExecution());await ready;
      await page.locator('#realShareLimit').fill('30');
      assert.equal((await postAndClick('/api/live-settings','#saveRealShareLimit')).status(),200);
      await page.waitForFunction(()=>document.getElementById('realShareLimitStatus').textContent.includes('Guardado: 30'));
      release();await oldPoll;await page.unroute('**/api/ticker-execution');
      await expectValue('realShareLimit','30');
    });
    await t.test('external changes require reviewing a fresh value before saving',async()=>{
      await page.locator('#exec-shares-META').fill('16');await state({external_shares:9});await page.evaluate(()=>fetchTickerExecution());
      assert.equal(await page.locator('#exec-shares-META').inputValue(),'16');
      assert.equal(await page.locator('#exec-save-META').isDisabled(),true);
      assert.match(await page.locator('#exec-feedback-META').textContent(),/otra sesión/);
      await page.locator('#exec-discard-META').click();await expectValue('exec-shares-META','9');
    });
    await t.test('engine state locks editing without destroying unfinished values',async()=>{
      await page.locator('#exec-shares-META').fill('14');await state({running:true});
      await page.evaluate(async()=>{await fetchStatus();await fetchTickerExecution();});
      for(const id of ['realShareLimit','realTickerLimit','exec-shares-META','exec-mode-META','exec-save-META'])assert.equal(await page.locator('#'+id).isDisabled(),true);
      assert.equal(await page.locator('#exec-shares-META').inputValue(),'14');
      await state({running:false});await page.evaluate(async()=>{await fetchStatus();await fetchTickerExecution();});
      assert.equal(await page.locator('#exec-shares-META').isDisabled(),false);
      await page.locator('#exec-discard-META').click();await expectValue('exec-shares-META','9');
    });
    await t.test('a ticker ceiling above the watchlist size saves without activating symbols',async()=>{
      await page.locator('#realTickerLimit').fill('25');
      assert.equal((await postAndClick('/api/live-settings','#saveRealTickerLimit')).status(),200);
      await page.waitForFunction(()=>document.getElementById('realTickerLimitStatus').textContent.includes('Guardado: 25'));
      await page.reload();await expectValue('realTickerLimit','25');
      assert.match(await page.locator('#realSelectionCount').textContent(),/5 tickers/);
      assert.match(await page.locator('#realSelectionSlots').textContent(),/20 cupos libres/);
      assert.equal((await api('/api/ticker-execution')).tickers.filter(t=>t.mode==='REAL').length,5);
    });
    await t.test('raise a full ticker ceiling and actually save ten REAL selections',async()=>{
      await page.locator('#realTickerLimit').fill('5');
      assert.equal((await postAndClick('/api/live-settings','#saveRealTickerLimit')).status(),200);
      await page.waitForFunction(()=>document.getElementById('realTickerLimitStatus').textContent.includes('Guardado: 5'));
      await page.locator('#exec-mode-AAPL').selectOption('REAL');await page.locator('#exec-shares-AAPL').fill('3');
      assert.equal(await page.locator('#exec-save-AAPL').isDisabled(),true);
      assert.match(await page.locator('#exec-feedback-AAPL').textContent(),/Sin cupos REAL: 5\/5/);
      await page.locator('#exec-raise-tickers-AAPL').click();await expectValue('realTickerLimit','6');
      await page.locator('#realTickerLimit').fill('10');
      assert.equal((await postAndClick('/api/live-settings','#saveRealTickerLimit')).status(),200);
      await page.waitForFunction(()=>!document.getElementById('exec-save-AAPL').disabled);
      assert.equal((await api('/api/live-settings')).max_shares_per_order,30);
      for(const ticker of ['AAPL','AMD','AMZN','BA','RCL']){
        await page.locator('#exec-mode-'+ticker).selectOption('REAL');await page.locator('#exec-shares-'+ticker).fill('3');
        assert.equal((await postAndClick('/api/ticker-execution/'+ticker,'#exec-save-'+ticker)).status(),200);
        await page.waitForFunction(ticker=>document.getElementById('exec-feedback-'+ticker).textContent.includes('Guardado: REAL · 3'),ticker);
      }
      await page.reload();await expectValue('realTickerLimit','10');
      const saved=(await api('/api/ticker-execution')).tickers.filter(row=>row.mode==='REAL');
      assert.equal(saved.length,10);
      assert.equal(await page.locator('#realSelectionTickers .REAL').count(),10);
      assert.match(await page.locator('#realSelectionSlots').textContent(),/0 cupos libres/);
      assert.equal(saved.find(row=>row.ticker==='META').shares,9);
    });
    await t.test('a conflicting reduction shows the required number to move to PAPER',async()=>{
      await page.locator('#realTickerLimit').fill('3');
      assert.equal((await postAndClick('/api/live-settings','#saveRealTickerLimit')).status(),400);
      await page.waitForFunction(()=>document.getElementById('realTickerLimitStatus').dataset.state==='error');
      assert.match(await page.locator('#realTickerLimitStatus').textContent(),/Cambie 7 a PAPER/);
      assert.equal((await api('/api/live-settings')).max_real_tickers,10);
      await page.locator('#realTickerLimit').fill('10');
      assert.equal((await postAndClick('/api/live-settings','#saveRealTickerLimit')).status(),200);
      await page.waitForFunction(()=>document.getElementById('realTickerLimitStatus').textContent.includes('Guardado: 10'));
    });
    await t.test('model details use the dark theme and all controls fit on mobile',async()=>{
      await page.locator('#exec-details-META').click();await page.locator('#v330Dialog[open]').waitFor();
      assert.notEqual(await page.locator('#v330Dialog .btn').first().evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(239, 239, 239)');
      await page.locator('#v330Dialog button[aria-label="Cerrar"]').click();
      const screenshotDir=process.env.UI_SCREENSHOT_DIR;
      await page.locator('#tickerExecutionCard').scrollIntoViewIfNeeded();
      await page.evaluate(()=>document.getElementById('tickerExecutionCard').scrollIntoView({block:'start'}));
      if(screenshotDir){await page.waitForTimeout(4000);fs.mkdirSync(screenshotDir,{recursive:true});await page.screenshot({path:path.join(screenshotDir,'editor-desktop.png')});}
      await page.setViewportSize({width:390,height:844});
      await page.evaluate(()=>document.getElementById('tickerExecutionCard').scrollIntoView({block:'start'}));
      assert.equal(await page.locator('#tickerExecutionRows tr').first().evaluate(el=>getComputedStyle(el).display),'grid');
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),true,JSON.stringify(await page.evaluate(()=>Array.from(document.querySelectorAll('body *')).filter(el=>el.getBoundingClientRect().right>innerWidth+1&&el.getBoundingClientRect().width>0).slice(0,12).map(el=>({tag:el.tagName,id:el.id,class:el.className,right:el.getBoundingClientRect().right})))));
      await page.locator('#exec-shares-META').fill('10');
      assert.equal((await postAndClick('/api/ticker-execution/META','#exec-save-META')).status(),200);
      await page.waitForFunction(()=>document.getElementById('exec-feedback-META').textContent.includes('Guardado: REAL · 10'));
      if(screenshotDir){await page.evaluate(()=>document.getElementById('tickerExecutionCard').scrollIntoView({block:'start'}));await page.screenshot({path:path.join(screenshotDir,'editor-mobile.png')});}
      assert.deepEqual(errors,[]);
    });
  }catch(error){
    if(page){console.error(JSON.stringify({responses,ui:await page.evaluate(()=>({login:document.getElementById('loginErr')?.textContent,status:document.getElementById('execEditStatus')?.textContent,rows:S.tickerExecution,html:document.getElementById('tickerExecutionRows')?.innerHTML.slice(0,500)}))}));if(process.env.UI_SCREENSHOT_DIR){fs.mkdirSync(process.env.UI_SCREENSHOT_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.UI_SCREENSHOT_DIR,'failure.png')});}}
    throw error;
  }finally{if(browser)await browser.close();f.child.kill();}
});
