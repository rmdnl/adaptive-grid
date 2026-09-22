"""Tests for Phase 5C Inventory-Aware Grid Management.

Covers the specification test matrix (A-Z):
- zero base / sufficient / insufficient base
- zero quote / insufficient quote
- budget cap, quantity rounding, min-notional
- both-side allocation + conservation
- inventory target boundaries (UNDERWEIGHT / BALANCED / OVERWEIGHT)
- lifecycle states, stale generation, risk gate
- determinism, no mutation, no order / no Binance
- restart / reconstruction, Decimal-only
- Phase 3 PaperAccountState and Phase 4 AdaptiveGridPlan compatibility
- immutability and fail-closed statuses
"""

import dataclasses
from decimal import Decimal
from typing import Any

import pytest

from grid_engine import GridLevel, build_geometric_grid
from grid_planner import AdaptiveGridPlan, PlanDecision, evaluate_adaptive_grid_plan
from inventory_model import (
    AllocationReasonCode,
    GridCellAllocation,
    InventoryAllocationStatus,
    InventoryBias,
    InventoryGridAllocation,
    InventorySnapshot,
    InventoryTarget,
    LifecycleState,
    _fee_buffer_rate,
    allocate_grid,
    allocation_equal,
    can_execute_allocation,
    filter_actionable_cells,
    load_inventory_target,
    reconstruct_allocation,
)
from market_regime import MarketRegime
from symbol_rules import SymbolRules


D = Decimal


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def base_cfg() -> dict[str, Any]:
    return {
        "grid": {
            "step_pct": 0.006,
            "hard_min_net_pct": 0.003,
            "min_cells": 6,
            "max_levels": 40,
        },
        "execution": {
            "order_quote_size": 25,
            "total_quote_budget": 0,
            "max_open_orders": 40,
        },
        "fees": {
            "maker_fee_fallback": 0.001,
            "taker_fee_fallback": 0.001,
            "slippage_roundtrip_pct": 0.0005,
        },
        "inventory": {
            "target_inventory_pct": 0.50,
            "min_inventory_pct": 0.30,
            "max_inventory_pct": 0.70,
        },
    }


def make_rules(
    tick_size: str = "0.001",
    step_size: str = "0.001",
    min_qty: str = "0.01",
    min_notional: str = "10",
) -> SymbolRules:
    return SymbolRules(
        symbol="BNBUSDT",
        base_asset="BNB",
        quote_asset="USDT",
        status="TRADING",
        tick_size=D(tick_size),
        min_price=D("0"),
        max_price=D("0"),
        step_size=D(step_size),
        min_qty=D(min_qty),
        max_qty=D("0"),
        market_step_size=D(step_size),
        market_min_qty=D(min_qty),
        market_max_qty=D("0"),
        min_notional=D(min_notional),
        max_notional=D("0"),
        percent_multiplier_up=D("0"),
        percent_multiplier_down=D("0"),
        percent_avg_mins=0,
        bid_multiplier_up=D("0"),
        bid_multiplier_down=D("0"),
        ask_multiplier_up=D("0"),
        ask_multiplier_down=D("0"),
        side_avg_mins=0,
        max_num_orders=0,
        max_num_algo_orders=0,
    )


def make_levels() -> tuple[GridLevel, ...]:
    """Six levels around a current price of 600 -> 3 BUY + 2 SELL cells.

    BUY cells:  (590, 594, 598)
    SELL cells: (606, 610)
    """
    return (
        GridLevel(0, D("590")),
        GridLevel(1, D("594")),
        GridLevel(2, D("598")),
        GridLevel(3, D("602")),
        GridLevel(4, D("606")),
        GridLevel(5, D("610")),
    )


def make_snapshot(
    base_free: str = "2",
    base_reserved: str = "0",
    quote_free: str = "1000",
    quote_reserved: str = "0",
    price: str = "600",
) -> InventorySnapshot:
    return InventorySnapshot(
        base_asset="BNB",
        quote_asset="USDT",
        base_free=D(base_free),
        base_reserved=D(base_reserved),
        quote_free=D(quote_free),
        quote_reserved=D(quote_reserved),
        current_price=D(price),
    )


def allocate(
    snapshot: InventorySnapshot,
    *,
    cfg: dict[str, Any] | None = None,
    levels: tuple[GridLevel, ...] | None = None,
    rules: SymbolRules | None = None,
    lifecycle: LifecycleState = LifecycleState.ACTIVE,
    expected_generation: int | None = None,
    risk_allowed: bool = True,
    plan_id: str = "plan_x",
    generation: int = 1,
) -> InventoryGridAllocation:
    return allocate_grid(
        plan_id=plan_id,
        generation=generation,
        levels=levels if levels is not None else make_levels(),
        snapshot=snapshot,
        rules=rules if rules is not None else make_rules(),
        cfg=cfg if cfg is not None else base_cfg(),
        lifecycle_state=lifecycle,
        expected_generation=expected_generation,
        risk_allowed=risk_allowed,
    )


