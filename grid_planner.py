"""Phase 5A – Adaptive Grid Planner.

Converts Phase 4 market intelligence into a validated, immutable candidate
grid plan.  This module is a PURE DECISION LAYER:

- Does NOT submit orders.
- Does NOT cancel orders.
- Does NOT mutate accounting or balances.
- Does NOT call any Binance trading endpoint.
- Does NOT predict price direction.
- Does NOT use randomness or wall-clock time as logical inputs.

Hard safety invariants:
  LOWER_PRICE <= candidate_lower < candidate_upper <= UPPER_PRICE
  net_profit_per_grid >= MIN_NET_PROFIT_PER_GRID (config hard_min_net_pct)
  sum(buy_quote_allocations) <= TOTAL_QUOTE_BUDGET
  no synthetic base inventory
  no leverage, no margin, no futures, no shorts
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from enum import Enum
from typing import Any

from grid_engine import build_geometric_grid, validate_grid_profit
from market_regime import MarketRegime
from profit_model import net_pct_from_step

getcontext().prec = 40


def _D(value: Any) -> Decimal:
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class PlanDecision(str, Enum):
    """Top-level decision produced by the adaptive planner."""
    GRID_ALLOWED            = "GRID_ALLOWED"
    GRID_BLOCKED            = "GRID_BLOCKED"
    KEEP_CURRENT_PLAN       = "KEEP_CURRENT_PLAN"
    RECONFIGURATION_REQUIRED = "RECONFIGURATION_REQUIRED"


class PlanBlockReason(str, Enum):
    """Structured blocking reasons.  Multiple reasons may apply simultaneously."""
    RANGE_OUTSIDE_CONFIG         = "RANGE_OUTSIDE_CONFIG"
    INVALID_CANDIDATE_RANGE      = "INVALID_CANDIDATE_RANGE"
    REGIME_BLOCKED               = "REGIME_BLOCKED"
    INSUFFICIENT_DATA            = "INSUFFICIENT_DATA"
    INVALID_MARKET_DATA          = "INVALID_MARKET_DATA"
    NET_PROFIT_BELOW_MINIMUM     = "NET_PROFIT_BELOW_MINIMUM"
    INSUFFICIENT_QUOTE_BUDGET    = "INSUFFICIENT_QUOTE_BUDGET"
    INSUFFICIENT_BASE_INVENTORY  = "INSUFFICIENT_BASE_INVENTORY"
    GRID_COUNT_INVALID           = "GRID_COUNT_INVALID"
    SYMBOL_RULE_VIOLATION        = "SYMBOL_RULE_VIOLATION"
    COOLDOWN_ACTIVE              = "COOLDOWN_ACTIVE"
    HYSTERESIS_NOT_TRIGGERED     = "HYSTERESIS_NOT_TRIGGERED"
    PRICE_OUTSIDE_CANDIDATE_RANGE = "PRICE_OUTSIDE_CANDIDATE_RANGE"


# ---------------------------------------------------------------------------
# Data model – immutable
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GridLevel:
    """A single price level in the candidate grid."""
    index: int
    price: Decimal


@dataclass(frozen=True)
class AdaptiveGridPlan:
    """Immutable candidate grid plan produced by the adaptive planner.

    Financial quantities use Decimal.  Never mutated after creation.
    """
    # Identity
    plan_id: str

    # Market context
    pair: str
    regime: MarketRegime
    range_quality_score: Decimal

    # Candidate geometry
    candidate_lower: Decimal
    candidate_upper: Decimal
    grid_type: str           # always "GEOMETRIC" in Phase 5A
    grid_step: Decimal       # e.g. Decimal("0.006")
    grid_count: int          # number of grid cells (len(levels) - 1)
    levels: tuple[GridLevel, ...]

    # Budget
    total_quote_budget: Decimal
    buy_quote_budget: Decimal

    # Inventory
    required_base_inventory: Decimal
    available_base_inventory: Decimal
    inventory_sufficient: bool

    # Economics
    estimated_net_profit_per_grid: Decimal

    # Decision
    decision: PlanDecision
    reasons: tuple[PlanBlockReason, ...]

    @property
    def is_actionable(self) -> bool:
        return self.decision in (
            PlanDecision.GRID_ALLOWED,
            PlanDecision.RECONFIGURATION_REQUIRED,
        )


# ---------------------------------------------------------------------------
# Active plan descriptor (caller-supplied; planner reads it for hysteresis)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActivePlan:
    """Minimal descriptor of the currently running grid plan.

    The caller (main.py) supplies this; the planner never mutates it.
    """
    plan_id: str
    candidate_lower: Decimal
    candidate_upper: Decimal
    grid_step: Decimal
    grid_count: int
    regime: MarketRegime
    range_quality_score: Decimal
    # candle_index is a monotonically increasing integer (closed-candle count),
    # used for cooldown tracking without using wall-clock time.
    candle_index: int = 0
    # generation is a monotonically increasing plan-generation counter; each
    # distinct active-plan identity increments it, giving every plan generation
    # its own order-identity namespace. Defaults to 0 for legacy states that
    # predate generation tracking.
    generation: int = 0


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _planner_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("adaptive_planner", {})


def _grid_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("grid", {})


def _exec_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("execution", {})


def _fees_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("fees", {})


# ---------------------------------------------------------------------------
# Plan identity
# ---------------------------------------------------------------------------

def compute_plan_id(
    pair: str,
    candidate_lower: Decimal,
    candidate_upper: Decimal,
    grid_step: Decimal,
    grid_count: int,
    regime: MarketRegime,
    cfg_step_pct: Decimal,
    cfg_hard_min: Decimal,
) -> str:
    """Deterministic stable hash.

    Same logical inputs → same plan_id.
    No randomness, no wall-clock time.
    """
    payload = json.dumps(
        {
            "pair": pair,
            "lower": str(candidate_lower),
            "upper": str(candidate_upper),
            "step": str(grid_step),
            "count": grid_count,
            "regime": regime.value,
            "cfg_step_pct": str(cfg_step_pct),
            "cfg_hard_min": str(cfg_hard_min),
        },
        sort_keys=True,
    )
    return "plan_" + hashlib.sha256(payload.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Regime → allowed to start NEW grid?
# ---------------------------------------------------------------------------

_REGIME_ALLOWS_NEW_GRID: dict[MarketRegime, bool] = {
    MarketRegime.RANGE:            True,
    MarketRegime.TREND_UP:         False,
    MarketRegime.TREND_DOWN:       False,
    MarketRegime.VOLATILE:         False,
    MarketRegime.INSUFFICIENT_DATA: False,
    MarketRegime.INVALID_DATA:     False,
}

# Regimes where we prefer keeping the current plan rather than reconfiguring
_REGIME_PREFER_KEEP: frozenset[MarketRegime] = frozenset({
    MarketRegime.TREND_UP,
    MarketRegime.TREND_DOWN,
    MarketRegime.VOLATILE,
})


def _regime_volatile_spacing(regime: MarketRegime, base_step: Decimal) -> Decimal:
    """For RANGE regime, base_step is used as-is.
    A different regime that reaches here (e.g. not blocked) gets wider spacing.
    Currently only RANGE reaches the spacing logic, but kept for extensibility.
    """
    return base_step


# ---------------------------------------------------------------------------
# Candidate range derivation
# ---------------------------------------------------------------------------

def _derive_candidate_range(
    configured_lower: Decimal,
    configured_upper: Decimal,
    current_price: Decimal,
    range_quality_score: Decimal,
    regime: MarketRegime,
    cfg: dict[str, Any],
) -> tuple[Decimal, Decimal, list[PlanBlockReason]]:
    """Derive candidate sub-range strictly inside the configured range.

    Phase 5A uses the configured range as the candidate range (no sub-range
    narrowing), but enforces the hard boundary invariant:
        LOWER_PRICE <= candidate_lower < candidate_upper <= UPPER_PRICE

    Returns (candidate_lower, candidate_upper, blocking_reasons).
    An empty reasons list means the candidate range is valid.
    """
    reasons: list[PlanBlockReason] = []

    lo = configured_lower
    hi = configured_upper

    if lo <= 0 or hi <= lo:
        reasons.append(PlanBlockReason.INVALID_CANDIDATE_RANGE)
        return lo, hi, reasons

    # Enforce hard boundary
    if lo < configured_lower or hi > configured_upper:
        reasons.append(PlanBlockReason.RANGE_OUTSIDE_CONFIG)

    if lo >= hi:
        reasons.append(PlanBlockReason.INVALID_CANDIDATE_RANGE)

    return lo, hi, reasons


# ---------------------------------------------------------------------------
# Grid count
# ---------------------------------------------------------------------------

def _compute_grid_count(
    candidate_lower: Decimal,
    candidate_upper: Decimal,
    grid_step: Decimal,
    min_grids: int,
    max_grids: int,
    max_levels_limit: int,
) -> tuple[list, Decimal, list[PlanBlockReason]]:
    """Build geometric grid levels and return (levels, effective_upper, reasons)."""
    reasons: list[PlanBlockReason] = []
    try:
        raw_levels, eff_upper = build_geometric_grid(
            candidate_lower,
            candidate_upper,
            grid_step,
            min_cells=min_grids,
            max_levels=max_levels_limit,
        )
    except ValueError as exc:
        reasons.append(PlanBlockReason.GRID_COUNT_INVALID)
        return [], candidate_upper, reasons

    cell_count = len(raw_levels) - 1
    if cell_count < min_grids:
        reasons.append(PlanBlockReason.GRID_COUNT_INVALID)
        return raw_levels, eff_upper, reasons
    if cell_count > max_grids:
        # Truncate to max_grids cells
        raw_levels = raw_levels[: max_grids + 1]
        eff_upper = raw_levels[-1].price

    return raw_levels, eff_upper, reasons


# ---------------------------------------------------------------------------
# Net profit validation
# ---------------------------------------------------------------------------

def _validate_spacing_profit(
    grid_step: Decimal,
    buy_fee: Decimal,
    sell_fee: Decimal,
    slippage: Decimal,
    hard_min: Decimal,
) -> tuple[Decimal, list[PlanBlockReason]]:
    """Return (net_pct, reasons).  net_pct < hard_min → GRID_BLOCKED."""
    reasons: list[PlanBlockReason] = []
    try:
        net = net_pct_from_step(grid_step, buy_fee, sell_fee, slippage)
    except (ValueError, ArithmeticError):
        reasons.append(PlanBlockReason.NET_PROFIT_BELOW_MINIMUM)
        return _D("0"), reasons

    if net < hard_min:
        reasons.append(PlanBlockReason.NET_PROFIT_BELOW_MINIMUM)
    return net, reasons


# ---------------------------------------------------------------------------
# Budget allocation
# ---------------------------------------------------------------------------

def _allocate_budget(
    levels: list,
    order_quote_size: Decimal,
    total_quote_budget: Decimal,
    current_price: Decimal,
    available_base: Decimal,
) -> tuple[Decimal, Decimal, bool, list[PlanBlockReason]]:
    """Return (buy_quote_budget, required_base, inventory_sufficient, reasons).

    BUY levels: price < current_price → funded from quote budget.
    SELL levels: price >= current_price → require base inventory.

    Invariant: buy_quote_budget <= total_quote_budget (never exceed).
    No synthetic inventory is created.
    """
    reasons: list[PlanBlockReason] = []

    if len(levels) < 2:
        reasons.append(PlanBlockReason.GRID_COUNT_INVALID)
        return _D("0"), _D("0"), False, reasons

    buy_levels = [lv for lv in levels[:-1] if lv.price < current_price]
    sell_levels = [lv for lv in levels[:-1] if lv.price >= current_price]

    buy_quote = order_quote_size * _D(len(buy_levels))
    if total_quote_budget > 0 and buy_quote > total_quote_budget:
        # Reduce BUY level count to fit budget
        max_buy_cells = int(total_quote_budget / order_quote_size)
        if max_buy_cells < 1:
            reasons.append(PlanBlockReason.INSUFFICIENT_QUOTE_BUDGET)
            return _D("0"), _D("0"), False, reasons
        buy_levels = buy_levels[-max_buy_cells:]  # prefer levels nearest current price
        buy_quote = order_quote_size * _D(len(buy_levels))

    # Verify budget ceiling
    if total_quote_budget > 0 and buy_quote > total_quote_budget:
        reasons.append(PlanBlockReason.INSUFFICIENT_QUOTE_BUDGET)
        return buy_quote, _D("0"), False, reasons

    # Required base for SELL levels: quote_size / level_price per level
    required_base = _D("0")
    for lv in sell_levels:
        if lv.price > 0:
            required_base += order_quote_size / lv.price

    inv_sufficient = available_base >= required_base
    if not inv_sufficient:
        reasons.append(PlanBlockReason.INSUFFICIENT_BASE_INVENTORY)

    return buy_quote, required_base, inv_sufficient, reasons


# ---------------------------------------------------------------------------
# Hysteresis
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HysteresisConfig:
    """Explicit hysteresis thresholds – all configurable, no magic numbers."""
    range_change_pct:      Decimal   # e.g. 0.02 → 2% change triggers reconfiguration
    step_change_pct:       Decimal   # e.g. 0.10 → 10% step change
    grid_count_change:     int       # e.g. 3 → ±3 cells
    quality_degradation:   Decimal   # e.g. 5 → score drops by 5 points
    regime_change:         bool      # any regime change → reconfiguration


def _load_hysteresis_config(cfg: dict[str, Any]) -> HysteresisConfig:
    pc = _planner_cfg(cfg)
    hyst = pc.get("hysteresis", {})
    return HysteresisConfig(
        range_change_pct    = _D(hyst.get("range_change_pct",   "0.02")),
        step_change_pct     = _D(hyst.get("step_change_pct",    "0.10")),
        grid_count_change   = int(hyst.get("grid_count_change",  3)),
        quality_degradation = _D(hyst.get("quality_degradation", "5")),
        regime_change       = bool(hyst.get("regime_change",     True)),
    )


def _check_hysteresis(
    active: ActivePlan,
    candidate_lower: Decimal,
    candidate_upper: Decimal,
    grid_step: Decimal,
    grid_count: int,
    regime: MarketRegime,
    range_quality_score: Decimal,
    hyst: HysteresisConfig,
) -> bool:
    """Return True if the candidate differs materially from the active plan.

    True  → RECONFIGURATION_REQUIRED
    False → KEEP_CURRENT_PLAN (change is within hysteresis band)

    Comparison rules (all must be within thresholds for keep):
    1. Range lower: |new_lower - old_lower| / old_lower < range_change_pct
    2. Range upper: |new_upper - old_upper| / old_upper < range_change_pct
    3. Grid step:   |new_step - old_step| / old_step < step_change_pct
    4. Grid count:  |new_count - old_count| <= grid_count_change
    5. Regime:      same (if hyst.regime_change is True)
    6. Quality:     new_score >= old_score - quality_degradation
    """
    # 1 & 2: Range change
    if active.candidate_lower > 0:
        lower_change = abs(candidate_lower - active.candidate_lower) / active.candidate_lower
        if lower_change >= hyst.range_change_pct:
            return True
    if active.candidate_upper > 0:
        upper_change = abs(candidate_upper - active.candidate_upper) / active.candidate_upper
        if upper_change >= hyst.range_change_pct:
            return True

    # 3: Step change
    if active.grid_step > 0:
        step_change = abs(grid_step - active.grid_step) / active.grid_step
        if step_change >= hyst.step_change_pct:
            return True

    # 4: Grid count change
    if abs(grid_count - active.grid_count) > hyst.grid_count_change:
        return True

    # 5: Regime change
    if hyst.regime_change and regime != active.regime:
        return True

    # 6: Quality degradation
    if range_quality_score < active.range_quality_score - hyst.quality_degradation:
        return True

    return False


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------

def _cooldown_active(
    active: ActivePlan | None,
    current_candle_index: int,
    cooldown_candles: int,
) -> bool:
    """Cooldown based on closed-candle count, NOT wall-clock time.

    Returns True if we are still within the cooldown window after the last
    reconfiguration decision.
    """
    if active is None:
        return False
    elapsed = current_candle_index - active.candle_index
    return elapsed < cooldown_candles


# ---------------------------------------------------------------------------
# Main planner entry point
# ---------------------------------------------------------------------------

def evaluate_adaptive_grid_plan(
    pair: str,
    regime: MarketRegime,
    range_quality_score: Decimal,
    current_price: Decimal,
    configured_lower: Decimal,
    configured_upper: Decimal,
    available_base_inventory: Decimal,
    cfg: dict[str, Any],
    active_plan: ActivePlan | None = None,
    current_candle_index: int = 0,
) -> AdaptiveGridPlan:
    """Evaluate and return an immutable AdaptiveGridPlan.

    Parameters
    ----------
    pair                   : Trading symbol, e.g. "BNBUSDT".
    regime                 : MarketRegime from Phase 4.
    range_quality_score    : RangeQualityResult.score from Phase 4.
    current_price          : Latest ticker price (Decimal).
    configured_lower       : Hard lower price boundary from config.
    configured_upper       : Hard upper price boundary from config.
    available_base_inventory: Base asset balance from paper accounting.
    cfg                    : Full config dict.
    active_plan            : Currently running plan (None if no plan active).
    current_candle_index   : Monotonically increasing closed-candle counter.

    Returns
    -------
    AdaptiveGridPlan  – always returns a plan object; decision field encodes
                        GRID_ALLOWED / GRID_BLOCKED / KEEP_CURRENT_PLAN /
                        RECONFIGURATION_REQUIRED.
    """
    grid_c   = _grid_cfg(cfg)
    exec_c   = _exec_cfg(cfg)
    fees_c   = _fees_cfg(cfg)
    plan_c   = _planner_cfg(cfg)

    # Configuration values
    hard_min       = _D(grid_c.get("hard_min_net_pct",     "0.003"))
    cfg_step_pct   = _D(grid_c.get("step_pct",             "0.006"))
    min_grids      = int(grid_c.get("min_grids",            int(grid_c.get("min_cells", 6))))
    max_grids      = int(grid_c.get("max_grids",            int(grid_c.get("max_levels", 40))))
    max_levels_lim = max_grids + 1   # build_geometric_grid uses level count

    total_budget   = _D(exec_c.get("total_quote_budget",  "0"))
    order_q_size   = _D(exec_c.get("order_quote_size",    "25"))
    max_open_orders= int(exec_c.get("max_open_orders",    40))

    maker_fee      = _D(fees_c.get("maker_fee_fallback",  "0.001"))
    sell_fee       = maker_fee  # prefer_limit_maker → both maker
    slippage       = _D(fees_c.get("slippage_roundtrip_pct", "0.0005"))

    cooldown_candles = int(plan_c.get("cooldown_candles", 4))
    hyst_cfg       = _load_hysteresis_config(cfg)

    # Collect all blocking reasons
    all_reasons: list[PlanBlockReason] = []

    # ------------------------------------------------------------------ #
    # 1. Data / regime gate
    # ------------------------------------------------------------------ #
    if regime == MarketRegime.INVALID_DATA:
        all_reasons.append(PlanBlockReason.INVALID_MARKET_DATA)
    if regime == MarketRegime.INSUFFICIENT_DATA:
        all_reasons.append(PlanBlockReason.INSUFFICIENT_DATA)

    regime_allowed = _REGIME_ALLOWS_NEW_GRID.get(regime, False)
    if not regime_allowed and regime not in (
        MarketRegime.INVALID_DATA, MarketRegime.INSUFFICIENT_DATA
    ):
        all_reasons.append(PlanBlockReason.REGIME_BLOCKED)

    # If data is bad, return early with a blocked plan (no further evaluation)
    if regime in (MarketRegime.INVALID_DATA, MarketRegime.INSUFFICIENT_DATA):
        return _make_blocked_plan(
            pair=pair,
            regime=regime,
            range_quality_score=range_quality_score,
            configured_lower=configured_lower,
            configured_upper=configured_upper,
            total_quote_budget=total_budget,
            available_base=available_base_inventory,
            cfg_step_pct=cfg_step_pct,
            hard_min=hard_min,
            reasons=all_reasons,
        )

    # ------------------------------------------------------------------ #
    # 2. Candidate range
    # ------------------------------------------------------------------ #
    cand_lower, cand_upper, range_reasons = _derive_candidate_range(
        configured_lower, configured_upper, current_price,
        range_quality_score, regime, cfg,
    )
    all_reasons.extend(range_reasons)

    # ------------------------------------------------------------------ #
    # 3. Determine grid step (regime-specific)
    # ------------------------------------------------------------------ #
    grid_step = _regime_volatile_spacing(regime, cfg_step_pct)

    # ------------------------------------------------------------------ #
    # 4. Net profit validation (with fee + slippage)
    # ------------------------------------------------------------------ #
    net_profit, profit_reasons = _validate_spacing_profit(
        grid_step, maker_fee, sell_fee, slippage, hard_min,
    )
    all_reasons.extend(profit_reasons)

    # ------------------------------------------------------------------ #
    # 5. Grid count
    # ------------------------------------------------------------------ #
    raw_levels, eff_upper, count_reasons = _compute_grid_count(
        cand_lower, cand_upper, grid_step,
        min_grids=min_grids,
        max_grids=min(max_grids, max_open_orders),
        max_levels_limit=max_levels_lim,
    )
    all_reasons.extend(count_reasons)

    grid_count = len(raw_levels) - 1 if len(raw_levels) >= 2 else 0
    levels_out: tuple[GridLevel, ...] = tuple(
        GridLevel(lv.index, lv.price) for lv in raw_levels
    )

    # ------------------------------------------------------------------ #
    # 6. Budget & inventory allocation
    # ------------------------------------------------------------------ #
    buy_quote, req_base, inv_ok, alloc_reasons = _allocate_budget(
        raw_levels, order_q_size, total_budget, current_price, available_base_inventory,
    )
    all_reasons.extend(alloc_reasons)

    # ------------------------------------------------------------------ #
    # 7. Price inside candidate range
    # ------------------------------------------------------------------ #
    if cand_lower > 0 and cand_upper > cand_lower:
        if current_price < cand_lower or current_price > cand_upper:
            all_reasons.append(PlanBlockReason.PRICE_OUTSIDE_CANDIDATE_RANGE)

    # ------------------------------------------------------------------ #
    # 8. Plan ID (deterministic hash of logical inputs)
    # ------------------------------------------------------------------ #
    plan_id = compute_plan_id(
        pair, cand_lower, cand_upper, grid_step, grid_count,
        regime, cfg_step_pct, hard_min,
    )

    # ------------------------------------------------------------------ #
    # 9. If anything is blocking, return GRID_BLOCKED
    # ------------------------------------------------------------------ #
    if all_reasons:
        return AdaptiveGridPlan(
            plan_id=plan_id,
            pair=pair,
            regime=regime,
            range_quality_score=range_quality_score,
            candidate_lower=cand_lower,
            candidate_upper=cand_upper,
            grid_type="GEOMETRIC",
            grid_step=grid_step,
            grid_count=grid_count,
            levels=levels_out,
            total_quote_budget=total_budget,
            buy_quote_budget=buy_quote,
            required_base_inventory=req_base,
            available_base_inventory=available_base_inventory,
            inventory_sufficient=inv_ok,
            estimated_net_profit_per_grid=net_profit,
            decision=PlanDecision.GRID_BLOCKED,
            reasons=tuple(all_reasons),
        )

    # ------------------------------------------------------------------ #
    # 10. Cooldown check (uses closed-candle index, not wall-clock)
    # ------------------------------------------------------------------ #
    if _cooldown_active(active_plan, current_candle_index, cooldown_candles):
        return AdaptiveGridPlan(
            plan_id=plan_id,
            pair=pair,
            regime=regime,
            range_quality_score=range_quality_score,
            candidate_lower=cand_lower,
            candidate_upper=cand_upper,
            grid_type="GEOMETRIC",
            grid_step=grid_step,
            grid_count=grid_count,
            levels=levels_out,
            total_quote_budget=total_budget,
            buy_quote_budget=buy_quote,
            required_base_inventory=req_base,
            available_base_inventory=available_base_inventory,
            inventory_sufficient=inv_ok,
            estimated_net_profit_per_grid=net_profit,
            decision=PlanDecision.GRID_BLOCKED,
            reasons=(PlanBlockReason.COOLDOWN_ACTIVE,),
        )

    # ------------------------------------------------------------------ #
    # 11. Hysteresis check
    # ------------------------------------------------------------------ #
    if active_plan is not None:
        material_change = _check_hysteresis(
            active_plan,
            cand_lower, cand_upper, grid_step, grid_count,
            regime, range_quality_score, hyst_cfg,
        )
        if not material_change:
            return AdaptiveGridPlan(
                plan_id=plan_id,
                pair=pair,
                regime=regime,
                range_quality_score=range_quality_score,
                candidate_lower=cand_lower,
                candidate_upper=cand_upper,
                grid_type="GEOMETRIC",
                grid_step=grid_step,
                grid_count=grid_count,
                levels=levels_out,
                total_quote_budget=total_budget,
                buy_quote_budget=buy_quote,
                required_base_inventory=req_base,
                available_base_inventory=available_base_inventory,
                inventory_sufficient=inv_ok,
                estimated_net_profit_per_grid=net_profit,
                decision=PlanDecision.KEEP_CURRENT_PLAN,
                reasons=(PlanBlockReason.HYSTERESIS_NOT_TRIGGERED,),
            )
        # Material change → RECONFIGURATION_REQUIRED
        return AdaptiveGridPlan(
            plan_id=plan_id,
            pair=pair,
            regime=regime,
            range_quality_score=range_quality_score,
            candidate_lower=cand_lower,
            candidate_upper=cand_upper,
            grid_type="GEOMETRIC",
            grid_step=grid_step,
            grid_count=grid_count,
            levels=levels_out,
            total_quote_budget=total_budget,
            buy_quote_budget=buy_quote,
            required_base_inventory=req_base,
            available_base_inventory=available_base_inventory,
            inventory_sufficient=inv_ok,
            estimated_net_profit_per_grid=net_profit,
            decision=PlanDecision.RECONFIGURATION_REQUIRED,
            reasons=(),
        )

    # ------------------------------------------------------------------ #
    # 12. No active plan → fresh GRID_ALLOWED
    # ------------------------------------------------------------------ #
    return AdaptiveGridPlan(
        plan_id=plan_id,
        pair=pair,
        regime=regime,
        range_quality_score=range_quality_score,
        candidate_lower=cand_lower,
        candidate_upper=cand_upper,
        grid_type="GEOMETRIC",
        grid_step=grid_step,
        grid_count=grid_count,
        levels=levels_out,
        total_quote_budget=total_budget,
        buy_quote_budget=buy_quote,
        required_base_inventory=req_base,
        available_base_inventory=available_base_inventory,
        inventory_sufficient=inv_ok,
        estimated_net_profit_per_grid=net_profit,
        decision=PlanDecision.GRID_ALLOWED,
        reasons=(),
    )


# ---------------------------------------------------------------------------
# Internal helper for early-blocked plans
# ---------------------------------------------------------------------------

def _make_blocked_plan(
    pair: str,
    regime: MarketRegime,
    range_quality_score: Decimal,
    configured_lower: Decimal,
    configured_upper: Decimal,
    total_quote_budget: Decimal,
    available_base: Decimal,
    cfg_step_pct: Decimal,
    hard_min: Decimal,
    reasons: list[PlanBlockReason],
) -> AdaptiveGridPlan:
    plan_id = compute_plan_id(
        pair, configured_lower, configured_upper,
        cfg_step_pct, 0, regime, cfg_step_pct, hard_min,
    )
    return AdaptiveGridPlan(
        plan_id=plan_id,
        pair=pair,
        regime=regime,
        range_quality_score=range_quality_score,
        candidate_lower=configured_lower,
        candidate_upper=configured_upper,
        grid_type="GEOMETRIC",
        grid_step=cfg_step_pct,
        grid_count=0,
        levels=(),
        total_quote_budget=total_quote_budget,
        buy_quote_budget=_D("0"),
        required_base_inventory=_D("0"),
        available_base_inventory=available_base,
        inventory_sufficient=False,
        estimated_net_profit_per_grid=_D("0"),
        decision=PlanDecision.GRID_BLOCKED,
        reasons=tuple(reasons),
    )
