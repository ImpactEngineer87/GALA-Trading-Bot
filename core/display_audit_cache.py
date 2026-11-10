"""Incremental, read-only projections for dashboard evidence.

Not used by execution, reconciliation, risk, or training. Source ledgers remain
authoritative. Retain only the latest events consumed by the display rules;
strategy candle receipts are large and irrelevant to fill attribution.
"""
import csv
import io
import os
import threading

from core.dashboard_evidence import _identity, session_date


SIGNAL_EVENTS = frozenset({
    'REVERSAL_ABORTED', 'POSITION_CLOSE_FAILED', 'ENTRY_FILLED',
    'ENTRY_TIMEOUT_CANCELED', 'PENDING_ENTRY_CANCELED', 'ENTRY_TERMINATED',
    'ENTRY_RECONCILE_REQUIRED', 'EXIT_RECONCILE_REQUIRED', 'ENTRY_REJECTED',
    'ENTRY_SUBMITTED', 'ENTRY_ACKNOWLEDGED', 'ENTRY_BLOCKED',
    'SIGNAL_EXECUTION_SKIPPED', 'REVERSAL_CLOSE_ONLY',
})
HISTORY_EVENTS = frozenset({
    'ENTRY_FILLED', 'POSITION_CLOSED', 'TP1_FILLED_70_PERCENT',
    'PAPER_OBSERVATION_QUARANTINED',
})


class DisplayAuditCache:
    def __init__(self):
        self._lock = threading.RLock()
        self._reset()

    def _reset(self):
        self._signature = None
        self._identity = None
        self._offset = 0
        self._anchor = b''
        self._fields = None
        self._history = {}
        self._signals = {}
        self._revision = object()

    def _read(self, path):
        """Consume complete CSV records; retry an interrupted final record."""
        try:
            with open(path, 'rb') as handle:
                stat = os.fstat(handle.fileno())
                signature = (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns)
                if signature == self._signature:
                    return self._fields is not None
                identity = (str(path), stat.st_dev, stat.st_ino)
                handle.seek(max(0, self._offset - len(self._anchor)))
                anchor = handle.read(len(self._anchor))
                if (identity != self._identity or stat.st_size < self._offset
                        or anchor != self._anchor
                        or (self._signature and stat.st_size == self._signature[2])):
                    self._reset()
                self._identity = identity
                handle.seek(self._offset)
                raw = handle.read()
                # A writer can be between UTF-8 bytes. Hold the unfinished suffix.
                text = raw.decode('utf-8', errors='surrogateescape')
                stream = io.StringIO(text, newline='')
                reader = csv.reader(stream, strict=True)
                committed = 0
                while True:
                    try:
                        values = next(reader)
                    except StopIteration:
                        break
                    except csv.Error as exc:
                        if 'unexpected end of data' in str(exc).lower():
                            break
                        self._reset()
                        return False
                    end = stream.tell()
                    fragment = text[committed:end]
                    if (not fragment.endswith(('\n', '\r'))
                            or any('\udc80' <= c <= '\udcff' for c in fragment)):
                        break
                    if self._fields is None:
                        fields = [v.lstrip('\ufeff') if i == 0 else v
                                  for i, v in enumerate(values)]
                        if not {'modo', 'ticker', 'signal_id', 'evento', 'order_id'} <= set(fields):
                            self._reset()
                            return False
                        self._fields = fields
                    elif len(values) != len(self._fields):
                        self._reset()
                        return False
                    else:
                        self._keep(dict(zip(self._fields, values)))
                    committed = end
                self._offset += len(text[:committed].encode('utf-8', errors='surrogateescape'))
                handle.seek(max(0, self._offset - 128))
                self._anchor = handle.read(min(128, self._offset))
                self._signature = signature
                return self._fields is not None
        except OSError:
            self._reset()
            return False

    def _keep(self, row):
        kind = str(row.get('evento') or '').upper()
        if kind in HISTORY_EVENTS:
            key = _identity(row)
            if all(key) and ((key[0] == 'REAL' and kind != 'PAPER_OBSERVATION_QUARANTINED')
                    or (key[0] == 'PAPER' and kind == 'PAPER_OBSERVATION_QUARANTINED')):
                identity = (*key, kind, str(row.get('order_id') or '') if key[0] == 'REAL' else '')
                if self._history.get(identity) != row:
                    self._history[identity] = row
                    self._revision = object()
        if kind in SIGNAL_EVENTS:
            mode, ticker = (str(row.get(k) or '') for k in ('modo', 'ticker'))
            sid = str(row.get('signal_id') or '').strip()
            if mode.upper() in {'PAPER', 'REAL'} and ticker and sid:
                day = session_date(row)
                if day:
                    self._signals.setdefault((mode.upper(), ticker.upper(), sid, day), {})[kind] = row

    def history(self, path):
        with self._lock:
            available = self._read(path)
            return self._revision, list(self._history.values()), available

    def signals(self, path, wanted):
        with self._lock:
            available = self._read(path)
            return [row for key in wanted for row in self._signals.get(key, {}).values()], available


DISPLAY_AUDIT_CACHE = DisplayAuditCache()
