# v3.3.9 Signal Learning Visibility

## Why this exists
v3.3.7 began recording non-executed GALA10 signals so each ticker model could build a more complete learning history. The client then requested direct visibility into what the layer is observing and whether it appears useful.

v3.3.9 makes that evidence visible without changing the learning policy.

## Dashboard metrics

### Executed valid
Historical execution-valid closed observations available to the ticker lifecycle. This is separate from the supplemental signal-learning ledger.

### x/50
Current retraining-cycle progress. Only execution-valid closed trades advance x/50.

### AI vetoes
Signals not executed because the AI/model layer blocked them.

### Missed / non-executed
Signals not executed due to execution/capacity or other recorded non-execution paths.

### Observing
A non-executed signal whose future confirmed candles are still being followed.

### TP / SL
Counterfactual result of the non-executed signal using its original entry/SL/TP. This does **not** claim a broker fill or realized PnL.

### Avoided loss
An AI/model veto whose signal later reached SL before TP. It is evidence that the veto would have avoided a losing setup under the counterfactual rules.

### Blocked winner
An AI/model veto whose signal later reached TP before SL. It is evidence that the veto would have blocked a would-be winning setup.

### Veto usefulness
`avoided losses / (avoided losses + blocked winners)` for resolved eligible AI/model veto observations. It is a descriptive audit statistic, not a guarantee of future profitability.

### FIT eligible
Unambiguous TP/SL observations that satisfy the signal-learning candidate rules. These can supplement the training FIT portion at reduced weight.

### Excluded
Resolved observations that are not safe labels, including ambiguous same-bar outcomes, timeouts, or policy-only observations.

## Recent observation ledger
The recent table shows the latest current state per observation rather than displaying both the original OPEN row and its later RESOLVED row as two different signals.

The API deliberately does not expose the complete feature vector; the dashboard shows only operator-relevant signal/outcome metadata.

## API

```text
GET /api/ai/signal-learning
GET /api/ai/signal-learning?ticker=META
GET /api/ai/signal-learning?ticker=META&limit=80
```

The endpoint requires the normal authenticated dashboard session and performs no broker mutation.

## Interpretation warning
The supplemental outcomes are counterfactual. They help measure signal-filter behavior and provide reduced-weight FIT evidence, but they are not identical to executed REAL/PAPER trades because no broker fill, slippage, or execution timing occurred.
