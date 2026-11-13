# AI training review and repair — v3.3.27

## Status

Deployed on the VPS at **04:30 Saratov on November 13** (**19:30 New York on
November 12**), source `40f995cacaabc9d6829e4988e1f6860b1998db58`.
The dashboard is available, the engine is stopped, and fresh Schwab checks show
zero positions and pending orders. REAL entries and the daily loss latch are
not blocked. No broker order was placed or canceled for this work.

All 17 tickers were trained/evaluated in isolation with production activation
disabled. Accepted SHADOW candidates were published for **NCLH, RCL, TSLA and
UNH**. All 14 production model/metadata files remained byte-identical during
that deployment. Following explicit user approval, **TSLA alone was then
updated in production at 04:42 Saratov on November 13** (19:42 NY on November
12), with its approved **52% threshold**. The dashboard's loaded production
model and effective threshold were verified after reload. Existing REAL
routing, quantities, threshold overrides, risk limits, training counters and
configuration were preserved; the engine remains stopped.

## What was actually happening

Training was running. At 16:15 NY on November 12, the automatic worker evaluated
NVDA, RCL, PINS and PYPL. RCL's candidate was accepted; the others were rejected
on their recorded quality/comparison results. No training exception was recorded
in that batch. Collecting outcomes, fitting a candidate, accepting a SHADOW
model and activating a production model are separate events.

The current automatic policy waits for **50 new eligible closes per ticker**
and a safe post-session window. It trains SHADOW models. It does not update all
production models daily. At review time META had 25 new closes, AMD 44, and
AAPL 18. Some REAL artifacts still had much older training dates in their
metadata. Their unchanged versions explain why newly collected trades did not
necessarily change the REAL model being used the next session.

The November 12 local, verified GALA REAL gross close total was **−$15.64**
across 33 closes; one intentional exclusion was separate. Five REAL tickers
were negative; META and BA were positive. This is GALA's local fill-based
calculation, not Schwab's complete account P/L or a net-after-all-fees total.

Recorded signal scores were variable, including:

| Ticker | Scored signals | Distinct rounded scores | Score range |
| --- | ---: | ---: | --- |
| AMD | 15 | 15 | 21.6–69.7% |
| META | 21 | 18 | 12.7–84.2% |
| PINS | 19 | 18 | 17.7–63.9% |

This session does not support the claim that every signal always received one
fixed percentage. Repeated scores and a losing session alone do not establish
whether fitting completed; the training records and artifact identities do.

## Defects corrected, with code locations

| Location in 3.3.27 | Defect / repair |
| --- | --- |
| `agents/training.py:227` | Shared model-pinned threshold policy for live inference and training evaluation. |
| `agents/training.py:810` | Candidate evaluation previously benchmarked production at its stored threshold, ignoring a live override. It now uses the effective policy, including an explicitly model-pinned probability bypass if present. |
| `agents/training.py:677` | A feedback CSV read/parse failure previously returned `None`, which the worker labeled insufficient data. It now records an explicit error and preserves the pending batch. |
| `agents/training.py:892` | Metrics retain the incumbent model version, original threshold and effective threshold/source. |
| `agents/training_report.py:18` | CSV exports now expose evaluation ID, dataset hash, TEST dates, acceptance/gate decisions, and incumbent policy. |
| `agents/retraining_lifecycle.py:305` | Per-ticker API/status evidence retains dataset hash and incumbent policy from the same evaluation. |

The threshold mismatch was present on the VPS: **BA stored 79%, effective 55%;
META stored 62%, effective 56%**. On the fresh common holdouts, the old BA
benchmark approved 8 trades while its effective policy approved 63; META's
counts were 13 versus 20. A candidate comparison using those different policies
could misstate whether a proposed model improved the actual production filter.
The repair changes the benchmark calculation; it does not alter those saved
live threshold values.

The CSV error path was a confirmed code defect, but no such error was observed
in November 12's automatic batch. These repairs do not establish the cause of
every losing trade or demonstrate future profitability.

## Fresh evaluation of all tickers

The input snapshot contained 9,251 unique eligible closes: 313 REAL and 8,938
PAPER. The existing complete-evidence/feature rules admitted 8,671 rows across
the 17 models. Incomplete or quarantined observations were not revived. Each
model used its own ticker's data, chronological FIT/VALIDATION/TEST blocks,
causal outcome cutoffs and the existing cost reserve. Resolved unexecuted
signals could supplement FIT only, at the configured lower weight.

