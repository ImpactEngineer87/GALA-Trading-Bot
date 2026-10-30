"""Offline reporting checks for saved routing versus actual entry blockers."""
import copy
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import server


class TickerExecutionReportingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"live_trading": {"per_ticker_execution": True}}
        self.rows = [
            {"ticker": "BA", "mode": "REAL", "effective_mode": "REAL", "shares": 1},
            {"ticker": "AAPL", "mode": "PAPER", "effective_mode": "PAPER", "shares": 5},
        ]
        self.risk = {"blocked": False, "real_accounting": {
            "blocked": True, "entry_blocked": True,
        }}
        self.engine = SimpleNamespace(_ticker_quarantine={}, real_capacity=None)
        self.enterContext(patch.object(server, "_ENGINE", self.engine))
        self.enterContext(patch.object(server, "_read_config_runtime", side_effect=lambda: self.cfg))
        self.enterContext(patch.object(server, "_ticker_execution_rows",
                                       side_effect=lambda *a, **k: copy.deepcopy(self.rows)))
        self.enterContext(patch.object(server, "_engine_daily_risk_snapshot",
                                       side_effect=lambda: copy.deepcopy(self.risk)))
        # The report must neither consult Schwab nor submit/cancel an order.
        self.http = self.enterContext(patch.object(server.requests, "get",
            side_effect=AssertionError("report attempted a broker request")))

    def report(self):
        with server.app.test_request_context("/api/ticker-execution"):
            response = server.get_ticker_execution()
        self.assertEqual(response.status_code, 200)
        self.http.assert_not_called()
        return response.get_json()

    def test_accounting_hold_blocks_real_only_without_changing_saved_routing(self):
        report = self.report()
        real, paper = report["tickers"]
        self.assertEqual(real["mode"], "REAL")
        self.assertTrue(real["entry_blocked"])
        self.assertIn("REAL_ACCOUNTING_UNRESOLVED", [x["code"] for x in real["entry_blockers"]])
        self.assertFalse(paper["entry_blocked"])
        self.assertEqual(paper["mode"], "PAPER")
        self.assertEqual(report["max_paper_shares"], 100000)
        self.assertFalse(report["real_partial_exit_supported"])

    def test_reviewed_history_does_not_hide_immutable_startup_quarantine(self):
        self.risk["real_accounting"].update(entry_blocked=False, historical_resume_active=True)
        self.engine._ticker_quarantine["BA"] = "REAL_ACCOUNTING_UNRESOLVED"
        real = self.report()["tickers"][0]
        self.assertTrue(real["entry_blocked"])
        self.assertEqual([x["code"] for x in real["entry_blockers"]], ["REAL_STARTUP_QUARANTINE"])
        self.engine._ticker_quarantine.clear()
        real = self.report()["tickers"][0]
        self.assertFalse(real["entry_blocked"])
        self.assertEqual(real["entry_status"], "NO_KNOWN_BLOCK")

    def test_partial_exit_policy_is_exposed_before_real_order_submission(self):
        self.risk["real_accounting"].update(blocked=False, entry_blocked=False)
        self.rows[0]["shares"] = 3
        real, paper = self.report()["tickers"]
        self.assertTrue(real["partial_plan"]["enabled"])
        self.assertEqual(real["entry_blockers"][0]["code"], "REAL_PARTIAL_EXIT_UNSUPPORTED")
        self.assertFalse(paper["entry_blocked"])
        self.cfg["salida_parcial"] = {"activo": False}
        self.assertFalse(self.report()["tickers"][0]["entry_blocked"])

    def test_legacy_accounting_and_unavailable_status_are_not_reported_as_clear(self):
        del self.risk["real_accounting"]["entry_blocked"]
        self.assertTrue(self.report()["tickers"][0]["entry_blocked"])
        del self.risk["real_accounting"]
        real = self.report()["tickers"][0]
        self.assertEqual(real["entry_blockers"][0]["code"], "REAL_ACCOUNTING_UNAVAILABLE")
        self.risk["real_accounting"] = {"entry_blocked": None}
        self.assertTrue(self.report()["tickers"][0]["entry_blocked"])

    def test_full_position_real_policy_preserves_paper_partial_plan(self):
        self.risk["real_accounting"].update(entry_blocked=False)
        self.cfg["live_trading"]["real_exit_policy"] = "FULL_POSITION"
        self.rows[0]["shares"] = 10
        real, paper = self.report()["tickers"]
        self.assertFalse(real["entry_blocked"])
        self.assertFalse(real["partial_plan"]["enabled"])
        self.assertEqual(real["partial_plan"]["partial_shares"], 0)
        self.assertEqual(real["partial_plan"]["runner_shares"], 10)
        self.assertTrue(paper["partial_plan"]["enabled"])
        self.assertEqual(self.report()["real_exit_policy"], "FULL_POSITION")

    def test_daily_loss_blocks_both_books_and_configured_paper_limit_is_respected(self):
        self.risk["blocked"] = True
        self.cfg["live_trading"]["max_paper_shares_per_order"] = 300
        report = self.report()
        self.assertEqual(report["max_paper_shares"], 300)
        for row in report["tickers"]:
            self.assertIn("DAILY_LOSS_BLOCK", [x["code"] for x in row["entry_blockers"]])


