# GALA10 guarded live-money validation

This patch is intentionally **paper by default**. Real-money startup fails closed
unless the two-ticker validation profile is explicitly prepared, armed, and
passes a read-only Schwab preflight.

## What was corrected

- REAL mode can use an approved **PRODUCTION** model even while
  `agents.entrenamiento.activo=false` keeps training/normal production filtering
  disabled.
- REAL mode always applies the saved per-ticker production threshold, even while
  `umbral_meta_filtro: 0.0` keeps PAPER/Shadow observation non-blocking.
- A Shadow probability can no longer replace the production probability shown
  for a REAL decision.
- REAL mode is hard-limited to the configured allowlist (`NVDA`, `META`); the
  normal 17-ticker PAPER universe may remain unchanged.
- REAL startup requires an environment arm, manual scheduler profile, one-share
  cap, no more than two simultaneous positions, approved production models,
  kill-switch protections, and a GET-only Schwab broker preflight.
- Short entries are blocked for the first validation (`allow_short: false`).

## Important limitation in the uploaded ZIP

The source ZIP supplied for review contains **no `data/` directory**. Therefore
I could not verify the actual deployed production artifacts for NVDA and META.
The VPS must pass `VERIFY_LIVE_READY.py` before `paper_trading` is changed to
`false`.

## Files to replace/add

Back up the current project, then copy these patch files into the project root:

- `agents/training.py`
- `agents/orchestrator.py`
- `engine/bot_engine.py`
- `core/live_guard.py` **(new)**
- `server.py`
- `config.yaml`
- `PREPARE_LIVE_PROFILE.py` **(new)**
- `VERIFY_LIVE_READY.py` **(new)**
- `tests/test_live_money_guard.py` **(new)**

The supplied `config.yaml` remains PAPER and preserves the existing normal
17-ticker configuration; it only adds the new live guardrails.

## 1. Compile and test after installation

```powershell
.\.venv\Scripts\python.exe -m py_compile `
  .\agents\training.py `
  .\agents\orchestrator.py `
  .\engine\bot_engine.py `
  .\core\live_guard.py `
  .\server.py `
  .\PREPARE_LIVE_PROFILE.py `
  .\VERIFY_LIVE_READY.py

.\.venv\Scripts\python.exe -m unittest `
  tests.test_live_money_guard `
  tests.test_training_policy `
  tests.test_position_sizing `
  tests.test_broker_check `
  tests.test_scheduler `
  tests.test_shadow_mode `
  -v
```

## 2. Prepare the two-ticker validation profile

Run:

```powershell
.\.venv\Scripts\python.exe .\PREPARE_LIVE_PROFILE.py
```

It creates a timestamped backup of `config.yaml` and sets the validation profile:

- NVDA + META are the only REAL-allowed tickers.
- 1 share maximum per order.
- 2 simultaneous positions maximum.
- fixed sizing.
- scheduler manual and inactive.
- reversals disabled.
- shorts disabled by the REAL allowlist policy.
- automatic production-model promotion remains disabled.
- **`paper_trading` remains `true`. No real orders are enabled by this script.**

## 3. Confirm the two approved production models exist on the VPS

These pairs must exist and be compatible with the current feature/policy version:

```text
data/modelos/modelo_NVDA.pkl
data/modelos/modelo_NVDA.meta.json
data/modelos/modelo_META.pkl
data/modelos/modelo_META.meta.json
```

Shadow-only files under `data/modelos_shadow/` are not sufficient for REAL mode.

## 4. Arm the readiness check

Add to `.env`:

```text
GALA10_LIVE_ARMED=YES
```

This is a deliberate second switch. Without it, REAL startup is blocked.

## 5. Run the read-only readiness check

```powershell
.\.venv\Scripts\python.exe .\VERIFY_LIVE_READY.py
```

The script does not submit, modify, or cancel orders. It verifies:

- local live safety profile,
- approved PRODUCTION models + thresholds,
- Schwab OAuth,
- exact account hash resolution,
- positions/open-orders readability,
- 1-minute market-data readability.

It must finish with both:

```text
RESULTADO: APROBADO
RESULT: READY
```

If it prints `BLOCKED`, do not enable real mode; fix the reported item first.

## 6. Enable real money only after READY

Change exactly this in `config.yaml`:

```yaml
risk:
  paper_trading: false
```

