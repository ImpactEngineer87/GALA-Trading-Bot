"""Bounded order-work mailboxes; one running and one latest update per symbol.

Analysis never waits for these workers. A newer snapshot replaces queued work,
with an explicit callback for any superseded, already analysed signal.
"""
import threading
import logging
from concurrent.futures import ThreadPoolExecutor


class SymbolWork:
    def __init__(self, workers, run, discarded, failed):
        self._pool = ThreadPoolExecutor(max_workers=max(1, int(workers)),
                                        thread_name_prefix='GalaOrders')
        self._lock = threading.Lock()
        self._pending = {}
        self._running = set()
        self._closed = False
        self._run, self._discarded, self._failed = run, discarded, failed

    def submit(self, symbol, payload):
        with self._lock:
            if self._closed:
                return False
            old = self._pending.get(symbol)
            self._pending[symbol] = payload
            if symbol not in self._running:
                self._running.add(symbol)
                self._pool.submit(self._drain, symbol)
        if old is not None:
            self._discarded(symbol, old, 'NEWER_CANDLE_ANALYZED')
        return True

    def _drain(self, symbol):
        while True:
            with self._lock:
                if self._closed or symbol not in self._pending:
                    self._running.discard(symbol)
                    return
                payload = self._pending.pop(symbol)
            try:
                self._run(symbol, payload)
            except Exception as exc:
                try:
                    self._failed(symbol, payload, exc)
                except Exception:
                    # A failed error reporter must not strand this symbol's
                    # mailbox. Never log arbitrary broker exception contents.
                    logging.getLogger(__name__).error('Unable to record symbol worker failure')

    def close(self):
        with self._lock:
            self._closed = True
            pending, self._pending = self._pending, {}
        for symbol, payload in pending.items():
            self._discarded(symbol, payload, 'ENGINE_STOPPED')
        self._pool.shutdown(wait=True, cancel_futures=True)


def causal_snapshot(datos, closed_at):
    """Use only settled native frames for an explicitly delayed AI observation.

    Hourly/four-hourly frames are right-labelled resamples of opening-stamped
    30m candles; their last underlying candle settles 30m after that label.
    No forming macro candle from the later live snapshot may enter a replay.
    """
    import pandas as pd
    from agents.contracts import DatosMultiTemporal
    cutoff = pd.Timestamp(closed_at)
    cutoff = cutoff.tz_localize('UTC') if cutoff.tzinfo is None else cutoff.tz_convert('UTC')
    durations = {'1m':1, '5m':5, '15m':15, '30m':30,
                 '1H':30, '4H':30, '1D':24*60, '1W':7*24*60}
    frames = {}
    for tf, frame in datos.velas.items():
        if frame is None or tf not in durations:
            continue
        index = pd.to_datetime(frame.index, utc=True, errors='coerce')
        mask = index.notna() & ((index + pd.Timedelta(minutes=durations[tf])) <= cutoff)
        if 'actual_in_formation' in frame:
            mask &= ~frame['actual_in_formation'].fillna(True).astype(bool).to_numpy()
        frames[tf] = frame.iloc[mask].copy()
    return DatosMultiTemporal(datos.ticker, frames, sintetico=datos.sintetico)
