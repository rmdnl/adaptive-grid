# EXTENDED PAPER VALIDATION — PHASE 7

Validation of the Binance Spot grid paper-trading engine (post FIX 4A/4B/4C + PATCH 5A/5B).
Validation-only: no architecture changes, no parameter optimization, no live trading.
Date: 2026-10-01.

---

## 1. Configuration

- **Mode:** `DRY_RUN=true`, `allow_live_execution=false`, `mode=testnet` (paper execution against a local SQLite engine).
- **Symbol:** BNBUSDT (spot, base BNB / quote USDT).
- **Timeframe:** 15m closed candles.
- **Initial balances:** 2 BNB base + 1000 USDT quote (project config `paper`).
- **Fees:** maker 0.001, taker 0.001, fee asset USDT (effective-fees path, single source).
- **Slippage:** 0.0005 round-trip (deterministic, per config).
- **Grid:** step 0.006 (0.6%), per-scenario ranges below; `hard_min_net_pct = 0.003`.
- No parameters were tuned during validation. Where the market was out of a configured range, cycles were allowed to block.

## 2. Data source

- `data/validation_klines_BNBUSDT_15m.csv` — Binance public **Spot** klines API, read-only GET, cached locally so all runs are offline-deterministic.
- 3999 closed candles, 2026-08-20 13:30 → 2026-10-01 05:15 UTC.
- No invented prices. No open orders, bids/asks, or account snapshots are synthesized: the engine's open-order/account truth is its own deterministic paper state (the paper-execution model), and market data is closed candles only.

## 3. Scenarios executed

All scenarios replay closed candles through the authoritative production path:
`main`-equivalent gates → `PaperSession.run_cycle()` (lifecycle + inventory + post-quant gate + capacity + atomic cycle transaction + deterministic fills + in-cycle recovery).

| ID | Candles | Grid range | Label | Cycles | Allowed | Blocked | Orders | Fills |
|---|---|---|---|---|---|---|---|---|
| A | 177–417 | 677–717 | stable range | 241 | 33 | 208 | 9 | 8 |
| B | 3045–3164 | 766–807 | volatile range | 120 | 28 | 92 | 6 | 5 |
| C | 1900–2016 | 705–750 | downward breakout | 117 | 13 | 104 | 10 | 8 |
| D | 1450–1557 | 717–760 | upward breakout | 108 | 7 | 101 | 9 | 8 |
| E | 177–417 | 677–717 | rapid oscillation | 241 | 33 | 208 | 9 | 8 |
| F | 177–272 | 677–717 | partial fills (full-fill model) | 96 | 33 | 63 | 9 | 5 |
| G | 177–272 | 677–717 | restarts @177/185/201/225/249/272 | 96 | 33 | 63 | 9 | 5 |
| H | 177–217 | 677–717 | 2nd-order failure injection | 41 | 20 | 21 | 9 | 3 |
| H3 | 177–217 | 677–717 | 3rd-order failure injection | 41 | 20 | 21 | 9 | 3 |
| HR | 177–217 | 677–717 | recovery-unhealthy injection | 41 | 20 | 21 | 9 | 3 |
| HA | 177–217 | 677–717 | accounting-mutation failure | 41 | 20 | 21 | 9 | 3 |
| HO | 177–217 | 677–717 | open-order fetch failure | 41 | 20 | 21 | 9 | 3 |
| HL | 177–217 | 677–717 | lifecycle transition failure | 41 | 20 | 21 | 9 | 3 |
| SOAK | 2400–3200 | 700–810 | extended 800-candle soak | 801 | 5 | 796 | 24 | 21 |
| **Total** | | | | **2066** | **285** | **1781** | **130** | **83** |

Block-reason census (all scenarios): `ALLOCATION_BLOCKED` 1213, `LIFECYCLE_BLOCKED:INVALID_TRANSITION` 383, `REGIME_BLOCKED` 162, `PRICE_OUTSIDE_CANDIDATE_RANGE` 46.

