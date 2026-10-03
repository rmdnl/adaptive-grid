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

## Milestone 4 — Roadmap H final audit (commit this milestone)

Full-scope audit of the now-complete safe roadmap (F-H1, F-H2, G, E, H).

- **Security / secret scan** — no hardcoded secrets in tracked files; `.env`
  untracked; `.venv` / DB / logs / caches untracked. The only
  credential-bearing file is `tests/test_phase8b_account_credential.py`,
  which contains redaction fixtures, not real secrets.
- **Configuration / default audit** — `config.yaml` defaults hold every
  invariant: `mode=testnet`, `dry_run=true`, `allow_live_execution=false`,
  `step_pct=0.006`, `hard_min_net_pct=0.003`,
  `max_equity_drawdown_pct=0.02`, `range_break_buffer_pct=0.01`.
  `main()` raises if `dry_run` is ever false.
- **Risk-invariant audit** — verified in source: `equity_dd_kill` /
  `range_break_kill` / `strict_order_price_gate` are all wired into the
  combined risk decision; the kill latch is an absolute veto on new order
  placement (`main.py` `kill_now_active` gate) and survives restart; the
  minimum-net / step enforcement is enforced fail-closed in
  `config_loader.py`.
- **Test audit** — 42 test files, 990 `def test_*` functions (plus
  parametrized cases); **1093 passed, 0 failed**.
- **Live-execution audit** — no production path enables live trading;
  `ExchangeEventApplier.place_order()` and the paper cycle both place no
  live orders; no withdrawal permission anywhere.
- **Changelog + limitations** — `CHANGES.md` (milestone history) and
  `LIMITATIONS.md` (hard invariants, known limitations, blocked decisions,
  definition-of-done status) added.

Outcome: the safe roadmap is complete. Every remaining capability (a live
user-data stream, live execution) is deliberately out of scope because it is
a human-authorization decision, not an autonomous coding task.

## Commit hashes

- `288349d` feat: persist equity-drawdown kill-switch reference across restarts (PATCH 1 / F-H1)
- `1311b03` docs: add autonomous run report; correct README test-count target
- `72bb9a4` feat: F-H2 fail-closed cancel-on-kill + explicit reference reset
- `ef2d3a9` feat: Roadmap G operational resilience (health/shutdown/restart-recovery)
- `d7005fa` feat: Roadmap E deterministic exchange-event applier + REST seam
- (this milestone) `docs: Roadmap H final audit + changelog + limitations report`

## Milestone 5 — 15m candle-close lower-boundary kill (dedicated gate)

Fixes the verified audit gap: the repository had `range_break_kill`
(current ticker price ± buffer) but NOT a dedicated 15-minute candle-close
lower-boundary stop. This milestone implements it as a separate, fail-closed
Risk Engine gate wired into the production `main()` risk decision.

- `risk_engine.lower_boundary_15m_kill(closed_candle_close, lower_price,
  stop_if_below_lower_pct)` — Decimal gate:
  `threshold = LOWER_PRICE * (1 - stop_pct)`; kill iff
  `closed_close <= threshold`.  Fail-closed outcomes:
  `LOWER_BOUNDARY_STOP_CONFIG_INVALID` (bad stop_pct / lower price),
  `LOWER_BOUNDARY_STOP_DATA_UNAVAILABLE` (missing/invalid/NaN/Inf close),
  `LOWER_BOUNDARY_STOP_15M` (trigger).  Never substitutes the ticker.
- `main._latest_closed_candle_close(df)` — reads `df["close"].iloc[-1]`
  from the closed-candle DataFrame (`fetch_klines(drop_incomplete=True)`);
  any missing/empty/NaN/non-finite/non-positive frame yields `None`
  (fail-closed veto, never a silent PASS).
- `main()` — the gate is in the base risk `decisions` list; its
  `LOWER_BOUNDARY_STOP_15M` reason is a kill trigger (latches the kill
  state + cancel-on-kill; survives restart).  `CONFIG_INVALID` /
  `DATA_UNAVAILABLE` veto new orders for the run but do NOT latch the
  kill state (a transient data problem must not persist a kill).
- Config: `risk.stop_if_below_lower_pct` is now REQUIRED and validated in
  `config_loader.validate_config` (finite Decimal strictly in (0,1)); no
  hidden fallback default.  `config.yaml` carries the default `0.02`.
  All 8 test-fixture config builders updated explicitly.
- Existing `range_break_buffer_pct` / `range_break_kill` are unchanged and
  remain a separate, independent protection.
- `tests/test_15m_lower_boundary_kill.py` (22 tests) covers spec items
  A–L: exact/below/above threshold, ticker-independent, malformed/missing/
  NaN/Inf close, restart persistence, new-order prevention, cancel-on-kill
  invocation, failed-cancel keeps kill active, idempotent re-evaluation,
  and unchanged `range_break_kill` behavior.  A production-wiring guard
  test prevents regression to a dead helper.
- Docs: `AGENTS.md` invariant expanded; `LIMITATIONS.md` audit row and
  invariant corrected (no longer claims the stop was already sufficient);
  `FINAL_INTEGRATION_AUDIT.md` marked HISTORICAL; this report updated.

Test result: full `pytest -q` -> **1136 passed, 0 failed** (was 1093; +43 total
across Milestones 5 and Round 3 audit hardening).
Targeted: `tests/test_15m_lower_boundary_kill.py` 22/22 pass; related
risk/cancel/recovery/config suites (169) pass.

## Round 4 — Paper Soak Test (commits `d687eb1`, `33d4422`)

