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
