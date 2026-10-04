"""Tests for Phase 5A Adaptive Grid Planner."""

from decimal import Decimal

import pytest

from grid_planner import (
    ActivePlan,
    AdaptiveGridPlan,
    GridLevel,
    HysteresisConfig,
    PlanBlockReason,
    PlanDecision,
    _allocate_budget,
    _check_hysteresis,
    _cooldown_active,
    _compute_grid_count,
    _derive_candidate_range,
    _load_hysteresis_config,
    _validate_spacing_profit,
    compute_plan_id,
    evaluate_adaptive_grid_plan,
)
from market_regime import MarketRegime


D = Decimal


def base_cfg() -> dict:
    return {
        "grid": {
            "step_pct": 0.006,
            "hard_min_net_pct": 0.002,
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
            "slippage_roundtrip_pct": 0.0005,
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
    }


def default_params(**overrides) -> dict:
    params = dict(
        pair="BNBUSDT",
        regime=MarketRegime.RANGE,
        range_quality_score=D("75"),
        current_price=D("600"),
        configured_lower=D("550"),
        configured_upper=D("650"),
        available_base_inventory=D("100"),
        cfg=base_cfg(),
        active_plan=None,
        current_candle_index=0,
    )
    params.update(overrides)
    return params


# ---------------------------------------------------------------------------
# compute_plan_id
# ---------------------------------------------------------------------------

class TestComputePlanId:
    def test_deterministic_same_inputs(self):
        args = ("BNBUSDT", D("550"), D("650"), D("0.006"), 10,
                MarketRegime.RANGE, D("0.006"), D("0.002"))
        assert compute_plan_id(*args) == compute_plan_id(*args)

    def test_prefix_and_length(self):
        pid = compute_plan_id("BNBUSDT", D("550"), D("650"), D("0.006"), 10,
                              MarketRegime.RANGE, D("0.006"), D("0.002"))
        assert pid.startswith("plan_")
        assert len(pid) == 5 + 16

    def test_different_regime_different_id(self):
        a = compute_plan_id("BNBUSDT", D("550"), D("650"), D("0.006"), 10,
                            MarketRegime.RANGE, D("0.006"), D("0.002"))
        b = compute_plan_id("BNBUSDT", D("550"), D("650"), D("0.006"), 10,
                            MarketRegime.TREND_UP, D("0.006"), D("0.002"))
        assert a != b

    def test_different_range_different_id(self):
        a = compute_plan_id("BNBUSDT", D("550"), D("650"), D("0.006"), 10,
                            MarketRegime.RANGE, D("0.006"), D("0.002"))
        b = compute_plan_id("BNBUSDT", D("551"), D("650"), D("0.006"), 10,
                            MarketRegime.RANGE, D("0.006"), D("0.002"))
        assert a != b


# ---------------------------------------------------------------------------
# _derive_candidate_range
# ---------------------------------------------------------------------------

class TestDeriveCandidateRange:
    def test_valid_range_no_reasons(self):
        lo, hi, reasons = _derive_candidate_range(
            D("550"), D("650"), D("600"), D("75"), MarketRegime.RANGE, base_cfg())
        assert lo == D("550")
        assert hi == D("650")
        assert reasons == []

    def test_zero_lower_blocked(self):
        _, _, reasons = _derive_candidate_range(
            D("0"), D("650"), D("600"), D("75"), MarketRegime.RANGE, base_cfg())
        assert PlanBlockReason.INVALID_CANDIDATE_RANGE in reasons

    def test_inverted_range_blocked(self):
        _, _, reasons = _derive_candidate_range(
            D("650"), D("550"), D("600"), D("75"), MarketRegime.RANGE, base_cfg())
        assert PlanBlockReason.INVALID_CANDIDATE_RANGE in reasons

    def test_equal_bounds_blocked(self):
        _, _, reasons = _derive_candidate_range(
            D("600"), D("600"), D("600"), D("75"), MarketRegime.RANGE, base_cfg())
        assert PlanBlockReason.INVALID_CANDIDATE_RANGE in reasons


# ---------------------------------------------------------------------------
# _compute_grid_count
# ---------------------------------------------------------------------------

class TestComputeGridCount:
    def test_happy_path(self):
        levels, eff, reasons = _compute_grid_count(
            D("550"), D("650"), D("0.006"), min_grids=6, max_grids=40,
            max_levels_limit=41)
        assert reasons == []
        assert len(levels) - 1 >= 6
        assert eff <= D("650")

    def test_too_few_cells_blocked(self):
        levels, _, reasons = _compute_grid_count(
            D("550"), D("551"), D("0.006"), min_grids=6, max_grids=40,
            max_levels_limit=41)
        assert PlanBlockReason.GRID_COUNT_INVALID in reasons

    def test_invalid_range_blocked(self):
        _, _, reasons = _compute_grid_count(
            D("0"), D("650"), D("0.006"), min_grids=6, max_grids=40,
            max_levels_limit=41)
        assert PlanBlockReason.GRID_COUNT_INVALID in reasons

    def test_truncates_to_max_grids(self):
        levels, _, reasons = _compute_grid_count(
            D("550"), D("650"), D("0.006"), min_grids=6, max_grids=10,
            max_levels_limit=11)
        assert reasons == []
        assert len(levels) - 1 <= 10


# ---------------------------------------------------------------------------
# _validate_spacing_profit
# ---------------------------------------------------------------------------

class TestValidateSpacingProfit:
    def test_pass_at_default(self):
        net, reasons = _validate_spacing_profit(
            D("0.006"), D("0.001"), D("0.001"), D("0.0005"), D("0.002"))
        assert reasons == []
        assert net > D("0.002")

    def test_fail_when_step_too_small(self):
        net, reasons = _validate_spacing_profit(
            D("0.001"), D("0.001"), D("0.001"), D("0.0005"), D("0.002"))
        assert PlanBlockReason.NET_PROFIT_BELOW_MINIMUM in reasons

    def test_zero_step_blocked(self):
        _, reasons = _validate_spacing_profit(
            D("0"), D("0.001"), D("0.001"), D("0.0005"), D("0.002"))
        assert PlanBlockReason.NET_PROFIT_BELOW_MINIMUM in reasons

    def test_exact_floor_is_rejected_strict(self, monkeypatch):
        """STRICT boundary in the planner gate: net == floor (0.200%) blocks;
        just above the floor (0.201%) passes."""
        import grid_planner
        monkeypatch.setattr(grid_planner, "net_pct_from_step",
                            lambda *a: D("0.002"))
        _, reasons = _validate_spacing_profit(
            D("0.006"), D("0.001"), D("0.001"), D("0.0005"), D("0.002"))
        assert PlanBlockReason.NET_PROFIT_BELOW_MINIMUM in reasons

        monkeypatch.setattr(grid_planner, "net_pct_from_step",
                            lambda *a: D("0.00201"))
        _, reasons = _validate_spacing_profit(
            D("0.006"), D("0.001"), D("0.001"), D("0.0005"), D("0.002"))
        assert reasons == []


# ---------------------------------------------------------------------------
# _allocate_budget
# ---------------------------------------------------------------------------

def make_levels(prices):
    return [GridLevel(i, D(p)) for i, p in enumerate(prices)]


class TestAllocateBudget:
    def test_no_budget_ceiling(self):
        levels = make_levels(["550", "560", "570", "580", "590", "600", "610"])
        buy, req, ok, reasons = _allocate_budget(
            levels, D("25"), D("0"), D("600"), D("100"))
        assert reasons == []
        assert buy > 0

    def test_budget_ceiling_respected(self):
        levels = make_levels(["550", "560", "570", "580", "590", "600", "610"])
        buy, _, _, reasons = _allocate_budget(
            levels, D("25"), D("50"), D("600"), D("100"))
        assert buy <= D("50")
        assert PlanBlockReason.INSUFFICIENT_QUOTE_BUDGET not in reasons

    def test_budget_too_small_blocked(self):
        levels = make_levels(["550", "560", "570", "580", "590", "600", "610"])
        _, _, _, reasons = _allocate_budget(
            levels, D("25"), D("10"), D("600"), D("100"))
        assert PlanBlockReason.INSUFFICIENT_QUOTE_BUDGET in reasons

    def test_insufficient_base_inventory_blocked(self):
        levels = make_levels(["550", "560", "570", "580", "590", "600", "610"])
        _, req, ok, reasons = _allocate_budget(
            levels, D("25"), D("0"), D("600"), D("0.001"))
        assert req > 0
        assert ok is False
        assert PlanBlockReason.INSUFFICIENT_BASE_INVENTORY in reasons

    def test_sufficient_base_inventory_ok(self):
        levels = make_levels(["550", "560", "570", "580", "590", "600", "610"])
        _, req, ok, reasons = _allocate_budget(
            levels, D("25"), D("0"), D("600"), D("1000"))
        assert ok is True
        assert PlanBlockReason.INSUFFICIENT_BASE_INVENTORY not in reasons

    def test_too_few_levels_blocked(self):
        levels = make_levels(["550"])
        _, _, _, reasons = _allocate_budget(
            levels, D("25"), D("0"), D("600"), D("100"))
        assert PlanBlockReason.GRID_COUNT_INVALID in reasons


# ---------------------------------------------------------------------------
# _load_hysteresis_config
# ---------------------------------------------------------------------------

class TestLoadHysteresisConfig:
    def test_defaults_when_missing(self):
        hyst = _load_hysteresis_config({})
        assert hyst.range_change_pct == D("0.02")
        assert hyst.step_change_pct == D("0.10")
        assert hyst.grid_count_change == 3
        assert hyst.quality_degradation == D("5")
        assert hyst.regime_change is True

    def test_custom_values(self):
        cfg = {"adaptive_planner": {"hysteresis": {
            "range_change_pct": 0.05,
            "step_change_pct": 0.20,
            "grid_count_change": 5,
            "quality_degradation": 10,
            "regime_change": False,
        }}}
        hyst = _load_hysteresis_config(cfg)
        assert hyst.range_change_pct == D("0.05")
        assert hyst.step_change_pct == D("0.20")
        assert hyst.grid_count_change == 5
        assert hyst.quality_degradation == D("10")
        assert hyst.regime_change is False


# ---------------------------------------------------------------------------
# _check_hysteresis
# ---------------------------------------------------------------------------

def default_active_plan(**overrides) -> ActivePlan:
    # Derive grid_count from a real fresh evaluation so hysteresis comparisons
    # use the same grid geometry the planner actually builds.
    fresh = evaluate_adaptive_grid_plan(**default_params())
    vals = dict(
        plan_id="plan_abc",
        candidate_lower=D("550"),
        candidate_upper=D("650"),
        grid_step=D("0.006"),
        grid_count=fresh.grid_count,
        regime=MarketRegime.RANGE,
        range_quality_score=D("75"),
        candle_index=0,
    )
    vals.update(overrides)
    return ActivePlan(**vals)


DEFAULT_HYST = HysteresisConfig(
    range_change_pct=D("0.02"),
    step_change_pct=D("0.10"),
    grid_count_change=3,
    quality_degradation=D("5"),
    regime_change=True,
)


_FRESH_COUNT = evaluate_adaptive_grid_plan(**default_params()).grid_count


def hyst(active, lower=D("550"), upper=D("650"), step=D("0.006"),
         count=_FRESH_COUNT, regime=MarketRegime.RANGE, score=D("75")):
    return _check_hysteresis(active, lower, upper, step, count, regime, score,
                             DEFAULT_HYST)


class TestHysteresis:
    def test_no_change_keep(self):
        assert hyst(default_active_plan()) is False

    def test_small_range_change_keep(self):
        # 1% < 2% threshold
        assert hyst(default_active_plan(), lower=D("555")) is False

    def test_large_range_change_trigger(self):
        # |570-550|/550 = 3.6% >= 2%
        assert hyst(default_active_plan(), lower=D("570")) is True

    def test_large_upper_change_trigger(self):
        # |670-650|/650 = 3.1% >= 2%
        assert hyst(default_active_plan(), upper=D("670")) is True

    def test_small_step_change_keep(self):
        # |0.0066-0.006|/0.006 = 10% ... boundary, >= 0.10 triggers
        assert hyst(default_active_plan(), step=D("0.0065")) is False

    def test_large_step_change_trigger(self):
        # |0.0072-0.006|/0.006 = 20% >= 10%
        assert hyst(default_active_plan(), step=D("0.0072")) is True

    def test_step_at_exact_threshold_triggers(self):
        # |0.0066 - 0.006| / 0.006 = 0.10 >= 0.10
        assert hyst(default_active_plan(), step=D("0.0066")) is True

    def test_grid_count_change_within_band_keep(self):
        assert hyst(default_active_plan(), count=_FRESH_COUNT + 3) is False

    def test_grid_count_change_beyond_band_trigger(self):
        assert hyst(default_active_plan(), count=_FRESH_COUNT + 4) is True

    def test_regime_change_trigger(self):
        assert hyst(default_active_plan(), regime=MarketRegime.TREND_UP) is True

    def test_regime_change_disabled_keep(self):
        hyst_no_regime = HysteresisConfig(
            range_change_pct=D("0.02"),
            step_change_pct=D("0.10"),
            grid_count_change=3,
            quality_degradation=D("5"),
            regime_change=False,
        )
        result = _check_hysteresis(
            default_active_plan(), D("550"), D("650"), D("0.006"),
            _FRESH_COUNT,
            MarketRegime.TREND_UP, D("75"), hyst_no_regime)
        assert result is False

    def test_quality_degradation_trigger(self):
        # 75 - 65 = 10 >= 5
        assert hyst(default_active_plan(), score=D("65")) is True

    def test_quality_mild_drop_keep(self):
        # 75 - 72 = 3 < 5
        assert hyst(default_active_plan(), score=D("72")) is False

    def test_quality_improvement_keep(self):
        assert hyst(default_active_plan(), score=D("90")) is False


# ---------------------------------------------------------------------------
# _cooldown_active
# ---------------------------------------------------------------------------

class TestCooldown:
    def test_no_active_plan_never_cooldown(self):
        assert _cooldown_active(None, 100, 4) is False

    def test_within_cooldown(self):
        active = default_active_plan(candle_index=100)
        assert _cooldown_active(active, 102, 4) is True

    def test_cooldown_expired(self):
        active = default_active_plan(candle_index=100)
        assert _cooldown_active(active, 104, 4) is False

    def test_cooldown_boundary_elapsed_equals_cooldown(self):
        active = default_active_plan(candle_index=100)
        assert _cooldown_active(active, 103, 4) is True

    def test_same_candle_cooldown(self):
        active = default_active_plan(candle_index=100)
        assert _cooldown_active(active, 100, 4) is True


# ---------------------------------------------------------------------------
# evaluate_adaptive_grid_plan – decision matrix
# ---------------------------------------------------------------------------

class TestEvaluatePlan:
    def test_range_regime_fresh_grid_allowed(self):
        plan = evaluate_adaptive_grid_plan(**default_params())
        assert plan.decision == PlanDecision.GRID_ALLOWED
        assert plan.reasons == ()
        assert plan.is_actionable is True
        assert plan.grid_count >= 6
        assert plan.grid_type == "GEOMETRIC"

    def test_invalid_data_early_block(self):
        plan = evaluate_adaptive_grid_plan(
            **default_params(regime=MarketRegime.INVALID_DATA))
        assert plan.decision == PlanDecision.GRID_BLOCKED
        assert PlanBlockReason.INVALID_MARKET_DATA in plan.reasons
        assert plan.grid_count == 0
        assert plan.levels == ()

    def test_insufficient_data_early_block(self):
        plan = evaluate_adaptive_grid_plan(
            **default_params(regime=MarketRegime.INSUFFICIENT_DATA))
        assert plan.decision == PlanDecision.GRID_BLOCKED
        assert PlanBlockReason.INSUFFICIENT_DATA in plan.reasons

    @pytest.mark.parametrize("regime", [
        MarketRegime.TREND_UP,
        MarketRegime.TREND_DOWN,
        MarketRegime.VOLATILE,
    ])
    def test_trend_volatile_regime_blocked(self, regime):
        plan = evaluate_adaptive_grid_plan(**default_params(regime=regime))
        assert plan.decision == PlanDecision.GRID_BLOCKED
        assert PlanBlockReason.REGIME_BLOCKED in plan.reasons

    def test_invalid_range_blocked(self):
        plan = evaluate_adaptive_grid_plan(
            **default_params(configured_lower=D("0"), configured_upper=D("650")))
        assert plan.decision == PlanDecision.GRID_BLOCKED
        assert PlanBlockReason.INVALID_CANDIDATE_RANGE in plan.reasons

    def test_step_below_hard_min_blocked(self):
        cfg = base_cfg()
        cfg["grid"]["step_pct"] = 0.001
        plan = evaluate_adaptive_grid_plan(**default_params(cfg=cfg))
        assert plan.decision == PlanDecision.GRID_BLOCKED
        assert PlanBlockReason.NET_PROFIT_BELOW_MINIMUM in plan.reasons

    def test_price_outside_candidate_range_blocked(self):
        plan = evaluate_adaptive_grid_plan(
            **default_params(current_price=D("700")))
        assert plan.decision == PlanDecision.GRID_BLOCKED
        assert PlanBlockReason.PRICE_OUTSIDE_CANDIDATE_RANGE in plan.reasons

    def test_price_below_candidate_range_blocked(self):
        plan = evaluate_adaptive_grid_plan(
            **default_params(current_price=D("500")))
        assert plan.decision == PlanDecision.GRID_BLOCKED
        assert PlanBlockReason.PRICE_OUTSIDE_CANDIDATE_RANGE in plan.reasons

    def test_insufficient_base_inventory_blocked(self):
        plan = evaluate_adaptive_grid_plan(
            **default_params(available_base_inventory=D("0.001")))
        assert plan.decision == PlanDecision.GRID_BLOCKED
        assert PlanBlockReason.INSUFFICIENT_BASE_INVENTORY in plan.reasons

    def test_insufficient_quote_budget_blocked(self):
        cfg = base_cfg()
        cfg["execution"]["total_quote_budget"] = 10
        plan = evaluate_adaptive_grid_plan(**default_params(cfg=cfg))
        assert plan.decision == PlanDecision.GRID_BLOCKED
        assert PlanBlockReason.INSUFFICIENT_QUOTE_BUDGET in plan.reasons

    def test_too_few_grid_cells_blocked(self):
        plan = evaluate_adaptive_grid_plan(
            **default_params(configured_lower=D("599"),
                              configured_upper=D("601"),
                              current_price=D("600")))
        assert plan.decision == PlanDecision.GRID_BLOCKED
        assert PlanBlockReason.GRID_COUNT_INVALID in plan.reasons


# ---------------------------------------------------------------------------
# evaluate_adaptive_grid_plan – cooldown & hysteresis decisions
# ---------------------------------------------------------------------------

class TestCooldownAndHysteresisDecisions:
    def test_cooldown_active_blocks(self):
        active = default_active_plan(candle_index=100)
        plan = evaluate_adaptive_grid_plan(
            **default_params(active_plan=active, current_candle_index=102))
        assert plan.decision == PlanDecision.GRID_BLOCKED
        assert plan.reasons == (PlanBlockReason.COOLDOWN_ACTIVE,)

    def test_cooldown_expired_falls_through_to_hysteresis(self):
        active = default_active_plan(candle_index=100)
        plan = evaluate_adaptive_grid_plan(
            **default_params(active_plan=active, current_candle_index=104))
        # identical candidate → within hysteresis
        assert plan.decision == PlanDecision.KEEP_CURRENT_PLAN
        assert plan.reasons == (PlanBlockReason.HYSTERESIS_NOT_TRIGGERED,)

    def test_material_change_reconfiguration_required(self):
        active = default_active_plan(candle_index=100)
        plan = evaluate_adaptive_grid_plan(
            **default_params(active_plan=active, current_candle_index=104,
                              configured_lower=D("570")))
        assert plan.decision == PlanDecision.RECONFIGURATION_REQUIRED
        assert plan.reasons == ()
        assert plan.is_actionable is True

    def test_cooldown_blocks_even_with_material_change(self):
        active = default_active_plan(candle_index=100)
        plan = evaluate_adaptive_grid_plan(
            **default_params(active_plan=active, current_candle_index=101,
                              configured_lower=D("570")))
        assert plan.decision == PlanDecision.GRID_BLOCKED
        assert PlanBlockReason.COOLDOWN_ACTIVE in plan.reasons

    def test_blocking_reason_takes_precedence_over_cooldown(self):
        active = default_active_plan(candle_index=100)
        plan = evaluate_adaptive_grid_plan(
            **default_params(active_plan=active, current_candle_index=101,
                              regime=MarketRegime.TREND_UP))
        # data/regime reasons are evaluated before cooldown
        assert plan.decision == PlanDecision.GRID_BLOCKED
        assert PlanBlockReason.REGIME_BLOCKED in plan.reasons
        assert PlanBlockReason.COOLDOWN_ACTIVE not in plan.reasons


# ---------------------------------------------------------------------------
# budget & inventory fields
# ---------------------------------------------------------------------------

class TestPlanFields:
    def test_budget_never_exceeds_ceiling(self):
        cfg = base_cfg()
        cfg["execution"]["total_quote_budget"] = 75
        plan = evaluate_adaptive_grid_plan(**default_params(cfg=cfg))
        if plan.buy_quote_budget > 0:
            assert plan.buy_quote_budget <= D("75")

    def test_total_quote_budget_echoed(self):
        cfg = base_cfg()
        cfg["execution"]["total_quote_budget"] = 250
        plan = evaluate_adaptive_grid_plan(**default_params(cfg=cfg))
        assert plan.total_quote_budget == D("250")

    def test_no_synthetic_inventory(self):
        plan = evaluate_adaptive_grid_plan(
            **default_params(available_base_inventory=D("0")))
        assert plan.required_base_inventory >= 0
        assert plan.available_base_inventory == D("0")
        if plan.required_base_inventory > D("0"):
            assert plan.inventory_sufficient is False

    def test_levels_are_frozen_grid_levels(self):
        plan = evaluate_adaptive_grid_plan(**default_params())
        assert all(isinstance(lv, GridLevel) for lv in plan.levels)
        assert len(plan.levels) == plan.grid_count + 1

    def test_grid_count_matches_levels(self):
        plan = evaluate_adaptive_grid_plan(**default_params())
        assert plan.grid_count == len(plan.levels) - 1

    def test_estimated_net_profit_at_least_hard_min(self):
        plan = evaluate_adaptive_grid_plan(**default_params())
        assert plan.decision == PlanDecision.GRID_ALLOWED
        # STRICT gate: the estimated net must be > hard_min (0.002 floor).
        assert plan.estimated_net_profit_per_grid > D("0.002")

    def test_plan_is_immutable(self):
        plan = evaluate_adaptive_grid_plan(**default_params())
        with pytest.raises(AttributeError):
            plan.grid_count = 99