Long-running deterministic PAPER-ONLY soak test proving the paper engine
remains deterministic, state-consistent, restart-safe,
reconciliation-safe, kill-switch-safe, duplicate-order-safe, and fail-closed.

Harness: `tests/test_paper_soak.py` (11 tests, no network, no live Binance).

### Objectives covered

- **A+K. Long deterministic run + replay**: 2 x 10,000 cycles (seed=42,
  default `MAIN_SOAK_CYCLES`; override via `PAPER_SOAK_CYCLES` env var).
  8-segment regime sequence cycled deterministically to reach target.
  Two identical runs → byte-equal order IDs, statuses, fills, account
  state, and kill state.
- **B. Market regimes**: ranging / trend-up / trend-down / sharp-move /
  boundary-approach / range-break / recovery / 15m lower-boundary kill.
  Accounting invariants verified every cycle (first 500 cycles) and every
  100 cycles thereafter.
- **C. Order lifecycle**: fill / partial-fill / cancel / duplicate-event /
  rejected / rejected-partial-fill, exercised via PaperSession and
  PaperOrderEngine; invariants checked after every event.
- **D. Network failure simulation**: UNKNOWN cancel keeps kill active;
  FAILED cancel keeps kill active; CONFIRMED cancel releases reservation;
  duplicate fill event returns `idempotent=True`.
- **E. Restart chaos**: restart before-submit, after-kill-activation,
  after-partial-fill; no duplicate orders; kill persists; invariants hold.
- **F. Kill-switch**: equity drawdown exact/above/below 2%; range-break
  ±1% buffer; 15m candle close <= LOWER x 0.98; kill persists across
  restart; unknown/failed cancel keeps kill active; subsequent cycles
  blocked.
- **G. Inventory/accounting conservation**: all balances >= 0, reservation
  sums match account state, after every cycle across 50-cycle stress test.
- **H. Order identity**: same `cycle_id` → same `client_order_id`;
  idempotent replay adds no new orders; restart replay is idempotent.
- **I. Persistence corruption fail-closed**: malformed reference equity
  (returns None), corrupt order status (unhealthy), corrupt Decimal
  (unhealthy), missing account state row (unhealthy), non-zero reservation
  on CANCELED order (unhealthy).
- **J. Reconciliation**: healthy local state passes recovery; exchange FILL /
  CANCEL events applied via `ExchangeEventApplier`; unknown remote order not
  applied; no-reconciler case raises (fail closed); duplicate event idempotent.
- **M. State growth**: after 150 cycles — single `kill_state` row (PK),
  single `paper_account_state` row (PK), final recovery healthy.

### Result

- Concrete safety findings: **NONE**
- Soak suite: **11/11 passed** (674s, default 10,000-cycle config)
- Full pytest: **1147 passed, 0 failed**

### Remaining known limitations

- OS-level SQLite write-failure (interrupted commit, disk error) is not
  injected in this harness; `cycle_transaction` atomicity and
  `PaperStateUnhealthyError` behavior are covered by existing
  `tests/test_paper_orchestrator.py` and `tests/test_recovery.py`.
- A live `RestReconciler` implementation is a future authorized task.

## Round 3 — Adversarial Safety Audit (commits `8bed7b5`, `71eb41a`, `57dfcbd`)

Comprehensive adversarial audit attempting to break safety invariants
through real exchange/API failure modes.

### Concrete findings fixed

**Finding 1: Silent latest CLOSED candle drop in fetch_klines (commit `8bed7b5`)**
- Root cause: `pd.to_numeric(errors="coerce")` + `dropna()` silently removed
  the latest CLOSED candle when it had malformed OHLCV. The 15m kill gate
  would then evaluate a *previous* candle's close, violating fail-closed.
- Fix: latest CLOSED candle OHLCV validated BEFORE dropna; raises
  `MarketDataError`. Only older/middle candles are dropped.
- Tests: `tests/test_fetch_klines_safety.py` (19 tests, spec A–I).

**Finding 2: NaN/Infinity equity bypass (commit `57dfcbd`)**
- Root cause: `build_account_risk_state` checked `current_equity < 0` but
  NaN passes `NaN < 0` as False in Decimal arithmetic. A non-finite equity
  would produce NaN drawdown/inventory that could bypass kill gates.
- Fix: explicit `current_equity.is_finite()` check →
  `AccountValidationError("Current equity is not finite")`.
- Tests: `test_account_risk_rejects_nan_equity`,
  `test_account_risk_rejects_infinite_equity`, expanded
  `test_dd_kill_exact_threshold_blocks` (added above-threshold boundary).

### Non-findings (analyzed, no change needed)

- **Indicator-layer dropna in latest_valid_row**: safe — fetch_klines now
  validates OHLCV before `enrich()`; derived indicator NaN (rolling warm-up)
  correctly falls back to previous valid observation; the kill gate uses
  raw close from validated DataFrame, not `latest_valid_row`.
- **Kill-switch concurrent activation**: kill-trigger reasons make
  `combined.allowed=False`; the cycle gate at `main.py:960` requires
  `combined.allowed`, preventing orders in a kill-triggering run. The
  `kill_now_active` re-read is defense-in-depth for restart recovery.
- **Timeout-after-submit duplicate order**: paper path has no network gap
  between submit and persist — SQLite `BEGIN IMMEDIATE`/commit provides
  atomicity. Restart reuses same `cycle_id` → same `fill_id` → idempotent.
- **NaN in snapshot balances**: already rejected by `_account_decimal()`
  in `fetch_account_snapshot`.
