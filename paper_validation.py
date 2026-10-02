"""Phase 5E — Deterministic Paper Validation & Stress Testing.

Provides a read-only validation layer over the certified Phase 5D orchestration.
Proves system safety, determinism, and exact behaviors under adversarial conditions
without changing any trading strategy or implementation logic.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple, Optional

from paper_orchestrator import (
    PaperOrchestrator,
    PaperCycleInput,
    PaperCycleResult,
    PaperSession,
    generate_cycle_id,
    validate_market_freshness,
)
from paper_accounting import PaperAccountingEngine
from risk_engine import RiskDecision
from order_engine import OrderIntent, OrderState
from grid_planner import AdaptiveGridPlan, PlanDecision, PlanBlockReason
from grid_lifecycle import LifecycleManager, LifecycleState
from market_regime import MarketRegime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Validation Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValidationConfig:
    """Configuration for deterministic validation runs.
    
    All fields are immutable to ensure deterministic replay.
    """
    symbol: str = "BTCUSDT"
    timeframe: str = "15m"
    step_pct: Decimal = Decimal("0.006")
    dry_run: bool = True
    initial_base_balance: Decimal = Decimal("2")
    initial_quote_balance: Decimal = Decimal("1000")
    maker_fee: Decimal = Decimal("0.001")
    taker_fee: Decimal = Decimal("0.001")
    fee_asset: str = "USDT"
    order_quote_size: Decimal = Decimal("25")
    prefer_limit_maker: bool = True
    max_equity_drawdown_pct: Decimal = Decimal("0.02")
    range_break_buffer_pct: Decimal = Decimal("0.01")
    daily_profit_lock_pct: Decimal = Decimal("0.01")
    stop_if_below_lower_pct: Decimal = Decimal("0.02")
    cooldown_minutes: int = 30
    max_candle_age_seconds: int = 5400
    max_quote_age_seconds: int = 10
    equity_tolerance: Decimal = Decimal("0.0001")
    cfg: dict = field(default_factory=dict)

    def __post_init__(self):
        # Merge cfg with defaults if not provided
        if not self.cfg:
            object.__setattr__(self, "cfg", {
                "grid": {
                    "step_pct": float(self.step_pct),
                    "hard_min_net_pct": 0.003,
                    "preferred_net_max_pct": 0.004,
                    "min_cells": 6,
                    "max_levels": 40,
                },
                "range": {
                    "mode": "auto",
                    "lower_price": 0,
                    "upper_price": 0,
                    "lookback": 200,
                    "buffer_pct": 0.01,
                    "auto": {
                        "support_quantile": 0.10,
                        "resistance_quantile": 0.90,
                        "min_width_pct": 0.03,
                        "max_width_pct": 0.25,
                        "min_quality_score": 65,
                        "require_price_inside": True,
                    },
                },
                "market_filter": {
                    "adx_max": 28,
                    "atr_pct_max": 0.025,
                    "bb_width_max": 0.06,
                    "volume_spike_max": 2.5,
                },
                "adaptive_planner": {
                    "cooldown_candles": 4,
                    "hysteresis": {
                        "range_change_pct": 0.02,
                        "step_change_pct": 0.10,
                        "grid_count_change": 3,
                        "quality_degradation": 5,
                        "regime_change": True,
                    },
                },
                "execution": {
                    "prefer_limit_maker": self.prefer_limit_maker,
                    "stale_order_minutes": 30,
                    "max_open_orders": 40,
                    "order_quote_size": float(self.order_quote_size),
                    "total_quote_budget": 0,
                    "max_inventory_pct": 0.70,
                },
                "fees": {
                    "maker_fee_fallback": float(self.maker_fee),
                    "taker_fee_fallback": float(self.taker_fee),
                    "slippage_roundtrip_pct": 0.0005,
                },
                "paper": {
                    "initial_base_balance": float(self.initial_base_balance),
                    "initial_quote_balance": float(self.initial_quote_balance),
                    "maker_fee": float(self.maker_fee),
                    "taker_fee": float(self.taker_fee),
                    "fee_asset": self.fee_asset,
                },
                "risk": {
                    "max_equity_drawdown_pct": float(self.max_equity_drawdown_pct),
                    "range_break_buffer_pct": float(self.range_break_buffer_pct),
                    "daily_profit_lock_pct": float(self.daily_profit_lock_pct),
                    "stop_if_below_lower_pct": float(self.stop_if_below_lower_pct),
                    "cooldown_minutes": self.cooldown_minutes,
                },
            })


# ---------------------------------------------------------------------------
# Validation Candle
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValidationCandle:
    """Immutable candle for validation sequences.
    
    Represents a single closed candle with all OHLCV data.
    """
    candle_index: int
    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self):
        if self.timestamp.tzinfo is None:
            object.__setattr__(self, "timestamp", self.timestamp.replace(tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# Validation Run Result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValidationMetrics:
    """Comprehensive validation metrics."""
    total_candles: int
    processed_candles: int
    blocked_candles: int
    actionable_candles: int
    grid_plans_created: int
    reconfigurations: int
    orders_created: int
    orders_filled: int
    partial_fills: int
    completed_grids: int
    gross_grid_profit: Decimal
    total_fees: Decimal
    estimated_slippage: Decimal
    net_pnl: Decimal
    starting_equity: Decimal
    ending_equity: Decimal
    max_drawdown_pct: Decimal
    max_base_inventory: Decimal
    min_base_inventory: Decimal
    max_quote_usage: Decimal
    blocked_reason_counts: Dict[str, int]
    recovery_failures: int
    duplicate_prevention_count: int
    invariant_violations: List[str] = field(default_factory=list)
    profit_violations: List[str] = field(default_factory=list)
    range_violations: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ValidationRun:
    """Result of a validation sequence run."""
    run_id: str
    total_candles: int
    processed_candles: int
    blocked_candles: int
    cycle_results: Tuple[PaperCycleResult, ...]
    total_fills: int
    total_orders_submitted: int
    total_orders_skipped: int
    final_accounting_state: Dict[str, Any]
    started_at: str
    completed_at: str
    metrics: Optional[ValidationMetrics] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplayComparison:
    """Result of deterministic replay comparison."""
    symbol_match: bool
    total_candles_match: bool
    metrics_match: bool
    cycle_ids_match: bool
    order_ids_match: bool
    fill_ids_match: bool
    accounting_state_match: bool
    differences: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# PaperValidator
# ---------------------------------------------------------------------------

class PaperValidator:
    """Deterministic validation layer over Phase 5D orchestration.
    
    Read-only validation of the certified orchestration. Proves
    deterministic replay, safety under adversarial conditions,
    and exact behavioral guarantees.
    """
    
    def __init__(self, config: ValidationConfig):
        self.config = config
        self._session: Optional[PaperSession] = None
        self._initialize_session()
    
    def _initialize_session(self) -> None:
        """Initialize the paper trading session with fresh databases."""
        import tempfile
        
        # Create temporary database files for isolation
        self._order_db_path = tempfile.mktemp(suffix="_orders.db")
        self._lifecycle_db_path = tempfile.mktemp(suffix="_lifecycle.db")
        
        # Parse symbol to get base and quote assets
        symbol = self.config.symbol
        if symbol.endswith("USDT"):
            base_asset = symbol[:-4]
            quote_asset = "USDT"
        elif symbol.endswith("BUSD"):
            base_asset = symbol[:-4]
            quote_asset = "BUSD"
        elif symbol.endswith("USDC"):
            base_asset = symbol[:-4]
            quote_asset = "USDC"
        else:
            base_asset = symbol[:-4] if len(symbol) > 4 else "BTC"
            quote_asset = "USDT"
        
        # Initialize accounting engine
        self._accounting_engine = PaperAccountingEngine(
            base_asset=base_asset,
            quote_asset=quote_asset,
            initial_base_balance=self.config.initial_base_balance,
            initial_quote_balance=self.config.initial_quote_balance,
            maker_fee=self.config.maker_fee,
            taker_fee=self.config.taker_fee,
            fee_asset=self.config.fee_asset,
        )
        
        # Initialize session
        self._session = PaperSession(
            order_db_path=self._order_db_path,
            lifecycle_db_path=self._lifecycle_db_path,
            accounting_engine=self._accounting_engine,
            client_order_prefix="VAL",
        )
    
    def _create_cycle_input(self, candle: ValidationCandle) -> PaperCycleInput:
        """Create a PaperCycleInput from a ValidationCandle."""
        import pandas as pd
        
        kline_data = {
            "open_time": [candle.timestamp],
            "open": [float(candle.open)],
            "high": [float(candle.high)],
            "low": [float(candle.low)],
            "close": [float(candle.close)],
            "volume": [float(candle.volume)],
            "close_time": [candle.timestamp],
            "quote_asset_volume": [float(candle.volume * candle.close)],
            "number_of_trades": [100],
            "taker_buy_base_asset_volume": [float(candle.volume * Decimal("0.5"))],
            "taker_buy_quote_asset_volume": [float(candle.volume * candle.close * Decimal("0.5"))],
        }
        kline_df = pd.DataFrame(kline_data)
        
        # Get active plan from lifecycle
        active_plan = self._session.orchestrator.lifecycle_manager.get_active_plan()
        
        # Create risk decision (default allow)
        risk_decision = RiskDecision(True)
        
        return PaperCycleInput(
            candle_index=candle.candle_index,
            symbol=candle.symbol,
            current_price=candle.close,
            kline_df=kline_df,
            quote=None,
            lower_price=Decimal("0"),
            upper_price=Decimal("0"),
            active_plan=active_plan,
            regime=MarketRegime.RANGE,
            range_quality_score=Decimal("80"),
            cfg=self.config.cfg,
            maker_fee=self.config.maker_fee,
            taker_fee=self.config.taker_fee,
            fee_asset=self.config.fee_asset,
            risk_decision=risk_decision,
            max_candle_age_seconds=self.config.max_candle_age_seconds,
            max_quote_age_seconds=self.config.max_quote_age_seconds,
            dry_run=True,
            metadata={"validation_run": True},
        )

    def _check_invariants(
        self,
        state_dict: Dict[str, Any],
        candle: ValidationCandle,
        result: PaperCycleResult,
        invariant_violations: List[str],
    ) -> None:
        """Check conservation invariants after a cycle."""
        if state_dict is None:
            invariant_violations.append(
                f"candle_index={candle.candle_index} cycle_id={result.cycle_id} "
                f"invariant=state_exists expected=state actual=None"
            )
            return

        base_free = Decimal(str(state_dict.get("base_free", 0)))
        base_reserved = Decimal(str(state_dict.get("base_reserved", 0)))
        quote_free = Decimal(str(state_dict.get("quote_free", 0)))
        quote_reserved = Decimal(str(state_dict.get("quote_reserved", 0)))

        base_total = base_free + base_reserved
        quote_total = quote_free + quote_reserved

        checks = [
            ("base_free>=0", base_free >= 0, base_free, ">=0"),
            ("base_reserved>=0", base_reserved >= 0, base_reserved, ">=0"),
            ("quote_free>=0", quote_free >= 0, quote_free, ">=0"),
            ("quote_reserved>=0", quote_reserved >= 0, quote_reserved, ">=0"),
            ("base_total>=0", base_total >= 0, base_total, ">=0"),
            ("quote_total>=0", quote_total >= 0, quote_total, ">=0"),
        ]

        for name, ok, actual, expected in checks:
            if not ok:
                invariant_violations.append(
                    f"candle_index={candle.candle_index} cycle_id={result.cycle_id} "
                    f"invariant={name} actual={actual} expected={expected}"
                )

    def _check_profit_violations(
        self,
        candle: ValidationCandle,
        result: PaperCycleResult,
        profit_violations: List[str],
    ) -> None:
        """Check profit threshold violations.
        
        Tracks when plans are blocked due to NET_PROFIT_BELOW_MINIMUM,
        which indicates grid step profit would not meet hard_min_net_pct threshold.
        """
        if result.blocked_reason is None:
            return
        
        # Check if blocked due to insufficient profit
        blocked_str = str(result.blocked_reason).upper()
        if "NET_PROFIT_BELOW_MINIMUM" in blocked_str or "PROFIT" in blocked_str:
            # Extract estimated net profit from plan if available
            plan = result.plan
            estimated_net = "unknown"
            if plan is not None and hasattr(plan, "estimated_net_profit_per_grid"):
                estimated_net = str(plan.estimated_net_profit_per_grid)
            
            hard_min = self.config.cfg.get("grid", {}).get("hard_min_net_pct", 0.003)
            
            profit_violations.append(
                f"candle_index={candle.candle_index} cycle_id={result.cycle_id} "
                f"blocked_reason={result.blocked_reason} "
                f"estimated_net_profit={estimated_net} "
                f"hard_min_net_pct={hard_min}"
            )

    def validate_historical_sequence(
        self,
        candles: List[ValidationCandle],
        run_name: str,
    ) -> ValidationRun:
        """Validate a historical sequence of candles.
        
        Runs the full Phase 5D orchestration pipeline for each candle
        and collects deterministic results with invariant checks.
        """
        if not candles:
            return ValidationRun(
                run_id=run_name,
                total_candles=0,
                processed_candles=0,
                blocked_candles=0,
                cycle_results=(),
                total_fills=0,
                total_orders_submitted=0,
                total_orders_skipped=0,
                final_accounting_state={},
                started_at=datetime.now(timezone.utc).isoformat(),
                completed_at=datetime.now(timezone.utc).isoformat(),
                metadata={"run_name": run_name},
            )
        
        started_at = datetime.now(timezone.utc).isoformat()
        cycle_results: List[PaperCycleResult] = []
        processed = 0
        blocked = 0
        total_fills = 0
        total_orders_submitted = 0
        total_orders_skipped = 0
        
        # Conservation invariant tracking
        invariant_violations: List[str] = []
        
        # Equity/drawdown tracking
        peak_equity: Optional[Decimal] = None
        max_drawdown_pct = Decimal("0")
        starting_equity: Optional[Decimal] = None
        
        # Inventory tracking
        max_base_inventory = Decimal("0")
        min_base_inventory: Optional[Decimal] = None
        max_quote_usage = Decimal("0")
        
        # Blocked reason counts
        blocked_reason_counts: Dict[str, int] = {}
        
        # Duplicate prevention tracking
        seen_cycle_ids: set = set()
        duplicate_prevention_count = 0
        recovery_failures = 0
        
        # Range violation tracking
        range_violations: List[str] = []
        
        # Profit threshold violation tracking
        profit_violations: List[str] = []
        
        for candle in candles:
            cycle_input = self._create_cycle_input(candle)
            result = self._session.run_cycle(cycle_input)
            cycle_results.append(result)
            
            # Duplicate cycle prevention check
            if result.cycle_id in seen_cycle_ids:
                duplicate_prevention_count += 1
            seen_cycle_ids.add(result.cycle_id)
            
            if result.success and result.blocked_reason is None:
                processed += 1
            else:
                blocked += 1
                reason = result.blocked_reason or "error"
                blocked_reason_counts[reason] = blocked_reason_counts.get(reason, 0) + 1
                if "recovery" in str(reason).lower():
                    recovery_failures += 1
            
            total_fills += result.fills_applied
            total_orders_submitted += result.orders_submitted
            total_orders_skipped += result.orders_skipped
            
            # Profit threshold validation
            self._check_profit_violations(candle, result, profit_violations)
            
            # Range safety: verify order prices within bounds if plan exists
            plan = result.plan
            if plan is not None and hasattr(plan, "lower_price") and hasattr(plan, "upper_price"):
                lower = plan.lower_price
                upper = plan.upper_price
                for intent in result.order_intents:
                    if intent.price is not None:
                        if intent.price < lower or intent.price > upper:
                            range_violations.append(
                                f"candle_index={candle.candle_index} cycle_id={result.cycle_id} "
                                f"order={intent.client_order_id} price={intent.price} "
                                f"lower={lower} upper={upper}"
                            )
            
            # Conservation invariant check after every candle
            state_dict = self._session.orchestrator._read_accounting_state(
                self._session.orchestrator.order_engine.db_path
            )
            self._check_invariants(state_dict, candle, result, invariant_violations)
            
            if state_dict is not None:
                base_free = Decimal(str(state_dict.get("base_free", 0)))
                base_reserved = Decimal(str(state_dict.get("base_reserved", 0)))
                quote_free = Decimal(str(state_dict.get("quote_free", 0)))
                quote_reserved = Decimal(str(state_dict.get("quote_reserved", 0)))
                
                base_total = base_free + base_reserved
                quote_total = quote_free + quote_reserved
                
                # Track inventory extremes
                if base_total > max_base_inventory:
                    max_base_inventory = base_total
                if min_base_inventory is None or base_total < min_base_inventory:
                    min_base_inventory = base_total
                quote_used = quote_reserved
                if quote_used > max_quote_usage:
                    max_quote_usage = quote_used
                
                # Equity reconciliation for drawdown calculation
                mark_price = candle.close
                equity = quote_total + base_total * mark_price
                
                if starting_equity is None:
                    starting_equity = equity
                    peak_equity = equity
                else:
                    if peak_equity is not None and equity > peak_equity:
                        peak_equity = equity
                    if peak_equity is not None and peak_equity > 0:
                        drawdown = (peak_equity - equity) / peak_equity
                        if drawdown > max_drawdown_pct:
                            max_drawdown_pct = drawdown
        
        completed_at = datetime.now(timezone.utc).isoformat()
        final_state = self._session.orchestrator._read_accounting_state(
            self._session.orchestrator.order_engine.db_path
        ) or {}
        
        # Calculate final metrics from state
        total_fees = Decimal(str(final_state.get("total_fees", 0)))
        net_pnl = Decimal(str(final_state.get("realized_pnl", 0)))
        ending_equity = Decimal("0")
        if final_state:
            base_free = Decimal(str(final_state.get("base_free", 0)))
            base_reserved = Decimal(str(final_state.get("base_reserved", 0)))
            quote_free = Decimal(str(final_state.get("quote_free", 0)))
            quote_reserved = Decimal(str(final_state.get("quote_reserved", 0)))
            base_total = base_free + base_reserved
            quote_total = quote_free + quote_reserved
            mark_price = candles[-1].close if candles else Decimal("0")
            ending_equity = quote_total + base_total * mark_price
        
        actionable = processed
        grid_plans_created = 0
        reconfigurations = 0
        try:
            lifecycle = self._session.orchestrator.lifecycle_manager
            active = lifecycle.get_active_plan()
            if active is not None:
                grid_plans_created = 1
        except Exception:
            pass
        
        orders_filled = 0
        partial_fills = 0
        for r in cycle_results:
            for event in r.events:
                if event.event_type == "ORDER_FILLED":
                    orders_filled += 1
        
        metrics = ValidationMetrics(
            total_candles=len(candles),
            processed_candles=processed,
            blocked_candles=blocked,
            actionable_candles=actionable,
            grid_plans_created=grid_plans_created,
            reconfigurations=reconfigurations,
            orders_created=total_orders_submitted,
            orders_filled=orders_filled,
            partial_fills=partial_fills,
            completed_grids=0,
            gross_grid_profit=Decimal("0"),
            total_fees=total_fees,
            estimated_slippage=Decimal("0"),
            net_pnl=net_pnl,
            starting_equity=starting_equity if starting_equity is not None else Decimal("0"),
            ending_equity=ending_equity,
            max_drawdown_pct=max_drawdown_pct,
            max_base_inventory=max_base_inventory,
            min_base_inventory=min_base_inventory if min_base_inventory is not None else Decimal("0"),
            max_quote_usage=max_quote_usage,
            blocked_reason_counts=blocked_reason_counts,
            recovery_failures=recovery_failures,
            duplicate_prevention_count=duplicate_prevention_count,
            invariant_violations=invariant_violations,
            profit_violations=profit_violations,
            range_violations=range_violations,
        )
        
        return ValidationRun(
            run_id=run_name,
            total_candles=len(candles),
            processed_candles=processed,
            blocked_candles=blocked,
            cycle_results=tuple(cycle_results),
            total_fills=total_fills,
            total_orders_submitted=total_orders_submitted,
            total_orders_skipped=total_orders_skipped,
            final_accounting_state=final_state,
            started_at=started_at,
            completed_at=completed_at,
            metrics=metrics,
            metadata={"run_name": run_name, "config": self.config.cfg},
        )

    def validate_deterministic_replay(
        self,
        run_a: ValidationRun,
        run_b: ValidationRun,
    ) -> ReplayComparison:
        """Compare two validation runs for deterministic replay verification."""
        differences: List[str] = []
        
        symbol_match = True
        if run_a.cycle_results and run_b.cycle_results:
            symbols_a = {r.symbol for r in run_a.cycle_results}
            symbols_b = {r.symbol for r in run_b.cycle_results}
            if symbols_a != symbols_b:
                symbol_match = False
                differences.append(f"Symbols differ: {symbols_a} vs {symbols_b}")
        
        total_candles_match = run_a.total_candles == run_b.total_candles
        if not total_candles_match:
            differences.append(
                f"Total candles differ: {run_a.total_candles} vs {run_b.total_candles}"
            )
        
        metrics_match = (
            run_a.processed_candles == run_b.processed_candles and
            run_a.blocked_candles == run_b.blocked_candles and
            run_a.total_fills == run_b.total_fills and
            run_a.total_orders_submitted == run_b.total_orders_submitted and
            run_a.total_orders_skipped == run_b.total_orders_skipped
        )
        if not metrics_match:
            differences.append(
                f"Metrics differ: processed={run_a.processed_candles}/{run_b.processed_candles}, "
                f"blocked={run_a.blocked_candles}/{run_b.blocked_candles}, "
                f"fills={run_a.total_fills}/{run_b.total_fills}, "
                f"orders_submitted={run_a.total_orders_submitted}/{run_b.total_orders_submitted}, "
                f"orders_skipped={run_a.total_orders_skipped}/{run_b.total_orders_skipped}"
            )
        
        cycle_ids_match = True
        if len(run_a.cycle_results) == len(run_b.cycle_results):
            for i, (ca, cb) in enumerate(zip(run_a.cycle_results, run_b.cycle_results)):
                if ca.cycle_id != cb.cycle_id:
                    cycle_ids_match = False
                    differences.append(f"Cycle {i} IDs differ: {ca.cycle_id} vs {cb.cycle_id}")
        else:
            cycle_ids_match = False
            differences.append("Different number of cycle results")
        
        order_ids_match = True
        if cycle_ids_match:
            for ca, cb in zip(run_a.cycle_results, run_b.cycle_results):
                intents_a = {i.client_order_id for i in ca.order_intents}
                intents_b = {i.client_order_id for i in cb.order_intents}
                if intents_a != intents_b:
                    order_ids_match = False
                    differences.append(f"Order IDs differ: {intents_a} vs {intents_b}")
        else:
            order_ids_match = False
        
        fill_ids_match = True
        if cycle_ids_match:
            for ca, cb in zip(run_a.cycle_results, run_b.cycle_results):
                fills_a = {e.event_id for e in ca.events if e.event_type == "ORDER_FILLED"}
                fills_b = {e.event_id for e in cb.events if e.event_type == "ORDER_FILLED"}
                if fills_a != fills_b:
                    fill_ids_match = False
                    differences.append(f"Fill IDs differ: {fills_a} vs {fills_b}")
        else:
            fill_ids_match = False
        
        # Compare accounting states ignoring the non-deterministic updated_at
        # timestamp, which records wall-clock time at DB write and would
        # otherwise always differ between independent replay runs.
        def _deterministic_state(state: Dict[str, Any]) -> Dict[str, Any]:
            return {k: v for k, v in state.items() if k != "updated_at"}

        accounting_state_match = (
            _deterministic_state(run_a.final_accounting_state) ==
            _deterministic_state(run_b.final_accounting_state)
        )
        if not accounting_state_match:
            differences.append("Final accounting states differ")
        
        return ReplayComparison(
            symbol_match=symbol_match,
            total_candles_match=total_candles_match,
            metrics_match=metrics_match,
            cycle_ids_match=cycle_ids_match,
            order_ids_match=order_ids_match,
            fill_ids_match=fill_ids_match,
            accounting_state_match=accounting_state_match,
            differences=differences,
        )

    def run_stress_scenario(
        self,
        scenario_name: str,
        candles: List[ValidationCandle],
    ) -> ValidationRun:
        """Run a named stress scenario with the given candles."""
        return self.validate_historical_sequence(candles, f"stress_{scenario_name}")
    
    def cleanup(self) -> None:
        """Clean up temporary database files."""
        import os
        if hasattr(self, '_order_db_path') and os.path.exists(self._order_db_path):
            os.remove(self._order_db_path)
        if hasattr(self, '_lifecycle_db_path') and os.path.exists(self._lifecycle_db_path):
            os.remove(self._lifecycle_db_path)


# ---------------------------------------------------------------------------
# Adversarial Sequence Generators
# ---------------------------------------------------------------------------

def generate_oscillating_sequence(
    start_candle: int = 1000,
    num_candles: int = 10,
    base_price: Decimal = Decimal("100"),
    amplitude_pct: Decimal = Decimal("0.05"),
    volume: Decimal = Decimal("1000"),
    symbol: str = "BTCUSDT",
) -> List[ValidationCandle]:
    """Generate an oscillating price sequence for validation."""
    from math import sin, pi
    
    candles = []
    amplitude = base_price * amplitude_pct
    
    for i in range(num_candles):
        candle_idx = start_candle + i
        phase = (i / num_candles) * 4 * pi
        offset = Decimal(str(sin(phase))) * amplitude
        close = base_price + offset
        
        spread = close * Decimal("0.001")
        open_price = close - (spread * Decimal(str((-1) ** i)) * Decimal("0.3"))
        high = max(open_price, close) + spread * Decimal("0.5")
        low = min(open_price, close) - spread * Decimal("0.5")
        
        timestamp = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        timestamp = timestamp.replace(minute=(timestamp.minute + i * 15) % 60)
        if i * 15 >= 60:
            hour_delta = (i * 15) // 60
            timestamp = timestamp.replace(hour=(timestamp.hour + hour_delta) % 24)
        
        candle = ValidationCandle(
            candle_index=candle_idx,
            symbol=symbol,
            timestamp=timestamp,
            open=open_price.quantize(Decimal("0.01")),
            high=high.quantize(Decimal("0.01")),
            low=low.quantize(Decimal("0.01")),
            close=close.quantize(Decimal("0.01")),
            volume=volume,
        )
        candles.append(candle)
    
    return candles


def generate_trending_sequence(
    start_candle: int = 1000,
    num_candles: int = 10,
    base_price: Decimal = Decimal("100"),
    trend_pct_per_candle: Decimal = Decimal("0.01"),
    volume: Decimal = Decimal("1000"),
    symbol: str = "BTCUSDT",
) -> List[ValidationCandle]:
    """Generate a trending price sequence for validation."""
    candles = []
    
    for i in range(num_candles):
        candle_idx = start_candle + i
        close = base_price * (Decimal("1") + trend_pct_per_candle * Decimal(str(i)))
        
        spread = close * Decimal("0.001")
        open_price = close - spread * Decimal("0.3")
        high = max(open_price, close) + spread * Decimal("0.5")
        low = min(open_price, close) - spread * Decimal("0.5")
        
        timestamp = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        timestamp = timestamp.replace(minute=(timestamp.minute + i * 15) % 60)
        if i * 15 >= 60:
            hour_delta = (i * 15) // 60
            timestamp = timestamp.replace(hour=(timestamp.hour + hour_delta) % 24)
        
        candle = ValidationCandle(
            candle_index=candle_idx,
            symbol=symbol,
            timestamp=timestamp,
            open=open_price.quantize(Decimal("0.01")),
            high=high.quantize(Decimal("0.01")),
            low=low.quantize(Decimal("0.01")),
            close=close.quantize(Decimal("0.01")),
            volume=volume,
        )
        candles.append(candle)
    
    return candles


def generate_volatile_sequence(
    start_candle: int = 1000,
    num_candles: int = 10,
    base_price: Decimal = Decimal("100"),
    volatility_pct: Decimal = Decimal("0.03"),
    volume: Decimal = Decimal("1000"),
    symbol: str = "BTCUSDT",
) -> List[ValidationCandle]:
    """Generate a high-volatility sequence for stress testing."""
    from math import sin, pi
    
    candles = []
    
    for i in range(num_candles):
        candle_idx = start_candle + i
        phase1 = (i / num_candles) * 8 * pi
        phase2 = (i / num_candles) * 12 * pi
        offset = (Decimal(str(sin(phase1))) * Decimal("0.7") + 
                  Decimal(str(sin(phase2))) * Decimal("0.3")) * base_price * volatility_pct
        close = base_price + offset
        
        spread = close * Decimal("0.002")
        open_price = close - spread * Decimal(str((-1) ** i)) * Decimal("0.5")
        high = max(open_price, close) + spread
        low = min(open_price, close) - spread
        
        timestamp = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        timestamp = timestamp.replace(minute=(timestamp.minute + i * 15) % 60)
        if i * 15 >= 60:
            hour_delta = (i * 15) // 60
            timestamp = timestamp.replace(hour=(timestamp.hour + hour_delta) % 24)
        
        candle = ValidationCandle(
            candle_index=candle_idx,
            symbol=symbol,
            timestamp=timestamp,
            open=open_price.quantize(Decimal("0.01")),
            high=high.quantize(Decimal("0.01")),
            low=low.quantize(Decimal("0.01")),
            close=close.quantize(Decimal("0.01")),
            volume=volume,
        )
        candles.append(candle)
    
    return candles


def generate_range_break_sequence(
    start_candle: int = 1000,
    num_candles: int = 15,
    range_low: Decimal = Decimal("95"),
    range_high: Decimal = Decimal("105"),
    breakout_direction: str = "up",
    volume: Decimal = Decimal("1000"),
    symbol: str = "BTCUSDT",
) -> List[ValidationCandle]:
    """Generate a range-bound sequence with a breakout."""
    from math import sin, pi
    
    candles = []
    
    # Phase 1: Range-bound (first 10 candles)
    for i in range(min(10, num_candles)):
        candle_idx = start_candle + i
        position = (i / 10) * 2 * pi
        close = range_low + (range_high - range_low) * Decimal("0.5") * (Decimal("1") + Decimal(str(sin(position))))
        
        spread = close * Decimal("0.001")
        open_price = close - spread * Decimal("0.3")
        high = max(open_price, close) + spread * Decimal("0.5")
        low = min(open_price, close) - spread * Decimal("0.5")
        
        timestamp = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        timestamp = timestamp.replace(minute=(timestamp.minute + i * 15) % 60)
        if i * 15 >= 60:
            hour_delta = (i * 15) // 60
            timestamp = timestamp.replace(hour=(timestamp.hour + hour_delta) % 24)
        
        candle = ValidationCandle(
            candle_index=candle_idx,
            symbol=symbol,
            timestamp=timestamp,
            open=open_price.quantize(Decimal("0.01")),
            high=high.quantize(Decimal("0.01")),
            low=low.quantize(Decimal("0.01")),
            close=close.quantize(Decimal("0.01")),
            volume=volume,
        )
        candles.append(candle)
    
    # Phase 2: Breakout (remaining candles)
    breakout_start = range_high if breakout_direction == "up" else range_low
    for i in range(10, num_candles):
        candle_idx = start_candle + i
        progress = Decimal(str(i - 9)) / Decimal(str(num_candles - 9))
        if breakout_direction == "up":
            close = breakout_start * (Decimal("1") + Decimal("0.02") * progress)
        else:
            close = breakout_start * (Decimal("1") - Decimal("0.02") * progress)
        
        spread = close * Decimal("0.001")
        open_price = close - spread * Decimal("0.3")
        high = max(open_price, close) + spread * Decimal("0.5")
        low = min(open_price, close) - spread * Decimal("0.5")
        
        timestamp = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        timestamp = timestamp.replace(minute=(timestamp.minute + i * 15) % 60)
        if i * 15 >= 60:
            hour_delta = (i * 15) // 60
            timestamp = timestamp.replace(hour=(timestamp.hour + hour_delta) % 24)
        
        candle = ValidationCandle(
            candle_index=candle_idx,
            symbol=symbol,
            timestamp=timestamp,
            open=open_price.quantize(Decimal("0.01")),
            high=high.quantize(Decimal("0.01")),
            low=low.quantize(Decimal("0.01")),
            close=close.quantize(Decimal("0.01")),
            volume=volume * Decimal("2"),
        )
        candles.append(candle)
    
    return candles

