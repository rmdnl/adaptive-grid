# adaptive-grid — Changelog

This repository is a Binance SPOT grid-trading bot that is deliberately kept
in DRY_RUN / paper mode. Live trading is not implemented and must not be
enabled without explicit authorization. All changes below preserve that
invariant.

## Release line: v5.0.0 (strategy replacement — locked multi-symbol spec)

### Milestone: the NEW strategy is the ONLY authoritative trading strategy
- **Entry (the ONLY mandatory filters, AND logic)**: `ADX(14) < 25 AND
  RSI(14) < 40 AND Bollinger %B <= 0` — all from CLOSED candles on the
  configured indicator timeframe (default 1h).  The Volume Oscillator is
  diagnostics only and NEVER gates entry; the previous ADX<20 / RSI<35 /
  VO>0 logic and the "RSI OR %B" alternative trigger are REMOVED.
- **Exit (OR logic, at-or-beyond the threshold)**: `RSI >= 70 OR
  ADX >= 25 OR %B >= 1 OR |Z-Score(20)| >= 2.5` (either Z extreme triggers
  the same emergency exit).  Boundary behavior pinned by tests: RSI 70.00 →
  EXIT, ADX 25.00 → EXIT, %B 1.00 → EXIT, Z ±2.5 → EXIT; entry boundaries
  strict (ADX 25.00 → FAIL, RSI 40.00 → FAIL, %B 0.01 → FAIL, %B 0.00 →
  PASS).
- **INSUFFICIENT_DATA**: missing/NaN indicators yield an explicit
  INSUFFICIENT_DATA signal (no trade, no silent defaults).
- **Grid step = GRID_STEP_ATR_MULTIPLIER × ATR(14)** (default 1.0): the
  fixed 0.5% floor is REMOVED — `grid_engine.atr_grid_step_pct` is pure
  ATR, an invalid ATR fails closed, and the 0.5% gross minimum is now a
  GATE (`GRID_GROSS_BELOW_MIN`) that BLOCKS low-volatility grids with a
  recorded reason instead of widening the step.  Executable/quantized net
  ≥ 0.30% remains the authoritative economics gate.
- **Old strategy removed (hard requirement)**: `main.py` (single-symbol
  legacy path), the old market-intelligence eligibility gate
  (`grid_eligibility.py`), the strategy-specific `market_filter` gate and
  config section, and the old validation replay harness are deleted with
  their dedicated tests; `runtime.py` remains as shared orchestration
  infrastructure only (GridRuntime) and can no longer run a strategy.
  Regression tests (`tests/test_strategy_replacement.py`) prove the old
  signal paths cannot generate orders.
- **Global account risk (2%, non-negotiable)**: `global_risk.py` computes
  shared global equity (USDT + Σ base×price across ALL symbols) once per
  pass; the reference equity is a persistent high-water-mark in the base
  DB (never auto-reset, audited); drawdown ≥ 2.00% latches the GLOBAL kill
  (boundary tests: 1.99% no kill / 2.00% kill / 2.01% kill), which
  propagates to EVERY symbol (cancel + latch, no automatic release);
  unknown equity with an existing reference FAILS CLOSED.
- **Per-symbol strategy state machine** (`strategy_state.py`): the ten
  locked states (WAITING_FOR_ENTRY … ERROR) with a validated transition
  table — invalid transitions are rejected and reported; state persists in
  each symbol's database and survives restart.
- **Exit/signal persistence**: `last_exit_signal` (symbol, timestamp,
  reasons, indicator values, thresholds, price, cancel/liquidation result)
  and `last_signal` (ADX/RSI/%B/VO/Z/ATR + entry/exit decisions + cooldown)
  persisted per symbol every cycle.
- **Configuration from `.env`** (operator-facing source of truth,
  config.yaml as documented fallback): PAIR_LIST, INDICATOR_TIMEFRAME=1h,
  ADX/RSI/BB/Z/ATR/VO periods, ADX_ENTRY_MAX=25, RSI_ENTRY_MAX=40,
  BB_ENTRY_MAX_PERCENT_B=0, ADX_EXIT_MIN=25, RSI_EXIT_MIN=70,
  BB_EXIT_MIN_PERCENT_B=1, ZSCORE_ABS_EXIT=2.5, GRID_STEP_ATR_MULTIPLIER=1.0,
  GRID_GROSS_MIN=0.005, MIN_NET_PROFIT_PER_GRID=0.003, COOLDOWN_HOURS=3,
  MAX_DRAWDOWN_PERCENT=2, STOP_IF_BELOW_LOWER_PERCENT=2.
