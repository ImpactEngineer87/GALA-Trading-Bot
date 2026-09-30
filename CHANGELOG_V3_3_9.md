# Chocoletra GALA10 v3.3.9 — Signal Learning Visibility

v3.3.9 is an observability/dashboard release on top of v3.3.8. It does **not** change GALA10 signal generation, model thresholds, broker execution, stop/TP rules, PAPER/REAL routing, x/50 counting, or model promotion gates.

## Added

### 1. Dedicated Signal Learning dashboard
The Signals page now includes **Visibilidad · Aprendizaje de señales** with global KPIs, a per-ticker table, ticker filtering, and a recent-observation ledger.

Visible per ticker:
- execution-valid historical observations,
- current x/50 progress,
- AI/model vetoes,
- missed/non-executed signals,
- observations still being followed,
- counterfactual TP and SL outcomes,
- vetoes that avoided a losing signal,
- vetoes that incorrectly blocked a would-be winner,
- supplemental FIT-eligible observations,
- excluded/ambiguous/timeout observations,
- veto usefulness percentage,
- last learning update.

### 2. Recent learning observations
Operators can inspect the latest observations with:
- timestamp,
- ticker,
- PAPER/REAL context,
- BUY/SELL direction,
- source/reason,
- current or final outcome,
- supplemental FIT eligibility,
- R multiple,
- AI probability/context where available.

### 3. Read-only API
Added `GET /api/ai/signal-learning` with optional `ticker` and `limit` filters. The endpoint is authenticated and read-only.

### 4. Clear learning effectiveness metrics
For AI/model veto observations, v3.3.9 separates:
- **avoided losses**: the blocked signal later reached SL,
- **blocked winners**: the blocked signal later reached TP,
- **veto usefulness %** = avoided losses / (avoided losses + blocked winners).

These are counterfactual audit metrics, not broker PnL.

## Safety invariants unchanged
- x/50 advances only from execution-valid closed trades.
- Counterfactual signal observations never enter VALIDATION or TEST.
- Only eligible, unambiguous TP/SL observations may supplement FIT at the configured reduced weight.
- Ambiguous same-bar outcomes and timeouts remain excluded from supervised labels.
- The visibility layer never sends, cancels, or modifies broker orders.

## Version
- Previous: `3.3.8`
- Current: `3.3.9`
- No configuration migration or model retraining required.
