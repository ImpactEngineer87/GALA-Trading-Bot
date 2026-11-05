"""Reproducible closed-candle inputs; contains no broker/account credentials."""
import hashlib
import json
import pandas as pd
import numpy as np


def evaluation_receipt(frame, strategy, calculated):
    inputs = frame[['open','high','low','close','volume']].astype(float).copy()
    inputs.index = pd.to_datetime(inputs.index, utc=True)
    if not inputs.index.is_monotonic_increasing or inputs.index.has_duplicates:
        raise ValueError('Candle timestamps must be ordered and unique')
    if inputs.empty or not np.isfinite(inputs.to_numpy()).all():
        raise ValueError('Incomplete OHLCV evidence')
    config = {k:v for k,v in vars(strategy).items() if isinstance(v,(str,int,float,bool))}
    config_hash = hashlib.sha256(json.dumps(config,sort_keys=True,allow_nan=False).encode()).hexdigest()
    candle_hash = hashlib.sha256(pd.util.hash_pandas_object(inputs,index=True).values.tobytes()).hexdigest()
    last = inputs.iloc[-1]
    side = calculated.iloc[-1].get('señal')
    return {'source':'SCHWAB_1M_CLOSED', 'history_start_utc':inputs.index[0].isoformat(),
        'bar_utc':inputs.index[-1].isoformat(),
        'decision_available_utc':(inputs.index[-1]+pd.Timedelta(minutes=1)).isoformat(),
        'bar_count':len(inputs), 'bar':last.to_dict(), 'strategy_inputs':config,
        'strategy_sha256':config_hash, 'candles_sha256':candle_hash,
        'signal':side if isinstance(side,str) else None}
