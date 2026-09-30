# v3.3.0 Safety Editor 4

This build corrects the incident observed on 2026-09-16 without weakening the
daily-loss protection.

## Corrected behavior

- Synthetic fallback candles are diagnostic only. They cannot update prices,
  calculate PnL, close PAPER positions, manage REAL positions, or submit orders.
- Cached synthetic prices are rejected by the emergency-price path.
- Daily-loss cards remain tied to the original trigger mode, trigger PnL, and
  trigger limit. Historical PAPER PnL is displayed separately from REAL PnL.
- A daily-loss liquidation is attempted once. Authentication failure no longer
  causes cancellation/liquidation requests to repeat every cycle.
- The daily lock survives VPS restarts and weekends and is cleared only on the
  next New York trading day.
- Starting while locked returns an explicit reason and next reset timestamp.
- The dashboard can run a Schwab **read-only** reconciliation while the engine is
  stopped. This performs GET requests only; it never submits, changes, or cancels
  an order.
- Open-trade count falls back to confirmed broker positions when the engine is
  stopped.

## Operator procedure for the current incident

1. Keep the engine stopped for the rest of the locked session.
2. In Schwab, confirm the NKE/NFLX positions and protective orders.
3. Use **Verificar Schwab (solo lectura)** in the dashboard. This updates the
   broker status but does not clear unresolved execution intent or touch orders.
4. Do not delete `data/daily_kill_switch_state.json` or
   `data/execution_pending.json`.
5. After the next New York trading-day reset, refresh Schwab again and start only
   when the dashboard shows known/confirmed exposure.

The original PAPER loss rows remain in history for auditability; they are not
broker-confirmed REAL losses and are not silently deleted by this update.