- **Dashboard**: per-symbol indicator snapshot (ADX, RSI, %B, Z, ATR%),
  strategy state, entry signal, last exit reason, cooldown end time —
  always from real persisted state; global aggregates unchanged; still
  read-only, public, unauthenticated, separate process.
- **Volume Oscillator formula** corrected to the locked definition:
  `(SMA(volume,5) / SMA(volume,10)) − 1`.
- Tests: `tests/test_strategy.py` (rewritten, boundary table + VO-no-
  influence), `tests/test_global_risk.py` (boundaries + propagation),
  `tests/test_strategy_state.py` (transition validation, persistence),
  `tests/test_strategy_replacement.py` (old strategy cannot trade);
  no backtest implemented (not part of this task).

## Release line: v4.1.1 (multi-symbol dashboard repair)

### Milestone: dashboard adapted to the multi-symbol architecture
- Root cause: `dashboard._read_symbol_db` queried the orchestrator's
  `paper_orch_cycles` table **unguarded**. A symbol database created by
  `storage.init_db` (every runtime cycle does this before any paper cycle
  runs) does not contain that table until the first paper cycle — so the
  whole snapshot raised `sqlite3.OperationalError` and every route answered
  503 "snapshot unavailable". Additionally, every aggregate section
  (account, equity, kill state, runtime, orders, fills, cycles) read only
  the configured base database, which is empty in the multi-symbol
  architecture.
- `dashboard.build_snapshot` rewritten for the current state model:
  - **Per-symbol databases are authoritative**: price, range, risk decision,
    derived status (ACTIVE/BLOCKED/KILL_ACTIVE/NO_DATA), market-filter
    status, grid economics (from the latest risk-event payload: step, cells,
    min net, ATR, mode), kill state, run state, paper account, equity,
    open orders, recent orders/fills/cycles — every query individually
    guarded (missing table → empty section + `missing_tables` entry; missing
    or corrupt symbol DB → degraded, not fatal).
  - **Global aggregates**: summed reference equity / equity / realized PnL /
    fees, merged open orders and recent fills/cycles (each row tagged with
    its symbol), kill-active-any with per-symbol breakdown, global runtime
    phase, per-symbol DB health and missing-table reporting.
  - **Legacy fallback**: when no per-symbol database holds a value, the
    base-database (single-symbol) value is used so legacy deployments keep
    rendering unchanged.
  - Corrupt database files are now detected by probing the schema header
    (a garbage file passes a bare `SELECT 1`); failed connections are
    closed and reported.
- HTML: symbol cards render real per-symbol state (status badge, price,
  range, grid economics, open orders, fills, reference equity, risk reason,
  DB health); new per-symbol tables for status, kill states, runtime state,
  and per-symbol database health; global badges/cards use the aggregates.
- Trading behavior untouched: read-only connections only (`mode=ro`), no
  schema changes, no `paper_orch_cycles` fabrication, no trading-module
  imports, no credentials, still public and unauthenticated, separate
  process.
- Tests: `tests/test_dashboard.py` extended from 24 to 33 tests — the
  production VPS schema as fixture (base DB without `paper_orch_cycles`),
  multi-symbol per-symbol + aggregate assertions, missing-optional-table
  degradation, corrupt symbol file degradation, kill aggregation across
  symbols, no-mutation across all databases, per-symbol read-only and
  runtime-coexistence checks, secrets never in responses.

## Release line: v4.1.0 (gated testnet execution bridge — roadmap B)

### Milestone: real-order mirroring for the multi-symbol runtime
- `execution_bridge.py` (new) — the risk-gated paper cycle stays the decision
  engine; a double-gated bridge mirrors its outcomes to REAL Binance Spot
  TESTNET LIMIT_MAKER orders (same clientOrderId/price/quantity):
  - **Double gate, default OFF**: requires BOTH
    `execution.testnet_execution: true` (config, validated boolean) AND
    `TESTNET_ORDERS_ENABLED=true` (.env).  Either alone leaves every bridge
    method a recorded no-op.  The write client re-asserts the full testnet
    adapter barrier; there is no live path.
  - **Crash-safe placement**: a `PENDING_PLACE` row is persisted before the
    POST; the POST is never retried (duplicate clientOrderId is rejected by
    the exchange, verified −2010); a lost/ambiguous ack lands in `UNKNOWN`
    and is settled only by authoritative re-query.
  - **Own ledger, paper untouched**: real orders and fills live in the new
    per-symbol `execution_orders` table (explicit state machine:
    PENDING_PLACE → OPEN → FILLED/CANCELED/EXPIRED/REJECTED/UNKNOWN).  The
    paper engine keeps its deterministic fills — exactly one writer per
    ledger, no double accounting; divergence is reported, never silently
    reconciled.
  - **Cancel propagation**: strategy auto-exit and kill paths propagate the
    close-all to real testnet orders; ambiguous cancels (−2011/−2013) are
    settled only by authoritative re-query.  Real inventory is deliberately
    never market-sold (LIMIT_MAKER-only write surface) — reported for
    operator action instead.
  - **Fail-closed unknown-remote guard**: any open exchange order the
    runtime did not place blocks new mirroring until an operator resolves
    it.
  - **Per-cycle reconciliation**: authoritative status pass over every
    non-terminal mirrored order before the gates run.
