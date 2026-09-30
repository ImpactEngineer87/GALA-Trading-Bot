"""Small durable stores for execution recovery and operator audit history."""
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOCKS = {}
_GUARD = threading.Lock()


def path_lock(path):
    with _GUARD:
        return _LOCKS.setdefault(str(Path(path).resolve()), threading.RLock())


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


class JsonState:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = path_lock(path)

    def _read(self):
        if not self.path.exists():
            return {}
        with self.path.open(encoding='utf-8') as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError('Invalid recovery state; reconciliation required')
        return value

    def get(self, key):
        with self.lock:
            return self._read().get(key)

    def put(self, key, value):
        with self.lock:
            state = self._read()
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
            atomic_json(self.path, state)


class EventHistory:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = path_lock(path)

    def append(self, event):
        row = {'timestamp_utc': utc_now(), **event}
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(row, ensure_ascii=False, default=str, allow_nan=False) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
        return row

    def recent(self, ticker='', limit=200):
        from collections import deque
        rows = deque(maxlen=max(1, min(int(limit), 2000)))
        with self.lock:
            if not self.path.exists():
                return []
            with self.path.open(encoding='utf-8') as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not ticker or row.get('ticker') == ticker:
                        rows.append(row)
        return list(rows)
