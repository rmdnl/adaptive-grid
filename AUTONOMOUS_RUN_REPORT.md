# AUTONOMOUS RUN REPORT — adaptive-grid

Run date: 2026-10-02 (UTC+7). Branch: `hermes/autonomous`.

## Test result

- Baseline (half-applied PATCH 1 in working tree): **19 failed, 1012 passed** (214s).
- After this run: **1031 passed, 0 failed** (237s), full `pytest -q`.
- Targeted (kill-switch + main integration suites): 34/34 passed.

## Summary of changes

Completed the half-applied "PATCH 1 (F-H1): persist the equity-drawdown kill-switch
reference" work that was left in the working tree (in-progress edits to `main.py`,
`risk_engine.py`, `tests/test_paper_validation.py`, new `tests/test_kill_switch_persistence.py`).

Root cause of the baseline failures: `main()` still referenced the deleted global
`_SESSION_REFERENCE_EQUITY` (NameError in production; `monkeypatch.setattr`
AttributeError in 19 integration tests), and the new persistence helpers
(`load_peak_equity`, `record_peak_equity`, `equity_reference_gate`) were never
wired into the risk path.

Changes (commit `288349d`):

- `main.py`
  - Removed the dead `_SESSION_REFERENCE_EQUITY` global usage.
  - `raw_reference`/`reference_equity` are now read from the persisted
    `paper_reference_equity` bot_state key on every run (unconditionally), and
    passed to `build_account_risk_state` (None when absent → bootstrap).
  - Peak equity recorded (high-water mark only: raises, never lowers) after
    every successful equity observation, and NOT recorded when a present
    reference is corrupt — silent "repair" would defeat the kill switch.
  - `equity_reference_gate(raw_reference, reference_equity)` added to the
    combined risk decisions: absent reference passes; corrupt reference blocks
    with `EQUITY_REFERENCE_INVALID` (fail-closed).
- `risk_engine.py` — `equity_reference_gate` (present in working tree; kept,
  now actually used by `main()`).
- `tests/test_kill_switch_persistence.py` (new, 9 tests) — all pass:
  persistence across restart, no down-reset of the peak, healthy-growth update,
  corrupt-state fail-closed load, absent-key load, drawdown block through the
  real `main.main()` production path, corrupt-reference block, open-book
  preservation under a vetoed submission, no-kill-switch on equity growth.
  - One exchange-stub fix: this file's tests use the shared `_symbol_info()`
    (±5% PERCENT_PRICE band), which made grid cells near the manual range edge
    get rejected by quantization before the kill switch could be the sole block
    reason. Added `_wide_percent_price_symbol_info()` (±10% band, matching the
    actual BNBUSDT testnet band) so the kill switch is isolated under test.
- `tests/test_paper_validation.py` — machine-independent repo-root paths for
    static source-scan tests (was hardcoded `c:/Projects/adaptive-grid/...`).
- `tests/test_main_order_integration.py`, `tests/test_main_market_intelligence.py`,
  `tests/test_main_adaptive_planner.py` — removed stale `monkeypatch.setattr(main,
  "_SESSION_REFERENCE_EQUITY", None)` lines referencing the deleted global
  (not a weakening: the reference now lives in the fresh per-test SQLite DB).
- `README.md` — "Account-risk state" section now documents the persisted
  high-water-mark reference and its fail-closed behavior (previously documented
  the old session-local, non-persisted semantics).
