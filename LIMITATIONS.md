# adaptive-grid — Limitations & Final Audit (Roadmap H)

## Audit result

| Check | Result |
|-------|--------|
|| Full test suite (`pytest -q`) | **1136 passed, 0 failed** (incl. 22 dedicated 15m lower-boundary-kill tests + 19 fetch_klines safety tests + 2 equity finiteness tests) |
| Hardcoded secrets in tracked files | none |
| `.env` tracked | no |
| `.venv` / DB / logs / caches tracked | no |
| Live-trading enablement in production code | none |
| `dry_run` default | `true` (config + `main()` raises if `false`) |
| `allow_live_execution` default | `false` |
| Grid invariants (0.30% min net / 0.60% step / 2% drawdown kill) | enforced in `config_loader.py` + `grid_engine.py` + `risk_engine.py` |
| Range-break kill (fail-closed) | present (`risk_engine.range_break_kill`, `main.py`) |
| 15m candle-close lower-boundary kill | present (`risk_engine.lower_boundary_15m_kill`, wired into `main()` risk decision + kill latch; config `risk.stop_if_below_lower_pct = 0.02` required & validated) |
| Kill-state prevents new orders + survives restart | present (`cancel_controller` + `main()` restart gate) |
| Withdrawal permission | never required |
| Futures / margin / leverage / shorting / martingale | not present |

## Hard invariants (verified, not weakened)

- Binance **SPOT** only; no futures, margin, leverage, shorting, martingale,
  or aggressive averaging.
- DRY_RUN is the default and is required by `main()` (raises otherwise).
- Live execution is disabled (`allow_live_execution=false`) and there is no
  order-placement path outside the risk-gated paper cycle.
- Minimum net profit per completed grid: 0.30% (`0.003`), enforced.
- Gross grid step: 0.60% (`0.006`).
- Conservative fee/slippage calculation (fees summed, discount omitted).
- Orders never placed outside LOWER_PRICE / UPPER_PRICE.
- Range-break kill is fail-closed.
- Equity drawdown kill switch remains 2%.
- 15m lower-boundary stop is intact.
- 15m candle-close lower-boundary kill (dedicated, implemented):
  `risk_engine.lower_boundary_15m_kill` — kills when the latest CLOSED 15m
  candle close satisfies `close <= LOWER_PRICE * (1 - stop_if_below_lower_pct)`
  (Decimal arithmetic; default `stop_if_below_lower_pct = 0.02`, i.e. a 2%
  stop band below the lower price).  Uses closed candles only (the currently
  forming candle is dropped by `fetch_klines(drop_incomplete=True)`; the
  ticker is never a substitute).  Fail-closed: `LOWER_BOUNDARY_STOP_CONFIG_INVALID`
  and `LOWER_BOUNDARY_STOP_DATA_UNAVAILABLE` both veto new orders;
  `LOWER_BOUNDARY_STOP_15M` additionally latches the persisted kill state and
  invokes the cancel-on-kill controller.  Independent of — and in addition
  to — the current-price range-break kill (`range_break_buffer_pct`
  unchanged).  The config field `risk.stop_if_below_lower_pct` is required by
  `config_loader.validate_config` (finite Decimal strictly in (0,1)); there
  is no hidden fallback default.  See `tests/test_15m_lower_boundary_kill.py`.
- `fetch_klines` validates the latest CLOSED candle OHLCV BEFORE the
  `dropna()` pass (fail-closed `MarketDataError` for NaN/Inf/non-positive/
  non-numeric on the critical closed row; older/middle rows may still be
  dropped).  See `tests/test_fetch_klines_safety.py`.
- `build_account_risk_state` validates `current_equity.is_finite()`
  (NaN/Infinity equity fails closed with `AccountValidationError`) before
  computing drawdown/inventory percentages.  See
  `tests/test_market_data.py::test_account_risk_rejects_nan_equity`.
- Risk Engine has veto authority over every order.
- Kill state prevents new orders and survives restart.
- No secrets committed, printed, or hardcoded; no withdrawal permission.

## Known limitations (documented, not defects)

1. **Live trading is not implemented and must not be enabled.** This is a
   safety invariant, not an unfinished task. Enabling it requires explicit
   human authorization and a separate, well-tested implementation.
2. **Round 7: the testnet order path is implemented and verified — the
   live-execution wiring is not.** `testnet_orders.py` provides a
   double-gated (`TESTNET_ORDERS_ENABLED` + validated testnet-only config)
   LIMIT_MAKER placement / cancel client and a concrete §5 cancel executor
   for the Round 6A `RestReconciler` seam, verified end-to-end on Binance
   Spot Testnet (`scripts/testnet_order_path_check.py`, all checks PASS).
   The `main()` production cycle remains paper-only by design (it raises
   when `dry_run=false`); wiring real orders into the trading cycle is a
   future, separately-authorized task. There is still no user-data
   websocket; event reconciliation against a live stream remains a seam.
3. **`open_orders` reconciliation is advisory for the paper path.**
   `open_orders_available_gate` blocks the plan whenever open-order state
   cannot be VERIFIED (UNKNOWN status). The F-H2 cancel path reconciles
   LOCAL paper-order + reservation state.  The Round 7/8 exchange-side
   reconciler + cancel executor are wired into the bounded TESTNET cycle
   harness (`testnet_cycle.py`), which reconciles every order against the
   exchange and proves zero open orders at cleanup; the paper kill path is
   unchanged.
4. **The kill-state release and reference-reset commands are paper-only** and
   refuse to run when the config is not explicitly `dry_run=true` with
   `allow_live_execution=false`.  The cycle harness has its OWN persistent
   kill latch (`cycle_kill_state` in the cycle ledger; identical semantics —
   2% drawdown, range-break, 15m lower-boundary — via the same risk_engine
   gates).  There is deliberately NO automatic reset and currently NO
   dedicated release command for the cycle latch: releasing it requires an
   explicit, auditable operator action on the ledger DB.  A release command
   mirroring `scripts/release_kill_state.py` (reason + actor + audit row) is
   the obvious future increment.
5. **No guaranteed profitability.** Grid trading has real market risk
   (trend, extreme volatility, slippage, fee changes, partial fills, API
   failures, inventory stuck). No formula in this repository removes market
   risk.
6. **Testnet ≠ live.** Testnet balances and results are not a guarantee of
   live behavior; testnet may reset.
7. **Partial-fill handling on the testnet order path is status-level only.**
   `PARTIALLY_FILLED` is a known, validated status in the resolve/reconcile
   paths (unit-tested); a live-forced partial fill is not deterministically
   reproducible, so fill-event accounting remains the paper engine's
   responsibility until the separately-authorized execution wiring exists.
8. **Binance Spot reuses a clientOrderId after its order is canceled**
   (uniqueness holds only among open orders — verified live, code -2010
   rejection while open).  Duplicate prevention must therefore test against
   the open state, as `scripts/testnet_order_path_check.py` does.

## Blocked decisions (require human authorization — NOT done)

- Enabling live trading or a real exchange event stream.
- Loosening any risk parameter.
- Adding withdrawal permission.

## Definition-of-done status

Implementation, tests, full suite, diff review, secret check, and
documentation are complete for every milestone through Roadmap E. Roadmap H
(audit) is this document plus the full-suite result above. The safe
roadmap is complete: no further work can proceed without a human decision
to authorize live-facing capability.
