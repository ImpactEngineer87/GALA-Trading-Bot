# Signal execution and closing-order review

Trading-code baseline: `9f8e55e`, application v3.3.13. The Auto AI JSON-header
fix did not implement LIMIT-only normal exits. This diagnostic update changes
the read-only audit, its tests and this document; trading behavior is unchanged.

## Confirmed missed signal

The operator-provided October 23 audit links BA's 10:17:36 NY SELL to
`ENTRY_BLOCKED` / `SIGNAL_EXECUTION_SKIPPED`, reason
`REAL_ACCOUNTING_UNRESOLVED`. BA's 09:32 short was filled, but its local closing
order ID is missing. An earlier successful short does not establish execution
of the later signal. NKE's 09:48 short and final close both had complete broker
fill evidence. These historical findings do not confirm current account exposure.

AI approval is an entry-filter decision; accounting, model, risk, capacity,
position, broker and cancellation gates still apply. Do not remove the
accounting block or fabricate a closing identity to force a new SELL submission.

## Current REAL closing routes

`EjecutorHibridoDeOrdenes.liquidar_posicion` uses the legacy MARKET path when
Execution V2 is disabled. With V2 enabled, `SmartExit._new_state` creates a
marketable LIMIT only when a fresh, non-delayed bid/ask is available and the
reason is non-emergency. Defaults are maximum quote age 5 seconds and price
buffer 5 basis points. With no valid quote the current code can start MARKET.

After a LIMIT attempt, the default wait is 1.5 seconds. A terminal/canceled
LIMIT can be followed by MARKET for remaining shares, because `market_fallback`
defaults to true. With that setting false, a quote-unavailable FIRST attempt
can still be MARKET: it is not a LIMIT-only switch. STOP/kill/unprotected-exposure
and other emergency reasons deliberately route to MARKET.

Thus the requested normal LIMIT-only close policy is still incomplete. A safe
implementation must handle unfilled and partially filled LIMITs, retain exact
broker evidence, restore appropriate protection for exposed remainder, and
prevent a reversal until the old position is genuinely flat. The current engine
cancels protective orders before attempting a close, so simply disabling the
MARKET fallback is not a complete protected-exit implementation. Emergency risk
exits must remain available and uncertain orders must never be duplicated.

## Fresh evidence for each actual exit

Run `scripts/audit_schwab_execution_prices.py --date 2026-10-23` from the original
installation. `broker_entry` / `broker_final_close` now include the exact
broker-reported `order_type` and matching `instruction`. A protective STOP
remains labeled STOP. Missing/invalid types stay null; neither a local order
label nor the presence of a price is used to invent a type. Missing local order
IDs remain unresolved, and the tool does not infer a replacement by time or
ticker. A reported order type does not relax price/quantity fill verification.

`current_exit_settings` reports only the current configured routing flags and
timings. It is not proof of which configuration was active when an old order was
submitted. The read-only tool does not include raw account/order IDs in its
report and never repairs history, clears blockers or submits/cancels orders.
Existing OAuth token refresh may persist authentication tokens as before.

Do not use screenshots or local submitted-order logs alone to assert the exact
Schwab exit type. Match each closing order through fresh broker evidence and
preserve the distinction between normal strategy exits and protective exits.