- `multi_symbol_main.py` — bridges built once at startup per symbol with
  gate-status logging; wired into the kill branch, the kill-trigger path,
  the strategy-exit path, the pre-cycle gate stack, and post-cycle
  mirroring.  Behavior is byte-identical when the bridge is disabled.
- `config_loader.py` / `config.yaml` — optional, validated
  `execution.testnet_execution` (default `false`).
- Tests: `tests/test_execution_bridge.py` (22) — gate matrix, ledger state
  machine, mirroring outcomes, lost-ack recovery, partial-fill progress,
  cancel semantics, unknown-remote guard, real gate construction, runtime
  wiring (mirror + fail-closed block).
- Verification: bridge disabled run unchanged on testnet; bridge enabled
  run against real testnet (reconciliation + guard clean, exchange open
  orders 0, no placements — no entry conditions met); config reverted to
  default OFF.

## Release line: v4.0.1 (multi-symbol audit + repair, testnet-verified)

### Milestone: multi-symbol audit and full repair (branch `hermes/autonomous`)
- `multi_symbol_main.py` (rewritten) — the authoritative multi-symbol
  entrypoint now implements the locked v4.0 strategy end-to-end:
  - **Strategy auto-exit** (RSI>=70 | ADX>25 | %B>1 | |Z|>2.5) is a
    close-all: idempotent cancel pass over every open grid order, paper
    market liquidation of all free base inventory at the current price
    (taker fee, realized PnL), lifecycle plan closure, and the 3-hour
    auto-exit cooldown. It deliberately does NOT latch the permanent kill
    state (operator release is reserved for risk kills).
  - **Risk kills** (equity drawdown >= 2%, range-break ±1% buffer, 15m
    lower-boundary stop) latch the persisted kill state via the same
    fail-closed cancel controller; restarts re-enter the kill branch.
  - Auto-range approval is mandatory before grid work (price-inside,
    width, quality); the market-intelligence eligibility decision is
    diagnostics-only per the strict strategy spec.
  - Dedicated 15m lower-boundary gate now consumes the latest CLOSED **15m**
    candle (`market_data.fetch_15m_closed_close`), not the 4h strategy
    candle; missing data fails closed (veto, no latch).
  - Paper-cycle clock anchored to the last closed candle + timeframe with a
    matching `max_candle_age_seconds` (the 5400s default rejected most 4h
    cycles as stale).
  - Restart safety per symbol: `verify_restart_safety` (REFUSE on corrupt
    state with activity), kill-latch restart recovery, run-state markers.
  - Per-symbol failure isolation, complete result payloads (entry-blocked /
    cooldown / range-blocked are healthy outcomes, not errors).
  - Continuous candle-cadence runtime by default (`--once` for a single
    pass, `--max-cycles N` for bounded observation) — the systemd unit
    previously died after one pass (`Restart=on-failure` never restarts a
    clean exit 0).
- `config_loader.py` — shared fail-closed `resolve_binance_credentials`:
  `.env`-only, `BINANCE_ENV` must be `testnet` (live refused), config mode
  must agree, testnet credential pair required; LIVE credentials never read.
- `binance_testnet.py` — `load_testnet_config_from_env` accepts the
  canonical `BINANCE_TESTNET_API_KEY/SECRET` scheme (legacy names as
  fallback) and defaults `BINANCE_BASE_URL` to the approved testnet
  endpoint (still re-validated; production URLs keep failing closed).
- `grid_lifecycle.py` — `LifecycleAction.CLOSE` +
  `LifecycleManager.close_active_plan(reason, details)`: audited, idempotent
  ACTIVE/RECONFIGURATION_PENDING/READY_TO_RECONFIGURE → NO_ACTIVE_GRID.
- `storage.py` — `record_paper_liquidation`: single-transaction, optimistic-
  concurrency liquidation of free base inventory mirroring the paper SELL
  accounting math; audited via `paper_accounting_events` (LIQUIDATION).
