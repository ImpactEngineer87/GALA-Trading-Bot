"""Known maintenance exclusions do not hide eligible PAPER P/L or unknown fills."""
import copy
from datetime import datetime,timezone
import unittest
from unittest.mock import patch
from core.dashboard_evidence import annotate_history
import server


class MaintenancePnlTests(unittest.TestCase):
    def fixtures(self):
        valid={'ticker':'TSLA','modo':'PAPER','position_id':'normal','execution_id':'en',
            'evento':'POSITION_CLOSE','fecha':'2026-11-06','pnl_usd':'12.50'}
        maintenance={**valid,'ticker':'PINS','position_id':'maintenance','execution_id':'em','pnl_usd':'-99'}
        audit={**maintenance,'evento':'PAPER_OBSERVATION_QUARANTINED',
            'motivo':'CONTROLLED_MAINTENANCE_CLOSE_20261106_3321',
            'detalle':{'training_eligible':False,'performance_eligible':False}}
        return [valid,maintenance],[audit]

    def summary(self,rows,events):
        annotated=annotate_history(rows,events)
        with patch.object(server,'_dashboard_history_rows',return_value=annotated),patch.object(
                server,'_ahora_ny_datetime',return_value=datetime(2026,11,6,18,tzinfo=timezone.utc)):
            return server._history_summary(),annotated

    def test_eligible_sum_excludes_maintenance_and_retains_count_and_source_records(self):
        rows,events=self.fixtures();before=copy.deepcopy((rows,events))
        result,annotated=self.summary(rows,events)
        self.assertEqual(result['paper_realized_today'],12.5)
        self.assertEqual(result['paper_pnl'],12.5)
        self.assertEqual(result['paper_closed_today'],1)
        self.assertEqual(result['paper_excluded_today'],1)
        self.assertIsNone(annotated[-1]['display_pnl_usd'])
        self.assertFalse(annotated[-1]['performance_verified'])
        self.assertEqual((rows,events),before)

    def test_missing_or_nonfinite_price_information_does_not_become_known_exclusion(self):
        for value in ('','nan','inf'):
            rows,events=self.fixtures();rows[-1]['pnl_usd']=value
            result,annotated=self.summary(rows,events)
            self.assertIsNone(result['paper_realized_today'])
            self.assertFalse(annotated[-1]['performance_intentionally_excluded'])

    def test_unrecognized_quarantine_or_missing_explicit_provenance_remains_unknown(self):
        for change in ({'motivo':'AMBIGUOUS_PAPER_DUPLICATE'}, {'detalle':{}}):
            rows,events=self.fixtures();events[-1].update(change)
            result,annotated=self.summary(rows,events)
            self.assertIsNone(result['paper_realized_today'])
            self.assertFalse(annotated[-1]['performance_intentionally_excluded'])

    def test_real_missing_fill_cannot_be_hidden_by_paper_maintenance_exception(self):
        rows,events=self.fixtures();rows.append({**rows[0],'modo':'REAL','position_id':'unknown-real'})
        result,_=self.summary(rows,events)
        self.assertIsNone(result['real_realized_today'])
        self.assertEqual(result['paper_realized_today'],12.5)


if __name__=='__main__':unittest.main()
