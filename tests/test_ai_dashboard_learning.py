"""Exercise the learning dashboard without starting Flask or contacting a broker."""

import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is required for dashboard checks")
class AILearningDashboardTests(unittest.TestCase):
    def test_learning_sources_economic_evidence_and_legacy_fallbacks(self):
        dashboard = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"
        script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
for(const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)){
  if(match[1].trim()) new vm.Script(match[1]);
}
const start=html.indexOf('function aiMetricNumber(');
const end=html.indexOf('// AI TRAINING',start);
assert(start>=0&&end>start,'Learning dashboard functions must be present');
let response;
const elements={};
const context=vm.createContext({
  S:{aiStatus:[],tickerExecution:[]},
  el:id=>elements[id]??=( {textContent:'',innerHTML:'',className:''} ),
  api:async()=>({json:async()=>response}),
  escHtml:value=>String(value??'').replace(/[&<>"']/g,
    c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
});
vm.runInContext(html.slice(start,end),context);
async function render(data){response={status:'ok',...data};await context.fetchAIStatus();}
(async()=>{
  await render({
    automatic_retraining:true,only_after_session:true,warning_samples:30,retrain_samples:60,
    summary:{pending:0,training:0},
    tickers:[{
      ticker:'AAPL',eligible_observations:50,eligible_closed_real:12,eligible_closed_paper:38,
      new_observations:50,retrain_samples:60,quarantined_observations:3,quarantined_closed_real:1,quarantined_closed_paper:2,
      training_readiness:{ready:false,reason:'Se requieren <60> cierres',min_samples:60,eligible_samples:50},
      learning_total:8,learning_resolved:6,learning_trainable:4,learning_open:2,
      last_evaluation:{muestras_fit_reales:30,muestras_fit_suplementarias:4,
        validation_test_actual_only:true,economic_gate_passed:false,
        economic_test:{evaluated:true,accepted_trades:6,net_r:-1.25,expectancy_r:-0.21,
          profit_factor:0.7,max_drawdown_r:2.5,round_trip_cost_bps:8}},
    }],
  });
  assert.equal(elements.aiVerifiedClosed.textContent,'50');
  assert.match(elements.aiClosedSources.textContent,/REAL 12 válidas \/ 1 excluidas · PAPER 38 válidas \/ 2 excluidas/);
  assert.equal(elements.aiResolvedSignals.textContent,'4');
  assert.match(elements.aiSignalSources.textContent,/6 resueltas · 2 pendientes · 2 excluidas/);
  assert.equal(elements.aiTrainingReady.textContent,'0/1');
  assert.match(elements.aiTrainingSchedule.textContent,/Después de la sesión NY/);
  assert.equal(elements.aiEconomicEvidence.textContent,'0/1');
  assert.match(elements.aiLifecycleRows.innerHTML,/50\/60/);
  assert.match(elements.aiLifecycleRows.innerHTML,/Se requieren &lt;60&gt; cierres/);
  assert.doesNotMatch(elements.aiLifecycleRows.innerHTML,/<60>/);
  assert.match(elements.aiLifecycleRows.innerHTML,/30 cierres \+ 4 señales/);
  assert.match(elements.aiLifecycleRows.innerHTML,/FAIL · -1\.25 R neto · 6 aprobadas/);
  assert.match(elements.aiLifecycleRows.innerHTML,/Coste ida\/vuelta 8\.00 pb/);
  assert.match(elements.aiLearningPolicy.textContent,/reentrenamiento: 60/);

  // Missing historical economics and source metadata must remain unknown.
  await render({retrain_samples:75,tickers:[{
    ticker:'OLD',new_observations:3,eligible_observations:9,last_fit_samples:5,
  }]});
  assert.match(elements.aiClosedSources.textContent,/REAL — válidas \/ — excluidas · PAPER — válidas \/ — excluidas/);
  assert.equal(elements.aiTrainingReady.textContent,'—');
  assert.equal(elements.aiEconomicEvidence.textContent,'Sin evaluar');
  assert.equal(elements.aiVerifiedClosed.textContent,'—');
  assert.match(elements.aiLifecycleRows.innerHTML,/3\/75/);
  assert.match(elements.aiLifecycleRows.innerHTML,/5 cierres \+ — señales/);
  assert.doesNotMatch(elements.aiLifecycleRows.innerHTML,/NaN|Infinity/);

  await render({tickers:[{
    ticker:'NVDA',eligible_closed_real:0,eligible_closed_paper:60,
    training_readiness:{ready:true,reason:'Datos listos; sólo evidencia SHADOW'},muestras_fit_reales:40,muestras_fit_suplementarias:0,
    economic_gate_passed:true,economic_test:{evaluated:true,accepted_trades:10,
      net_r:2.6,expectancy_r:0.26,profit_factor:null,no_loss_trades:true,max_drawdown_r:0,round_trip_cost_bps:0},
  }]});
  assert.equal(elements.aiTrainingReady.textContent,'1/1');
  assert.equal(elements.aiEconomicEvidence.textContent,'1/1');
  assert.match(elements.aiLifecycleRows.innerHTML,/40 cierres \+ 0 señales/);
  assert.match(elements.aiLifecycleRows.innerHTML,/PASS · 2\.60 R neto · 10 aprobadas/);
  assert.match(elements.aiLifecycleRows.innerHTML,/PF sin pérdidas/);
  assert.match(elements.aiLifecycleRows.innerHTML,/Datos listos; sólo evidencia SHADOW/);
  assert.match(elements.aiLifecycleRows.innerHTML,/Coste ida\/vuelta 0\.00 pb/);

  await render({summary:{eligible_observations:65},tickers:[{
    ticker:'MIXED',eligible_observations:65,eligible_closed_real:10,
    eligible_closed_paper:30,eligible_closed_legacy:25,
  }]});
  assert.equal(elements.aiVerifiedClosed.textContent,'40');
  assert.match(elements.aiClosedSources.textContent,/25 históricos sin verificación/);

  await render({tickers:[]});
  assert.match(elements.aiLifecycleRows.innerHTML,/colspan="20"/);
  assert.equal(elements.aiEconomicEvidence.textContent,'Sin evaluar');
  console.log('Learning dashboard checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(dashboard)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Learning dashboard checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()

