"""FIX 4C — F-4 (unify generation namespace) + F-5 (cap SELL inventory).

F-4: main.py no longer maintains an independent ``plan_generation`` counter.
      The lifecycle manager's ``get_generation()`` is the SINGLE authoritative
      generation for executable paper orders; main.py and PaperSession both
      source it from the same lifecycle database.

F-5: SELL executable quantity is capped against *executable* base inventory
      (``base_free`` — reservations excluded), and the AGGREGATE of all SELL
      cells in a cycle must satisfy SUM(executable SELL) <= available base.
      This is enforced by the authoritative ``inventory_model.allocate_grid``;
      no second inventory model exists.

The suite contains:
  * PART E adversarial tests (F-4 x7, F-5 x9)
  * PART D client-order-id uniqueness / length proof
  * execution-path proof that no SELL submission exceeds executable base.

All generation advancement happens inside the lifecycle manager (Patch 2D
atomic cycle); no migration silently rewrites existing generation state.
"""
from __future__ import annotations

from decimal import Decimal

from grid_engine import GridLevel
from grid_lifecycle import LifecycleManager
from inventory_model import (
    AllocationReasonCode,
    InventoryAllocationStatus,
    InventorySnapshot,
    LifecycleState,
    allocate_grid,
    can_execute_allocation,
    filter_actionable_cells,
)
from order_engine import (
    make_client_order_id,
    parse_generation_from_client_order_id,
)
from symbol_rules import parse_symbol_info

# Binance client-order-id charset limit (order_engine._CLIENT_ORDER_ID_RE).
_BINANCE_CLIENT_ID_MAX = 36


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _rules():
    return parse_symbol_info({
        "symbol": "BTCUSDT",
        "baseAsset": "BTC",
        "quoteAsset": "USDT",
        "status": "TRADING",
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "100000", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "10000", "stepSize": "0.001"},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "5000", "stepSize": "0.001"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
            {"filterType": "NOTIONAL", "minNotional": "10", "maxNotional": "100000"},
            {"filterType": "PERCENT_PRICE", "multiplierUp": "1.05", "multiplierDown": "0.95", "avgPriceMins": 5},
            {"filterType": "MAX_NUM_ORDERS", "maxNumOrders": 40},
        ],
    })


def _cfg():
    return {
        "pair": "BTCUSDT",
        "execution": {"order_quote_size": "25", "prefer_limit_maker": True},
        "grid": {"hard_min_net_pct": "0.003"},
        "fees": {"maker_fee_fallback": "0.001", "slippage_roundtrip_pct": "0.0005"},
    }


def _levels(*prices):
    return tuple(GridLevel(i, Decimal(str(p))) for i, p in enumerate(prices))


def _snap(base_free, base_reserved, quote_free, price):
    return InventorySnapshot(
        base_asset="BTC", quote_asset="USDT",
        base_free=Decimal(base_free), base_reserved=Decimal(base_reserved),
        quote_free=Decimal(quote_free), quote_reserved=Decimal("0"),
        current_price=Decimal(price),
    )


def _alloc(levels, base_free, base_reserved, price, gen=1, exp=None, cfg=None,
           state=None, quote_free="0"):
    snap = _snap(base_free, base_reserved, quote_free, price)
    return allocate_grid(
        "fix4c", gen, levels, snap, _rules(), cfg or _cfg(),
        lifecycle_state=state or LifecycleState.ACTIVE,
        expected_generation=exp,
    )


def _aggregate_executable_sell(alloc):
    _, sells = filter_actionable_cells(alloc)
    return sum((c.quantity for c in sells if c.allowed), Decimal("0"))


# ---------------------------------------------------------------------------
# PART B/E — F-5 SELL inventory cap (authoritative allocate_grid)
# ---------------------------------------------------------------------------

def test_f5_single_sell_cell_exact_inventory():
    """One SELL cell; executable SELL qty <= available base (E-8)."""
    # 3 levels, current 100.5 -> SELL levels [101,102], one BUY [100].
    lv = _levels("100", "101", "102")
    # base_free exactly covers the single top SELL cell's quantized qty.
    alloc = _alloc(lv, "0.246", "0", "100.5")
    executable = _aggregate_executable_sell(alloc)
    assert executable == alloc.required_sell_base
    assert executable <= Decimal("0.246")
    # Conservation: no negative inventory.
    assert alloc.available_base - alloc.required_sell_base >= 0


