# GALA10 v3.3.10 Validation

## Root cause
The v3.3.9 dashboard resolved the signal-learning filter with:

```javascript
select?.value || S.signalLearningTicker || ''
```

The **Todos** option deliberately has an empty value. JavaScript therefore fell back to the previous ticker.

## Fix
v3.3.10 resolves it with:

```javascript
(select ? select.value : S.signalLearningTicker) || ''
```

and routes selector changes through `selectSignalLearningTicker(this.value)`, so an empty value explicitly clears the previous filter.

## Regression checks
- selecting a ticker still requests that ticker only;
- selecting **Todos** sends no `ticker` query parameter;
- stored ticker filter is cleared;
- signal-learning data/metrics are unchanged;
- trading and broker code are unchanged.

A static regression test was added at:
`tests/test_v3_3_10_signal_learning_all_filter.py`.
