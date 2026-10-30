# GALA10 Oct 22 REAL signals and order execution upgrade (draft review)

## Evidence and scope

Operator screenshots from the Oct 22, 2026 NY session show:
- Thinkorswim GALA10 SELL markers and independently generated signals on
  TSLA/BA/META/NKE; GALA dashboard signals are not proof of broker submissions.
- NKE working STOP and late/unfilled entry concerns.
- Previous NVDA/NKE/TSLA `RECONCILIAR` reversal failures.
- Differences between GALA's theoretical trade P/L and Thinkorswim account
  P/L Day. These measures are not directly equivalent.

An external AI's six suggested root causes **must be verified**, not accepted
as fact. The source already:
- Sends **SELL_SHORT** for REAL short entries. Invalid directions are now
  explicitly rejected rather than silently mapped to SELL_SHORT.
- Uses `ThreadPoolExecutor(max_workers=...)` to scan tickers in parallel.
  `/api/signals` POST is a manual/dashboard signal recorder, **not** the
  authoritative strategy webhook execution path. No evidence yet demonstrates
  a dropped 14:26 TSLA signal due to a single-threaded webhook server.
- Maintains an independent broker order-monitor thread; it confirms entry
  fills and attempts cancellations. A default general entry timeout of 180s
  was too long for the requested REAL LIMIT policy.
- Already cancels tracked SL/TP and verifies absence of working orders before
  a reversal close. A blanket `CANCEL_ALL` without differentiating manual
  account orders or confirmed broker exposure is unsafe.

## New changes in this review branch

1. A REAL entry LIMIT timeout of **20 seconds by default**, using the
   existing independent broker order monitor. Config:
   `execution_engine.entry_limit_timeout_sec`. Applies to LIMIT or
   marketable-LIMIT *entry* orders only when execution v2 is enabled.
   Broker working/cancel state and position reconciliation must succeed before
   releasing a pending slot. If a cancel is ambiguous, local state remains
   pending (never send a duplicate order). Legacy MARKET/PAPER paths keep
   their original configured timeout.
2. Optional strict **0.00–0.10 USD** price tolerance on submitted REAL LIMIT
   entries. Config: `execution_engine.entry_limit_tolerance_usd`.
   Defaults to **0.00 (off)** until the operator explicitly approves. Set
   **0.05** or **0.10** only after testing; BUY moves up, SELL_SHORT moves
   down by this maximum extra concession. Does not force MKT, chase stale
   orders, or guarantee fills. Unreasonable config fails closed.
3. Explicit Schwab open-position day P/L **read-only** endpoint
   `GET /api/broker/open-position-pnl` and manual refresh card in Trades.
   Reads authenticated existing SchwabClient `obtener_posiciones` data.
   Missing P/L values stay unavailable. This is P/L Day **of currently open
   broker positions**, not the bot's strategy trade P/L, not a broker-wide
   realized account ledger, and not necessarily Thinkorswim's account P/L
   Day. No API calls on each dashboard 6-second refresh.
4. Offline read-only `scripts/audit_signal_order_funnel.py` joins archived
   GALA signals to order-audit events **only by exact signal_id**, reports
   unlinked records separately, and displays `live_trading.allow_short`.
   Use it to distinguish strategy not observed / AI veto / entry blocked /
   submitted-not-filled / actual entry fill. It does not invent attribution
   or read Schwab. Does not replace broker fill reconciliation.
5. Tests in
   `tests/test_real_signal_execution_controls.py`,
   `tests/test_broker_open_position_pnl.py`,
   `tests/test_signal_order_funnel.py`.

## Verify without changing the ACTIVE VPS process

Create a separate test worktree outside the production working tree:

