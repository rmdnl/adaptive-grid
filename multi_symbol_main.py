"""Multi-Symbol Adaptive Grid Bot — Main Entry Point (testnet/paper only).

This module is the authoritative multi-symbol orchestrator.  Each symbol runs
its own complete cycle with isolated state (per-symbol SQLite database) and
its own grid configuration.

Architecture
------------
- Per-symbol SQLite database (``data/grid_bot_{SYMBOL}.sqlite3``).
- Per-symbol grid mode (arithmetic for BTC/ETH/BNB, geometric for SOL).
- Shared read-only market-data client (Binance Spot TESTNET only).
- Per-symbol restart-safety verification, kill-latch recovery, and run-state
  marker, mirroring the single-symbol ``main.py`` guarantees.
- One fail-closed cancel/liquidation path for the strategy auto-exit.

Strategy (locked specification)
-------------------------------
- Auto-entry (ALL must hold, AND logic): ADX(14) < 20; RSI(14) < 35 OR
  %B <= 0; Volume Oscillator(5,10) > 0.
- Auto-exit (ANY triggers, OR logic — close-all): RSI(14) >= 70;
  ADX(14) > 25; %B > 1; |Z-Score(20)| > 2.5.  On exit the bot cancels every
  remaining grid order, liquidates all held base inventory at the market
  price, closes the active plan, and enters a 3-hour cooldown before new
  auto-entry is evaluated again.  This is a STRATEGY exit: it never latches
  the permanent kill state.
- Risk kills (equity drawdown >= 2%, range-break beyond the ±1% buffer, the
  dedicated 15m lower-boundary candle-close stop) latch the persisted kill
  state and require an explicit operator release — unchanged.
- Grid: step = 1x ATR(14) percentage with a 0.5% gross floor (>= 0.50%);
  every grid must pass the hard minimum net profit of STRICTLY MORE than
  0.20% after maker+taker fees, slippage, tick/quantity rounding and
  exchange filters, or it is rejected (0.200% = REJECT, 0.201% = PASS).
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from cancel_controller import CancelController
from config_loader import ConfigError, load_config, resolve_binance_credentials
from execution_bridge import (
    BridgeGate,
    TestnetExecutionBridge,
    build_bridge_from_env,
)
from fee_model import effective_fees
from grid_engine import (
    GridMode,
    atr_grid_step_pct,
    build_grid,
    validate_grid_profit,
)
from grid_lifecycle import LifecycleManager
from global_risk import evaluate as global_risk_evaluate
from global_risk import is_killed as global_risk_killed
from grid_planner import ActivePlan
from indicators import enrich, latest_valid_row
from market_data import (
    AccountDataError,
    MAX_TICKER_AGE_SECONDS,
    MarketDataError,
    build_account_risk_state,
    fetch_15m_closed_close,
    fetch_account_commission,
    fetch_account_snapshot,
    fetch_book_ticker,
    fetch_klines,
    fetch_open_orders,
    fetch_symbol_info,
    fetch_ticker_price,
    is_quote_fresh,
    is_ticker_fresh,
    make_client,
)
from market_features import calculate_market_features
from market_regime import MarketRegime, classify_market_regime
from paper_accounting import PaperAccountingEngine
from paper_orchestrator import PaperCycleInput, PaperSession
from range_engine import auto_range
from range_quality import calculate_range_quality
from risk_engine import (
    RiskDecision,
    account_state_gate,
    combine,
    cooldown_gate,
    daily_profit_lock,
    equity_dd_kill,
    equity_reference_gate,
    inventory_gate,
    lower_boundary_15m_kill,
    open_orders_available_gate,
    open_orders_gate,
    profit_gate,
    range_break_kill,
    strict_order_price_gate,
)
from runstate import persist_run_state, verify_restart_safety
from runtime import GridRuntime, RuntimeConfigError, load_runtime_config
from shutdown import ShutdownCoordinator
from strategy_state import StrategyState, StrategyStateTracker
from storage import (
    get_kill_state,
    get_paper_account_state,
    get_state,
    init_db,
    record_equity,
    record_paper_liquidation,
    record_risk_event,
    set_state,
)
from symbol_rules import parse_symbol_info, validate_quantized_order_plan
from strategy import calculate_percent_b, evaluate_strategy

#: Risk-gate reasons that latch the permanent kill state.  The strategy
#: auto-exit deliberately is NOT in this set: it cancels + liquidates and
#: re-enters after the cooldown instead of requiring an operator release.
_KILL_TRIGGER_REASONS = frozenset({
    "EQUITY_DRAWDOWN_KILL",
    "RANGE_BREAK_BELOW_BUFFER",
    "RANGE_BREAK_ABOVE_BUFFER",
    "LOWER_BOUNDARY_STOP_15M",
})

_TIMEFRAME_DELTA = {
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
}


# ---------------------------------------------------------------------------
# Per-symbol database path helper
# ---------------------------------------------------------------------------
def _symbol_db_path(base_path: str, symbol: str) -> str:
    """Generate the per-symbol database path (shared with the dashboard)."""
    base = Path(base_path)
    return str(base.parent / f"{base.stem}_{symbol}{base.suffix}")


# ---------------------------------------------------------------------------
# Peak equity persistence (PATCH 1, F-H1)
# ---------------------------------------------------------------------------
def _load_peak_equity(db_path: str) -> Decimal | None:
    raw = get_state(db_path, "paper_reference_equity")
    if raw is None:
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    return value


def _record_peak_equity(db_path: str, equity: Decimal) -> None:
    if not equity.is_finite() or equity <= 0:
        return
    peak = _load_peak_equity(db_path)
    if peak is None or equity > peak:
        set_state(db_path, "paper_reference_equity", str(equity))


# ---------------------------------------------------------------------------
# Cooldown timer persistence (auto-exit 3-hour cooldown)
# ---------------------------------------------------------------------------
def _load_last_auto_exit_ts(db_path: str) -> datetime | None:
    """Load the timestamp of the last auto-exit from database."""
    raw = get_state(db_path, "last_auto_exit_ts")
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _record_auto_exit_ts(db_path: str, ts: datetime | None = None) -> None:
    """Record the timestamp of an auto-exit event."""
    if ts is None:
        ts = datetime.now(timezone.utc)
    set_state(db_path, "last_auto_exit_ts", ts.isoformat())


def _is_in_cooldown(db_path: str, cooldown_hours: int) -> bool:
    """Check if we're in the post-auto-exit cooldown period."""
    if cooldown_hours <= 0:
        return False
    last_exit = _load_last_auto_exit_ts(db_path)
    if last_exit is None:
        return False
    now = datetime.now(timezone.utc)
    cooldown_delta = timedelta(hours=cooldown_hours)
    return (now - last_exit) < cooldown_delta