# ---------------------------------------------------------------------------
# A. Zero base inventory -> no SELL cell may be funded
# ---------------------------------------------------------------------------

def test_a_zero_base_never_funds_sell():
    snap = make_snapshot(base_free="0")
    alloc = allocate(snap)

    assert alloc.status == InventoryAllocationStatus.PARTIALLY_FUNDABLE
    assert AllocationReasonCode.INSUFFICIENT_BASE in alloc.reason_codes
    assert all(not c.allowed for c in alloc.sell_cells)
    assert all(c.reason_codes == (AllocationReasonCode.INSUFFICIENT_BASE,) for c in alloc.sell_cells)
    assert alloc.required_sell_base == D("0")
    assert alloc.shortfall_base > 0
    # BUY side is unaffected by missing base inventory.
    assert all(c.allowed for c in alloc.buy_cells)
    assert not alloc.is_actionable


# ---------------------------------------------------------------------------
# B. Sufficient base + sufficient quote -> full VALID allocation
# ---------------------------------------------------------------------------

def test_b_sufficient_inventory_full_valid():
    snap = make_snapshot(base_free="2", quote_free="1000")
    alloc = allocate(snap)

    assert alloc.status == InventoryAllocationStatus.VALID
    assert alloc.is_actionable
    assert can_execute_allocation(alloc)
    assert alloc.reason_codes == ()
    assert len(alloc.buy_cells) == 3
    assert len(alloc.sell_cells) == 2
    assert all(c.allowed for c in alloc.buy_cells)
    assert all(c.allowed for c in alloc.sell_cells)
    assert alloc.funded_buy_quote == D("74.357369")
    assert alloc.required_sell_base == D("0.081")
    assert alloc.shortfall_quote == D("0")
    assert alloc.shortfall_base == D("0")


def test_b_snapshot_math():
    s = InventorySnapshot(
        base_asset="BNB", quote_asset="USDT",
        base_free=D("2"), base_reserved=D("1"),
        quote_free=D("1000"), quote_reserved=D("100"),
        current_price=D("600"),
    )
    assert s.base_total == D("3")
    assert s.quote_total == D("1100")
    assert s.inventory_value == D("1800")
    assert s.equity == D("2900")
    assert s.inventory_pct == D("0.62068966")   # 1800 / 2900
    assert s.quote_pct == D("0.37931034")       # 1100 / 2900
    assert s.available_base == D("2")           # free, not reserved
    assert s.available_quote == D("1000")       # free, not reserved


# ---------------------------------------------------------------------------
# C. Insufficient base -> partial SELL funding, fail closed
# ---------------------------------------------------------------------------

def test_c_insufficient_base_partially_fundable():
    # 0.05 base covers the first SELL cell (0.041) but not the second.
    snap = make_snapshot(base_free="0.05", quote_free="1000")
    alloc = allocate(snap)

    assert alloc.status == InventoryAllocationStatus.PARTIALLY_FUNDABLE
    assert AllocationReasonCode.INSUFFICIENT_BASE in alloc.reason_codes
    assert alloc.sell_cells[0].allowed
    assert alloc.sell_cells[1].quantity == D("0")
    assert not alloc.sell_cells[1].allowed
    assert alloc.sell_cells[1].reason_codes == (AllocationReasonCode.INSUFFICIENT_BASE,)
    assert alloc.required_sell_base == D("0.041")   # only first cell funded
    assert alloc.shortfall_base == D("0.031")       # ideal 0.081 - 0.05
    assert not alloc.is_actionable
    assert not can_execute_allocation(alloc)


# ---------------------------------------------------------------------------
# D. Zero quote -> no BUY cell may be funded
# ---------------------------------------------------------------------------

def test_d_zero_quote_never_funds_buy():
    snap = make_snapshot(base_free="0", quote_free="0")
    alloc = allocate(snap)

    assert alloc.status == InventoryAllocationStatus.UNFUNDED
    assert AllocationReasonCode.INSUFFICIENT_QUOTE in alloc.reason_codes
    assert AllocationReasonCode.INSUFFICIENT_BASE in alloc.reason_codes
    assert all(not c.allowed for c in alloc.buy_cells)
    assert all(c.reason_codes == (AllocationReasonCode.INSUFFICIENT_QUOTE,) for c in alloc.buy_cells)
    assert all(not c.allowed for c in alloc.sell_cells)
    assert alloc.funded_buy_quote == D("0")
    assert alloc.required_sell_base == D("0")
    assert not alloc.is_actionable