```bash
cd /opt/gala/GALA-Trading-Bot
git fetch origin
git worktree add --detach /opt/gala/GALA-Trading-Bot-exec-review \
  origin/fix/real-signal-execution-controls-20261023
cd /opt/gala/GALA-Trading-Bot-exec-review
../GALA-Trading-Bot/.venv/bin/python -m unittest discover -s tests \
  -p 'test_real_signal_execution_controls.py' -v
../GALA-Trading-Bot/.venv/bin/python -m unittest discover -s tests \
  -p 'test_broker_open_position_pnl.py' -v
../GALA-Trading-Bot/.venv/bin/python -m unittest discover -s tests \
  -p 'test_signal_order_funnel.py' -v
../GALA-Trading-Bot/.venv/bin/python scripts/audit_signal_order_funnel.py \
  --date 2026-10-22 --tickers TSLA NKE BA META NVDA \
  --runtime-root /opt/gala/GALA-Trading-Bot
```

The per-session audit does NOT contact Schwab or change files, orders, position
states, P/L, counters, or AI gates. **Never restart/stop** an active REAL
process merely to run these checks; bot shutdown can liquidate live positions.
Changes in the worktree do not deploy into the existing running Python
process.

If the reviewer later approves bounded entry slippage, add under the existing
`execution_engine:` block of `config.yaml`, without replacing any existing
settings:

```yaml
entry_limit_timeout_sec: 20
entry_limit_tolerance_usd: 0.05
```

Do this **only in an explicitly scheduled, supervised REAL deployment**,
after checking broker positions/open orders and all tests. No hot editing of
risk settings while real positions are active.

## Remaining live dependencies

- Match specific TOS markers and the exact **confirmed one-minute candle** to
  the bot's archived signal/decision and order lifecycle. TOS signal may be
  displayed on a forming candle while GALA waits for closed-bar confirmation.
- If `live_trading.allow_short` is false, a REAL SELL entry is blocked by
  deliberate risk policy even if AI approves a similar signal; the client
  must separately authorize and preflight enabling short trading.
- Before considering lost 14:26 signals, inspect the durable archived
  signal-to-order funnel and API latency/concurrency metrics. Existing
  concurrent ticker workers mean Redis/Celery is not the first justified fix.
- No blanket broker `CANCEL_ALL`: current symbol-scoped cancel-confirm and
  position-reconciliation rules must remain in force to protect existing and
  manually managed positions.
- Historical REAL training counts still require genuine execution evidence,
  as documented in the parent PR #15; supplemental non-executed signals
  appropriately do not advance x/50. Separate optional post-session automatic
  retraining PR #14 is NOT enabled by this branch.
- Price-tolerance and P/L fixes, while committed, **have not been tested on
  the user's VPS or deployed**. The code may need additional fixes based on
  actual broker replies. Do not promise fills or profitability.


## Follow-up: actual broker-order funnel evidence, 2026-10-22 (NY)

Operator ran the read-only audit from the **live runtime files**:
- `TSLA 10:51:11 SELL APPROVED`: no linked order event or learning skip
  reason. Cause not established. Check whether GALA had a same-side
  position, reserved entry, failed preflight, or only incomplete audit
  linkage; do **not** retry historical order.
- `BA 14:26:05 SELL APPROVED`: likewise no linked order event or reason.
  A previous `BA 13:35:34 SELL` had a linked `ENTRY_FILLED` event, so
  duplicate same-side suppression is **plausible but unproven** without
  timestamped position state.
- `NKE 12:29:39 BUY APPROVED`: a real LIMIT was submitted/acknowledged
  and then `ENTRY_TIMEOUT_CANCELED` without a linked verified fill.
  Another `NKE 12:12:07 SELL` was likewise canceled. Existing historical
  timeouts can be inspected from recorded event reason; the new 20s
  default was not active in that session.
- `META 14:02:04 SELL APPROVED`: broker entry submitted/acknowledged
  then `ENTRY_TIMEOUT_CANCELED`; this reversal also has
  `EXIT_RECOVERED_FLAT` and requires strict broker evidence before
  any learning label. Other META exits had unresolvable order IDs.
