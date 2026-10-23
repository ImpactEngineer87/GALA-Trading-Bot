# Dashboard data provenance — v3.3.11

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

The v3.3.9 display correction preserves broker safety checks, entry cancellation,
position liquidation rules, AI x/50 state, validation thresholds and SHADOW
promotion permissions. The v3.3.10 accounting block is described below.

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
The diagnostic alone does not repair accounting or authorize REAL trading.

## Execution accounting repair in v3.3.10

The Schwab audit confirmed actual broker entries and discrepancies between
recorded exit prices and execution legs. For example, NKE's 10:24 close recorded
+$0.30 locally, while its exact broker entry/exit fills yield -$0.65 gross.
The earlier requested-exit-price fallback and entry-price rounding were not a
reliable source of realized REAL P/L.

New REAL closes use verified broker execution prices and preserve sub-cent
average entry fills. STOP/LIMIT protection instructions retain tick rounding.
The live order parser rejects incomplete price coverage instead of applying a
partial execution's average to a larger filled quantity. A missing execution
price, order identity, entry confirmation or quantity match produces unavailable
P/L, never the signal price, a LIMIT approximation or zero. Broker-flat closes
release exposure tracking only after the existing flat/order safety checks.

Unresolved accounting blocks new REAL entries (including reversal entries) in
`data/real_accounting_reconciliation.json`. Optional configuration is
`execution_engine.accounting_state_path`; relative paths resolve under the
original project. The block survives restarts and has no automatic date reset.
At startup, incomplete current-session REAL history also blocks entries because
it cannot establish an accurate daily-risk baseline. No historical records are
rewritten. Do not delete/edit the block to enable trading: reconciliation must
independently establish complete broker execution evidence first.

Existing positions continue through normal broker management and protection.
An unknown partial fill price still updates remainder protection, but cannot
produce P/L or admit a new REAL entry. The daily loss guard remains enabled and
can act conservatively on known losses; an incomplete REAL daily total displays
N/D. Unknown closes persist quarantined feedback with blank exit/R/win labels,
and SHADOW evaluation does not manufacture a win/loss. Existing models, valid
observation counts, validation requirements and promotion permissions remain
unchanged. This does not retroactively reconcile the 17 historical closes or
make them eligible for training.

## Strict REAL presentation in v3.3.11

REAL headline/session totals no longer include candle-derived floating profit.
The engine keeps those estimates for internal risk calculations; the public
snapshot retains a clearly named diagnostic estimate separately. Active REAL
position cards show broker P/L as unavailable until the separate Schwab query,
and never substitute a local estimate for it. Ticker headlines explicitly show
verified closed-fill gross P/L, not a mixture of closed fills and estimates.
Broker-reported open-position P/L remains separately scoped: it is not the
account-wide TOS P/L Day, especially after all positions have closed.

Structured engine events now carry their actual execution mode and accounting
evidence instead of inheriting the dashboard's current global mode. A manually
posted/unproven event cannot become a REAL fill or profit. Unknown event P/L
renders N/D rather than $0.00. Legacy six-argument event hooks remain available.

REAL partial-close evidence requires an explicit verified execution price as
well as the linked order identity and full quantity arithmetic. Old partial
FILLED flags/target prices alone do not establish profitability. Chart markers
follow the same requirement and do not substitute zero for missing P/L. Tests
cover a toy three-share position with +$1 partial and -$4 final outcome, correctly
recording -$3 overall; this is not a reconstruction of historical TSLA fills.

The historical Oct-22 TSLA trade with three entry shares and only a two-share
final close remains unresolved without the exact additional exit evidence.
This release does not invent missing order IDs, rewrite ledgers, alter AI
eligibility/counters, promote models, or change broker order/risk execution.
It adds stricter display provenance on top of v3.3.10 accounting protection.
