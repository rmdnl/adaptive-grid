# FINAL INTEGRATION AUDIT — ROBOT GRID SPOT CRYPTO

**Date:** 2026-09-30
**Mode:** Audit-first (no code modified, no commit, no push)
**Scope:** Phase 1–6A + Patch 1, 2A, 2B, 2C, 2D, 3
**Baseline at audit time:** 949 tests passed, 0 failed, 0 skipped; nothing committed

---

## Verdict

# NEEDS FIX

Three **HIGH** findings on real order-submission paths, all missed by unit tests:

| ID | Severity | One-line summary |
|----|----------|------------------|
| F-1 | HIGH | `main.py` (the `__main__` production entry) submits paper orders **without** the lifecycle-integrity / generation gate, inventory-allocation gate, or cycle transaction that the orchestrator path enforces |
| F-2 | HIGH | `main.py` `_submit_paper_orders` is a non-transactional per-order loop — a reservation failure on a later order leaves earlier orders committed mid-plan (the exact partial-cycle state Patch 2D eliminates on the orchestrator path) |
| F-3 | HIGH | The orchestrator path can emit executable intents whose **post-quantization** net profit is below the 0.003 hard minimum — the profit gate runs only on the pre-quantized step; no quantized recheck exists in `paper_orchestrator.py` / `inventory_model.py` allocation |

Per the required verdict logic, any HIGH finding ⇒ **NEEDS FIX**.

---

## 1. Scope reviewed

All 20 focus areas across: `main.py` (629 L), `order_engine.py`, `storage.py`, `paper_orchestrator.py` (1683 L), `grid_lifecycle.py` (1614 L), `grid_planner.py`, `inventory_model.py` (858 L), `symbol_rules.py`, `grid_engine.py`, `profit_model.py`, `fee_model.py`, `risk_engine.py`, `market_data.py`, `binance_testnet.py`, `recovery.py`, `paper_accounting.py`, `config_loader.py`, `market_regime.py` / `grid_eligibility.py`, plus repo-wide security/config sweeps. No code modified.

## 2. Actual execution path

**Two front-ends exist; only `main.py` is the wired production entry point** (`if __name__ == "__main__"`). The orchestrator is exercised by `paper_validation.py` (validation harness) and tests — no production runner wires `binance_testnet` → `PaperSession`.

**Path A — main.py (production):**
`make_client(mode)` → `fetch_symbol_info` → `fetch_klines(drop_incomplete=True)` → `fetch_ticker_price` + `is_ticker_fresh` (fail-closed: aborts if None, main.py:184) → `fetch_book_ticker` → `fetch_account_snapshot` → `fetch_open_orders` → `build_account_risk_state` → market intelligence (`classify_market_regime` / `evaluate_grid_eligibility`) → `evaluate_adaptive_grid_plan` (decision only) → `build_geometric_grid` + `validate_grid_profit` + `validate_quantized_order_plan` → combined risk gates → `_submit_paper_orders` → `engine.submit(...)` (standalone, per-order, no cycle transaction).

**Path B — orchestrator (validation harness + tests):**
`run_cycle` → pre-transaction planner decision → single `cycle_transaction(order_db, lifecycle_db)` → `_run_cycle_txn` (lifecycle mutation → lifecycle-integrity gate → inventory allocation → capacity gate → `submit` → fills → `reconcile(con)` → cycle record) → commit/rollback as one unit.

## 3. Order-submission call graph

`PaperOrderEngine.submit` (order_engine.py:392) has **exactly two production call sites**:

1. `main.py:116` inside `_submit_paper_orders` — gated by `combined.allowed and plan_validation is not None`.
2. `paper_orchestrator.py:~1100` inside the `_run_cycle_txn` submit loop — gated by `should_submit` + lifecycle-integrity + capacity gate.

`submit` itself re-validates: recovery healthy (`_ensure_healthy`), risk decision allowed, price within `[lower, effective_upper]`, `client_order_id` recomputed and matched, duplicate rejection, accounting reservation solvency. Every "must-block" condition in §B is honored on **both** paths — with two exceptions covered by F-1/F-2 (Path A) and F-3 (Path B post-quantization profit).

## 4. Safety gates verified (per path)