def test_d_zero_quote_with_base_only_sells():
    snap = make_snapshot(base_free="2", quote_free="0")
    alloc = allocate(snap)

    assert alloc.status == InventoryAllocationStatus.PARTIALLY_FUNDABLE
    assert AllocationReasonCode.INSUFFICIENT_QUOTE in alloc.reason_codes
    assert all(not c.allowed for c in alloc.buy_cells)
    assert all(c.allowed for c in alloc.sell_cells)  # base 2 >= 0.081
    assert alloc.funded_buy_quote == D("0")
    assert not alloc.is_actionable


# ---------------------------------------------------------------------------
# E. Insufficient quote -> deterministic partial BUY funding
# ---------------------------------------------------------------------------

def test_e_insufficient_quote_partially_fundable():
    # 30 free quote funds exactly one BUY cell (24.81717); the next needs
    # ~49.80, so it fails closed with INSUFFICIENT_QUOTE.
    snap = make_snapshot(base_free="0.1", quote_free="30")
    alloc = allocate(snap)

    assert alloc.status == InventoryAllocationStatus.PARTIALLY_FUNDABLE
    assert AllocationReasonCode.INSUFFICIENT_QUOTE in alloc.reason_codes
    assert alloc.buy_cells[0].allowed
    assert alloc.buy_cells[0].buy_notional == D("24.78")
    assert alloc.buy_cells[1].quantity == D("0")
    assert not alloc.buy_cells[1].allowed
    assert alloc.buy_cells[1].reason_codes == (AllocationReasonCode.INSUFFICIENT_QUOTE,)
    assert alloc.funded_buy_quote == D("24.81717")
    assert alloc.funded_buy_quote <= snap.available_quote
    assert alloc.shortfall_quote == D("44.357369")   # ideal 74.357369 - 30
    assert not alloc.is_actionable


# ---------------------------------------------------------------------------
# F. TOTAL_QUOTE_BUDGET cap is never exceeded
# ---------------------------------------------------------------------------

def test_f_budget_cap_respected():
    cfg = base_cfg()
    cfg["execution"]["total_quote_budget"] = 30
    snap = make_snapshot(base_free="2", quote_free="1000")
    alloc = allocate(snap, cfg=cfg)

    assert AllocationReasonCode.BUDGET_LIMIT in alloc.reason_codes
    assert alloc.status == InventoryAllocationStatus.PARTIALLY_FUNDABLE
    assert alloc.buy_cells[0].allowed
    assert alloc.buy_cells[1].reason_codes == (AllocationReasonCode.BUDGET_LIMIT,)
    assert alloc.buy_cells[2].reason_codes == (AllocationReasonCode.BUDGET_LIMIT,)
    assert alloc.funded_buy_quote == D("24.81717")
    assert alloc.funded_buy_quote <= D("30")
    assert alloc.funded_buy_quote <= D(cfg["execution"]["total_quote_budget"])
    # SELL side still fundable from actual base inventory.
    assert all(c.allowed for c in alloc.sell_cells)


# ---------------------------------------------------------------------------
# G. Quantity rounding failure -> fail closed with explicit code
# ---------------------------------------------------------------------------

def test_g_quantity_rounding_failure():
    # A huge step size rounds every quantity below step -> 0 -> rejected.
    rules = make_rules(step_size="10")
    snap = make_snapshot(base_free="2", quote_free="1000")
    alloc = allocate(snap, rules=rules)

    assert alloc.status == InventoryAllocationStatus.UNFUNDED
    assert AllocationReasonCode.QUANTITY_ROUNDING_FAILURE in alloc.reason_codes
    assert all(not c.allowed for c in alloc.buy_cells)
    assert all(not c.allowed for c in alloc.sell_cells)
    assert all(
        AllocationReasonCode.QUANTITY_ROUNDING_FAILURE in c.reason_codes
        for c in (*alloc.buy_cells, *alloc.sell_cells)
    )
    assert not alloc.is_actionable


# ---------------------------------------------------------------------------
# H. Min-notional failure -> explicit MIN_NOTIONAL_FAILURE
# ---------------------------------------------------------------------------

def test_h_min_notional_failure():
    rules = make_rules(min_notional="100")   # 25-quote orders can never reach it
    snap = make_snapshot(base_free="2", quote_free="1000")
    alloc = allocate(snap, rules=rules)

    assert AllocationReasonCode.MIN_NOTIONAL_FAILURE in alloc.reason_codes
    assert all(not c.allowed for c in alloc.buy_cells)
    assert all(not c.allowed for c in alloc.sell_cells)
    assert all(
        AllocationReasonCode.MIN_NOTIONAL_FAILURE in c.reason_codes
        for c in (*alloc.buy_cells, *alloc.sell_cells)
    )
    assert not alloc.is_actionable


