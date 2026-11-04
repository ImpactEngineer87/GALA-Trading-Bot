import unittest
from agents.learning_dataset import eligible_closed_rows
from core.dashboard_evidence import annotate_history
from scripts.quarantine_duplicate_paper_observations import ambiguous_groups, DAY, REASON


class DuplicatePaperQuarantineTests(unittest.TestCase):
    def fixtures(self):
        rows = [dict(ticker='PINS', tipo='BUY', modo='PAPER', execution_valid='1',
            timestamp=DAY+' 10:00:03', position_id='p'+str(i), execution_id='e'+str(i),
            entry_signal_id='s'+str(i), precio_entrada='21', precio_salida=str(20+i*2)) for i in range(2)]
        signals = [dict(signal_id='s'+str(i), date=DAY, ticker='PINS', tipo='BUY',
            execution_mode='PAPER', signal_bar_epoch_ms=1793804340000) for i in range(2)]
        return rows, signals

    def test_concurrent_positions_on_one_bar_are_both_ambiguous_and_superseding_rows_exclude_them(self):
        rows, signals = self.fixtures()
        groups = ambiguous_groups(rows, signals)
        self.assertEqual(sum(len(group) for group in groups.values()), 2)
        amended = [{**r, 'execution_valid':'0', 'exclusion_reason':REASON} for r in rows]
        self.assertEqual(eligible_closed_rows(rows+amended), [])
        self.assertEqual(ambiguous_groups(rows+amended, signals), {})

    def test_distinct_bars_and_real_trades_are_preserved(self):
        rows, signals = self.fixtures()
        signals[1]['signal_bar_epoch_ms'] += 60000
        self.assertEqual(ambiguous_groups(rows, signals), {})
        for r in rows: r['modo'] = 'REAL'
        signals[1]['signal_bar_epoch_ms'] -= 60000
        self.assertEqual(ambiguous_groups(rows, signals), {})

    def test_quarantined_paper_pnl_is_excluded_without_rewriting_history_or_touching_real(self):
        history = [dict(modo='PAPER', ticker='PINS', position_id='p0', execution_id='e0',
            evento='POSITION_CLOSE', pnl_usd='10', timestamp_utc='2026-11-04T15:00:00Z')]
        event = {**history[0], 'evento':'PAPER_OBSERVATION_QUARANTINED', 'motivo':REASON}
        checked = annotate_history(history, [event])[0]
        self.assertFalse(checked['performance_verified'])
        self.assertEqual(checked['performance_exclusion_reason'], REASON)
        self.assertEqual(history[0]['pnl_usd'], '10')
