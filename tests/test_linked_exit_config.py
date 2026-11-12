"""Policy migration preserves every other parsed configuration value."""
import unittest
import yaml
from scripts.enable_linked_profit_targets import updated_config


class LinkedExitConfigTests(unittest.TestCase):
    def test_changes_existing_policy_and_preserves_comments_and_risk(self):
        text='salida_parcial:\n  activo: true\n  pct_primer_objetivo: 0.70\nlive_trading:\n  real_exit_policy: "FULL_POSITION" # retained\n  max_shares_per_order: 10\nrisk:\n  daily: 200\n'
        result,before,after=updated_config(text)
        self.assertIn('real_exit_policy: LINKED_70_30 # retained',result)
        self.assertEqual(after['risk'],before['risk'])
        self.assertEqual(result,text.replace('"FULL_POSITION"','LINKED_70_30'))

    def test_adds_only_the_missing_policy_and_is_idempotent(self):
        text='live_trading:\n  max_shares_per_order: 10\nrisk:\n  daily: 200\n'
        result,before,after=updated_config(text)
        self.assertEqual(after['live_trading']['real_exit_policy'],'LINKED_70_30')
        after['live_trading'].pop('real_exit_policy');self.assertEqual(after,before)
        self.assertEqual(updated_config(result)[0],result)

    def test_disabled_or_different_fraction_requires_configuration_review(self):
        for partial in ['activo: false','pct_primer_objetivo: 0.5','pct_primer_objetivo: .nan']:
            with self.assertRaises(ValueError):updated_config('live_trading: {}\nsalida_parcial:\n  '+partial+'\n')