Restart the dashboard/server. Start the bot **manually** from the dashboard.
Before the engine is started, the server repeats the model + read-only Schwab
preflight. Any failure blocks startup.

The engine then applies a second guard and will only trade the REAL allowlist,
even if the dashboard still contains the normal 17 monitored tickers.

## 7. First-session profile

For the first validation this patch is intentionally conservative:

```text
REAL tickers:              NVDA, META
Maximum per order:         1 share
Maximum open positions:    2
Short entries:             blocked
Reversal:                  disabled by prepared profile
Scheduler startup:         manual only
Automatic model promotion: disabled
Daily kill switch:         remains enabled
Stop-loss protection:      remains required
```

Do not change the existing live daily-loss dollar limit unless the account owner
has explicitly chosen a different loss budget.

## 8. Disarm after the test

After the live validation session, return to PAPER and/or remove the arm:

```yaml
risk:
  paper_trading: true
```

and remove or change:

```text
GALA10_LIVE_ARMED=YES
```

## Interpretation

Backtest/retraining Balanced Accuracy is a model-validation measurement, not a
guaranteed live trading result. Real fills can differ because execution depends
on liquidity, order size, market conditions, latency, and broker routing.

## 9. v3.1.1 market-regime rollout for mixed PAPER/REAL routing

The bot already supports PAPER and REAL simultaneously per ticker. v3.1.1 keeps
that execution architecture intact and gives the market-regime guard a separate
policy for each execution mode:

```yaml
agents:
  market_regime:
    enabled: true
    execution_policy:
      PAPER: enforce
      REAL: observe
```

Recommended rollout:

1. `PAPER: enforce` — simulated trades use the new range/choppiness guard.
2. `REAL: observe` — live tickers keep their existing execution behavior while
   the guard records the counterfactual veto.
3. Run `REPORTAR_SHADOW.py` and compare PAPER and REAL rows separately.
4. Only after the evidence is acceptable, change `REAL: enforce`.

Policies:

- `off`: do not use the regime rule for that execution mode.
- `observe`: calculate/audit what the rule would do, but do not block orders.
- `enforce`: allow the rule to veto a qualifying GALA10 entry in that execution mode.

The report now includes `execution_mode` so META-PAPER and META-REAL are not
mixed into one statistic. Review `regime_losers_filtered_pct`,
`regime_winners_vetoed_pct`, `regime_delta_r`, and the execution mode before
promoting REAL to `enforce`.

The regime layer does not bypass the approved-model gate, kill switch, broker
preflight, position limits, reversal sequencing, or other deterministic safety
controls.

## 10. v3.2 per-ticker broker preflight isolation

v3.2 prevents a ticker-local Schwab/model preflight problem from unnecessarily
stopping the entire mixed PAPER + REAL engine.

Default configuration:

```yaml
live_trading:
  per_ticker_execution: true
  preflight_isolate_ticker_failures: true
  broker_preflight_retry_attempts: 3
  broker_preflight_retry_delay_sec: 0.75
```

When the operator starts the bot manually:

1. Global REAL-money safety checks run first and still fail closed.
2. Each requested REAL ticker is checked independently.
3. Broker preflight is retried up to the configured attempt count.
4. A ticker that still fails is explicitly persisted back to PAPER and logged.
5. Other approved REAL tickers plus all PAPER tickers may continue to start.

The dashboard start notification includes the isolated ticker and reason. Before
placing that ticker back into REAL, correct the reported issue and apply REAL
again deliberately.

## 11. v3.2 AI percentage interpretation

The AI-cycle dashboard now separates:

- Candidate Balanced Accuracy on the current evaluation TEST block;
- SHADOW incumbent Balanced Accuracy on that same TEST block;
- PRODUCTION incumbent Balanced Accuracy on that same TEST block;
- decision threshold selected on VALIDATION;
- SHADOW candidate acceptance;
- independent PRODUCTION gate result and exact failure reasons;
- evaluation ID/time/window.

Do not compare an August BA and a September BA as if they were one permanent
accuracy number: later retraining uses a later chronological evaluation window.
The dashboard also avoids mixing a newly evaluated candidate with a stale
incumbent metric from an earlier run.

A model's decision threshold (for example 43%) is not its Balanced Accuracy and
is not expected to equal the live signal probability.
