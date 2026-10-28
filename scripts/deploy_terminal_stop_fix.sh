#!/usr/bin/env bash
# Operator-run installer for the exact offline-tested terminal-STOP code.
# Never activates the trading engine; broker reads are GET-only.
set -Eeuo pipefail
trap 'echo "STOP: deployment failed. Keep REAL OFF; inspect output and service state." >&2' ERR
cd /opt/gala/GALA-Trading-Bot

target=cc74a5496ef260062615422ec46013e66a81552a
branch=fix/terminal-stop-broker-flat-reconciliation-20261028
test "$(id -u)" = 0
test "$(git branch --show-current)" = fix/real-signal-execution-controls-20261023
test "$(systemctl show gala -p User --value)" = gala
test "$(systemctl show gala -p WorkingDirectory --value)" = /opt/gala/GALA-Trading-Bot
test "$(systemctl is-active gala)" = active
git fetch --no-tags origin "$branch"
git cat-file -e "$target^{commit}"
git merge-base --is-ancestor "$target" FETCH_HEAD
git merge-base --is-ancestor HEAD "$target"
test "$(git show "$target:VERSION")" = 3.3.15

# Refuse any source collision or changes to private files.
if test -n "$(git diff --name-only HEAD "$target" -- .env config.yaml data models dashboard/index.html)"; then
  echo "STOP: target modifies protected runtime files"; exit 1
fi
changed=$(mktemp); local_files=$(mktemp)
trap 'rm -f -- "$changed" "$local_files"' EXIT
git diff --name-only HEAD "$target" | sort -u > "$changed"
{ git diff --name-only HEAD; git diff --cached --name-only; git ls-files --others --exclude-standard; } | sort -u > "$local_files"
if test -n "$(comm -12 "$changed" "$local_files")"; then
  echo "STOP: uncommitted files conflict with the update"; comm -12 "$changed" "$local_files"; exit 1
fi

echo "=== FRESH STOPPED-ENGINE AND SCHWAB PREFLIGHT ==="
./.venv/bin/python - <<'PY'
import contextlib, getpass, io, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests, yaml
from dotenv import load_dotenv
from core.auto_ai import policy, window
try:
    with open("/dev/tty", "r", encoding="utf-8") as tty:
        print("Dashboard username: ", end="", file=sys.stderr, flush=True)
        username = tty.readline().strip()
    password = getpass.getpass("Dashboard password: ")
except (OSError, EOFError):
    raise SystemExit("STOP: interactive SSH terminal required")
if not username or not password:
    raise SystemExit("STOP: dashboard credentials missing")
s = requests.Session()
s.trust_env = False
try:
    r = s.post("http://127.0.0.1:5000/api/login",
               json={"username": username, "password": password}, timeout=(5, 30))
    if r.status_code != 200 or r.json().get("status") != "ok":
        raise ValueError("Login failed")
    def read(path):
        r = s.get("http://127.0.0.1:5000" + path, timeout=(5, 60))
        if r.status_code != 200 or r.json().get("status") != "ok":
            raise ValueError("Dashboard status unavailable")
        return r.json()
    status, ai = read("/api/status"), read("/api/ai/status")
except (requests.RequestException, ValueError):
    raise SystemExit("STOP: dashboard authentication or status check failed")
finally:
    password = None

bot = status.get("bot") or {}
runtime = bot.get("engine_runtime") or {}
if bot.get("running") is not False or runtime.get("thread_alive") is not False:
    raise SystemExit("STOP: engine/thread not confirmed OFF")
if status.get("scheduler", {}).get("modo") != "manual":
    raise SystemExit("STOP: scheduler must be manual")
if type(status.get("stats", {}).get("real_open_count")) is not int or status["stats"]["real_open_count"] != 0:
    raise SystemExit("STOP: local REAL positions not confirmed zero")
if type(ai.get("summary", {}).get("training")) is not int or ai["summary"]["training"] != 0:
    raise SystemExit("STOP: training workers not confirmed idle")
if ai.get("auto_activate_production") is not False:
    raise SystemExit("STOP: automatic AI model promotion enabled")
load_dotenv(".env", override=False)
account = os.getenv("SCHWAB_ACCOUNT_NUMBER", "").strip()
if not account or str(bot.get("account_number") or "").strip() != account:
    raise SystemExit("STOP: configured account differs from dashboard")
cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8")) or {}
now = datetime.now(timezone.utc)
if policy(cfg)["enabled"] and any(window(cfg, stamp)[0] for stamp in (now, now + timedelta(minutes=10))):
    raise SystemExit("STOP: automatic AI training window in progress/too close")
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from api.broker_check import ejecutar_broker_check
        check = ejecutar_broker_check("config.yaml", account, "MSFT")
except Exception:
    raise SystemExit("STOP: live broker check unavailable")
if check.get("ok") is not True:
    raise SystemExit("STOP: live broker check failed")
for name in ("positions", "open_orders"):
    step = (check.get("steps") or {}).get(name) or {}
    if step.get("ok") is not True or type(step.get("count")) is not int or step["count"] != 0:
        raise SystemExit("STOP: %s not verified zero" % name)
print("PASS: engine OFF, scheduler manual, AI idle, Schwab positions/orders zero")
PY

echo "=== PRESERVE CURRENT DASHBOARD AND DATA ==="
umask 077
backup=$(mktemp -d /opt/gala/backups/gala-stop-fix-XXXXXXXX)
git rev-parse HEAD > "$backup/previous-commit.txt"
git diff --binary HEAD > "$backup/uncommitted.patch"
git status --porcelain -uall > "$backup/git-status.txt"
for f in engine/bot_engine.py dashboard/index.html data/real_accounting_reconciliation.json .env config.yaml; do
  if test -f "$f"; then cp -a --parents "$f" "$backup/"; fi
done
sha256sum .env config.yaml data/real_accounting_reconciliation.json > "$backup/private.sha256"
echo "Private backup: $backup"

echo "=== STOP DASHBOARD; INSTALL TESTED COMMIT ==="
systemctl stop gala
test "$(systemctl show gala -p ActiveState --value)" = inactive
test "$(systemctl show gala -p MainPID --value)" = 0
git merge --ff-only "$target"
test "$(git rev-parse HEAD)" = "$target"
# Root-owned 0600 source files previously caused crash loops.
for f in server.py engine/bot_engine.py scripts/upgrade_original_v315.sh; do
  test -f "$f"; chmod a+r "$f"; runuser -u gala -- test -r "$f"
done
while IFS= read -r f; do
  case "$f" in
    *.py|*.sh|*.html|*.js) test ! -f "$f" || chmod a+r "$f" ;;
  esac
done < "$changed"

echo "=== 450 OFFLINE REGRESSIONS AS gala ==="
if ! unshare -n -- runuser -u gala -- ./.venv/bin/python -m unittest discover -s tests -q > "$backup/regression.log" 2>&1; then
  tail -n 70 "$backup/regression.log"
  echo "STOP: test failures; dashboard remains stopped" >&2
  exit 1
fi
./.venv/bin/python - "$backup/regression.log" <<'PY'
import re, sys
from pathlib import Path
log = Path(sys.argv[1]).read_text(errors="replace")
if not re.search(r"^Ran 450 tests\b", log, re.M) or not re.search(r"^OK$", log, re.M) or "skipped=" in log:
    raise SystemExit("STOP: expected 450 successful tests without skips")
print("PASS: 450 offline tests as gala")
PY
sha256sum -c "$backup/private.sha256"

echo "=== START WEB DASHBOARD WITH ENGINE OFF ==="
systemctl start gala
test "$(systemctl is-active gala)" = active
http=000
for i in $(seq 1 25); do
  http=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5000/api/auth/status || true)
  test "$http" != 200 || break
  sleep 1
done
test "$http" = 200
sha256sum -c "$backup/private.sha256"
echo "=== FINAL STATE ==="
git log -1 --oneline
systemctl show gala -p ActiveState -p MainPID -p NRestarts --no-pager
./.venv/bin/python - <<'PY'
import json
from pathlib import Path
u = json.loads(Path("data/real_accounting_reconciliation.json").read_text()).get("unresolved") or {}
print("Unresolved historical REAL accounting:", len(u))
print("REAL accounting still blocked:", bool(u))
PY
echo "DEPLOYMENT COMPLETED. DO NOT ENABLE REAL TRADING."
echo "Private backup: $backup"
