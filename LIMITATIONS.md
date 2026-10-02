# adaptive-grid — Limitations & Final Audit (Roadmap H)

## Audit result

| Check | Result |
|-------|--------|
| Full test suite (`pytest -q`) | **1115 passed, 0 failed** (incl. 22 dedicated 15m lower-boundary-kill tests) |
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
- Risk Engine has veto authority over every order.
- Kill state prevents new orders and survives restart.
- No secrets committed, printed, or hardcoded; no withdrawal permission.

## Known limitations (documented, not defects)

1. **Live trading is not implemented and must not be enabled.** This is a
   safety invariant, not an unfinished task. Enabling it requires explicit
   human authorization and a separate, well-tested implementation.
2. **Roadmap E `RestReconciler` is an abstract seam.** There is no live
   Binance user-data stream and no REST cancel/fetch implementation in this
   repository. The deterministic event model, state machine, and tests are
   the deliverable; a live feed is a future, separately-authorized task. Until
   then, reconciliation reconciles against LOCAL paper state.
3. **`open_orders` reconciliation is advisory.** `open_orders_available_gate`
   blocks the plan whenever open-order state cannot be VERIFIED (UNKNOWN
   status). The F-H2 cancel path reconciles LOCAL paper-order + reservation
   state.
4. **The kill-state release and reference-reset commands are paper-only** and
   refuse to run when the config is not explicitly `dry_run=true` with
   `allow_live_execution=false`.
5. **No guaranteed profitability.** Grid trading has real market risk
   (trend, extreme volatility, slippage, fee changes, partial fills, API
   failures, inventory stuck). No formula in this repository removes market
   risk.
6. **Testnet ≠ live.** Testnet balances and results are not a guarantee of
   live behavior; testnet may reset.

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
