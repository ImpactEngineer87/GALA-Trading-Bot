# Schwab broker validation

## Safety boundary

`demo` and `backtest` always force simulated execution. They cannot inherit
`risk.paper_trading: false` from `config.yaml`.

`broker-check` is read-only. It validates OAuth, the exact account hash,
positions, open orders and real 1-minute market data without importing or
instantiating an order executor.

`broker-latency` is also read-only. It reuses one authenticated client,
performs one excluded warm-up pass, and then measures repeated GET calls for
positions, open orders and a quote. It does not measure order placement,
cancel, fill or exchange-routing latency.

## Read-only command

```powershell
python main.py --modo broker-check --account ACCOUNT_NUMBER --tickers MSFT
```

An approved result requires `OK` for OAuth, account hash, positions, open
orders and market data. Stop if the result is `BLOQUEADO`.

## Warm read-only latency report

```powershell
python main.py --modo broker-latency --account ACCOUNT_NUMBER --tickers MSFT --samples 10
```

The command writes both a timestamped JSON report and
`data/reportes/broker_latency_latest.json`. Keep this report with the session
evidence. It reports min, median, p95 and maximum response time without
including account numbers or OAuth secrets.

## Real-mode arming

Real mode remains blocked while this setting is true:

```yaml
risk:
  paper_trading: true
```

Only during a supervised market-session test:

1. Confirm `broker-check` is approved.
2. Confirm the account contains no unrelated position in the test ticker.
3. Keep `risk.max_acciones_por_orden` at the agreed validation quantity.
4. Change `risk.paper_trading` to `false`.
5. Run `python main.py --modo real --account ACCOUNT_NUMBER --tickers MSFT`.
6. Enter the account-specific phrase requested by the CLI.

The bot manages and reconciles only symbols present in `--tickers`; it does not
adopt, cancel or liquidate positions belonging to other symbols.

## Evidence to retain

Save console output for every broker transition:

- entry acknowledgement and fill;
- initial stop acknowledgement;
- TP1 fill;
- original stop cancellation confirmation;
- replacement 30% break-even stop;
- final target or stop closure;
- reversal cancellation, flat confirmation and opposite entry;
- final kill-switch flat confirmation.