| Gate | Path A (main.py) | Path B (orchestrator) |
|------|------------------|------------------------|
| Ticker unavailable → abort | YES (`raise`, main.py:184) | caller-supplied pre-txn; fail-closed |
| Ticker stale | `is_ticker_fresh` (10 s / cfg window) | `validate_market_freshness` |
| Account unavailable | `account_state_gate(False)` → combined | accounting read via `con`; reservation solvency |
| Open-orders unavailable | `open_orders_available_gate(False)` | `_get_open_orders(con)`; adapter never returns `[]` on error |
| Regime / range blocked | `market_gate` + RANGE + MARKET_INTELLIGENCE reasons | planner `plan_decision` + risk |
| Profit < 0.003 (pre-quant) | `profit_gate` + `validate_grid_profit` | planner `_validate_spacing_profit` |
| Profit < 0.003 (**post-quant**) | `validate_quantized_order_plan` (symbol_rules.py:133) | **NOT enforced** ← F-3 |
| Symbol filter | `validate_quantized_order_plan` | `allocate_grid` symbol-rule checks (no profit recheck) |
| Price in range | `strict_order_price_gate` | submit range check |
| Drawdown kill / range-break / inventory | risk gates | `cycle_input.risk_decision` (caller-computed) |
| Lifecycle invalid / generation mismatch | **absent** (F-1) | integrity gate + `STALE_GENERATION` |
| Inventory allocation | `inventory_gate` | `allocate_grid` + reservation solvency |
| Open-order capacity | `open_orders_gate` + plan limit | capacity gate + submit dup check |
| Recovery unhealthy | engine-init reconcile → `_ensure_healthy` | `reconcile(con)` + submit gate |
| Transaction unavailable | n/a (standalone; F-2) | `cycle_transaction` rollback |

## 5. Exchange-truth verification — PASS

No local-state substitution when the exchange is unavailable.

- **Ticker fails** → `main.py:184` raises; orchestrator caller must supply it. No candle-close fallback (`current_price = ticker.price`, main.py:216).
- **Account fails** → `account_state_gate(False)`; `prepare_reservation` raises `InsufficientPaperFunds` — never zero balances.
- **Open-orders fail** → `open_orders_available_gate(False)`; adapter maps failures to typed exceptions; `open_orders()` never returns `[]` on error (covered by `test_open_orders_api_failure_not_empty_list`).
- **Exchange unavailable** → `combined.allowed = False` → **no submission**.

## 6. Market-data verification — PASS

Closed-candle only: `fetch_klines(drop_incomplete=True)` + `latest_valid_row`; `close_time <= now` drops the incomplete current candle. `is_ticker_fresh` / `is_quote_fresh` enforce the 10 s (or cfg) window with tz-aware `fetched_at`. Stale data cannot silently pass as fresh.

## 7. Range/grid verification — PASS

`build_geometric_grid` raises on `lower <= 0 or upper <= lower or step <= 0`, enforces `min_cells`/`max_levels`. `validate_quantized_order_plan` floors prices to tick (ROUND_DOWN), checks sell > buy after quantization (else `SYMBOL_RULE_BLOCK`), notional min/max, percent-price. Submit re-checks `lower <= price <= effective_upper`. No order escapes `[LOWER_PRICE, effective_upper]`; quantization cannot push an order out of range.

## 8. Profit-threshold verification — **FINDING F-3 (HIGH)**

`MIN_NET_PROFIT_PER_GRID = 0.003` is enforced on the **pre-quantization** step in both paths, and re-checked on the **quantized** plan in Path A (`validate_quantized_order_plan`). Path B (orchestrator) has **zero** `hard_min`/`net_pct` references: the planner gates on pre-quant `net_pct_from_step`, and allocation/intent generation (`_compute_allocation`, `filter_actionable_cells`, `_generate_order_intents`) perform no post-tick/step-quantization net-profit recheck. A grid whose quantized spread erodes below 0.003 can therefore emit executable intents on Path B. Direct violation of the hard profit invariant.

## 9. Risk-engine verification — PASS

All `risk_engine.py` gates are pure-Decimal and fail-closed (return `allowed=False` with deterministic reasons; `combine()` ANDs reasons). No float boundaries (all `Decimal(str(...))`), no NaN/Infinity paths (finite rejection everywhere since Patch 3), no gate that downgrades a veto to a warning. Drawdown kill, lower-bound stop, range-break buffer, open-order capacity, account/ticker availability all block. No risk failure can become ALLOWED.

## 10. Lifecycle/generation verification — PASS (one LOW)

