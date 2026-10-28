#!/usr/bin/env bash
# Reviewed one-time NKE historical reconciliation on already upgraded cc74a54.
# Never starts trading engine, modifies order/history/model files, or sends an order.
# Uses strictly scoped broker evidence; any uncertain state refuses and leaves
# the dashboard stopped after the maintenance stop.
set -Eeuo pipefail
trap 'echo "STOP: NKE maintenance halted. Keep REAL OFF. Check status and private evidence before retrying." >&2' ERR

cd /opt/gala/GALA-Trading-Bot
plan=/opt/gala/backups/gala-accounting-plan-4vp9bp6u/plan_private.json
review_commit=4735e8da08015f077b862179c5874a79e4cebd1f
installed=cc74a5496ef260062615422ec46013e66a81552a

test "$(id -u)" = 0
test "$(git rev-parse HEAD)" = "$installed"
test "$(git branch --show-current)" = fix/real-signal-execution-controls-20261023
test "$(systemctl show gala -p ActiveState --value)" = active
test "$(systemctl show gala -p User --value)" = gala
test "$(systemctl show gala -p WorkingDirectory --value)" = /opt/gala/GALA-Trading-Bot
test -f "$plan" && test ! -L "$plan"
git cat-file -e "$review_commit^{commit}"

echo "=== VERIFY PATCHED SCHWAB ACCOUNT-HASH RECONCILER ==="
temp=$(mktemp -d /tmp/gala-nke-reviewed-XXXXXXXX)
apply_source="$temp/reconcile_verified_history.py"
git show "$review_commit:scripts/reconcile_verified_history.py" > "$apply_source"
test -s "$apply_source"
grep -Fq 'client.obtener_posiciones(account_hash) == [] and client.obtener_ordenes_abiertas(account_hash) == []' "$apply_source"
chmod 600 "$apply_source"

echo "=== 450 TESTS IN ISOLATED REVIEW TREE (NO NETWORK) ==="
review="$temp/review"
mkdir -p "$review"
git worktree add --detach "$review" "$review_commit" >/dev/null
if ! (cd "$review" && unshare -n -- env PYTHONDONTWRITEBYTECODE=1 \
  /opt/gala/GALA-Trading-Bot/.venv/bin/python -m unittest discover -s tests -q \
  > "$temp/regressions.log" 2>&1); then
  tail -n 70 "$temp/regressions.log"
  echo "STOP: patched reconciler regression suite failed" >&2
  exit 1
fi
if ! grep -Eq '^Ran 450 tests\b' "$temp/regressions.log" ||
   ! grep -Eq '^OK$' "$temp/regressions.log" ||
   grep -q 'skipped=' "$temp/regressions.log"; then
  tail -n 40 "$temp/regressions.log"
  echo "STOP: expected 450 passing, zero skipped tests" >&2
  exit 1
fi
echo "PASS: 450 offline tests; broker account-hash regression covered"

echo "=== AUTHENTICATED ENGINE-OFF, PLAN, AI, BROKER PREFLIGHT ==="
./.venv/bin/python - "$plan" <<'PY'
import contextlib, getpass, hashlib, io, json, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests, yaml
from dotenv import load_dotenv
from core.auto_ai import policy, window
from core.project_paths import resolve_runtime_path

def require(condition, reason):
    if not condition:
        raise SystemExit("STOP: " + reason)

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
root = Path.cwd()
cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) or {}
guard = Path(resolve_runtime_path((cfg.get("execution_engine") or {}).get(
    "accounting_state_path", "data/real_accounting_reconciliation.json"
), project_root=root))
raw = guard.read_bytes()
state = json.loads(raw)
unresolved = state.get("unresolved") or {}
require(plan.get("format") == 1 and plan.get("action") == "ARCHIVE_VERIFIED_PAST_ACCOUNTING"
    and plan.get("root") == str(root) and plan.get("ticker") == "NKE"
    and plan.get("session_ny") == "2026-10-23"
    and plan.get("position_ref") == "d744cdfefff2", "unexpected plan identity")