# ---------------------------------------------------------------------------
# Kill latch (same fail-closed contract as main.py, local implementation)
# ---------------------------------------------------------------------------
def _activate_kill_state(db_path, cfg, rules, trigger, note="", actor="risk_engine"):
    """Latch the persisted kill state and cancel open orders.

    * Latches ``kill_state`` (persisted, survives restart) FIRST so a crash
      mid-pass still leaves the kill active and a restart re-enters the kill
      branch.
    * Runs one cancel pass over every open local order.  A failed or unknown
      cancel does NOT clear the latch: the state stays active with
      ``cancel_status=PENDING_RECONCILIATION`` and a fresh attempt is made on
      the next run.  Only an explicitly reconciled pass permits an operator
      release (``scripts/release_kill_state.py``).
    * Never places replacement orders and never weakens the risk gate.
    """
    controller = CancelController(db_path, cfg, rules)
    controller.pre_latch(trigger, actor="risk_engine", note=note)
    report = controller.cancel_open_orders(actor=actor, trigger_note=trigger)
    controller.latch_kill_state(trigger, report, actor="risk_engine", note=note)
    return report


# ---------------------------------------------------------------------------
# Paper-cycle clock anchoring (market-freshness gate compatibility)
# ---------------------------------------------------------------------------
def _paper_clock(kline_df, timeframe: str):
    """Clock anchored to the last closed candle plus one timeframe.

    The orchestrator's market-freshness gate compares the last closed
    candle's ``close_time`` against this clock.  A wall clock would reject
    4h candles (age up to 14400s) against the 5400s freshness window for
    most of every candle, so — exactly like the single-symbol ``main.py`` —
    the clock is anchored to ``last_close + timeframe``.  Falls back to the
    wall clock only when no candle frame is available.
    """
    if hasattr(kline_df, "iloc") and len(kline_df) and "close_time" in getattr(kline_df, "columns", []):
        try:
            last_close = kline_df["close_time"].iloc[-1].to_pydatetime()
            delta = _TIMEFRAME_DELTA[timeframe]
            return lambda: last_close + delta
        except Exception:
            pass
    return lambda: datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Single-symbol cycle runner
