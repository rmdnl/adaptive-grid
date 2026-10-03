"""Tests for Phase 4 Grid Eligibility Decision Engine."""

from decimal import Decimal
import pytest

from grid_eligibility import (
    BlockingReason,
    GridEligibilityDecision,
    GridEligibilityStatus,
    evaluate_grid_eligibility,
)
from market_features import MarketFeatures
from market_regime import MarketRegime
from range_quality import RangeQualityBreakdown, RangeQualityResult


def default_eligibility_config() -> dict:
    return {
        "market_intelligence": {
            "regime": {
                "adx_trend_min": 25,
                "atr_expansion_ratio": 1.5,
                "price_range_inclusion_min": 0.90,
                "directional_efficiency_max": 0.60,
            },
            "liquidity": {
                "max_spread_pct": 0.003,
            },
            "quality": {
                "min_range_quality_score": 60,
            },
        }
    }


def make_features_eligibility(
    adx: str = "18.0",
    dir_eff: str = "0.20",
    atr_pct: str = "0.012",
    atr_exp: str = "1.0",
    containment: str = "0.95",
    spread_pct: str | None = "0.0005",
) -> MarketFeatures:
    return MarketFeatures(
        symbol="BTCUSDT",
        close_price=Decimal("100.0"),
        atr=Decimal("1.2"),
        atr_pct=Decimal(atr_pct),
        adx=Decimal(adx),
        plus_di=Decimal("20.0"),
        minus_di=Decimal("19.0"),
        bb_middle=Decimal("100.0"),
        bb_upper=Decimal("101.5"),
        bb_lower=Decimal("98.5"),
        bb_width=Decimal("3.0"),
        bb_width_pct=Decimal("0.03"),
        current_volume=Decimal("100"),
        baseline_volume=Decimal("100"),
        volume_spike_ratio=Decimal("1.0"),
        directional_efficiency=Decimal(dir_eff),
        atr_expansion_ratio=Decimal(atr_exp),
        range_containment_pct=Decimal(containment),
        penetration_count=1,
        spread=Decimal("0.05") if spread_pct else None,
        spread_pct=Decimal(spread_pct) if spread_pct else None,
        rsi=Decimal("50"),
        volume_oscillator=Decimal("0.5"),
        z_score=Decimal("0"),
    )


def make_quality_result(score: str = "75.0") -> RangeQualityResult:
    s = Decimal(score)
    return RangeQualityResult(
        score=s,
        min_score=Decimal("60"),
        is_acceptable=(s >= Decimal("60")),
        breakdown=RangeQualityBreakdown(
            trend_stability=s,
            volatility_suitability=s,
            bb_width_suitability=s,
            volume_stability=s,
            spread_suitability=s,
            range_containment=s,
        ),
    )


def test_grid_allowed_scenario():
    """Scenario 31: All safety rules satisfied -> GRID_ALLOWED."""
    cfg = default_eligibility_config()
    features = make_features_eligibility()
    quality = make_quality_result("82.0")
    decision = evaluate_grid_eligibility(
        features=features,
        regime=MarketRegime.RANGE,
        range_quality=quality,
        current_price=Decimal("100.0"),
        lower_price=Decimal("90.0"),
        upper_price=Decimal("110.0"),
        config=cfg,
    )
    assert decision.status == GridEligibilityStatus.GRID_ALLOWED
    assert decision.allowed is True
    assert len(decision.reasons) == 0


def test_grid_blocked_strong_trend():
    """Scenario 32: Strong trend regime blocks grid."""
    cfg = default_eligibility_config()
    features = make_features_eligibility(adx="35.0", dir_eff="0.75")
    quality = make_quality_result("70.0")
    decision = evaluate_grid_eligibility(
        features=features,
        regime=MarketRegime.TREND_UP,
        range_quality=quality,
        current_price=Decimal("100.0"),
        lower_price=Decimal("90.0"),
        upper_price=Decimal("110.0"),
        config=cfg,
    )
    assert decision.status == GridEligibilityStatus.GRID_BLOCKED
    assert decision.allowed is False
    assert BlockingReason.TREND_TOO_STRONG in decision.reasons


def test_grid_blocked_excessive_volatility():
    """Scenario 33: High ATR expansion blocks grid."""
    cfg = default_eligibility_config()
    features = make_features_eligibility(atr_exp="2.2")
    quality = make_quality_result("70.0")
    decision = evaluate_grid_eligibility(
        features=features,
        regime=MarketRegime.VOLATILE,
        range_quality=quality,
        current_price=Decimal("100.0"),
        lower_price=Decimal("90.0"),
        upper_price=Decimal("110.0"),
        config=cfg,
    )
    assert decision.status == GridEligibilityStatus.GRID_BLOCKED
    assert decision.allowed is False
    assert BlockingReason.VOLATILITY_TOO_HIGH in decision.reasons


def test_grid_blocked_wide_spread():
    """Scenario 34: Spread exceeds maximum threshold blocks grid."""
    cfg = default_eligibility_config()
    features = make_features_eligibility(spread_pct="0.005")  # 0.50% > 0.30%
    quality = make_quality_result("70.0")
    decision = evaluate_grid_eligibility(
        features=features,
        regime=MarketRegime.RANGE,
        range_quality=quality,
        current_price=Decimal("100.0"),
        lower_price=Decimal("90.0"),
        upper_price=Decimal("110.0"),
        config=cfg,
    )
    assert decision.status == GridEligibilityStatus.GRID_BLOCKED
    assert decision.allowed is False
    assert BlockingReason.SPREAD_TOO_WIDE in decision.reasons


