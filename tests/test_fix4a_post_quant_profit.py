"""FIX 4A — post-quantization net profit hard gate (audit finding F-3).

The planner's profit gate runs on the PRE-quantization grid step.  The
orchestrator must NOT emit an executable intent for any grid cell whose
ACTUAL (tick-quantized) round-trip net profit is below the configured hard
minimum (``grid.hard_min_net_pct``, default 0.003).

These tests drive ``PaperOrchestrator._generate_order_intents`` directly
(precise raw levels + coarse tick size) and the full ``run_cycle`` path,
reusing the same ``quantize_price`` + ``net_pct_from_prices`` helpers that
``main.py``'s ``validate_quantized_order_plan`` uses — no duplicated formula.

Required invariant:

    pre_quant_profit >= hard_min  AND  post_quant_profit >= hard_min
        => executable intent
    post_quant_profit < hard_min
        => NO executable intent (deterministic block reason)
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd

from grid_engine import GridLevel
from grid_lifecycle import LifecycleState as GridLifecycleState
from grid_planner import AdaptiveGridPlan, PlanDecision
from market_regime import MarketRegime
from paper_accounting import PaperAccountingEngine
from paper_orchestrator import PaperCycleInput, PaperSession
from profit_model import net_pct_from_prices, net_pct_from_step
from symbol_rules import SymbolRules
from symbol_rules import quantize_price  # noqa: F401  (reuse proof, shared w/ main.py)

D = Decimal
HARD_MIN = D("0.003")
_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _rules(tick=D("0.01")):
    return SymbolRules(
        symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", status="TRADING",
        tick_size=tick, min_price=D("0.01"), max_price=D("1000000"),
        step_size=D("0.000001"), min_qty=D("0.001"), max_qty=D("1000"),
        market_step_size=D("0.000001"), market_min_qty=D("0.001"),
        market_max_qty=D("1000"), min_notional=D("5"), max_notional=D("0"),
        percent_multiplier_up=D("0"), percent_multiplier_down=D("0"),
        percent_avg_mins=0,
        bid_multiplier_up=D("0"), bid_multiplier_down=D("0"),
        ask_multiplier_up=D("0"), ask_multiplier_down=D("0"), side_avg_mins=0,
        max_num_orders=199, max_num_algo_orders=0,
    )


def _cfg():
    return {
        "pair": "BTCUSDT",
        "grid": {"hard_min_net_pct": "0.003"},
        "execution": {"order_quote_size": "25", "prefer_limit_maker": True},
        "fees": {
            "maker_fee_fallback": "0.001",
            "taker_fee_fallback": "0.001",
            "slippage_roundtrip_pct": "0.0005",
        },
        "lifecycle": {"reconfiguration": {
            "step_change_threshold_pct": 0.10,
            "grid_count_threshold": 3,
            "cooldown_candles": 20,
        }},
    }


def _make_session(tmp_path):
    accounting = PaperAccountingEngine(
        base_asset="BTC", quote_asset="USDT",
        initial_base_balance=D("100"), initial_quote_balance=D("100000"),
        maker_fee=D("0.001"), taker_fee=D("0.001"), fee_asset="USDT",
    )
    return PaperSession(
        str(tmp_path / "orders.db"), str(tmp_path / "lifecycle.db"),
        accounting, client_order_prefix="AG",
    )


def _plan_from_levels(levels, current_price, plan_id="fix4a"):
    return AdaptiveGridPlan(
        plan_id=plan_id,
        pair="BTCUSDT",
        regime=MarketRegime.RANGE,
        range_quality_score=D("80"),
        candidate_lower=levels[0].price,
        candidate_upper=levels[-1].price,
        grid_type="GEOMETRIC",
        grid_step=D("0.006"),
        grid_count=max(0, len(levels) - 1),
        levels=tuple(levels),
        total_quote_budget=D("0"),
        buy_quote_budget=D("0"),
        required_base_inventory=D("0"),
        available_base_inventory=D("100"),
        inventory_sufficient=True,
        estimated_net_profit_per_grid=HARD_MIN + D("0.0005"),
        decision=PlanDecision.GRID_ALLOWED,
        reasons=(),
    )


def _cycle_input(plan, tick, current_price, taker_fee=None, prefer_maker=True):
    kline_df = pd.DataFrame([{
        "open_time": _NOW - pd.Timedelta(minutes=15),
        "close_time": _NOW,
        "open": float(current_price), "high": float(current_price),
        "low": float(current_price), "close": float(current_price),
        "volume": 100.0,
    }])
    cfg = _cfg()
    if not prefer_maker:
        cfg["execution"]["prefer_limit_maker"] = False
    return PaperCycleInput(
        candle_index=1, symbol="BTCUSDT", current_price=current_price,
        kline_df=kline_df, lower_price=plan.candidate_lower,
        upper_price=plan.candidate_upper, regime=MarketRegime.RANGE,
        range_quality_score=D("80"), cfg=cfg, clock=lambda: _NOW,
        dry_run=True, rules=_rules(tick), metadata={},
        taker_fee=taker_fee or D("0.001"),
    )


def _accounting_state():
    """Funded paper account so allocation legs are executable (not a block)."""
    return {
        "base_asset": "BTC", "quote_asset": "USDT",
        "base_free": "100", "base_reserved": "0",
        "quote_free": "100000", "quote_reserved": "0",
        "average_cost": "0", "realized_pnl": "0", "total_fees": "0",
    }


def _intents_for(tmp_path, levels, tick, current_price,
                 taker_fee=None, prefer_maker=True):
    """Run _generate_order_intents directly with a funded account + ACTIVE
    lifecycle (monkeypatched) so the post-quant profit gate is the deciding
    factor, not a funding/lifecycle block."""
    session = _make_session(tmp_path)
    orch = session.orchestrator
    plan = _plan_from_levels(levels, current_price)
    ci = _cycle_input(plan, tick, current_price,
                      taker_fee=taker_fee, prefer_maker=prefer_maker)

    # Lifecycle must read ACTIVE + a matching generation so allocation passes
    # and the post-quant profit gate is what actually decides.
    orch.lifecycle_manager.get_current_state = (
        lambda con=None, prefix="": GridLifecycleState.ACTIVE
    )
    orch.lifecycle_manager.get_generation = lambda con=None, prefix="": 1

    result = orch._generate_order_intents(
        ci, plan, ci.clock, _accounting_state(),
        con=None, lc_prefix="",
    )
    return result, session


def _idx(side, intents):
    return sorted(i.grid_index for i in intents if i.side == side)


def _gate_net(raw_buy, raw_sell, tick):
    import paper_orchestrator as po
    return po._post_quant_net_profit(
        raw_buy, raw_sell, _rules(tick),
        D("0.001"), D("0.001"), D("0.0005"),
    )


def _block_reasons(result, side, index):
    return f"POST_QUANTIZATION_PROFIT_BELOW_MIN:{side}:{index}"


# ---------------------------------------------------------------------------
# 1. Quantization preserves net >= hard_min → intent generated
# ---------------------------------------------------------------------------

def test_4a_1_quantization_preserves_net_intent_generated(tmp_path):
    levels = [GridLevel(0, D("100")), GridLevel(1, D("100.60"))]
    assert _gate_net(D("100"), D("100.60"), D("0.01")) >= HARD_MIN
    result, _ = _intents_for(tmp_path, levels, D("0.01"), D("100.2"))
    assert result.allocation_blocked is None
    assert _idx("BUY", result.intents) == [0]
    assert result.profit_blocked_cells == ()


# ---------------------------------------------------------------------------
# 2. Quantization reduces net below hard_min → zero intent for that cell
# ---------------------------------------------------------------------------

def test_4a_2_quantization_erodes_net_cell_blocked(tmp_path):
    # BUY i=0 pairs (100.60→101.00): quantized net 0.001468 < 0.003 → blocked.
    # BUY i=1 pairs (101.00→101.61): net 0.003527 >= 0.003 → passes.
    levels = [
        GridLevel(0, D("100.60")),
        GridLevel(1, D("101.00")),
        GridLevel(2, D("101.61")),
    ]
    assert _gate_net(D("100.60"), D("101.00"), D("0.01")) < HARD_MIN
    result, _ = _intents_for(tmp_path, levels, D("0.01"), D("101.5"))
    assert _block_reasons(result, "BUY", 0) in result.profit_blocked_cells
    assert _idx("BUY", result.intents) == [1], "only the valid BUY cell survives"
    assert result.allocation_blocked is None, "partial drop is not a whole block"


# ---------------------------------------------------------------------------
# 3. Exact boundary: post-quant net >= hard_min → accepted
# ---------------------------------------------------------------------------

def test_4a_3_boundary_accepted(tmp_path):
    # (100 → 100.65) quantized net 0.003986 >= 0.003 → accepted.
    levels = [GridLevel(0, D("100")), GridLevel(1, D("100.65"))]
    assert _gate_net(D("100"), D("100.65"), D("0.01")) >= HARD_MIN
    result, _ = _intents_for(tmp_path, levels, D("0.01"), D("100.2"))
    assert _idx("BUY", result.intents) == [0]
    assert result.profit_blocked_cells == ()


def test_4a_3b_boundary_equivalence():
    """The gate uses exactly net_pct_from_prices on quantized prices."""
    import paper_orchestrator as po
    q_buy = quantize_price(D("100.60"), _rules(D("0.01")))
    q_sell = quantize_price(D("101.00"), _rules(D("0.01")))
    assert po._post_quant_net_profit(
        D("100.60"), D("101.00"), _rules(D("0.01")),
        D("0.001"), D("0.001"), D("0.0005"),
    ) == net_pct_from_prices(
        q_buy, q_sell, D("0.001"), D("0.001"), D("0.0005"),
    )


# ---------------------------------------------------------------------------
# 4. Slightly below: post-quant net < hard_min → rejected
# ---------------------------------------------------------------------------

def test_4a_4_slightly_below_rejected(tmp_path):
    # (101 → 101.5): quantized net 0.00244 < 0.003 → rejected, whole block.
    levels = [
        GridLevel(0, D("101")),
        GridLevel(1, D("101.5")),
        GridLevel(2, D("102.0")),
    ]
    assert _gate_net(D("101"), D("101.5"), D("0.01")) < HARD_MIN
    assert _gate_net(D("101.5"), D("102.0"), D("0.01")) < HARD_MIN
    result, _ = _intents_for(tmp_path, levels, D("0.01"), D("101.7"))
    assert result.allocation_blocked is not None
    assert result.allocation_blocked["status"] == "PROFIT_GATE_BLOCKED"
    assert _block_reasons(result, "BUY", 0) in result.allocation_blocked["reasons"]
    assert result.intents == []
    assert _block_reasons(result, "BUY", 0) in result.profit_blocked_cells


# ---------------------------------------------------------------------------
# 5. Multiple cells: valid + invalid → only valid becomes executable
# ---------------------------------------------------------------------------

def test_4a_5_mixed_cells_only_valid_executable(tmp_path):
    levels = [
        GridLevel(0, D("100.60")),
        GridLevel(1, D("101.00")),
        GridLevel(2, D("101.61")),
        GridLevel(3, D("102.22")),
        GridLevel(4, D("102.83")),
    ]
    result, _ = _intents_for(tmp_path, levels, D("0.01"), D("102.0"))
    assert result.allocation_blocked is None, "partial drop is not a whole block"
    assert _idx("BUY", result.intents) == [1, 2], "only the two valid BUY cells"
    assert _idx("SELL", result.intents) == [4]
    assert _block_reasons(result, "BUY", 0) in result.profit_blocked_cells


# ---------------------------------------------------------------------------
# 6. All cells invalid → zero submissions
# ---------------------------------------------------------------------------

def test_4a_6_all_cells_invalid_zero_submissions(tmp_path):
    levels = [
        GridLevel(0, D("101")),
        GridLevel(1, D("101.5")),
        GridLevel(2, D("102.0")),
    ]
    result, _ = _intents_for(tmp_path, levels, D("0.01"), D("101.7"))
    assert result.allocation_blocked is not None
    assert result.allocation_blocked["status"] == "PROFIT_GATE_BLOCKED"
    assert result.intents == []
    assert _block_reasons(result, "BUY", 0) in result.allocation_blocked["reasons"]
    assert _block_reasons(result, "BUY", 1) in result.allocation_blocked["reasons"]


# ---------------------------------------------------------------------------
# 7. BUY/SELL quantization changes economics separately → actual quantized
#    prices are used (raw spread looks fine, quantized spread does not)
# ---------------------------------------------------------------------------

def test_4a_7_quantized_prices_used_not_raw(tmp_path):
    # Raw pair (100.00 → 100.559): pre-quant net 0.003078 ≥ 0.003 (would
    # earn on the paper economics).
    assert net_pct_from_prices(
        D("100.00"), D("100.559"), D("0.001"), D("0.001"), D("0.0005"),
    ) >= HARD_MIN, "pre-quant economics pass"
    # Tick-0.01 quantization floors 100.559 → 100.55: post-quant net
    # 0.002988 < 0.003 → the gate MUST use the quantized price and block.
    assert _gate_net(D("100.00"), D("100.559"), D("0.01")) < HARD_MIN, \
        "post-quant economics fail (quantized 100.55, not raw 100.559)"
    levels = [GridLevel(0, D("100.00")), GridLevel(1, D("100.559"))]
    result, _ = _intents_for(tmp_path, levels, D("0.01"), D("100.2"))
    assert result.intents == []
    assert _block_reasons(result, "BUY", 0) in result.profit_blocked_cells


# ---------------------------------------------------------------------------
# 8. Fee + slippage are included in the post-quantization calculation
# ---------------------------------------------------------------------------

def test_4a_8_fees_and_slippage_included(tmp_path):
    # Same spread, but taker sell fee (0.005) erodes the net below the min.
    levels = [GridLevel(0, D("100")), GridLevel(1, D("100.65"))]
    # Baseline maker/maker passes:
    result_pass, _ = _intents_for(tmp_path, levels, D("0.01"), D("100.2"))
    assert _idx("BUY", result_pass.intents) == [0]
    # High taker sell fee fails (prefer_maker=False → sell uses taker_fee):
    result_fail, _ = _intents_for(
        tmp_path, levels, D("0.01"), D("100.2"),
        taker_fee=D("0.005"), prefer_maker=False,
    )
    assert result_fail.intents == []
    assert _block_reasons(result_fail, "BUY", 0) in result_fail.profit_blocked_cells


# ---------------------------------------------------------------------------
# 9. ADVERSARIAL (audit F-3): planner (pre-quant) passes, tick quantization
#    erodes post-quant net below hard_min → run_cycle must NOT submit and
#    must record a deterministic block reason.
# ---------------------------------------------------------------------------

def test_4a_9_adversarial_full_cycle_no_submission(tmp_path, monkeypatch):
    """Audit F-3 end-to-end: the planner's PRE-quantization economics pass,
    but Binance tick quantization erodes the actual cell spread below the
    hard minimum, so run_cycle must NOT submit any order and must record a
    deterministic POST_QUANTIZATION_PROFIT_BELOW_MIN block reason."""
    import paper_orchestrator as po
    from storage import connect

    session = _make_session(tmp_path)
    orch = session.orchestrator

    # Pre-quantization economics the planner gates on (config step 0.006):
    pre = net_pct_from_step(D("0.006"), D("0.001"), D("0.001"), D("0.0005"))
    assert pre >= HARD_MIN, "planner's pre-quant economics must PASS"

    # These levels have a ~0.5% raw step; after tick-0.01 quantization every
    # BUY round-trip cell earns < 0.003 (post-quant net 0.0025 / 0.0024).
    levels = [
        GridLevel(0, D("101")),
        GridLevel(1, D("101.5")),
        GridLevel(2, D("102.0")),
    ]
    tick = D("0.01")
    plan = _plan_from_levels(levels, D("101.7"), plan_id="fix4a_adv")

    # Post-quantization economics of the actual cells FAIL the hard min:
    for lo, hi in ((D("101"), D("101.5")), (D("101.5"), D("102.0"))):
        assert po._post_quant_net_profit(
            lo, hi, _rules(tick), D("0.001"), D("0.001"), D("0.0005"),
        ) < HARD_MIN

    ci = _cycle_input(plan, tick, D("101.7"))

    # Seed a funded paper account so allocation is not a funding block; the
    # post-quant profit gate is what must decide.
    con = connect(session.order_engine.db_path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO paper_account_state (id, base_asset, "
            "quote_asset, base_free, base_reserved, quote_free, "
            "quote_reserved, average_cost, realized_pnl, total_fees, updated_at) "
            "VALUES (1,'BTC','USDT','100','0','100000','0','0','0','0',?)",
            ("2026-01-01T00:00:00+00:00",),
        )
        con.commit()
    finally:
        con.close()

    # Keep the real planner decision but feed it the adversarial raw levels.
    monkeypatch.setattr(
        po, "evaluate_adaptive_grid_plan",
        lambda **kw: _plan_from_levels(levels, D("101.7"), plan_id="fix4a_adv"),
    )
    # Neutralize the lifecycle plumbing so the integrity gate passes and the
    # allocation runs against an ACTIVE generation — only the new profit gate
    # stands between the cycle and a submission.
    from types import SimpleNamespace

    def _active_plan_state():
        return SimpleNamespace(
            plan_id="fix4a_adv", pair="BTCUSDT",
            candidate_lower=D("101"), candidate_upper=D("102.0"),
            grid_step=D("0.006"), grid_count=2, regime="RANGE",
            range_quality_score=D("80"), candle_index=1, generation=1,
            lifecycle_state=GridLifecycleState.ACTIVE,
        )

    monkeypatch.setattr(
        orch.lifecycle_manager, "get_current_state",
        lambda con=None, prefix="": GridLifecycleState.ACTIVE,
    )
    monkeypatch.setattr(
        orch.lifecycle_manager, "get_generation",
        lambda con=None, prefix="": 1,
    )
    monkeypatch.setattr(
        orch.lifecycle_manager, "get_active_plan", lambda con=None, prefix="": _active_plan_state(),
    )
    monkeypatch.setattr(
        orch.lifecycle_manager, "handle_planner_decision",
        lambda *a, **kw: SimpleNamespace(to_state=GridLifecycleState.ACTIVE),
    )

    submit_calls = []

    def spy_submit(intent, *a, **kw):
        submit_calls.append(intent.client_order_id)
        raise AssertionError("PaperOrderEngine.submit must NOT be called")

    monkeypatch.setattr(session.order_engine, "submit", spy_submit)

    result = session.run_cycle(ci)

    # The cycle fails closed on the post-quant profit gate — no partial
    # state, no submission.
    assert result.success is False
    assert submit_calls == []
    assert result.orders_submitted == 0
    assert result.blocked_reason is not None
    assert "ALLOCATION_BLOCKED" in result.blocked_reason
    assert "POST_QUANTIZATION_PROFIT_BELOW_MIN" in result.blocked_reason
    # No partial persistent state from the blocked cycle.
    con = connect(session.order_engine.db_path)
    try:
        assert con.execute("SELECT COUNT(*) c FROM orders").fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) c FROM paper_reservations").fetchone()[0] == 0
    finally:
        con.close()
    assert session.is_healthy() is True