- Committed the repo's contract files `AGENTS.md` and `AUTONOMOUS_TASK.md`
  (untracked on-disk, part of the project's contract).

## Milestone 2 — F-H2 cancel-on-kill + explicit reference reset

Test result: **1055 passed, 0 failed** (full `pytest -q`). Targeted:
`tests/test_cancel_on_kill.py` (14), `tests/test_kill_state_integration.py`
(10), `tests/test_kill_switch_persistence.py` (9) all pass. Both operator
commands smoke-tested end-to-end (live-mode refusal, input validation, audit
rows, key clearing, release-with-unreconciled refusal, reconcile+release).

### Task 1: explicit operator-triggered `paper_reference_equity` reset

- `main.reset_reference_equity(db, new_value, reason, actor)` — the ONLY way
  to change the reference apart from the automatic high-water-mark raise.
  Requires a non-empty reason + actor; writes a durable
  `reference_equity_audits` row (timestamp, previous, new, reason, actor).
  `new_value=None` clears the key so the next run re-bootstraps; a value
  replaces it. Automatic reset is still prohibited: `record_peak_equity`
  only ever raises the peak, never lowers it.
- `scripts/reset_reference_equity.py` — the explicit operator command.
  Refuses unless the config is explicitly `dry_run=true` AND
  `allow_live_execution=false` (fail-closed). Supports `--value` / `--clear`
  and `--dry-run` preview.
- New storage primitive `clear_state` (distinct from `set_state`) so a clear
  yields key-absent, not an empty string the fail-closed gate would reject.
- Tests: set + audit, clear, missing reason/actor, invalid value,
  persistence across restart, and that a reset does NOT weaken the 2% drawdown
  kill (a higher reference still lets the kill fire through `main.main()`).

### Task 2: F-H2 fail-closed cancel-on-kill

- `cancel_controller.CancelController` — owns local paper-order state and the
  durable kill latch. An injectable `canceler` models exchange outcomes:
  `CONFIRMED` / `ALREADY_CANCELED` transition the local order to CANCELED and
  release the remaining reservation; `UNKNOWN` / `FAILED` never move local
  state, keep the reservation, and leave the kill active with
  `PENDING_RECONCILIATION`. Bounded retry (injectable sleep), idempotent
  cancel records, and operator-confirmed `reconcile_cancelled` to close out
  unreconciled orders. `release()` refuses while any open order is
  unreconciled (fail-closed); it only removes the latch, never places orders.
- Crash-safety: `pre_latch` persists the kill **before** the cancel pass, so a
  crash mid-cancel still leaves the kill latched and a restart re-enters the
  kill branch (regression-tested).
- `main()`:
  * restart gate — an active persisted kill re-enters the cancel/reconcile
    branch and stops the run (no orders).
  * trigger-time latch — equity-drawdown kill or range-break kill latches the
    kill state, cancels open orders, and records a `KILL_TRIGGER` risk event.
  * absolute veto — new order placement is blocked while the kill latch is
    active, re-read from the authoritative `kill_state` (no replacement orders).
- `scripts/release_kill_state.py` — operator release with `--reconcile`
  per-order confirmation; live-mode and unreconciled-order refusals are
  fail-closed and audited.
- New schema: `kill_state`, `kill_state_audits`, `cancel_records`,
  `reference_equity_audits` (idempotent migrations).
- Tests: successful cancel, no-open-orders, FAILED / UNKNOWN fail-closed,
  partial-fail-then-reconcile-allows-release, reconcile refusal (unconfirmed /
  filled / unknown), idempotent repeat pass, already-canceled, bounded-retry
  then confirm, malformed canceler → UNKNOWN, kill-state persistence + restart
  block + no new orders, release audit trail, pre-latch crash safety,
  pre-latch progress preservation, main() kill-trigger latching (drawdown and
  range-break), main() restart blocking, and release-refused-with-unreconciled.

Commits: see "Commit hashes" below.

## Commit hashes

- `288349d` feat: persist equity-drawdown kill-switch reference across restarts (PATCH 1 / F-H1)
- `1311b03` docs: add autonomous run report; correct README test-count target
- (this milestone) `feat: F-H2 fail-closed cancel-on-kill + explicit reference reset`

## Milestone 3 — Roadmap G operational resilience + Roadmap E exchange events

Test result: **1093 passed, 0 failed** (full `pytest -q`). Targeted:
`tests/test_roadmap_g.py` (17) and `tests/test_roadmap_e.py` (21) pass.

### Roadmap G — operational resilience (read-only observability + safe
boundaries; no trading-logic changes)

- `health.py` — `HealthStatus` / `HealthReport` / `collect_health_report` /
  `write_health_jsonl`. Deterministic, machine-parseable snapshot of the
  persisted state (kill latch, reconciliation health, reference equity, local
  order counts, pending cancels, last risk/plan/cycle decisions, last run
  marker). A fresh DB with no paper activity is healthy-by-construction; a
  corrupt DB with activity is UNHEALTHY. The JSON payload is `sort_keys`
  with no wall-clock fields, so two identical states are byte-identical.
- `shutdown.py` — `ShutdownCoordinator`: a shutdown request only flips a
  flag; the run loop consults it at safe boundaries. Idempotent; the 2nd
  request sets `forced`; `complete()` is terminal. Signal handlers are opt-in
  and never used by the paper path or tests.
- `runstate.py` — `persist_run_state` / `read_run_state` /
  `verify_restart_safety` / `mark_run_interrupted` / `has_paper_activity`.
  The restart gate reads kill latch + prior run marker + reconciliation and
  decides RESUME / KILL_BRANCH / RECONCILE_THEN_RESUME / REFUSE. A fresh DB
  is safe to resume; corruption with activity is refused fail-closed.
- `main()` wiring: a `verify_restart_safety` REFUSE at the top of the run
  stops the process before any market data is fetched; a
  `shutdown.is_requested` check at the paper-cycle boundary skips the cycle;
  and a run-state marker + health JSONL line are persisted on completion
  (a health-report write failure is logged, not fatal — observability is
  never a trading gate).
- `scripts/status_report.py` — read-only operator status report; exit 0/1/2
  by status; `--json` / `--log`; refuses unless the config is explicitly
  dry-run with live execution disabled.
- Tests: `tests/test_roadmap_g.py` (17) — status precedence + exit codes,
  deterministic JSON, restart verdicts (fresh / completed / interrupted-with
  activity / kill / corrupt-with-activity), coordinator idempotency / forced /
  terminal-complete / boundary check, main() COMPLETED marker + JSONL, main()
  cycle-skip on shutdown request, main() REFUSE on corrupt-with-activity, kill
  veto, and collect_health_report reflecting kill/reference/unsafe-config.

