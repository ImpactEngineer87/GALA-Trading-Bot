# Chocoletra GALA10 v3.3.10 — Signal Learning “Todos” Filter Fix

v3.3.10 is a focused dashboard hotfix on top of v3.3.9.

## Fixed

### Signal-learning ticker selector could not return to “Todos”
After selecting a ticker (for example META), choosing **Todos** produced an empty select value. The dashboard used JavaScript `||` fallback logic, so the empty value was treated as false and the previous ticker stored in memory was selected again.

Old behavior:

```text
Todos selected
→ select.value = ""
→ "" || previousTicker
→ previousTicker remains active
```

v3.3.10 treats the select element's empty value as an intentional “all tickers” selection and clears the stored ticker filter.

New behavior:

```text
Ticker selected → per-ticker visibility
Todos selected  → no ticker query parameter → all 17 tickers
```

## Scope
This release changes dashboard filtering only. It does not change:
- GALA10 signal generation
- AI/model decisions
- signal-learning labels or weights
- x/50
- VALIDATION/TEST
- PAPER/REAL routing
- broker orders
- stops/take profits
- Schwab OAuth

## Version
- Previous: `3.3.9`
- Current: `3.3.10`
- No configuration or data migration required.