- `handle_planner_decision` → `activate_plan` / `_transition_to_pending_reconfig` with single-use candidate guard + UNIQUE index; corrupt/duplicate/stale generation ⇒ `LifecycleError` ⇒ hard veto (Patch 2C) — zero intents generated, `submit` never called.
- Orchestrator integrity gate cross-checks active-plan vs manager generation, pending-candidate staleness, plan/pair consistency, before order generation.
- `allocate_grid` blocks on `STALE_GENERATION`; restart preserves generation (DB-persisted).
- `make_client_order_id` is generation-marked and zero-padded → gen1/gen2 cannot collide; legacy IDs read as gen0 and are never reused; `submit` recomputes and rejects mismatched IDs.
- **LOW (F-6):** `grid_lifecycle._pending_row` (line 1404) queries `pending_reconfigs` unprefixed; method is dead (no callers) — latent schema bug only under a 2-file ATTACH cycle.

## 11. Inventory verification — PASS on Path B, MED on Path A

- **Path B (PASS):** accounting state read through the cycle connection; `InventorySnapshot` → `allocate_grid` blocks on `INSUFFICIENT_QUOTE` / `INSUFFICIENT_BASE`; only actionable cells produce intents; reserved inventory counted; fee/slippage buffer respected.
- **Path A (F-5, MED):** SELL cells come from the full geometric plan, not from allocation `available_base`. Bounded by the per-order non-negative reservation check (no negative balances possible), so a robustness gap rather than a capital-loss path.

## 12. Accounting/fill verification — PASS

- BUY: quote decreases, base increases, fee applied; SELL: base decreases, quote increases, fee applied. Fee asset = BASE and QUOTE both handled in `paper_accounting.prepare_fill_accounting`.
- Average cost updates on BUY only; realized PnL on SELL.
- Reservation released on CANCELED/REJECTED via `prepare_release`.
- Fill deduplication by `trade_id` → replayed fills are idempotent (no double accounting); `save_paper_fill` early-returns on existing fill.
- Negative balances impossible: `prepare_reservation` raises `InsufficientPaperFunds` before any mutation. No orphan-reservation path found (terminal transitions always release).

## 13. Recovery verification — PASS

- `reconcile_on_init` defaults True at engine construction on both paths; `accounting=None` / `reconcile_on_init=False` are not used by any production entry (sweep: zero hits).
- `_ensure_healthy` gates **every** `submit`.
- Orchestrator runs `reconcile(con)` **inside** the cycle transaction and records RECOVERY_OK/RECOVERY_FAILED; an unhealthy post-mutation state fails the cycle and rolls back.
- `recover_paper_state` validates: no orphan reservation, no orphan fill, no terminal order with reservation, no accounting mismatch, no impossible balances, no lifecycle inconsistency. Post-rollback recovery reports the pre-cycle (healthy) state (Patch 2D tests).

## 14. Cycle atomicity verification — PASS (orchestrator) / FAIL (main.py, F-2)

- **Path B:** one transaction owner (`PaperOrchestrator.run_cycle` → `cycle_transaction`); all lower-level components participate via `con` and never commit independently (`owns = con is None` guards in storage writes; `owns_commit = not joined` in `grid_lifecycle`). No hidden `sqlite.connect()`/`BEGIN`/`COMMIT` inside the cycle. One logical cycle = one commit or one rollback (empirically verified cross-file, including attached WAL database).
- **Path A:** per-order standalone `engine.submit` — no shared transaction, no cycle record, no rollback. F-2.

## 15. SQLite two-database transaction verification — PASS

- `cycle_transaction(order_db, lifecycle_db)`: when the two are different files, the lifecycle DB is `ATTACH`ed under schema alias `lifecycle` **before** `BEGIN IMMEDIATE`; all lifecycle mutations target `lifecycle.<table>` via the prefix-aware `_table()` helper.
- Single shared connection for both files; rollback atomically reverts both (verified: injection mid-transaction leaves zero surviving rows in either file).
- `DETACH` runs after commit/rollback; commit/rollback/DETACH/close error handling localized in one context manager.
- Audit of direct-DB writes: confined to `storage.py`, `grid_lifecycle.py` (prefix-aware), and `paper_orchestrator` helpers — none bypass `con` during a cycle.

## 16. Idempotency/restart verification — PASS

- Same candle ⇒ deterministic `cycle_id`; same generation ⇒ same `client_order_id`; same (order, price, remaining) ⇒ same `fill_id` (SHA-256-12 over order/fill semantics, deliberately excluding `cycle_id`).
- Successful cycle ⇒ record persisted (`INSERT OR IGNORE` on `paper_orch_cycles`) ⇒ replay returns cached result, `is_idempotent=True`, zero duplicate mutable state.
- Rolled-back cycle ⇒ **no record persisted** ⇒ retry of the same logical cycle executes cleanly against the pre-cycle state (Patch 2D Test 6).

