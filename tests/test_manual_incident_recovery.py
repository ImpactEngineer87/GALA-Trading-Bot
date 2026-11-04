import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from core.manual_accounting_resolution import history_resolved, load_resolutions, manual_risk_adjustment
from scripts.recover_ba_manual_incident import proof_for, DAY, ENTRY, EXIT, REJECTED, MANAGED, ORPHAN
from tests.test_manual_close_reconciliation import order


class ManualIncidentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.history = [dict(fecha=DAY, modo='REAL', ticker='BA', evento='POSITION_CLOSE',
            position_id=pid, history_id='history-' + pid, cantidad='5', entrada='176.59',
            position_side='SHORT', pnl_usd='', order_id=REJECTED) for pid in (MANAGED, ORPHAN)]
        self.baseline = {'unresolved': {'old-review': {'reason': 'preserve'}}}
        self.state = copy.deepcopy(self.baseline)
        self.state['unresolved'].update({key: {'ticker': 'BA'} for key in
            [MANAGED, ORPHAN, *['history:' + DAY + ':' + r['history_id'] for r in self.history]]})
        for key in (MANAGED, ORPHAN):
            self.state['unresolved'][key].update(entry_order_id=ENTRY if key == MANAGED else '',
                exit_order_id=REJECTED, broker_flat=True, broker_exit_price=None,
                requested_exit_price=176.59)
        self.audit = [dict(fecha=DAY, modo='REAL', ticker='BA', evento='ENTRY_FILLED',
                           position_id=MANAGED, order_id=ENTRY)]
        start = datetime(2026, 11, 4, 10, 17, tzinfo=ZoneInfo('America/New_York'))
        self.orders = [order(ENTRY, 'SELL_SHORT', 176.59, start),
                       order(EXIT, 'BUY_TO_COVER', 177.0419, start + timedelta(minutes=20), tag='API_TOS:CHART')]
        for item in self.orders:
            item['quantity'] = item['filledQuantity'] = 5
            item['orderLegCollection'][0]['instrument']['symbol'] = 'BA'
            item['orderActivityCollection'][0]['executionLegs'][0]['quantity'] = 5

    def proof(self):
        return proof_for(self.state, self.baseline, self.audit, self.history, self.orders)

    def test_exact_broker_fill_resolves_duplicate_records_once_without_training_credit(self):
        result = self.proof()
        self.assertEqual(result['gross_pnl'], -2.2595)
        self.assertEqual(len(result['resolved_guard_keys']), 4)
        self.assertFalse(result['training_eligible'])
        self.assertEqual(len(self.state['unresolved']), 5)

    def test_other_guard_changes_or_unknown_fill_price_are_refused(self):
        self.state['unresolved']['new-unrelated'] = {'ticker': 'NVDA'}
        with self.assertRaises(ValueError): self.proof()
        self.state['unresolved'].pop('new-unrelated')
        self.orders[1]['orderActivityCollection'][0]['executionLegs'][0]['price'] = 0
        with self.assertRaises(ValueError): self.proof()

    def test_resolution_requires_trusted_file_and_unchanged_history(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = root / 'manual_accounting_resolutions.json'
        path.write_text(json.dumps({'schema_version':1, 'incidents':{'BA': self.proof()}}))
        with patch('core.manual_accounting_resolution._trusted_approval_file', return_value=False):
            self.assertEqual(load_resolutions(root), {})
        with patch('core.manual_accounting_resolution._trusted_approval_file', return_value=True):
            data = load_resolutions(root)
            self.assertTrue(history_resolved(self.history[0], data))
            self.assertFalse(history_resolved({**self.history[0], 'cantidad':'6'}, data))
            self.assertAlmostEqual(manual_risk_adjustment(root, DAY), -2.2595)
            self.assertEqual(manual_risk_adjustment(root, '2026-11-05'), 0)
