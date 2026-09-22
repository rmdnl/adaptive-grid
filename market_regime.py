"""Deterministic Market Regime Classification.

Classifies market state into explicit enums:
- RANGE: Market is bounded, non-trending, and suitable for grid oscillation.
- TREND_UP: Market has strong upward directional bias (+DI > -DI, high ADX, high efficiency).
- TREND_DOWN: Market has strong downward directional bias (-DI > +DI, high ADX, high efficiency).
- VOLATILE: Market is experiencing rapid volatility expansion or volume breakout.
- INSUFFICIENT_DATA: History has fewer candles than required for indicator stability.
- INVALID_DATA: Candle data is corrupt, non-monotonic, or contains invalid values.

Semantics are deterministic and never rely on a single indicator in isolation.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any

from market_features import MarketFeatures


class MarketRegime(str, Enum):
    """Explicit deterministic market regime states."""

    RANGE = "RANGE"
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    VOLATILE = "VOLATILE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    INVALID_DATA = "INVALID_DATA"


def classify_market_regime(
    features: MarketFeatures | None,
    config: dict[str, Any],
    is_insufficient_data: bool = False,
    is_invalid_data: bool = False,
    error_message: str | None = None,
) -> tuple[MarketRegime, str]:
    """Classify the current market regime deterministically.

    Returns:
        tuple of (MarketRegime, diagnostic_reason)
    """
    if is_invalid_data:
        return MarketRegime.INVALID_DATA, error_message or "Candle data failed validity checks"

    if is_insufficient_data or features is None:
        return MarketRegime.INSUFFICIENT_DATA, error_message or "Insufficient closed candle history"

    mi = config.get("market_intelligence", config)
    regime_cfg = mi.get("regime", {})

    adx_trend_min = Decimal(str(regime_cfg.get("adx_trend_min", "25")))
    atr_expansion_max = Decimal(str(regime_cfg.get("atr_expansion_ratio", "1.5")))
    dir_eff_max = Decimal(str(regime_cfg.get("directional_efficiency_max", "0.60")))
    containment_min = Decimal(str(regime_cfg.get("price_range_inclusion_min", "0.90")))

    # 1. Volatility expansion check
    if features.atr_expansion_ratio > atr_expansion_max:
        return (
            MarketRegime.VOLATILE,
            f"ATR expansion ratio {features.atr_expansion_ratio} exceeds max {atr_expansion_max}",
        )

    # 2. Trending checks: requires both high ADX and high directional efficiency
    is_trending = (features.adx >= adx_trend_min) and (features.directional_efficiency > dir_eff_max)
    if is_trending:
        if features.plus_di > features.minus_di:
            return (
                MarketRegime.TREND_UP,
                f"Uptrend detected: ADX={features.adx}>={adx_trend_min}, "
                f"DirEff={features.directional_efficiency}>{dir_eff_max}, "
                f"+DI={features.plus_di} > -DI={features.minus_di}",
            )
        if features.minus_di > features.plus_di:
            return (
                MarketRegime.TREND_DOWN,
                f"Downtrend detected: ADX={features.adx}>={adx_trend_min}, "
                f"DirEff={features.directional_efficiency}>{dir_eff_max}, "
                f"-DI={features.minus_di} > +DI={features.plus_di}",
            )

    # 3. If containment is severely broken, classify as VOLATILE
    if features.range_containment_pct < containment_min:
        return (
            MarketRegime.VOLATILE,
            f"Range containment {features.range_containment_pct:.2%} below min {containment_min:.2%}",
        )

    # 4. Otherwise, market satisfies range-bound conditions
    return (
        MarketRegime.RANGE,
        f"Range regime confirmed: ADX={features.adx} < {adx_trend_min}, "
        f"DirEff={features.directional_efficiency} <= {dir_eff_max}, "
        f"Containment={features.range_containment_pct:.2%}",
    )