# ---------------------------------------------------------------------------
# I. Both-side allocation is computed with per-cell amounts
# ---------------------------------------------------------------------------

def test_i_both_side_allocation():
    snap = make_snapshot(base_free="2", quote_free="1000")
    alloc = allocate(snap)

    buys = [c for c in alloc.buy_cells if c.allowed]
    sells = [c for c in alloc.sell_cells if c.allowed]
    assert len(buys) == 3
    assert len(sells) == 2

    assert [(c.buy_price, c.quantity, c.buy_notional) for c in buys] == [
        (D("590"), D("0.042"), D("24.78")),
        (D("594"), D("0.042"), D("24.948")),
        (D("598"), D("0.041"), D("24.518")),
    ]
    assert [(c.sell_price, c.quantity, c.sell_notional) for c in sells] == [
        (D("606"), D("0.041"), D("24.846")),
        (D("610"), D("0.040"), D("24.4")),
    ]
    for c in buys:
        assert c.side == "BUY" and c.allowed and c.reason_codes == ()
        assert c.buy_notional == c.buy_price * c.quantity
    for c in sells:
        assert c.side == "SELL" and c.allowed and c.reason_codes == ()
        assert c.sell_notional == c.sell_price * c.quantity


# ---------------------------------------------------------------------------
# J. Inventory target classification + boundaries
# ---------------------------------------------------------------------------

def test_j_target_classification_boundaries():
    target = InventoryTarget(D("0.5"), D("0.3"), D("0.7"))

    assert target.classify(D("0.20")) == InventoryBias.UNDERWEIGHT_BASE
    assert target.classify(D("0.30")) == InventoryBias.BALANCED       # boundary
    assert target.classify(D("0.50")) == InventoryBias.BALANCED
    assert target.classify(D("0.70")) == InventoryBias.BALANCED       # boundary
    assert target.classify(D("0.70000001")) == InventoryBias.OVERWEIGHT_BASE

    assert target.within_bounds(D("0.30")) is True
    assert target.within_bounds(D("0.70")) is True
    assert target.within_bounds(D("0.29999999")) is False
    assert target.within_bounds(D("0.70000001")) is False

    # Allocation bias is informational state derived from the same rules.
    snap = make_snapshot(base_free="2", quote_free="1000")
    alloc = allocate(snap)
    assert alloc.inventory_bias == InventoryBias.BALANCED
    assert alloc.inventory_pct == D("0.54545455")
    assert alloc.target_inventory_pct == D("0.5")


# ---------------------------------------------------------------------------
# K. Lifecycle state gating
# ---------------------------------------------------------------------------

def test_k_no_active_grid_and_blocked():
    snap = make_snapshot(base_free="2", quote_free="1000")

    no_grid = allocate(snap, lifecycle=LifecycleState.NO_ACTIVE_GRID)
    assert no_grid.status == InventoryAllocationStatus.BLOCKED
    assert no_grid.reason_codes == (AllocationReasonCode.NO_ACTIVE_GRID,)
    assert no_grid.buy_cells == () and no_grid.sell_cells == ()
    assert not no_grid.is_actionable
    assert not can_execute_allocation(no_grid)

    blocked = allocate(snap, lifecycle=LifecycleState.BLOCKED)
    assert blocked.status == InventoryAllocationStatus.BLOCKED
    assert blocked.reason_codes == (AllocationReasonCode.PLAN_BLOCKED,)
    assert blocked.buy_cells == () and blocked.sell_cells == ()
    assert not blocked.is_actionable
    assert not can_execute_allocation(blocked)


def test_k_reconfiguration_pending_informational():
    snap = make_snapshot(base_free="2", quote_free="1000")
    alloc = allocate(snap, lifecycle=LifecycleState.RECONFIGURATION_PENDING)

    # Full computation still happens (informational), but the lifecycle gate
    # forbids execution.
    assert alloc.status == InventoryAllocationStatus.VALID
    assert AllocationReasonCode.RECONFIGURATION_PENDING in alloc.reason_codes
    assert alloc.is_actionable                      # status-based only
    assert not can_execute_allocation(alloc)         # lifecycle gate wins


def test_k_ready_to_reconfigure_allowed():
    snap = make_snapshot(base_free="2", quote_free="1000")
    alloc = allocate(snap, lifecycle=LifecycleState.READY_TO_RECONFIGURE)

    assert alloc.status == InventoryAllocationStatus.VALID
    assert alloc.is_actionable
    assert can_execute_allocation(alloc)


# ---------------------------------------------------------------------------
# L. Stale generation fails closed
# ---------------------------------------------------------------------------