- `recovery.py` — LIQUIDATION is a first-class accounting-event class in the
  reconciliation validator (reserved `LIQUIDATION-` client_order_id
  namespace + strict payload contract); all other events still require a
  known order.
- `market_data.py` — fail-closed `fetch_15m_closed_close`.
- `main.py` (legacy single-symbol) — `.env` credential scheme + live
  refusal; 15m gate input fix; `config.yaml` restores legacy
  `grid.step_pct: 0.006` for compatibility.
- Tests: `tests/test_multi_symbol.py` (21) + `tests/test_strategy.py` (17)
  — credentials, lifecycle close, liquidation math/idempotency, exit/
  cooldown flows, range gate, 15m fail-closed veto, drawdown kill latch,
  per-symbol isolation, clock anchoring, and the full entry/exit spec;
  conftest provides deterministic testnet env + safe 15m stubs.
- Verification: full suite 1407 passed; Binance Spot TESTNET read-only
  check PASS (auth, skew −352ms, filters); gated order-path check ALL PASS
  (LIMIT_MAKER placement, reconciliation, duplicate-cid prevention,
  verified cancel, restart recovery); repaired multi-symbol cycle verified
  end-to-end on testnet (real 4h data → correct RANGE_BLOCKED /
  ENTRY_BLOCKED fail-closed outcomes) in both `--once` and runtime-loop
  modes.

## Release line: v3.2.2 (dedicated 15m candle-close lower-boundary kill)

### Milestone: 15m lower-boundary candle-close kill (commit `20b13f7`)
- `risk_engine.lower_boundary_15m_kill` — dedicated fail-closed Decimal
  gate: kill when the latest CLOSED 15m candle close is
  `<= LOWER_PRICE * (1 - stop_if_below_lower_pct)`.  Distinct outcomes:
  `LOWER_BOUNDARY_STOP_CONFIG_INVALID`,
  `LOWER_BOUNDARY_STOP_DATA_UNAVAILABLE`, `LOWER_BOUNDARY_STOP_15M`.
  Independent of the unchanged current-price `range_break_kill`.
- `main.py` — `_latest_closed_candle_close(df)` reads the close from the
  fetched closed-kline DataFrame (`df["close"].iloc[-1]`; ticker never a
  substitute; None on missing/NaN/Inf/non-positive → fail-closed veto).
  Gate wired into the combined risk decision; `LOWER_BOUNDARY_STOP_15M`
  added to the kill-trigger set (latch + cancel-on-kill + restart survival).
  `CONFIG_INVALID` / `DATA_UNAVAILABLE` veto the run without latching kill.
- `config.yaml` / `config_loader.py` — `risk.stop_if_below_lower_pct`
  required & validated (finite Decimal strictly in (0,1)); no hidden fallback.
  All test-fixture configs updated explicitly.
- Tests: `tests/test_15m_lower_boundary_kill.py` (22) — spec A–L plus
  config-validation and production-wiring guard tests.
- Docs: AGENTS.md invariant, LIMITATIONS.md audit rows, README safeguard
  section, FINAL_INTEGRATION_AUDIT.md marked historical.

### Hardening: fetch_klines latest CLOSED candle validation (commit `8bed7b5`)
- **Root cause:** `fetch_klines()` used `pd.to_numeric(errors="coerce")` +
  `dropna()`, silently removing malformed latest CLOSED candle rows.
  The 15m lower-boundary kill could not evaluate safely on a substituted
  previous candle.
- **Fix:** latest CLOSED candle OHLCV validated BEFORE `dropna()`.
  Missing/NaN/Inf/non-positive/zero/negative → `MarketDataError` (fail
  closed). Only older/middle candles are dropped by `dropna`.
- Tests: `tests/test_fetch_klines_safety.py` (19) — spec A–I plus
  production-wiring and indicator non-finding tests.

### Hardening: account equity finiteness + boundary tests (commit `57dfcbd`)
- **Root cause:** `build_account_risk_state` checked `current_equity < 0`
  but NaN passes `NaN < 0` as False in Decimal arithmetic, potentially
  producing NaN drawdown/inventory that silently bypasses kill gates.
- **Fix:** explicit `current_equity.is_finite()` check →
  `AccountValidationError("Current equity is not finite")`.
- Tests: expanded `test_dd_kill_exact_threshold_blocks` with
  "just above 2%" + Decimal boundary; added
  `test_account_risk_rejects_nan_equity` / `test_account_risk_rejects_infinite_equity`.

