"""Multi-Symbol Adaptive Grid Bot — Main Entry Point.

This module orchestrates the adaptive grid strategy across multiple symbols
independently. Each symbol runs its own complete cycle with isolated state.

Architecture:
- Per-symbol SQLite database (data/grid_bot_{SYMBOL}.sqlite3)
- Per-symbol configuration (grid mode, step, range)
- Shared market data client (Binance API)
- Independent strategy evaluation per symbol

Strategy:
- Auto-entry: ADX < 20 AND (RSI < 35 OR %B <= 0)
- Auto-exit: RSI >= 70 OR ADX > 25 OR %B > 1
- Grid mode: Arithmetic for BTC/ETH/BNB, Geometric for SOL
- Grid step: 1x ATR(14) percentage, min 0.5% gross profit
- Min net profit: 0.3% per grid
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from config_loader import ConfigError, load_config
from fee_model import effective_fees
from grid_engine import (
    GridMode,
    build_grid,
    calculate_dynamic_step_pct,
    validate_grid_profit,
)
from grid_planner import (
    ActivePlan,
    AdaptiveGridPlan,
    PlanBlockReason,
    PlanDecision,
    evaluate_adaptive_grid_plan,
)
from indicators import enrich, latest_valid_row
from market_data import (
    AccountDataError,
    MAX_TICKER_AGE_SECONDS,
    MarketDataError,
    build_account_risk_state,
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
from market_features import (
    CandleValidationError,
    InsufficientDataError,
    MarketFeatures,
    calculate_market_features,
)
from market_regime import MarketRegime, classify_market_regime
from paper_accounting import PaperAccountingEngine
from paper_orchestrator import PaperSession, PaperCycleInput
from grid_lifecycle import LifecycleManager
from range_quality import calculate_range_quality
from profit_model import profit_class
from range_engine import auto_range
from risk_engine import (
    account_state_gate, combine, cooldown_gate, daily_profit_lock, equity_dd_kill,
    equity_reference_gate, inventory_gate, lower_boundary_15m_kill,
    market_gate, open_orders_gate, profit_gate,
    open_orders_available_gate, range_break_kill, strict_order_price_gate,
)
from storage import (
    clear_state,
    connect,
    get_kill_state,
    get_state,
    init_db,
    record_equity,
    record_risk_event,
    record_reference_equity_audit,
    set_state,
)
from shutdown import ShutdownCoordinator
from runstate import persist_run_state, verify_restart_safety
from symbol_rules import parse_symbol_info, validate_quantized_order_plan
from strategy import (
    EntryDecision,
    EntrySignal,
    ExitDecision,
    ExitSignal,
    evaluate_entry_signal,
    evaluate_exit_signal,
    evaluate_strategy,
)

# ---------------------------------------------------------------------------
# Per-symbol database path helper
# ---------------------------------------------------------------------------
def _symbol_db_path(base_path: str, symbol: str) -> str:
    """Generate per-symbol database path."""
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
        # Parse ISO format timestamp
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


def _reset_reference_equity(db_path: str, new_value: Decimal | None, reason: str, actor: str, clear: bool = False) -> Decimal | None:
    if not reason or not str(reason).strip():
        raise ValueError("reset_reference_equity requires a non-empty reason")
    if not actor or not str(actor).strip():
        raise ValueError("reset_reference_equity requires a non-empty actor")

    previous = _load_peak_equity(db_path)
    previous_raw = get_state(db_path, "paper_reference_equity")

    if new_value is None:
        clear_state(db_path, "paper_reference_equity")
    else:
        parsed = Decimal(str(new_value))
        if not parsed.is_finite() or parsed <= 0:
            raise ValueError("new_value must be a finite positive Decimal")
        set_state(db_path, "paper_reference_equity", str(parsed))
        if clear:
            clear_state(db_path, "paper_reference_equity")

    record_reference_equity_audit(
        db_path,
        previous_value=previous_raw,
        new_value=str(new_value) if new_value is not None else "CLEARED",
        reason=str(reason).strip(),
        actor=str(actor).strip(),
    )
    return previous


# ---------------------------------------------------------------------------
# Single-symbol cycle runner
# ---------------------------------------------------------------------------
class SymbolCycleRunner:
    """Runs one complete cycle for a single symbol."""
    
    def __init__(
        self,
        symbol: str,
        cfg: dict[str, Any],
        db_path: str,
        client,
        logger: logging.Logger,
        shutdown: ShutdownCoordinator,
    ):
        self.symbol = symbol
        self.cfg = cfg
        self.db_path = db_path
        self.client = client
        self.logger = logger
        self.shutdown = shutdown
        self.rules = None
        self._init_symbol()
    
    def _init_symbol(self) -> None:
        """Initialize symbol-specific state."""
        symbol_info = fetch_symbol_info(self.client, self.symbol)
        self.rules = parse_symbol_info(symbol_info)
        init_db(self.db_path)
    
    def _get_grid_mode(self) -> GridMode:
        """Get grid mode for this symbol from config."""
        mode_str = self.cfg["grid"]["mode_by_symbol"].get(self.symbol, "geometric")
        return "arithmetic" if mode_str == "arithmetic" else "geometric"
    
    def _get_min_gross_profit(self) -> Decimal:
        return Decimal(str(self.cfg["grid"]["min_gross_profit_pct"]))
    
    def _get_hard_min_net(self) -> Decimal:
        return Decimal(str(self.cfg["grid"]["hard_min_net_pct"]))
    
    def run_cycle(self) -> dict[str, Any]:
        """Execute one complete cycle for this symbol."""
        result = {
            "symbol": self.symbol,
            "success": False,
            "error": None,
            "kill_triggered": False,
            "entry_decision": None,
            "exit_decision": None,
        }
        
        try:
            # Fetch market data
            df = fetch_klines(
                self.client,
                self.symbol,
                self.cfg["timeframe"],
                self.cfg["range"]["lookback"],
                drop_incomplete=True,
            )
            enriched = enrich(df)
            last = latest_valid_row(enriched)
            
            # Fetch ticker
            ticker = fetch_ticker_price(self.client, self.symbol)
            if not is_ticker_fresh(ticker, MAX_TICKER_AGE_SECONDS):
                raise MarketDataError(f"Ticker price is stale for {ticker.symbol}")
            current_price = ticker.price
            
            # Fetch book ticker for market intelligence
            book_quote = None
            if "market_intelligence" in self.cfg:
                try:
                    book_quote = fetch_book_ticker(self.client, self.symbol)
                    max_quote_age = int(
                        self.cfg["market_intelligence"].get("liquidity", {}).get(
                            "max_quote_ticker_age_seconds", 10
                        )
                    )
                    if not is_quote_fresh(book_quote, max_quote_age):
                        raise MarketDataError(f"Book ticker quote is stale for {self.symbol}")
                except Exception as exc:
                    self.logger.warning("BOOK TICKER UNAVAILABLE for %s: %s", self.symbol, exc)
            
            # Calculate market features
            range_cfg = self.cfg["range"]
            if range_cfg["mode"] == "manual":
                lower = Decimal(str(range_cfg["lower_price"]))
                upper = Decimal(str(range_cfg["upper_price"]))
            else:
                candidate = auto_range(enriched, **range_cfg["auto"])
                lower, upper = candidate.lower, candidate.upper
            
            # Market intelligence
            mi_decision = None
            regime = MarketRegime.RANGE
            range_quality_score = Decimal("80")
            if "market_intelligence" in self.cfg:
                try:
                    features = calculate_market_features(
                        df=df,
                        quote=book_quote,
                        lower_price=lower,
                        upper_price=upper,
                        symbol=self.symbol,
                        config=self.cfg,
                    )
                    regime, _ = classify_market_regime(features, self.cfg)
                    quality_res = calculate_range_quality(features, self.cfg)
                    range_quality_score = quality_res.score
                    from grid_eligibility import evaluate_grid_eligibility
                    mi_decision = evaluate_grid_eligibility(
                        features=features,
                        regime=regime,
                        range_quality=quality_res,
                        current_price=current_price,
                        lower_price=lower,
                        upper_price=upper,
                        config=self.cfg,
                    )
                except Exception as exc:
                    self.logger.warning("MARKET INTELLIGENCE ERROR for %s: %s", self.symbol, exc)
            
            # ================================================================
            # NEW STRATEGY: Auto-Entry / Auto-Exit Evaluation
            # ================================================================
            has_active_grid = False
            active_plan = None
            lifecycle = LifecycleManager(self.db_path)
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
            
            # Evaluate entry/exit signals using unified strategy function
            entry_decision = None
            exit_decision = None
            cooldown_hours = int(self.cfg.get("strategy", {}).get("cooldown_hours", 3))
            in_cooldown = _is_in_cooldown(self.db_path, cooldown_hours)
            
            if "market_intelligence" in self.cfg:
                try:
                    features = calculate_market_features(
                        df=df,
                        quote=book_quote,
                        lower_price=lower,
                        upper_price=upper,
                        symbol=self.symbol,
                        config=self.cfg,
                    )
                    entry_decision, exit_decision = evaluate_strategy(
                        features=features,
                        config=self.cfg,
                        has_active_grid=has_active_grid,
                        in_cooldown=in_cooldown,
                    )
                    
                    if entry_decision:
                        result["entry_decision"] = {
                            "signal": entry_decision.signal.value,
                            "allowed": entry_decision.allowed,
                            "reasons": entry_decision.reasons,
                            "adx": str(entry_decision.adx),
                            "rsi": str(entry_decision.rsi),
                            "percent_b": str(entry_decision.percent_b),
                            "volume_oscillator": str(entry_decision.volume_oscillator),
                        }
                    if exit_decision:
                        result["exit_decision"] = {
                            "signal": exit_decision.signal.value,
                            "should_exit": exit_decision.should_exit,
                            "triggered_reasons": exit_decision.triggered_reasons,
                            "adx": str(exit_decision.adx),
                            "rsi": str(exit_decision.rsi),
                            "percent_b": str(exit_decision.percent_b),
                            "z_score": str(exit_decision.z_score),
                        }
                except Exception as exc:
                    self.logger.warning("STRATEGY EVALUATION ERROR for %s: %s", self.symbol, exc)
            
            # Auto-exit takes priority: liquidate if exit triggered
            if exit_decision and exit_decision.should_exit:
                self.logger.warning(
                    "AUTO-EXIT TRIGGERED for %s: %s", self.symbol, exit_decision.triggered_reasons
                )
                # Record the auto-exit timestamp for cooldown
                _record_auto_exit_ts(self.db_path)
                # Activate kill state to liquidate
                from main import _activate_kill_state  # reuse existing kill logic
                report = _activate_kill_state(
                    self.db_path, self.cfg, self.rules,
                    " | ".join(exit_decision.triggered_reasons),
                    note=f"auto-exit: {exit_decision.triggered_reasons}",
                )
                result["kill_triggered"] = True
                result["exit_reasons"] = exit_decision.triggered_reasons
                return result
            
            # Auto-entry: only proceed if entry allowed
            if not has_active_grid:
                if entry_decision and not entry_decision.allowed:
                    self.logger.info(
                        "AUTO-ENTRY BLOCKED for %s: %s", self.symbol, entry_decision.reasons
                    )
                    result["entry_blocked"] = True
                    result["entry_reasons"] = entry_decision.reasons
                    return result
                elif entry_decision and entry_decision.allowed:
                    self.logger.info("AUTO-ENTRY ALLOWED for %s", self.symbol)
            
            # ================================================================
            # Continue with existing grid logic if entry allowed or grid active
            # ================================================================
            
            # Fetch account data
            account_snapshot = fetch_account_snapshot(self.client, self.rules.base_asset, self.rules.quote_asset)
            account_risk = None
            account_error = None
            if account_snapshot is not None:
                reference_equity = _load_peak_equity(self.db_path)
                try:
                    account_risk = build_account_risk_state(account_snapshot, current_price, reference_equity)
                    raw_reference = get_state(self.db_path, "paper_reference_equity")
                    if raw_reference is None or reference_equity is not None:
                        _record_peak_equity(self.db_path, account_risk.current_equity)
                    try:
                        record_equity(self.db_path, account_risk.current_equity, account_risk.drawdown_pct)
                    except Exception:
                        pass
                except AccountDataError as exc:
                    account_error = str(exc)
            
            # Fetch fees
            commission_payload, _ = fetch_account_commission(self.client, self.symbol)
            fees = effective_fees(
                commission_payload,
                self.cfg["fees"]["maker_fee_fallback"],
                self.cfg["fees"]["taker_fee_fallback"],
            )
            
            # Dynamic grid step based on ATR
            atr_pct = Decimal(str(last.get("atr_pct", "0.01")))
            min_gross = self._get_min_gross_profit()
            dynamic_step = calculate_dynamic_step_pct(
                atr_pct,
                min_gross,
                fees.maker,
                fees.taker,
                Decimal(str(self.cfg["fees"]["slippage_roundtrip_pct"])),
            )
            
            # Build grid
            grid_mode = self._get_grid_mode()
            grid_result = build_grid(
                lower, upper, dynamic_step, grid_mode,
                min_cells=int(self.cfg["grid"]["min_cells"]),
                max_levels=int(self.cfg["grid"]["max_levels"]),
            )
            
            # Validate grid profit
            sell_fee = fees.maker if self.cfg["execution"]["prefer_limit_maker"] else fees.taker
            validation = validate_grid_profit(
                grid_result.levels, fees.maker, sell_fee,
                Decimal(str(self.cfg["fees"]["slippage_roundtrip_pct"])),
                self._get_hard_min_net(),
            )
            plan_validation = validate_quantized_order_plan(
                grid_result.levels,
                self.rules,
                Decimal(str(self.cfg["execution"]["order_quote_size"])),
                current_price,
                fees.maker,
                sell_fee,
                Decimal(str(self.cfg["fees"]["slippage_roundtrip_pct"])),
                self._get_hard_min_net(),
                int(self.cfg["execution"]["max_open_orders"]),
            )
            
            # Risk gates
            last_closed_close = self._latest_closed_candle_close(df)
            decisions = [
                profit_gate(
                    plan_validation.min_net_pct if plan_validation else Decimal("0"),
                    self._get_hard_min_net(),
                ),
                market_gate(last, self.cfg["market_filter"]),
                strict_order_price_gate(lower, grid_result.effective_upper, current_price),
                range_break_kill(lower, grid_result.effective_upper, current_price, self.cfg["risk"]["range_break_buffer_pct"]),
                lower_boundary_15m_kill(
                    last_closed_close,
                    lower,
                    self.cfg["risk"]["stop_if_below_lower_pct"],
                ),
                cooldown_gate(False),
                daily_profit_lock(Decimal("0"), self.cfg["risk"]["daily_profit_lock_pct"]),
                equity_reference_gate(
                    get_state(self.db_path, "paper_reference_equity"),
                    _load_peak_equity(self.db_path),
                ),
            ]
            if account_risk:
                decisions.extend([
                    equity_dd_kill(account_risk.drawdown_pct, self.cfg["risk"]["max_equity_drawdown_pct"]),
                    inventory_gate(account_risk.inventory_pct, self.cfg["execution"]["max_inventory_pct"]),
                ])
            else:
                decisions.append(account_state_gate(False))
            
            # Fetch open orders for reconciliation
            open_orders = fetch_open_orders(self.client, self.symbol)
            if open_orders is None:
                decisions.append(open_orders_available_gate(False))
            else:
                decisions.extend((
                    open_orders_available_gate(True),
                    open_orders_gate(len(open_orders), self.cfg["execution"]["max_open_orders"]),
                ))
            
            combined = combine(*decisions)
            
            # Apply strategy gates
            if not has_active_grid:
                if entry_decision and not entry_decision.allowed:
                    combined = combine(combined, type(combined)(False, ("ENTRY_BLOCKED",)))
            
            if exit_decision and exit_decision.should_exit:
                combined = combine(combined, type(combined)(False, ("EXIT_TRIGGERED",)))
            
            # Kill trigger detection
            kill_triggers = [
                r for r in combined.reasons
                if r in {
                    "EQUITY_DRAWDOWN_KILL",
                    "RANGE_BREAK_BELOW_BUFFER",
                    "RANGE_BREAK_ABOVE_BUFFER",
                    "LOWER_BOUNDARY_STOP_15M",
                    "EXIT_TRIGGERED",
                }
            ]
            if kill_triggers:
                from main import _activate_kill_state
                report = _activate_kill_state(
                    self.db_path, self.cfg, self.rules,
                    " | ".join(kill_triggers),
                    note=f"price={current_price} range={lower}->{grid_result.effective_upper}",
                )
                result["kill_triggered"] = True
            
            # Record risk event
            record_risk_event(self.db_path, combined.allowed, combined.reason, {
                "symbol": self.symbol,
                "price": str(current_price),
                "range": [str(lower), str(grid_result.effective_upper)],
                "grid_mode": grid_mode,
                "dynamic_step_pct": str(dynamic_step),
                "atr_pct": str(atr_pct),
                "grid_cells": grid_result.cells,
                "min_net_pct": str(plan_validation.min_net_pct if plan_validation else Decimal("0")),
            })
            
            # Persist state
            set_state(self.db_path, "last_symbol", self.symbol)
            set_state(self.db_path, "last_price", str(current_price))
            set_state(self.db_path, "last_range", {"lower": str(lower), "upper": str(grid_result.effective_upper)})
            set_state(self.db_path, "last_risk_decision", {"allowed": combined.allowed, "reason": combined.reason})
            
            # Run paper cycle if allowed
            if combined.allowed and plan_validation and not self.shutdown.is_requested:
                kill_now = get_kill_state(self.db_path)
                if not (kill_now and kill_now.get("active")):
                    cycle_candle_index = int(get_state(self.db_path, "paper_cycle_index") or "0") + 1
                    set_state(self.db_path, "paper_cycle_index", str(cycle_candle_index))
                    
                    accounting = PaperAccountingEngine(
                        self.rules.base_asset,
                        self.rules.quote_asset,
                        Decimal(str(self.cfg["paper"]["initial_base_balance"])),
                        Decimal(str(self.cfg["paper"]["initial_quote_balance"])),
                        Decimal(str(self.cfg["paper"]["maker_fee"])),
                        Decimal(str(self.cfg["paper"]["taker_fee"])),
                        str(self.cfg["paper"]["fee_asset"]),
                    )
                    session = PaperSession(self.db_path, self.db_path, accounting, client_order_prefix=f"AG{self.symbol[:3]}")
                    cycle_input = PaperCycleInput(
                        candle_index=cycle_candle_index,
                        symbol=self.symbol,
                        current_price=current_price,
                        kline_df=df,
                        quote=None,
                        lower_price=lower,
                        upper_price=upper,
                        active_plan=active_plan,
                        regime=regime,
                        range_quality_score=range_quality_score,
                        cfg=self.cfg,
                        maker_fee=fees.maker,
                        taker_fee=sell_fee,
                        fee_asset=str(self.cfg["paper"]["fee_asset"]),
                        risk_decision=combined,
                        clock=lambda: datetime.now(timezone.utc),
                        dry_run=True,
                        rules=self.rules,
                    )
                    cycle_result = session.run_cycle(cycle_input)
                    result["cycle_result"] = {
                        "orders_submitted": cycle_result.orders_submitted,
                        "fills_applied": cycle_result.fills_applied,
                        "success": cycle_result.success,
                    }
            
            result["success"] = True
            result["combined_allowed"] = combined.allowed
            result["combined_reason"] = combined.reason
            result["grid_cells"] = grid_result.cells
            result["dynamic_step_pct"] = str(dynamic_step)
            result["current_price"] = str(current_price)
            result["range"] = [str(lower), str(grid_result.effective_upper)]
            
        except Exception as exc:
            self.logger.error("CYCLE ERROR for %s: %s", self.symbol, exc)
            result["error"] = str(exc)
        
        return result

    def _latest_closed_candle_close(self, kline_df) -> Decimal | None:
        if kline_df is None or not hasattr(kline_df, "empty") or kline_df.empty:
            return None
        if "close" not in getattr(kline_df, "columns", []):
            return None
        try:
            raw = kline_df["close"].iloc[-1]
            value = Decimal(str(raw))
        except Exception:
            return None
        try:
            if not value.is_finite() or value <= 0:
                return None
        except Exception:
            return None
        return value


# ---------------------------------------------------------------------------
# Multi-symbol main
# ---------------------------------------------------------------------------
def main() -> int:
    load_dotenv()
    
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"CONFIG BLOCK: {exc}")
        return 2
    
    if not cfg["environment"]["dry_run"]:
        raise RuntimeError("DRY_RUN must remain enabled; live execution is disabled")
    
    # Get Binance environment and credentials
    binance_env = os.getenv("BINANCE_ENV", "testnet").strip().lower()
    if binance_env == "live":
        api_key = os.getenv("BINANCE_LIVE_API_KEY", "")
        api_secret = os.getenv("BINANCE_LIVE_API_SECRET", "")
    else:
        api_key = os.getenv("BINANCE_TESTNET_API_KEY", "")
        api_secret = os.getenv("BINANCE_TESTNET_API_SECRET", "")
    
    # Setup logging
    log_path = cfg["logging"]["log_path"]
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("adaptive_grid_multi")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        sh = logging.StreamHandler()
        sh.setFormatter(formatter)
        logger.addHandler(sh)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    
    # Create client
    client = make_client(binance_env, api_key, api_secret)
    
    # Get symbols
    symbols = cfg["_parsed_symbols"]
    base_db_path = cfg["logging"]["sqlite_path"]
    
    logger.info("=== Multi-Symbol Adaptive Grid Bot Started ===")
    logger.info("Mode=%s dry_run=%s symbols=%s timeframe=%s", 
                binance_env, cfg["environment"]["dry_run"], symbols, cfg["timeframe"])
    
    # Run cycle for each symbol
    shutdown = ShutdownCoordinator()
    all_results = []
    
    for symbol in symbols:
        if shutdown.is_requested:
            logger.warning("Shutdown requested, stopping cycle loop")
            break
        
        symbol_db = _symbol_db_path(base_db_path, symbol)
        Path(symbol_db).parent.mkdir(parents=True, exist_ok=True)
        
        runner = SymbolCycleRunner(symbol, cfg, symbol_db, client, logger, shutdown)
        result = runner.run_cycle()
        all_results.append(result)
        
        # Log summary
        if result["success"]:
            logger.info(
                "%s: price=%s range=%s->%s grid_cells=%d step=%.4f%% allowed=%s reason=%s",
                symbol,
                result["current_price"],
                result["range"][0],
                result["range"][1],
                result["grid_cells"],
                float(result["dynamic_step_pct"]) * 100,
                result["combined_allowed"],
                result["combined_reason"],
            )
            if result.get("entry_decision"):
                logger.info("%s: ENTRY %s reasons=%s", symbol, result["entry_decision"]["signal"], result["entry_decision"]["reasons"])
            if result.get("exit_decision"):
                logger.info("%s: EXIT %s reasons=%s", symbol, result["exit_decision"]["signal"], result["exit_decision"]["triggered_reasons"])
            if result.get("kill_triggered"):
                logger.warning("%s: KILL TRIGGERED", symbol)
        else:
            logger.error("%s: CYCLE FAILED: %s", symbol, result["error"])
    
    # Print summary
    print("\n=== MULTI-SYMBOL CYCLE SUMMARY ===")
    for r in all_results:
        sym = r["symbol"]
        if r["success"]:
            status = "OK" if r["combined_allowed"] else "BLOCKED"
            print(f"  {sym}: {status} | price={r['current_price']} | grid={r['grid_cells']} cells @ {float(r['dynamic_step_pct'])*100:.4f}% | reason={r['combined_reason']}")
            if r.get("entry_decision"):
                print(f"    ENTRY: {r['entry_decision']['signal']} ({r['entry_decision']['reasons']})")
            if r.get("exit_decision"):
                print(f"    EXIT: {r['exit_decision']['signal']} ({r['exit_decision']['triggered_reasons']})")
            if r.get("kill_triggered"):
                print(f"    KILL: ACTIVE")
        else:
            print(f"  {sym}: ERROR - {r['error']}")
    
    shutdown.complete()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())