- **Malformed/missing latest closed candle**: now raises `MarketDataError`
  before `main()` reaches indicator computation or order placement (verified
  by `test_main_aborts_on_malformed_latest_closed_candle`).

### Remaining tasks (next safe milestones)

NONE remaining on the safe roadmap. Roadmap H (final audit) is complete this
run; every safe milestone (F-H1, F-H2, Roadmap G, Roadmap E, Roadmap H) is
done.

## Round 5 — Independent Code Audit + Testnet Readiness (commit `fd04540`)

Independent audit performed directly from source, not from prior Hermes reports.
Covers Steps 1–13 of the Round 5 brief.

### Step 1 — Contract files

`SKILL.md`, `AGENTS.md`, `AUTONOMOUS_TASK.md` read and verified as the
engineering contract. All invariants confirmed present in source.

### Step 2 — Source audit (all 13 production modules)

Inspected: `market_data.py`, `risk_engine.py`, `grid_engine.py`,
`symbol_rules.py`, `order_engine.py`, `paper_orchestrator.py`, `storage.py`,
`exchange_events.py`, `market_features.py`, `market_regime.py`,
`grid_eligibility.py`, `main.py`, `fee_model.py`, `cancel_controller.py`,
`paper_accounting.py`, `recovery.py`, `runstate.py`, `config_loader.py`,
`binance_testnet.py`, `inventory_model.py`.

No dead safety paths, no duplicate kill-switch implementations, no unused
config keys affecting safety, no live-execution bypass.

### Step 3 — Round 3 verification (A: latest CLOSED candle; B: non-finite equity)

**A — Closed candle path verified in source:**
- `fetch_klines(drop_incomplete=True)` filters on `close_time <= now`.
- `market_data.py:687–718`: latest CLOSED candle OHLCV validated BEFORE
  `dropna()`; NaN/Inf/non-positive/non-numeric close raises `MarketDataError`
  (fail closed). Same for open/high/low/volume.
- `main._latest_closed_candle_close(df)`: reads `df["close"].iloc[-1]`, returns
  `None` (not a substitute close) for any missing/empty/NaN/non-finite/non-positive
  value; `None` → `LOWER_BOUNDARY_STOP_DATA_UNAVAILABLE` veto (fail closed).
- Ticker price is **never** used as a substitute for the closed candle close.
- `tests/test_fetch_klines_safety.py` (19 tests) and
  `tests/test_15m_lower_boundary_kill.py` (22 tests) exercise the production path.

**B — Non-finite equity verified in source:**
- `build_account_risk_state` (`market_data.py:638`): explicit
  `current_equity.is_finite()` check → `AccountValidationError` (fail closed).
- NaN drawdown path is impossible after this check.
- Drawdown boundary tests (exact 2%, just below 2%, just above 2%) all
  covered in `tests/test_risk.py` and `tests/test_kill_switch_persistence.py`.

### Step 4 — Round 3 adversarial claims verified

1. **Timeout-after-submit**: paper path uses `BEGIN IMMEDIATE` / `commit`;
   no network gap. `cycle_id` → `fill_id` are deterministic; restart is idempotent.
2. **Duplicate client_order_id**: `save_order_submission` checks `SELECT 1` before
   insert; `DuplicateOrder` raised if already present. `order_engine.submit` also
   checks `self.get(intent.client_order_id)` pre-insert. Two independent guards.
3. **Restart after submission**: idempotency via `_check_existing_cycle`; committed
   cycle record → idempotent replay. Rolled-back cycle → no record → clean re-run.
4. **Partial fill**: `apply_fill` updates `executed_qty`; `remaining_qty` tracked;
   state `PARTIALLY_FILLED` → `FILLED` only when `remaining_qty == 0`.
5. **Duplicate fill event**: `get_fill(fill_id)` checked before insert; same
   semantics → `idempotent=True`; different semantics → `FillIdentityMismatch`.
6. **Unknown order event**: `ExchangeEventApplier` records and flags unknown orders;
   local state untouched; reconciliation required.
7. **Reconciliation uncertainty**: `_ensure_healthy()` gate; unhealthy recovery
   raises `PaperStateUnhealthyError` → blocks submit/fill/transition.
8. **Failed cancellation**: `CancelOutcome.UNKNOWN/FAILED` leaves local state
   untouched; kill stays active with `PENDING_RECONCILIATION`; latch persists.
9. **Unknown cancellation**: treated as `UNKNOWN`; fail-closed. Malformed
   canceler response → `CancelOutcome.UNKNOWN`.
10. **Kill-state persistence**: `kill_state` table with `key='kill_state'` PRIMARY KEY;
    survives restart; `verify_restart_safety` blocks the run if kill is active.
11. **Kill vs order-submission race**: kill trigger reasons collapse
    `combined.allowed=False` before `should_submit` is evaluated (line 1240–1247
    in `paper_orchestrator.py`); no race window.
12. **Atomic paper cycle rollback**: `cycle_transaction` covers lifecycle, order
    submission, fill, accounting, cycle record in one `BEGIN IMMEDIATE`/`COMMIT`;
    any exception triggers full rollback.

**CAN A NEW ECONOMIC ORDER BE CREATED TWICE?** No.
- `save_order_submission` SELECT-before-INSERT within `BEGIN IMMEDIATE`.
- `order_engine.submit` `self.get()` check before `save_order_submission`.
- `cycle_transaction` atomicity: failed second-order submission rolls back
  entire cycle including any first-order already submitted in that cycle.
- `_check_existing_cycle` idempotency: a committed cycle record is replayed,
  not re-executed.

