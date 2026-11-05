# November 5: META replacement STOP and recurring REAL entry hold

All trading times below are America/New_York. Version 3.3.18 was running when
this incident occurred. This incident is separate from November 4's duplicate
engine and BA stop-cancellation loop.

## Broker evidence

- META's GALA SELL_SHORT LIMIT entry filled 3 shares at 627.59 at 09:35:11.
- Its original protective STOP was replaced through a chain of orders. The
  final replacement BUY_TO_COVER STOP filled 3 shares at 601.25 at 10:06:39.
  Gross realized profit before fees: **79.02 USD**. The final fill is a STOP,
  not a MARKET entry or an inferred fill at the stop's requested price.
- The replacement orders have a different broker tag. A tag does not establish
  who submitted an order. Recovery establishes the exact closing execution,
  without attributing the replacements to the client or to another app.
- Fresh broker reads confirmed an empty account position list and no pending
  orders. The engine still retained META locally while waiting for close proof.
- BA's earlier SELL_SHORT LIMIT order requested 5 shares at 175.62 at 09:40:23,
  when the recorded bid/ask were 175.31/175.47. It had zero fills and was canceled
  at 09:40:52. That entry timeout preceded META's close and is a separate,
  ordinary unfilled LIMIT cancellation. An approved signal does not guarantee
  a fill at its requested limit price.

## Why the hold recurred

Schwab returned the replaced STOP orders with `filledQuantity: 0`, and activities
containing `activityType: EXECUTION`, `executionType: CANCELED`, and zero-price
cancellation legs. The verifier attempted to validate these as fills. Strict
fill validation rejected them, so it could not reach the final valid STOP fill.

The 3.3.18 delayed-fill retry kept running, but repeatedly reached the same
rejection. META therefore remained in `manual_close_pending.json`, and unknown
REAL realized risk held new REAL entries globally. The original five archived
November 2 accounting discrepancies and their review authorization were not
changed by today's incident. Schwab being flat alone could not clear the queue.

This was a missed broker cancellation format in the reconciliation upgrade.
It was not evidence that META's AI model stopped training or that the BA limit
order had executed.

## Version 3.3.19 changes

- Ignore a cancellation activity only when the order is terminal, explicitly
  zero-filled, and every activity is an explicit cancellation with zero price
  and finite valid cancellation quantity. Working orders, claimed fills,
  positive prices, malformed quantities and actual FILL activities retain the
  strict verification path.
- Apply the same classification to external-close proof, known partial-close
  totals, and orphan-entry ownership checks.
- Preserve exact entry/exit order identities, side, quantity, execution price,
  session and net share balance checks. No MARKET fallback or daily-loss bypass
  was added. Verified external closes use the existing active-engine retry path.
- Add a maintenance recovery command for an already persisted pending close.
  Preview performs broker GETs only. Apply requires root, inactive service,
  unfiltered account identity/flat/no-pending evidence, current-session audited
  entry, and one exact matching external exit.
- Link the recovered close to the actual broker fill time and IDs. Recognize
  gross realized risk once using a persisted position/order marker. Retain the
  original accounting guard bytes. Exclude externally altered exits from GALA
  strategy performance and AI labels.

## Verification

The final candidate passed **581 tests** in the isolated Linux worktree as the
`gala` service user with network disabled. Regression cases cover zero-fill
replacement/cancellation records, rejection of contradictory fill claims,
exact replacement STOP recovery, persistence of the risk recovery marker, and
idempotent recovery through the actual history and audit writers.
The existing live retry tests also verify recovery without stopping the engine,
no duplicate realized risk, and no emergency order submission on a verified
manual-flat close.

Read-only preview against today's actual Schwab session records identified only
META, exactly 3 shares, entry 627.59, exit 601.25, gross P/L 79.02, exit type STOP.

Original broker packet and incident evidence are retained privately on the VPS
under `/opt/gala/backups/live-meta-20261105-w4twuzbs`.

## Deployment

The upgrade was pushed to GitHub and deployed on the VPS. The controlled stop
waited for the engine thread to terminate and confirmed Schwab flat. Eight PAPER
positions closed by that maintenance stop were explicitly excluded from AI
training and performance; their original prices and outcomes remain recorded.
Positions that closed normally before the stop retain their existing eligibility.

A Git metadata ownership problem interrupted the first fast forward. Ownership
was restored to the service user. Both index and worktree were checked against
the exact candidate tree before completing that fast forward. A recovery audit
writer argument mismatch was corrected and covered by a real-ledger regression
test. Recovery reused the already persisted history close without duplicating it.
The audit observation time and original broker fill time are recorded separately.

Authenticated checks at **15:49:36 UTC / 10:49:36 New York** confirmed:

- Server version 3.3.19; web service active; trading engine and runner off.
- Fresh Schwab flat/no-pending confirmation; no phantom local META position.
- REAL `entry_blocked=false`; pending manual-close queue empty.
- One META close at 10:06:39, 3 shares, entry 627.59, exit 601.25, gross P/L 79.02.
- REAL realized risk 79.02, recorded once with its position/order marker.
- META's external exit is accounting-verified and excluded from GALA strategy
  performance/training. Its pending learning record was retired without an
  invented training label.
- All 47 protected configuration, routing, production/shadow model and original
  accounting guard file hashes unchanged. The five historical review records
  remain intact. REAL/PAPER daily loss limits remain 200/700 USD.

Private maintenance backup: `/opt/gala/backups/upgrade-3.3.19-efgil7xy`.
Private recovery backup: `/opt/gala/backups/pending-external-close-jaoaxv4y`.
Verification artifact: `data/upgrade_verification_20261105_3319.json`.

The engine was left off for the client's dashboard start. Future verified
external closes use the active engine's existing automatic retry path; they do
not require this maintenance procedure. Incomplete or contradictory broker
evidence and an actual daily loss breach can still hold entries. No claim of a
successful new live-session execution test is made by these read-only checks.
