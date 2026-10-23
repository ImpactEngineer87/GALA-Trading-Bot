"""Execute dashboard rendering offline, including unknown and mixed-source data."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is required for dashboard checks")
class DashboardProvenanceRenderingTests(unittest.TestCase):
    def test_modes_unavailable_numbers_and_source_labels_survive_rendering(self):
        dashboard = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"
        script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
for(const m of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)) {
  if(m[1].trim()) new vm.Script(m[1]);
}
const elements={}; let payload, requestUrl;
const context=vm.createContext({
  el:id=>elements[id]??={textContent:'',innerHTML:'',style:{},value:''},
  escHtml:v=>String(v??'').replace(/</g,'&lt;').replace(/>/g,'&gt;'),
  api:async url=>{requestUrl=url;return {ok:true,json:async()=>payload};},
});
let a=html.indexOf('async function fetchTickerPerformance(){');
let b=html.indexOf('function renderTradeEvents(',a);
vm.runInContext(html.slice(a,b),context);
a=html.indexOf('async function fetchTickerAnalysis(){');
b=html.indexOf('async function fetchHistorial(){',a);
vm.runInContext(html.slice(a,b),context);
(async()=>{
  assert.equal(context.money(null),'N/D');
  assert.equal(context.money(undefined),'N/D');
  assert.equal(context.money('NaN'),'N/D');
  assert.equal(context.money(0),'$0.00');
  payload={status:'ok',date:'2026-10-22',summary:{by_mode:{PAPER:{total_pnl:500},REAL:{total_pnl:null}}},
    rows:[{ticker:'META',mode:'REAL',mode_breakdown:{
      PAPER:{closed_trades:1,realized_pnl:500,unrealized_pnl:0,total_pnl:500},
      REAL:{closed_trades:0,excluded_closed_trades:1,realized_pnl:null,unrealized_pnl:null,total_pnl:null},
    }}]};
  await context.fetchTickerPerformance();
  assert.match(elements.tickerPerfSummary.textContent,/PAPER simulado \$500.00/);
  assert.match(elements.tickerPerfSummary.textContent,/REAL bruto local N\/D/);
  assert.match(elements.tickerPerformanceGrid.innerHTML,/Simulación PAPER/);
  assert.match(elements.tickerPerformanceGrid.innerHTML,/no P\/L Schwab/);
  assert.match(elements.tickerPerformanceGrid.innerHTML,/Sin verificar \/ excluidos <b>1/);
  context.renderPositions('positions',[{ticker:'META',mode:'REAL',status:'OPEN',
    entry_price:null,last_price:0,floating_pnl:null,valuation_available:false,
    initial_quantity:5,remaining_quantity:5}],false);
  assert.match(elements.positions.innerHTML,/Flotante estimado local/);
  assert.match(elements.positions.innerHTML,/valoración no disponible/);
  assert.doesNotMatch(elements.positions.innerHTML,/\$0\.00/);

  context.el('tickerAnalysisMode').value='REAL';
  payload={status:'ok',evidence_mode:'REAL',summary:{},rows:[{
    ticker:'META',mode:'REAL',configured_mode:'PAPER',excluded_closed_trades:4,
    sample_eligible:false,net_pnl:null,net_return_pct:null,
  }]};
  await context.fetchTickerAnalysis();
  assert.match(requestUrl,/mode=REAL/);
  assert.match(elements.tickerAnalysisGrid.innerHTML,/EVIDENCIA INCOMPLETA/);
  assert.match(elements.tickerAnalysisGrid.innerHTML,/Excluido del ranking/);
  context.el('tickerAnalysisMode').value='PAPER';
  await context.fetchTickerAnalysis();
  assert.match(requestUrl,/mode=PAPER/);
  console.log('Dashboard provenance rendering passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which("node"), "-e", script, str(dashboard)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
