# Chocoletra GALA10 v3.3.3 — Broker-Authoritative Local State Sync

v3.3.3 is a narrow synchronization hotfix on top of v3.3.2. It does **not** change GALA10 signals, AI models, stop/TP math, PAPER/REAL routing, position sizing, entry logic, or the v3.3 execution-quality policy.

## Problem fixed

After the v3.3.2 kill-switch recovery, Schwab could correctly report the account as `FLAT` while the stopped engine still retained old REAL rows in its in-memory `_posiciones` registry. The dashboard therefore showed `REAL 3` even after the read-only Schwab reconciliation proved there were no broker positions and no working orders.

## New behavior

When the operator presses **Verificar Schwab (solo lectura)** and the engine is stopped:

1. Schwab positions and working orders are read successfully.
2. For each explicitly `REAL` local position, Schwab is treated as the authority.
3. If that ticker has neither a broker position nor a working order, the local REAL row is retired.
4. The corresponding local state-machine entry and REAL capacity reservation are released.
5. A stale durable exit row in `execution_pending.json` is retired only after the authoritative flat proof.
6. Resolved current-failure banners for that ticker are cleared from the live reconciliation state.
7. PAPER positions are never modified by this cleanup.
8. No order is submitted, changed, or canceled by this synchronization path.

If the engine is active, or Schwab cannot successfully confirm broker state, no local position is retired.

## Dashboard

The read-only reconciliation button now refreshes Dashboard/Trades immediately after a successful synchronization and reports which local REAL tickers were retired.

## Version

- Previous: `3.3.2`
- Current: `3.3.3`
- No configuration migration required.
