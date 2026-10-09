# REAL close and kill switch not confirmed — v3.3.19

## Evidence

On October 8 (v3.3.18, 5 REAL + 12 PAPER) the dashboard showed `Motor parado ·
cierre REAL no confirmado` with one REAL trade open after the session ended.
The red card said `No se pudo confirmar Schwab órdenes: SchwabRequestDeferred:
order_read_backoff; espere 54s` (updated 15:45:29). The rejections were:

| Ticker | Reason |
|---|---|
| META | No se pudieron leer las órdenes de Schwab (`order_read_backoff`) |
| NKE | No se pudieron leer las órdenes de Schwab (`order_read_backoff`) |
| TSLA | No se pudieron leer las órdenes de Schwab (`order_read_backoff`) |
| NVDA | `timeout waiting for ['CANCELED', 'CANCELLED', 'EXPIRED', 'REJECTED']` |

The client reported that closes are not working.

In v3.3.18 single-order reads have their own pause, so `order_read_backoff` on
the order list means **Schwab's order-list endpoint itself failed**. The
3.3.18 change made this likely: the open-position monitor read the full order
list (two days, every status) about every 5 seconds. October 6 had already
shown order-list reads returning HTTP 400 with broker status 500.

The redacted error log did not record which list query failed (WORKING-only or
full). v3.3.19 now records it (see Changes).

## Root cause

- Every REAL close, including the end-of-session kill switch, does four things
  in order. It cancels the TP1 and STOP orders, confirms they are no longer
  working, confirms the symbol has no working orders, and then reads the
  position and liquidates.
- Confirmation reads the account's WORKING-order list (`status=WORKING`).
  In v3.3.18 that list shared one failure pause with the full order list. When
  the monitor's frequent full-list reads failed, the pause also refused the
  WORKING-list reads. The cancellation then timed out (NVDA), the close could
  not be confirmed, and the engine stopped, keeping the position marked open
  (fail-closed by design).
- Entry checks for other REAL tickers were refused the same way.
- A STOP filled by Schwab could not be confirmed either. The close required
  the order's detail (`FILLED`), which comes from the single-order and
  full-list reads, both failing for this account.
- A TP1 fill had the same dependency. If its fill price was unknown the old
  code re-protected the remainder but never marked TP1 done, so every monitor
  poll would cancel and replace the STOP again.

## Changes

- **Isolated order-read pauses** (`api/schwab_transport.py`). Each order read
  has its own failure pause, keyed by query and traffic class:
  - **Queries:** single order, WORKING list and full list.
  - **Traffic classes:** order-action/control and background.

  A failing background poll can never pause the WORKING-list reads that
  closes, cancellations, the kill switch and entry checks use. The pause
  timings (5 s doubling to 60 s), request limits and the HTTP 403/429 handling
  are unchanged.
- **Error log names the failing list.** `logs/schwab_http_errors.jsonl` now
  adds `orders_query` (`status=WORKING` or `status=ALL`) for order-list
  failures. Still no account or order identifiers.
- **Cancellations use the order-action reserve**
  (`api/hybrid_order_executor.py`). `cancelar_orden_confirmada` and its
  confirmation reads run as control traffic, so a busy background budget
  cannot leave a cancellation, or the close waiting on it, unconfirmed.
- **Monitor on reliable sources.** `estado_orden_vigilada` gives the monitor
  an order's status in three cases:
  - **Still working:** reported as WORKING from a WORKING list shared for 5 s
    by every open position.
  - **Left the list, detail readable:** its final status.
  - **Left the list, no detail:** `NOT_WORKING`.

  The full list is read only when an order leaves the WORKING list. The engine
  (`engine/bot_engine.py`) then uses the broker position:
  - **TP1:** gone, with the position reduced to the 30% remainder → TP1
    executed. The remainder is re-protected once and `tp1_hit` is set.
  - **STOP:** gone and the broker flat → closed by the STOP.
  - **Otherwise:** an order that is gone while the position is unchanged, or
    an unreadable position, causes no action.
- **STOP close confirmation without order detail.** When Schwab returns no
  detail, the STOP being absent from the WORKING list **and** the broker
  being flat confirms the close. A STOP still working, or an unreadable list
  or position, still keeps the position open. The exit price is then marked
  unverified, so the trade stays out of the verified x/50 learning set.
- **TP1 with an unknown price.** `tp1_hit` is now set, ending the repeated
  STOP replacement. Realized PnL for TP1 stays pending; nothing is invented
  from the limit price.
- **Not changed:** entry safety (the WORKING list must be read and must be
  clean), liquidation and flat confirmation, the kill switch, and
  daily-loss rules.

## Offline verification

The October 8 failure mode was simulated for 30 minutes: single-order and
full-list reads always fail and the WORKING list works. The monitor polls TP1
and STOP every 2 s, and order-action WORKING-list reads (closes, kill switch,
entry checks) run every 30 s:

| | WORKING-list reads refused | monitor status unknown |
|---|---|---|
| v3.3.18 | 60 of 60 | 1800 of 1800 |
| v3.3.19 | 0 of 60 | 0 of 1800 |

In the test version of this scenario, a STOP filled at minute 20 is detected
and closed within 8 s, using at most 4 single-order or full-list requests.
That test is in `tests/test_v3_3_19_reliable_real_exits.py` (20 tests), which
also covers:
- isolated pauses and the new error-log field
- the shared WORKING read, and order-gone, detail-readable and unreadable
  outcomes
- cancellations running as control traffic
- monitor decisions from position evidence
- the TP1 one-time re-protection
- STOP close confirmation: confirmed when gone and flat; not confirmed while
  working, unreadable or not flat

Three earlier tests were updated for the new behavior:
- the WORKING list is now still read after the other order reads fail;
- the 3.3.18 monitor test now expects the new monitor status call;
- the `test_execution_safety` executor double gained the new monitor status
  method. Its assertion is unchanged.

With `requirements-dev.txt` installed (Python 3.13, Windows), the full
`pytest tests` suite passes 538 tests on this branch and 509 on `main`
(3.3.18). The difference is the new exit and AI-threshold tests. The same 31
tests fail on both, so those failures predate this change; three modules
cannot be collected on either because their `MIGRATE_CONFIG_V2_8/V2_9`
scripts are missing. Four dashboard VM tests also pass. No live broker,
credentials, VPS or real orders were used.

## VPS steps after review

1. **Before anything else, check the Schwab app or website** for open
   positions and working orders (NVDA from October 8). The engine was stopped
   with that close unconfirmed and does not manage it while stopped.
2. Deploy `VERSION` and the source together, restart the backend, and confirm
   3.3.19.
3. After the next session, send the error log tail:
   ```powershell
   Get-Content logs\schwab_http_errors.jsonl -Tail 40
   ```
   The `orders_query` field now shows whether the WORKING list or the full
   list is the one failing. If the **WORKING** list fails too, Schwab cannot
   confirm any close for this account. That would need to go to Schwab Trader
   API support with the correlation IDs.
