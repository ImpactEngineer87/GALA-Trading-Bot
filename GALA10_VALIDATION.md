# GALA10 / ThinkOrSwim validation

This build treats the client-confirmed ThinkScript named **Gala10** as the
single master trigger.

## Session model

- Indicator data: 1-minute candles including extended hours.
- Signal evaluation: only fully closed 1-minute candles.
- New order entries: 09:30–15:40 New York time.
- Kill switch / flattening: 15:45 New York time.
- Volume and liquidity: AI context features only; they do not create a signal.

The execution hours remain independent from the data session. Turning on
extended-hours data does not authorize pre-market or after-hours orders.

## Replace the old RTH-only historical files

The previous Massive/Polygon CSVs contain regular-hours bars only. They cannot
reproduce the accumulated EMA, MACD, ATR, crossing memory, or cooldown visible
in the supplied TOS screenshots.

1. Back up the entire project.
2. In `config.yaml`, temporarily set:

   ```yaml
   ai:
     forzar_descarga: true
   ```

3. Run:

   ```powershell
   python DESCARGAR_MASSIVE.py
   ```

   Each replaced CSV is backed up automatically next to the original file.

4. Return `forzar_descarga` to `false`.
5. Run:

   ```powershell
   python PRE_ENTRENAR.py
   ```

Models remain inactive unless they pass the chronological validation gates.

## Produce an exact parity report

After MSFT historical data includes July 27–28, 2026:

```powershell
python GALA10_PARITY.py `
  --csv data\historico\MSFT.csv `
  --ticker MSFT `
  --from-et "2026-07-27 08:00" `
  --to-et "2026-07-28 17:05"
```

Output:

```text
data\reportes\paridad_gala10_MSFT.csv
```

The report includes every BUY, SELL, OM-R, and OM-T plot, the bar timestamp in
New York time, the decision-available time (bar close), and the internal
indicator/cooldown state. Use the exact TOS timestamps supplied by the client to
calculate one-to-one parity.

## Safe test sequence

```powershell
python -m compileall -q .
python -m unittest discover -s tests -p "test_*.py" -v
python main.py --modo backtest --csv data\historico\MSFT.csv --tickers MSFT
```

Do not enable real trading until OAuth is valid, parity is audited, models pass
the activation policy, and paper-session order lifecycle tests have completed.

## Continuous learning from REAL, PAPER and signals

Learning policy 2 combines verified REAL and PAPER closed positions. One stable
position identity counts once, even after repeated callbacks or a service restart.
Blank integrity flags and unconfirmed broker closes are excluded. Old external
datasets without integrity fields remain a compatibility input, shown separately
from verified REAL/PAPER counts. Migration preserves existing CSVs and backs up
the previous schema; it does not invent broker confirmations or close timestamps.

Resolved, nonexecuted signals supplement **FIT only**, at lower weight (default
0.35) and at most one signal per actual FIT close. They do not advance the closed
trade counter or enter VALIDATION/TEST. TP/SL ambiguity, timeout and missing candle
coverage are not binary training labels. Confirmed candles are replayed in order;
the original signal candle and older candles cannot resolve their own signal.
The conservative gap check can exclude holidays/early closes because no exchange
calendar is installed. Retain complete candle history for long service outages.

FIT, VALIDATION and TEST remain chronological. Known positions that closed after
the following holdout began are purged; supplemental outcomes must already have
been available before VALIDATION. Unknown historical close timestamps can support
SHADOW exploration, but cannot satisfy the new production evidence gate. This
separation follows the same future-data constraint described in
[scikit-learn's time-series validation documentation](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html).

The model learns net winning/losing outcomes when recorded fills and planned risk
are available. Returns are measured in **R (planned risk units), not dollars**.
Threshold selection maximizes net R on VALIDATION only. TEST reports net R,
expectancy, profit factor, drawdown and the strategy baseline. Production candidates
must also pass existing classification gates, complete cost/temporal evidence,
positive net expectancy, minimum approved TEST sample count, and no degradation
against the unfiltered baseline or a measurable production incumbent. A small
initial batch can train a SHADOW model without providing enough production evidence.

These checks measure historical evidence; they do not guarantee future profits.
Mixed REAL/PAPER evidence is explicitly identified and is not a live-only result.
The cost reserve below is an assumption, not a Schwab fee quote. Actual-fill
slippage is already included in fill prices; the reserve is subtracted once.
PAPER fill realism, borrowing costs and changing market conditions still affect
how well these results transfer to live execution.

Optional settings to merge into the existing `agents` section of `config.yaml`:

```yaml
agents:
  signal_learning:
    enabled: true
    use_for_training: true
    counterfactual_weight: 0.35
    max_counterfactual_to_actual_fit_ratio: 1.0
  entrenamiento:
    automatic_retraining:
      retrain_samples: 50
      auto_activate_production: false
    economic_validation:
      round_trip_cost_bps: 2.0   # estimated total fee/cost reserve; set for your costs
      min_accepted_test_trades: 10
      min_expectancy_r: 0.0      # strictly positive net expectancy is required
      min_profit_factor: 1.10
      max_drawdown_r: 6.0
      min_net_r_lift: 0.0       # no degradation vs the same TEST without AI
```

Explicit zero signal weight or ratio disables supplemental fitting. Existing
configured retraining intervals are retained; the fallback is now 50. Automatic
production activation remains disabled by default. Normal promotion of an old
SHADOW artifact requires a new evaluation under policy 2; installed production
artifacts retain their runtime compatibility. Existing deliberate manual overrides
remain separately audited.

The Signals page and `/api/ai/status` expose per-mode verified counts, usable sample
readiness, actual FIT source counts, and the latest economic TEST evidence.
JSON/CSV training reports preserve the same evaluation snapshot. Pending closed
outcomes survive failed writes and are replayed on restart without broker calls.

For deployment, commit and push the source changes first. On the VPS, stop the
bot from the dashboard and wait for broker-confirmed closure before updating.
Replace `SERVICE_NAME` below with the existing systemd unit name:

```bash
cd /opt/gala/GALA-Trading-Bot
sudo systemctl stop SERVICE_NAME
git pull --ff-only origin master
# Activate the same virtual environment used by the service, then:
python -m pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_*.py'
sudo systemctl start SERVICE_NAME
sudo journalctl -u SERVICE_NAME -n 100 --no-pager
```

Preserve `config.yaml`, `.env`, `data/`, model artifacts and learning sidecars.
No ZIP packaging or live order placement is required for this update.