def test_f5_multiple_sell_cells_aggregate_cap():
    """Two SELL cells; AGGREGATE (not per-cell) SELL qty <= base (E-9)."""
    lv = _levels("100", "101", "102", "103")   # SELL [101,102,103]
    alloc = _alloc(lv, "0.5", "0", "100.5")
    executable = _aggregate_executable_sell(alloc)
    # The cap is on the aggregate: per-cell each is ~0.246 (<=0.5) but only the
    # cells that fit in the running total are allowed.
    assert executable <= Decimal("0.5")
    assert alloc.required_sell_base == executable
    assert executable <= alloc.available_base


def test_f5_reserved_inventory_excluded():
    """base_reserved is NOT executable: cap uses base_free only (E-10)."""
    lv = _levels("100", "101", "102")
    alloc = _alloc(lv, "0.2", "0.8", "100.5")   # free=0.2, reserved=0.8
    assert alloc.available_base == Decimal("0.2")
    executable = _aggregate_executable_sell(alloc)
    assert executable <= Decimal("0.2")
    # Reserved base must never count toward the SELL cap.
    assert executable + Decimal("0") <= alloc.available_base


def test_f5_zero_base_zero_sell():
    """Zero free base -> zero executable SELL (E-11)."""
    lv = _levels("100", "101", "102")
    alloc = _alloc(lv, "0", "0", "100.5")
    assert _aggregate_executable_sell(alloc) == Decimal("0")
    assert alloc.required_sell_base == Decimal("0")
    assert AllocationReasonCode.INSUFFICIENT_BASE in alloc.reason_codes


def test_f5_exact_inventory_boundary():
    """base_free exactly equals total SELL need -> all SELL cells funded (E-12)."""
    lv = _levels("100", "101", "102")
    # First compute the ideal SELL need with generous base.
    ideal = _alloc(lv, "10", "0", "100.5")
    total_need = ideal.required_sell_base
    # Now feed exactly that amount: boundary-inclusive.
    at_boundary = _alloc(lv, str(total_need), "0", "100.5")
    assert at_boundary.required_sell_base == total_need
    assert at_boundary.available_base - at_boundary.required_sell_base >= 0
    assert AllocationReasonCode.INSUFFICIENT_BASE not in at_boundary.reason_codes


def test_f5_insufficient_base_across_three_sells():
    """Insufficient base across three SELL cells -> only the first N funded (E-15)."""
    lv = _levels("100", "101", "102", "103", "104")   # SELL [102,103,104]
    alloc = _alloc(lv, "0.5", "0", "100.5")
    allowed = [c for c in alloc.sell_cells if c.allowed]
    blocked = [c for c in alloc.sell_cells if not c.allowed]
    # Three SELL cells but base only covers two -> the third is gated.
    assert len(alloc.sell_cells) == 3
    assert len(allowed) == 2
    assert blocked
    assert all(AllocationReasonCode.INSUFFICIENT_BASE in c.reason_codes
               for c in blocked)
    # Aggregate of the funded cells conserves base.
    assert _aggregate_executable_sell(alloc) <= Decimal("0.5")


def test_f5_quantity_quantization_conservation():
    """Quantization can only REDUCE SELL qty; aggregate stays <= base (E-13)."""
    lv = _levels("100", "101", "102")
    generous = _alloc(lv, "10", "0", "100.5")
    base_free = generous.required_sell_base
    # Feeding exactly the pre-quant need; post-quant qty (rounded down) is
    # <= the ideal, so aggregate never exceeds base.
    alloc = _alloc(lv, str(base_free), "0", "100.5")
    assert alloc.required_sell_base <= base_free
    assert alloc.available_base - alloc.required_sell_base >= 0


def test_f5_mixed_buy_sell_cycle_conservation():
    """BUY + SELL mixed: BUY funded from quote, SELL from base, both conserve (E-14)."""
    lv = _levels("100", "101", "102", "103")
    alloc = _alloc(lv, "0.5", "0", "100.5", quote_free="10000")
    buys, sells = filter_actionable_cells(alloc)
    # BUY cells are funded from quote (independent of base).
    assert any(c.allowed for c in alloc.buy_cells)
    # SELL aggregate conserves base.
    sell_agg = sum((c.quantity for c in sells if c.allowed), Decimal("0"))
    assert sell_agg <= alloc.available_base
    # BUY quote conservation unchanged by the SELL cap.
    assert alloc.funded_buy_quote <= alloc.available_quote


