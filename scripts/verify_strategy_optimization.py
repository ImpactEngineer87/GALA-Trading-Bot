"""Offline exact-output comparison against a pinned strategy source revision.

Input is a gzip JSON containing only strategy config and archived OHLCV rows:
{config: {...}, frames: {symbol: [[epoch_ms, open, high, low, close, volume], ...]}}.
No broker access, models, credentials, ledger writes, or orders.
"""
import argparse
import gzip
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from core.strategy import GALA10Strategy


def verify(input_path, baseline):
    original = subprocess.check_output(['git', 'show', f'{baseline}:core/strategy.py'], text=True, encoding='utf-8')
    namespace = {'__name__': 'strategy_baseline'}
    exec(compile(original, 'strategy_baseline.py', 'exec'), namespace)
    with gzip.open(input_path, 'rt', encoding='utf-8') as handle:
        data = json.load(handle)
    old, new = namespace['GALA10Strategy'](data['config']), GALA10Strategy(data['config'])
    timings, candles = [], 0
    for symbol, rows in data['frames'].items():
        frame = pd.DataFrame(rows, columns=['epoch_ms', 'open', 'high', 'low', 'close', 'volume'])
        frame.index = pd.to_datetime(frame.pop('epoch_ms'), unit='ms', utc=True)
        start = time.perf_counter(); expected = old.calcular(frame); old_sec = time.perf_counter()-start
        start = time.perf_counter(); actual = new.calcular(frame); new_sec = time.perf_counter()-start
        pd.testing.assert_frame_equal(expected, actual, check_exact=True)
        timings.append({'ticker': symbol, 'candles': len(frame), 'baseline_ms': round(old_sec*1000, 2),
                        'optimized_ms': round(new_sec*1000, 2), 'all_columns_exact': True})
        candles += len(frame)
    return {'baseline': baseline, 'tickers': len(timings), 'candles': candles,
            'all_columns_exact': True, 'baseline_ms': round(sum(t['baseline_ms'] for t in timings), 2),
            'optimized_ms': round(sum(t['optimized_ms'] for t in timings), 2), 'results': timings}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input'); parser.add_argument('--baseline', required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.input, args.baseline)))
