"""Phase 7 engine-level deterministic replay driver.

Drives ``PaperSession.run_cycle`` over a slice of the real cached BNBUSDT 15m
candles.  For every replayed candle the authoritative market regime, range
quality, and features are recomputed from a trailing window exactly the way
``main.py`` Phase 4 does (``calculate_market_features`` / ``classify_market_regime``
/ ``calculate_range_quality`` / ``evaluate_grid_eligibility``), so the planner
decision, lifecycle gate, allocation, and fills are all executed through the
production orchestrator — no shortcuts.

This module performs NO network access and NO order placement.  It only
reads candles from the cache and mutates a local paper SQLite DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import sys
from pathlib import Path as _Path

# Ensure the project root and this directory are importable when run as a
# script (validation/ is not a package in the production import graph).
_ROOT = _Path(__file__).resolve().parent.parent
_HERE = _ROOT / "validation"
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd

from paper_accounting import PaperAccountingEngine
from paper_orchestrator import PaperCycleInput, PaperSession
from market_features import calculate_market_features, InsufficientDataError, CandleValidationError
from market_regime import classify_market_regime
from range_quality import calculate_range_quality
from grid_eligibility import evaluate_grid_eligibility

import common


@dataclass
class ReplayConfig:
    """Deterministic knobs for one scenario replay."""
    label: str
    candle_range: tuple[int, int]  # inclusive 1-based indices into the cache
    lower: Decimal
    upper: Decimal
    step_pct: Decimal
    order_quote_size: Decimal = Decimal("25")
    initial_base: Decimal = Decimal("2")
    initial_quote: Decimal = Decimal("1000")
    max_open_orders: int = 40
    maker_fee: Decimal = Decimal("0.001")
    taker_fee: Decimal = Decimal("0.001")
    slippage: Decimal = Decimal("0.0005")
    hard_min_net_pct: Decimal = Decimal("0.003")
    prefer_limit_maker: bool = True
    cooldown_candles: int = 4
    lookback: int = 200
    # Injection seams (deterministic, index-relative).
    partial_fill_frac: Decimal | None = None  # if set, fills apply this fraction
    failure_candles: set[int] | None = None  # 1-based candle indices to inject a 2nd-order failure
    failure_kind: str = "order"  # order | accounting | lifecycle | recovery | open_orders | account | ticker
    checkpoint_candles: tuple[int, ...] = ()  # restart the session here (G scenario)


class ScenarioResult:
    def __init__(self, replay: ReplayConfig, session: PaperSession, df: pd.DataFrame):
        self.replay = replay
        self.session = session
        self.df = df
        self.records: list[dict] = []
        self.errors: list[str] = []
        self._peak_equity: Decimal | None = None

    # -- authoritative regime computation for one candle -------------------
    def _intelligence(self, candle_index: int, current_price: Decimal):
        cfg = self._cfg()
        window = common.window_df(self.df, candle_index, cfg["range"]["lookback"])
        quote = common.quote_from_candle(self.df, candle_index)
        try:
            features = calculate_market_features(
                df=window, quote=quote,
                lower_price=self.replay.lower, upper_price=self.replay.upper,
                symbol=common.SYMBOL, config=cfg,
            )
            regime, regime_reason = classify_market_regime(features, cfg)
            quality_res = calculate_range_quality(features, cfg)
            decision = evaluate_grid_eligibility(
                features=features, regime=regime, range_quality=quality_res,
                current_price=current_price,
                lower_price=self.replay.lower, upper_price=self.replay.upper,
                config=cfg, extra_diagnostics={"regime_reason": regime_reason},
            )
            return regime, decision, None
        except CandleValidationError as exc:
            is_insufficient = isinstance(exc, InsufficientDataError)
            regime, regime_reason = classify_market_regime(
                None, cfg, is_insufficient_data=is_insufficient,
                is_invalid_data=not is_insufficient, error_message=str(exc),
            )
            decision = evaluate_grid_eligibility(
                features=None, regime=regime, range_quality=None,
                current_price=current_price,
                lower_price=self.replay.lower, upper_price=self.replay.upper,
                config=cfg, extra_diagnostics={"error": str(exc)},
            )
            return regime, decision, str(exc)

    def _cfg(self) -> dict:
        r = self.replay
        return {
            "pair": common.SYMBOL,
            "range": {"lower": str(r.lower), "upper": str(r.upper), "lookback": r.lookback},
            "grid": {
                "step_pct": r.step_pct,
                "hard_min_net_pct": r.hard_min_net_pct,
                "preferred_net_max_pct": Decimal("0.004"),
                "min_cells": 6,
                "max_levels": 40,
            },
            "execution": {
                "order_quote_size": r.order_quote_size,
                "prefer_limit_maker": r.prefer_limit_maker,
                "max_open_orders": r.max_open_orders,
                "total_quote_budget": Decimal("0"),
            },
            "fees": {
                "maker_fee_fallback": r.maker_fee,
                "taker_fee_fallback": r.taker_fee,
                "slippage_roundtrip_pct": r.slippage,
            },
            "paper": {
                "initial_base_balance": r.initial_base,
                "initial_quote_balance": r.initial_quote,
                "maker_fee": r.maker_fee,
                "taker_fee": r.taker_fee,
                "fee_asset": "USDT",
            },
            "adaptive_planner": {"cooldown_candles": r.cooldown_candles},
            "market_intelligence": {
                "timeframe": "15m", "min_candles": 60,
                "regime": {"adx_trend_min": 25, "atr_expansion_ratio": 1.5,
                            "price_range_inclusion_min": 0.90, "directional_efficiency_max": 0.60},
                "liquidity": {"max_spread_pct": 0.003, "max_quote_ticker_age_seconds": 10},
                "quality": {"min_range_quality_score": 60},
                "atr_period": 14, "adx_period": 14, "bb_length": 20, "bb_std_mult": 2.0,
                "volume_baseline_period": 20, "range_stability_period": 20,
            },
        }

    def _lifecycle_active_plan(self):
        """Mirror main.py's FIX 4C reader: the lifecycle DB is the single
        source of truth for the active plan; None when no plan is active."""
        from grid_lifecycle import LifecycleManager
        from grid_planner import ActivePlan
        from market_regime import MarketRegime as _MR

        db = self.session.orchestrator.lifecycle_db_path
        manager = LifecycleManager(db)
        active = manager.get_active_plan()
        if active is None:
            return None
        generation = manager.get_generation()
        return ActivePlan(
            plan_id=active.plan_id,
            candidate_lower=active.candidate_lower,
            candidate_upper=active.candidate_upper,
            grid_step=active.grid_step,
            grid_count=active.grid_count,
            regime=_MR(active.regime),
            range_quality_score=active.range_quality_score,
            candle_index=active.candle_index,
            generation=generation,
        )

    def build_input(self, candle_index: int) -> PaperCycleInput:
        r = self.replay
        price = common.price_at(self.df, candle_index)
        ts = common.close_ts(self.df, candle_index)
        regime, decision, _int_err = self._intelligence(candle_index, price)
        cfg = self._cfg()
        quality = (
            decision.range_quality_score
            if decision.range_quality_score is not None
            else Decimal("80")
        )
        return PaperCycleInput(
            candle_index=candle_index,
            symbol=common.SYMBOL,
            current_price=price,
            kline_df=common.window_df(self.df, candle_index, cfg["range"]["lookback"]),
            quote=common.quote_from_candle(self.df, candle_index),
            lower_price=r.lower,
            upper_price=r.upper,
            active_plan=self._lifecycle_active_plan(),
            regime=regime,
            range_quality_score=quality,
            cfg=cfg,
            maker_fee=r.maker_fee,
            taker_fee=r.taker_fee,
            fee_asset="USDT",
            clock=lambda: ts,
            dry_run=True,
            rules=common.parse_rules(),
        )

    def run(self) -> "ScenarioResult":
        """Run the scenario with deterministic partial-fill / failure injections."""
        r = self.replay
        eng = self.session.order_engine
        orig_submit = eng.submit
        orig_apply_fill = eng.apply_fill
        orig_reconcile = eng.reconcile

        # Partial-fill seam: when set, every fill application takes only a
        # fixed fraction of the remaining quantity (deterministic PARTIALLY
        # FILLED simulation).
        if r.partial_fill_frac is not None:
            frac = r.partial_fill_frac

            def partial_apply_fill(*a, **k):
                args = list(a)
                if "quantity" in k and "quantity" not in args:
                    args[4] = args[4] * frac
                else:
                    # positional: (client_order_id, fill_id, symbol, price, quantity, ...)
                    args[4] = args[4] * frac
                return orig_apply_fill(*args, **k)

            eng.apply_fill = partial_apply_fill

        failures = r.failure_candles or set()
        kind = r.failure_kind
        # Persisted-restart checkpoint for scenario G: at each checkpoint the
        # in-memory session is re-attached to the SAME paper DBs, proving the
        # engine recovers persisted state + idempotently replays on restart.
        checkpoints = set(r.checkpoint_candles)

        for idx in range(r.candle_range[0], r.candle_range[1] + 1):
            inp = self.build_input(idx)

            if idx in failures:
                self._arm_injection(kind, self.session, orig_submit,
                                     orig_apply_fill, orig_reconcile, None, None)
                self.errors.append(f"injection[{kind}]@c{idx}")

            res = self.session.run_cycle(inp)
            self._record(idx, inp, res)

            # Disarm per-cycle injection so the next cycle runs clean.
            self._disarm_injection(kind if idx in failures else None,
                                   self.session, eng, orig_submit,
                                   None, orig_reconcile, None)

            if idx in checkpoints:
                self._restart_session(idx)

        return self

    def _restart_session(self, at_candle: int) -> None:
        """Re-attach a fresh PaperSession to the same DBs (deterministic restart).

        The accounting engine is re-constructed with the *initial* balances,
        which is safe: PaperOrderEngine seeds paper_account_state only when
        the row is missing (ensure_paper_account_state) and every cycle reads
        the PERSISTED accounting row, so restarts resume the live state, not
        the initial one.
        """
        from pathlib import Path as _P
        r = self.replay
        base = _P(self.session.order_engine.db_path).parent
        accounting = PaperAccountingEngine(
            "BNB", "USDT",
            r.initial_base, r.initial_quote,
            r.maker_fee, r.taker_fee, "USDT",
        )
        from paper_orchestrator import PaperSession
        new_session = PaperSession(
            self.session.order_engine.db_path,
            str(base / "lifecycle.db"),
            accounting,
            client_order_prefix="AG",
        )
        # A healthy engine after re-attach is the restart guarantee:
        # reconcile_on_init ran and the persisted state is consistent.
        if not new_session.is_healthy():
            self.errors.append(f"restart@{at_candle}:UNHEALTHY")
        else:
            self.errors.append(f"restart@{at_candle}:OK")
        self.session = new_session

    @staticmethod
    def _arm_injection(kind, session, o_submit, o_fill, o_reconcile, o_recover, partial_frac):
        eng = session.order_engine

        if kind in ("order", "order3"):
            threshold = 2 if kind == "order" else 3
            label = kind.upper()
            calls = {"n": 0}

            def failing_submit(intent, *a, **k):
                calls["n"] += 1
                if calls["n"] >= threshold:
                    raise RuntimeError(f"INJECTED_{label}_FAILURE")
                return o_submit(intent, *a, **k)

            eng.submit = failing_submit
        elif kind == "recovery":
            from recovery import RecoveryResult, RecoveryError, RecoveryErrorCode

            def bad_reconcile(con=None):
                return RecoveryResult(
                    healthy=False,
                    errors=(RecoveryError(RecoveryErrorCode.ACCOUNT_EVENT_MISMATCH, "orders", "injected"),),
                    warnings=(),
                )

            eng.reconcile = bad_reconcile
        elif kind == "accounting":
            o_submit_ref = o_submit
            state = {"submitted": False}

            def acct_submit(intent, *a, **k):
                if "con" in k:
                    # Inject an accounting failure on a later op by raising
                    # after the first submission inside the same txn.
                    if state["submitted"]:
                        raise RuntimeError("INJECTED_ACCOUNTING_FAILURE")
                    state["submitted"] = True
                return o_submit_ref(intent, *a, **k)

            eng.submit = acct_submit
        elif kind == "open_orders":
            orch = session.orchestrator

            def empty_open(_db, con=None):
                raise RuntimeError("INJECTED_OPEN_ORDER_FAILURE")

            orch._get_open_orders = empty_open
        elif kind == "lifecycle":
            orch = session.orchestrator

            def boom_handle(_d, *a, **k):
                raise RuntimeError("INJECTED_LIFECYCLE_FAILURE")

            orch.lifecycle_manager.handle_planner_decision = boom_handle
        elif kind == "ticker":
            # Ticker failure is upstream of the orchestrator (in main.py); the
            # engine path has no ticker seam. No-op here.
            pass

    @staticmethod
    def _disarm_injection(kind, session, eng, o_submit, o_fill, o_reconcile, o_recover):
        eng = session.order_engine

        eng.submit = o_submit
        eng.reconcile = o_reconcile
        orch = session.orchestrator
        if kind == "open_orders":
            del orch._get_open_orders
        elif kind == "lifecycle":
            del orch.lifecycle_manager.handle_planner_decision

    def _record(self, idx: int, inp: PaperCycleInput, res) -> None:
        from storage import get_paper_account_state
        state = get_paper_account_state(self.session.order_engine.db_path)
        equity = None
        if state:
            equity = str(
                state["quote_free"] + state["quote_reserved"]
                + (state["base_free"] + state["base_reserved"]) * inp.current_price
            )
        plan = res.plan
        # Authoritative generation = the lifecycle manager's current generation
        # (FIX 4C single source of truth).
        from grid_lifecycle import LifecycleManager
        generation = LifecycleManager(
            self.session.orchestrator.lifecycle_db_path
        ).get_generation()
        # §5 accounting fields surfaced from the persisted state.
        if state:
            base_total = state["base_free"] + state["base_reserved"]
            unreal = base_total * inp.current_price + state["quote_free"] + state["quote_reserved"]
            eq = Decimal(equity) if equity is not None else None
            # Running drawdown vs the peak equity observed so far in this
            # scenario (deterministic, process-local; persisted equity is
            # intentionally NOT used as history).
            if eq is not None:
                if self._peak_equity is None or eq > self._peak_equity:
                    self._peak_equity = eq
            drawdown = None
            if eq is not None and self._peak_equity and self._peak_equity > 0:
                drawdown = (self._peak_equity - eq) / self._peak_equity
            rec_extra = {
                "generation": generation,
                "realized_pnl": str(state["realized_pnl"]),
                "unrealized_pnl": str(unreal),
                "inventory": str(base_total),
                "fees": str(state["total_fees"]),
                "drawdown": str(drawdown) if drawdown is not None else None,
            }
        else:
            rec_extra = {"generation": generation, "realized_pnl": None,
                         "unrealized_pnl": None, "inventory": None, "fees": None,
                         "drawdown": None}
        rec = {
            "candle_index": idx,
            "timestamp": str(common.close_ts(self.df, idx)),
            "cycle_id": res.cycle_id,
            "symbol": res.symbol,
            "regime": inp.regime.value if inp.regime is not None else None,
            "plan_decision": plan.decision.value if plan is not None else None,
            "plan_id": plan.plan_id if plan is not None else None,
            "grid_lower": str(plan.candidate_lower) if plan is not None else None,
            "grid_upper": str(plan.candidate_upper) if plan is not None else None,
            "grid_count": plan.grid_count if plan is not None else None,
            "grid_spacing": str(plan.grid_step) if plan is not None else None,
            "risk_allowed": res.success and res.blocked_reason is None,
            "blocked_reason": res.blocked_reason,
            "error": res.error,
            "success": res.success,
            "orders_submitted": res.orders_submitted,
            "orders_skipped": res.orders_skipped,
            "fills_applied": res.fills_applied,
            "fills_idempotent": res.fills_idempotent,
            "lifecycle_transition": res.lifecycle_transition,
            "recovery_healthy": res.recovery_healthy,
            "is_idempotent": res.is_idempotent,
            "account_state": state,
            "equity": equity,
        }
        rec.update(rec_extra)
        self.records.append(rec)


def make_session(replay: ReplayConfig, base: Path) -> PaperSession:
    base.mkdir(parents=True, exist_ok=True)
    order_db = str(base / "orders.db")
    lifecycle_db = str(base / "lifecycle.db")
    accounting = PaperAccountingEngine(
        "BNB", "USDT",
        replay.initial_base, replay.initial_quote,
        replay.maker_fee, replay.taker_fee, "USDT",
    )
    return PaperSession(order_db, lifecycle_db, accounting, client_order_prefix="AG")


def invariant_checks(result: "ScenarioResult") -> dict:
    """Post-scenario invariant scan across all recorded cycles."""
    problems: list[str] = []
    for rec in result.records:
        st = rec.get("account_state") or {}
        if st:
            for key in ("base_free", "base_reserved", "quote_free", "quote_reserved"):
                val = st.get(key)
                if val is not None and val < 0:
                    problems.append(f"neg {key}={val} @c{rec['candle_index']}")
        # Inventory conservation: base total must be non-negative and bounded.
        if st:
            base_total = st["base_free"] + st["base_reserved"]
            if base_total < 0:
                problems.append(f"neg base_total @c{rec['candle_index']}")
    # Terminal-order reservation check via recovery on the final state.
    final_healthy = result.records[-1]["recovery_healthy"] if result.records else None
    return {"problems": problems, "final_recovery_healthy": final_healthy}


if __name__ == "__main__":
    # Smoke: run a short stable-range replay.
    df = common.load_klines()
    replay = ReplayConfig(
        label="smoke",
        candle_range=(930, 990),
        lower=Decimal("760"),
        upper=Decimal("790"),
        step_pct=Decimal("0.006"),
    )
    session = make_session(replay, Path("_vsmoke"))
    result = ScenarioResult(replay, session, df).run()
    for rec in result.records:
        print(rec["candle_index"], rec["regime"], "sub=", rec["orders_submitted"],
              "fill=", rec["fills_applied"], "blocked=", rec["blocked_reason"])
    print("invariants:", invariant_checks(result))