def test_l_stale_generation_gate():
    snap = make_snapshot(base_free="2", quote_free="1000")

    current = allocate(snap, generation=7, expected_generation=7)
    assert current.status == InventoryAllocationStatus.VALID
    assert current.generation == 7

    stale = allocate(snap, generation=7, expected_generation=8)
    assert stale.status == InventoryAllocationStatus.BLOCKED
    assert stale.reason_codes == (AllocationReasonCode.STALE_GENERATION,)
    assert stale.buy_cells == () and stale.sell_cells == ()
    assert not stale.is_actionable
    assert not can_execute_allocation(stale)


# ---------------------------------------------------------------------------
# M. Determinism: identical inputs -> identical output
# ---------------------------------------------------------------------------

def test_m_determinism_identical_inputs():
    snap = make_snapshot(base_free="2", quote_free="1000")

    a1 = allocate(snap)
    a2 = allocate(snap)
    a3 = allocate(snap)

    assert a1 == a2 == a3
    assert a1.hash == a2.hash == a3.hash
    assert a1.hash.startswith("alloc_")
    assert allocation_equal(a1, a2)

    # Any logical input change changes the hash and the allocation.
    other = allocate(make_snapshot(base_free="2", quote_free="999"))
    assert other.hash != a1.hash
    assert other.funded_buy_quote == a1.funded_buy_quote  # BUY unaffected
    assert not allocation_equal(a1, other)


# ---------------------------------------------------------------------------
# N. Risk gate is never overridden by inventory bias/allocation state
# ---------------------------------------------------------------------------

def test_n_risk_gate_blocks_everything():
    snap = make_snapshot(base_free="2", quote_free="1000")
    alloc = allocate(snap, risk_allowed=False)

    assert alloc.status == InventoryAllocationStatus.BLOCKED
    assert alloc.reason_codes == (AllocationReasonCode.RISK_BLOCKED,)
    assert alloc.buy_cells == () and alloc.sell_cells == ()
    assert not alloc.is_actionable
    assert not can_execute_allocation(alloc)

    # Even with inventory perfectly balanced, risk veto still blocks.
    assert alloc.inventory_bias == InventoryBias.BALANCED


def test_n_risk_gate_overweight_never_autoroutes():
    # Overweight inventory must NOT trigger any trade or override the gate.
    snap = make_snapshot(base_free="3", quote_free="300")   # 85.7% inventory
    alloc = allocate(snap, risk_allowed=False)
    assert alloc.status == InventoryAllocationStatus.BLOCKED
    assert alloc.inventory_bias == InventoryBias.OVERWEIGHT_BASE
    assert alloc.buy_cells == () and alloc.sell_cells == ()

    allowed = allocate(snap, risk_allowed=True)
    assert allowed.status == InventoryAllocationStatus.VALID
    assert AllocationReasonCode.INVENTORY_TARGET_EXCEEDED in allowed.reason_codes
    assert allowed.inventory_bias == InventoryBias.OVERWEIGHT_BASE


# ---------------------------------------------------------------------------
# O. allocate_grid never mutates its inputs
# ---------------------------------------------------------------------------

def test_o_no_mutation_of_inputs():
    raw_state = {
        "base_asset": "BNB",
        "quote_asset": "USDT",
        "base_free": "2",
        "base_reserved": "0",
        "quote_free": "1000",
        "quote_reserved": "0",
    }
    snapshot = InventorySnapshot.from_paper_state(raw_state, D("600"))
    before = dict(raw_state)

    levels = make_levels()
    rules = make_rules()
    cfg = base_cfg()

    alloc = allocate_grid(
        plan_id="plan_x", generation=1, levels=levels, snapshot=snapshot,
        rules=rules, cfg=cfg,
    )

    assert raw_state == before
    assert levels == make_levels()
    assert [(lv.index, lv.price) for lv in levels] == \
        [(i, D(p)) for i, p in enumerate(("590", "594", "598", "602", "606", "610"))]
    assert snapshot.base_free == D("2")
    assert snapshot.quote_free == D("1000")
    assert cfg["execution"]["order_quote_size"] == 25
    assert alloc.buy_cells[0].quantity == D("0.042")


# ---------------------------------------------------------------------------
# P. No order submission, no Binance, no accounting dependency
# ---------------------------------------------------------------------------

def test_p_no_order_or_binance_dependency():
    import inspect
    import inventory_model as im

    src = inspect.getsource(im).lower()
    # Scan for imports of and calls into execution/accounting/exchange paths.
    # (The docstring legitimately states the no-Binance invariant, so scan
    # code patterns only, not the bare words.)
    for banned in (
        "import binance",
        "from binance",
        "import order_engine",
        "from order_engine",
        "import paper_accounting",
        "from paper_accounting",
        "import main",
        "from main",
        "client.order(",
        "create_order(",
        "cancel_order(",
        "place_order(",
        "submit_order(",
        "requests.",
        "websocket",
    ):
        assert banned not in src, f"inventory_model must not reference {banned!r}"

    assert "hashlib" in src                      # deterministic hash present
    assert "no order submission" in src          # docstring states the invariant


