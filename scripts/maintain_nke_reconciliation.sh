#!/usr/bin/env bash
# Reviewed NKE incident maintenance in the original installation only.
# Run only during an operator-coordinated pause in dashboard/TOS order changes.
# This requests the normal engine stop; PAPER positions may close normally.
# It never starts the trading engine or sends a broker order itself.
set -Eeuo pipefail
trap 'printf "\nSTOP: maintenance halted. No automatic restart or rollback. Send the output.\n" >&2; systemctl show gala -p ActiveState -p MainPID --no-pager >&2' ERR
cd /opt/gala/GALA-Trading-Bot
test "$(id -u)" = 0
gala_target=${1:?Pass the reviewed commit SHA}
gala_plan=${2:?Pass the reviewed private NKE plan path}
test "$#" = 2
[[ "$gala_target" =~ ^[0-9a-f]{40}$ ]]
test -r "$gala_plan"
test "$(git branch --show-current)" = fix/real-signal-execution-controls-20261023
git diff --quiet
git diff --cached --quiet
git fetch origin fix/real-signal-execution-controls-20261023
test "$(git rev-parse origin/fix/real-signal-execution-controls-20261023)" = "$gala_target"
git merge-base --is-ancestor HEAD "$gala_target"
gala_tmp=$(mktemp -d /tmp/gala-nke-maintenance.XXXXXXXX)
cleanup() {
  rm -f -- "$gala_tmp/upgrade.sh" "$gala_tmp/apply.json"
  rmdir -- "$gala_tmp"
}
trap cleanup EXIT
git show "${gala_target}:scripts/upgrade_original_v315.sh" > "$gala_tmp/upgrade.sh"
bash -n "$gala_tmp/upgrade.sh"
printf 'Operator maintenance window required. The normal stop policy may close PAPER positions.\n'

# PRE_STOP_PY: authenticated checks, fresh configured-account flatness, then
# the normal stop endpoint with CSRF. No stop request precedes those checks.
./.venv/bin/python - "$gala_plan" <<'PRE_STOP_PY'
import contextlib, getpass, hashlib, io, json, os, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests, yaml
from dotenv import load_dotenv
from core.auto_ai import policy, window
from core.project_paths import resolve_runtime_path

def require(ok, message):
    if not ok:
        raise SystemExit('STOP: ' + message)

root = Path.cwd().resolve()
plan = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
require(plan.get('format') == 1 and plan.get('action') == 'ARCHIVE_VERIFIED_PAST_ACCOUNTING'
        and plan.get('root') == str(root) and plan.get('ticker') == 'NKE'
        and plan.get('position_ref') == 'd744cdfefff2'
        and plan.get('session_ny') == '2026-10-23', 'Unexpected NKE plan scope.')
require(plan.get('fingerprint') == hashlib.sha256(json.dumps(
    {k:v for k,v in plan.items() if k != 'fingerprint'}, sort_keys=True,
    separators=(',', ':'), allow_nan=False).encode()).hexdigest(), 'Plan fingerprint invalid.')
cfg = yaml.safe_load(Path('config.yaml').read_text(encoding='utf-8')) or {}
guard = Path(resolve_runtime_path((cfg.get('execution_engine') or {}).get(
    'accounting_state_path', 'data/real_accounting_reconciliation.json'), project_root=root))
require(hashlib.sha256(guard.read_bytes()).hexdigest() == plan.get('guard_sha256'), 'Accounting state changed; review the plan again.')
require((cfg.get('scheduler') or {}).get('modo') == 'manual', 'Scheduler must be manual.')
def require_window():
    now = datetime.now(timezone.utc)
    require(not policy(cfg)['enabled'] or not any(window(cfg, stamp)[0]
            for stamp in (now, now + timedelta(minutes=10))), 'Choose a window away from scheduled Auto AI training.')
require_window()

