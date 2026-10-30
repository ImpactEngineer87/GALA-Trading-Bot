#!/usr/bin/env bash
# Guarded VPS deployment/start: MIXED configuration, PAPER execution only.
# Does NOT edit config, accounting, ticker routing, or approval files.
set -Eeuo pipefail
umask 077
cd /opt/gala/GALA-Trading-Bot
BASE=050a1a8efdd7e35a733a2199db596410b7827779
FILES=(server.py core/mixed_accounting_start.py tests/test_mixed_accounting_start.py scripts/start_mixed_paper_fallback.py)
if [[ "$(git rev-parse HEAD)" != "$BASE" ]]; then
  echo "REFUSED: VPS git HEAD differs from reviewed baseline; no changes" >&2
  exit 2
fi
if [[ -n "$(git status --porcelain -- "${FILES[@]}")" ]]; then
  echo "REFUSED: target code files already modified; no changes" >&2
  exit 2
fi
if ! systemctl is-active --quiet gala; then
  echo "REFUSED: dashboard service gala is not active; no changes" >&2
  exit 2
fi
mkdir -p /opt/gala/backups
backup=$(mktemp -d /opt/gala/backups/gala-mixed-safety-20261030-XXXXXXXX)
cp -p server.py "$backup/server_before_mixed.py"
patch="$backup/mixed_safe.patch"
git diff --binary "$BASE" FETCH_HEAD -- "${FILES[@]}" > "$patch"
[[ -s "$patch" ]] || { echo "REFUSED: empty deployment patch" >&2; exit 2; }
git apply --check "$patch"
applied=0
restart_attempted=0
cleanup() {
  code=$?
  if [[ $code -ne 0 && $applied -eq 1 && $restart_attempted -eq 0 ]]; then
    if git apply -R "$patch"; then
      echo "DEPLOYMENT_ABORTED: patch rolled back; dashboard not restarted" >&2
    else
      echo "WARNING: inspect modified code; automatic rollback failed" >&2
    fi
  fi
}
trap cleanup EXIT

git apply "$patch"
applied=1
.venv/bin/python -m py_compile server.py core/mixed_accounting_start.py scripts/start_mixed_paper_fallback.py
.venv/bin/python -m unittest discover -s tests -p test_mixed_accounting_start.py -v

echo "=== PRE-RESTART SAFETY CHECK ==="
.venv/bin/python scripts/start_mixed_paper_fallback.py --check-stopped

echo "=== RESTART DASHBOARD (ENGINE CONFIRMED STOPPED) ==="
restart_attempted=1
systemctl restart gala
systemctl is-active --quiet gala

ready=0
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl --silent --fail --max-time 3 http://127.0.0.1:5000/api/auth/status > /dev/null; then
    ready=1
    break
  fi
  sleep 1
done
[[ "$ready" == 1 ]] || { echo "REFUSED: dashboard not reachable after restart" >&2; exit 2; }

echo "=== AUTHENTICATED MIXED PAPER-ONLY ENGINE START ==="
.venv/bin/python scripts/start_mixed_paper_fallback.py
echo "CODE_BACKUP: $backup"
echo "DEPLOYMENT_COMPLETE: MIXED_CONFIGURED_WITH_REAL_QUARANTINED"
