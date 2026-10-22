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


## Oct 22: REAL reversal `RECONCILIAR` and AI source visibility

Dashboard screenshots show failed NVDA/NKE/TSLA reversal closes and
`REVERSAL_PRIOR_CLOSE_FAILED` supplemental observations. Such signals
**must not advance x/50** because they were not executed as new,
broker-verified completed trades. They may contribute only to bounded
FIT evidence after a separately resolved virtual TP/SL outcome.
The current automatic training configuration is independent of this fix
(PR #14 is a separate, not yet deployed operator-control feature).

The guarded SmartExit improvement additionally handles two cases that
previously could leave local exit intent stuck in `RECONCILIAR`:

- Schwab definitively reports FLAT but recorded execution quantities
  disagree with the broker snapshot.
- Schwab reports FLAT and the journal has terminal orders whose actual
  fill prices cannot be recovered.

Only if the broker also confirms **zero WORKING orders** does the engine
release local intent via the existing `EXIT_RECOVERED_FLAT` quarantine
path. It does **not** submit another order, fake a price or count
the questionable trade for AI learning. If the broker position or
WORKING snapshot is unavailable, the close remains blocked. If
position exposure remains, existing fail-closed recovery applies.

The bot error log now includes the exact internal reconciliation reason.
The dashboard adds a breakdown of `quarantined_closed_real` and
`quarantined_closed_paper`. No historical rows or last-attempt
counters are changed.

### Safe isolated test on a VPS with an ACTIVE REAL bot

Do **not** run `git pull` or restart the currently running service merely
to test; an unexpected systemd restart can load new code, and shutdown
can liquidate live positions. Instead, create a separate Git worktree
for code-only tests:

```bash
cd /opt/gala/GALA-Trading-Bot
git fetch origin
git worktree add --detach /opt/gala/GALA-Trading-Bot-review-20261022 \
  origin/fix/smartexit-real-fill-evidence-20261021
cd /opt/gala/GALA-Trading-Bot-review-20261022
../GALA-Trading-Bot/.venv/bin/python -m unittest discover \
  -s tests -p 'test_smartexit_flat_reconciliation.py' -v
../GALA-Trading-Bot/.venv/bin/python -m unittest discover \
  -s tests -p 'test_ai_real_exclusion_counters.py' -v
../GALA-Trading-Bot/.venv/bin/python -m unittest discover \
  -s tests -p 'test_smartexit_verified_flat.py' -v
../GALA-Trading-Bot/.venv/bin/python scripts/audit_live_real_blockers.py \
  --runtime-root /opt/gala/GALA-Trading-Bot
```

The last command reads local persisted audit, exit journal, training
state and config only. It does not query Schwab; it does not verify
that the live broker account is flat or that a restart is safe.
Test changes are not loaded into the already-running Python process.
Even passing unit tests are not a substitute for broker preflight,
a supervised deployment plan, and reconciliation of every outstanding
REAL position and WORKING order.

All new tests are committed but remain **unexecuted on the user's VPS**
until the operator supplies results. Do not enable production model
promotion, bypass normal AI validation gates, or repair historical
`execution_valid` flags solely to increase x/50.