## 17. Binance testnet/live-boundary verification — PASS

- Adapter base URL must exactly equal `https://testnet.binance.vision`; production bases in `_REJECTED_PRODUCTION_BASES` are explicitly rejected. `environment` must be `testnet`, `dry_run` must be true, `allow_live_execution` must be false.
- `market_data._base_path` / `make_client` fail closed for any non-testnet mode; `SPOT_REST_API_PROD_URL` import removed (Patch 3); no missing/invalid-env → live fallback.
- Adapter exposes **no trading methods** (ping / server time / exchangeInfo / ticker / account / open orders only).
- Repo-wide sweep: all live-endpoint marker hits classified as (a) the rejection blocklist, (b) test fixtures asserting absence, or (c) the harmless `new_order` local variable in `storage.py`. **Zero executable live-trading paths.**

## 18. Secret security — PASS

- 0 hardcoded-credential hits across production files. `.env` absent; `.gitignore` lists `.env`; only `.env.example` with empty values.
- Config `api_key`/`api_secret` are `field(repr=False)` — never in `repr()`/`str()`.
- `market_data.redact_credentials` scrubs all four request-error paths and adapter exception messages (deterministic masking: `abcd...7890`).
- No credentials in logs, exceptions, cycle events, or SQLite.

## 19. Configuration verification — PASS

Strict bool parsing (`true`/`false` only — `1`/`yes` rejected), strict numeric parsing (reject empty/malformed/NaN/±Infinity/negative where impossible), `hard_min_net_pct ≥ 0.003` enforced, drawdown limits / range / budget / fee / slippage validated fail-closed, `binance.max_open_orders` / `max_account_assets` must be positive ints when present. No malformed configuration can accidentally enable live trading.

## 20. Failure-injection results

Covered by tests: submit/rollback matrix (Patch 2D tests 1–6: second-order, accounting, fill, lifecycle, multi-order success, retry-after-rollback), post-rollback recovery health, ticker/account/open-orders failure (no local substitution), lifecycle hard-veto (2C), planner exception → `GRID_BLOCKED`, Decimal NaN/Infinity, duplicate asset, over-cap open-orders, filter contradiction, connectivity fail-closed (any component failure ⇒ overall FAIL), strict env/bool/numeric rejection.

**Gaps (the two things unit tests cannot catch):**
- No test exercises **Path A's non-atomic partial submit** (F-1/F-2).
- No test drives **Path B with tick-quantization that erodes the spread below 0.003** (F-3).

## 21. Cross-module/static findings

- Direct DB writes confined to `storage` + `grid_lifecycle` (prefix-aware) + `paper_orchestrator._ensure_schema/_record_*` (owned, standalone, no in-cycle use). No direct order creation outside `PaperOrderEngine`; no direct accounting mutation outside `paper_accounting`; no duplicated risk/inventory logic bypassing gates.
- No `except: pass`, no silent continuation, no TODO/FIXME safety bypasses, no disabled safety flags, no debug/test bypasses reachable in production. All broad `except Exception` handlers re-raise typed errors or fail closed.
- Dead `_pending_row` helper is unprefixed but has no callers (F-6, latent only).

## 22. Resource/performance findings

Grid bounded by `max_levels`; adapter responses bounded by Patch-3 caps (`max_open_orders` default 100, `max_account_assets` default 1000); connectivity check makes 6 bounded read-only calls; cycle-transaction duration bounded by grid size. No unbounded loops, no repeated reconciliation inside a cycle. No material risk.

---

## Findings table

