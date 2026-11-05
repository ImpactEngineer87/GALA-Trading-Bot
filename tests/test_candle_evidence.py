import tempfile
import unittest
from pathlib import Path
import pandas as pd
from core.candle_evidence import evaluation_receipt
from core.strategy import GALA10Strategy
from core.ticker_execution import TickerExecutionRegistry
from core.model_identity import model_snapshot


class CandleAndRoutingEvidenceTests(unittest.TestCase):
    def test_candle_or_strategy_changes_have_distinct_evidence(self):
        f=pd.DataFrame({'open':[10.,11.],'high':[12.,13.],'low':[9.,10.],
            'close':[11.,12.],'volume':[10,20]},index=pd.date_range('2026-11-04 15:00',periods=2,freq='min'))
        strategy=GALA10Strategy({});calc=f.assign(señal=None)
        first=evaluation_receipt(f,strategy,calc)
        self.assertEqual(first,evaluation_receipt(f.copy(),strategy,calc))
        self.assertEqual(first['decision_available_utc'],'2026-11-04T15:02:00+00:00')
        f.loc[f.index[0],'close']=10.5
        self.assertNotEqual(first['candles_sha256'],evaluation_receipt(f,strategy,calc)['candles_sha256'])
        strategy.cooldown_bars=9
        self.assertNotEqual(first['strategy_sha256'],evaluation_receipt(f,strategy,calc)['strategy_sha256'])

    def test_missing_corrupt_or_new_registry_rows_never_inherit_real_from_config(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'routing.json'
            reg=TickerExecutionRegistry(str(p),default_mode='REAL',initial_real_tickers=['AAPL','AMD'])
            self.assertEqual(reg.get('AAPL')['mode'],'PAPER')
            reg.ensure_tickers(['AAPL']);self.assertEqual(reg.mode('AAPL'),'PAPER')
            reg.update('AAPL',mode='REAL');self.assertEqual(reg.mode('AAPL'),'REAL')
            self.assertEqual(reg.mode('AMD'),'PAPER')
            p.write_text('bad json');self.assertEqual(reg.mode('AAPL'),'PAPER')

    def test_artifact_timestamp_does_not_replace_training_date_or_byte_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'model.pkl';p.write_bytes(b'original')
            before=model_snapshot({'entrenado_utc':'2026-08-13T19:45:35Z'},str(p),True)
            self.assertTrue(before['artifact_modified_utc'])
            self.assertEqual(before['training_date'],'2026-08-13T19:45:35Z')
            p.write_bytes(b'changed')
            self.assertNotEqual(before['model_version'],model_snapshot({},str(p),True)['model_version'])