# ---------------------------------------------------------------------------
# Q. Restart / reconstruction determinism
# ---------------------------------------------------------------------------

def test_q_restart_reconstruction():
    snap = make_snapshot(base_free="2", quote_free="1000")

    original = allocate(snap, generation=3)
    rebuilt = reconstruct_allocation(
        "plan_x", 3, make_levels(), snap, make_rules(), base_cfg(),
    )

    assert rebuilt == original
    assert rebuilt.hash == original.hash
    assert allocation_equal(original, rebuilt)
    assert rebuilt.funded_buy_quote == D("74.357369")

    rebuilt_other = reconstruct_allocation(
        "plan_x", 4, make_levels(), snap, make_rules(), base_cfg(),
    )
    assert not allocation_equal(original, rebuilt_other)


# ---------------------------------------------------------------------------
# R. Decimal-only monetary arithmetic
# ---------------------------------------------------------------------------

def test_r_decimal_only_arithmetic():
    snap = make_snapshot(base_free="2", quote_free="1000")
    alloc = allocate(snap)

    for field in (
        "base_free", "base_reserved", "quote_free", "quote_reserved",
        "current_price", "average_cost", "realized_pnl", "total_fees",
    ):
        assert isinstance(getattr(snap, field), Decimal), field
    for field in (
        "funded_buy_quote", "required_sell_base", "available_quote",
        "available_base", "inventory_pct", "target_inventory_pct",
        "shortfall_quote", "shortfall_base",
    ):
        assert isinstance(getattr(alloc, field), Decimal), field

    for cell in (*alloc.buy_cells, *alloc.sell_cells):
        assert isinstance(cell.buy_price, Decimal)
        assert isinstance(cell.sell_price, Decimal)
        assert isinstance(cell.quantity, Decimal)
        assert isinstance(cell.buy_notional, Decimal)
        assert isinstance(cell.sell_notional, Decimal)

    target = load_inventory_target(base_cfg())
    assert isinstance(target.target_inventory_pct, Decimal)
    assert isinstance(target.min_inventory_pct, Decimal)
    assert isinstance(target.max_inventory_pct, Decimal)
    assert _fee_buffer_rate(base_cfg()) == D("0.0015")


# ---------------------------------------------------------------------------
# S. Phase 3 PaperAccountState compatibility
# ---------------------------------------------------------------------------

def test_s_phase3_paper_account_state_compat():
    from paper_accounting import PaperAccountingEngine

    engine = PaperAccountingEngine(
        base_asset="BNB",
        quote_asset="USDT",
        initial_base_balance=D("2"),
        initial_quote_balance=D("1000"),
        maker_fee=D("0.001"),
        taker_fee=D("0.001"),
        fee_asset="USDT",
    )
    state = engine.initial_state()
    assert isinstance(state.base_free, Decimal)

    snapshot = InventorySnapshot.from_paper_state(state, D("600"))
    assert snapshot.base_asset == "BNB"
    assert snapshot.base_free == D("2")
    assert snapshot.quote_free == D("1000")
    assert snapshot.current_price == D("600")
    assert snapshot.equity == D("2200")

    alloc = allocate(snapshot)
    assert alloc.status == InventoryAllocationStatus.VALID
    assert alloc.funded_buy_quote == D("74.357369")


def test_s_phase3_state_dict_compat():
    raw = {
        "base_asset": "BNB",
        "quote_asset": "USDT",
        "base_free": "2",
        "base_reserved": "0",
        "quote_free": "1000",
        "quote_reserved": "0",
        "average_cost": "580",
        "realized_pnl": "12.5",
        "total_fees": "3.1",
    }
    snapshot = InventorySnapshot.from_paper_state(raw, D("600"))
    assert snapshot.average_cost == D("580")
    assert snapshot.realized_pnl == D("12.5")
    assert snapshot.total_fees == D("3.1")
    assert snapshot.base_total == D("2")
    assert snapshot.quote_total == D("1000")


# ---------------------------------------------------------------------------
# T. Phase 4 AdaptiveGridPlan compatibility
# ---------------------------------------------------------------------------