### Step 5 — Round 4 soak harness quality audit

**Finding (P2 — test coverage gap): AK harness never latched kill state.**

The `test_soak_AK_long_run_and_determinism_replay` test's loop polled
`get_kill_state()` to detect kill activation, but `paper_orchestrator.run_cycle()`
never writes `kill_state` — that is `main.py`'s responsibility via
`CancelController.pre_latch/latch_kill_state`. As a result:
- `kill_active` never flipped to `True` in the AK loop.
- All 10,000 cycles ran with `risk_allowed=True` (no post-kill blocked cycles).
- The "determinism replay" compared two identical runs that both exercised only
  the pre-kill path (23 orders, 17 fills), never the post-kill state machine.

**Fix (commit `fd04540`):** The AK loop now explicitly calls `set_kill_state`
when the first `range_break` segment price arrives (matching what `main.py` does
via `CancelController`). Added hard assertions:
- `run_submitted > 0` and `run_fills > 0` (meaningful activity before kill).
- `kill_active == True` and DB row present after `set_kill_state` call.
- `ks["trigger"] == "RANGE_BREAK_BELOW_BUFFER"`.
- `post_kill_submitted == 0` (no new orders after kill latch; enforced by the
  vetoing `risk_allowed=False` passed to the orchestrator).

Tested: `PAPER_SOAK_CYCLES=300` → 11/11 passed (76s). Full non-soak suite: 1136/1136.

**Coverage of meaningful transitions confirmed (probe at 3,000 cycles):**
- `orders_submitted`: 23 (pre-kill), `fills_applied`: 17 (pre-kill).
- `orders by status`: `{'FILLED': 17, 'OPEN': 6}`.
- Post-kill (`risk_allowed=False`): 0 orders submitted.

**Sampled invariant check gaps:** Invariant checks every 100 cycles (after first 500)
can miss corruption between samples. This is a documented trade-off for runtime:
the first 500 cycles check every cycle and cover all 8 regime segments plus the
kill activation. Post-kill cycles are structurally identical (no-op blocked cycles);
sampling every 100 is sufficient.

**Determinism verification:** two identical seed-42 runs compare order IDs,
statuses, fill IDs, account state, and kill state. With the kill-latch fix, both
runs now exercise 23 orders, 17 fills, and post-kill blocked state identically.
UUIDs and timestamps are excluded from the comparison (only semantic state compared).

### Step 6 — Testnet readiness audit

**`binance_testnet.py` (BinanceTestnetClient):**
- Class docstring: "Read-only Binance Spot Testnet client. Never exposes order methods."
- No `new_order`, `place_order`, `cancel_order`, `modify_order`, `withdraw` methods exist.
- `assert_testnet_read_only(config)` called in `__init__`; `_validate()` enforces:
  `environment == "testnet"`, `dry_run == True`, `allow_live_execution == False`,
  `base_url == "https://testnet.binance.vision"`.
- Production URLs in `_REJECTED_PRODUCTION_BASES` explicitly rejected.
- `timeout_ms` default 5000, max 30000; `retries` default 3; `backoff_ms` default 1000.
- Rate-limit handling: SDK retries with `backoff`; no explicit 429 handling beyond SDK.
- Symbol filter coverage: `PRICE_FILTER`, `LOT_SIZE`, `MARKET_LOT_SIZE`,
  `NOTIONAL/MIN_NOTIONAL`, `PERCENT_PRICE`, `PERCENT_PRICE_BY_SIDE`, `MAX_NUM_ORDERS`,
  `MAX_NUM_ALGO_ORDERS` — all parsed in `symbol_rules.parse_symbol_info`.
- `_validate_filter_consistency`: rejects contradictory filter bounds (e.g. minPrice > maxPrice).
- Fee retrieval: `account_commission(symbol)` → `fee_model.effective_fees` → conservative
  maker/taker fallback when incomplete.
- `LIMIT_MAKER` behaviour: order_engine accepts `LIMIT_MAKER` with GTC; validation
  in `_SUPPORTED_ORDER_TYPES = frozenset({"LIMIT", "LIMIT_MAKER"})`.
- Order status / open-order retrieval: `open_orders(symbol)` → validated snapshot.
- Account balance: `account()` → duplicate-asset detection (fail closed).
- Clock/timestamp: `server_time()` → skew-ms reported in connectivity snapshot.
- Credential-redaction: `_redact_credentials` removes key/secret from error messages.
- **Testnet limitation**: `MAX_NUM_ALGO_ORDERS` may differ from production (testnet
  often returns 0 or a different limit). The adapter parses it; a 0 value means no
  limit enforced at the exchange level (documented, not a bug).

### Step 7 — Live safety barrier

Traced every path from config to order submission:

1. `config_loader.validate_config`: `if not dry_run: raise ConfigError(...)` (line 140–143).
2. `BinanceTestnetConfig._validate()`: `if not self.dry_run: raise BinanceTestnetConfigError`.
3. `assert_testnet_read_only(config)` called in `BinanceTestnetClient.__init__`.
4. `_validate_binance` in `config_loader.py`: `environment` must be `'testnet'`;
   `base_url` must contain `testnet.binance.vision`.
5. `BinanceTestnetClient` exposes no order-placement method (confirmed by inspection
   and `test_read_client_issues_get_only_and_no_trading_methods`).
6. `paper_orchestrator.run_cycle` / `PaperOrderEngine.submit` contain no HTTP calls.
7. `main()` checks `cfg["dry_run"] == False → raise` at startup.