s = requests.Session(); s.trust_env = False
base = 'http://127.0.0.1:5000'
r = s.post(base + '/api/login', json={
    'username': getpass.getpass('Dashboard username: '),
    'password': getpass.getpass('Dashboard password: '),
}, timeout=(5, 30))
require(r.status_code == 200, 'Dashboard login failed.')
login = r.json(); csrf = login.get('csrf_token')
require(login.get('status') == 'ok' and bool(csrf), 'Authenticated CSRF session unavailable.')
def read(path):
    response = s.get(base + path, timeout=(5, 120))
    require(response.status_code == 200, 'Dashboard status request failed.')
    data = response.json()
    require(data.get('status') == 'ok', 'Dashboard status unavailable.')
    return data

status = read('/api/status'); ai = read('/api/ai/status')
require(status.get('scheduler', {}).get('modo') == 'manual', 'Scheduler state changed.')
require(type(ai.get('summary', {}).get('training')) is int and ai['summary']['training'] == 0,
        'A training worker is active or unavailable.')
require(ai.get('auto_activate_production') is False, 'Automatic production setting requires review.')
require(status.get('stats', {}).get('real_open_count') == 0, 'Local REAL exposure is not confirmed zero.')
if status.get('bot', {}).get('running') is True:
    require(status.get('daily_risk', {}).get('real_accounting', {}).get('blocked') is True,
            'Expected accounting entry block is not confirmed active.')
load_dotenv('.env', override=False)
account = os.getenv('SCHWAB_ACCOUNT_NUMBER', '').strip()
require(bool(account) and str(status.get('bot', {}).get('account_number') or '').strip() == account,
        'Dashboard and configured broker account are not confirmed identical.')
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from api.broker_check import ejecutar_broker_check
    report = ejecutar_broker_check('config.yaml', account, 'MSFT')
require(report.get('ok') is True, 'Fresh broker check failed.')
for name in ('positions', 'open_orders'):
    step = (report.get('steps') or {}).get(name) or {}
    require(step.get('ok') is True and type(step.get('count')) is int and step['count'] == 0,
            'Fresh broker positions/orders are not confirmed zero.')
print('Fresh checks passed: account matches, REAL exposure/orders zero, AI idle, manual scheduler.', flush=True)
require_window()
require(hashlib.sha256(guard.read_bytes()).hexdigest() == plan.get('guard_sha256'),
        'Accounting state changed during checks; review the plan again.')
if status.get('bot', {}).get('running') is True:
    response = s.post(base + '/api/bot/stop', headers={
        'Content-Type':'application/json', 'X-CSRF-Token':csrf}, json={}, timeout=(5, 120))
    require(response.status_code == 200 and response.json().get('status') == 'ok',
            'Normal engine stop was not confirmed. No retry or service stop requested.')
deadline = time.monotonic() + 45
while True:
    stopped = read('/api/status').get('bot') or {}
    if stopped.get('running') is False and stopped.get('engine_runtime', {}).get('thread_alive') is False:
        break
    require(time.monotonic() < deadline, 'Engine thread has not ended; do not force a restart.')
    time.sleep(0.5)
after_ai = read('/api/ai/status')
require(type(after_ai.get('summary', {}).get('training')) is int and after_ai['summary']['training'] == 0,
        'A training worker became active; leave the service running and review.')
print('Engine OFF and thread ended. Ready for the stopped-service update.', flush=True)
PRE_STOP_PY

# Existing guarded updater performs private backup, fresh flatness checks,
# fast-forward update, all 450 isolated tests as gala and preservation checks.
bash "$gala_tmp/upgrade.sh" "$gala_target" --hold-stopped
./.venv/bin/python scripts/reconcile_verified_history.py \
  --runtime-root /opt/gala/GALA-Trading-Bot --apply "$gala_plan" | tee "$gala_tmp/apply.json"

# PRE_START_PY: independently inspect the intended narrow state change and
# current broker flatness. An unexpected result leaves gala.service stopped.
./.venv/bin/python - "$gala_tmp/apply.json" <<'PRE_START_PY'
import contextlib, io, json, os, sys
from pathlib import Path
import yaml
from dotenv import load_dotenv
from core.project_paths import resolve_runtime_path
from core.real_accounting import RealAccountingGuard
result = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
if result.get('status') not in {'APPLIED', 'ALREADY_APPLIED'}:
    raise SystemExit('STOP: NKE reconciliation was not confirmed.')