def test_t_phase4_plan_compat():
    plan = evaluate_adaptive_grid_plan(
        pair="BNBUSDT",
        regime=MarketRegime.RANGE,
        range_quality_score=D("75"),
        current_price=D("600"),
        configured_lower=D("550"),
        configured_upper=D("650"),
        available_base_inventory=D("100"),
        cfg=base_cfg(),
    )
    assert plan.decision == PlanDecision.GRID_ALLOWED
    assert len(plan.levels) >= 2

    # grid_planner.GridLevel and grid_engine.GridLevel share (index, price).
    levels = tuple(GridLevel(lv.index, lv.price) for lv in plan.levels)
    snap = make_snapshot(base_free="2", quote_free="1000")
    alloc = allocate(snap, levels=levels, plan_id=plan.plan_id)

    assert alloc.plan_id == plan.plan_id
    assert alloc.status == InventoryAllocationStatus.VALID
    assert len(alloc.buy_cells) > 0
    assert len(alloc.sell_cells) > 0
    assert alloc.funded_buy_quote > 0
    assert alloc.required_sell_base > 0


def test_t_phase4_plan_object_direct():
    levels, _ = build_geometric_grid(D("580"), D("620"), D("0.006"))
    plan = AdaptiveGridPlan(
        plan_id="plan_direct",
        pair="BNBUSDT",
        regime=MarketRegime.RANGE,
        range_quality_score=D("75"),
        candidate_lower=D("580"),
        candidate_upper=D("620"),
        grid_type="GEOMETRIC",
        grid_step=D("0.006"),
        grid_count=len(levels) - 1,
        levels=tuple(levels),
        total_quote_budget=D("0"),
        buy_quote_budget=D("25"),
        required_base_inventory=D("0.2"),
        available_base_inventory=D("2"),
        inventory_sufficient=True,
        estimated_net_profit_per_grid=D("0.004"),
        decision=PlanDecision.GRID_ALLOWED,
        reasons=(),
    )
    snap = make_snapshot(base_free="2", quote_free="1000")
    alloc = allocate(snap, levels=plan.levels, plan_id=plan.plan_id)
    assert alloc.plan_id == "plan_direct"
    assert alloc.status == InventoryAllocationStatus.VALID


# ---------------------------------------------------------------------------
# U. Conservation invariants
# ---------------------------------------------------------------------------

def test_u_conservation_invariants():
    snap = make_snapshot(base_free="2", quote_free="1000")
    alloc = allocate(snap)

    # Quote committed never exceeds quote available; base never exceeds base.
    assert alloc.funded_buy_quote <= alloc.available_quote
    assert alloc.required_sell_base <= alloc.available_base
    # Buffer is included: funded >= sum of raw buy notionals.
    raw_buy = sum(c.buy_notional for c in alloc.buy_cells)
    assert alloc.funded_buy_quote > raw_buy
    # Funded quote equals the accumulated per-cell requirement.
    assert alloc.funded_buy_quote == D("74.357369")
    # No allowed cell has zero quantity; no rejected cell has quantity.
    for cell in (*alloc.buy_cells, *alloc.sell_cells):
        assert (cell.quantity > 0) == cell.allowed
        assert (cell.reason_codes != ()) == (not cell.allowed)


def test_u_budget_conservation():
    cfg = base_cfg()
    cfg["execution"]["total_quote_budget"] = 30
    alloc = allocate(make_snapshot(base_free="2", quote_free="1000"), cfg=cfg)
    assert alloc.funded_buy_quote <= D("30")
    assert alloc.funded_buy_quote <= alloc.available_quote
    assert alloc.required_sell_base <= alloc.available_base


# ---------------------------------------------------------------------------
# V. Fail-closed on symbol-rule violations and empty grids
# ---------------------------------------------------------------------------

def test_v_symbol_rule_violation_blocks():
    rules = make_rules(tick_size="0")   # missing/invalid price rule
    snap = make_snapshot(base_free="2", quote_free="1000")
    alloc = allocate(snap, rules=rules)

    assert alloc.status == InventoryAllocationStatus.BLOCKED
    assert alloc.reason_codes == (AllocationReasonCode.SYMBOL_RULE_VIOLATION,)
    assert alloc.buy_cells == () and alloc.sell_cells == ()
    assert not alloc.is_actionable


def test_v_less_than_two_levels():
    snap = make_snapshot(base_free="2", quote_free="1000")
    single = allocate(snap, levels=(GridLevel(0, D("590")),))
    assert single.status == InventoryAllocationStatus.BLOCKED
    assert single.reason_codes == (AllocationReasonCode.ZERO_BUY_CELLS,)

    empty = allocate(snap, levels=())
    assert empty.status == InventoryAllocationStatus.BLOCKED
    assert empty.reason_codes == (AllocationReasonCode.ZERO_BUY_CELLS,)


# ---------------------------------------------------------------------------
# W. Inventory target exceeded is informational, never blocking
# ---------------------------------------------------------------------------