**Verdict:** An accidental `dry_run=false` in `config.yaml` raises `ConfigError`
at load time before any market data is fetched. There is no path from config to a
live order endpoint when the dry_run guard is active. The testnet adapter has no
order-placement method; it is structurally impossible for it to place a live order.

### Step 8 — Configuration audit

| Key | Validated default |
|---|---|
| `dry_run` | `true` (ConfigError if false) |
| `allow_live_execution` | `false` (BinanceTestnetConfigError if true) |
| `grid.step_pct` | `0.006` (>0 required) |
| `grid.hard_min_net_pct` | `0.003` (≥0.003 required) |
| `risk.max_equity_drawdown_pct` | `0.02` (>0 required) |
| `risk.range_break_buffer_pct` | `0.01` (≥0 required) |
| `risk.stop_if_below_lower_pct` | `0.02` (required; finite Decimal in (0,1)) |
| `timeframe` | `"15m"` (locked; ConfigError if anything else) |
| `binance.environment` | `"testnet"` (required; ConfigError if not testnet) |
| `binance.base_url` | must contain `testnet.binance.vision` |

Malformed config: every field is validated before `main()` proceeds to market data.
No dangerous default can silently enable live execution.

### Step 9 — Database / state audit

**Schema version:** `SCHEMA_VERSION = "3.2.1"` / `PRAGMA user_version = 321`.
No runtime migration logic; idempotent `CREATE TABLE IF NOT EXISTS`.

**Crash windows and behavior:**
1. *Before order intent*: no DB writes. Clean restart → empty run.
2. *Submit started, transaction open*: `BEGIN IMMEDIATE` prevents concurrent writers.
   Crash before `COMMIT` → rollback. No record → clean re-run.
3. *After commit*: cycle record exists → idempotent replay on restart.
4. *Fill processing mid-cycle*: fill + order update + accounting update all in same
   `cycle_transaction`; crash rolls back all. Restart re-evaluates fills.
5. *Kill latch (`pre_latch`)* written BEFORE cancel pass (separate connection).
   Crash mid-cancel → kill stays latched → restart enters kill branch.
6. *Reconciliation*: `recover_paper_state` detects orphan reservations, non-zero
   reservation on CANCELED orders, malformed Decimals; returns unhealthy →
   `PaperStateUnhealthyError` gates submit/fill/transition.

**Generation checks:** `active_plan.generation != current_generation` detected
in `_validate_lifecycle_integrity` → `GENERATION_MISMATCH` → fail closed.

**No stale state paths**: `expected_order` parameter in `save_order` detects
concurrent mutation (optimistic locking).

### Step 10 — Security audit

- No secrets in tracked files. `grep` scan: clean (only `.venv` library fixtures).
- `.env` untracked; confirmed by `git check-ignore -v .env`.
- `_redact_credentials` in `market_data.py` and `binance_testnet.py` redacts
  known secrets from SDK error messages.
- Logs use `logger.error` with structured messages; no print of balances/keys.
- No withdrawal method anywhere in the codebase.
- Testnet credentials isolated from production: `BinanceTestnetConfig` enforces
  `environment == "testnet"` and `base_url` must be the testnet host.
- Paper mode (dry run) requires no credentials (`PaperOrderEngine` has no client).

### Step 11 — Test quality audit

- No `pytest.skip`, `@pytest.mark.skip`, or `@pytest.mark.xfail` in any test file.
- No bare `except: pass` swallowing assertions.
- No live network calls in tests (`test_phase8b_account_credential.py` monkeypatches
  `HTTPAdapter.send` at the transport layer — no real HTTP traffic; it verifies
  method=GET constraint at the adapter level).
- `time.sleep` / `uuid` / `datetime.now()`: absent from tests except controlled
  synthetic clocks. `random.Random(seed)` used deterministically in soak harness.
- 15 test files use `monkeypatch`; all stub at `main.*` (module-level attributes
  the production path calls through). Production `PaperOrderEngine`, `RiskDecision`,
  `CancelController`, `ExchangeEventApplier` are exercised without stubs.
- Full suite: **1147 passed, 0 failed** (984s at default 10,000-cycle soak).

### Step 12 — Code quality / dead paths

- No unreachable safety code found.
- `range_gate` in `risk_engine.py` is an alias for `range_break_kill`
  (backward-compatible); it is used in `tests/test_range_engine.py` but not in
  `main.py` (main.py calls `range_break_kill` directly). Not a dead safety path.
- `fee_model.effective_fees` discount path intentionally omitted (conservative).
- No live execution paths found that bypass the Risk Engine.
- `error` paths in `paper_orchestrator._run_cycle_txn` always re-raise
  (never swallowed); `cycle_transaction` rolls back on `BaseException`.

### Step 13 — Testnet readiness verdict

| Area | Verdict | Notes |
|---|---|---|
| Paper engine | **READY** | Deterministic, restart-safe, kill-safe, tested at 10k cycles |
| Risk engine | **READY** | All 4 kill gates wired, fail-closed, regression-tested |
| Market data | **READY** | Closed-candle validation, NaN guard, ticker freshness |
| Persistence | **READY** | Atomic transactions, schema version, optimistic locking |
| Reconciliation | **NEEDS HARDENING** | Local-only; live `RestReconciler` not implemented |
| Binance SDK integration | **READY** | Read-only testnet adapter; order methods absent |
| Symbol validation | **READY** | All 8 filter types parsed; consistency check present |
| Order lifecycle | **READY** | Full state machine; generation-aware IDs; no duplication |
| Cancellation | **READY** | Fail-closed; UNKNOWN/FAILED keeps kill active |
| Account/equity | **READY** | NaN/Inf guard; persist peak; reference gate |
| Testnet isolation | **READY** | URL, environment, dry_run triple-locked |
| Live safety barrier | **READY** | Config → adapter → paper engine: no order path |
| Observability/logging | **NEEDS HARDENING** | Health JSONL and structured logs present; no live alerting |