cfg = yaml.safe_load(Path('config.yaml').read_text(encoding='utf-8')) or {}
if (cfg.get('scheduler') or {}).get('modo') != 'manual':
    raise SystemExit('STOP: scheduler is no longer manual.')
path = resolve_runtime_path((cfg.get('execution_engine') or {}).get(
    'accounting_state_path', 'data/real_accounting_reconciliation.json'), project_root=Path.cwd())
snapshot = RealAccountingGuard(path).snapshot()
if snapshot.get('unresolved') != 3 or snapshot.get('tickers') != ['BA', 'META'] or snapshot.get('blocked') is not True:
    raise SystemExit('STOP: unexpected accounting result; inspect the private backup before restarting.')
load_dotenv('.env', override=False)
account = os.getenv('SCHWAB_ACCOUNT_NUMBER', '').strip()
if not account:
    raise SystemExit('STOP: exact configured broker account unavailable.')
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from api.broker_check import ejecutar_broker_check
    report = ejecutar_broker_check('config.yaml', account, 'MSFT')
if report.get('ok') is not True:
    raise SystemExit('STOP: final broker check failed.')
for name in ('positions', 'open_orders'):
    step = (report.get('steps') or {}).get(name) or {}
    if step.get('ok') is not True or type(step.get('count')) is not int or step['count'] != 0:
        raise SystemExit('STOP: broker no longer confirms zero positions/orders.')
print(json.dumps({'NKE_reconciled':True, 'remaining_accounting':snapshot}, indent=2))
PRE_START_PY

systemctl start gala
systemctl is-active gala
git log -1 --oneline
cat VERSION
printf '\nDashboard service started. Trading engine must remain OFF.\n'

# VERIFY_DASHBOARD_PY: wait for Flask, then authenticate and verify loaded
# runtime and unchanged AI production safeguards. Never start an engine.
./.venv/bin/python - <<'VERIFY_DASHBOARD_PY'
import getpass, json, time
import requests
s = requests.Session(); s.trust_env = False
base = 'http://127.0.0.1:5000'
deadline = time.monotonic() + 45
while True:
    try:
        if s.get(base + '/api/auth/status', timeout=(2, 3)).status_code == 200:
            break
    except requests.RequestException:
        pass
    if time.monotonic() >= deadline:
        raise SystemExit('STOP: dashboard readiness not confirmed; send service logs before another restart.')
    time.sleep(0.5)
r = s.post(base + '/api/login', json={
    'username':getpass.getpass('Verify dashboard username: '),
    'password':getpass.getpass('Verify dashboard password: '),
}, timeout=(5, 30))
if r.status_code != 200 or r.json().get('status') != 'ok':
    raise SystemExit('Verification login failed; do not toggle trading.')
def read(path):
    r = s.get(base + path, timeout=(5, 120)); r.raise_for_status()
    data = r.json()
    if data.get('status') != 'ok':
        raise SystemExit('Verification status unavailable.')
    return data
status = read('/api/status'); ai = read('/api/ai/status')
bot = status.get('bot') or {}
if (bot.get('server_version') != '3.3.15' or bot.get('running') is not False
        or bot.get('engine_runtime', {}).get('thread_alive') is not False
        or status.get('scheduler', {}).get('modo') != 'manual'
        or ai.get('auto_activate_production') is not False):
    raise SystemExit('STOP: unexpected post-maintenance runtime state; leave controls unchanged.')
print(json.dumps({'server_version':bot.get('server_version'), 'engine_running':bot.get('running'),
    'engine_runtime':bot.get('engine_runtime'), 'scheduler_mode':status.get('scheduler', {}).get('modo'),
    'automatic_retraining':ai.get('automatic_retraining'), 'auto_activate_production':ai.get('auto_activate_production'),
    'training_workers':ai.get('summary', {}).get('training')}, indent=2))
VERIFY_DASHBOARD_PY
