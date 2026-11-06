"""Refresh slow historical context without occupying candle-analysis workers."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time


class MacroRefresh:
    def __init__(self, fetch):
        self._fetch = fetch
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='GalaHistory')
        self._lock = threading.Lock()
        self._futures = {}
        self._closed = False

    def _load(self, symbol):
        return self._fetch(symbol), time.time()

    def request(self, symbol):
        with self._lock:
            if self._closed or symbol in self._futures:
                return False
            self._futures[symbol] = self._pool.submit(self._load, symbol)
            return True

    def completed(self, symbol):
        with self._lock:
            future = self._futures.get(symbol)
            if self._closed or future is None or not future.done():
                return None
            del self._futures[symbol]
        return future.result()

    def close(self):
        with self._lock:
            self._closed = True
            self._futures.clear()
        # Running work performs market-data GETs only. Its result cannot be
        # installed after close; never delay protective liquidation for history.
        self._pool.shutdown(wait=False, cancel_futures=True)
