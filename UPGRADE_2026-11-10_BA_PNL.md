# BA entry, latency and P/L review - 2026-11-10

## Confirmed causes

- The highlighted BA BUY at 10:03:46 New York came from the 10:00 candle,
  which closed at 10:01. Analysis arrived 166 seconds late and the bot correctly
  recorded historical analysis without submitting a retroactive order. This
  happened before the 10:44 deployment of 3.3.24.
- Later BA signals did reach Schwab. At 10:54:04.529, the bot submitted a BUY
  LIMIT of $172.09 while the fresh ask was $172.40. The order remained unfilled
  and timed out. Forcing reactive entry intent to LIMIT had lost the executable
  quote price. A similar mismatch occurred at 13:55 ($173.49 versus $173.60 ask).
- BA also had confirmed ordinary fills at 13:34 ($173.00, five shares) and 14:25
  ($174.10, five shares). An approved signal is distinct from submission and fill.
- 3.3.24 improved calculation and display speed, but market-data reads still
  downloaded two days of minute bars per ticker per cycle. Profiling found
  network reads dominating analysis time. Most cycles were shorter, but a
  72.562-second cycle at 13:23 still produced a late analysis.

## What 3.3.25 changes

- Reactive REAL entries use a fresh ask for BUY or bid for SELL_SHORT, bounded
  by the existing 0.2% slippage cap and any stricter explicit dollar tolerance.
  Broker orders remain LIMIT. Passive entries and explicit zero dollar tolerance
  retain their reference price. Pending orders keep the existing timeout policy.
- Warm minute-data refreshes request ten minutes of overlap and retain the full
  cached warmup. Full reads every five minutes pick up older broker corrections.
  Stopped analysis workers do not publish signals after a completed data read.
- Signal details show candle-close time, analysis time and delay, then actual
  linked submission time, LIMIT price and quote when evidence exists.
- A read-only P/L breakdown lists totals by ticker and every contributing close,
  with entry/exit prices, quantities and exclusions. The headline explicitly
  identifies PAPER simulation. The brand version follows the running server.
- Known maintenance and verified manual exclusions no longer make otherwise
  verified performance totals unavailable. Unknown executions still show N/D.
  Manual closes remain excluded from GALA performance and training; accounting
  verification, risk guards, thresholds, models, routing and quantities are unchanged.

## The -$371.96 screenshot

At 13:44:07 New York, eligible closed PAPER trades totalled **-$397.05** and the
screenshot showed **+$25.09** in open PAPER P/L: **-$371.96 combined**. This was
not a Schwab account loss or REAL trading total. Eight controlled maintenance
closes were excluded. The offline replay reconstructs the closed total from
89 recorded eligible closes; no recorded prices or P/L values were changed.

At the separate 14:26 snapshot, PAPER was -$390.38. Verified GALA REAL closes
totalled +$20.01, with four verified manual closes (-$11.26) shown separately.
These figures describe different snapshots and are not interchangeable.

## Verification and rollout

- **682 Linux tests passed**, with external networking disabled.
- Replayed **19,530 history rows** against the previous annotation logic: prices,
  P/L and accounting/performance verification flags were identical. Only the
  display classification of verified manual exclusions changed.
- Both observed unmarketable BA limits replay to the fresh ask within the
  existing 0.2% cap. This demonstrates pricing, not a guaranteed live fill.
- Screenshot PAPER total and the separate current snapshot reconcile exactly.
- Deployed source `9763b15` at approximately **16:02 New York**, after the
  configured 15:45 session close and fresh Schwab verification of zero positions
  and pending orders. The engine remains off; neither the accounting hold nor
  daily-loss latch is active. No REAL position was closed for this deployment.
- Increased VPS analysis workers **2 to 4** on the existing four-CPU host.
  API pacing remains unchanged. After that explicit configuration adjustment,
  **all 63 protected files were byte-identical across the source deployment and
  web restart**. No new maintenance PAPER close required exclusion.
- The read-only P/L endpoint and performance summary agree: **139 eligible
  PAPER closes, -$495.71**; **19 verified GALA REAL closes, +$28.69**; **four
  verified manual closes, -$11.26**, outside GALA performance. Eight earlier
  maintenance closes remain excluded. No unverified closes were present.
- Native candle samples retain full cached history and match the full fetch's
  overlapping closed OHLCV values. The provider may return more bars than the
  requested ten-minute overlap; a smaller request is not a guaranteed response
  size or latency. Full live signal/order latency for 3.3.25 remains to be
  observed next session, because deployment completed after market close.

The first broker-flat check also detected a separate three-share AAPL position
with a working stop. Schwab subsequently reported that stop filled; deployment
waited for the subsequent zero-position, zero-pending-order verification. No
deployment action canceled or changed that order.

Deployment receipt: `data/upgrade_verification_20261110_3325.json`.
Backup: `/opt/gala/backups/upgrade-3.3.25-bphcbsqi/`.

Private evidence is retained under `data/ba-pnl-review-20261110-4pn0pmiu/` on the
VPS. No credentials or broker identities are included in this report.
