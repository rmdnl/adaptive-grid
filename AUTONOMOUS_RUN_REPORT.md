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

## Remaining tasks (next safe milestones, not yet started)

DONE in this run: F-H2 cancel-on-kill (incl. persistence/restart + no-new-orders
veto + crash-safe pre-latch) and the explicit operator reference-reset command.

1. **Roadmap G: operational resilience** — structured logging, health/status
   reporting, graceful shutdown, restart recovery beyond what `recovery.py`
   already provides.
2. **Roadmap E: exchange event handling** (user-data stream) with
   deterministic reconnect/retry; reconcile local vs exchange state.  The
   cancel-on-kill path currently reconciles against LOCAL paper state; a real
   exchange-side cancel outcome (user-data stream / REST cancel) would plug
   into `CancelController._run_canceler` via the injected `canceler`.
3. **Operator kill-state release UX** — the release command is tested and
   documented; a dashboard/observability surface (roadmap, explicitly not a
   trading-control layer) is a future, non-safety-critical task.
4. **Roadmap H final audit** — secret scan, default review, changelog.

## Known limitations

- Live trading remains disabled (`dry_run: true`, `allow_live_execution: false`,
  `main()` raises if dry_run is false). No withdrawal permission anywhere.
- `open_orders` reconciliation is still advisory: `open_orders_available_gate`
  blocks the plan whenever open-order state cannot be VERIFIED (UNKNOWN status).
  The F-H2 cancel path reconciles LOCAL paper-order + reservation state; a real
  exchange-side cancel/outcome feed is a follow-up (Roadmap E) and would plug
  into `CancelController` via the injected `canceler` seam.
- The kill-state release command is paper-only and refuses to run when the
  config is not explicitly dry-run with live execution disabled.
- The 15m lower-boundary stop and range-break kill remain fail-closed; the
  persisted peak is used only for the 2% equity drawdown gate.
- Grid invariants unchanged: 0.30% hard min net, 0.60% gross step, 2% drawdown
  kill, strict range protection, risk-engine veto over every order.

## Blocked decisions (require human authorization)

- None in this run. (Items above are follow-ups, not blockers.) Enabling live
  trading, loosening any risk parameter, or adding withdrawal permission
  would each require explicit human sign-off per AGENTS.md and were NOT done.