def test_w_inventory_target_informational_only():
    # 3 BNB at 600 -> 1800; 300 quote -> 2100 equity -> 85.7% > max 70%.
    snap = make_snapshot(base_free="3", quote_free="300")
    alloc = allocate(snap)

    assert alloc.inventory_bias == InventoryBias.OVERWEIGHT_BASE
    assert AllocationReasonCode.INVENTORY_TARGET_EXCEEDED in alloc.reason_codes
    # Allocation still computed and even actionable -- bias never blocks.
    assert alloc.status == InventoryAllocationStatus.VALID
    assert alloc.is_actionable
    assert can_execute_allocation(alloc)
    assert len(alloc.buy_cells) == 3 and len(alloc.sell_cells) == 2

    under = make_snapshot(base_free="0", quote_free="1000")
    under_alloc = allocate(under)
    assert under_alloc.inventory_bias == InventoryBias.UNDERWEIGHT_BASE
    assert AllocationReasonCode.INVENTORY_TARGET_EXCEEDED in under_alloc.reason_codes


# ---------------------------------------------------------------------------
# X. Actionable-cell filters
# ---------------------------------------------------------------------------

def test_x_filter_actionable_cells():
    snap = make_snapshot(base_free="2", quote_free="1000")
    alloc = allocate(snap)

    buys, sells = filter_actionable_cells(alloc)
    assert len(buys) == 3 and len(sells) == 2
    assert all(c.allowed for c in (*buys, *sells))

    unfunded = allocate(make_snapshot(base_free="0", quote_free="0"))
    buys2, sells2 = filter_actionable_cells(unfunded)
    assert buys2 == [] and sells2 == []


# ---------------------------------------------------------------------------
# Y. InventoryTarget invariant validation
# ---------------------------------------------------------------------------

def test_y_inventory_target_validation_errors():
    with pytest.raises(ValueError):
        InventoryTarget(D("0.5"), D("0.6"), D("0.7"))     # min > target
    with pytest.raises(ValueError):
        InventoryTarget(D("0.5"), D("0.3"), D("1.1"))     # max > 1
    with pytest.raises(ValueError):
        InventoryTarget(D("0.5"), D("-0.1"), D("0.7"))    # negative min
    with pytest.raises(ValueError):
        InventoryTarget(D("0.8"), D("0.3"), D("0.7"))     # target > max

    ok = InventoryTarget(D("0.5"), D("0.3"), D("0.7"))
    assert ok.min_inventory_pct == D("0.3")


def test_y_load_inventory_target_defaults():
    target = load_inventory_target({})
    assert target.target_inventory_pct == D("0.50")
    assert target.min_inventory_pct == D("0.30")
    assert target.max_inventory_pct == D("0.70")

    cfg = {"inventory": {"target_inventory_pct": "0.4", "min_inventory_pct": "0.2",
                         "max_inventory_pct": "0.6"}}
    target2 = load_inventory_target(cfg)
    assert target2.target_inventory_pct == D("0.4")
    assert target2.min_inventory_pct == D("0.2")
    assert target2.max_inventory_pct == D("0.6")


# ---------------------------------------------------------------------------
# Z. Full immutability of outputs
# ---------------------------------------------------------------------------

def test_z_outputs_are_frozen():
    snap = make_snapshot(base_free="2", quote_free="1000")
    alloc = allocate(snap)

    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.base_free = D("99")          # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        alloc.status = InventoryAllocationStatus.UNFUNDED   # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        alloc.buy_cells[0].quantity = D("99")   # type: ignore[misc]

    assert snap.base_free == D("2")
    assert alloc.status == InventoryAllocationStatus.VALID
    assert alloc.buy_cells[0].quantity == D("0.042")


# ---------------------------------------------------------------------------
# Lifecycle integration values match Phase 5B
# ---------------------------------------------------------------------------

def test_lifecycle_state_enum_matches_phase5b():
    import grid_lifecycle

    for name in (
        "NO_ACTIVE_GRID", "ACTIVE", "RECONFIGURATION_PENDING",
        "READY_TO_RECONFIGURE", "BLOCKED",
    ):
        ours = getattr(LifecycleState, name)
        theirs = getattr(grid_lifecycle.LifecycleState, name)
        assert ours.value == theirs.value


def test_zero_reserved_never_blocks_available():
    # Reserved balances are excluded from available funds, exactly like
    # open-order reservations in paper accounting.
    snap = make_snapshot(base_free="2", base_reserved="0.1", quote_free="1000")
    assert snap.available_base == D("2")
    assert snap.base_total == D("2.1")

    alloc = allocate(snap)
    assert alloc.available_base == D("2")
    assert alloc.required_sell_base == D("0.081")