| ID | Severity | File | Evidence | Impact | Recommendation |
|----|----------|------|----------|--------|----------------|
| F-1 | **HIGH** | `main.py:100–118, 551–581` | `__main__` entry submits paper orders from `plan_validation.cells` with **no** lifecycle-integrity/generation gate and **no** inventory-allocation gate (Patch 2C/5C), unlike `PaperOrchestrator` | The actual production entry lacks the lifecycle hard-veto + inventory gates the orchestrator enforces; a stale/corrupt lifecycle or over-inventory grid can still reach `submit` on Path A | Add the lifecycle-integrity gate + inventory allocation to the main.py submit path, or retire main.py's direct submit in favor of the orchestrator |
| F-2 | **HIGH** | `main.py:107–117` | `_submit_paper_orders` calls standalone `engine.submit` per order with **no cycle transaction and no try/except**; `InsufficientPaperFunds` on a later SELL propagates out of `main()` | Mid-loop reservation failure leaves earlier orders already **committed** (no rollback, no cycle record) → partial committed open-order state; the run crashes mid-plan | Wrap Path-A submission in a transaction/rollback unit, or catch-and-rollback per cycle |
| F-3 | **HIGH** | `paper_orchestrator.py` (no `hard_min`/`net_pct` refs), `inventory_model.py:_compute_allocation`, `grid_planner.py:_validate_spacing_profit` | Planner gates on **pre-quantization** `net_pct_from_step`; allocation/intent generation do **not** re-validate post-tick/step-quantization net profit vs `hard_min_net_pct` (main.py does, via `validate_quantized_order_plan`) | Path B can emit executable intents whose **quantized** spread earns < 0.003, violating the hard profit invariant | Re-validate post-quantization `net_pct_from_prices` per cell against `hard_min` before emitting intents in the orchestrator |
| F-4 | MEDIUM | `main.py:288–320` vs `grid_lifecycle.generation` (DB table) | `plan_generation` persisted in single-DB `bot_state`; orchestrator persists lifecycle generations in the lifecycle `generations` table | If both drive one symbol, generation / `client_order_id` namespaces diverge | Unify the generation source (single authoritative counter) |
| F-5 | MEDIUM | `main.py:_paper_order_intents` | SELL cells come from the full geometric plan, not from allocation `available_base` | Order-set can exceed real available base (capped only by per-order non-negative reservation) | Cap main.py SELL cells by available base, or use allocation |
| F-6 | LOW | `grid_lifecycle.py:1404` | `_pending_row` uses unprefixed `pending_reconfigs` SQL | Latent — method is dead (no callers); would break only if re-enabled under a 2-file ATTACH cycle | Prefix it or delete it |
| F-7 | INFO | `grid_lifecycle.py:514` | `get_transition_history` reads unprefixed table on a standalone connection | Read-only audit, non-cycle, safe | None |
| F-8 | INFO | `market_data.py`, `binance_testnet.py` | Broad `except Exception` handlers all re-raise typed errors (no silent continue) | None | None |
| F-9 | INFO | `main.py` | `cooldown_gate(False)`, `daily_profit_lock(0)` are hardcoded constants | No live cooldown/profit-lock state in main.py (by design, process-local) | Note only |

---

## MEDIUM/LOW/INFO disposition — do these block testnet paper validation?

- **F-4 (MEDIUM):** divergent generation namespaces — blocks only if main.py and the orchestrator drive the *same* symbol concurrently; the paper-validation harness uses one path, so it does **not** block paper validation, but must be resolved before any dual-path deployment.
- **F-5 (MEDIUM):** main.py SELL cells uncapped by available base — bounded by the per-order non-negative reservation check (no negative balances possible), so a robustness gap, not a capital-loss path. Does not block paper validation (harness doesn't use main.py submit).
- **F-6 (LOW):** dead unprefixed `_pending_row` — latent only, no callers. No action needed before paper validation; delete or prefix opportunistically.
- **F-7 / F-8 / F-9 (INFO):** read-only audit helpers, re-raising exception handlers, process-local cooldown constants — no safety impact.

**Blocking finding for paper validation: F-3** — a hard-invariant violation (`MIN_NET_PROFIT_PER_GRID = 0.003`) on the orchestrator path that paper validation would actually exercise. F-1/F-2 block safe *unsupervised* `main.py` runs but not the validation harness itself.

## Recommended fix order

1. **F-3:** re-validate per-cell post-quantization `net_pct_from_prices` against `hard_min_net_pct` before emitting intents in `paper_orchestrator._generate_order_intents` (+ regression test: a grid where tick-quantization erodes the spread below 0.003 ⇒ zero intents).
2. **F-1 / F-2:** either route `main.py` submission through the orchestrator's `cycle_transaction` + lifecycle-integrity + inventory-allocation gates, or retire main.py's direct submit as an orchestrator-only entry.
3. **F-4 / F-5:** unify the generation source and cap main.py SELL cells by available base.
4. **F-6:** delete or prefix `_pending_row`.

## Tests

**949 passed, 0 failed, 0 skipped** (matches reported baseline; 1 unrelated pandas/pyarrow deprecation warning).

---

*No commit, no push, no code modified in this audit pass. Working tree contains only the pre-existing Patch 2A–3 changes.*
