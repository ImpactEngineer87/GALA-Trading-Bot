import json
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from scripts.recover_pending_external_closes import prove_pending
from tests.test_manual_close_reconciliation import order
from core.daily_loss_guard import DailyLossGuard


class PendingExternalCloseRecoveryTests(unittest.TestCase):
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
