"""Tests for Phase 4 Market Regime Classification and Boundary Conditions."""

from decimal import Decimal
import pytest

from market_features import MarketFeatures
from market_regime import MarketRegime, classify_market_regime


def default_regime_config() -> dict:
    return {
        "market_intelligence": {
            "regime": {
                "adx_trend_min": 25,
                "atr_expansion_ratio": 1.5,
                "price_range_inclusion_min": 0.90,
                "directional_efficiency_max": 0.60,
            }
        }
    }


def make_features(
    adx: str = "18.0",
    plus_di: str = "20.0",
    minus_di: str = "19.0",
    directional_efficiency: str = "0.25",
    atr_expansion_ratio: str = "1.0",
    range_containment_pct: str = "1.0",
) -> MarketFeatures:
    return MarketFeatures(
        symbol="BTCUSDT",
        close_price=Decimal("100.0"),
        atr=Decimal("1.0"),
        atr_pct=Decimal("0.01"),
        adx=Decimal(adx),
        plus_di=Decimal(plus_di),
        minus_di=Decimal(minus_di),
        bb_middle=Decimal("100.0"),
        bb_upper=Decimal("102.0"),
        bb_lower=Decimal("98.0"),
        bb_width=Decimal("4.0"),
        bb_width_pct=Decimal("0.04"),
        current_volume=Decimal("100"),
        baseline_volume=Decimal("100"),
        volume_spike_ratio=Decimal("1.0"),
        directional_efficiency=Decimal(directional_efficiency),
        atr_expansion_ratio=Decimal(atr_expansion_ratio),
        range_containment_pct=Decimal(range_containment_pct),
        penetration_count=0,
        spread=Decimal("0.02"),
        spread_pct=Decimal("0.0002"),
        rsi=Decimal("50"),
        volume_oscillator=Decimal("0.5"),
        z_score=Decimal("0"),
    )


def test_range_classification():
    """Scenario 15: Non-trending, stable volatility, high containment -> RANGE."""
    cfg = default_regime_config()
    features = make_features(
        adx="18.0",
        directional_efficiency="0.30",
        atr_expansion_ratio="1.0",
        range_containment_pct="0.95",
    )
    regime, reason = classify_market_regime(features, cfg)
    assert regime == MarketRegime.RANGE
    assert "Range regime confirmed" in reason


def test_trend_up_classification():
    """Scenario 16: High ADX, high directional efficiency, +DI > -DI -> TREND_UP."""
    cfg = default_regime_config()
    features = make_features(
        adx="32.0",
        plus_di="35.0",
        minus_di="12.0",
        directional_efficiency="0.75",
        atr_expansion_ratio="1.1",
        range_containment_pct="0.95",
    )
    regime, reason = classify_market_regime(features, cfg)
    assert regime == MarketRegime.TREND_UP
    assert "Uptrend detected" in reason


def test_trend_down_classification():
    """Scenario 17: High ADX, high directional efficiency, -DI > +DI -> TREND_DOWN."""
    cfg = default_regime_config()
    features = make_features(
        adx="35.0",
        plus_di="10.0",
        minus_di="40.0",
        directional_efficiency="0.80",
        atr_expansion_ratio="1.2",
        range_containment_pct="0.95",
    )
    regime, reason = classify_market_regime(features, cfg)
    assert regime == MarketRegime.TREND_DOWN
    assert "Downtrend detected" in reason


def test_volatile_classification_atr_expansion():
    """Scenario 18a: High ATR expansion -> VOLATILE."""
    cfg = default_regime_config()
    features = make_features(atr_expansion_ratio="1.8")
    regime, reason = classify_market_regime(features, cfg)
    assert regime == MarketRegime.VOLATILE
    assert "ATR expansion ratio" in reason


def test_volatile_classification_containment_breach():
    """Scenario 18b: Containment below minimum -> VOLATILE."""
    cfg = default_regime_config()
    features = make_features(range_containment_pct="0.75")
    regime, reason = classify_market_regime(features, cfg)
    assert regime == MarketRegime.VOLATILE
    assert "Range containment" in reason


def test_insufficient_data_classification():
    """Scenario 19: Insufficient data flag returns INSUFFICIENT_DATA."""
    cfg = default_regime_config()
    regime, reason = classify_market_regime(None, cfg, is_insufficient_data=True)
    assert regime == MarketRegime.INSUFFICIENT_DATA
    assert "Insufficient" in reason


def test_invalid_data_classification():
    """Scenario 20: Invalid data flag returns INVALID_DATA."""
    cfg = default_regime_config()
    regime, reason = classify_market_regime(None, cfg, is_invalid_data=True, error_message="corrupt OHLC")
    assert regime == MarketRegime.INVALID_DATA
    assert "corrupt OHLC" in reason


@pytest.mark.parametrize(
    "adx_val,eff_val,expected_regime",
    [
        ("24.99", "0.75", MarketRegime.RANGE),     # ADX just below 25 -> RANGE
        ("25.00", "0.60", MarketRegime.RANGE),     # Eff at threshold 0.60 (not > 0.60) -> RANGE
        ("25.00", "0.6001", MarketRegime.TREND_UP),# Exactly 25 and eff > 0.60 -> TREND_UP
        ("25.01", "0.61", MarketRegime.TREND_UP),  # Just above threshold -> TREND_UP
    ],
)
def test_trend_boundary_conditions(adx_val, eff_val, expected_regime):
    """Boundary conditions around ADX (25) and directional efficiency (0.60)."""
    cfg = default_regime_config()
    features = make_features(
        adx=adx_val,
        plus_di="30.0",
        minus_di="15.0",
        directional_efficiency=eff_val,
        atr_expansion_ratio="1.0",
        range_containment_pct="1.0",
    )
    regime, _ = classify_market_regime(features, cfg)
    assert regime == expected_regime


@pytest.mark.parametrize(
    "atr_exp,expected_regime",
    [
        ("1.499", MarketRegime.RANGE),
        ("1.500", MarketRegime.RANGE),    # at threshold 1.5 is <= 1.5 -> RANGE
        ("1.501", MarketRegime.VOLATILE), # > 1.5 -> VOLATILE
    ],
)
def test_atr_expansion_boundary_conditions(atr_exp, expected_regime):
    """Boundary conditions around ATR expansion ratio (1.50)."""
    cfg = default_regime_config()
    features = make_features(atr_expansion_ratio=atr_exp)
    regime, _ = classify_market_regime(features, cfg)
    assert regime == expected_regime