Every cycle record carries: timestamp, cycle_id, symbol, plan_id, generation, regime, grid bounds/count/spacing, risk_allowed, blocked_reason, orders_submitted, fills, inventory, equity, realized_pnl, unrealized_pnl, fees, recovery_status, running drawdown (`<scenario>_cycles.json`).

## 4. Metrics

- Total cycles: 2066 · allowed: 285 · blocked: 1781 (block rate 86.2%).
- Plans: 13 activation-eligible zones exercised; reconfiguration triggered in A/B/C/D/E (see anomaly A1).
- Orders submitted: 130 · fills: 83 · duplicate-order attempts: 0 · duplicate-fill attempts: 0.
- Recovery failures (injected): 1 (HR) · rollbacks from injection: 1 per H-variant (6 total), 0 elsewhere.
- Completed grids: **13** (A: 5, E: 5, SOAK: 3) · inventory (one-way) sales: 13 (B: 5, D: 8).
- Total fees (distinct A–H + SOAK): **1.7734827 USDT**.
- Realized PnL (same set): **895.2172306 USDT** — includes zero-cost-basis initial inventory sales (see anomaly A3).
- Max drawdown (running, equity-peak basis): A 3.11 %, B 2.96 %, C 4.34 %, D 4.45 %, E 3.11 %, F 1.80 %, G 1.80 %, H-matrix 1.12 %, **SOAK 7.23 %** (peak 2598.84 → trough 2410.98 USDT).

## 5. Grid economics (net-per-completed-grid)

Post-quant gate recomputed with the single authoritative formula
`profit_model.net_pct_from_prices(buy_price, sell_price, 0.001, 0.001, 0.0005)` on the persisted quantized order prices:

- Completed round-trip grids: 13 — **all ≥ 0.003. Zero violations.**
  - min 0.009509 · P10 0.009514 · P25 0.015571 · median 0.021664 · P75 0.027799 · P90 0.033957 · max 0.033957.
- Every placed (executable) cell re-checked (76 gate entries across all DBs): **0 below 0.003** — the FIX 4A invariant holds on every executable cell the engine ever persisted.
- No failure was averaged away: 0 of 13 completed grids violated the hard minimum.

## 6. Accounting checks

Per scenario DB (final state, `invariant_scan.json`):
- `base_free/base_reserved/quote_free/quote_reserved` ≥ 0: **all 14 DBs PASS**.
- Fees accounted exactly once: `total_fees` monotonically increases only on fills; per-DB fee sums match fill counts (0 mismatches).
- Equity identity `quote_total + base_total × mark` verified against the authoritative close-price mark each cycle (per-cycle `equity`, running peak/trough in §4).
- No negative balances anywhere.

## 7. Inventory checks

- `SUM(executable SELL qty) ≤ available base` enforced by `allocate_grid` (reservations excluded) — re-asserted per cycle; **0 violations**.
- Account-level `quote_reserved`/`base_reserved` equals the sum of per-order reservation remainders (asset-wise, 1e-8 tolerance) in all 14 DBs: **PASS** (`reservation_conservation.ok=true`).
- Fill-quantity conservation: cumulative fills per order ≤ order quantity: **PASS** (0 overflow).

## 8. Risk checks

Out-of-range and regime cycles produced **zero** executable orders. Exact deterministic block reasons recorded (`PRICE_OUTSIDE_CANDIDATE_RANGE`, `REGIME_BLOCKED`, capacity/ALLOCATION blocks). Breakout scenarios C/D: no BUY placed above the effective upper bound, no SELL below the lower bound; pre-existing open orders remained recoverable and filled deterministically.

## 9. Recovery / integrity checks (end of every scenario)

All 14 scenario DBs: `recover_paper_state` = **HEALTHY**. No orphan reservations, no orphan fills, no duplicate fill IDs, no duplicate client-order IDs, no terminal non-zero reservations, no accounting-event mismatches, no impossible states.

## 10. Failure injection (scenario H family)

Each variant fired exactly one injected failure at candle 177; the atomically-rolled-back cycle left **zero** cycle-owned mutations, and the remaining 40 cycles ran clean (rollback → healthy → retry proven end-to-end):

