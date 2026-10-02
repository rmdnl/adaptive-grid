# adaptive-grid — Changelog

This repository is a Binance SPOT grid-trading bot that is deliberately kept
in DRY_RUN / paper mode. Live trading is not implemented and must not be
enabled without explicit authorization. All changes below preserve that
invariant.

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
