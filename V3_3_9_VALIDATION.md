# GALA10 v3.3.9 Validation Report

## Scope
v3.3.9 adds read-only visibility for the v3.3.7 signal-learning ledger and does not modify trading or model-decision behavior.

## Targeted validation
Command used with the project root on `PYTHONPATH`:

```text
python -m pytest -q
  tests/test_v3_3_7_signal_learning.py
  tests/test_v3_3_8_schwab_oauth_reliability.py
  tests/test_v3_3_9_signal_learning_visibility.py
```

Result:

```text
16 passed
0 failed
```

The new v3.3.9 tests verify:
- avoided-loss vs blocked-winner accounting,
- veto usefulness percentage,
- REAL/PAPER observation counts,
- TP/SL/ambiguous/timeout visibility,
- excluded vs FIT-eligible outcomes,
- open observation counts,
- recent observation de-duplication (OPEN + RESOLVED becomes one current state),
- omission of full model feature vectors from the visibility payload,
- dashboard/API/version presence.

## Broader v3.3 regression comparison
v3.3.9 selected suite:

```text
97 passed
1 failed
```

Clean v3.3.8 baseline using the same selected legacy tests:

```text
93 passed
1 failed
```

The one failure is unchanged from v3.3.8: `test_quarantine_keeps_open_real_position_live` constructs `ChocoletraBot` with `__new__` and omits `_ticker_quarantine_health`, an existing test-fixture initialization issue unrelated to v3.3.9.

Therefore v3.3.9 adds four passing visibility tests and no new failure in the comparable set.

## Static validation
- `python -m py_compile agents/signal_learning_store.py server.py` — PASS
- Dashboard inline JavaScript `node --check` — PASS
- `node tests/test_v3_3_dashboard_limits.cjs` — 6 passed, 0 failed

## Safety invariants checked
- x/50 semantics unchanged.
- supplemental signal outcomes remain FIT-only at reduced weight.
- VALIDATION/TEST remain actual execution-valid data only.
- no broker write endpoint was added.
