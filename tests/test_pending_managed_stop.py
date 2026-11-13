"""Exact managed STOP recovery rejects attribution errors and cannot double book."""
import copy
import csv
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agents.contracts import DecisionFinal
from agents.feedback_store import FeedbackStore
from scripts.recover_pending_managed_stop import commit_proof, prove_pending_stop
from tests.test_learning_feedback import open_context
from tests.test_manual_close_reconciliation import order


class PendingManagedStopTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('requests.sessions.Session.request',
            side_effect=AssertionError('Recovery tests cannot contact Schwab')))
        self.day='2026-11-13';stamp=datetime(2026,11,13,16,44,tzinfo=timezone.utc)
        self.pending={'ticker':'TSLA','position_id':'p1','entry_order_id':'entry-p1',
            'expected_quantity':3,'entry_price':410.8,'side':'SELL','session_ny':self.day}
        self.orders=[order('entry-p1','SELL_SHORT',410.8,stamp),
            order('replacement-stop','BUY_TO_COVER',411.22,stamp+timedelta(minutes=25),order_type='STOP')]
        self.entry={'modo':'REAL','ticker':'TSLA','position_id':'p1','execution_id':'execution-p1',
            'signal_id':'signal-p1','correlation_id':'correlation-p1','position_side':'SHORT',
            'evento':'ENTRY_FILLED','order_id':'entry-p1','cantidad':'3','precio':'410.8',
            'lado':'SELL','detalle':json.dumps({'broker_price_verified':True})}
        self.audit=[self.entry,{**self.entry,'evento':'STOP_SUBMITTED','order_id':'replacement-stop'}]

    def proof(self):
        return prove_pending_stop(self.pending,self.audit,self.orders,self.day)

    def test_exact_filled_stop_with_matching_entry_supplies_actual_loss(self):
        proof=self.proof()
        self.assertEqual(proof['exit']['order_id'],'replacement-stop')
        self.assertEqual(proof['gross_pnl'],-1.26)

    def test_external_or_other_position_stop_cannot_be_claimed_by_gala(self):
        for patch_row in ({'position_id':'other'},{'order_id':'other'}, {'evento':'STOP_ACKNOWLEDGED'}):
            audit=copy.deepcopy(self.audit);audit[1].update(patch_row)
            with self.assertRaisesRegex(ValueError,'EXACT_AUDITED_STOP_FILL_REQUIRED'):
                prove_pending_stop(self.pending,audit,self.orders,self.day)

    def test_wrong_entry_or_missing_price_or_multiple_closes_are_refused(self):
        for key,value in [('entry_price',410.9),('expected_quantity',2),('session_ny','2026-11-12')]:
            with self.assertRaises(ValueError):
                prove_pending_stop({**self.pending,key:value},self.audit,self.orders,self.day)
        self.orders[1]['orderActivityCollection'][0]['executionLegs'][0]['price']=0
        with self.assertRaises(ValueError):self.proof()

    @unittest.skipUnless(hasattr(os,'fchown'),'POSIX ownership/atomic replacement test')
    def test_verified_close_is_booked_once_with_original_features_and_resolution_time(self):
        root=Path(self.enterContext(tempfile.TemporaryDirectory()));data=root/'data';data.mkdir()
        feedback=FeedbackStore(str(data/'agentes_feedback.csv'))
        decision=DecisionFinal(ticker='TSLA',operar=True,tipo='SELL',
            timestamp=datetime(2026,11,13,11,44,tzinfo=timezone.utc))
        feedback.abrir(decision,410.8,411.22,lifecycle=open_context('p1',side='SELL'))
        (data/'daily_kill_switch_state.json').write_text(json.dumps({
            'session_date':self.day,'realized_pnl':{'REAL':6.97,'PAPER':0}}))
        (data/'manual_close_pending.json').write_text(json.dumps({'pending':{'p1':self.pending}}))
        proof=self.proof();commit_proof(root,proof,self.day);commit_proof(root,proof,self.day)
        risk=json.loads((data/'daily_kill_switch_state.json').read_text())
        self.assertAlmostEqual(risk['realized_pnl']['REAL'],5.71)
        self.assertFalse(json.loads((data/'manual_close_pending.json').read_text())['pending'])
        with (data/'agentes_feedback.csv').open() as handle:rows=list(csv.DictReader(handle))
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['execution_valid'],'1')
        self.assertEqual(rows[0]['ganadora'],'0');self.assertEqual(rows[0]['closed_utc'],proof['exit']['filled_at'])
        with (data/'historial_operaciones.csv').open() as handle:history=list(csv.DictReader(handle))
        self.assertEqual(len(history),1);self.assertEqual(history[0]['order_id'],'replacement-stop')
        before=(data/'historial_operaciones.csv').read_bytes()
        with self.assertRaises(ValueError):commit_proof(root,{**proof,'gross_pnl':99},self.day)
        self.assertEqual((data/'historial_operaciones.csv').read_bytes(),before)


if __name__=='__main__':unittest.main()
