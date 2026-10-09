# NVDA opposite-signal close and kill-switch recovery — v3.3.20

Reviewed main `ca1662f0eaf1ee6d01f861ea44a908b4cd7158d0` (v3.3.19).

The client reports that an NVDA short stayed open after an opposite chart
signal and a kill-switch request. The screenshots show five short shares.
They do not establish which GALA10 signal the bot received, whether Schwab
accepted/rejected an order, or the time at which the bot attempted a close.
The historical cause requires the matching NVDA execution audit and logs.

## Confirmed source gaps

- `_senal_estrategia` consumes a closed candle even if its requested close later
  fails. There was no retained engine close intent, and the order monitor only
  checked entry/protection orders. A one-shot opposite signal could therefore
  leave exposure unresolved until another signal or manual stop.
- Market-close shutdown stops the strategy and order monitor even if the
  bounded kill-switch attempts cannot confirm flatness. Retaining the engine
  for a manual retry did not provide automatic close-only recovery.
- The close wrapper did not put its whole sequence in control-request scope.
  Position/quote reads could consume background capacity instead of the
  reserved capacity needed to close risk.
- SmartExit called `Session.post` directly. Its actual closing submissions
  were absent from the shared request budget, burst limit and pause handling.
- Macro `WARMUP_PENDING` / `MACRO_STALE` returned before evaluating the 1m
  opposite signal, including when the bot already had a REAL position.

The first and fourth gaps were reproduced against the exact reviewed baseline:
a failed close retained no intent, and a successful SmartExit POST incremented
no shared-gate request counter.

## Changed behavior

1. A requested REAL close retains an intent for the exact tracked position.
   Failed attempts keep the position and block further signal/entry processing
   for that ticker. The strategy and order monitor retry the intent when due;
   a separate close-only worker continues after strategy/monitor shutdown.
2. Retry spacing is at least 30 seconds, or the remaining shared Schwab pause
   if longer. Recovery never evaluates AI or opens the opposite side. The
   original reversal is not replayed as a delayed entry after recovery.
3. Automatic recovery ends after one hour or at a New York session-date change.
   An unresolved position remains visible and emits an operator error. Intents
   do not transfer to another account, side, position identity or engine.
   A pending entry adopted from a confirmed late fill during kill-switch
   cancellation retains its kill intent; cancellation itself is also paced.
4. The entire close/liquidation sequence uses control capacity, including
   position/quote reads, POST, cancellation and flat confirmation. All traffic
   still respects the global budget, burst limit and upstream pause. The final
   kill-switch sweep does not bypass a tracked position's retry deadline.
5. SmartExit POST uses `schwab_request(..., recovery_safe=False)`, disables
   redirects and is counted in request metrics. A mutation cannot probe route
   recovery. A locally unsent or definite HTTP 401/403/429 rejected leg is
   retired so the durable operation can resume later. Timeout, missing order
   ID and other uncertain outcomes remain journaled and cannot be replayed
   blindly. Confirmed partial fills are preserved when a remainder is deferred.
6. During macro warmup/staleness, only an existing REAL position can request
   fresh native 1m bars for opposite-signal close-only evaluation. Forming bars
   are excluded; the latest closed bar must be at most 120 seconds old, with a
   valid positive price and sufficient history. Auth/micro failures do not
   trigger a backup data request. Entries still require the normal MTF/AI path.
7. Position snapshots expose `exit_pending`, `exit_reason`, `exit_attempts`
   and `exit_recovery_expired`. Broker flatness and verified fill prices remain
   separate facts. Unknown exposure is never treated as flat.

## Validation

176 focused offline regressions passed, including 39 new recovery/traffic
tests. Actual production method bodies, the complete SmartExit implementation
and the shared request gate are exercised with fake broker I/O and clocks.
Coverage includes long/short closing instructions, shutdown recovery, cooldown,
same-symbol concurrency, pending-entry adoption, position/account/session
fencing, fresh/stale/forming bars, partial fills, cancel/fill races, uncertain
POSTs and global/control request capacity.

Run the dependency-light checks:

```bash
python -m unittest discover -s tests -p test_v3_3_15_exit_control.py
python -m unittest discover -s tests -p test_v3_3_16_market_data_recovery.py
python -m unittest discover -s tests -p test_v3_3_17_warmup_admission.py
python -m unittest discover -s tests -p test_v3_3_18_order_read_backoff.py
python -m unittest discover -s tests -p test_v3_3_19_reliable_real_exits.py
python -m unittest discover -s tests -p test_schwab_request_budget_offline.py
python -m unittest discover -s tests -p test_v3_3_20_pending_exit_recovery.py
```

Compilation and whitespace checks also passed. Full application/pytest and
live integration were not run: this workspace lacks requests, Flask, pytest
and python-dotenv. A broader discovery also hit the unrelated Flask-dependent
AI-threshold module. No credentials, VPS changes or live orders were used.

## Deployment and incident checks

- Confirm the current NVDA position and WORKING orders directly in Schwab.
  Stop strategy entries while exposure remains unresolved. Stopping Python
  does not close a broker position, and this recovery worker requires the
  dashboard process to remain alive.
- Deploy v3.3.20 after resolving/reviewing existing exposure. Preserve `.env`,
  `config.yaml` and `data/`, including OAuth state, execution journals and
  reconciliation state. No configuration change or credential reset is needed.
- Run one bot instance from one VPS/account owner. The shared traffic budget
  is per process, not an account-wide limit across multiple running bots.
- Verify the running version, request metrics and authoritative broker
  reconciliation before enabling REAL entries. In paper/staging, exercise an
  opposite signal and shutdown before returning to live operation.
- Match NVDA `POSITION_CLOSE_REQUESTED`, `PROTECTION_CANCEL_RESULT`,
  `EXIT_SUBMITTED`, `EXIT_FILLED` / `EXIT_RECOVERED_FLAT`,
  `OPPOSITE_SIGNAL_CLOSE_FAILED` and `EXIT_RECOVERY` records to the chart time.
  Review `logs/chocoletra.log`, `logs/schwab_http_errors.jsonl` and NVDA rows in
  `data/auditoria_ordenes.csv` / `data/execution_v2.jsonl`. Redact credentials
  and account identifiers before sharing incident evidence.

Recovery cannot guarantee a fill while Schwab is unavailable, orders remain
ambiguous, the market is closed or the process is stopped/replaced. Durable
SmartExit journals and startup broker reconciliation remain necessary across
process restarts; in-memory engine intents are not replayed into a new engine.
