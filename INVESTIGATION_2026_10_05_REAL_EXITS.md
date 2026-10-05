# REAL exits and dashboard controls — October 5, 2026

## What the screenshots establish

The v3.3.14 dashboard shows the engine PARADO with three local REAL trades,
failed broker-position reconciliation (`respuesta vacía`), and cancellation
confirmation timeouts for BA, META and NVDA. The selected REAL universe is BA,
META, NVDA, TSLA and NKE; the other twelve monitored symbols are PAPER. A
Thinkorswim image shows a META long position of three shares and a protective
stop at that image's timestamp. Neither screenshot is a current account audit.

A green OAuth indicator does not confirm that position reads, cancellation,
closing orders or fills succeeded. The dashboard image is timestamped 15:57 NY;
the exchange's regular session normally ends at 16:00 NY, while the bot uses
its own configured `scheduler.hora_fin`. Verify the actual configured cutoff
and `kill_switch.liquidar_al_cierre_mercado` before concluding that the session
close path failed to trigger.

## Confirmed defects in the source

1. A failed stop returns `running: false` when Schwab exposure is unresolved,
   but the browser previously inverted the checkbox, briefly reporting ACTIVO.
2. Missing production models, disabled entry agents, or exceptions during AI
   inference returned before processing a known opposite GALA10 signal exit.
   AI permission is intended to govern entries, not retention of a position
   against a trusted opposite signal.
3. Stop-without-liquidation could report the broker flat without a fresh read.
   A server with no engine object could also report flat merely because no
   engine existed to stop. Missing account configuration could be mistaken for
   an account with no REAL exposure.
4. Some unexpected broker response shapes could be normalized as an empty
   account/order list. Broker read failures were reduced to `respuesta vacía`,
   concealing whether the cause was authorization, cooldown, HTTP failure, or
   an unexpected response shape.

## Proposed changes

- Read the actual server state after failed/uncertain toggle requests; prevent
  duplicate control submissions.
- Display “Motor parado · cierre REAL no confirmado” and provide an explicit
  `Reintentar cierre REAL` control using the existing guarded stop endpoint.
  Rendering the dashboard never submits a recovery order. The control is
  disabled during the shared Schwab cooldown.
- Permit close-only handling of an existing position when entry AI is
  unavailable. It requires a genuine GALA10 signal from trusted closed-bar
  market data and uses the existing cancellation/position confirmation path.
  It does not open the opposite side or relax broker confirmation checks.
- Keep local exposure when a close is unconfirmed and show that failure in
  ticker processing instead of reporting a successful close-only action.
- Verify broker exposure even when liquidation-on-stop is disabled. Preserve
  the stopped engine context when the account cannot be confirmed flat.
  If no engine context exists, the stop request performs read-only checks and
  instructs the operator to supervise exposure directly through Schwab.
- Fail closed on malformed broker reads and missing account configuration;
  surface safe HTTP/error categories through per-thread diagnostics.

The changes do not modify ticker selections, order sizes, AI entry thresholds,
daily-loss settings, exit prices or session times. They do not claim to remove
the reason Schwab rejected or failed a real request.

## Validation and limits

Passed:

- `python -m unittest discover -s tests -p test_v3_3_15_exit_control.py`:
  26 isolated scenarios executing actual production method bodies.
- `node tests/test_v3_3_15_dashboard_control.cjs`: 8 browser-function VM scenarios.
- Python compilation of changed Python files; full inline dashboard script
  parsing; `git diff --check`.

The Python regression harness uses AST extraction to avoid importing the
Flask/ML application in this dependency-limited workspace. It exercises the
actual control/pipeline methods with simulated broker boundaries. It is not
an application integration test. Runtime dependency installation was blocked;
the full pre-existing pytest suite and an installed Flask/engine smoke test
have not run. The fix remains a draft for review and additional validation.
No Schwab credentials, external requests, or live orders were used.

## What is still needed from the VPS

- Redacted engine/server logs around the META opposite signal and session stop,
  including console lines from `obtener_posiciones` and
  `obtener_ordenes_abiertas`. These API-client errors are printed to the server
  console and may not be present in the dashboard log.
- Relevant `config.yaml` sections only: `scheduler`, `kill_switch`, and
  `live_trading`; no `.env`, tokens, client secret or account identifiers.
- Per-ticker processing status/entry rejection reasons for the five REAL
  symbols, and broker order history for the protective-order cancellation.
- Current positions and open orders verified directly through Schwab.

Until reviewed, keep new REAL entries stopped and have the account owner
supervise existing positions and protective orders in Schwab. Do not delete
pending-exit records, mark the account flat manually, remove protections, or
bypass reconciliation to force a reversal. A market/stop close cannot be
assumed executable merely because a session has ended.

The earlier OAuth startup-expiry and token-file atomicity improvements remain
separate follow-up work; this patch does not claim to implement them.
