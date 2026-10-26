#!/usr/bin/env bash
# Original-installation maintenance only. No trading-engine start or order POST.
# Requires fresh operator checks: engine OFF, AI idle, scheduler manual, broker flat.
# Run as root, with dashboard operators paused, passing the reviewed commit SHA.
set -Eeuo pipefail
trap 'printf "\nUpdate halted after an error. Keep trading OFF; send the output.\n" >&2' ERR
cd /opt/gala/GALA-Trading-Bot
umask 022
test "$(id -u)" = 0
test "$(git branch --show-current)" = fix/real-signal-execution-controls-20261023
git diff --quiet
git diff --cached --quiet
test "$(systemctl show gala -p WorkingDirectory --value)" = /opt/gala/GALA-Trading-Bot
test "$(systemctl show gala -p User --value)" = gala
gala_target=${1:?Pass the reviewed full commit SHA}
[[ "$gala_target" =~ ^[0-9a-f]{40}$ ]]
git fetch origin fix/real-signal-execution-controls-20261023
test "$(git rev-parse origin/fix/real-signal-execution-controls-20261023)" = "$gala_target"
test "$(git show "${gala_target}:VERSION")" = 3.3.15
git merge-base --is-ancestor HEAD "$gala_target"
test -z "$(git diff --name-only HEAD "$gala_target" -- .env config.yaml data models)"
unshare -n -- true

if ! runuser -u gala -- node -e 'for(const x of [Request,Headers]) if(typeof x!=="function") process.exit(1)' >/dev/null 2>&1; then
  apt-get update
  apt-get install -y --no-install-recommends nodejs
fi
runuser -u gala -- node -e 'for(const x of [Request,Headers]) if(typeof x!=="function") process.exit(1); console.log("Node UI checks ready:",process.version)'

check_window() {
  ./.venv/bin/python - <<'PY'
from datetime import datetime, timezone
from pathlib import Path
import yaml
from core.auto_ai import policy, window
cfg = yaml.safe_load(Path('config.yaml').read_text()) or {}
if (cfg.get('scheduler') or {}).get('modo') != 'manual':
    raise SystemExit('STOP: scheduler must remain manual.')
if policy(cfg)['enabled'] and window(cfg, datetime.now(timezone.utc))[0]:
    raise SystemExit('STOP: Auto AI is inside its training window; arrange idle maintenance first.')
print('Scheduler manual; Auto AI setting preserved; outside automatic training window.')
PY
}
broker_flat() {
  ./.venv/bin/python - <<'PY'
import os
from dotenv import load_dotenv
load_dotenv('.env', override=False)
from api.broker_check import ejecutar_broker_check
r = ejecutar_broker_check('config.yaml', os.getenv('SCHWAB_ACCOUNT_NUMBER', ''), 'MSFT')
if r.get('ok') is not True:
    raise SystemExit('STOP: broker check failed.')
for name in ('positions', 'open_orders'):
    step = (r.get('steps') or {}).get(name) or {}
    if step.get('ok') is not True or step.get('count') != 0:
        raise SystemExit('STOP: broker exposure or working orders are not confirmed zero.')
PY
}
check_window
broker_flat
systemctl stop gala
test "$(systemctl show gala -p ActiveState --value)" = inactive

umask 077
mkdir -p /opt/gala/backups
gala_backup=$(mktemp -d /opt/gala/backups/gala-before-v3.3.15-XXXXXXXX)
printf 'Private backup: %s\n' "$gala_backup"
git rev-parse HEAD > "$gala_backup/original-commit.txt"
systemctl cat gala > "$gala_backup/gala.service.txt"
sha256sum .env config.yaml > "$gala_backup/private.sha256"
tar --acls --xattrs --numeric-owner --exclude='./.git' --exclude='./.venv' -cpf "$gala_backup/project.tar" .
./.venv/bin/python - "$gala_backup/preserved-files.list" <<'PY'
import os, sys
from pathlib import Path
skip_dirs = {'.git', '.venv', '__pycache__', '.pytest_cache'}
source_suffixes = {'.py', '.pyc', '.html', '.css', '.js', '.md', '.svg', '.png', '.ico'}
with open(sys.argv[1], 'wb') as output:
    for directory, dirs, names in os.walk('.'):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in names:
            path = Path(directory) / name
            if name == 'VERSION' or path.suffix in source_suffixes or name.endswith('.log') or '.log.' in name:
                continue
            output.write(b'./' + os.fsencode(path.as_posix()) + b'\0')
PY
umask 022
git merge --ff-only "$gala_target"
./.venv/bin/python - <<'PY'
import stat, subprocess
from pathlib import Path
for raw in subprocess.check_output(['git', 'ls-files', '-z']).split(b'\0'):
    if not raw:
        continue
    path = Path(raw.decode())
    if path.is_file() and not path.is_symlink() and (path.name == 'VERSION' or path.suffix in {'.py', '.html', '.css', '.js'}):
        path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o444)
PY
runuser -u gala -- test -r server.py
runuser -u gala -- test -r .env
runuser -u gala -- test -r config.yaml
printf '\nRunning the full isolated suite as gala...\n'
if ! unshare -n -- runuser -u gala -- ./.venv/bin/python -m unittest discover -s tests -v > "$gala_backup/regression.log" 2>&1; then
  tail -n 80 "$gala_backup/regression.log"
  exit 1
fi
./.venv/bin/python - "$gala_backup/regression.log" <<'PY'
import re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(errors='replace')
if not re.search(r'^Ran 420 tests\b', text, re.M) or not re.search(r'^OK$', text, re.M):
    raise SystemExit('STOP: expected 420 successful tests with no skips. Inspect regression.log.')
print('\n'.join(line for line in text.splitlines() if line.startswith(('Ran ', 'OK'))))
PY
sha256sum -c "$gala_backup/private.sha256"
tar --compare --file "$gala_backup/project.tar" --no-recursion --null --files-from "$gala_backup/preserved-files.list"
printf 'Preserved non-source/non-log files match the backup.\n'
check_window
broker_flat
systemctl start gala
systemctl is-active gala
git log -1 --oneline
cat VERSION
printf 'Backup and full test log: %s\nTrading engine must remain OFF.\n' "$gala_backup"

