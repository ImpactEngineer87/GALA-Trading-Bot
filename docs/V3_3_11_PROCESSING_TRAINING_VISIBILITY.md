# v3.3.11 Processing and Training Visibility

## Why only a few tickers may appear to work

The engine schedules the full ticker universe each normal cycle, but a ticker can return before execution.

Pipeline:
Ticker worker → market data → GALA10 → AI → risk/broker → execution.

Possible final states:
- PROCESSED_NO_SIGNAL: worker ran normally but GALA10 produced no BUY/SELL.
- VETOED: GALA10 produced a signal and AI rejected the entry.
- BLOCKED: risk, capacity, OAuth, reconciliation, or broker safety blocked execution.
- QUARANTINED: market data/model/inference health was not safe enough for REAL.
- ORDER_SUBMITTED / EXECUTED: execution reached the broker/PAPER engine.

## Signal learning vs retraining

Blocked/missed signals are followed to a clean hypothetical TP/SL and may become supplemental FIT candidates.
They do not increment x/50 and do not trigger retraining by themselves.
When a normal eligible/manual ticker retraining runs, eligible supplemental rows resolved before the FIT cutoff can be added at reduced weight.
v3.3.11 shows how many supplemental rows were actually used in the last retraining.

## Schwab edge 403

HTML Access Denied / errors.edgesuite.net is distinct from OAuth JSON invalid_client or invalid_grant.
The HTML edge denial is treated as transient and retried with bounded backoff.
Hard OAuth errors still require explicit operator reauthorization.
