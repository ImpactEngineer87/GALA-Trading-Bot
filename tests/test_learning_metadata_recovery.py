import unittest
from scripts.recover_learning_metadata import recover_metadata, stamp
from agents.learning_dataset import eligible_closed_rows


class LearningMetadataRecoveryTests(unittest.TestCase):
    def fixtures(self):
        row=dict(modo='PAPER',ticker='NVDA',position_id='p',execution_id='e',tipo='SELL',
            entry_order_id='entry',exit_order_id='exit',execution_valid='1',
            timestamp='2026-09-24 15:16:01',precio_entrada='223.76',precio_salida='223.63',
            closed_utc='',risk_per_share='')
        entry={**row,'lado':'SELL','evento':'ENTRY_FILLED','order_id':'entry','precio':'223.76','cantidad':'10',
            'timestamp_utc':'2026-09-24T19:16:03+00:00','detalle':'{"stop_loss":224.18}'}
        close={**row,'evento':'POSITION_CLOSED','order_id':'exit',
            'timestamp_utc':'2026-09-24T19:45:02.352+00:00'}
        history={**row,'lado':'SELL','evento':'POSITION_CLOSE','order_id':'exit','parent_order_id':'entry',
            'entrada':'223.76','salida':'223.63','cantidad':'10','pnl_usd':'1.30',
            'timestamp_utc':'2026-09-24T19:45:02.242+00:00'}
        return row,entry,close,history

    def test_exact_existing_stop_and_close_time_recover_metadata_without_new_observation(self):
        row,entry,close,history=self.fixtures()
        result=recover_metadata(row,[entry,close],[history])
        self.assertEqual(result['risk_per_share'],.42)
        self.assertEqual(stamp(result['closed_utc']),stamp(close['timestamp_utc']))
        self.assertEqual(len(eligible_closed_rows([row,{**row,**result}])),1)

    def test_quarantined_or_unlinked_evidence_cannot_be_revived(self):
        row,entry,close,history=self.fixtures();row['execution_valid']='0'
        self.assertEqual(recover_metadata(row,[entry,close],[history]),{})
        row['execution_valid']='1';history['parent_order_id']='different'
        self.assertEqual(recover_metadata(row,[entry,close],[history]),{})

    def test_unknown_stop_or_unzoned_close_never_gets_an_estimated_value(self):
        row,entry,close,history=self.fixtures();entry['detalle']='{}'
        self.assertEqual(recover_metadata(row,[entry,close],[history]),{})
        entry['detalle']='{"stop_loss":224.18}';close['timestamp_utc']='2026-09-24T19:45:02'
        self.assertEqual(recover_metadata(row,[entry,close],[history]),{})

    def test_wrong_side_or_nonfinite_evidence_is_not_recovered(self):
        row,entry,close,history=self.fixtures();entry['detalle']='{"stop_loss":223.00}'
        self.assertEqual(recover_metadata(row,[entry,close],[history]),{})
        entry['detalle']='{"stop_loss":224.18}';history['pnl_usd']='nan'
        self.assertEqual(recover_metadata(row,[entry,close],[history]),{})

    def test_real_close_requires_verified_execution_evidence(self):
        row,entry,close,history=self.fixtures()
        for record in (row,entry,close,history):record['modo']='REAL'
        self.assertEqual(recover_metadata(row,[entry,close],[history]),{})
