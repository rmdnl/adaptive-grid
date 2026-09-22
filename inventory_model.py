"""Phase 5C – Inventory-Aware Grid Management.

Deterministic inventory allocation for adaptive grid planning.
Planning/allocation only — no order submission, no accounting mutation,
no Binance calls, no reservation, no DB writes.

Core principles:
- Inventory derived from validated account/paper state only
- Never fabricate base inventory
- Never exceed available quote balance or TOTAL_QUOTE_BUDGET
- SELL cells require actual base inventory
- Immutable structured output with explicit reason codes
- Decimal-only monetary calculations
- No wall-clock time, no randomness (input-set identical -> result identical)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, getcontext
from enum import Enum
from typing import Any

from grid_engine import GridLevel
from symbol_rules import (
    SymbolRules,
    quantize_price as _sym_quantize_price,
    quantize_quantity as _sym_quantize_quantity,
    validate_notional as _sym_validate_notional,
    validate_percent_price as _sym_validate_percent_price,
)

getcontext().prec = 40


def _D(value: Any) -> Decimal:
    """Convert to Decimal with string coercion."""
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class InventoryAllocationStatus(str, Enum):
    """Top-level allocation status."""
    VALID = "VALID"
    PARTIALLY_FUNDABLE = "PARTIALLY_FUNDABLE"
    UNFUNDED = "UNFUNDED"
    BLOCKED = "BLOCKED"


class AllocationReasonCode(str, Enum):
    """Explicit structured reason codes — never vague strings."""
    INSUFFICIENT_QUOTE = "INSUFFICIENT_QUOTE"
    INSUFFICIENT_BASE = "INSUFFICIENT_BASE"
    BUDGET_LIMIT = "BUDGET_LIMIT"
    MIN_NOTIONAL_FAILURE = "MIN_NOTIONAL_FAILURE"
    QUANTITY_ROUNDING_FAILURE = "QUANTITY_ROUNDING_FAILURE"
    INVENTORY_TARGET_EXCEEDED = "INVENTORY_TARGET_EXCEEDED"
    RISK_BLOCKED = "RISK_BLOCKED"
    PLAN_BLOCKED = "PLAN_BLOCKED"
    STALE_GENERATION = "STALE_GENERATION"
    NO_ACTIVE_GRID = "NO_ACTIVE_GRID"
    RECONFIGURATION_PENDING = "RECONFIGURATION_PENDING"
    SYMBOL_RULE_VIOLATION = "SYMBOL_RULE_VIOLATION"
    ZERO_BUY_CELLS = "ZERO_BUY_CELLS"
    ZERO_SELL_CELLS = "ZERO_SELL_CELLS"


class InventoryBias(str, Enum):
    """Inventory target bias — information/allocation state only, never trades."""
    UNDERWEIGHT_BASE = "UNDERWEIGHT_BASE"
    BALANCED = "BALANCED"
    OVERWEIGHT_BASE = "OVERWEIGHT_BASE"


class LifecycleState(str, Enum):
    """Phase 5B lifecycle states for integration."""
    NO_ACTIVE_GRID = "NO_ACTIVE_GRID"
    ACTIVE = "ACTIVE"
    RECONFIGURATION_PENDING = "RECONFIGURATION_PENDING"
    READY_TO_RECONFIGURE = "READY_TO_RECONFIGURE"
    BLOCKED = "BLOCKED"


# ---------------------------------------------------------------------------
# Data models — immutable
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InventorySnapshot:
    """Validated account/paper state snapshot — read-only, immutable."""
    base_asset: str
    quote_asset: str
    base_free: Decimal
    base_reserved: Decimal
    quote_free: Decimal
    quote_reserved: Decimal
    current_price: Decimal
    average_cost: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    total_fees: Decimal = Decimal("0")

    @classmethod
    def from_paper_state(cls, state: Any, current_price: Decimal) -> "InventorySnapshot":
        """Build from an existing Phase 3 PaperAccountState (or state dict).

        Inventory must be derived from validated account/paper state — this is
        the only sanctioned source of balances.  No synthetic inventory, ever.
        """
        if isinstance(state, dict):
            base_free = _D(state["base_free"])
            base_reserved = _D(state["base_reserved"])
            quote_free = _D(state["quote_free"])
            quote_reserved = _D(state["quote_reserved"])
            base_asset = str(state["base_asset"])
            quote_asset = str(state["quote_asset"])
            average_cost = _D(state.get("average_cost", "0"))
            realized_pnl = _D(state.get("realized_pnl", "0"))
            total_fees = _D(state.get("total_fees", "0"))
        else:
            base_free = _D(state.base_free)
            base_reserved = _D(state.base_reserved)
            quote_free = _D(state.quote_free)
            quote_reserved = _D(state.quote_reserved)
            base_asset = str(state.base_asset)
            quote_asset = str(state.quote_asset)
            average_cost = _D(state.average_cost)
            realized_pnl = _D(state.realized_pnl)
            total_fees = _D(state.total_fees)
        return cls(
            base_asset=base_asset,
            quote_asset=quote_asset,
            base_free=base_free,
            base_reserved=base_reserved,
            quote_free=quote_free,
            quote_reserved=quote_reserved,
            current_price=_D(current_price),
            average_cost=average_cost,
            realized_pnl=realized_pnl,
            total_fees=total_fees,
        )

    @property
    def base_total(self) -> Decimal:
        return self.base_free + self.base_reserved

    @property
    def quote_total(self) -> Decimal:
        return self.quote_free + self.quote_reserved

    @property
    def equity(self) -> Decimal:
        """Total equity in quote terms."""
        return self.quote_total + self.base_total * self.current_price

    @property
    def inventory_value(self) -> Decimal:
        """Base inventory valued at current price."""
        return self.base_total * self.current_price

    @property
    def inventory_pct(self) -> Decimal:
        """Base inventory as percentage of total equity."""
        eq = self.equity
        if eq == 0:
            return Decimal("0")
        return (self.inventory_value / eq).quantize(Decimal("0.00000001"))

    @property
    def quote_pct(self) -> Decimal:
        """Quote as percentage of total equity."""
        eq = self.equity
        if eq == 0:
            return Decimal("0")
        return (self.quote_total / eq).quantize(Decimal("0.00000001"))

    @property
    def available_base(self) -> Decimal:
        """Base available for SELL cells (= free, not reserved)."""
        return self.base_free

    @property
    def available_quote(self) -> Decimal:
        """Quote available for BUY cells (= free, not reserved)."""
        return self.quote_free


@dataclass(frozen=True)
class InventoryTarget:
    """Deterministic configurable inventory target — information only.

    Bounds invariant: 0 <= min <= target <= max <= 1.
    """
    target_inventory_pct: Decimal
    min_inventory_pct: Decimal
    max_inventory_pct: Decimal

    def __post_init__(self) -> None:
        if not (
            Decimal("0") <= self.min_inventory_pct
            <= self.target_inventory_pct
            <= self.max_inventory_pct
            <= Decimal("1")
        ):
            raise ValueError(
                "Invalid inventory target bounds: "
                "0 <= min <= target <= max <= 1"
            )

    def classify(self, current_pct: Decimal) -> InventoryBias:
        """Classify current inventory vs target.  NEVER triggers trades."""
        current = _D(current_pct)
        if current < self.min_inventory_pct:
            return InventoryBias.UNDERWEIGHT_BASE
        if current > self.max_inventory_pct:
            return InventoryBias.OVERWEIGHT_BASE
        return InventoryBias.BALANCED

    def within_bounds(self, current_pct: Decimal) -> bool:
        """Boundary-inclusive check."""
        current = _D(current_pct)
        return self.min_inventory_pct <= current <= self.max_inventory_pct


@dataclass(frozen=True)
class GridCellAllocation:
    """Individual grid cell allocation — immutable."""
    index: int
    buy_price: Decimal
    sell_price: Decimal
    quantity: Decimal
    buy_notional: Decimal          # price * quantity for BUY (quote spent)
    sell_notional: Decimal         # price * quantity for SELL (quote received)
    side: str                      # "BUY" | "SELL"
    allowed: bool
    reason_codes: tuple[AllocationReasonCode, ...]


@dataclass(frozen=True)
class InventoryGridAllocation:
    """Immutable structured allocation result for a candidate plan."""
    plan_id: str
    generation: int
    status: InventoryAllocationStatus
    buy_cells: tuple[GridCellAllocation, ...]
    sell_cells: tuple[GridCellAllocation, ...]
    funded_buy_quote: Decimal          # quote committed to BUY cells (incl. fee buffer)
    required_sell_base: Decimal        # base committed to SELL cells
    available_quote: Decimal
    available_base: Decimal
    inventory_pct: Decimal
    target_inventory_pct: Decimal
    inventory_bias: InventoryBias
    shortfall_quote: Decimal           # >0 only if the grid cannot be fully funded
    shortfall_base: Decimal
    reason_codes: tuple[AllocationReasonCode, ...]
    lifecycle_state: LifecycleState
    hash: str                          # deterministic hash of allocation inputs

    @property
    def is_actionable(self) -> bool:
        """VALID only — PARTIALLY_FUNDABLE/UNFUNDED/BLOCKED are not executable."""
        return self.status == InventoryAllocationStatus.VALID


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _inventory_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("inventory", {})


def _exec_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("execution", {})


def _grid_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("grid", {})


def _fees_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("fees", {})


def load_inventory_target(cfg: dict[str, Any]) -> InventoryTarget:
    """Load inventory target configuration from config."""
    inv_cfg = _inventory_cfg(cfg)
    return InventoryTarget(
        target_inventory_pct=_D(inv_cfg.get("target_inventory_pct", "0.50")),
        min_inventory_pct=_D(inv_cfg.get("min_inventory_pct", "0.30")),
        max_inventory_pct=_D(inv_cfg.get("max_inventory_pct", "0.70")),
    )


def _fee_buffer_rate(cfg: dict[str, Any]) -> Decimal:
    """Conservative fee+slippage buffer for BUY funding requirements.

    required_quote = price * quantity * (1 + max(buy_fee, taker) + slippage)
    Using the largest applicable rate is deterministic and conservative.
    """
    fees = _fees_cfg(cfg)
    maker = _D(fees.get("maker_fee_fallback", "0.001"))
    taker = _D(fees.get("taker_fee_fallback", "0.001"))
    slippage = _D(fees.get("slippage_roundtrip_pct", "0.0005"))
    return max(maker, taker) + slippage


# ---------------------------------------------------------------------------
# Deterministic allocation hash
# ---------------------------------------------------------------------------

def _allocation_hash(
    plan_id: str,
    generation: int,
    snapshot: InventorySnapshot,
    target: InventoryTarget,
    rules: SymbolRules,
    cfg: dict[str, Any],
) -> str:
    """Deterministic hash of all logical allocation inputs."""
    payload = json.dumps(
        {
            "plan_id": plan_id,
            "generation": generation,
            "base_free": str(snapshot.base_free),
            "base_reserved": str(snapshot.base_reserved),
            "quote_free": str(snapshot.quote_free),
            "quote_reserved": str(snapshot.quote_reserved),
            "current_price": str(snapshot.current_price),
            "target_pct": str(target.target_inventory_pct),
            "min_pct": str(target.min_inventory_pct),
            "max_pct": str(target.max_inventory_pct),
            "symbol": rules.symbol,
            "tick_size": str(rules.tick_size),
            "step_size": str(rules.step_size),
            "min_qty": str(rules.min_qty),
            "min_notional": str(rules.min_notional),
            "total_quote_budget": str(
                _D(_exec_cfg(cfg).get("total_quote_budget", "0"))
            ),
            "order_quote_size": str(
                _D(_exec_cfg(cfg).get("order_quote_size", "25"))
            ),
            "total_quote_budget_active": bool(
                _D(_exec_cfg(cfg).get("total_quote_budget", "0")) > 0
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "alloc_" + hashlib.sha256(payload.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Symbol rule helpers (reuse existing Phase 2/3 infrastructure)
# ---------------------------------------------------------------------------

def _quantize_price(price: Decimal, rules: SymbolRules) -> Decimal:
    return _sym_quantize_price(price, rules)


def _quantize_quantity(qty: Decimal, rules: SymbolRules) -> Decimal:
    return _sym_quantize_quantity(qty, rules)


def _validated_buy_quantity(
    level: GridLevel, order_quote_size: Decimal, rules: SymbolRules
) -> tuple[Decimal, Decimal, bool]:
    """Raw validated BUY quantity/price.

    Returns (buy_price, quantity, ok).  ok=False when symbol rules reject the
    cell (minQty / notional / percent-price violations).
    """
    try:
        buy_price = _quantize_price(level.price, rules)
        if buy_price <= 0:
            return Decimal("0"), Decimal("0"), False
        raw_qty = order_quote_size / buy_price
        qty = _quantize_quantity(raw_qty, rules)
        if qty <= 0:
            return buy_price, Decimal("0"), False
        _sym_validate_notional(buy_price, qty, rules)
        return buy_price, qty, True
    except Exception:
        return Decimal("0"), Decimal("0"), False


def _validated_sell_quantity(
    level: GridLevel, order_quote_size: Decimal, rules: SymbolRules
) -> tuple[Decimal, Decimal, bool]:
    """Raw validated SELL quantity/price.

    Returns (sell_price, quantity, ok).  ok=False when symbol rules reject the
    cell (minQty / notional / percent-price violations).
    """
    try:
        sell_price = _quantize_price(level.price, rules)
        if sell_price <= 0:
            return Decimal("0"), Decimal("0"), False
        raw_qty = order_quote_size / sell_price
        qty = _quantize_quantity(raw_qty, rules)
        if qty <= 0:
            return sell_price, Decimal("0"), False
        _sym_validate_notional(sell_price, qty, rules)
        return sell_price, qty, True
    except Exception:
        return Decimal("0"), Decimal("0"), False


# ---------------------------------------------------------------------------
# Core allocation algorithm
# ---------------------------------------------------------------------------

def allocate_grid(
    plan_id: str,
    generation: int,
    levels: tuple[GridLevel, ...],
    snapshot: InventorySnapshot,
    rules: SymbolRules,
    cfg: dict[str, Any],
    lifecycle_state: LifecycleState = LifecycleState.ACTIVE,
    expected_generation: int | None = None,
    risk_allowed: bool = True,
) -> InventoryGridAllocation:
    """Calculate deterministic grid funding allocation.

    Single entry point for Phase 5C inventory-aware allocation.
    NEVER mutates state, NEVER submits orders, NEVER calls Binance.

    ``expected_generation`` when provided verifies the candidate generation is
    still current (STALE_GENERATION fails closed on mismatch).
    ``risk_allowed`` gates on the current risk-engine decision (RISK_BLOCKED).
    """
    exec_cfg = _exec_cfg(cfg)

    # Risk gate: inventory bias / allocation state must never override the
    # current risk engine veto.
    if not risk_allowed:
        return _blocked_allocation(
            plan_id, generation, snapshot, cfg,
            AllocationReasonCode.RISK_BLOCKED, lifecycle_state,
        )

    # Generation gate: candidate must still be current.
    if expected_generation is not None and generation != expected_generation:
        return _blocked_allocation(
            plan_id, generation, snapshot, cfg,
            AllocationReasonCode.STALE_GENERATION, lifecycle_state,
        )

    target = load_inventory_target(cfg)

    # Lifecycle gating.
    if lifecycle_state == LifecycleState.NO_ACTIVE_GRID:
        return _blocked_allocation(
            plan_id, generation, snapshot, cfg,
            AllocationReasonCode.NO_ACTIVE_GRID, lifecycle_state,
        )
    if lifecycle_state == LifecycleState.BLOCKED:
        return _blocked_allocation(
            plan_id, generation, snapshot, cfg,
            AllocationReasonCode.PLAN_BLOCKED, lifecycle_state,
        )

    # Compute in every other lifecycle state (ACTIVE / PENDING / READY).
    return _compute_allocation(
        plan_id, generation, levels, snapshot, rules, cfg, target, lifecycle_state,
    )


def _blocked_allocation(
    plan_id: str,
    generation: int,
    snapshot: InventorySnapshot,
    cfg: dict[str, Any],
    reason_code: AllocationReasonCode,
    lifecycle_state: LifecycleState,
) -> InventoryGridAllocation:
    """Fail-closed allocation with no cells."""
    rules = _noop_rules(cfg)
    target = load_inventory_target(cfg)
    return InventoryGridAllocation(
        plan_id=plan_id,
        generation=generation,
        status=InventoryAllocationStatus.BLOCKED,
        buy_cells=(),
        sell_cells=(),
        funded_buy_quote=Decimal("0"),
        required_sell_base=Decimal("0"),
        available_quote=snapshot.available_quote,
        available_base=snapshot.available_base,
        inventory_pct=snapshot.inventory_pct,
        target_inventory_pct=target.target_inventory_pct,
        inventory_bias=target.classify(snapshot.inventory_pct),
        shortfall_quote=Decimal("0"),
        shortfall_base=Decimal("0"),
        reason_codes=(reason_code,),
        lifecycle_state=lifecycle_state,
        hash=_allocation_hash(plan_id, generation, snapshot, target, rules, cfg),
    )


def _noop_rules(cfg: dict[str, Any]) -> SymbolRules:
    """Placeholder rules used only for hashing blocked allocations."""
    return SymbolRules(
        symbol="NONE",
        base_asset="BASE",
        quote_asset="QUOTE",
        status="TRADING",
        tick_size=Decimal("0"),
        min_price=Decimal("0"),
        max_price=Decimal("0"),
        step_size=Decimal("0"),
        min_qty=Decimal("0"),
        max_qty=Decimal("0"),
        market_step_size=Decimal("0"),
        market_min_qty=Decimal("0"),
        market_max_qty=Decimal("0"),
        min_notional=Decimal("0"),
        max_notional=Decimal("0"),
        percent_multiplier_up=Decimal("0"),
        percent_multiplier_down=Decimal("0"),
        percent_avg_mins=0,
        bid_multiplier_up=Decimal("0"),
        bid_multiplier_down=Decimal("0"),
        ask_multiplier_up=Decimal("0"),
        ask_multiplier_down=Decimal("0"),
        side_avg_mins=0,
        max_num_orders=0,
        max_num_algo_orders=0,
    )


def _compute_allocation(
    plan_id: str,
    generation: int,
    levels: tuple[GridLevel, ...],
    snapshot: InventorySnapshot,
    rules: SymbolRules,
    cfg: dict[str, Any],
    target: InventoryTarget,
    lifecycle_state: LifecycleState,
) -> InventoryGridAllocation:
    """Core allocation computation (shared by ACTIVE/PENDING/READY)."""
    all_reasons: list[AllocationReasonCode] = []
    exec_cfg = _exec_cfg(cfg)

    total_quote_budget = _D(exec_cfg.get("total_quote_budget", "0"))
    order_quote_size = _D(exec_cfg.get("order_quote_size", "25"))
    buffer_rate = _fee_buffer_rate(cfg)

    if rules.tick_size <= 0 or rules.step_size <= 0:
        return _blocked_allocation(
            plan_id, generation, snapshot, cfg,
            AllocationReasonCode.SYMBOL_RULE_VIOLATION, lifecycle_state,
        )

    current_price = snapshot.current_price
    if len(levels) < 2:
        return _blocked_allocation(
            plan_id, generation, snapshot, cfg,
            AllocationReasonCode.ZERO_BUY_CELLS, lifecycle_state,
        )

    # Split levels into BUY (below current) and SELL (at/above current) cells.
    pair_cells: list[tuple[GridLevel, GridLevel | None]] = list(
        zip(levels[:-1], levels[1:])
    )
    buy_levels = [p[0] for p in pair_cells if p[0].price < current_price]
    sell_levels = [p[1] if p[1] is not None else p[0] for p in pair_cells if p[0].price >= current_price]

    available_quote = snapshot.available_quote
    available_base = snapshot.available_base

    # ------------------------------------------------------------------ #
    # BUY cells
    # ------------------------------------------------------------------ #
    buy_cells: list[GridCellAllocation] = []
    funded_buy_quote = Decimal("0")
    ideal_buy_quote = Decimal("0")

    for lv in buy_levels:
        buy_price, qty, ok = _validated_buy_quantity(lv, order_quote_size, rules)
        if not ok or qty <= 0:
            code = _buy_failure_code(lv.price, order_quote_size, rules)
            all_reasons.append(code)
            buy_cells.append(
                GridCellAllocation(
                    index=lv.index, buy_price=buy_price, sell_price=Decimal("0"),
                    quantity=Decimal("0"), buy_notional=Decimal("0"),
                    sell_notional=Decimal("0"), side="BUY",
                    allowed=False, reason_codes=(code,),
                )
            )
            continue
        buy_notional = buy_price * qty
        required_quote = buy_notional * (Decimal("1") + buffer_rate)
        sell_price = _next_level_price(pair_cells, lv.index)
        ideal_buy_quote += required_quote

        # Budget cap.
        if total_quote_budget > 0 and funded_buy_quote + required_quote > total_quote_budget:
            buy_cells.append(
                GridCellAllocation(
                    index=lv.index, buy_price=buy_price, sell_price=sell_price,
                    quantity=Decimal("0"), buy_notional=Decimal("0"),
                    sell_notional=Decimal("0"), side="BUY",
                    allowed=False, reason_codes=(AllocationReasonCode.BUDGET_LIMIT,),
                )
            )
            continue
        # Available quote balance.
        if available_quote < funded_buy_quote + required_quote:
            buy_cells.append(
                GridCellAllocation(
                    index=lv.index, buy_price=buy_price, sell_price=sell_price,
                    quantity=Decimal("0"), buy_notional=Decimal("0"),
                    sell_notional=Decimal("0"), side="BUY",
                    allowed=False,
                    reason_codes=(AllocationReasonCode.INSUFFICIENT_QUOTE,),
                )
            )
            continue

        buy_cells.append(
            GridCellAllocation(
                index=lv.index, buy_price=buy_price, sell_price=sell_price,
                quantity=qty, buy_notional=buy_notional,
                sell_notional=Decimal("0"), side="BUY",
                allowed=True, reason_codes=(),
            )
        )
        funded_buy_quote += required_quote

    # ------------------------------------------------------------------ #
    # SELL cells — SELL orders require actual base inventory.
    # ------------------------------------------------------------------ #
    sell_cells: list[GridCellAllocation] = []
    required_sell_base = Decimal("0")
    ideal_sell_base = Decimal("0")

    for lv in sell_levels:
        sell_price, qty, ok = _validated_sell_quantity(lv, order_quote_size, rules)
        if not ok or qty <= 0:
            code = _sell_failure_code(lv.price, order_quote_size, rules)
            all_reasons.append(code)
            sell_cells.append(
                GridCellAllocation(
                    index=lv.index, buy_price=Decimal("0"), sell_price=sell_price,
                    quantity=Decimal("0"), buy_notional=Decimal("0"),
                    sell_notional=Decimal("0"), side="SELL",
                    allowed=False, reason_codes=(code,),
                )
            )
            continue
        sell_notional = sell_price * qty
        ideal_sell_base += qty

        # Inventory constraint: never fund a SELL cell without real base.
        if available_base < required_sell_base + qty:
            sell_cells.append(
                GridCellAllocation(
                    index=lv.index, buy_price=Decimal("0"), sell_price=sell_price,
                    quantity=Decimal("0"), buy_notional=Decimal("0"),
                    sell_notional=Decimal("0"), side="SELL",
                    allowed=False,
                    reason_codes=(AllocationReasonCode.INSUFFICIENT_BASE,),
                )
            )
            continue

        sell_cells.append(
            GridCellAllocation(
                index=lv.index, buy_price=Decimal("0"), sell_price=sell_price,
                quantity=qty, buy_notional=Decimal("0"),
                sell_notional=sell_notional, side="SELL",
                allowed=True, reason_codes=(),
            )
        )
        required_sell_base += qty

    # ------------------------------------------------------------------ #
    # Aggregate shortfalls (informational: how much more would be needed to
    # fully fund the grid).
    # ------------------------------------------------------------------ #
    shortfall_quote = max(Decimal("0"), ideal_buy_quote - available_quote)
    shortfall_base = max(Decimal("0"), ideal_sell_base - available_base)
    if shortfall_quote > 0:
        all_reasons.append(AllocationReasonCode.INSUFFICIENT_QUOTE)
    if shortfall_base > 0:
        all_reasons.append(AllocationReasonCode.INSUFFICIENT_BASE)
    if total_quote_budget > 0 and ideal_buy_quote > total_quote_budget:
        all_reasons.append(AllocationReasonCode.BUDGET_LIMIT)

    # Inventory target is INFORMATION ONLY — never blocks, never trades.
    if not target.within_bounds(snapshot.inventory_pct):
        all_reasons.append(AllocationReasonCode.INVENTORY_TARGET_EXCEEDED)

    if lifecycle_state == LifecycleState.RECONFIGURATION_PENDING:
        all_reasons.append(AllocationReasonCode.RECONFIGURATION_PENDING)

    status = _determine_status(buy_cells, sell_cells, all_reasons)
    unique_reasons = tuple(dict.fromkeys(all_reasons).keys())

    return InventoryGridAllocation(
        plan_id=plan_id,
        generation=generation,
        status=status,
        buy_cells=tuple(buy_cells),
        sell_cells=tuple(sell_cells),
        funded_buy_quote=funded_buy_quote,
        required_sell_base=required_sell_base,
        available_quote=available_quote,
        available_base=available_base,
        inventory_pct=snapshot.inventory_pct,
        target_inventory_pct=target.target_inventory_pct,
        inventory_bias=target.classify(snapshot.inventory_pct),
        shortfall_quote=shortfall_quote,
        shortfall_base=shortfall_base,
        reason_codes=unique_reasons,
        lifecycle_state=lifecycle_state,
        hash=_allocation_hash(plan_id, generation, snapshot, target, rules, cfg),
    )


def _next_level_price(
    pair_cells: list[tuple[GridLevel, GridLevel | None]], index: int
) -> Decimal:
    """Return the SELL price paired with a BUY level (upper level of the cell)."""
    for lower, upper in pair_cells:
        if lower.index == index:
            return upper.price if upper is not None else Decimal("0")
    return Decimal("0")


def _buy_failure_code(
    price: Decimal, order_quote_size: Decimal, rules: SymbolRules
) -> AllocationReasonCode:
    """Classify a symbol-level BUY failure reason."""
    try:
        qty = _quantize_quantity(order_quote_size / price, rules)
        return AllocationReasonCode.MIN_NOTIONAL_FAILURE
    except Exception:
        return AllocationReasonCode.QUANTITY_ROUNDING_FAILURE


def _sell_failure_code(
    price: Decimal, order_quote_size: Decimal, rules: SymbolRules
) -> AllocationReasonCode:
    """Classify a symbol-level SELL failure reason."""
    try:
        qty = _quantize_quantity(order_quote_size / price, rules)
        return AllocationReasonCode.MIN_NOTIONAL_FAILURE
    except Exception:
        return AllocationReasonCode.QUANTITY_ROUNDING_FAILURE


def _determine_status(
    buy_cells: list[GridCellAllocation],
    sell_cells: list[GridCellAllocation],
    reasons: list[AllocationReasonCode],
) -> InventoryAllocationStatus:
    """Determine overall status from per-cell outcomes."""
    hard_blocks = {
        AllocationReasonCode.INSUFFICIENT_QUOTE,
        AllocationReasonCode.INSUFFICIENT_BASE,
        AllocationReasonCode.BUDGET_LIMIT,
        AllocationReasonCode.MIN_NOTIONAL_FAILURE,
        AllocationReasonCode.QUANTITY_ROUNDING_FAILURE,
        AllocationReasonCode.SYMBOL_RULE_VIOLATION,
        AllocationReasonCode.RISK_BLOCKED,
        AllocationReasonCode.PLAN_BLOCKED,
        AllocationReasonCode.STALE_GENERATION,
    }
    any_allowed = any(c.allowed for c in buy_cells) or any(
        c.allowed for c in sell_cells
    )

    if any(r in hard_blocks for r in reasons):
        return (
            InventoryAllocationStatus.PARTIALLY_FUNDABLE
            if any_allowed
            else InventoryAllocationStatus.UNFUNDED
        )

    if any_allowed:
        return InventoryAllocationStatus.VALID
    return InventoryAllocationStatus.UNFUNDED


# ---------------------------------------------------------------------------
# Lifecycle integration helpers
# ---------------------------------------------------------------------------

def can_execute_allocation(allocation: InventoryGridAllocation) -> bool:
    """Whether an allocation may be executed.

    - status must be VALID
    - NO_ACTIVE_GRID / BLOCKED: never
    - RECONFIGURATION_PENDING: informational only, never
    - ACTIVE / READY_TO_RECONFIGURE: yes
    """
    if allocation.status != InventoryAllocationStatus.VALID:
        return False
    if allocation.lifecycle_state in (
        LifecycleState.NO_ACTIVE_GRID,
        LifecycleState.BLOCKED,
        LifecycleState.RECONFIGURATION_PENDING,
    ):
        return False
    return True


def filter_actionable_cells(
    allocation: InventoryGridAllocation,
) -> tuple[list[GridCellAllocation], list[GridCellAllocation]]:
    """Extract allowed (actionable) buy/sell cells."""
    buy = [c for c in allocation.buy_cells if c.allowed]
    sell = [c for c in allocation.sell_cells if c.allowed]
    return buy, sell


# ---------------------------------------------------------------------------
# Restart / reconstruction determinism
# ---------------------------------------------------------------------------

def reconstruct_allocation(
    plan_id: str,
    generation: int,
    levels: tuple[GridLevel, ...],
    snapshot: InventorySnapshot,
    rules: SymbolRules,
    cfg: dict[str, Any],
    lifecycle_state: LifecycleState = LifecycleState.ACTIVE,
    expected_generation: int | None = None,
    risk_allowed: bool = True,
) -> InventoryGridAllocation:
    """Reconstruct allocation from identical inputs.

    Identical inputs MUST produce an identical allocation (restart/recovery
    determinism).  No randomness; no wall-clock time.
    """
    return allocate_grid(
        plan_id, generation, levels, snapshot, rules, cfg,
        lifecycle_state=lifecycle_state,
        expected_generation=expected_generation,
        risk_allowed=risk_allowed,
    )


def allocation_equal(a: InventoryGridAllocation, b: InventoryGridAllocation) -> bool:
    """Identity check for determinism verification."""
    return a.hash == b.hash and a == b