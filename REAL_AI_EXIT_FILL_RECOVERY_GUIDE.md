# REAL AI learning: SmartExit recovered-flat fill evidence

## Diagnosis (from VPS, 2026-10-21)

The original 5 REAL tickers have 272 REAL closed-feedback rows; 155 rows
contain valid execution evidence, while 117 are excluded. Every excluded
example inspected had entry confirmation present but exit confirmation
missing. A separate read-only journal check counted these events:

|Ticker|EXIT_RECOVERED_FLAT events|
|---|---:|
|BA|3|
|META|12|
|NVDA|14|
|TSLA|5|
|NKE|22|
|Total|56|

These are **journal events**, not necessarily 56 unique excluded trades.
`STALE_EXIT_RETIRED_FOR_EMERGENCY` events may describe several recovery
attempts for one exposure, not new trades. No historical x/50 updates can
be inferred from event counts alone.

## Fix

`core/execution_v2.SmartExit._recover_flat` previously always returned
`fill_price_verified=False` after confirming Schwab was FLAT. That is
correct when broker fill details cannot be read, but it missed an opportunity
to reread **known previously submitted order IDs**.

The proposed patch:

1. Runs only after the existing broker-flat and no-WORKING-order safeguards.
2. Confirms that the stored exit intent matches the requesting ticker's
   **exact engine position ID, side and share quantity**. Newly created
   SmartExit states retain the original position ID supplied by the engine.
   Legacy pending journals without that provenance are never upgraded to
   verified training evidence simply because the broker is flat.
3. Uses existing `detalle_orden` GET lookups to reread the exact, known
   exit order IDs. It never places or cancels orders.
4. Accepts completed evidence **only if** unique IDs, terminal broker states,
   complete filled share quantity, and positive actual execution-leg prices
   reconcile precisely. Canceled, partially filled LIMIT legs are included
   only when their actual fill prices are broker verified.
5. If evidence is sufficient, reuses the ordinary `EXIT_COMPLETED` path and
   returns an actual volume-weighted price, verified close order ID and
   `fill_price_verified=True`. The existing feedback integrity checks then
   decide whether a genuinely verified closed trade counts.
6. Otherwise preserves the original `EXIT_RECOVERED_FLAT` fail-closed
   quarantine path. It does not invent filled prices, order IDs, P&L or a
   synthetic training observation.
7. Also tightens `_complete` to reject empty/partial/duplicate broker fill
   evidence, preventing a previous zero-filled-legs false positive.

This will not retroactively rewrite historical CSVs or turn invalid REAL rows
into training samples. For old events, use a separate audited Schwab
reconciliation process when reliable broker execution details are available.

## Testing / deployment

A read-only historical inspection is provided:

```bash
./.venv/bin/python scripts/audit_recovered_flat_evidence.py
```

It **does not connect to Schwab**. A complete archived leg snapshot is
interesting, but is NOT a verified fill until the broker confirms those data.

Offline regression tests with mock broker detail responses:

```bash
./.venv/bin/python -m unittest discover \
  -s tests -p 'test_smartexit_verified_flat.py' -v
./.venv/bin/python -m unittest discover \
  -s tests -p 'test_learning_feedback.py' -v
./.venv/bin/python -m unittest discover \
  -s tests -p 'test_signal_learning.py' -v
```

New tests are committed for review but have **not yet been run in the VPS
environment**. No branch checkout, restart, runtime config change,
auto-training enablement, or REAL model promotion has been performed.

**Do not deploy into an active mixed/REAL process or restart the service
without supervised maintenance and broker order/position reconciliation.**
Schwab order-status and META P&L-sign discrepancies remain independent
safety concerns. Do not approve new REAL orders because the AI training
counter increased.
