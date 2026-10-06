"""Adaptive Spot Grid Planner.

Deterministic computation of grid parameters (LOWER_PRICE, UPPER_PRICE,
TOTAL_GRIDS, TOTAL_QUOTE_BUDGET) from market data and policy configuration.

Pure function - no exchange I/O, no side effects. Easy to unit test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import List, Optional

import grid as grid_mod
from config import Config
from grid import ExchangeFilters, GridLevel

log = logging.getLogger("adaptive_grid")


@dataclass(frozen=True)
class AdaptiveGridPlan:
    """Complete adaptive grid plan ready for execution."""
    lower_price: float
    upper_price: float
    total_grids: int
    quote_budget: float
    step: float
    reference_price: float
    levels: List[GridLevel]
    gross_pct: float
    net_pct: float
    mode: str  # "arithmetic" or "geometric"


class AdaptiveGridPlanner:
    """Deterministic adaptive grid planner."""

    @staticmethod
    def plan(
        symbol: str,
        current_price: float,
        atr: float,
        cfg: Config,
        filters: ExchangeFilters,
        reference_price: float,
        available_usdt: float,
    ) -> AdaptiveGridPlan:
        """
        Compute adaptive grid parameters.

        Algorithm:
        1. Compute grid step = atr * GRID_STEP_ATR_MULTIPLIER
        2. Build symmetric ATR-based range around current_price
        3. For candidate_count in range(MIN_GRIDS, MAX_GRIDS + 1):
           a. Build grid using grid.build_grid() with candidate bounds/count
           b. Validate executable economics (net >= MIN_NET_PROFIT_PER_GRID)
           c. Validate against Binance filters
           d. Check quote budget feasibility
        4. Select highest passing candidate_count
        5. Fail-closed if no candidate passes

        Args:
            symbol: Trading symbol (e.g., "BTC/USDT")
            current_price: Current market price (last close)
            atr: ATR value from indicator snapshot
            cfg: Config object with adaptive policy parameters
            filters: ExchangeFilters from exchangeInfo
            reference_price: Exchange weighted-average price (for PERCENT_PRICE_BY_SIDE)
            available_usdt: Available USDT balance from account

        Returns:
            AdaptiveGridPlan with all computed parameters

        Raises:
            ValueError: If no valid grid can be constructed (fail-closed)
        """
        # Validate inputs - fail-closed on missing/invalid data
        if current_price is None or current_price <= 0:
            raise ValueError("invalid current_price")
        if atr is None or atr <= 0:
            raise ValueError("invalid ATR")
        if available_usdt is None or available_usdt <= 0:
            raise ValueError("invalid available USDT balance")
        if reference_price is None or reference_price <= 0:
            raise ValueError("invalid reference price")
        if cfg.adaptive_grid is False:
            raise ValueError("adaptive_grid is disabled")

        # Compute base grid step from ATR
        grid_step = atr * cfg.grid_step_atr_multiplier
        if grid_step <= 0:
            raise ValueError("computed grid_step <= 0")

        # Compute quote budget with reserve and allocation cap
        # Available after reserve: available_usdt * (1 - reserve/100)
        # Max allocation across all symbols: available_after_reserve * max_allocation/100
        # For single symbol: divide by number of symbols in PAIR_LIST (conservative)
        num_symbols = len(cfg.pair_list)
        reserve_factor = 1.0 - (cfg.quote_reserve_percent / 100.0)
        allocation_factor = cfg.max_quote_allocation_percent / 100.0
        per_symbol_budget = (
            available_usdt * reserve_factor * allocation_factor / max(1, num_symbols)
        )

        if per_symbol_budget <= 0:
            raise ValueError("computed quote budget <= 0")

        # Base step from ATR (fixed for all candidates)
        base_step = atr * cfg.grid_step_atr_multiplier
        if base_step <= 0:
            raise ValueError("computed grid_step <= 0")

        # Try candidate grid counts from MAX_GRIDS down to MIN_GRIDS
        # (prefer highest usable count that passes all constraints)
        best_plan: Optional[grid_mod.GridPlan] = None
        best_count = 0
        best_lower = 0.0
        best_upper = 0.0

        for candidate_count in range(cfg.max_grids, cfg.min_grids - 1, -1):
            try:
                # Compute bounds for this candidate: all grids fit below reference_price
                # Lowest buy = current_price - candidate_count * base_step
                # Highest sell = current_price + base_step (level 1)
                cand_lower = current_price - candidate_count * base_step
                cand_upper = current_price + base_step

                if cand_lower <= 0:
                    cand_lower = current_price * 0.001

                # Quantize bounds to tick_size
                tick = filters.tick_size
                quantized_lower = AdaptiveGridPlanner._quantize_price_floor(cand_lower, tick)
                quantized_upper = AdaptiveGridPlanner._quantize_price_ceil(cand_upper, tick)

                if quantized_lower >= quantized_upper:
                    continue
                if quantized_lower >= current_price:
                    continue
                if quantized_upper <= current_price:
                    continue

                plan = grid_mod.build_grid(
                    symbol=symbol,
                    mode=cfg.grid_mode(symbol),
                    price=current_price,
                    atr_value=atr,
                    filters=filters,
                    cfg=cfg,
                    reference_price=reference_price,
                    lower_override=quantized_lower,
                    upper_override=quantized_upper,
                    total_grids_override=candidate_count,
                )
                if plan.executable:
                    # Additional check: total buy notional must fit within budget
                    total_buy_notional = sum(
                        lvl.buy_price * lvl.qty for lvl in plan.levels
                    )
                    if total_buy_notional <= per_symbol_budget:
                        if not plan.levels:
                            # Candidate produced zero executable levels - reject
                            continue
                        best_plan = plan
                        best_count = len(plan.levels)
                        best_lower = min(lvl.buy_price for lvl in plan.levels)
                        best_upper = max(lvl.sell_price for lvl in plan.levels)
                        break  # highest passing count found
            except Exception:
                # Any error in grid building -> try the next (smaller)
                # candidate; the error is logged so a genuine bug in the
                # grid builder is never silently swallowed.
                log.exception("adaptive planner: candidate %s grid failed for %s",
                              candidate_count, symbol)
                continue

        if best_plan is None:
            raise ValueError(
                f"no valid grid found for {symbol} "
                f"(tried {cfg.min_grids}-{cfg.max_grids} grids, "
                f"budget={per_symbol_budget:.2f}, range=[{best_lower:.8f},{best_upper:.8f}])"
            )

        return AdaptiveGridPlan(
            lower_price=best_lower,
            upper_price=best_upper,
            total_grids=best_count,
            quote_budget=per_symbol_budget,
            step=best_plan.step,
            reference_price=reference_price,
            levels=best_plan.levels,
            gross_pct=best_plan.gross_pct,
            net_pct=best_plan.net_pct,
            mode=best_plan.mode,
        )

    @staticmethod
    def _quantize_price_floor(price: float, tick: float) -> float:
        """Round price DOWN to tick size."""
        if tick <= 0:
            return price
        d = Decimal(str(price)) / Decimal(str(tick))
        quantized = (d.to_integral_value(rounding=ROUND_FLOOR)) * Decimal(str(tick))
        return float(quantized)

    @staticmethod
    def _quantize_price_ceil(price: float, tick: float) -> float:
        """Round price UP to tick size."""
        if tick <= 0:
            return price
        d = Decimal(str(price)) / Decimal(str(tick))
        quantized = (d.to_integral_value(rounding=ROUND_CEILING)) * Decimal(str(tick))
        return float(quantized)