# ---------------------------------------------------------------------------
class SymbolCycleRunner:
    """Runs one complete fail-closed cycle for a single symbol."""

    def __init__(
        self,
        symbol: str,
        cfg: dict[str, Any],
        db_path: str,
        client,
        logger: logging.Logger,
        shutdown: ShutdownCoordinator,
        bridge: TestnetExecutionBridge | None = None,
        global_db_path: str | None = None,
        global_risk_allowed: bool = True,
    ):
        self.symbol = symbol
        self.cfg = cfg
        self.db_path = db_path
        self.client = client
        self.logger = logger
        self.shutdown = shutdown
        self.bridge = bridge
        self.global_db_path = global_db_path
        self.global_risk_allowed = global_risk_allowed
        self.state_tracker = StrategyStateTracker(db_path)
        init_db(db_path)

    # -- config accessors ----------------------------------------------------
    def _get_grid_mode(self) -> GridMode:
        mode_str = self.cfg["grid"]["mode_by_symbol"].get(self.symbol, "geometric")
        return "arithmetic" if mode_str == "arithmetic" else "geometric"

    def _get_min_gross_profit(self) -> Decimal:
        return Decimal(str(self.cfg["grid"]["min_gross_profit_pct"]))

    def _get_hard_min_net(self) -> Decimal:
        return Decimal(str(self.cfg["grid"]["hard_min_net_pct"]))

    # -- cycle ----------------------------------------------------------------
    def run_cycle(self) -> dict[str, Any]:
        """Execute one complete cycle for this symbol.

        Every terminal outcome returns ``success=True`` (the cycle itself ran
        to a deterministic, fail-closed decision); only an unexpected
        exception sets ``success=False``.  ``status`` carries the outcome:
        OK, RISK_BLOCKED, ENTRY_BLOCKED, ENTRY_COOLDOWN, RANGE_BLOCKED,
        AUTO_EXIT, KILL_TRIGGERED, KILL_ACTIVE, RESTART_REFUSED or ERROR.
        """
        result: dict[str, Any] = {
            "symbol": self.symbol,
            "success": True,
            "status": "OK",
            "error": None,
            "kill_triggered": False,
            "entry_decision": None,
            "exit_decision": None,
            "combined_allowed": False,
            "combined_reason": "NOT_EVALUATED",
            "current_price": None,
            "range": None,
            "grid_cells": 0,
            "dynamic_step_pct": None,
            "open_orders": 0,
            "pending_cancels": 0,
        }
        try:
            self._run_cycle_inner(result)
        except Exception as exc:
            self.logger.exception("CYCLE ERROR for %s", self.symbol)
            self.state_tracker.transition(
                StrategyState.ERROR, f"cycle exception: {exc}")
            result["success"] = False
            result["status"] = "ERROR"
            result["error"] = str(exc)
        return result

    def _transition_state(self, target: StrategyState, reason: str) -> None:
        report = self.state_tracker.transition(target, reason)
        result_state = self.state_tracker.current()
        set_state(self.db_path, "last_strategy_state",
                  {"state": result_state.value, "reason": reason,
                   "transition_applied": report["applied"],
                   "transition_rejected": report["rejected"],
                   "from_state": report["from"], "to_state": report["to"]})
        if report["rejected"]:
            self.logger.warning(
                "STRATEGY STATE transition rejected for %s: %s -> %s (%s)",
                self.symbol, report["from"], report["to"], reason)

    def _run_cycle_inner(self, result: dict[str, Any]) -> None:
        cfg = self.cfg
        db_path = self.db_path

        # -- symbol rules (fail-closed: invalid symbol aborts this cycle) ----
        symbol_info = fetch_symbol_info(self.client, self.symbol)
        rules = parse_symbol_info(symbol_info)

        # -- restart safety ----------------------------------------------------
        restart = verify_restart_safety(db_path)
        if restart["restart_action"] == "REFUSE":
            record_risk_event(db_path, False, "RESTART_RECONCILIATION_REFUSED",
                              {"symbol": self.symbol,
                               "recovery_errors": restart["recovery_errors"]})
            self._transition_state(StrategyState.BLOCKED,
                                   "RESTART_RECONCILIATION_REFUSED")
            result["status"] = "RESTART_REFUSED"
            result["combined_reason"] = "RESTART_RECONCILIATION_REFUSED"
            self.logger.error(
                "RESTART REFUSED for %s: paper-state reconciliation failed; "
                "fix or reconcile, then re-run", self.symbol)
            return

        # GLOBAL risk has priority over everything (locked spec section 25).
        if not self.global_risk_allowed:
            global_kill = (
                self.global_db_path is not None
                and global_risk_killed(self.global_db_path))
            if global_kill:
                # Propagate: latch the per-symbol kill and cancel its orders.
                report = _activate_kill_state(
                    db_path, cfg, rules, "GLOBAL_EQUITY_DRAWDOWN_KILL",
                    note="global kill propagation",
                )
                bridge_cancels = (
                    self.bridge.cancel_all_open("global_kill")
                    if self.bridge is not None else None
                )
                record_risk_event(db_path, False, "GLOBAL_KILL", {
                    "symbol": self.symbol,
                    "cancel_status": report.overall_status,
                    "pending_orders": report.pending,
                    "bridge_cancels": bridge_cancels,
                })
                self._transition_state(StrategyState.BLOCKED, "GLOBAL_KILL")
                result["status"] = "KILL_ACTIVE"
                result["combined_reason"] = "GLOBAL_KILL"
                result["pending_cancels"] = int(report.pending)
                self.logger.warning(
                    "GLOBAL_KILL active for %s (cancel_status=%s pending=%s)",
                    self.symbol, report.overall_status, report.pending)
                return
            # Unknown global drawdown (fail-closed, section 17).
            self._transition_state(StrategyState.BLOCKED,
                                   "GLOBAL_RISK_UNAVAILABLE")
            result["status"] = "RISK_BLOCKED"
            result["combined_reason"] = "GLOBAL_RISK_UNAVAILABLE"
            self.logger.warning(
                "GLOBAL RISK UNAVAILABLE for %s: drawdown unknown — "
                "trading blocked (fail closed)", self.symbol)
            return

        kill_prior = get_kill_state(db_path)
        kill_active = bool(kill_prior and kill_prior.get("active"))
        if restart["restart_action"] == "KILL_BRANCH" or kill_active:
            # F-H2: a persisted kill latch MUST be honored on every start —
            # re-attempt cancellation/reconciliation, keep the latch, place
            # nothing.  A restart can never silently resume trading.
            report = _activate_kill_state(
                db_path, cfg, rules,
                (kill_prior or {}).get("trigger") or "KILL_STATE_RESTART_RECOVERY",
                note="restart recovery",
            )
            bridge_cancels = (
                self.bridge.cancel_all_open("kill")
                if self.bridge is not None else None
            )
            record_risk_event(db_path, False, "KILL_STATE_ACTIVE", {
                "symbol": self.symbol,
                "trigger": (kill_prior or {}).get("trigger"),
                "activated_at": (kill_prior or {}).get("activated_at"),
                "cancel_status": report.overall_status,
                "pending_orders": report.pending,
                "bridge_cancels": bridge_cancels,
                "note": "restart recovery: kill state remains active",
            })
            self._transition_state(StrategyState.BLOCKED, "KILL_STATE_ACTIVE")
            result["status"] = "KILL_ACTIVE"
            result["combined_reason"] = "KILL_STATE_ACTIVE"
            result["pending_cancels"] = int(report.pending)
            self.logger.warning(
                "KILL STATE ACTIVE for %s (trigger=%s cancel_status=%s pending=%s)",
                self.symbol, (kill_prior or {}).get("trigger"),
                report.overall_status, report.pending)
            return

        # -- market data --------------------------------------------------------
        df = fetch_klines(
            self.client, self.symbol, cfg["timeframe"], cfg["range"]["lookback"],
            drop_incomplete=True,
        )
        enriched = enrich(df)
        last = latest_valid_row(enriched)

        ticker = fetch_ticker_price(self.client, self.symbol)
        if not is_ticker_fresh(ticker, MAX_TICKER_AGE_SECONDS):
            raise MarketDataError(f"Ticker price is stale for {ticker.symbol}")
        current_price = ticker.price
        result["current_price"] = str(current_price)

        book_quote = None
        if "market_intelligence" in cfg:
            try:
                book_quote = fetch_book_ticker(self.client, self.symbol)
                max_quote_age = int(
                    cfg["market_intelligence"].get("liquidity", {}).get(
                        "max_quote_ticker_age_seconds", 10)
                )
                if not is_quote_fresh(book_quote, max_quote_age):
                    raise MarketDataError(
                        f"Book ticker quote is stale for {self.symbol}")
            except Exception as exc:
                self.logger.warning("BOOK TICKER UNAVAILABLE for %s: %s",
                                    self.symbol, exc)
                book_quote = None

        # -- range (approval is mandatory before any grid work) -----------------
        range_cfg = cfg["range"]
        if range_cfg["mode"] == "manual":
            lower = Decimal(str(range_cfg["lower_price"]))
            upper = Decimal(str(range_cfg["upper_price"]))
            range_approved = True
            range_reason = "MANUAL_RANGE"
        else:
            candidate = auto_range(enriched, **range_cfg["auto"])
            lower, upper = candidate.lower, candidate.upper
            range_approved = candidate.approved
            range_reason = candidate.reason
        result["range"] = [str(lower), str(upper)]

        if not range_approved:
            # The auto-range is the grid's safety envelope: an unapproved
            # range (price outside, quality too low, width outside limits,
            # or a degenerate 0/0 candidate) must never host a grid.
            set_state(db_path, "last_risk_decision",
                      {"allowed": False, "reason": f"RANGE:{range_reason}"})
            record_risk_event(db_path, False, f"RANGE:{range_reason}", {
                "symbol": self.symbol,
                "price": str(current_price),
                "range": [str(lower), str(upper)],
            })
            self._transition_state(
                StrategyState.BLOCKED, f"RANGE:{range_reason}")
            result["status"] = "RANGE_BLOCKED"
            result["combined_reason"] = f"RANGE:{range_reason}"
            self.logger.info("RANGE BLOCKED for %s: %s (range=%s..%s)",
                             self.symbol, range_reason, lower, upper)
            return

        # -- market features / indicators (planner inputs; no strategy gating) ---
        # The indicator layer supplies ADX/RSI/%B/VolOsc/Z for the strategy
        # decision below.  The legacy market-intelligence eligibility gate was
        # removed with the old strategy: entry/exit is decided exclusively by
        # the locked indicator rules in strategy.py.
        regime = MarketRegime.RANGE
        range_quality_score = Decimal("80")
        features = None
        if "market_intelligence" in cfg:
            try:
                features = calculate_market_features(
                    df=df, quote=book_quote, lower_price=lower,
                    upper_price=upper, symbol=self.symbol, config=cfg,
                )
                regime, _ = classify_market_regime(features, cfg)
                quality_res = calculate_range_quality(features, cfg)
                range_quality_score = quality_res.score
            except Exception as exc:
                self.logger.warning("MARKET FEATURES ERROR for %s: %s",
                                    self.symbol, exc)
                features = None

        # -- active plan (lifecycle is the single source of truth) ---------------
        has_active_grid = False
        active_plan = None
        lifecycle = LifecycleManager(db_path)
        active = lifecycle.get_active_plan()
        if active is not None:
            has_active_grid = True
            active_plan = ActivePlan(
                plan_id=active.plan_id,
                candidate_lower=active.candidate_lower,
                candidate_upper=active.candidate_upper,
                grid_step=active.grid_step,
                grid_count=active.grid_count,
                regime=MarketRegime(active.regime),
                range_quality_score=active.range_quality_score,
                candle_index=active.candle_index,
                generation=lifecycle.get_generation(),
            )

        # -- strategy evaluation (auto-entry / auto-exit) -------------------------
        entry_decision = None
        exit_decision = None
        cooldown_hours = int(cfg.get("strategy", {}).get("cooldown_hours", 3))
        in_cooldown = _is_in_cooldown(db_path, cooldown_hours)

        if features is not None:
            try:
                entry_decision, exit_decision = evaluate_strategy(
                    features=features, config=cfg,
                    has_active_grid=has_active_grid, in_cooldown=in_cooldown,
                )
            except Exception as exc:
                self.logger.warning("STRATEGY EVALUATION ERROR for %s: %s",
                                    self.symbol, exc)
            if entry_decision is not None:
                result["entry_decision"] = {
                    "signal": entry_decision.signal.value,
                    "allowed": entry_decision.allowed,
                    "reasons": list(entry_decision.reasons),
                }
            if exit_decision is not None:
                result["exit_decision"] = {
                    "signal": exit_decision.signal.value,
                    "should_exit": exit_decision.should_exit,
                    "triggered_reasons": list(exit_decision.triggered_reasons),
                }
        elif not has_active_grid:
            # Fail-closed: without market features the entry conditions
            # cannot be evaluated, so entry is blocked this cycle.
            entry_decision = None
            result["entry_decision"] = {
                "signal": "ENTRY_BLOCKED",
                "allowed": False,
                "reasons": ["MARKET_FEATURES_UNAVAILABLE"],
            }

        # Persist the per-symbol signal snapshot (dashboard + audit):
        # indicator values, entry/exit decisions, cooldown, timeframe.
        def _ind(value):
            return str(value) if value is not None else None

        signal_snapshot = {
            "symbol": self.symbol,
            "timeframe": cfg["timeframe"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "indicators": {
                "rsi": _ind(features.rsi) if features is not None else None,
                "adx": _ind(features.adx) if features is not None else None,
                "percent_b": (
                    str(calculate_percent_b(
                        features.close_price, features.bb_upper,
                        features.bb_lower, features.bb_middle))
                    if features is not None else None),
                "volume_oscillator": (
                    _ind(features.volume_oscillator)
                    if features is not None else None),
                "z_score": _ind(features.z_score) if features is not None else None,
                "atr_pct": _ind(features.atr_pct) if features is not None else None,
            },
            "entry_signal": (
                {"signal": entry_decision.signal.value,
                 "allowed": entry_decision.allowed,
                 "reasons": list(entry_decision.reasons)}
                if entry_decision is not None else None),
            "exit_signal": (
                {"signal": exit_decision.signal.value,
                 "should_exit": exit_decision.should_exit,
                 "triggered_reasons": list(exit_decision.triggered_reasons)}
                if exit_decision is not None else None),
            "in_cooldown": in_cooldown,
            "has_active_grid": has_active_grid,
        }
        set_state(db_path, "last_signal", signal_snapshot)

        # -- strategy auto-exit: cancel all + liquidate + cooldown ----------------
        if exit_decision is not None and exit_decision.should_exit:
            self._transition_state(
                StrategyState.EXIT_SIGNAL,
                " | ".join(exit_decision.triggered_reasons))
            self._execute_strategy_exit(exit_decision, current_price, rules,
                                        result)
            return

        # -- strategy auto-entry gate ---------------------------------------------
        if not has_active_grid:
            if features is None:
                set_state(db_path, "last_risk_decision",
                          {"allowed": False, "reason": "ENTRY_BLOCKED"})
                self._transition_state(StrategyState.BLOCKED,
                                       "MARKET_FEATURES_UNAVAILABLE")
                result["status"] = "ENTRY_BLOCKED"
                result["combined_reason"] = "MARKET_FEATURES_UNAVAILABLE"
                self.logger.info(
                    "AUTO-ENTRY BLOCKED for %s: market features unavailable",
                    self.symbol)
                return
            if entry_decision is not None and not entry_decision.allowed:
                set_state(db_path, "last_risk_decision",
                          {"allowed": False, "reason": "ENTRY_BLOCKED"})
                result["status"] = (
                    "ENTRY_COOLDOWN"
                    if entry_decision.signal.value == "ENTRY_COOLDOWN"
                    else "ENTRY_BLOCKED"
                )
                result["combined_reason"] = (
                    "ENTRY_COOLDOWN" if result["status"] == "ENTRY_COOLDOWN"
                    else " | ".join(entry_decision.reasons)
                )
                self._transition_state(
                    StrategyState.COOLDOWN
                    if result["status"] == "ENTRY_COOLDOWN"
                    else StrategyState.WAITING_FOR_ENTRY,
                    result["combined_reason"][:200])
                self.logger.info("AUTO-ENTRY BLOCKED for %s: %s", self.symbol,
                                 result["combined_reason"])
                return
            if entry_decision is not None and entry_decision.allowed:
                self.logger.info("AUTO-ENTRY ALLOWED for %s", self.symbol)

        # -- account data -----------------------------------------------------------
        account_snapshot = fetch_account_snapshot(
            self.client, rules.base_asset, rules.quote_asset)
        account_risk = None
        if account_snapshot is not None:
            reference_equity = _load_peak_equity(db_path)
            try:
                account_risk = build_account_risk_state(
                    account_snapshot, current_price, reference_equity)
                _record_peak_equity(db_path, account_risk.current_equity)
                try:
                    record_equity(db_path, account_risk.current_equity,
                                  account_risk.drawdown_pct)
                except Exception:
                    pass
            except AccountDataError as exc:
                self.logger.warning("ACCOUNT RISK STATE ERROR for %s: %s",
                                    self.symbol, exc)
                account_risk = None

        # -- fees + grid construction ------------------------------------------------
        commission_payload, _ = fetch_account_commission(self.client, self.symbol)
        fees = effective_fees(
            commission_payload,
            cfg["fees"]["maker_fee_fallback"],
            cfg["fees"]["taker_fee_fallback"],
        )

        # Locked grid step = GRID_STEP_ATR_MULTIPLIER x ATR(14) — no fixed
        # floor, no silent cap.  An invalid ATR fails closed (explicit block).
        # Prefer the features-path ATR (honors the configured periods); the
        # enrich-path ATR (period 14) is the fallback.
        if features is not None and features.atr_pct is not None:
            atr_pct = Decimal(str(features.atr_pct))
        else:
            atr_pct = Decimal(str(last.get("atr_pct", "0.01")))
        atr_multiplier = Decimal(str(cfg["grid"].get("atr_multiplier", "1.0")))
        try:
            dynamic_step = atr_grid_step_pct(atr_pct, atr_multiplier)
        except ValueError as exc:
            set_state(db_path, "last_risk_decision",
                      {"allowed": False, "reason": f"GRID:{exc}"})
            record_risk_event(db_path, False, "GRID_ATR_INVALID", {
                "symbol": self.symbol, "atr_pct": str(atr_pct),
                "reason": str(exc),
            })
            self._transition_state(StrategyState.BLOCKED, f"GRID:{exc}")
            result["status"] = "RISK_BLOCKED"
            result["combined_reason"] = f"GRID:{exc}"
            self.logger.warning("GRID BLOCKED for %s: %s", self.symbol, exc)
            return
        result["dynamic_step_pct"] = str(dynamic_step)

        grid_mode = self._get_grid_mode()
        grid_result = build_grid(
            lower, upper, dynamic_step, grid_mode,
            min_cells=int(cfg["grid"]["min_cells"]),
            max_levels=int(cfg["grid"]["max_levels"]),
        )
        result["range"] = [str(lower), str(grid_result.effective_upper)]
        result["grid_cells"] = int(grid_result.cells)

        # Gross economics gate: every adjacent spacing must clear the
        # configured minimum GROSS profit (0.5%).  With a pure-ATR step this
        # is what BLOCKS low-volatility grids (recorded, never widened).
        min_spacing = min(
            (b.price / a.price) - Decimal("1")
            for a, b in zip(grid_result.levels[:-1], grid_result.levels[1:])
        )
        gross_ok = min_spacing >= self._get_min_gross_profit()

        sell_fee = fees.maker if cfg["execution"]["prefer_limit_maker"] else fees.taker
        pre_quant_validation = validate_grid_profit(
            grid_result.levels, fees.maker, sell_fee,
            Decimal(str(cfg["fees"]["slippage_roundtrip_pct"])),
            self._get_hard_min_net(),
        )
        plan_validation = validate_quantized_order_plan(
            grid_result.levels, rules,
            Decimal(str(cfg["execution"]["order_quote_size"])),
            current_price,
            fees.maker,
            sell_fee,
            Decimal(str(cfg["fees"]["slippage_roundtrip_pct"])),
            self._get_hard_min_net(),
            int(cfg["execution"]["max_open_orders"]),
        )

        # -- risk gates ---------------------------------------------------------------
        # NOTE: the legacy market_filter gate (ADX/ATR/BB-width/volume-spike
        # caps) was removed with the old strategy.  Entry is decided solely by
        # the locked indicator rules; safety is enforced by the gates below.
        close_15m = fetch_15m_closed_close(self.client, self.symbol)
        decisions = [
            profit_gate(
                plan_validation.min_net_pct,
                self._get_hard_min_net(),
            ),
            strict_order_price_gate(lower, grid_result.effective_upper, current_price),
            range_break_kill(lower, grid_result.effective_upper, current_price,
                             cfg["risk"]["range_break_buffer_pct"]),
            # Dedicated 15m lower-boundary stop: consumes the latest CLOSED
            # 15m candle close (never the strategy-timeframe candle, never
            # the ticker); None fails closed and vetoes new orders.
            lower_boundary_15m_kill(
                close_15m,
                lower,
                cfg["risk"]["stop_if_below_lower_pct"],
            ),
            cooldown_gate(False),
            daily_profit_lock(Decimal("0"), cfg["risk"]["daily_profit_lock_pct"]),
            equity_reference_gate(
                get_state(db_path, "paper_reference_equity"),
                _load_peak_equity(db_path),
            ),
        ]
        if not plan_validation.allowed:
            decisions.append(RiskDecision(
                False, (f"PLAN_VALIDATION:{plan_validation.reason}",)))
        if not gross_ok:
            decisions.append(RiskDecision(False, ("GRID_GROSS_BELOW_MIN",)))
        if account_risk is not None:
            decisions.extend([
                equity_dd_kill(account_risk.drawdown_pct,
                               cfg["risk"]["max_equity_drawdown_pct"]),
                inventory_gate(account_risk.inventory_pct,
                               cfg["execution"]["max_inventory_pct"]),
            ])
        else:
            decisions.append(account_state_gate(False))

        open_orders = fetch_open_orders(self.client, self.symbol)
        if open_orders is None:
            decisions.append(open_orders_available_gate(False))
        else:
            result["open_orders"] = len(open_orders)
            decisions.extend((
                open_orders_available_gate(True),
                open_orders_gate(len(open_orders),
                                 cfg["execution"]["max_open_orders"]),
            ))

        # -- testnet execution bridge (double-gated; no-op when disabled) --------
        bridge_reconcile = None
        unknown_remote = None
        if self.bridge is not None:
            # Authoritative status pass over mirrored real orders, then the
            # fail-closed foreign-order guard: own-namespace orders resting
            # on the exchange that the runtime did not submit block new
            # mirroring until an operator resolves them.
            bridge_reconcile = self.bridge.reconcile()
            unknown_remote = self.bridge.unknown_remote_orders()
            if unknown_remote:
                decisions.append(RiskDecision(
                    False, ("EXECUTION_UNKNOWN_REMOTE_ORDERS",)))
                self.logger.warning(
                    "UNKNOWN REMOTE ORDERS on %s: %s — mirroring blocked "
                    "until resolved", self.symbol,
                    [u["cid"] for u in unknown_remote])

        combined = combine(*decisions)

        # -- risk kills: latch the persisted kill state --------------------------------
        kill_triggers = [r for r in combined.reasons if r in _KILL_TRIGGER_REASONS]
        if kill_triggers:
            report = _activate_kill_state(
                db_path, cfg, rules,
                " | ".join(kill_triggers),
                note=f"price={current_price} range={lower}->{grid_result.effective_upper}",
            )
            bridge_cancels = (
                self.bridge.cancel_all_open("kill")
                if self.bridge is not None else None
            )
            record_risk_event(db_path, False, " | ".join(kill_triggers), {
                "symbol": self.symbol,
                "price": str(current_price),
                "range": [str(lower), str(grid_result.effective_upper)],
                "cancel_status": report.overall_status,
                "pending_orders": report.pending,
                "bridge_cancels": bridge_cancels,
            })
            self._transition_state(StrategyState.BLOCKED,
                                   " | ".join(kill_triggers))
            result["status"] = "KILL_TRIGGERED"
            result["kill_triggered"] = True
            result["combined_reason"] = combined.reason
            result["pending_cancels"] = int(report.pending)
            self.logger.warning("KILL TRIGGERED for %s: %s", self.symbol,
                                kill_triggers)
            return

        # -- persist observability state -------------------------------------------------
        set_state(db_path, "last_symbol", self.symbol)
        set_state(db_path, "last_price", str(current_price))
        set_state(db_path, "last_range",
                  {"lower": str(lower), "upper": str(grid_result.effective_upper)})
        set_state(db_path, "last_risk_decision",
                  {"allowed": combined.allowed, "reason": combined.reason})
        record_risk_event(db_path, combined.allowed, combined.reason, {
            "symbol": self.symbol,
            "price": str(current_price),
            "range": [str(lower), str(grid_result.effective_upper)],
            "grid_mode": grid_mode,
            "dynamic_step_pct": str(dynamic_step),
            "atr_pct": str(atr_pct),
            "grid_cells": grid_result.cells,
            "min_net_pct": str(plan_validation.min_net_pct),
            "pre_quant_min_net_pct": str(pre_quant_validation.min_net_pct),
            "pre_quant_allowed": pre_quant_validation.allowed,
            "min_spacing_pct": str(min_spacing),
            "gross_ok": gross_ok,
            "bridge_reconcile": bridge_reconcile,
            "unknown_remote": unknown_remote,
        })

        result["combined_allowed"] = bool(combined.allowed)
        result["combined_reason"] = combined.reason
        if not combined.allowed:
            # With an active grid the plan keeps running (fills/cancels are
            # still managed); only NEW deployment is blocked.
            self._transition_state(
                StrategyState.GRID_ACTIVE if has_active_grid
                else StrategyState.BLOCKED,
                combined.reason)
            result["status"] = "RISK_BLOCKED" if not has_active_grid                 else "GRID_ACTIVE"
            return
        if has_active_grid:
            self._transition_state(StrategyState.GRID_ACTIVE,
                                   "grid active, no exit signal")
        else:
            self._transition_state(StrategyState.ENTRY_SIGNAL,
                                   "entry conditions met")

        # -- paper cycle (dry-run only, idempotent per cycle index) ----------------------
        if not self.shutdown.is_requested:
            kill_now = get_kill_state(db_path)
            if kill_now and kill_now.get("active"):
                result["status"] = "KILL_ACTIVE"
                result["combined_reason"] = "KILL_STATE_ACTIVE"
                return
            cycle_candle_index = int(get_state(db_path, "paper_cycle_index") or "0") + 1
            set_state(db_path, "paper_cycle_index", str(cycle_candle_index))

            accounting = PaperAccountingEngine(
                rules.base_asset,
                rules.quote_asset,
                Decimal(str(cfg["paper"]["initial_base_balance"])),
                Decimal(str(cfg["paper"]["initial_quote_balance"])),
                Decimal(str(cfg["paper"]["maker_fee"])),
                Decimal(str(cfg["paper"]["taker_fee"])),
                str(cfg["paper"]["fee_asset"]),
            )
            self._transition_state(StrategyState.DEPLOYING_GRID,
                                   "deploying risk-vetted grid")
            session = PaperSession(db_path, db_path, accounting,
                                   client_order_prefix=f"AG{self.symbol[:3]}")
            timeframe_seconds = int(
                _TIMEFRAME_DELTA[cfg["timeframe"]].total_seconds())
            cycle_input = PaperCycleInput(
                candle_index=cycle_candle_index,
                symbol=self.symbol,
                current_price=current_price,
                kline_df=df,
                quote=None,
                lower_price=lower,
                upper_price=grid_result.effective_upper,
                active_plan=active_plan,
                regime=regime,
                range_quality_score=range_quality_score,
                cfg=cfg,
                maker_fee=fees.maker,
                taker_fee=sell_fee,
                fee_asset=str(cfg["paper"]["fee_asset"]),
                risk_decision=combined,
                clock=_paper_clock(df, cfg["timeframe"]),
                # The freshness window must cover one full strategy candle:
                # the anchored clock (last closed candle + timeframe) yields
                # an age of exactly one timeframe, and the 5400s default
                # would reject every 4h cycle.
                max_candle_age_seconds=timeframe_seconds + 900,
                dry_run=True,
                rules=rules,
            )
            cycle_result = session.run_cycle(cycle_input)
            result["cycle_result"] = {
                "orders_submitted": cycle_result.orders_submitted,
                "fills_applied": cycle_result.fills_applied,
                "success": cycle_result.success,
                "blocked_reason": cycle_result.blocked_reason,
            }
            if not cycle_result.success:
                self.logger.warning(
                    "PAPER CYCLE INCOMPLETE for %s: blocked=%s error=%s",
                    self.symbol, cycle_result.blocked_reason, cycle_result.error)
                self._transition_state(
                    StrategyState.BLOCKED,
                    f"paper cycle blocked: {cycle_result.blocked_reason}")
            else:
                self._transition_state(StrategyState.GRID_ACTIVE,
                                       "grid deployed (paper cycle ok)")
            # Bridge: mirror every risk-gated paper submission as a real
            # testnet LIMIT_MAKER order (no-op when the bridge is disabled).
            if self.bridge is not None and cycle_result.order_intents:
                mirrored = [self.bridge.mirror_order(intent)
                            for intent in cycle_result.order_intents]
                result["bridge_mirrored"] = mirrored
                placed = sum(1 for m in mirrored if m.get("mirrored"))
                self.logger.info(
                    "BRIDGE MIRROR for %s: %d/%d orders placed on testnet",
                    self.symbol, placed, len(mirrored))

    # -- strategy exit ---------------------------------------------------------
    def _execute_strategy_exit(self, exit_decision, current_price, rules,
                               result: dict[str, Any]) -> None:
        """Auto-exit: cancel all grid orders, liquidate inventory, cooldown.

        This is a STRATEGY exit, deliberately NOT the permanent kill latch:
        1. every remaining open grid order is canceled (idempotent pass);
        2. all free base inventory is market-sold at the current price with
           the taker fee (paper accounting, audited LIQUIDATION event);
        3. the active lifecycle plan is closed so auto-entry can fire again;
        4. the 3-hour cooldown timestamp is recorded.
        Every step is idempotent, so a repeat visit while the exit condition
        persists is a safe no-op.
        """
        reasons = " | ".join(exit_decision.triggered_reasons)
        self.logger.warning("AUTO-EXIT TRIGGERED for %s: %s", self.symbol,
                            exit_decision.triggered_reasons)

        controller = CancelController(self.db_path, self.cfg, rules)
        cancel_report = controller.cancel_open_orders(
            actor="strategy_exit", trigger_note=reasons)
        # Propagate the close-all to the real testnet orders (no-op when the
        # bridge is disabled).  Real base inventory is deliberately NOT
        # market-sold here (LIMIT_MAKER-only write surface) — it is reported
        # for operator action instead.
        bridge_cancels = (
            self.bridge.cancel_all_open("strategy_exit")
            if self.bridge is not None else None
        )

        paper_state = get_paper_account_state(self.db_path)
        liquidation: dict[str, Any] = {"liquidated": False,
                                       "reason": "NO_ACCOUNTING_STATE"}
        if paper_state is not None:
            event_id = (
                f"autoexit-{self.symbol}-"
                f"{int(datetime.now(timezone.utc).timestamp())}"
            )
            liquidation = record_paper_liquidation(
                self.db_path,
                event_id=event_id,
                symbol=self.symbol,
                price=current_price,
                quantity=paper_state["base_free"],
                taker_fee=Decimal(str(self.cfg["paper"]["taker_fee"])),
                fee_asset=str(self.cfg["paper"]["fee_asset"]),
                reason=f"AUTO_EXIT: {reasons}",
            )

        lifecycle = LifecycleManager(self.db_path)
        plan_closed = lifecycle.close_active_plan(
            reason="AUTO_EXIT",
            details={"reasons": list(exit_decision.triggered_reasons),
                     "price": str(current_price)},
        )

        _record_auto_exit_ts(self.db_path)
        already_flat = (
            cancel_report.overall_status == "NO_OPEN_ORDERS"
            and liquidation.get("liquidated") is False
            and not plan_closed
        )
        # Persist the full exit signal (locked spec): indicator values,
        # thresholds, exit reason, timestamp, symbol.
        exit_signal = {
            "symbol": self.symbol,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "triggered_reasons": list(exit_decision.triggered_reasons),
            "indicators": {
                "rsi": str(exit_decision.rsi),
                "adx": str(exit_decision.adx),
                "percent_b": str(exit_decision.percent_b),
                "z_score": str(exit_decision.z_score),
            },
            "thresholds": {
                "rsi_min": str(exit_decision.rsi_threshold),
                "adx_min": str(exit_decision.adx_threshold),
                "bb_percent_b_min": str(exit_decision.bb_threshold),
                "zscore_threshold": str(exit_decision.zscore_threshold),
            },
            "price": str(current_price),
            "cancel_status": cancel_report.overall_status,
            "liquidation": liquidation,
        }
        set_state(self.db_path, "last_exit_signal", exit_signal)
        record_risk_event(self.db_path, False, "AUTO_EXIT_LIQUIDATION", {
            "symbol": self.symbol,
            "price": str(current_price),
            "triggered_reasons": list(exit_decision.triggered_reasons),
            "indicators": exit_signal["indicators"],
            "thresholds": exit_signal["thresholds"],
            "cancel_status": cancel_report.overall_status,
            "pending_cancels": int(cancel_report.pending),
            "liquidation": liquidation,
            "plan_closed": plan_closed,
            "already_flat": already_flat,
            "bridge_cancels": bridge_cancels,
        })
        set_state(self.db_path, "last_risk_decision",
                  {"allowed": False, "reason": "AUTO_EXIT_LIQUIDATION"})

        self._transition_state(StrategyState.AUTO_EXIT,
                               f"auto-exit: {reasons}")
        self._transition_state(StrategyState.LIQUIDATING,
                               "liquidating held base inventory")
        cooldown_hours = int(self.cfg.get("strategy", {}).get("cooldown_hours", 3))
        self._transition_state(
            StrategyState.COOLDOWN,
            f"cooldown {cooldown_hours}h started (symbol-specific)")
        result["status"] = "AUTO_EXIT"
        result["combined_reason"] = f"AUTO_EXIT: {reasons}"
        result["exit_reasons"] = list(exit_decision.triggered_reasons)
        result["liquidation"] = liquidation
        result["pending_cancels"] = int(cancel_report.pending)
        result["already_flat"] = already_flat
        self.logger.warning(
            "AUTO-EXIT COMPLETE for %s: cancel=%s liquidation=%s plan_closed=%s",
            self.symbol, cancel_report.overall_status,
            liquidation.get("liquidated"), plan_closed)


# ---------------------------------------------------------------------------
# Multi-symbol pass and entrypoints
# ---------------------------------------------------------------------------
def _setup_logger(cfg: dict[str, Any]) -> logging.Logger:
    log_path = cfg["logging"]["log_path"]
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(formatter)
    # The multi-symbol cycle logger and the GridRuntime orchestration logger
    # (runtime.py) both write to the same sinks so the daemon's startup,
    # candle cadence, and stop reasons are visible in the same log stream.
    for name in ("adaptive_grid_multi", "adaptive_grid.runtime"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            logger.addHandler(sh)
            logger.addHandler(fh)
    return logging.getLogger("adaptive_grid_multi")


def run_once(cfg, logger, client, symbols, shutdown,
             bridges: dict[str, TestnetExecutionBridge | None] | None = None) -> int:
    """Run one pass over every configured symbol; returns a process exit code.

    A per-symbol failure is isolated: it is logged, persisted, and the loop
    continues with the remaining symbols.  The pass itself only fails (exit
    1) when shutdown was requested mid-pass.

    ``bridges`` maps symbol → optional TestnetExecutionBridge (built once at
    startup); when omitted, mirroring is disabled for every symbol.
    """
    base_db_path = cfg["logging"]["sqlite_path"]
    bridges = bridges or {}
    # The base database carries the GLOBAL risk state (reference equity,
    # global kill latch); ensure its schema exists.
    init_db(base_db_path)

    # -- GLOBAL account risk (locked spec section 17) -------------------------
    # Shared equity = USDT total + sum(base totals x price) across ALL
    # symbols; reference is a persistent high-water-mark in the base DB;
    # drawdown >= 2% latches the GLOBAL kill.  Unknown equity with an
    # existing reference FAILS CLOSED (blocks every symbol this pass).
    global_risk_allowed = True
    try:
        equity_total = Decimal("0")
        quote_total = None
        for symbol in symbols:
            info = fetch_symbol_info(client, symbol)
            rules_s = parse_symbol_info(info)
            snapshot = fetch_account_snapshot(
                client, rules_s.base_asset, rules_s.quote_asset)
            ticker = fetch_ticker_price(client, symbol)
            if quote_total is None:
                quote_total = snapshot.quote_total
            equity_total += snapshot.base_total * ticker.price
        if quote_total is not None:
            equity_total += quote_total
        global_eval = global_risk_evaluate(base_db_path, equity_total)
    except Exception as exc:
        logger.warning("GLOBAL RISK evaluation failed (%s); failing closed",
                       type(exc).__name__)
        global_eval = global_risk_evaluate(base_db_path, None)
    global_risk_allowed = bool(global_eval.get("allowed"))
    logger.info(
        "DRAWDOWN_UPDATE symbol=GLOBAL allowed=%s equity=%s reference=%s "
        "drawdown=%s reason=%s",
        global_eval.get("allowed"), global_eval.get("equity"),
        global_eval.get("reference"), global_eval.get("drawdown_pct"),
        global_eval.get("reason"))
    if global_eval.get("kill_triggered"):
        logger.warning("GLOBAL_KILL triggered: %s", global_eval)
    logger.info(
        "=== Multi-Symbol Adaptive Grid pass started: mode=testnet "
        "dry_run=%s symbols=%s timeframe=%s testnet_execution=%s ===",
        cfg["environment"]["dry_run"], symbols, cfg["timeframe"],
        {s: ("ON" if bridges.get(s) is not None else "OFF") for s in symbols})

    all_results = []
    for symbol in symbols:
        if shutdown.is_requested:
            logger.warning("Shutdown requested, stopping cycle loop")
            break
        symbol_db = _symbol_db_path(base_db_path, symbol)
        Path(symbol_db).parent.mkdir(parents=True, exist_ok=True)

        runner = SymbolCycleRunner(symbol, cfg, symbol_db, client, logger,
                                   shutdown, bridge=bridges.get(symbol),
                                   global_db_path=base_db_path,
                                   global_risk_allowed=global_risk_allowed)
        result = runner.run_cycle()
        all_results.append(result)

        if result["success"]:
            logger.info(
                "%s: status=%s price=%s range=%s->%s grid_cells=%s step=%s "
                "reason=%s",
                symbol, result["status"], result["current_price"],
                (result["range"] or ["?", "?"])[0],
                (result["range"] or ["?", "?"])[1],
                result["grid_cells"], result["dynamic_step_pct"],
                result["combined_reason"])
        else:
            logger.error("%s: CYCLE FAILED: %s", symbol, result["error"])

        persist_run_state(
            symbol_db,
            run_id=f"multi-{symbol}-{int(get_state(symbol_db, 'paper_cycle_index') or 0)}",
            completed=not shutdown.is_requested,
            risk_allowed=bool(result.get("combined_allowed")),
            kill_active=result["status"] in {"KILL_ACTIVE", "KILL_TRIGGERED"},
            pending_cancels=int(result.get("pending_cancels") or 0),
            open_orders=int(result.get("open_orders") or 0),
        )

    print("\n=== MULTI-SYMBOL CYCLE SUMMARY ===")
    for r in all_results:
        sym = r["symbol"]
        if not r["success"]:
            print(f"  {sym}: ERROR - {r['error']}")
            continue
        price = r.get("current_price") or "?"
        rng = r.get("range") or ["?", "?"]
        step = (r.get("dynamic_step_pct") or "0")
        try:
            step_pct = f"{float(step) * 100:.4f}%"
        except (TypeError, ValueError):
            step_pct = str(step)
        print(f"  {sym}: {r['status']} | price={price} | "
              f"range={rng[0]}..{rng[1]} | grid={r['grid_cells']} cells "
              f"@ {step_pct} | reason={r['combined_reason']}")
        if r.get("entry_decision"):
            print(f"    ENTRY: {r['entry_decision']['signal']} "
                  f"({'; '.join(r['entry_decision']['reasons'])})")
        if r.get("exit_decision"):
            print(f"    EXIT: {r['exit_decision']['signal']} "
                  f"({'; '.join(r['exit_decision']['triggered_reasons'])})")
        if r.get("kill_triggered"):
            print("    KILL: ACTIVE (operator release required)")
        if r.get("status") == "AUTO_EXIT":
            print(f"    LIQUIDATION: {r.get('liquidation')}")

    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Default mode is the continuous candle-cadence runtime loop (one
    multi-symbol pass per closed candle of the configured timeframe, SIGTERM
    safe).  ``--once`` runs a single pass and exits — for manual checks and
    bounded testnet verification.
    """
    parser = argparse.ArgumentParser(
        description="Multi-symbol adaptive grid bot (Binance Spot TESTNET, "
                    "dry-run only)")
    parser.add_argument("--once", action="store_true",
                        help="run a single multi-symbol pass and exit")
    parser.add_argument("--max-cycles", type=int, default=None,
                        help="stop the runtime loop after N passes "
                             "(bounded observation only)")
    args = parser.parse_args(argv)

    load_dotenv()
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"CONFIG BLOCK: {exc}")
        return 2
    if not cfg["environment"]["dry_run"]:
        raise RuntimeError("DRY_RUN must remain enabled; live execution is disabled")

    try:
        env, api_key, api_secret = resolve_binance_credentials(cfg)
    except ConfigError as exc:
        print(f"CONFIG BLOCK: {exc}")
        return 2

    logger = _setup_logger(cfg)
    client = make_client(env, api_key, api_secret)
    symbols = list(cfg["_parsed_symbols"])
    shutdown = ShutdownCoordinator()

    # Testnet execution bridge (roadmap B): built once per symbol.  Enabled
    # only when execution.testnet_execution=true AND TESTNET_ORDERS_ENABLED=
    # true; otherwise a disabled no-op for every symbol.
    base_db_path = cfg["logging"]["sqlite_path"]
    bridges: dict[str, TestnetExecutionBridge | None] = {}
    for symbol in symbols:
        bridge, gate = build_bridge_from_env(
            cfg, symbol, _symbol_db_path(base_db_path, symbol))
        bridges[symbol] = bridge
        if gate.enabled:
            logger.warning(
                "TESTNET EXECUTION BRIDGE ENABLED for %s: real LIMIT_MAKER "
                "orders will be placed on Binance Spot TESTNET "
                "(paper ledger remains the strategy ledger)", symbol)
        else:
            logger.info(
                "Testnet execution bridge disabled for %s: %s",
                symbol, " | ".join(gate.reasons))

    if args.once:
        return run_once(cfg, logger, client, symbols, shutdown,
                        bridges=bridges)

    try:
        runtime_cfg = load_runtime_config(cfg)
    except RuntimeConfigError as exc:
        print(f"RUNTIME CONFIG BLOCK: {exc}")
        return 2
    logger.info("RUNTIME LOOP enabled: interval=%ds timeframe=%ds grace=%ds",
                runtime_cfg.interval_seconds, runtime_cfg.timeframe_seconds,
                runtime_cfg.boundary_grace_seconds)
    runtime = GridRuntime(
        runtime_cfg,
        cycle=lambda: run_once(cfg, logger, client, symbols, shutdown,
                               bridges=bridges),
        shutdown=shutdown,
    )
    return runtime.run(max_cycles=args.max_cycles)


if __name__ == "__main__":
    raise SystemExit(main())
