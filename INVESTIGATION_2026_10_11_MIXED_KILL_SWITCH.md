# MIXED kill switch, REAL concurrency and PAPER exits — v3.3.23

Reviewed main `e4bda6d` (v3.3.22). The operator reports, for a MIXED session
with five REAL tickers and twelve PAPER tickers:

1. Only one REAL ticker trades at a time.
2. A large PAPER loss on the last session (Friday, Oct 9).
3. The automatic kill switch did not close the open REAL position.

No VPS logs, ledger or config were supplied with this report. Every cause below
is confirmed in source and reproduced offline. Which ones fired on the VPS needs
`data/auditoria_ordenes.csv` (`ENTRY_BLOCKED` / `CAPACITY`), the trade history
and `logs/chocoletra.log`.

## Kill switch

- **PAPER was never checked in MIXED mode.** `daily_risk_snapshot` evaluated only
  the REAL book whenever any ticker was REAL, so PAPER could lose without limit.
  v3.3.22 documented this as an open policy question. The operator has now
  asked for the kill switch to act, so v3.3.23 checks **both** books against their
  own limits. Either breach blocks entries and liquidates **all** PAPER and REAL
  positions until the next New York session.
- **A failed REAL exit hid its own loss.** A ticker with `exit_pending` returns
  before market data, so its price aged past 120 s. The position then counted as
  "unpriced" at $0, and the REAL limit could not trip. This matches the Oct 9
  panel: REAL $0 / -$200 with "valoración incompleta" while NVDA SELL 5 was open.
  Two changes:
  - Every cycle re-quotes stale open positions in one batched request.
  - A stale price still counts its *loss* (never its gain) toward the kill
    switch. It remains "unpriced" for capacity.
- **Kill-switch close retries gave up after one hour.** They now continue until
  16:05 ET, never less than the old hour. They still end at the New York date
  change and are never extended by their own retries.
- **Leftover exposure was never retried.** After the trigger's single
  liquidation, the blocked cycle did nothing more. Untracked broker shares or a
  failed final sweep stayed open. The sweep now re-runs every 2 minutes while
  local positions remain or broker flatness is unconfirmed.

## Only one REAL position at a time

- **Filled positions were counted twice.** `RealCapacityManager` subtracted
  every open REAL position's notional from Schwab's available funds, which
  already exclude it. Each position therefore used about 2× its cost.
  - Example, $3,000 account: NVDA 5 × $180 fills and $2,100 remains, but
    capacity saw $1,200. TSLA 3 × $430 needs $1,354.50, so it was blocked.
  - The fix: fills older than `capacity.balance_settle_sec` (default 60 s) no
    longer reserve notional. In-flight entries, working orders and fresh fills
    still do. Stop risk and the position count apply to every position.
  - The audit detail adds `positions_already_in_broker_balance`.
- **Any stale REAL price blocked every REAL entry**, through the same
  unpriced-exposure gate. The per-cycle re-quote above addresses it. The
  dashboard capacity reason is now updated in this branch as well.
- **The balance read was fragile.**
  - It ran under the engine-wide lock, stalling every worker and the order
    monitor during an HTTP call.
  - It used background capacity, so warmup could defer it, failing as
    "Confirmed broker balance required".
  - It now runs outside the lock, under control capacity. `reserve()` remains
    serialized and counts in-flight reservations.
- **Cash accounts:** with no `buyingPower` field, buying power read as $0 and
  blocked every REAL entry. It now falls back to the available cash.

## PAPER losses

- **Order size could exceed the dashboard's share count.** With per-ticker
  execution and `risk.sizing_mode: atr`, the hybrid multiplier enlarged orders
  ×2 (AI ≥ 80%) or ×2.5 (master signal). PAPER is capped at 100,000 shares. REAL
  orders could exceed the capital and stop risk that capacity had reserved.
  Per-ticker execution now forces both executors to fixed size.
- **PAPER stops were skipped during market-data waits.** While MACRO_STALE or
  WARMUP_PENDING, PAPER positions had no SL/TP simulation. The close-only path
  now fetches fresh closed 1m bars and runs PAPER SL/TP on them, then checks
  for an opposite signal. PAPER reads do not use control capacity.
- **Only the newest candle was simulated.** A stop touched on a bar skipped by
  a wait or a slow cycle was never applied. Unmanaged closed bars after the
  entry bar and cursor are now replayed oldest-first, up to 30, stopping at the
  first close. v3.3.22 chronology rules are unchanged.
- **Dashboard PAPER session PnL omitted banked TP1 profit.** Partial-exit profit
  of still-open positions is now included.

## Not changed (operator decision)

PAPER approves every raw GALA10 signal: the AI threshold and hard vetoes apply
only to REAL, and opposite signals always close and reverse. That is a
strategy/training policy, not a defect, and it can churn PAPER on choppy days.
Tightening it would change PAPER's role as the AI learning stream.

## Validation

- **New suite:** `tests/test_v3_3_23_mixed_kill_switch.py` has 32 offline tests.
  They load the actual production method bodies with real pandas, the real
  `DailyLossGuard`, `RealCapacityManager` and `server`, fake broker I/O and
  clocks.
- **Updated tests:** three earlier tests encoded the old policies — the MIXED
  PAPER limitation, the one-hour kill retry, and PAPER being skipped during
  waits. They were updated to the new behaviour.
- **Full suite:** `pytest tests` gives the same 28 failures plus 3 collection
  errors as main `e4bda6d`, with no new failures. These need `config.yaml`,
  playwright or other runtime files. Dashboard VM tests pass.
- **Not run:** live integration and VPS deployment. No orders, credentials or
  broker calls were used.

```bash
python -m pytest tests/test_v3_3_23_mixed_kill_switch.py -q
```

## Deployment checks

- Check Schwab directly for open positions and WORKING orders before upgrading.
  Stopping Python does not close a broker position.
- Preserve `.env`, `config.yaml` and `data/`. No config change is required.
  Review `kill_switch.daily_loss.paper_limit_usd`: in MIXED mode, a PAPER breach
  now also liquidates the REAL positions.
- After the restart, the startup log should show version 3.3.23. The daily-risk
  card should list the PAPER book next to the REAL one.
