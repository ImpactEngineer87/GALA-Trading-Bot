"""Add v3.3.0 settings without replacing credentials, models, or runtime history."""
import argparse
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import shutil
import yaml
from core.runtime_store import atomic_json


def migrate(config):
    cfg = deepcopy(config)
    live = cfg.setdefault('live_trading', {})
    live.setdefault('max_real_tickers', live.get('max_tickers', 10))
    live.setdefault('real_tickers', [])
    live.setdefault('runtime_isolate_ticker_failures', True)
    capacity = live.setdefault('capacity', {})
    for key, value in {'enabled': True, 'cash_reserve_usd': 0, 'notional_multiplier': 1.05,
                       'max_balance_age_sec': 10}.items():
        capacity.setdefault(key, value)
    auth = live.setdefault('manual_authorization', {})
    for key, value in {'enabled': True, 'state_path': 'data/real_authorization.json',
                       'audit_path': 'data/real_authorization_audit.jsonl'}.items():
        auth.setdefault(key, value)
    execution = cfg.setdefault('execution_engine', {})
    for key, value in {'enabled': True, 'entry_order_type': 'LIMIT', 'partial_tp_order_type': 'LIMIT',
                       'exit_buffer_bps': 5, 'limit_wait_sec': 1.5, 'market_fallback': True,
                       'max_quote_age_sec': 5, 'pending_path': 'data/execution_pending.json',
                       'history_path': 'data/execution_v2.jsonl'}.items():
        execution.setdefault(key, value)
    cfg.setdefault('logging', {}).setdefault('execution_diagnostics', 'data/execution_diagnostics.jsonl')
    return cfg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.yaml')
    args = parser.parse_args()
    path = Path(args.config)
    with path.open(encoding='utf-8-sig') as handle:
        original = yaml.safe_load(handle) or {}
    updated = migrate(original)
    if updated == original:
        print('Configuration already includes v3.3.0 settings.')
        return
    backup = path.with_name(path.name + '.before_v3.3.0_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    shutil.copy2(path, backup)
    import os
    tmp = path.with_suffix('.v330.tmp')
    with tmp.open('w', encoding='utf-8') as handle:
        yaml.safe_dump(updated, handle, sort_keys=False, allow_unicode=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    print('v3.3.0 configuration saved. Existing routing and live ticker limit preserved.')
    print('Backup: ' + str(backup))


if __name__ == '__main__':
    main()
