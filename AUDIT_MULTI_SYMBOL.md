# Audit — Multi-Symbol (v4.0) Repair

Date: 2026-10-04 · Branch: `hermes/autonomous` · Baseline: `main @ 36ee116`

## Objective

Adaptive-grid is now multi-symbol (`multi_symbol_main.py` is the authoritative
entry point). Audit the multi-symbol path against the locked trading contract
and the v4.0 strategy specification (auto-entry / auto-exit with close-all),
repair all defects, and verify on Binance Spot Testnet.

## Strategy specification (authoritative, user-supplied)

- Symbols from `.env` only (`SYMBOLS`), credentials from `.env` only
  (`BINANCE_ENV` + `BINANCE_TESTNET_API_KEY/SECRET`, `BINANCE_LIVE_API_KEY/SECRET`).
- Timeframe: 1h or 4h (config: 4h).
- Auto-entry (AND): ADX(14) < 20; RSI(14) < 35 OR %B <= 0; Volume
  Oscillator(5,10) > 0.
- Auto-exit (OR, close-all): RSI(14) >= 70; ADX(14) > 25; %B > 1;
  |Z-Score(20)| > 2.5. On exit: cancel ALL remaining grid orders, liquidate
  ALL held base inventory, then a 3-hour cooldown before new auto-entry.
- Grid: arithmetic for BTC/ETH/BNB, geometric for SOL; step = 1x ATR(14),
  floor gross 0.5%/grid; hard minimum net STRICTLY > 0.20% after fees (maker+taker
  0.1% each) and conservative slippage.
- Equity drawdown kill switch 2% — verified present (`risk.max_equity_drawdown_pct: 0.02`,
  `risk_engine.equity_dd_kill`, fail-closed config validation).

## Findings (multi-symbol path)

| ID | Severity | Finding |
|----|----------|---------|
| M1 | Critical (invariant) | `lower_boundary_15m_kill` was fed the latest closed **4h** candle (config timeframe) instead of the required **15m** close. The dedicated 15m lower-boundary stop silently degraded into a 4h gate. |
| M2 | High | `auto_range(...).approved` was ignored: unapproved ranges (price outside range, quality below threshold, width outside limits) flowed into grid construction; a degenerate (0,0) range surfaced as a crash instead of a clean block. |
| M3 | Critical (user spec) | Auto-exit routed through `_activate_kill_state`, latching the **permanent operator-release kill state**. User spec requires cancel-all + liquidate + 3h cooldown re-entry, not a manual-unlock latch. |
| M4 | Critical (user spec) | No liquidation existed at all: held base inventory was never market-sold on auto-exit. |
| M5 | High | No lifecycle close on auto-exit: the ACTIVE plan stayed open forever, so `has_active_grid` remained true and auto-entry could never fire again (zombie plan). |
| M6 | High | `PaperCycleInput.clock` used the wall clock while the orchestrator's market-freshness gate allows 5400s candle age — with 4h candles (age up to 14400s) the paper cycle was rejected as stale for most of every candle. main.py anchors the clock to the last closed candle; multi-symbol did not. |
| M7 | High (deployment) | `python3 multi_symbol_main.py` ran ONE pass and exited 0; the systemd unit (`Restart=on-failure`) never restarts a clean exit — the deployed bot died after its first cycle. |
| M8 | Medium | `verify_restart_safety` (REFUSE on corrupt prior activity) and the kill-latch restart branch (cancel/reconcile, refuse new orders) never ran in the multi path. |
| M9 | Medium | Entry-blocked and auto-exit outcomes returned `success=False` → logged as "CYCLE FAILED: None"; blocked results also lacked the price/range/step keys the summary printer reads (latent KeyError). |
| M10 | Medium | A failing `SymbolCycleRunner` construction (bad symbol / exchangeInfo error) aborted the whole loop — remaining symbols never ran. |
| M11 | Low | `BINANCE_ENV` selected credentials while `config.yaml` `environment.mode` was validated separately — the two could disagree (no live leak possible: `make_client` fail-closes to testnet only, but the inconsistency is unsafe). Testnet credentials were not required to exist. |
| M12 | Low | Market-intelligence eligibility decision was computed and discarded. Per the user's strict strategy spec, entry/exit is governed solely by the specified indicator rules — MI is retained as diagnostics/reporting only (main.py's legacy stack keeps its own behavior). |
| M13 | Low | `main.py` (legacy) read `BINANCE_API_KEY`/`BINANCE_API_SECRET`, which do not exist in `.env.example` — the legacy path could never authenticate account endpoints; also fed 4h data to the 15m gate (M1). |
| M14 | Low | `tests/test_multi_symbol.py` and `tests/test_strategy.py` referenced by README did not exist. |

