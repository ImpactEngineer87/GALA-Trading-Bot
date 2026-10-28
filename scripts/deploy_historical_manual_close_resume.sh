#!/usr/bin/env bash
# Installs reviewed source only. DOES NOT arm REAL or issue a historical grant.
set -Eeuo pipefail
trap 'echo "STOP: upgrade incomplete. Do not start REAL; inspect service state." >&2' ERR
cd /opt/gala/GALA-Trading-Bot
test "$#" = 1
target="$1"
[[ "$target" =~ ^[0-9a-f]{40}$ ]]
test "$(id -u)" = 0
test "$(git rev-parse HEAD)" = cc74a5496ef260062615422ec46013e66a81552a
test "$(git branch --show-current)" = fix/real-signal-execution-controls-20261023
test "$(systemctl is-active gala)" = active
test "$(systemctl show gala -p User --value)" = gala
git fetch --no-tags origin fix/reviewed-historical-manual-close-resume-20261028
test "$(git rev-parse FETCH_HEAD)" = "$target"
git merge-base --is-ancestor HEAD "$target"
git diff --quiet
git diff --cached --quiet
test -z "$(git diff --name-only HEAD "$target" -- .env config.yaml data models dashboard/index.html)"
GALA_TARGET="$target" ./.venv/bin/python - <<'PY'
import os, subprocess
def files(*args):
    return set(subprocess.check_output(["git",*args]).split(b"\0")[:-1])
future=files("diff","--name-only","-z","HEAD",os.environ["GALA_TARGET"])
local=files("ls-files","--others","--exclude-standard","-z")
if future & local:
    raise SystemExit("STOP: release would collide with local untracked files")
print("PASS: repository preflight")
PY

echo "=== ENGINE OFF / AI IDLE / BROKER FLAT ==="
./.venv/bin/python - <<'PY'
import contextlib, getpass, io, os, sys
import requests
from dotenv import load_dotenv
try:
    with open("/dev/tty",encoding="utf-8") as tty:
        print("Dashboard username: ",end="",file=sys.stderr,flush=True)
        user=tty.readline().strip()
    password=getpass.getpass("Dashboard password: ")
except (EOFError,OSError):
    raise SystemExit("STOP: interactive SSH terminal required")
s=requests.Session()
s.trust_env=False
r=s.post("http://127.0.0.1:5000/api/login",json={
    "username":user,"password":password},timeout=(5,30))
if r.status_code!=200 or r.json().get("status")!="ok":
    raise SystemExit("STOP: dashboard authentication failed")
def read(path):
    r=s.get("http://127.0.0.1:5000"+path,timeout=(5,60))
    if r.status_code!=200 or r.json().get("status")!="ok":
        raise SystemExit("STOP: dashboard state unavailable")
    return r.json()
status,ai=read("/api/status"),read("/api/ai/status")
bot=status.get("bot") or {}
if (bot.get("running") is not False
    or (bot.get("engine_runtime") or {}).get("thread_alive") is not False):
    raise SystemExit("STOP: trading engine running")
if (status.get("scheduler") or {}).get("modo")!="manual":
    raise SystemExit("STOP: scheduler must be manual")
if (status.get("stats") or {}).get("real_open_count")!=0:
    raise SystemExit("STOP: local REAL positions exist")
if (type((ai.get("summary") or {}).get("training")) is not int
    or ai["summary"]["training"]!=0 or ai.get("auto_activate_production") is not False):
    raise SystemExit("STOP: AI worker/promotion gate failed")
load_dotenv(".env",override=False)
account=os.getenv("SCHWAB_ACCOUNT_NUMBER","").strip()
if not account or str(bot.get("account_number") or "").strip()!=account:
    raise SystemExit("STOP: configured account mismatch")
with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
    from api.broker_check import ejecutar_broker_check
    report=ejecutar_broker_check("config.yaml",account,"MSFT")
if report.get("ok") is not True:
    raise SystemExit("STOP: broker GET failed")
for name in ("positions","open_orders"):
    val=(report.get("steps") or {}).get(name) or {}
    if val.get("ok") is not True or type(val.get("count")) is not int or val["count"]!=0:
        raise SystemExit("STOP: broker "+name+" not confirmed zero")
print("PASS: engine OFF, AI idle, manual scheduler, broker positions=0/orders=0")
PY

echo "=== PRIVATE BACKUP ==="
umask 077
backup=$(mktemp -d /opt/gala/backups/gala-manual-resume-XXXXXXXX)
for f in .env config.yaml data/real_accounting_reconciliation.json \
  engine/bot_engine.py server.py; do
  test -f "$f"
  cp -a --parents "$f" "$backup/"
done
git rev-parse HEAD > "$backup/previous_commit.txt"
sha256sum .env config.yaml data/real_accounting_reconciliation.json > "$backup/original.sha256"
echo "Private backup: $backup"

echo "=== STOP DASHBOARD, INSTALL CODE, TEST AS gala ==="
systemctl stop gala
test "$(systemctl show gala -p ActiveState --value)" = inactive
test "$(systemctl show gala -p MainPID --value)" = 0
git merge --ff-only "$target"
test "$(git rev-parse HEAD)" = "$target"
for f in core/historical_manual_resume.py scripts/authorize_historical_manual_close.py \
  tests/test_historical_manual_close_resume.py engine/bot_engine.py server.py; do
  chmod a+r "$f"
  runuser -u gala -- test -r "$f"
done

if ! unshare -n -- runuser -u gala -- ./.venv/bin/python -m unittest discover -s tests -q \
  > "$backup/regression.log" 2>&1; then
  tail -n 70 "$backup/regression.log"
  echo "STOP: tests failed; service remains inactive." >&2
  exit 1
fi
./.venv/bin/python - "$backup/regression.log" <<'PY'
import re,sys
from pathlib import Path
output=Path(sys.argv[1]).read_text(errors="replace")
if not re.search(r"^Ran 463 tests\b",output,re.M) or not re.search(
        r"^OK$",output,re.M) or "skipped=" in output:
    raise SystemExit("STOP: expected 463 successful offline tests, zero skipped")
print("PASS: 463 offline tests")
PY
sha256sum -c "$backup/original.sha256"

echo "=== START DASHBOARD, NOT TRADING ENGINE ==="
systemctl start gala
test "$(systemctl is-active gala)" = active
http=000
for i in $(seq 1 25); do
  http=$(curl -s -o /dev/null -w '%{http_code}' \
    http://127.0.0.1:5000/api/auth/status || true)
  test "$http" != 200 || break
  sleep 1
done
test "$http" = 200
sha256sum -c "$backup/original.sha256"
./.venv/bin/python - <<'PY'
import json,yaml
from pathlib import Path
cfg=yaml.safe_load(Path("config.yaml").read_text()) or {}
if (cfg.get("live_trading") or {}).get("historical_manual_close_resume") is True:
    raise SystemExit("STOP: opt-in unexpectedly active")
guard=json.loads(Path("data/real_accounting_reconciliation.json").read_text())
rows=guard.get("unresolved") or {}
if len(rows)!=3 or {row.get("ticker") for row in rows.values()}!={"BA","META"}:
    raise SystemExit("STOP: historical guard scope changed")
print("PASS: 3 original unresolved BA/META rows preserved; feature remains OFF")
PY
echo "CODE UPGRADE COMPLETE. REAL HISTORICAL EXCEPTION NOT AUTHORIZED."
echo "Private backup: $backup"