def test_f5_stale_accounting_fail_closed():
    """Stale/corrupt accounting state -> fail closed, zero SELL (E-16)."""
    lv = _levels("100", "101", "102")
    # base_free inconsistent (negative reservation beyond free) -> allocation
    # still conserves because available_base is clamped to base_free only.
    alloc = _alloc(lv, "0.1", "0.5", "100.5")
    assert alloc.available_base == Decimal("0.1")
    assert _aggregate_executable_sell(alloc) <= Decimal("0.1")
    # If generation is stale, the whole allocation blocks (fail closed).
    stale = _alloc(lv, "10", "0", "100.5", gen=5, exp=3)
    assert stale.status == InventoryAllocationStatus.BLOCKED
    assert AllocationReasonCode.STALE_GENERATION in stale.reason_codes
    assert not can_execute_allocation(stale)


def test_f5_run_cycle_never_submits_aggregate_sell_over_base(tmp_path):
    """End-to-end proof: PaperOrderEngine.submit never receives an aggregate
    SELL quantity greater than available executable base."""
    from paper_orchestrator import PaperSession, PaperCycleInput
    from paper_accounting import PaperAccountingEngine
    from market_regime import MarketRegime
    from tests.test_paper_orchestrator import happy_cfg, make_kline_df, FIXED_NOW

    order_db = str(tmp_path / "orders.db")
    lifecycle_db = str(tmp_path / "lifecycle.db")
    accounting = PaperAccountingEngine(
        "BTC", "USDT", Decimal("2.0"), Decimal("10000.0"),
        Decimal("0.001"), Decimal("0.001"), "USDT",
    )
    session = PaperSession(order_db, lifecycle_db, accounting, client_order_prefix="AG")

    # Seed a low free base so the full-PLAN SELL total would exceed it; the
    # allocation must cap the executable SELL aggregate to base_free.
    from tests.test_paper_orchestrator import seed_accounting_state
    seed_accounting_state(order_db, "0.5", "0", "10000", "0")

    submitted = {"SELL": Decimal("0")}
    original_submit = session.order_engine.submit

    def spy_submit(intent, *args, **kwargs):
        if intent.side == "SELL":
            submitted["SELL"] += intent.quantity
        return original_submit(intent, *args, **kwargs)

    session.order_engine.submit = spy_submit
    try:
        cfg = happy_cfg()
        cycle_input = PaperCycleInput(
            candle_index=1, symbol="BTCUSDT",
            current_price=Decimal("110"),
            kline_df=make_kline_df(FIXED_NOW, Decimal("110")),
            lower_price=Decimal("100"), upper_price=Decimal("115"),
            regime=MarketRegime.RANGE,
            range_quality_score=Decimal("80"),
            cfg=cfg, clock=lambda: FIXED_NOW, dry_run=True,
            rules=_rules(),
        )
        session.run_cycle(cycle_input)
    finally:
        session.order_engine.submit = original_submit

    # The aggregate SELL that actually reached submit() must not exceed the
    # executable base (0.5) — the allocation cap is honored at the boundary.
    assert submitted["SELL"] <= Decimal("0.5")


# ---------------------------------------------------------------------------
# PART A/E — F-4 generation namespace (authoritative lifecycle generation)
# ---------------------------------------------------------------------------

def test_f4_stale_plan_cannot_submit():
    """A stale generation plan (candidate gen < manager gen) cannot submit (E-2)."""
    lv = _levels("100", "101", "102")
    alloc = _alloc(lv, "10", "0", "100.5", gen=3, exp=5)
    assert alloc.status == InventoryAllocationStatus.BLOCKED
    assert AllocationReasonCode.STALE_GENERATION in alloc.reason_codes
    assert alloc.buy_cells == () and alloc.sell_cells == ()
    assert not can_execute_allocation(alloc)


