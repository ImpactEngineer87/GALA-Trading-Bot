"""Actual signal API/rendering, with no broker queries or ledger rewrites."""
import copy
import csv
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core.signal_execution_evidence import annotate_signals, read_signal_audit


def signal(**change):
    return dict(ticker='BA', execution_mode='REAL', signal_id='sig1',
                date='2026-10-23', precio=184.43, prob_ia=.9,
                ai_threshold=.5, ai_decision='APPROVED', **change)


def event(kind, **change):
    row = dict(ticker='BA', modo='REAL', signal_id='sig1', fecha='2026-10-23',
               evento=kind, order_id='', motivo='', estado='FILLED' if kind == 'ENTRY_FILLED' else '')
    row.update(change)
    return row


class ExecutionVisibilityTests(unittest.TestCase):
    def state(self, rows, value=None):
        return annotate_signals([value or signal()], rows)[0]['execution_evidence']

    def test_high_probability_is_never_a_submission_or_fill(self):
        result = self.state([])
        self.assertEqual(result['state'], 'UNLINKED')
        self.assertFalse(result['is_live_broker_query'])
        self.assertFalse(result['is_price_or_quantity_verification'])

    def test_exact_accounting_block_is_visible_without_modifying_ai_decision(self):
        signals = [signal()]
        events = [event('ENTRY_BLOCKED', motivo='REAL_ACCOUNTING_UNRESOLVED')]
        before = copy.deepcopy((signals, events))
        result = annotate_signals(signals, events)[0]
        self.assertEqual(result['execution_evidence']['reason_code'], 'REAL_ACCOUNTING_UNRESOLVED')
        self.assertEqual(result['execution_evidence']['state'], 'BLOCKED')
        self.assertEqual(result['ai_decision'], 'APPROVED')
        self.assertEqual((signals, events), before)

    def test_other_ticker_mode_signal_or_session_cannot_supply_a_fill(self):
        for change in ({'ticker': 'NKE'}, {'modo': 'PAPER'}, {'signal_id': 'other'},
                       {'fecha': '2026-10-22'}, {'signal_id': ''}):
            with self.subTest(change=change):
                self.assertEqual(self.state([event('ENTRY_FILLED', order_id='private', **change)])['state'], 'UNLINKED')

    def test_missing_signal_identity_cannot_be_inferred_from_same_price(self):
        value = signal()
        value['signal_id'] = ''
        self.assertEqual(self.state([event('ENTRY_FILLED', order_id='private')], value)['state'], 'UNLINKED')

    def test_submitted_and_uncertain_are_not_fills(self):
        self.assertEqual(self.state([event('ENTRY_SUBMITTED', order_id='private')])['state'], 'SUBMITTED')
        self.assertEqual(self.state([event('ENTRY_RECONCILE_REQUIRED')])['state'], 'UNCERTAIN')
        self.assertEqual(self.state([event('ENTRY_FILLED')])['state'], 'UNCERTAIN')

    def test_working_or_simulated_status_cannot_supply_a_real_fill(self):
        for state in ('WORKING', 'SIMULATED_FILLED', '', 'ERROR'):
            self.assertEqual(self.state([event('ENTRY_FILLED', order_id='private', estado=state)])['state'], 'UNCERTAIN')

    def test_timeout_does_not_claim_zero_fill_or_still_working(self):
        result = self.state([event('ENTRY_SUBMITTED', order_id='private'), event('ENTRY_TIMEOUT_CANCELED')])
        self.assertEqual(result['state'], 'TERMINATED')
        self.assertIn('revisar fills', result['label'])

    def test_actual_fill_has_recorded_scope_and_no_order_id_exposure(self):
        result = self.state([event('ENTRY_FILLED', order_id='PRIVATE-ORDER')])
        self.assertEqual(result['state'], 'FILL_RECORDED')
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_accepted_order_plus_later_block_cannot_be_called_not_sent(self):
        result = self.state([event('ENTRY_SUBMITTED', order_id='private'), event('ENTRY_BLOCKED')])
        self.assertEqual(result['state'], 'SUBMITTED')

    def test_failed_reversal_is_visible_and_not_a_new_fill(self):
        self.assertEqual(self.state([event('REVERSAL_ABORTED')])['state'], 'CLOSE_UNRESOLVED')
        value = signal()
        value['ai_decision'] = 'VETO_CLOSE_ONLY'
        self.assertEqual(self.state([], value)['state'], 'CLOSE_ONLY')

    def test_broker_error_url_and_credentials_are_not_returned_as_reason_codes(self):
        for text in ('https://broker/accounts/123456 token=SECRET', 'PRIVATE_TOKEN_VALUE'):
            result = self.state([event('ENTRY_BLOCKED', motivo=text)])
            self.assertEqual(result['reason_code'], '')

    def test_paper_fill_stays_explicitly_simulated(self):
        value = signal()
        value['execution_mode'] = 'PAPER'
        result = self.state([event('ENTRY_FILLED', modo='PAPER', order_id='sim')], value)
        self.assertIn('PAPER · simulación', result['label'])

    def test_reads_never_migrate_a_legacy_audit_schema(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = root/'legacy.csv'
        path.write_text('ticker,evento\nBA,ENTRY_FILLED\n')
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(read_signal_audit(path, [signal()]), ([], False))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(len(list(root.iterdir())), 1)

    def test_api_joins_only_selected_exact_signals_and_keeps_counts(self):
        import server
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = root/'audit.csv'
        rows = [event('SIGNAL_EXECUTION_SKIPPED', motivo='REAL_ACCOUNTING_UNRESOLVED')]
        with path.open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        before = path.read_bytes()
        with patch.object(server, '_read_signal_history', return_value=[signal()]), patch.object(
                server, '_order_audit_path', return_value=str(path)), server.app.test_client() as client:
            with client.session_transaction() as session:
                session['logged_in'] = True
            response = client.get('/api/signals?limit=1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['signals'][0]['execution_evidence']['reason_code'], 'REAL_ACCOUNTING_UNRESOLVED')
        self.assertEqual(response.json['total'], 1)
        self.assertEqual(path.read_bytes(), before)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for dashboard checks')
    def test_actual_signal_renderer_labels_reference_prices_and_blocked_fills(self):
        dashboard = Path(__file__).resolve().parents[1]/'dashboard/index.html'
        js = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),h=fs.readFileSync(process.argv[1],'utf8');
const elements={},c=vm.createContext({S:{},el:id=>elements[id]??={},document:{getElementById:id=>elements[id]??={}},escHtml:v=>String(v??'').replaceAll('<','&lt;').replaceAll('>','&gt;')});
let a=h.indexOf('function financialNumber('),b=h.indexOf('function renderPositions(',a);vm.runInContext(h.slice(a,b),c);
a=h.indexOf('function renderSignalFeed(');b=h.indexOf('function updateTickerSignalBadges(',a);vm.runInContext(h.slice(a,b),c);
a=h.indexOf('function updateBotUI(');b=h.indexOf('// ─',a);vm.runInContext(h.slice(a,b),c);
c.renderSignalFeed('signals',[{ticker:'BA',precio:184.43,prob_ia:.9,ai_decision:'APPROVED',execution_evidence:{label:'Entrada bloqueada/no enviada',reason_code:'REAL_ACCOUNTING_UNRESOLVED'}}]);
assert.match(elements.signals.innerHTML,/Señal \$184.43/);assert.match(elements.signals.innerHTML,/no es precio de ejecución/);
assert.match(elements.signals.innerHTML,/REAL_ACCOUNTING_UNRESOLVED/);assert.match(elements.signals.innerHTML,/APPROVED/);
c.renderSignalFeed('signals',[{ticker:'BA',precio:null}]);assert.match(elements.signals.innerHTML,/Señal N\/D/);assert.doesNotMatch(elements.signals.innerHTML,/\$0\.00/);
c.updateBotUI(false,{state:'FAILED'});assert.equal(elements.botStatus.textContent,'ERROR MOTOR');assert.equal(elements.botToggle.checked,false);
"""
        result = subprocess.run([shutil.which('node'), '-e', js, str(dashboard)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for dashboard checks')
    def test_failed_polls_do_not_keep_old_active_prices_or_claim_a_flat_account(self):
        dashboard = Path(__file__).resolve().parents[1]/'dashboard/index.html'
        js = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),h=fs.readFileSync(process.argv[1],'utf8');
const elements={},el=id=>elements[id]??={textContent:'OLD DATA',innerHTML:'OLD PRICE 999',style:{}};
const c=vm.createContext({S:{positions:[{ticker:'BA',entry_price:999}]},el,document:{getElementById:el},
  api:async()=>{throw Error('offline')},updateDailyRisk:()=>{},updateSchedulerStatus:()=>{},updateBrokerReconciliation:()=>{}});
for(const [start,end] of [['function financialNumber(','function renderPositions('],
 ['function updateBotUI(','// ─'],['async function fetchStatus(){','function updateDailyRisk('],
 ['async function fetchTrades(){','async function fetchTickerPerformance(){'],
 ['async function fetchSignals(){','function renderSignalFeed(']]){
 const a=h.indexOf(start),b=h.indexOf(end,a);vm.runInContext(h.slice(a,b),c);
}
(async()=>{
 await c.fetchStatus();assert.equal(el('botStatus').textContent,'SIN CONEXIÓN');assert.equal(el('botToggle').disabled,true);
 assert.equal(el('stPnl').textContent,'N/D');assert.match(el('engineRuntimeStatus').textContent,/No confirma parada ni cuenta plana/);
 await c.fetchTrades();assert.match(el('livePositionsFeed').innerHTML,/no implica cuenta plana/);
 await c.fetchSignals();assert.match(el('allSignalFeed').innerHTML,/no verificables/);
 assert.doesNotMatch(el('livePositionsFeed').innerHTML,/999/);assert.equal(el('tr-open').textContent,'N/D');assert.equal(c.S.positions.length,0);
 c.api=async()=>({ok:true,json:async()=>({status:'ok',bot:{running:false,engine_runtime:{state:'STOPPED'}},stats:{}})});
 await c.fetchStatus();assert.equal(el('botStatus').textContent,'PARADO');assert.equal(el('botToggle').disabled,false);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which('node'), '-e', js, str(dashboard)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)


if __name__ == '__main__':
    unittest.main()
