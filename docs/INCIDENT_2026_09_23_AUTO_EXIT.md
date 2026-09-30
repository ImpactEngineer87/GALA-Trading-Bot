# Incident — 2026-09-23 REAL auto-exit / reversal blocked

## Evidence

Runtime bundle inspected: `auditoria_ordenes.csv`, `execution_pending.json`, `broker_reconciliation_state.json`, `chocoletra.log`, and `dashboard_server.log`.

The four affected REAL tickers had durable pending exits created on 2026-09-15. Those records remained in `execution_pending.json` when new positions were opened on 2026-09-23.

| Ticker | stale exit order | first blocked close (NY) | result |
|---|---:|---:|---|
| NKE | 1007937228308 | 10:01:08 | RECONCILIAR |
| TSLA | 1007932837818 | 10:54:13 | RECONCILIAR |
| NVDA | 1007931387599 | 12:58:07 | RECONCILIAR |
| META | 1007934136542 | 13:40:14 | RECONCILIAR |

Each failure reason was: `Prior exit state cannot be verified against WORKING orders`.

The local protective STOPs from the new September 23 positions were separately canceled/handled, but SmartExit then encountered the old September 15 pending exit and aborted the close. This blocked reversals/opposite-signal closes and later contributed to end-of-day liquidation failure.

On 2026-09-24 the broker reconciliation state reported four live positions but `open_order_symbols: []`. That broker read succeeded through `SchwabClient.obtener_ordenes_abiertas()`. The SmartExit path still returned unknown for its independent WORKING-order read, demonstrating split broker-read state.

## Root cause

Two conditions combined:

1. SmartExit used a separate direct HTTP path for WORKING orders instead of the authoritative SchwabClient path used by reconciliation.
2. v3.3.2 allowed stale pending replacement only for emergency reasons. Even after proving an old order was not live, normal reversal/opposite-signal exits intentionally returned `RECONCILIAR`.

## v3.3.5 correction

- Centralize WORKING-order reads through SchwabClient.
- Permit normal stale-exit recovery only after authoritative no-WORKING-order proof and side-consistent broker exposure.
- Preserve fail-closed behavior for unknown state, conflicting orders, ambiguous positions and side mismatch.
