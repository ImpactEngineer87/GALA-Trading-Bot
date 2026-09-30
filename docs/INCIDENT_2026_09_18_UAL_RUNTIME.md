# Incident analysis — UAL / 2026-09-18

## Client symptom

The Signals page showed a UAL SELL at approximately 13:34, while the chart did not show the signal. The client also asked whether PAPER trading was operating and later reported that the bot stopped.

## What actually happened

### UAL PAPER execution was operating
The persistent signal ledger records:

- ticker: UAL
- side: SELL
- timestamp: 2026-09-18 13:34:04 New York
- signal price: $108.19
- AI probability: about 59.7%
- execution mode: PAPER
- AI decision: APPROVED

The engine log records the corresponding PAPER entry request at 13:34:04 for 20 shares at $108.19, and the persistent trade history records `POSITION_OPEN` with the same signal id.

Therefore this specific UAL signal was **not blocked**. PAPER trading opened the position.

### Why the chart marker was missing
A deeper check of the candle database shows the final archived UAL source candle was `13:33:00`. This is consistent with the engine design: a 1-minute bar that opens at 13:33 becomes fully closed at 13:34, and the multithreaded decision was emitted a few seconds later at `13:34:04`.

v3.3.0 plotted/filtered the signal using only its decision wall-clock timestamp. That made the 13:34:04 decision look later than the last archived source candle and the marker was excluded even though the signal was genuinely generated from that 13:33 bar.

v3.3.1 persists two timestamps for new signals: the decision time and the exact closed GALA10 source-bar time. The chart uses the source-bar time for placement while retaining 13:34:04 in the history/audit display. For legacy rows, the chart infers the immediately preceding closed minute for display only.

### Why UAL showed a 0% threshold
UAL was using a SHADOW model in PAPER. Its model metadata shows a decision threshold of 0.41 and balanced accuracy of 60.88%. The signal row, however, persisted the production-trainer fallback threshold (0.0) while its immutable AI decision context correctly contained `threshold=0.41`.

v3.3.1 uses the threshold from the exact decision snapshot and repairs the display of historical rows at read time.

### What happened later to Schwab connectivity
At approximately 13:38:55 New York, the logs begin reporting that the authenticated Schwab user token was unavailable/expired and refresh did not restore an authorized session.

Because both REAL execution and PAPER simulation depend on genuine Schwab market data, the engine correctly refuses to treat synthetic fallback frames as executable prices. During such an outage:

- REAL entry/execution stays fail-closed.
- PAPER does not pretend synthetic data is real market data.
- Trading/data processing therefore appears paused until Schwab authorization is restored.

v3.3.1 makes this condition less noisy and more explicit in logs; it does not weaken the fail-safe behavior.

### Shutdown issue found
A later stop initiated the kill-switch path. v3.3.0 allowed the dashboard stop path and the engine thread's final cleanup to enter shutdown concurrently. The same period also contains a Windows file-sharing error while replacing `broker_reconciliation_state.json.tmp`.

v3.3.1 serializes shutdown and broker-reconciliation persistence to remove both races.

## Important limitation of the historical evidence
The engine log proves that a stop/kill-switch sequence occurred, but the v3.3.0 dashboard control log was memory/console-only. The uploaded archive does not contain a durable record proving whether that stop was initiated from the dashboard, scheduler, or another process-level action.

v3.3.1 adds `logs/dashboard_server.log` and logs the start/stop request **before** executing the control action, so the source will be auditable in future incidents.