**TESTNET READINESS:** The repository is technically ready for a controlled read-only
Binance Spot Testnet phase (market data, account snapshot, symbol info, connectivity
check). It is NOT ready for live order placement on testnet or production.

**Remaining testnet blockers:**
1. `RestReconciler` (live cancel/order-status fetch) not implemented.
2. Rate-limit handling relies on SDK retry; no explicit 429/418 backoff documented.
3. `MAX_NUM_ALGO_ORDERS` testnet value may be 0 (no algo-order limit); adapter
   parses but does not warn when the limit is effectively absent.
4. Clock skew handling: `server_time()` reports skew-ms but does not adjust
   request timestamps; large skew (>1000ms) may cause authentication failures.

**Security:** CLEAN. No secrets in repo. Live execution: DISABLED.

### Commits

- `fd04540` test: Round 5 — strengthen AK soak harness with meaningful-activity
  and kill-latch coverage assertions

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
- The 15m lower-boundary stop (dedicated `lower_boundary_15m_kill` candle-close
  gate, implemented in this milestone) and the range-break kill remain
  fail-closed; the persisted peak is used only for the 2% equity drawdown gate.
- Grid invariants unchanged: 0.30% hard min net, 0.60% gross step, 2%
  drawdown kill, strict range protection, risk-engine veto over every order.

## Blocked decisions (require human authorization)

- None in this run. Enabling live trading, a real exchange event stream,
  loosening any risk parameter, or adding withdrawal permission would each
  require explicit human sign-off per AGENTS.md and were NOT done.

---

# Round 7 — Gated testnet order path + live testnet verification (2026-10-03)

## Objective

Close the Round 5 testnet blockers and reach "deployment-ready and verified
on Binance Testnet": implement the explicitly gated order path (LIMIT_MAKER
+ cancel) and the concrete REST reconciliation executor, then verify the
full order lifecycle against the real Binance Spot Testnet.

## What was implemented

- `testnet_orders.py` (new) — `BinanceTestnetOrderClient`:
  - Double-gated: validated testnet-only config (testnet URL, `DRY_RUN=true`,
    `ALLOW_LIVE_EXECUTION=false` re-asserted at construction) AND explicit
    `TESTNET_ORDERS_ENABLED=true` env gate (strict boolean, default false).
  - Exactly two capabilities: `place_limit_maker_order` (post-only, exact
    decimal strings, `newOrderRespType=RESULT` for validated acks) and
    `cancel_order_by_client_id`.  No market/OCO/algo/SOR/batch/withdraw
    methods exist on the class (tested).
  - Deterministic outcome semantics (Round 6A §3): validated ack = CONFIRMED;
    400-family exchange rejection with Binance error code = deterministic
    FAILED (`BinanceTestnetOrderRejectedError`); timeout/network/429/418 =
    typed UNKNOWN (rate-limit errors keep Retry-After; timestamp skew keeps
    the §10 classification).  POSTs are never retried; a lost submission ack
    is settled by resolving the deterministic clientOrderId (§4), never by
    resubmitting.
  - `make_cancel_executor(resolver=...)` — the concrete §5 cancel executor
    for the `RestReconciler` seam: CONFIRMED_CANCELED only on a validated
    CANCELED ack or an authoritative re-query settling the ambiguous
    -2011/-2013 family; a fill racing the cancel stays UNRECONCILED;
    the executor never raises.
- `scripts/testnet_order_path_check.py` (new) — the skill §21 verification
  harness: read-only by default; `--place-order` (gate required) runs
  place → authoritative resolve → open-order reconcile → duplicate-cid
  rejection while open → verified cancel → post-cancel re-resolve →
  restart recovery with fresh client instances; `--verify-cid` proves
  restart recovery from a brand-new process.  Fail-closed, best-effort
  cleanup of any stray order.
- `tests/test_testnet_orders.py` (new, 36 tests) — gate enforcement, minimal
  surface, ack validation, rejection/UNKNOWN/rate-limit/skew classification,
  executor contract (never raises, never confirms an unproven cancel),
  reconciliation integration, credential redaction, decimal serialization.
- Docs: README Phase 7 section, `.env.example` gate, LIMITATIONS refresh.

## Live Binance Spot Testnet verification evidence (2026-10-03)

Read-only suite (`scripts/testnet_order_path_check.py`): connectivity,
clock skew (13-62ms), symbol filters (tick 0.01 / step 0.001 / minNotional 5),
balances (405,275 USDT free), open orders — ALL PASS.

Full order path (`--place-order`, 12/12 PASS, exit 0):

- Placed LIMIT_MAKER BUY 0.01 BNB @ 764.62 (notional 7.65 USDT, filter-validated):
  ack status NEW, orderId 3790263, cid AGTV-BNBUSDT-1790998639.
- Authoritative single-order resolve by clientOrderId: NEW.
- `reconcile_open_orders`: exact match, authoritative.
- Duplicate prevention: resubmission with the SAME clientOrderId while the
  order is open → deterministic rejection (Binance code -2010); open-order
  count unchanged.
- Verified cancel: testnet consistently "loses" the first DELETE response
  (SDK retry sees -2011 "Unknown order sent."); the executor settled the
  ambiguity via authoritative re-query → CANCELED, executedQty=0.  This is
  the live demonstration of the §5 read/write settlement rules.
