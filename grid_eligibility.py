"""Deterministic Grid Eligibility Decision Engine for Phase 4.

Determines whether the market satisfies explicit deterministic conditions for
running a fixed-range spot grid.

Outputs:
- GRID_ALLOWED: All market regime, liquidity, volatility, and range containment
  criteria are satisfied.
- GRID_BLOCKED: One or more explicit blocking reasons were encountered. All
  reasons are preserved in a structured tuple.

Design Principles:
- Fails closed on any defect or threshold breach.
- Never forces a grid.
- Preserves multiple blocking reasons (does not exit on the first failure).
- Uses the configured fixed LOWER_PRICE and UPPER_PRICE without modification.
- Pure and deterministic: identical inputs produce identical decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

from market_features import MarketFeatures
from market_regime import MarketRegime
from range_quality import RangeQualityResult


class GridEligibilityStatus(str, Enum):
    """Overall grid eligibility status."""

    GRID_ALLOWED = "GRID_ALLOWED"
    GRID_BLOCKED = "GRID_BLOCKED"


class BlockingReason(str, Enum):
    """Explicit reasons why a grid may be blocked."""

    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    INVALID_MARKET_DATA = "INVALID_MARKET_DATA"
    TREND_TOO_STRONG = "TREND_TOO_STRONG"
    VOLATILITY_TOO_HIGH = "VOLATILITY_TOO_HIGH"
    VOLATILITY_TOO_LOW = "VOLATILITY_TOO_LOW"
    RANGE_TOO_UNSTABLE = "RANGE_TOO_UNSTABLE"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    LIQUIDITY_UNAVAILABLE = "LIQUIDITY_UNAVAILABLE"
    RANGE_QUALITY_TOO_LOW = "RANGE_QUALITY_TOO_LOW"
    PRICE_OUTSIDE_RANGE = "PRICE_OUTSIDE_RANGE"


@dataclass(frozen=True)
class GridEligibilityDecision:
    """The structured, deterministic decision for grid eligibility."""

    status: GridEligibilityStatus
    allowed: bool
    reasons: tuple[BlockingReason, ...]
    regime: MarketRegime
    range_quality_score: Decimal
    features: MarketFeatures | None
    diagnostics: dict[str, Any] = field(default_factory=dict)


def evaluate_grid_eligibility(
    features: MarketFeatures | None,
    regime: MarketRegime,
    range_quality: RangeQualityResult | None,
    current_price: Decimal,
    lower_price: Decimal,
    upper_price: Decimal,
    config: dict[str, Any],
    is_insufficient_data: bool = False,
    is_invalid_data: bool = False,
    extra_diagnostics: dict[str, Any] | None = None,
) -> GridEligibilityDecision:
    """Evaluate grid eligibility deterministically against all configured safety rules.

    Preserves multiple blocking reasons simultaneously.
    """
    reasons: list[BlockingReason] = []
    diagnostics: dict[str, Any] = dict(extra_diagnostics or {})

    # 1. Invalid or Insufficient data gates
    if is_invalid_data or regime == MarketRegime.INVALID_DATA:
        reasons.append(BlockingReason.INVALID_MARKET_DATA)
    if is_insufficient_data or regime == MarketRegime.INSUFFICIENT_DATA or features is None:
        reasons.append(BlockingReason.INSUFFICIENT_DATA)

    # If data is completely invalid or missing, fail closed immediately
    if reasons:
        score = range_quality.score if range_quality else Decimal("0")
        return GridEligibilityDecision(
            status=GridEligibilityStatus.GRID_BLOCKED,
            allowed=False,
            reasons=tuple(reasons),
            regime=regime,
            range_quality_score=score,
            features=features,
            diagnostics=diagnostics,
        )

    mi = config.get("market_intelligence", config)
    regime_cfg = mi.get("regime", {})
    liq_cfg = mi.get("liquidity", {})
    qual_cfg = mi.get("quality", {})

    # Extract thresholds
    adx_trend_min = Decimal(str(regime_cfg.get("adx_trend_min", "25")))
    atr_expansion_max = Decimal(str(regime_cfg.get("atr_expansion_ratio", "1.5")))
    dir_eff_max = Decimal(str(regime_cfg.get("directional_efficiency_max", "0.60")))
    containment_min = Decimal(str(regime_cfg.get("price_range_inclusion_min", "0.90")))
    max_spread = Decimal(str(liq_cfg.get("max_spread_pct", "0.003")))
    min_quality_score = Decimal(str(qual_cfg.get("min_range_quality_score", "60")))

    # 2. Trend gate: reject strong directional momentum
    if regime in (MarketRegime.TREND_UP, MarketRegime.TREND_DOWN):
        reasons.append(BlockingReason.TREND_TOO_STRONG)
        diagnostics["trend_adx"] = str(features.adx)
        diagnostics["directional_efficiency"] = str(features.directional_efficiency)
    elif features.adx >= adx_trend_min and features.directional_efficiency > dir_eff_max:
        reasons.append(BlockingReason.TREND_TOO_STRONG)

    # 3. Volatility gates:
    # Too high: rapid expansion or extreme ATR
    if features.atr_expansion_ratio > atr_expansion_max or features.atr_pct > Decimal("0.05"):
        reasons.append(BlockingReason.VOLATILITY_TOO_HIGH)
        diagnostics["atr_expansion_ratio"] = str(features.atr_expansion_ratio)
        diagnostics["atr_pct"] = str(features.atr_pct)

    # Too low: dead market where grid cycles cannot execute
    if features.atr_pct < Decimal("0.002"):
        reasons.append(BlockingReason.VOLATILITY_TOO_LOW)
        diagnostics["atr_pct_too_low"] = str(features.atr_pct)

    # 4. Range stability gate: containment within configured boundaries
    if features.range_containment_pct < containment_min:
        reasons.append(BlockingReason.RANGE_TOO_UNSTABLE)
        diagnostics["containment_pct"] = str(features.range_containment_pct)
        diagnostics["penetration_count"] = features.penetration_count

    # 5. Price inside range gate: current price must be within [lower, upper]
    if current_price < lower_price or current_price > upper_price:
        reasons.append(BlockingReason.PRICE_OUTSIDE_RANGE)
        diagnostics["current_price"] = str(current_price)
        diagnostics["lower_price"] = str(lower_price)
        diagnostics["upper_price"] = str(upper_price)

    # 6. Liquidity & Spread guards:
    if features.spread_pct is None or features.spread is None:
        reasons.append(BlockingReason.LIQUIDITY_UNAVAILABLE)
    elif features.spread_pct > max_spread:
        reasons.append(BlockingReason.SPREAD_TOO_WIDE)
        diagnostics["spread_pct"] = str(features.spread_pct)
        diagnostics["max_spread_pct"] = str(max_spread)

    # 7. Range quality score gate:
    score = range_quality.score if range_quality else Decimal("0")
    if range_quality is None or not range_quality.is_acceptable or score < min_quality_score:
        reasons.append(BlockingReason.RANGE_QUALITY_TOO_LOW)
        diagnostics["range_quality_score"] = str(score)
        diagnostics["min_quality_score"] = str(min_quality_score)

    is_allowed = len(reasons) == 0
    status = GridEligibilityStatus.GRID_ALLOWED if is_allowed else GridEligibilityStatus.GRID_BLOCKED

    return GridEligibilityDecision(
        status=status,
        allowed=is_allowed,
        reasons=tuple(reasons),
        regime=regime,
        range_quality_score=score,
        features=features,
        diagnostics=diagnostics,
    )