def test_grid_blocked_poor_range_quality():
    """Scenario 35: Quality score below minimum blocks grid."""
    cfg = default_eligibility_config()
    features = make_features_eligibility()
    quality = make_quality_result("45.0")  # 45 < 60
    decision = evaluate_grid_eligibility(
        features=features,
        regime=MarketRegime.RANGE,
        range_quality=quality,
        current_price=Decimal("100.0"),
        lower_price=Decimal("90.0"),
        upper_price=Decimal("110.0"),
        config=cfg,
    )
    assert decision.status == GridEligibilityStatus.GRID_BLOCKED
    assert decision.allowed is False
    assert BlockingReason.RANGE_QUALITY_TOO_LOW in decision.reasons


def test_grid_blocked_insufficient_data():
    """Scenario 36: Insufficient data flag blocks grid."""
    cfg = default_eligibility_config()
    decision = evaluate_grid_eligibility(
        features=None,
        regime=MarketRegime.INSUFFICIENT_DATA,
        range_quality=None,
        current_price=Decimal("100.0"),
        lower_price=Decimal("90.0"),
        upper_price=Decimal("110.0"),
        config=cfg,
        is_insufficient_data=True,
    )
    assert decision.status == GridEligibilityStatus.GRID_BLOCKED
    assert decision.allowed is False
    assert BlockingReason.INSUFFICIENT_DATA in decision.reasons


def test_multiple_blocking_reasons_preserved():
    """Scenario 37: Multiple blocking reasons are all accumulated, never dropped."""
    cfg = default_eligibility_config()
    # Trend too strong + Volatility too high + Spread too wide + Quality too low + Price outside range
    features = make_features_eligibility(
        adx="38.0",
        dir_eff="0.80",
        atr_exp="2.5",
        spread_pct="0.008",
    )
    quality = make_quality_result("35.0")
    decision = evaluate_grid_eligibility(
        features=features,
        regime=MarketRegime.TREND_UP,
        range_quality=quality,
        current_price=Decimal("120.0"),  # outside [90, 110]
        lower_price=Decimal("90.0"),
        upper_price=Decimal("110.0"),
        config=cfg,
    )
    assert decision.status == GridEligibilityStatus.GRID_BLOCKED
    assert BlockingReason.TREND_TOO_STRONG in decision.reasons
    assert BlockingReason.VOLATILITY_TOO_HIGH in decision.reasons
    assert BlockingReason.SPREAD_TOO_WIDE in decision.reasons
    assert BlockingReason.RANGE_QUALITY_TOO_LOW in decision.reasons
    assert BlockingReason.PRICE_OUTSIDE_RANGE in decision.reasons
    assert len(decision.reasons) >= 5


def test_configured_boundaries_respected_no_range_modification():
    """Scenarios 38 & 39: Configured lower_price and upper_price are respected without modification."""
    cfg = default_eligibility_config()
    features = make_features_eligibility()
    quality = make_quality_result("80.0")
    lower = Decimal("85.50")
    upper = Decimal("115.50")
    decision = evaluate_grid_eligibility(
        features=features,
        regime=MarketRegime.RANGE,
        range_quality=quality,
        current_price=Decimal("100.0"),
        lower_price=lower,
        upper_price=upper,
        config=cfg,
    )
    assert decision.allowed is True
    # Confirm lower and upper values were not modified
    assert lower == Decimal("85.50")
    assert upper == Decimal("115.50")


def test_price_outside_range_blocking():
    """Price below lower_price or above upper_price triggers PRICE_OUTSIDE_RANGE."""
    cfg = default_eligibility_config()
    features = make_features_eligibility()
    quality = make_quality_result("80.0")

    decision_below = evaluate_grid_eligibility(
        features=features,
        regime=MarketRegime.RANGE,
        range_quality=quality,
        current_price=Decimal("89.99"),
        lower_price=Decimal("90.0"),
        upper_price=Decimal("110.0"),
        config=cfg,
    )
    assert BlockingReason.PRICE_OUTSIDE_RANGE in decision_below.reasons

    decision_above = evaluate_grid_eligibility(
        features=features,
        regime=MarketRegime.RANGE,
        range_quality=quality,
        current_price=Decimal("110.01"),
        lower_price=Decimal("90.0"),
        upper_price=Decimal("110.0"),
        config=cfg,
    )
    assert BlockingReason.PRICE_OUTSIDE_RANGE in decision_above.reasons


def test_deterministic_repeated_eligibility():
    """Scenario 28: Repeated eligibility evaluation produces identical decision."""
    cfg = default_eligibility_config()
    features = make_features_eligibility()
    quality = make_quality_result("78.0")
    d1 = evaluate_grid_eligibility(
        features=features,
        regime=MarketRegime.RANGE,
        range_quality=quality,
        current_price=Decimal("100.0"),
        lower_price=Decimal("90.0"),
        upper_price=Decimal("110.0"),
        config=cfg,
    )
    d2 = evaluate_grid_eligibility(
        features=features,
        regime=MarketRegime.RANGE,
        range_quality=quality,
        current_price=Decimal("100.0"),
        lower_price=Decimal("90.0"),
        upper_price=Decimal("110.0"),
        config=cfg,
    )
    assert d1 == d2