- Post-cancel re-resolve: CANCELED.  Restart recovery: fresh client +
  reconciler instances (and two brand-new processes via `--verify-cid`)
  resolved both historical orders as CANCELED.

Production `main()` dry-run cycle against real testnet data: real candles,
ticker 768.52, equity 405,275.83 USDT, open orders VERIFIED (0), and the
risk engine correctly vetoed the grid (range too narrow → GRID_COUNT_INVALID,
VOLATILITY_TOO_LOW) — "Execution: DRY RUN, no order placement".

Notable live-learned behavior: Binance Spot frees a clientOrderId once its
order is canceled (uniqueness holds only among open orders).  The
duplicate-prevention check therefore runs while the order is open, and the
first (pre-fix) post-cancel resubmission test actually placed a duplicate
that was cleaned up by the script's best-effort cancel pass (final state:
0 open orders; documented in LIMITATIONS item 8).

## Test results

- New suite: `tests/test_testnet_orders.py` — 36 passed.
- Related suites (testnet adapter, reconciler): 177 passed.
- Full `pytest -q`: see final count below (recorded at commit time).
- Lint: ruff not enforced repo-wide (no config); new files match repo
  conventions (Optional[...] style, deliberate §16 blind-except guards).
  Real findings fixed (unused import, unused noqa tags).

## Safety invariants — unchanged

- Spot only; no futures/margin/leverage/shorting.
- `dry_run=true` default; `main()` still raises on `dry_run=false`.
- `allow_live_execution=false`; no production endpoint constructible.
- The order path cannot reach any non-testnet host (URL pinned + re-asserted).
- `TESTNET_ORDERS_ENABLED` defaults to false — unset env = read-only.
- Grid invariants unchanged (0.30% min net, 0.60% step, 2% drawdown kill,
  ±1% range-break buffer, 15m lower-boundary kill, risk veto over orders).

## Remaining (future, separately-authorized tasks)

- Wiring the verified order path into a live testnet trading cycle
  (state machine exists; `main()` remains paper-only by invariant).
- User-data websocket event stream (Roadmap E seam is ready for it).
- Live trading enablement (explicit human sign-off required).

---

# Round 8 — Continuous TESTNET cycle hardening (2026-10-03)

## Audit before coding (what Round 7 left open)

Round 7 delivered a verified *one-shot* order path (place → resolve →
reconcile → cancel → cleanup inside a single script run).  Missing for a
controlled *continuous* cycle:

1. No cycle loop: no repeated market-data → range/grid → risk → order →
   reconcile → cleanup stages.
2. No persistent cycle ledger: a restart mid-run leaves no durable local
   record of placed orders/cids beyond the exchange itself.
3. No kill latch for the cycle path (the paper kill_state lives in the paper
   DB and is bound to the paper engine).
4. No end-of-run cleanup PROOF (zero unintended open orders) or
   foreign-order handling (orders on the account outside our namespace).
5. No graceful-shutdown integration for the cycle loop.
6. No restart recovery pass that reconciles non-terminal orders from prior
   runs before new cycles.

## Implementation plan (written before coding)

New module `testnet_cycle.py` — touches NO production path (main.py,
config.yaml, paper DB, PaperOrderEngine stay untouched):

- `TestnetCycleConfig` (frozen, validated fail-closed): symbol, bounded
  max_cycles (1..100), poll interval, per-cycle order cap, per-order quote
  size, ledger path; strategy/risk parameters sourced ONLY from the
  validated `config_loader.load_config()` dict (step 0.006, hard_min 0.003,
  min_cells 6, drawdown 2%, range-break buffer 1%, 15m lower-boundary stop
  2%, market filter) — single source of truth, no re-declared constants.
- `CycleLedger` (separate SQLite file, `data/testnet_cycle.sqlite3`,
  gitignored; PRAGMA user_version 800; no migration of any existing DB):
  `cycle_runs`, `cycle_orders` (cid PK, state machine INTENT →
  SUBMITTED_UNKNOWN → OPEN/PARTIALLY_FILLED → FILLED/CANCELED/REJECTED,
  plus PENDING_RECONCILIATION), `cycle_events` (observability journal),
  `cycle_kill_state` (persistent latch), `cycle_bot_state` (reference
  equity high-water mark).
- `TestnetCycleRunner`:
  - REUSES the production grid/risk math read-only: `fetch_klines` +
    `indicators.enrich` + `auto_range` + `build_geometric_grid` +
    `validate_quantized_order_plan` (min-net + filters enforced) and the
    EXACT `risk_engine` gates (`range_break_kill`,
    `lower_boundary_15m_kill`, `equity_dd_kill` vs persisted HWM,
    `equity_reference_gate`, `market_gate`, `open_orders_gate`,
    `profit gate`, `strict_order_price_gate`).  No re-implemented risk
    logic; the Risk Engine stays the authoritative veto.
  - `preflight`: clock-skew sync (fail closed beyond bound), symbol rules,
    foreign-open-order refusal (fail-closed before any placement), kill
    check.
  - `run_cycle`: gates → intents (lowest allowed BUY cells INSIDE the
    effective range, quantity re-checked against free quote balance) →
    placement (deterministic cid `AGTC-...`, never reused) →
    per-order settlement (ack / deterministic rejection / UNKNOWN resolved
    by clientOrderId — never resubmitted) → reconciliation pass (authoritative
    snapshot corrects local state; fills from executedQty) → cycle-end
    verified cancellation of still-open orders.
  - `recover()`: startup reconciliation of non-terminal ledger orders from
    prior runs; unresolvable → PENDING_RECONCILIATION and placement stays
    blocked (fail closed).
  - `cleanup()`: reconcile → cancel only confirmed-open own orders →
    re-resolve → prove zero own open orders and zero non-terminal ledger
    orders; anything unprovable → FAIL with exact ids.  Foreign orders are
    never touched and reported.
  - Kill path: latch FIRST (persisted, survives restart), then fail-closed
    cancel-on-kill; no new orders while latched; restart enters kill branch.
  - ShutdownCoordinator consulted at cycle boundaries (graceful stop;
    cleanup always runs).
