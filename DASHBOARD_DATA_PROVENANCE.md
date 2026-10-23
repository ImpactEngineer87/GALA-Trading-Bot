# Dashboard data provenance — v3.3.9

## Corrected behavior

The old daily ticker view summed PAPER and REAL history and labeled that sum
using the ticker's current configured mode. The 30-day ranking mixed both modes
in the same way. These are local strategy calculations, not Schwab-reported P/L.
An older dashboard candle function and the single-timeframe Schwab helper also
generated synthetic candles automatically when broker data was unavailable.
The main engine already rejected frames marked synthetic; that protection stays.

The dashboard now presents separate PAPER and REAL results. PAPER is explicitly
simulated. REAL calculations require matching recorded broker entry and close
confirmations with the exact ticker, position, execution and order identities.
Quantities, prices, partial exits and gross P/L must agree. A local closed-state
event or an order ID alone does not qualify. Missing or contradictory evidence
is excluded and displayed as N/D rather than a fabricated result or a zero.
Such records remain available in the original ledgers/downloads; no history is
rewritten, reconstructed, or retroactively marked eligible for AI training.

The ranking defaults to REAL and has a separate PAPER selector. A ticker with
excluded records in the selected period cannot receive a performance rank.
Acceptance in SHADOW, model balanced accuracy, strategy signals and AI approvals
are not evidence of broker execution or future profitability.

The live position card explicitly labels its value as a local estimate from the
last confirmed market-data candle, including the source bar time. Missing,
unverified or more than 120-second-old valuation inputs display N/D. A historical
chart no longer revalues today's active position using old chart candles. Exit
markers use the confirmed final fill, not the requested signal price.

The manual Schwab open-position P/L query reads the broker's own
`currentDayProfitLoss`, including when the engine has not been initialized.
It resolves the exact configured account hash first. It does not compute a
replacement if Schwab omits a value. It is explicitly limited to currently open
positions: it is NOT account-wide Thinkorswim P/L Day or realized daily P/L.
Local fill-derived figures are gross strategy calculations, not tax-lot P/L and
not net of every brokerage fee. No equivalence to TOS accounting is claimed.

Automatic synthetic market-data fallback is removed from the dashboard and
Schwab candle/MTF requests, including PAPER operation. Missing data remains
unavailable. The explicit `obtener_velas_demo` generator remains an opt-in demo
and tags generated frames as synthetic. It is not called by production paths.

This change does not alter risk gates, entry cancellation/reconciliation,
position liquidation rules, AI x/50 state, validation thresholds or SHADOW
promotion permissions.

## Validation and activation

Run the full suite in the original project with the trading engine stopped:

```bash
cd /opt/gala/GALA-Trading-Bot
unshare -n -- ./.venv/bin/python -m unittest discover \
  -s tests -v > /tmp/gala_data_provenance_tests.log 2>&1
test_result=$?
grep -E '^Ran |^OK|^FAILED|^ERROR:|^FAIL:' /tmp/gala_data_provenance_tests.log
printf 'Test exit code: %s\n' "$test_result"
```

Node.js enables two additional browser-rendering regressions; unittest reports
those as skipped if Node is unavailable. Do not interpret a skipped rendering
test as a verified browser check.

Before updating or restarting the VPS, establish a controlled maintenance window.
Verify that the engine is stopped and the scheduler is manual, then use the
existing read-only broker check and Schwab/TOS to confirm positions and working
orders. A passing broker check alone does not mean the account is flat: inspect
the actual counts. Do not stop or restart while exposure remains unresolved;
`kill_switch.liquidar_al_apagar` may liquidate REAL positions.

Make a private backup of the project runtime files before a fast-forward update.
Preserve `.env`, `config.yaml`, model artifacts and all trade/AI history. Do not
reset, clean, stash blindly, or remove pending execution files. If the working
tree has tracked changes or the update is not a fast-forward, inspect it first.
Run tests before activation. Recheck broker exposure before starting the service.

After the dashboard starts, keep the trading engine off and scheduler manual.
Verify mode-separated ticker results, the ranking source selector, excluded
record reasons, and the manual Schwab P/L lookup. Compare exact broker fills
before relying on REAL results. This patch is not approval to start REAL trading.

## Fresh historical execution-price audit

Use `python scripts/audit_schwab_execution_prices.py --date 2026-10-22`
with the engine OFF to compare REAL ledger rows with fresh Schwab order reads.
The audit uses the exact configured account, reads the order list (including
nested child orders), and falls back to individual GETs for recorded IDs not
found in that bounded list. It reports list truncation and unavailable reads;
neither proves the absence of an execution. No trading executor is initialized.
OAuth may refresh/persist authentication tokens through the existing client.

Each price requires a single matching equity leg, exact order ID, symbol and
instruction, finite positive execution prices/quantities, and complete quantity
coverage in a terminal order. The LIMIT price and a local FILLED/flat flag are
never substitutes. Missing exit IDs remain unresolved; the audit does not guess
an exit from ticker, time or price. A partial/multiple-order exit cannot produce
a whole-trade P/L from only the final order. When full quantities match, gross
P/L is calculated separately from the stored ledger value; it remains a local
calculation, not Schwab/TOS account P/L or net of every fee.

The command does not modify history, counters, feedback, models or promotion
state. Its results do not make historical observations eligible for AI training.
It is a diagnostic, not an accounting repair: the current engine can still
record a requested exit price when broker-flat recovery lacks execution-price
evidence. The dashboard excludes those rows, but the underlying accounting and
daily-loss treatment must be resolved before enabling REAL trading.
