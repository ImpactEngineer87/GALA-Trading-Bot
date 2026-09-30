# Chocoletra GALA10 v3.3.2 — Kill-Switch Recovery Hotfix

v3.3.2 is a safety/recovery update on top of v3.3.1. It does **not** change GALA10 signal generation, model files, SL/TP mathematics, PAPER/REAL routing, or the v3.3 execution-quality policy.

## Incident fixed

The September 21 kill switch correctly started and closed several positions, but NVDA, NKE and META repeatedly returned `RECONCILIAR`. The uploaded runtime evidence showed that `data/execution_pending.json` still contained exit records from September 15 whose broker status was `DESCONOCIDO`. The final broker reconciliation had no WORKING orders, but those stale local records still prevented a replacement emergency close.

META also produced a Schwab response equivalent to `Order in state FILLED cannot be canceled`; the previous code treated that as a cancellation failure rather than useful terminal evidence.

## Changes

### 1. Broker-authoritative stale pending recovery
During an emergency/kill-switch close, a historical pending exit no longer blocks liquidation forever. v3.3.2 now requires all of the following before retiring a stale pending exit:

1. the old order is proven not to be WORKING;
2. the ticker has no other WORKING broker orders;
3. the current Schwab position is readable.

If Schwab is already flat, the stale pending record is cleared without submitting a duplicate close.

If Schwab still has exposure, the old pending state is atomically replaced with a new emergency-close intent based on the broker's **current side and quantity**, then a fresh MARKET close is submitted.

If any broker read is unavailable, the system still fails closed and does not send a duplicate order.

### 2. Terminal cancel responses are recognized
Schwab errors such as `Order in state FILLED cannot be canceled` are parsed as terminal-state evidence. They are not treated as proof that the account is flat; current broker position is still reconciled before any follow-up action.

### 3. Broker-flat recovery is separated from AI-training validity
If Schwab confirms a position is flat but the historical fill price cannot be recovered, the kill switch may release the exposure safely, but the close is marked `fill_price_verified=false`. That operation is quarantined from the verified x/50 learning set rather than inventing an execution price.

### 4. Stop state distinguishes engine-stopped from broker-flat
The stop path now reports two different states:

- engine stopped + broker flat confirmed;
- engine stopped + unresolved REAL exposure.

A stopped engine with unresolved REAL exposure remains available for another explicit stop/recovery attempt instead of silently discarding the recovery context.

### 5. Start guard after unresolved stop
If a prior stop left unresolved REAL exposure, a fresh start first retries the recovery path. New entries remain blocked until the broker is confirmed flat.

### 6. New audit events
v3.3.2 adds explicit events such as:

- `STALE_EXIT_RETIRED_FOR_EMERGENCY`
- `EXIT_RECOVERED_FLAT`
- `CANCEL_TERMINAL_HINT`

These make future kill-switch incidents traceable without relying on generic `RECONCILIAR` messages.

## Version

- Previous: `3.3.1`
- Current: `3.3.2`
- No configuration migration required.