require(hashlib.sha256(json.dumps({k:v for k,v in plan.items() if k != "fingerprint"},
    sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    == plan.get("fingerprint"), "plan fingerprint invalid")
require(hashlib.sha256(raw).hexdigest() == plan.get("guard_sha256"),
    "accounting state changed since plan preparation")
require(len(unresolved) == 5 and len(plan.get("keys") or []) == 2
    and all(k in unresolved and unresolved[k].get("ticker") == "NKE" for k in plan["keys"]),
    "NKE records or accounting scope changed")
require((cfg.get("scheduler") or {}).get("modo") == "manual",
    "scheduler not manual")
now = datetime.now(timezone.utc)
require(not policy(cfg)["enabled"] or not any(window(cfg, tick)[0]
    for tick in (now, now + timedelta(minutes=10))),
    "automatic AI training window too close")
try:
    with open("/dev/tty", "r", encoding="utf-8") as tty:
        print("Dashboard username: ", end="", file=sys.stderr, flush=True)
        username = tty.readline().strip()
    password = getpass.getpass("Dashboard password: ")
except (OSError, EOFError):
    raise SystemExit("STOP: interactive SSH terminal required")
require(bool(username and password), "dashboard credentials unavailable")
s = requests.Session()
s.trust_env = False
try:
    response = s.post("http://127.0.0.1:5000/api/login",
        json={"username": username, "password": password}, timeout=(5,30))
    require(response.status_code == 200 and response.json().get("status") == "ok",
        "dashboard login failed")
    def read(path):
        r = s.get("http://127.0.0.1:5000" + path, timeout=(5,60))
        require(r.status_code == 200 and r.json().get("status") == "ok",
            "dashboard status unavailable")
        return r.json()
    status, ai = read("/api/status"), read("/api/ai/status")
finally:
    password = None
bot = status.get("bot") or {}
runtime = bot.get("engine_runtime") or {}
require(bot.get("running") is False and runtime.get("thread_alive") is False,
    "trading engine or thread still running")
require((status.get("scheduler") or {}).get("modo") == "manual",
    "runtime scheduler not manual")
require(type((status.get("stats") or {}).get("real_open_count")) is int
    and status["stats"]["real_open_count"] == 0,
    "local REAL exposure not verified zero")
require(type((ai.get("summary") or {}).get("training")) is int
    and ai["summary"]["training"] == 0,
    "training not confirmed idle")
require(ai.get("auto_activate_production") is False,
    "automatic model promotion must remain disabled")
load_dotenv(".env", override=False)
account = os.getenv("SCHWAB_ACCOUNT_NUMBER", "").strip()
require(bool(account) and str(bot.get("account_number") or "").strip() == account,
    "configured Schwab account differs from dashboard")
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from api.broker_check import ejecutar_broker_check
    check = ejecutar_broker_check("config.yaml", account, "MSFT")
require(check.get("ok") is True, "fresh broker health check failed")
for name in ("positions", "open_orders"):
    item = (check.get("steps") or {}).get(name) or {}
    require(item.get("ok") is True and type(item.get("count")) is int
        and item["count"] == 0,
        "broker " + name + " not confirmed zero")
print("PASS: NKE plan unchanged, engine OFF, manual scheduler, AI idle, Schwab flat")
PY

echo "=== PRESERVE PRIVATE GUARD AND PLAN ==="
umask 077
backup=$(mktemp -d /opt/gala/backups/gala-before-nke-apply-XXXXXXXX)
cp -a -- "$plan" "$backup/original-plan-private.json"
cp -a -- data/real_accounting_reconciliation.json "$backup/original-guard-private.json"
echo "Private pre-apply backup: $backup"

echo "=== STOP WEB SERVICE FOR ATOMIC NKE APPLY ==="
systemctl stop gala
test "$(systemctl show gala -p ActiveState --value)" = inactive
test "$(systemctl show gala -p MainPID --value)" = 0

echo "=== APPLY EXACT NKE PLAN WITH FRESH SCHWAB EVIDENCE ==="
if ! ./.venv/bin/python "$apply_source" \
    --runtime-root /opt/gala/GALA-Trading-Bot --apply "$plan" \
    > "$backup/nke-result.json"; then
  cat "$backup/nke-result.json"
  echo "STOP: NKE apply refused or unknown. Dashboard remains stopped." >&2
  exit 1
fi

echo "=== VERIFY ONLY NKE RECORDS RESOLVED ==="
./.venv/bin/python - "$plan" "$backup/nke-result.json" <<'PY'
import json, sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
result = json.loads(Path(sys.argv[2]).read_text())
state = json.loads(Path("data/real_accounting_reconciliation.json").read_text())
unresolved = state.get("unresolved") or {}
if result.get("status") not in {"APPLIED", "ALREADY_APPLIED"}:
    raise SystemExit("STOP: apply result not verified")
if len(unresolved) != 3 or sorted({r.get("ticker") for r in unresolved.values()}) != ["BA", "META"]:
    raise SystemExit("STOP: unexpected remaining accounting records")
if any(k in unresolved for k in plan["keys"]):
    raise SystemExit("STOP: NKE guard not resolved")
if plan["position_id"] not in (state.get("verified_historical_reconciliations") or {}):
    raise SystemExit("STOP: archived NKE proof missing")
print("PASS: NKE 2 accounting records archived; 3 BA/META remain; REAL still blocked")
PY

echo "=== CONFIRM SCHWAB STILL FLAT BEFORE DASHBOARD START ==="
./.venv/bin/python - <<'PY'
import contextlib, io, os
from dotenv import load_dotenv
load_dotenv(".env", override=False)
account = os.getenv("SCHWAB_ACCOUNT_NUMBER", "").strip()
if not account:
    raise SystemExit("STOP: configured account unavailable")
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from api.broker_check import ejecutar_broker_check
    r = ejecutar_broker_check("config.yaml", account, "MSFT")
if r.get("ok") is not True:
    raise SystemExit("STOP: final broker check failed")
for name in ("positions", "open_orders"):
    v = (r.get("steps") or {}).get(name) or {}
    if v.get("ok") is not True or type(v.get("count")) is not int or v["count"] != 0:
        raise SystemExit("STOP: broker " + name + " not confirmed flat")
print("PASS: Schwab positions=0, open orders=0")
PY

echo "=== START DASHBOARD SERVICE ONLY ==="
systemctl start gala
test "$(systemctl is-active gala)" = active
http=000
for i in $(seq 1 25); do
  http=$(curl --silent --output /dev/null --write-out '%{http_code}' \
    http://127.0.0.1:5000/api/auth/status || true)
  test "$http" != 200 || break
  sleep 1
done
test "$http" = 200
echo "PASS: Dashboard HTTP 200"

echo "=== AUTHENTICATED POST-RESTART ENGINE-OFF CHECK ==="
./.venv/bin/python - <<'PY'
import getpass, sys
import requests
try:
    with open("/dev/tty", "r", encoding="utf-8") as tty:
        print("Verify dashboard username: ", end="", file=sys.stderr, flush=True)
        username = tty.readline().strip()
    password = getpass.getpass("Verify dashboard password: ")
except (OSError, EOFError):
    raise SystemExit("STOP: terminal unavailable for postrestart verification")
s = requests.Session(); s.trust_env = False
try:
    r = s.post("http://127.0.0.1:5000/api/login",
        json={"username": username, "password": password}, timeout=(5,30))
    if r.status_code != 200 or r.json().get("status") != "ok":
        raise ValueError("login failed")
    r = s.get("http://127.0.0.1:5000/api/status", timeout=(5,30))
    if r.status_code != 200 or r.json().get("status") != "ok":
        raise ValueError("status check failed")
    status = r.json()
except (requests.RequestException, ValueError):
    raise SystemExit("STOP: postrestart authenticated dashboard verification failed")
finally:
    password = None
bot = status.get("bot") or {}
accounting = ((status.get("daily_risk") or {}).get("real_accounting") or {})
if bot.get("running") is not False or (bot.get("engine_runtime") or {}).get("thread_alive") is not False:
    raise SystemExit("STOP: engine or thread unexpectedly running; do not enable REAL")
if (status.get("scheduler") or {}).get("modo") != "manual" or accounting.get("blocked") is not True or accounting.get("unresolved") != 3:
    raise SystemExit("STOP: unexpected scheduler or accounting state")
print("PASS: dashboard engine OFF; 3 unresolved BA/META; REAL blocked")
PY
echo "NKE RECONCILIATION COMPLETE. DO NOT START REAL TRADING."
echo "Private backup: $backup"
