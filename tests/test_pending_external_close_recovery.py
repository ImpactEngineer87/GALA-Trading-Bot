import json
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from scripts.recover_pending_external_closes import prove_pending,record_verified_close
from tests.test_manual_close_reconciliation import order
from core.daily_loss_guard import DailyLossGuard
from core.order_audit import OrderAuditLedger
from core.trade_history import TradeHistoryLedger
from core.dashboard_evidence import annotate_history


class PendingExternalCloseRecoveryTests(unittest.TestCase):
    def test_recovery_writes_actual_ledgers_and_reuses_partial_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=OrderAuditLedger(Path(folder)/'audit.csv')
            history=TradeHistoryLedger(Path(folder)/'history.csv')
            entry=ledger.record('ENTRY_FILLED',modo='REAL',ticker='META',position_id='p',
                execution_id='e',correlation_id='c',position_side='SHORT',lado='SELL',
                order_id='entry',cantidad=3,precio=627.59,estado='FILLED',
                detalle={'broker_price_verified':True})
            pending=dict(position_id='p',entry_order_id='entry',entry_price=627.59,side='SELL')
            exit=dict(order_id='exit',quantity=3,avg_price=601.25,order_type='STOP',tag='external',
                filled_at='2026-11-05T15:06:39+00:00')
            proof=dict(pending=pending,entry=entry,exit=exit,gross_pnl=79.02)
            record_verified_close(history,ledger,ledger.read(limit=None),proof,'2026-11-05')
            # A retry after history/audit persisted must not append another close.
            record_verified_close(history,ledger,ledger.read(limit=None),proof,'2026-11-05')
            closes=history.read();self.assertEqual(len(closes),1)
            self.assertEqual(closes[0]['timestamp_utc'],exit['filled_at'])
            self.assertEqual(closes[0]['hora_ny'],'10:06:39')
            audits=ledger.read(limit=None)
            exits=[r for r in audits if r['evento']=='POSITION_CLOSED']
            self.assertEqual(len(exits),1)
            self.assertEqual(json.loads(exits[0]['detalle'])['broker_filled_at'],exit['filled_at'])
            displayed=annotate_history(closes,audits)[0]
            self.assertTrue(displayed['accounting_verified'])
            self.assertFalse(displayed['performance_verified'])

    def test_confirmed_replacement_stop_after_cancellations_supplies_exact_risk(self):
        start=datetime(2026,11,5,9,35,tzinfo=ZoneInfo('America/New_York'))
        entry=order('entry','SELL_SHORT',627.59,start)
        exit=order('exit','BUY_TO_COVER',601.25,start+timedelta(minutes=31),order_type='STOP')
        canceled=order('old-stop','BUY_TO_COVER',0,start+timedelta(minutes=3),order_type='STOP')
        canceled.update(status='REPLACED',filledQuantity=0)
        canceled['orderActivityCollection'][0]['executionType']='CANCELED'
        audit=[dict(evento='ENTRY_FILLED',modo='REAL',ticker='TSLA',position_id='p',
            execution_id='e',correlation_id='c',order_id='entry',cantidad='3',precio='627.59',
            lado='SELL',detalle=json.dumps({'broker_price_verified':True}))]
        row=dict(session_ny='2026-11-05',ticker='TSLA',side='SELL',position_id='p',
            entry_order_id='entry',expected_quantity=3,entry_price=627.59)
        proof=prove_pending(row,audit,[entry,canceled,exit],'2026-11-05')
        self.assertEqual(proof['gross_pnl'],79.02)
        self.assertEqual(proof['exit']['order_type'],'STOP')
        self.assertRaises(ValueError,prove_pending,{**row,'expected_quantity':4},audit,
                          [entry,canceled,exit],'2026-11-05')

    def test_recovery_risk_marker_survives_guard_load_and_later_updates(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'risk.json'
            cfg={'kill_switch':{'daily_loss':{'state_file':str(p)}}}
            guard=DailyLossGuard(cfg)
            data=json.loads(p.read_text());data['verified_pending_closes']={'p':{'gross_pnl':79.02}}
            p.write_text(json.dumps(data))
            restored=DailyLossGuard(cfg);restored.add_realized(False,1)
            self.assertEqual(json.loads(p.read_text())['verified_pending_closes'],data['verified_pending_closes'])