- `NVDA 11:35:05 SELL APPROVED`: `POSITION_CLOSE_FAILED` and
  `REVERSAL_ABORTED`, so sending a new SELL_SHORT would be unsafe.
  `NVDA 13:17:03 BUY` was independently submitted then timed out.
- Live config showed `live_trading.allow_short=True`, so short trading
  wasn't disabled. No evidence of a dropped webhook or wrong
  Schwab `SELL_SHORT` field in this incident.

### Additional traceability / AI integrity

- Future approved signals skipped without broker submission now create
  `SIGNAL_EXECUTION_SKIPPED` with exact signal_id and structured reason
  including same-side, reserved slot, entry-slot capacity and pending
  reconciliation. Future entry timeout/cancel events carry the same ID.
- Read-only incident audit additionally joins saved supplemental learning
  reasons and **only exact** previously linked broker order IDs for old
  cancellations. If the old event recorded its elapsed age (e.g., `180s`),
  it is displayed; no elapsed duration is invented.
- When broker order detail reports a filled quantity but no actual average
  execution price, GALA still tracks the REAL position and attempts normal
  protection. It may use a conservative reference/cost-basis price to
  manage exposure, but **must not mark the entry confirmed for training**.
  Entry `entry_confirmed` now also requires an order identity and a
  positive finite price sourced from the actual broker order details,
  not simply a local state transition or fallback LIMIT. PAPER behavior
  remains unchanged. Legacy history is untouched.
- Regression tests:
  `test_signal_order_funnel.py` and
  `test_real_entry_broker_price_evidence.py`.
  As of this note, the tests have **not** been executed on the VPS.
- The dashboard's existing `x/50` means 50 **new verified closed
  executions**, not approvals, canceled LIMITs, or signal observations.
  No automatic model retraining, risk-guard override, manual promotion
  or broker order replays are introduced.


## Manual AI retraining button fix (2026-10-23)

Screenshot after the first live dashboard deployment showed:
- `Auto IA OFF`, `17/17` datasets suitable for FIT/validation/TEST,
  but **only** RCL `60/50`, MSFT `51/50`, ADBE `55/50` were
  `PENDING_RETRAIN`. Readiness and `x/50` trigger are distinct counters.
- Clicking `Reentrenar pendientes` was unable to start. Proven bug:
  `server.py:/api/ai/train` passed `solo_elegibles=True`, and
  `EntrenadorAgentes.solicitar_reentreno_todos()` unconditionally
  returned False if `automatic_retraining.enabled=False`. It accidentally
  applied the automatic worker gate to explicit manual requests.

Fix:
- `/api/ai/train` explicitly passes `manual_request=True`; the
  training scheduler honors this **only for pending-only manual requests**.
  Unattended `al_fin_sesion()` keeps `manual_request=False` and remains
  blocked when Auto IA OFF. `eligible_tickers()` still enforces >=50 new
  verified closed observations; no history/counter changes to pass the gate.
- Manual route still refuses while the trading engine is running.
  The worker still computes candidate in SHADOW, preserves actual-only
  validation and TEST, and **cannot promote PRODUCTION automatically**
  unless the existing separate configured authorization applies.
- Dashboard renames the misleading `17/17` label to
  `Datos aptos para evaluación`, tells the operator the job is queued,
  and refreshes lifecycle at 1/5/15 seconds rather than implying an
  accepted model immediately.
- Tests in `tests/test_manual_retrain_auto_off.py` check manual access,
  scheduler isolation, 50-close eligibility, no unauthorized production
  activation, and deferred insufficient evidence.

Deployment: Source changes in GitHub branch alone do not update a
previously running Python process. Run new tests and full offline
regression in the original source checkout after safely pausing only
when there is no unresolved GALA-managed Schwab exposure. Restart only
the dashboard/server on manual scheduler after broker preflight.
Trigger the Train button only with trading engine STATIONARY and
watch `journalctl -u gala -f --no-pager`. A SHADOW candidate can be
REJECTED or WAITING_DATA; that is an evaluation outcome, not evidence
the Train button failed.