| Ticker | Complete usable closes | Candidate balanced accuracy | SHADOW accepted | Production gates passed |
| --- | ---: | ---: | --- | --- |
| AAPL | 478 | 56.16% | No | No |
| ADBE | 691 | 47.97% | No | No |
| AMD | 714 | 49.29% | No | No |
| AMZN | 585 | 49.25% | No | No |
| BA | 406 | 48.37% | No | No |
| META | 100 | 46.46% | No | No |
| MSFT | 578 | 48.28% | No | No |
| NCLH | 735 | 50.71% | Yes | No |
| NFLX | 555 | 49.07% | No | No |
| NKE | 311 | 48.08% | No | No |
| NVDA | 109 | 46.43% | No | No |
| PINS | 741 | 55.41% | No | Yes |
| PYPL | 513 | 50.97% | No | No |
| RCL | 648 | 54.07% | Yes | No |
| TSLA | 215 | 55.48% | Yes | Yes |
| UAL | 695 | 52.29% | No | No |
| UNH | 597 | 65.85% | Yes | Yes |

PINS passed the production gates but did not beat its existing SHADOW model on
the same TEST block, so that artifact was retained. SHADOW acceptance alone does
not qualify NCLH or RCL for REAL. None of these results changed ticker routing.
All candidates, including rejected ones, were archived with their metadata.

### TSLA production update completed with explicit approval

Evaluation `62cdc83c9d954badbb4d64a984839d55` uses 43 held-out closes.
The approved model's validation-selected threshold is **52%**, versus the
previous production threshold of **55%**. On that common TEST block:

- Candidate: balanced accuracy **55.48%**, 17 accepted trades, estimated net
  **+1.526956 R**, profit factor **1.288242**, maximum drawdown **3.571678 R**.
- Previous production filter: balanced accuracy **46.55%**, estimated net
  **−2.236330 R**.

R is profit/loss divided by initial per-share risk, after the configured cost
reserve. These are a small historical holdout, not a forecast or a guarantee.
The user explicitly approved this exact candidate and threshold. It was
promoted through `core/model_promotion.py:282` using the normal eligibility
checks, with no gate override. Production activation completed at
`2026-11-13T00:42:41.657832+00:00`; verification completed at
`2026-11-13T00:43:02.795548+00:00`.

- Loaded model SHA-256:
  `76576f9f4a7bf8cf6ddcc7c979bb5cede6a43ec5c342efc5f9cecb70e981e341`.
- Previous model SHA-256:
  `17b3f8f1d567b758abe29f00d6761caeb0edb66b769f2643229117d8271def26`.
- Runtime inference uses PRODUCTION at **52%**, without a manual probability
  override. The six other REAL models and all SHADOW pairs are unchanged.
- All 17 tickers retained their training counts. Seven REAL ticker selections,
  trade ledgers, risk limits and the linked exit policy were preserved.
- The engine remains stopped. A fresh broker check found **0 positions and
  0 pending orders**; REAL accounting and daily-loss entry holds were inactive.
- The previous TSLA pair was archived, and an explicit promotion audit event
  was written. No broker orders were sent or canceled for this maintenance.

UNH remains PAPER; it was not switched to REAL.

## Verification and retained evidence

- **720 Linux tests passed on both GitHub CI and the VPS**, with external
  networking disabled. [CI run](https://github.com/ImpactEngineer87/GALA-Trading-Bot/actions/runs/43336513276).
- 45 focused Windows tests passed with one POSIX permission test skipped;
  the additional lifecycle evidence/progress regression passed separately.
- Training used an isolated feedback/model snapshot with networking disabled.
  Live model files and source ledgers were verified unchanged by that evaluation.
- Publishing the four accepted SHADOW candidates preserved every ticker's
  `total_observations`, `new_observations`, and `last_attempt_count`.
- Configuration, risk/accounting files, trade history and feedback ledgers
  stayed unchanged across the controlled source/model publication. Original
  SHADOW artifacts were archived before replacement.
- The previous 3.3.26 exit upgrade was verified successful at 15:50 NY on
  November 12, with no forced closes. The linked exit policy remains enabled;
  live acceptance of its native OCO orders still awaits a trading-session fill.

VPS evidence and backups:

- Incident evidence: `/opt/gala/backups/ai-review-20261113-bz6_8zoh/`.
- Deployment backup: `/opt/gala/backups/upgrade-3.3.27-gngdyrke/`.
- Deployment receipt: `data/upgrade_verification_20261113_3327.json`.
- TSLA promotion receipt: `data/tsla_model_promotion_20261113.json`.
- TSLA maintenance backup: `/opt/gala/backups/tsla-production-20261113-bihjfg0y/`.
- Previous production pair:
  `data/modelos/archive/TSLA/20261113T004241662072Z_d63a7435/`.
- Full metrics: `data/reportes/metricas_modelos_20261113T002939Z.json` and `.csv`.
- Persistent candidates: `data/reportes/candidates/<ticker>/<evaluation_id>/`.

No code change can certify that the seven REAL tickers will all become
profitable. The confirmed pipeline defects are corrected, current quality
results are recorded, and further production-model changes remain reviewable.
