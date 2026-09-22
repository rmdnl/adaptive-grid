"""Tests for Phase 4 Range Quality Scoring Engine."""

from decimal import Decimal
import pytest

from market_features import MarketFeatures
from range_quality import calculate_range_quality


def default_quality_config() -> dict:
    return {
        "market_intelligence": {
            "quality": {
                "min_range_quality_score": 60,
                "weight_trend_stability": 0.25,
                "weight_volatility_suitability": 0.20,
                "weight_bb_width_suitability": 0.15,
                "weight_volume_stability": 0.10,
                "weight_spread_suitability": 0.10,
                "weight_range_containment": 0.20,
            },
            "liquidity": {
                "max_spread_pct": 0.003,
            },
        }
    }


def make_features_for_quality(
    adx: str = "15.0",
    dir_eff: str = "0.20",
    atr_pct: str = "0.015",
    atr_exp: str = "1.0",
    bb_width_pct: str = "0.03",
    vol_spike: str = "1.0",
    spread_pct: str = "0.0003",
    containment: str = "1.0",
) -> MarketFeatures:
    return MarketFeatures(
        symbol="BTCUSDT",
        close_price=Decimal("100.0"),
        atr=Decimal("1.5"),
        atr_pct=Decimal(atr_pct),
        adx=Decimal(adx),
        plus_di=Decimal("20.0"),
        minus_di=Decimal("20.0"),
        bb_middle=Decimal("100.0"),
        bb_upper=Decimal("101.5"),
        bb_lower=Decimal("98.5"),
        bb_width=Decimal("3.0"),
        bb_width_pct=Decimal(bb_width_pct),
        current_volume=Decimal("100"),
        baseline_volume=Decimal("100"),
        volume_spike_ratio=Decimal(vol_spike),
        directional_efficiency=Decimal(dir_eff),
        atr_expansion_ratio=Decimal(atr_exp),
        range_containment_pct=Decimal(containment),
        penetration_count=0,
        spread=Decimal("0.03"),
        spread_pct=Decimal(spread_pct),
    )


def test_range_quality_not_hardcoded():
    """Scenario 29: Range quality score responds dynamically to changing features."""
    cfg = default_quality_config()
    ideal_features = make_features_for_quality(
        adx="12.0",
        dir_eff="0.15",
        atr_pct="0.015",
        bb_width_pct="0.03",
        vol_spike="1.0",
        spread_pct="0.0002",
        containment="1.0",
    )
    degraded_features = make_features_for_quality(
        adx="35.0",
        dir_eff="0.65",
        atr_pct="0.045",
        bb_width_pct="0.09",
        vol_spike="2.2",
        spread_pct="0.0025",
        containment="0.60",
    )
    res_ideal = calculate_range_quality(ideal_features, cfg)
    res_degraded = calculate_range_quality(degraded_features, cfg)

    assert res_ideal.score > res_degraded.score
    assert res_ideal.score >= Decimal("90")
    assert res_degraded.score < Decimal("50")
    assert res_ideal.is_acceptable is True
    assert res_degraded.is_acceptable is False


def test_range_quality_score_bounds():
    """Scenario 30: Range quality score stays strictly within [0, 100]."""
    cfg = default_quality_config()

    # Extreme worst-case features
    worst_features = make_features_for_quality(
        adx="99.0",
        dir_eff="1.0",
        atr_pct="0.10",
        atr_exp="3.0",
        bb_width_pct="0.30",
        vol_spike="10.0",
        spread_pct="0.05",
        containment="0.0",
    )
    res_worst = calculate_range_quality(worst_features, cfg)
    assert res_worst.score == Decimal("0")

    # Extreme best-case features
    best_features = make_features_for_quality(
        adx="5.0",
        dir_eff="0.05",
        atr_pct="0.015",
        atr_exp="0.9",
        bb_width_pct="0.03",
        vol_spike="1.0",
        spread_pct="0.0001",
        containment="1.0",
    )
    res_best = calculate_range_quality(best_features, cfg)
    assert res_best.score == Decimal("100")


def test_quality_breakdown_components():
    """Verify all 6 component scores are properly populated and bounded."""
    cfg = default_quality_config()
    features = make_features_for_quality()
    res = calculate_range_quality(features, cfg)
    b = res.breakdown

    for comp_score in (
        b.trend_stability,
        b.volatility_suitability,
        b.bb_width_suitability,
        b.volume_stability,
        b.spread_suitability,
        b.range_containment,
    ):
        assert Decimal("0") <= comp_score <= Decimal("100")


def test_quality_weights_sum_to_one():
    """Verify configured weights sum to 1.0 (100%)."""
    cfg = default_quality_config()
    qc = cfg["market_intelligence"]["quality"]
    total_w = (
        Decimal(str(qc["weight_trend_stability"]))
        + Decimal(str(qc["weight_volatility_suitability"]))
        + Decimal(str(qc["weight_bb_width_suitability"]))
        + Decimal(str(qc["weight_volume_stability"]))
        + Decimal(str(qc["weight_spread_suitability"]))
        + Decimal(str(qc["weight_range_containment"]))
    )
    assert total_w == Decimal("1.00")