@unittest.skipUnless(shutil.which("node"), "Node.js is required for dashboard checks")
class TickerExecutionRenderingTests(unittest.TestCase):
    def test_reviewed_history_blocked_entries_and_partial_plans_render_honestly(self):
        dashboard = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"
        script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
for(const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)) {
  if(match[1].trim()) new vm.Script(match[1]);
}
const elements={};
const S={tickerExecutionMeta:{real_partial_exit_supported:false},botRunning:true};
const context=vm.createContext({S,document:{getElementById:id=>elements[id]},
  el:id=>elements[id]??={textContent:'',style:{},classList:{toggle:()=>{}}},
  money:value=>value==null?'N/D':String(value),
  renderRealLimitControls:()=>{},renderTickerExecution:()=>{},
});
function load(start,end){const a=html.indexOf(start),b=html.indexOf(end,a);assert(a>=0&&b>a);vm.runInContext(html.slice(a,b),context);}
load('function updateDailyRisk(risk){','function updateBrokerReconciliation(');
load('function partialPlanForShares(','function renderExecPartialPlan(');
load('function tickerEntryStatus(row){','function renderTickerExecution(');
load('function updateBotUI(running,runtime){','async function fetchStatus(){');
context.el('botToggle');context.el('botStatus');
const risk={real_accounting:{blocked:true,entry_blocked:false,historical_resume_active:true}};
context.updateDailyRisk(risk);
assert.match(elements.dailyRiskState.textContent,/HISTÓRICA PENDIENTE/);
assert.doesNotMatch(elements.dailyRiskState.textContent,/REAL BLOQUEADAS/);
assert.match(elements.dailyRiskDetail.textContent,/sin resolver/);
risk.real_accounting.entry_blocked=true;
context.updateDailyRisk(risk);
assert.match(elements.dailyRiskState.textContent,/REAL BLOQUEADAS/);
delete risk.real_accounting.entry_blocked;
context.updateDailyRisk(risk);
assert.match(elements.dailyRiskState.textContent,/REAL BLOQUEADAS/);
assert.match(context.tickerEntryStatus({mode:'REAL',entry_blocked:true,entry_blockers:[{message:'Contabilidad pendiente'}]}),/entradas bloqueadas.*Contabilidad pendiente/);
assert.match(context.tickerEntryStatus({mode:'PAPER',entry_status:'NO_KNOWN_BLOCK'}),/sin bloqueo conocido/);
assert.match(context.tickerEntryStatus({mode:'REAL'}),/no verificable/);
assert.equal(context.partialPlanForShares(3,'REAL').blocked,true);
assert.match(context.partialPlanForShares(3,'REAL').text,/REAL bloqueado/);
assert.equal(context.partialPlanForShares(3,'PAPER').text,'TP1 2 + resto 1');
assert.equal(context.partialPlanForShares(2,'REAL').enabled,false);
S.tickerExecutionMeta.real_exit_policy='FULL_POSITION';
assert.equal(context.partialPlanForShares(10,'REAL').enabled,false);
assert.match(context.partialPlanForShares(10,'REAL').text,/Salida completa: 10/);
assert.equal(context.partialPlanForShares(10,'PAPER').text,'TP1 7 + resto 3');
S.tickerExecutionMeta.bot_running=true;
context.updateBotUI(false,{state:'STOPPED'});
assert.equal(S.botRunning,false);assert.equal(S.tickerExecutionMeta.bot_running,false);
assert.equal(S.execRevision,1);
console.log('Ticker execution reporting rendering passed');
"""
        result = subprocess.run([shutil.which("node"), "-e", script, str(dashboard)],
                                capture_output=True, text=True, timeout=15, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