## Release line: v3.2.1 (safety foundation + deterministic paper core)

### Milestone: Roadmap G — operational resilience (commit `ef2d3a9`)
- `health.py` — deterministic, machine-parseable health/status snapshot
  (`HealthStatus` / `HealthReport` / `collect_health_report` /
  `write_health_jsonl`). A fresh empty DB is healthy-by-construction; a
  corrupt DB with activity is UNHEALTHY. JSON payloads are `sort_keys` with no
  wall-clock fields.
- `shutdown.py` — `ShutdownCoordinator`: a shutdown request only flips a
  flag; the run loop consults it at safe boundaries. Idempotent; the second
  request sets `forced`; `complete()` is terminal.
- `runstate.py` — `persist_run_state` / `read_run_state` /
  `verify_restart_safety` / `mark_run_interrupted` / `has_paper_activity`.
  Restart gate decides RESUME / KILL_BRANCH / RECONCILE_THEN_RESUME / REFUSE.
- `main()` wiring: restart-safety REFUSE before market data is fetched; a
  shutdown boundary check skips the paper cycle; a run-state marker + health
  JSONL line are persisted on completion (observability, never a trading
  gate).
- `scripts/status_report.py` — read-only operator status command; exit 0/1/2
  by status; refuses unless the config is explicitly dry-run with live
  execution disabled.
- Tests: `tests/test_roadmap_g.py` (17 cases).

### Milestone: Roadmap E — deterministic exchange events (commit `d7005fa`)
- `exchange_events.py` — `ExchangeEventApplier` + typed event model. Maps
  exchange fill / cancel / reject / expire events onto the existing
  `PaperOrderEngine` + `CancelController`. Read/outcome-only: no order
  placement, no kill-latch release, no live stream (`RestReconciler` is an
  abstract seam). A per-scope monotonic sequence watermark + persisted event
  log deliver idempotent duplicate handling, fail-closed out-of-order / gap
  detection, partial/full fill mapping, cancel/already-canceled no-ops,
  unknown-order / unknown-state refusal, stale-state + network-failure
  fail-closed reconciliation, restart recovery, and convergence.
- `order_engine.py` — state machine extended so `OPEN` and
  `PARTIALLY_FILLED` may also transition to `REJECTED` (a pure extension).
- `storage.py` — `exchange_events` / `exchange_sequence` tables and
  monotonic / re-baseline sequence helpers.
- Tests: `tests/test_roadmap_e.py` (21 cases).

### Milestone: F-H2 fail-closed cancel-on-kill + explicit reference reset
(commit `72bb9a4`)
- `cancel_controller.py` — `CancelController` (injectable canceler;
  CONFIRMED / ALREADY_CANCELED cancel locally, UNKNOWN / FAILED keep the
  kill active and unreconciled; bounded retry; operator reconcile; release
  refuses while any open order is unreconciled). `pre_latch` persists the
  kill before the cancel pass so a crash mid-cancel still leaves it latched.
- `main()` — restart gate, trigger-time kill latch on drawdown / range-break
  kills, and an absolute veto on new order placement while the latch is
  active.
- `scripts/release_kill_state.py` / `scripts/reset_reference_equity.py` —
  explicit, paper-only, fail-closed, audited operator commands.
- `storage.py` — `kill_state`, `kill_state_audits`, `cancel_records`,
  `reference_equity_audits` (idempotent migrations) + `clear_state`.
- Tests: `tests/test_cancel_on_kill.py` (14), `tests/test_kill_state_integration.py` (10).

### Milestone: F-H1 persisted equity-drawdown reference (commit `288349d`)
- `main.py` / `risk_engine.py` — the drawdown reference (peak / high-water
  mark) is persisted in `bot_state` (`paper_reference_equity`) so the 2%
  equity kill switch survives a restart. Corrupt reference state fails closed
  (`EQUITY_REFERENCE_INVALID`) instead of being silently reset.
- Tests: `tests/test_kill_switch_persistence.py` (9 cases).

## Baseline (pre-autonomous)
- 5c5a452 Complete Phase 8B testnet validation
- 28abff5 test: expand fee tests and add range engine unit tests
- 293147b feat: add Binance Spot testnet read-only adapter
- 9e93cfe feat: add paper validation and stress testing
- 5b5a618 feat: add deterministic paper-trading orchestration
- 56d277a / 68d4b1c / 2fdfc36 / ac0a264 … deterministic grid + lifecycle
  foundations

## Version
v3.2.1 — DRY RUN / paper only. Live execution disabled. No withdrawal
permission. No futures / margin / leverage / shorting / martingale.
