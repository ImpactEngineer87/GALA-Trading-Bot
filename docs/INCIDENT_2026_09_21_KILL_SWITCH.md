# Incident: September 21 Kill-Switch Reconciliation Failure

## What happened

The kill switch itself fired at the configured market-close path and successfully closed several positions. NVDA, NKE and META repeatedly returned `RECONCILIAR` because the local durable exit journal still contained old unresolved exits from September 15.

The duplicate-order guard was doing the right thing by refusing to blindly send another close, but it had no recovery rule for the case where the historical order was no longer WORKING and the broker position remained open. This made the stale local journal stronger than the current broker state.

## Evidence

The uploaded final reconciliation showed no WORKING order symbols. It still showed NVDA long 3 and NKE short 3. META was absent from the final open-position list. The order audit also recorded Schwab's explicit `FILLED cannot be canceled` response for the old META order.

## v3.3.2 rule

For emergency liquidation only:

```text
old pending exit
      ↓
is old order WORKING?
  yes → do not duplicate; wait/reconcile
  no  → check all WORKING orders for ticker
              ↓
         any still live?
          yes → do not duplicate
          no  → read current broker position
                    ↓
                 flat → clear stale journal; no new order
                 open → replace stale journal atomically and close
                        broker-confirmed current side/quantity
```

If any read is unavailable, the path remains fail-closed.
