# Execution visibility and engine lifecycle — v3.3.15

## What the supplied screenshots establish

The Thinkorswim screenshots and bot panel show October 22. The panel appears
to be an older v3.3.8 display. They do not establish which code ran on October
23 or whether v3.3.14 has been deployed. The TSLA and META P/L values differ,
and position snapshots also need reconciliation. TOS daily P/L, open-position
P/L and local gross strategy P/L have different scopes. Comparing those
headlines cannot identify the exact missing/mispriced trade. Match timestamps,
account, position and exact broker order identities; do not invent a linkage.

An earlier October 23 audit confirmed BA's later signals were blocked with
REAL_ACCOUNTING_UNRESOLVED. A missing closing order identity, including after
a manual broker close, can leave this guard active. That is not proof that
the Python process crashed. Neither those old records nor existing guards are
cleared by this release.

## Source bugs addressed

The controller previously set BOT_STATE.running=True after starting the engine
thread. If the thread failed immediately, its finally block set False first,
then the controller could overwrite it with True. Publish the starting flag
before starting the thread, preserve its actual final state, and prevent a
previous engine runner from clearing a newer engine's flag. These changes do
not add an automatic stop, restart, liquidation or retry of a failed engine.

The status API now exposes the Python version loaded at server startup and
the current thread lifecycle, start/end times in UTC and New York, and a static
error code. A thread start is not engine initialization, a healthy market-data
feed, a broker order, or a fill. Startup/exit logs are persisted through the
existing dashboard server log. No historical start time is reconstructed from
the process start, first trade or an operator's uncertain recollection.

The dashboard no longer retains an ACTIVO badge when status polling fails. It
shows SIN CONEXION and disables the uncertain toggle until status recovers.
Failed position requests remove stale price cards and show unknown state;
they do not display zero exposure or imply the Schwab account is flat.

## Per-signal execution evidence

GET /api/signals preserves archived AI decisions and signal reference prices,
and annotates a copy from the local order audit. Correlation requires the exact
signal ID, ticker, mode and NY session date. No time/price/ticker-only guess is
allowed. A missing/legacy/unreadable audit remains unavailable, without schema
migration or file writes. Raw order IDs, arbitrary broker exceptions and
unrecognized reason strings are not added to this annotation.

Signal rows separately show AI approval and recorded order evidence: blocked
(including REAL_ACCOUNTING_UNRESOLVED), submitted without confirmed fill,
uncertain, canceled/terminated, recorded entry fill, or unresolved close/reversal.
Canceled does not assert zero partial fills. A recorded fill observation is
not a fresh broker query or a verification of its price, size, current position
or P/L. PAPER remains explicitly simulated. Signal price is labeled as a
strategy reference, not an execution price; a missing reference is N/D.

The existing mode-separated/fill-verified P/L rules from earlier releases are
preserved. No historical fill, trade, x/50 counter, model or production approval
is edited. This release does not add entry repricing, relax risk/AI gates or
guarantee a LIMIT fill. The protected ordinary LIMIT exit policy from v3.3.14
remains in force, with emergency exits available.

## Read-only historical timeline

scripts/audit_engine_timeline.py --date 2026-10-23 --journal reads the retained
dashboard_server.log and optionally gala.service journalctl JSON. It exports
only fixed event codes, mode, source and aware timestamps; it makes no broker
or authentication request and writes no project files. Date selection is NY,
including DST conversion of UTC journal timestamps.

Start requests, controller acceptances, engine initialization logs, thread
failures, stops, unresolved exposure, service exits/restarts and source-file
permission failures remain distinct. A service exit is not automatically an
OOM or an engine crash. Missing/rotated logs are unknown evidence. The report's
first observed time is not a claim that no earlier launch occurred.

Read-only diagnostics can be run from the fetched script without merging or
restarting the service. The checked-out version in that report is distinct from
the loaded server version in authenticated /api/status. Historical journal and
audit evidence are still required to establish October 23's exact incident.

## Activation

Use the original /opt/gala/GALA-Trading-Bot installation. Preserve .env,
config.yaml, existing models, trading/AI history, ownership and private modes.
Before stopping or restarting gala.service, arrange the maintenance window,
check engine/scheduler/training state and obtain fresh broker evidence for
positions and working orders: stopping can trigger REAL liquidation. Run the
complete isolated regression suite as gala before activation. Deployment is
not authorization to resume REAL trading or promote SHADOW models.

The maintenance helper scripts/upgrade_original_v315.sh takes the reviewed full
commit SHA as its only argument. Run as root only after fresh operator checks
show engine OFF, scheduler manual, AI idle, and no broker positions/orders,
with all dashboard operators paused. It validates the original service path
and user, a clean tracked checkout, the exact remote head, a fast-forward-only
update, an unchanged private/runtime path diff, and network-namespace support.
It ensures Node's Request/Headers APIs are available to gala for the UI tests.

Before stopping, the helper rechecks the configured automatic-training window
and fresh structured read-only broker flatness. An active Auto AI training
window makes it abort rather than change the saved ON/OFF flag. It archives the
original installation privately (excluding .git and .venv), including existing
model/history/config artifacts, and records a private list of pre-existing
non-source/non-operational-log files for a GNU tar comparison after tests.
Only tracked public Python/HTML/CSS/JS source and VERSION gain read permission;
.env, config.yaml and binary/JSON model/history permissions are preserved.

The helper requires all 420 tests to pass as gala inside unshare -n with zero
skips, checks .env/config checksums and preserved-file contents/metadata, then
rechecks training-window and broker-flat evidence before starting gala.service.
It never calls /api/bot/start, changes scheduler/modes/AI settings, clears an
accounting block, repairs historical fills or promotes a model. A failed
pre-start check leaves the service stopped for review; no automatic rollback
or state reset is attempted. Failures after starting require service-status
inspection. After success, authenticate to /api/status and /api/ai/status to
verify loaded server version, engine OFF and saved Auto AI settings. OAuth may
persist legitimately refreshed credentials during the read-only broker checks.

Validation: 420 offline unittest tests passed with zero skips, including 33
new tests of the real controller/runner race, authenticated signal/status APIs,
actual JavaScript rendering and failed polls, read-only timeline extraction,
NY/DST date boundaries and private-data suppression. No live broker order,
service operation, training job or model promotion was performed.