## Interactions preserved (no locked parameter changed)

- Gross grid floor 0.5% + hard net minimum STRICTLY > 0.20%: with the conservative
  0.05% round-trip slippage, a 0.5% gross step nets 0.25% and is correctly
  REJECTED by the profitability guard; the effective gross floor is ~0.55%.
  This is the conservative behavior required by the contract (never weaken
  fee/slippage assumptions) and satisfies both user constraints simultaneously.
- Equity drawdown kill 2%, range-break buffer ±1%, 15m lower-boundary stop,
  risk-engine veto authority, DRY_RUN default — all unchanged and enforced
  on the multi-symbol path.

## Repairs

1. `grid_lifecycle.py`: new `LifecycleAction.CLOSE` and
   `LifecycleManager.close_active_plan(reason, details)` — ACTIVE /
   RECONFIGURATION_PENDING / READY_TO_RECONFIGURE → NO_ACTIVE_GRID with an
   audited transition row (idempotent).
2. `storage.py`: `record_paper_liquidation(...)` — single-transaction,
   optimistic-concurrency liquidation of the free base inventory at a market
   price with taker fee; realized PnL mirrors `prepare_fill_accounting`
   SELL math; audited via `paper_accounting_events` (event_type
   `LIQUIDATION`).
3. `market_data.py`: `fetch_15m_closed_close(client, symbol)` — fail-closed
   fetch/extract of the latest closed 15m candle close (None on any problem).
4. `multi_symbol_main.py`: strategy auto-exit = cancel pass → liquidation →
   lifecycle close → cooldown timestamp (no kill latch); risk kills still
   latch the permanent kill state via the same fail-closed controller; range
   approval gate; 15m boundary gate on true 15m data; anchored paper clock;
   restart-safety + kill-latch restart branch; per-symbol failure isolation;
   complete result payloads; `.env`-only credential selection with live
   refusal; continuous candle-cadence runtime (default `__main__`, `--once`
   for a single pass).
5. `main.py` (legacy): `.env` credential scheme + live refusal + 15m gate fix.
6. New tests: `tests/test_multi_symbol.py`, `tests/test_strategy.py`.

## Testnet verification results (2026-10-04, Binance Spot TESTNET)

| Check | Result |
|-------|--------|
| `scripts/testnet_readonly_check.py` | **PASS** — env testnet, ticker 788.39, skew −352 ms, account AVAILABLE, symbol rules VALID |
| `scripts/testnet_order_path_check.py` (read-only) | **PASS** — connectivity, clock skew, filters, balances, open orders |
| `scripts/testnet_order_path_check.py --place-order` (gated) | **ALL PASS** — LIMIT_MAKER BUY placed (status NEW), authoritative resolution, reconciliation exact match, duplicate clientOrderId rejected (−2010), verified cancel with authoritative re-query (CANCELED), fresh-process restart recovery |
| `multi_symbol_main.py --once` | **PASS** — 4 symbols, real 4h data: BTCUSDT RANGE_BLOCKED (auto-range width 30.9% > 25% limit), ETH/SOL/BNB ENTRY_BLOCKED with full indicator reasons; per-symbol isolation, state persisted, exit 0 |
| `multi_symbol_main.py --max-cycles 1` (runtime loop) | **PASS** — RUNTIME START → CYCLE START (STARTUP) → CYCLE RESULT OK → RUNTIME STOP (MAX_CYCLES) |
| `main.py` (legacy, after fixes) | **PASS** — authenticated account read (equity 405,275 USDT testnet), 39-cell grid evaluated, fail-closed block, open orders VERIFIED (0) |
| Full test suite | **1407 passed** (baseline 1369 + 38 new tests) |

Note: the pre-existing per-symbol databases contained orphaned reserved
balances (`base_reserved=0.1`, `quote_reserved=500` with zero reservation
rows) left by the pre-repair multi-symbol code. The newly enforced restart
safety correctly REFUSED them; they were archived to
`data/legacy_backup_20261004/` (gitignored) and the bot re-bootstrapped
cleanly. Hand-editing state to fake health was deliberately not done.

## Deployment posture

- `deploy/adaptive-grid-multi.service` runs `python3 multi_symbol_main.py`
  which now defaults to the continuous candle-cadence loop (one pass per
  closed 4h candle, SIGTERM-safe, `Restart=on-failure` for crashes).
- `BINANCE_ENV=testnet`, `dry_run=true`, `allow_live_execution=false`;
  live credentials are never read; live mode is refused at startup.
- Release command for a latched kill: `scripts/release_kill_state.py`
  (operator-gated, reconciliation-verified).