| Variant | Injected failure | Rolled back | Partial state |
|---|---|---|---|
| H | 2nd `submit()` raises | 1 | none (0 orders/reservations/lifecycle from that cycle) |
| H3 | 3rd `submit()` raises | 1 | none |
| HR | `reconcile` → unhealthy | 1 | none (5A: in-cycle recovery failure aborts commit) |
| HA | accounting mutation fails | 1 | none |
| HO | open-order fetch fails | 1 | none (hard veto, zero submit) |
| HL | lifecycle transition raises | 1 | none (lifecycle hard veto, zero submit) |

## 11. Restart (scenario G)

Session re-attached to the same DBs at candles 177/185/201/225/249/272 spanning idle → open orders → filled → blocked states. All 6 restarts: **OK** (reconcile healthy on init, no duplicate generation, no duplicate orders/fills, accounting exactly-once).

## 12. Anomalies / findings

**A1 — MEDIUM (liveness, not safety): `RECONFIGURATION_PENDING` stalls.**
When the planner returns `RECONFIGURATION_REQUIRED` while a plan is active, the lifecycle moves to `RECONFIGURATION_PENDING` and records a pending candidate. Subsequent `KEEP_CURRENT_PLAN` cycles are rejected by the transition table (`LIFECYCLE_BLOCKED:INVALID_TRANSITION`, 383 cycles ≈ 18 % of blocked cycles). `validate_pending_reconfiguration` / `finalize_reconfiguration` have **no production caller**, so the pending reconfiguration is never completed by the engine itself. The grid recovers only when a later `GRID_ALLOWED` (re-activate) or `GRID_BLOCKED` decision leaves the PENDING state. Behavior is **fail-closed and safe** (no orders on invalid transitions, recovery stays healthy), but it suppresses grid uptime and leaves a pending record that a human or future patch must resolve.

**A2 — INFO: very high block rates on trending data (SOAK 99.4 %, C/D 89–94 %).**
The configured ranges are narrow; when price leaves them, the allocation/capacity gates correctly block (no trading outside the range). This is correct risk behavior, not a defect — but it means extended profitability depends on price staying inside the range.

**A3 — INFO: realized PnL includes zero-cost-basis initial inventory.**
The initial 2 BNB carry a zero average cost, so selling that inventory books the full sale price as "PnL". The fair strategy measure is net/grid (§5) plus fees; the 895 USDT aggregate is not trading profit.

**A4 — INFO: external partial-fill scaling breaks the atomic invariant (by design).**
Scaling a fill quantity externally (seam `partial_fill_frac`) desynchronizes per-order reservations from account totals; the engine's 5A hardening correctly rejects those cycles (`QUOTE_RESERVED_MISMATCH` → rollback) instead of committing an inconsistent state. Native partial fills (engine-computed) are covered by the canonical `test_paper_fill` / `test_paper_accounting` / `test_recovery` suites; scenario F therefore runs clean full-fill accounting.

## 13. Verdicts

| Dimension | Verdict |
|---|---|
| **ENGINE SAFETY** | **PASS** — no execution bypass, no partial state, fail-closed on every injection; 14/14 scenario DBs end recovery-healthy |
| **EXECUTION CORRECTNESS** | **PASS** — orders/reservations/accounting/fills match the intended model; idempotent restarts; atomic rollbacks; fee + conservation invariants hold |
| **GRID ECONOMICS** | **PASS** — every executable cell and every completed round-trip clears `net ≥ 0.003` post-quant (0 of 13 violations) |
| **STRATEGY PERFORMANCE** | **OBSERVATION ONLY** — net/grid 0.95 %–3.4 %, 13 round-trips + 13 inventory sales in ~6 weeks on 15m BNBUSDT; high block rates when price leaves the configured range; no profitability guarantee |

### Do not enable live trading. No commits/pushes made.

**Recommended next step (not required for safety):** a small patch giving the production path a reconfiguration-completion step (call `validate_pending_reconfiguration` → `finalize_reconfiguration` when state is `READY_TO_RECONFIGURE`), to close anomaly A1 before Binance Testnet deployment. Until then, the engine is safe to validate further in paper; a wedged PENDING state is recoverable but degrades grid uptime.