### Roadmap E — deterministic, paper-only exchange events

- `exchange_events.py` — `ExchangeEventApplier` + typed `ExchangeEvent` /
  `ExchangeEventType` / `ApplyResult`. Maps exchange fill / cancel / reject /
  expire events onto the existing `PaperOrderEngine` + `CancelController`.
  **Read/outcome-only**: no order-placement path, no kill-latch release, no
  live stream (the `RestReconciler` is an abstract seam). A per-scope
  sequence watermark (monotonic high-water mark, advanced only on a
  successfully applied in-order event) plus a persisted event log
  (`exchange_events` / `exchange_sequence`) deliver:
  * duplicate events → idempotent no-op;
  * out-of-order / sequence gap → recorded, NOT applied, reconciliation
    required;
  * partial / full fills → `apply_fill` (excess qty rejected by the engine,
    flagged, never forced);
  * cancel / already-canceled → cancel path; terminal orders are clean no-ops;
  * unknown order / unknown state → recorded, NOT applied, flagged;
  * stale local state / network failure → fail-closed REST reconciliation via
    the read-only seam (local state untouched on failure, watermark not
    re-baselined);
  * restart recovery → resume from the persisted watermark, re-apply nothing
    already seen;
  * convergence → repeated apply / reconcile from the same inputs yields
    identical local state;
  * kill-state interaction → the applier never places orders and never
    releases the latch; while killed it only records cancels/fills.
- `order_engine.py` — extended the state machine so `OPEN` and
  `PARTIALLY_FILLED` may also transition to `REJECTED` (an exchange rejection
  that arrives after local submission). Pure extension; no transitions
  removed.
- `storage.py` — new `exchange_events` / `exchange_sequence` tables +
  `record_exchange_event` / `get_exchange_event` / `get_exchange_sequence` /
  `has_exchange_sequence` / `advance_exchange_sequence` /
  `set_exchange_sequence` (monotonic advance, plus an explicit re-baseline
  reserved for authoritative reconciliation).
- Tests: `tests/test_roadmap_e.py` (21) — partial/full fill, fill exceeding
  remaining qty rejected, fill-on-FILLED idempotent, duplicate no-op,
  out-of-order + sequence gap not applied, cancel confirmation,
  already-canceled no-op, reject transition, unknown local order, unknown
  event kind, cancel-on-filled stale-state flag, network-failure fail-closed
  (no reconciler / connection error), restart recovery, reconciliation
  convergence, exchange-only orders flagged for review, no order placement,
  and kill-latch interaction.

Commits: see "Commit hashes" below.

## Commit hashes

- `288349d` feat: persist equity-drawdown kill-switch reference across restarts (PATCH 1 / F-H1)
- `1311b03` docs: add autonomous run report; correct README test-count target
- `72bb9a4` feat: F-H2 fail-closed cancel-on-kill + explicit reference reset
- `ef2d3a9` feat: Roadmap G operational resilience (health/shutdown/restart-recovery)
- (this milestone) `feat: Roadmap E deterministic exchange-event applier + REST reconciliation seam`

## Remaining tasks (next safe milestones, not yet started)

DONE in this run: F-H2 cancel-on-kill + explicit reference reset; Roadmap G
(health / status / graceful shutdown / restart-recovery hardening); Roadmap E
(deterministic exchange-event applier + REST reconciliation seam + tests).

1. **Roadmap H final audit** — secret scan, configuration/default audit,
   risk-invariant audit, test audit, changelog / documentation, final
   repository audit.  (The remaining safe roadmap item.)

## Known limitations

- Live trading remains disabled (`dry_run: true`,
  `allow_live_execution: false`, `main()` raises if dry_run is false). No
  withdrawal permission anywhere.
- The Roadmap E `RestReconciler` is an abstract seam: there is no live
  Binance user-data stream or REST cancel/fetch implementation in this
  repository.  The deterministic model, state machine, and tests are the
  deliverable; a live feed is a future, separately-authorized task.  Until
  then, reconciliation reconciles against LOCAL paper state.
- `open_orders` reconciliation is still advisory:
  `open_orders_available_gate` blocks the plan whenever open-order state
  cannot be VERIFIED.
- The kill-state release command is paper-only and refuses to run when the
  config is not explicitly dry-run with live execution disabled.
- The 15m lower-boundary stop and range-break kill remain fail-closed; the
  persisted peak is used only for the 2% equity drawdown gate.
- Grid invariants unchanged: 0.30% hard min net, 0.60% gross step, 2%
  drawdown kill, strict range protection, risk-engine veto over every order.

## Blocked decisions (require human authorization)

- None in this run. Enabling live trading, a real exchange event stream,
  loosening any risk parameter, or adding withdrawal permission would each
  require explicit human sign-off per AGENTS.md and were NOT done.