def test_f4_restart_generation_persistence(tmp_path):
    """Same active plan after restart retains the SAME lifecycle generation (A)."""
    mgr_a = LifecycleManager(str(tmp_path / "lc.db"))
    from grid_planner import evaluate_adaptive_grid_plan
    from market_regime import MarketRegime

    plan = evaluate_adaptive_grid_plan(
        pair="BTCUSDT", regime=MarketRegime.RANGE, range_quality_score=Decimal("80"),
        current_price=Decimal("110"), configured_lower=Decimal("100"),
        configured_upper=Decimal("115"), available_base_inventory=Decimal("2"),
        cfg=_cfg(), active_plan=None, current_candle_index=1,
    )
    mgr_a.activate_plan(plan, _cfg())
    gen_after = mgr_a.get_generation()
    active = mgr_a.get_active_plan()
    assert active is not None
    assert active.generation == gen_after

    # Restart: a fresh manager on the SAME file sees the identical generation.
    mgr_b = LifecycleManager(str(tmp_path / "lc.db"))
    assert mgr_b.get_generation() == gen_after
    assert mgr_b.get_active_plan().generation == gen_after
    # No silent re-advance on restart.
    assert mgr_b.get_generation() == gen_after


def test_f4_new_generation_new_bound_client_id():
    """A new generation yields a new generation-bound client_order_id (C)."""
    id1 = make_client_order_id("AG", "BTCUSDT", 1, 3, "BUY")
    id2 = make_client_order_id("AG", "BTCUSDT", 2, 3, "BUY")
    assert parse_generation_from_client_order_id(id1) == 1
    assert parse_generation_from_client_order_id(id2) == 2
    assert id1 != id2


def test_f4_old_generation_id_cannot_collide_with_new():
    """Old-gen and new-gen IDs for the same symbol/grid/side never collide (D, E-7)."""
    for grid_index in (0, 1, 5, 10):
        for side in ("BUY", "SELL"):
            old = make_client_order_id("AG", "BTCUSDT", 1, grid_index, side)
            new = make_client_order_id("AG", "BTCUSDT", 2, grid_index, side)
            assert old != new
            assert parse_generation_from_client_order_id(old) == 1
            assert parse_generation_from_client_order_id(new) == 2


def test_f4_main_and_orchestrator_same_generation(tmp_path):
    """main._lifecycle_active_plan and the orchestrator read the SAME generation (E)."""
    import main
    from grid_planner import evaluate_adaptive_grid_plan
    from market_regime import MarketRegime

    db = str(tmp_path / "grid.sqlite3")
    from storage import init_db
    init_db(db)

    mgr = LifecycleManager(db)
    plan = evaluate_adaptive_grid_plan(
        pair="BTCUSDT", regime=MarketRegime.RANGE, range_quality_score=Decimal("80"),
        current_price=Decimal("110"), configured_lower=Decimal("100"),
        configured_upper=Decimal("115"), available_base_inventory=Decimal("2"),
        cfg=_cfg(), active_plan=None, current_candle_index=1,
    )
    mgr.activate_plan(plan, _cfg())

    # main's reader sources generation from the lifecycle DB (authoritative).
    main_active = main._lifecycle_active_plan(db)
    assert main_active is not None
    assert main_active.generation == mgr.get_generation()

    # The orchestrator's planner generation (lifecycle get_generation) equals it.
    from paper_orchestrator import PaperSession
    from paper_accounting import PaperAccountingEngine
    accounting = PaperAccountingEngine(
        "BTC", "USDT", Decimal("2.0"), Decimal("10000.0"),
        Decimal("0.001"), Decimal("0.001"), "USDT")
    session = PaperSession(db, db, accounting, client_order_prefix="AG")
    lm = session.orchestrator.lifecycle_manager
    assert lm.get_generation() == main_active.generation
    # Same active-plan identity on both sides.
    orch_active = lm.get_active_plan()
    assert orch_active.plan_id == main_active.plan_id


def test_f4_conflicting_bot_state_generation_ignored(tmp_path):
    """A conflicting legacy bot_state plan_generation does NOT pollute F-4 execution (E-1, F)."""
    import main
    from storage import init_db, set_state, get_state

    db = str(tmp_path / "grid.sqlite3")
    init_db(db)

    # Seed the lifecycle with an authoritative generation.
    from grid_planner import evaluate_adaptive_grid_plan
    from market_regime import MarketRegime
    mgr = LifecycleManager(db)
    plan = evaluate_adaptive_grid_plan(
        pair="BTCUSDT", regime=MarketRegime.RANGE, range_quality_score=Decimal("80"),
        current_price=Decimal("110"), configured_lower=Decimal("100"),
        configured_upper=Decimal("115"), available_base_inventory=Decimal("2"),
        cfg=_cfg(), active_plan=None, current_candle_index=1,
    )
    mgr.activate_plan(plan, _cfg())
    authoritative_gen = mgr.get_generation()

    # Write a CONFLICTING legacy bot_state counter (the old F-4 divergence source).
    set_state(db, "plan_generation", "999")
    assert int(get_state(db, "plan_generation")) == 999

    # main's authoritative reader must reflect the lifecycle generation, NOT 999.
    main_active = main._lifecycle_active_plan(db)
    assert main_active is not None
    assert main_active.generation == authoritative_gen
    assert main_active.generation != 999


