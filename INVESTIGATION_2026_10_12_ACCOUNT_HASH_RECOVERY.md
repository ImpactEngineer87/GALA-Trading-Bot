# Restart blocked after Schwab reconnect — v3.3.24

Reviewed main `33f701f` (v3.3.23). Dashboard screenshot, 2026-10-12 13:23 NY:

- **Red banner:** "Motor parado · cierre REAL no confirmado — No se pudo confirmar
  Schwab posiciones: HTTP 400: HTTPError".
- **Start rejected:** "Arranque bloqueado: todavía existe exposición REAL no
  resuelta".
- **Preflight:** BA/META/NKE/NVDA/TSLA blocked with "OAuth de usuario no
  disponible".
- **Open trades:** 0 open (PAPER 0, REAL 0).
- **Reconnect:** the operator reports Schwab OAuth was fixed, but the bot still
  cannot start.

## Cause

`_preparar_trading_real` resolves the encrypted account hash only after a
successful user-token check. With OAuth down at engine start, it returned early
and `account_number` kept the plain number.

The plain number was retried only before a new entry. All other account reads
kept using it:

- `_refresh_broker_reconciliation` (dashboard "Verificar Schwab", stop, kill
  switch, startup)
- the kill-switch final sweep

Schwab rejects the plain number on `/accounts/{account}`, so each read raised
HTTP 400 and the message read "HTTP 400: HTTPError". It never reaches
`AUTH_UNAVAILABLE`. A reconnect refreshes the token, which the engine adopts,
but not the hash.

The stopped engine is retained in memory while flatness is unconfirmed. Each
start calls `previous.detener()`, which re-reads positions with the plain
number, gets HTTP 400 and blocks the start again. Only a server restart cleared
it.

## Fix

`_resolver_hash_cuenta()` swaps a still-plain account number for the hash.

- **When it runs:** after every successful trading-token check, and before
  reconciliation reads and the kill-switch final sweep.
- **Pacing:** failed lookups are paced at 15 s, and nothing is ever treated as
  flat without a successful read.
- **Startup:** `_preparar_trading_real` reuses it, so startup performs a single
  lookup.

## Validation

- **New tests:** six in `tests/test_v3_3_24_account_hash_recovery.py`.
  - A reconnected stopped engine resolves the hash and confirms flat.
  - A failed lookup stays fail-closed and is paced.
  - Startup does a single lookup.
- **Full suite:** `pytest tests` has the same 28 pre-existing failures and 3
  collection errors as before. Nothing new fails.

## Operator steps

1. Check NVDA/BA/META/NKE/TSLA positions and orders directly in Schwab.
2. Restart the bot process. The restart alone clears the stuck stopped engine on
   v3.3.23; deploying v3.3.24 also requires a restart.
3. Click "Verificar Schwab (solo lectura)". The red banner should show
   "Broker confirmado plano".
4. Start the bot.
