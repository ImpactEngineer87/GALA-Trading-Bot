"""Enable the native 70/30 policy only during stopped, broker-flat maintenance."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def updated_config(text):
    """Preserve formatting and prove the policy is the only semantic change."""
    original = yaml.safe_load(text)
    if not isinstance(original, dict) or not isinstance(original.get('live_trading'), dict):
        raise ValueError('Existing live_trading configuration required')
    partial = original.get('salida_parcial') or {}
    if partial.get('activo', True) is not True or abs(float(partial.get('pct_primer_objetivo', .70)) - .70) > 1e-9:
        raise ValueError('An enabled 70/30 partial plan is required')
    if original['live_trading'].get('real_exit_policy') == 'LINKED_70_30':
        return text, original, original
    tree = yaml.compose(text)
    live = next(node for key,node in tree.value if key.value == 'live_trading')
    policy = next((node for key,node in live.value if key.value == 'real_exit_policy'), None)
    if policy is not None:
        result = text[:policy.start_mark.index] + 'LINKED_70_30' + text[policy.end_mark.index:]
    else:
        index = live.end_mark.index
        insertion = ('' if not index or text[index-1] == '\n' else '\n') + '  real_exit_policy: LINKED_70_30\n'
        result = text[:index] + insertion + text[index:]
    expected = copy.deepcopy(original)
    expected['live_trading']['real_exit_policy'] = 'LINKED_70_30'
    if yaml.safe_load(result) != expected:
        raise ValueError('Configuration changed outside live_trading.real_exit_policy')
    return result, original, expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Apply after service-stop and broker-flat verification')
    args = parser.parse_args()
    config = ROOT/'config.yaml'
    text = config.read_text(encoding='utf-8')
    changed, before, after = updated_config(text)
    result = {'apply':args.apply, 'field':'live_trading.real_exit_policy',
        'before':before['live_trading'].get('real_exit_policy','CONFIGURED'), 'after':'LINKED_70_30'}
    if not args.apply or changed == text:
        print(json.dumps(result)); return
    if not hasattr(os,'geteuid') or os.geteuid() != 0:
        raise ValueError('Root maintenance access required')
    from scripts.authorize_archived_accounting_resume import service_inactive, verify_broker_flat
    from api.schwab_client import SchwabClient, TRADER_URL
    from dotenv import load_dotenv
    import contextlib
    import io
    service_inactive()
    load_dotenv(ROOT/'.env')
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        client = SchwabClient(before); client.TRADER_URL = TRADER_URL
        broker = verify_broker_flat(client, os.environ['SCHWAB_ACCOUNT_NUMBER'])
    folder = Path(tempfile.mkdtemp(prefix='native-exits-3.3.26-', dir='/opt/gala/backups'))
    folder.chmod(0o700)
    shutil.copy2(config, folder/'config.yaml'); (folder/'config.yaml').chmod(0o600)
    stat = config.stat()
    fd, temporary = tempfile.mkstemp(prefix='.config-native-exits-', dir=config.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(changed); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, stat.st_mode); os.chown(temporary, stat.st_uid, stat.st_gid)
        # Refuse an intervening dashboard/config change.
        service_inactive()
        if config.read_text(encoding='utf-8') != text:
            raise ValueError('Configuration changed during maintenance')
        os.replace(temporary, config)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    result.update(backup=str(folder), broker=broker,
        before_sha256=hashlib.sha256(text.encode()).hexdigest(),
        after_sha256=hashlib.sha256(changed.encode()).hexdigest(), only_policy_changed=True)
    receipt=folder/'policy_change.json';receipt.write_text(json.dumps(result,indent=2));receipt.chmod(0o600)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