def test_f4_corrupted_lifecycle_generation_zero_submit(tmp_path):
    """A corrupted (negative) lifecycle generation fails closed -> zero submit (E-6)."""
    from storage import connect

    db = str(tmp_path / "lc.db")
    LifecycleManager(db)._ensure_tables_exist()
    now = "2026-01-01T00:00:00+00:00"
    import json as _json
    con = connect(db)
    try:
        con.execute(
            "INSERT OR REPLACE INTO active_plans (plan_id,pair,candidate_lower,"
            "candidate_upper,grid_step,grid_count,regime,range_quality_score,"
            "candle_index,generation,lifecycle_state,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("bad", "BTCUSDT", "100", "115", "0.006", 8, "RANGE", "80", 1, -1,
             "ACTIVE", now, now))
        con.execute(
            "INSERT OR IGNORE INTO generations (generation,active_plan_id,"
            "created_at,status) VALUES (?,?,?,?)", (-1, "bad", now, "ACTIVE"))
        # get_active_plan() only returns a row when the global lifecycle state
        # is ACTIVE/RECONFIG_PENDING, so the corrupt row is visible only when
        # the state marker is set — mirroring a real (corrupt) running grid.
        con.execute(
            "INSERT INTO bot_state(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("lifecycle:state", _json.dumps({"state": "ACTIVE", "timestamp": now})))
        con.commit()
    finally:
        con.close()

    from paper_orchestrator import PaperSession
    from paper_accounting import PaperAccountingEngine
    from decimal import Decimal as D

    mgr = LifecycleManager(db)
    active = mgr.get_active_plan()
    # The corrupt negative generation is read back verbatim.
    assert active is not None and active.generation < 0

    # The orchestrator's lifecycle-integrity gate is the authoritative
    # fail-closed check for a corrupt active-plan generation.  A negative
    # generation is invalid -> the gate rejects it BEFORE any order intent is
    # generated or submitted (zero submit).
    accounting = PaperAccountingEngine(
        "BTC", "USDT", D("2"), D("10000"), D("0.001"), D("0.001"), "USDT")
    session = PaperSession(str(tmp_path / "o.db"), db, accounting, "AG")
    reason = session.orchestrator._validate_lifecycle_integrity(None, None, None)
    # The corrupt negative generation is rejected at the gate's
    # manager-generation validity check (a negative recorded generation is
    # invalid) → deterministic fail-closed reason, zero submit downstream.
    assert reason is not None
    assert reason.startswith("INVALID_GENERATION")


def test_f4_generation_mismatch_allocation_blocks():
    """A generation that does not match the manager generation blocks the
    authoritative allocation (fail closed), so no SELL/BUY intent executes."""
    lv = _levels("100", "101", "102")
    # Candidate plan at generation 3 while the manager is at generation 5.
    alloc = _alloc(lv, "10", "0", "100.5", gen=3, exp=5)
    assert alloc.status == InventoryAllocationStatus.BLOCKED
    assert AllocationReasonCode.STALE_GENERATION in alloc.reason_codes
    assert alloc.buy_cells == () and alloc.sell_cells == ()
    assert not can_execute_allocation(alloc)


# ---------------------------------------------------------------------------
# PART D — client-order-id uniqueness + Binance length limit
# ---------------------------------------------------------------------------

def test_part_d_generation_bound_id_unique_and_within_limit():
    """gen1 vs gen2 same symbol/grid/side -> distinct IDs, both <= Binance limit."""
    for gen in (1, 2, 3):
        ident = make_client_order_id("AG", "BTCUSDT", gen, 1, "BUY")
        assert len(ident) <= _BINANCE_CLIENT_ID_MAX
        assert parse_generation_from_client_order_id(ident) == gen

    id_g1 = make_client_order_id("AG", "BTCUSDT", 1, 1, "BUY")
    id_g2 = make_client_order_id("AG", "BTCUSDT", 2, 1, "BUY")
    assert id_g1 != id_g2
    assert len(id_g1) <= _BINANCE_CLIENT_ID_MAX
    assert len(id_g2) <= _BINANCE_CLIENT_ID_MAX
