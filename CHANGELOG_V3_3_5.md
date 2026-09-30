# Chocoletra GALA10 v3.3.5 — Auto Exit / Stale Pending Recovery

v3.3.5 fixes the September 23 REAL auto-exit/reversal failure seen on NKE, TSLA, NVDA and META. It does not change GALA10 signal generation, AI models, retraining mathematics, risk sizing, PAPER/REAL routing or production gates.

## Incident confirmed from runtime files

The four REAL positions opened normally on September 23, but the first later close attempt for each was blocked by an older durable exit record from September 15:

- NKE: old exit order `1007937228308`; first blocked close 10:01:08 NY.
- TSLA: old exit order `1007932837818`; first blocked close 10:54:13 NY.
- NVDA: old exit order `1007931387599`; first blocked close 12:58:07 NY.
- META: old exit order `1007934136542`; first blocked close 13:40:14 NY.

All returned: `Prior exit state cannot be verified against WORKING orders`.

The September 24 reconciliation simultaneously proved `open_order_symbols: []` while the execution layer continued reporting WORKING-order state as unknown. The cause was a split broker-read path: dashboard/startup reconciliation used `SchwabClient.obtener_ordenes_abiertas()`, while SmartExit used a separate direct HTTP session.

## Fixed

### 1. One authoritative WORKING-order provider

The REAL executor now receives the same `SchwabClient.obtener_ordenes_abiertas()` provider used by startup/dashboard reconciliation. SmartExit therefore shares OAuth refresh, account handling and order visibility with the rest of the bot.

`None` remains unknown/fail-closed. `[]` is authoritative evidence that no WORKING orders exist.

### 2. Safe recovery for normal reversals / opposite-signal exits

v3.3.2 could recover stale pending exits automatically only for emergency/kill-switch reasons. Therefore an ordinary reversal could remain blocked forever by an old journal row.

v3.3.5 allows stale recovery for a normal close only after all of these are proven:

1. the historical pending order is not WORKING;
2. no other WORKING order exists for the ticker;
3. current Schwab exposure is readable;
4. current broker side matches the local side the engine intends to close.

Only then is the old pending record atomically replaced with a fresh exit built from the broker-confirmed remaining quantity. Normal exits continue through the existing marketable-LIMIT policy; emergency exits remain MARKET.

### 3. Safety preserved

The bot still refuses a replacement close when:

- WORKING-order state cannot be read;
- any broker order for the symbol is still WORKING;
- broker position is unknown/ambiguous;
- broker side conflicts with the local position during a normal close.

This prevents duplicate closes and accidental reversals.

## Version

- Previous: `3.3.4`
- Current: `3.3.5`
- No config migration required.