- `scripts/testnet_cycle_check.py`: gated CLI (`--mode rehearsal|orders`,
  orders requires `TESTNET_ORDERS_ENABLED=true`; `--cleanup-only`,
  `--status`), bounded cycles, JSON/human output.
- `tests/test_testnet_cycle.py`: deterministic fake-exchange tests covering
  the 25 required scenarios (reusing Round 7 executor-contract tests where
  they already cover cancellation timeout / connection failure / lost
  submission ACK / duplicate-cid semantics at the client level).

Safety boundary: `DRY_RUN=true`, `ALLOW_LIVE_EXECUTION=false`,
`main()` paper-only, `TESTNET_ORDERS_ENABLED` default false — unchanged.

## Results

(appended after implementation and verification)

## Results (Round 8)

### Deterministic tests

`tests/test_testnet_cycle.py` — 39 deterministic fake-exchange tests over
the REAL production math (calibrated candle fixture: ADX 23.3, ATR 1.8%,
BB 5.0%, range approved q≈83, 8-cell grid, net 0.352%/cell) covering all
25 required scenarios:

fresh start; existing open orders (own → recovered, foreign → placement
refused); restart while orders open / after confirmed fill / with UNKNOWN
state / while kill latched; kill during active cycle (latch + cancel-on-kill
persisted); cancellation timeout (lost ack settled by authoritative
re-query) and connection failure (order stays unresolved, never claimed
canceled); lost submission ack (resolved by clientOrderId, exactly one
POST, never resubmitted); duplicate order prevention (ledger-level cid
refusal + exchange -2010 handling); stale local state corrected from the
authoritative snapshot (CANCELED and FILLED corrections with fill qty);
no new order after kill; no order outside range (strict gate vetoes a
cell priced above the upper bound; all placed prices proven inside the
effective range); risk veto (real market_gate block); insufficient balance
(fail-closed before submission); rate-limit response (bounded 3-attempt
budget, no blind retry, preflight and cycle paths); clock skew beyond
bound (fail closed); network interruption mid-cycle (fail closed, state
persisted) and recovery; graceful shutdown at safe boundary (cycle 1
completes, cycle 2 never starts, cleanup runs); persistence survives
restart (orders/events/HWM/kill intact, HWM never lowered); rehearsal mode
makes zero write calls + static proof that main.py and the paper modules
never import the cycle/order modules; locked invariants cannot be loosened
via cycle config (hard_min ≥ 0.003, drawdown == 0.02, buffer == 0.01,
bounded cycles/orders).

### Live Binance Spot Testnet verification (2026-10-03)

- Rehearsal cycle (BNBUSDT, read-only): real candles → range veto recorded
  (width ~1.8% < 3% minimum) → zero writes → cleanup ok → exit 0.
- Symbol probes (read-only): ETHUSDT vetoed (5 cells < 6), BTCUSDT vetoed
  (quality), DOGEUSDT vetoed (volume spike) — gates authoritative on live
  data; SOLUSDT / ADAUSDT / LINKUSDT / XRPUSDT legitimately passed the full
  stack.
- Orders cycle (SOLUSDT, 2 cycles): 4 real LIMIT_MAKER orders placed
  (~25 USDT each, deterministic cids AGTC-SOLUSDT-…), all inside the
  effective range (117.328–122.24, quality 82.48), risk PASS, each cycle
  ended flat via verified cancellation; cleanup proved zero own open
  orders; exit 0.
- Restart run (fresh process, same ledger): recovered prior terminal
  orders, placed 2 more orders, cycle-end cancel + cleanup ok; exit 0.
- cleanup-only proof: `{"ok": true, "canceled": [], "unresolved": [],
  "foreign": []}` — zero open own orders, zero unresolved.
- Ungated `--mode orders` invocation refused (fail-closed gate verified).

### Round 8 testnet order accounting

| Metric | Value |
|---|---|
| Orders created | 6 (4 + 2 across two runs) |
| Filled | 0 |
| Canceled (verified) | 6 |
| UNKNOWN / PENDING_RECONCILIATION | 0 |
| Final open-order count (proven) | 0 |
| Foreign orders touched | 0 |

### Safety boundary (unchanged)

`DRY_RUN=true`; `ALLOW_LIVE_EXECUTION=false`; `main()` paper-only and still
raises on `dry_run=false`; `TESTNET_ORDERS_ENABLED` default false; no
production endpoint constructible; Risk Engine veto over every order; no
secrets in code, logs, events, or the ledger.

### Remaining (future increments, not blockers)

- Operator release command for the cycle kill latch (mirroring
  `release_kill_state.py`) — documented in LIMITATIONS.
- Fill-event accounting for orders that FILL while the cycle runs (status
  is tracked and reconciled; PnL accounting remains the paper engine's
  domain until a separately authorized execution wiring).
- User-data websocket event stream (Roadmap E seam ready).
