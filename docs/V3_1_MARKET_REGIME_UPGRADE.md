# Chocoletra / GALA10 Bot v3.1.0 — Market-Regime AI Guard

## Objective

Version 3.1.0 addresses the case where a per-ticker AI model can approve several alternating GALA10 signals while price is moving sideways inside a wider range.

The existing v3.0.7 `LATERAL_RANGE` rule detects only narrow compression over a short window. A market can still be range-bound without meeting that narrow compression threshold. v3.1 adds an independent, explainable market-regime layer.

**GALA10 remains the only BUY/SELL signal generator.** The new layer cannot create a trade, reverse a signal, change the entry price, or bypass deterministic risk controls.

## New decision pipeline

```text
Confirmed 1m GALA10 signal
        |
        v
Existing macro / micro / risk context
        |
        v
Per-ticker XGBoost AI veto
        |
        v
Market-Regime Guard (v3.1)
        |
        v
Deterministic risk + execution safety
        |
        v
Broker execution
```

The Market-Regime Guard classifies the current micro context as one of:

- `TREND_UP`
- `TREND_DOWN`
- `RANGE`
- `HIGH_VOLATILITY`
- `UNCERTAIN`
- `INSUFFICIENT_DATA`

## How v3.1 detects range/chop

The new classifier uses only current and historical closed bars. It does not look into future candles.

It evaluates:

1. **Directional efficiency** — net displacement divided by total price path. A low value means price moved a lot but went nowhere.
2. **Flip ratio** — how often consecutive close-to-close movements alternate direction.
3. **Net movement in ATR units** — whether the market has actually progressed far enough to be directional.
4. **Recent range width** — the size of the price box as a percentage of price.
5. **EMA slope normalized by ATR** — directional slope relative to current volatility.
6. **ATR regime** — whether current volatility is unusually high versus recent ATR.
7. **GALA10 breakout confirmation** — a range veto is not applied when the existing GALA10 breakout feature confirms a sufficiently strong escape from the box.

This means a wider sideways market can be classified as `RANGE` even when the old 8-bar / 0.18% compression rule says `not lateral`.

## Controlled rollout

The default configuration is:

```yaml
agents:
  market_regime:
    enabled: true
    enforcement: shadow
```

Values:

- `off`: no regime evaluation affects Shadow or REAL decisions.
- `shadow`: calculate and audit the regime veto, but do not block actual GALA10 execution.
- `real`: allow a valid regime veto to block a REAL entry after the normal model gate.

The supplied v3.1.0 package intentionally starts in `shadow` mode. This prevents an unvalidated new rule from silently changing live-money behavior.

To promote after reviewing results:

```yaml
agents:
  market_regime:
    enforcement: real
```

Restart the bot after changing configuration.

## Range breakout exception

A signal inside a range is not automatically rejected. If the existing causal GALA10 `gala_breakout_atr` value is at or above the configured confirmation threshold, the decision becomes:

```text
REGIME_RANGE_BREAKOUT_CONFIRMED
```

and the regime layer does not veto it.

Default:

```yaml
breakout_confirm_atr: 0.20
```

This is intended to reduce repeated range oscillation entries without suppressing every legitimate breakout.

## Audit visibility

Every audited GALA10 signal now carries market-regime context on `DecisionFinal`, including:

- regime name
- regime confidence
- range percentage
- directional efficiency
- flip ratio
- net movement in ATRs
- ATR regime ratio
- EMA slope / ATR
- breakout / ATR
- signal-vs-trend alignment
- whether the regime layer would block
- exact regime reason

Examples:

```text
AI_APPROVED|REGIME=TREND_UP
```

```text
AI_APPROVED|REGIME_SHADOW_VETO=REGIME_RANGE_CHOP|REGIME=RANGE
```

When REAL enforcement is enabled:

```text
AI_VETO:REGIME_RANGE_CHOP
```

The engine also emits an `[AI-AUDIT]` log line before execution.

## Shadow outcome report

`REPORTAR_SHADOW.py` now reports both XGBoost Shadow-model metrics and the new regime guard metrics.

New regime metrics include:

- `regime_samples`
- `range_samples`
- `regime_veto_rate_pct`
- `regime_losers_filtered_pct`
- `regime_winners_vetoed_pct`
- `regime_net_r`
- `regime_delta_r`
- `regime_decision_accuracy_pct`

The Shadow CSV automatically migrates to the v3.1 schema. A backup is created as:

```text
data/shadow_decisions.csv.before_v3_1
```

before the header is rewritten.

## Model compatibility

The new market-regime fields are deliberately **not** added to the XGBoost `FEATURES` vector in v3.1.0.

Therefore:

- existing approved per-ticker models keep the same feature count;
- model metadata / feature-version checks are not invalidated;
- this upgrade does not force an immediate full model retraining;
- the regime guard can be measured independently from the learned model.

A later model version can incorporate validated regime measurements as training features only after sufficient Shadow evidence exists.

## Safety behavior retained

v3.1 does not remove or weaken:

- approved-model requirement for REAL tickers;
- daily-loss kill switch;
- fixed/per-configured position sizing;
- broker preflight and reconciliation;
- duplicate-entry protection;
- reversal lifecycle safeguards;
- REAL synthetic-data prohibition;
- RTH checks;
- long/short execution policy;
- stop-loss / take-profit logic;
- execution-integrity training filters.

## Recommended validation for META

Before switching `enforcement` to `real`, review at least:

1. how many META signals were classified `RANGE`;
2. how many losing trades the range veto would have prevented;
3. how many winning trades it would have incorrectly blocked;
4. the change in net R (`regime_delta_r`);
5. whether breakout-confirmed signals are still passing as expected;
6. whether the result is stable across more than one trading session.

The thresholds should be tuned from actual Shadow outcomes rather than changed because of a small number of visually selected trades.

## Tests added

`tests/test_v3_1_market_regime.py` verifies:

- a broad choppy box is detected as `RANGE` even when width exceeds the old 0.18% compression threshold;
- a confirmed breakout can pass through a range;
- a clean directional move is classified `TREND_UP`;
- Shadow regime veto does not change execution;
- REAL regime enforcement blocks only when explicitly configured;
- the dashboard/audit veto reason reports `REGIME_RANGE_CHOP` instead of incorrectly labeling it `AI_THRESHOLD`;
- regime-only Shadow observations are joined to eventual trade outcomes;
- existing Shadow CSV files migrate safely to the v3.1 audit schema.
