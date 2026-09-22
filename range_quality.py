"""Deterministic Range Quality Scoring Model for Phase 4.

Calculates an objective, reproducible Range Quality Score in the range [0, 100].
The score measures how suitable the current market conditions are for running
a fixed-range spot grid strategy.

Components & Weights (configured in config.yaml, sum = 1.0):
1. Trend Stability (25%): Low ADX and low directional efficiency.
2. Volatility Suitability (20%): Moderate ATR percentage (avoiding dead markets
   and excessive volatility expansion).
3. Bollinger Width Suitability (15%): Moderate band width (avoiding severe
   squeeze and extreme volatility).
4. Volume Stability (10%): Absence of massive volume spikes.
5. Spread / Liquidity Suitability (10%): Tight bid/ask spread.
6. Range Containment (20%): High percentage of recent candle highs and lows
   strictly contained within [lower_price, upper_price].
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from market_features import MarketFeatures


def _clamp(val: Decimal, low: Decimal = Decimal("0"), high: Decimal = Decimal("100")) -> Decimal:
    return max(low, min(high, val))


@dataclass(frozen=True)
class RangeQualityBreakdown:
    """Component scores (each 0..100) before weighting."""

    trend_stability: Decimal
    volatility_suitability: Decimal
    bb_width_suitability: Decimal
    volume_stability: Decimal
    spread_suitability: Decimal
    range_containment: Decimal


@dataclass(frozen=True)
class RangeQualityResult:
    """The aggregate range quality assessment."""

    score: Decimal
    min_score: Decimal
    is_acceptable: bool
    breakdown: RangeQualityBreakdown


def _score_trend_stability(adx: Decimal, dir_eff: Decimal) -> Decimal:
    """Score trend absence: low ADX and low directional efficiency yield 100."""
    # ADX component: <= 20 -> 100; >= 40 -> 0; linear in between
    if adx <= Decimal("20"):
        s_adx = Decimal("100")
    elif adx >= Decimal("40"):
        s_adx = Decimal("0")
    else:
        s_adx = (Decimal("40") - adx) / Decimal("20") * Decimal("100")

    # Directional efficiency: <= 0.30 -> 100; >= 0.70 -> 0; linear in between
    if dir_eff <= Decimal("0.30"):
        s_eff = Decimal("100")
    elif dir_eff >= Decimal("0.70"):
        s_eff = Decimal("0")
    else:
        s_eff = (Decimal("0.70") - dir_eff) / Decimal("0.40") * Decimal("100")

    return _clamp((s_adx + s_eff) / Decimal("2"))


def _score_volatility(atr_pct: Decimal, atr_expansion: Decimal) -> Decimal:
    """Score volatility suitability: ideal range is 0.5% to 2.5% ATR."""
    if atr_pct < Decimal("0.005"):
        # Dead market penalty
        base = (atr_pct / Decimal("0.005")) * Decimal("100")
    elif atr_pct <= Decimal("0.025"):
        base = Decimal("100")
    elif atr_pct >= Decimal("0.060"):
        base = Decimal("0")
    else:
        base = ((Decimal("0.060") - atr_pct) / Decimal("0.035")) * Decimal("100")

    # Penalize rapid volatility expansion
    if atr_expansion > Decimal("1.0"):
        expansion_factor = max(Decimal("0"), Decimal("2.0") - atr_expansion)
        base = base * min(Decimal("1.0"), expansion_factor)

    return _clamp(base)


def _score_bb_width(bb_width_pct: Decimal) -> Decimal:
    """Score Bollinger width: ideal band width is 1.5% to 6.0%."""
    if bb_width_pct < Decimal("0.015"):
        # Extreme squeeze / breakout danger
        return _clamp((bb_width_pct / Decimal("0.015")) * Decimal("100"))
    if bb_width_pct <= Decimal("0.060"):
        return Decimal("100")
    if bb_width_pct >= Decimal("0.120"):
        return Decimal("0")
    return _clamp(((Decimal("0.120") - bb_width_pct) / Decimal("0.060")) * Decimal("100"))


def _score_volume_stability(volume_spike_ratio: Decimal) -> Decimal:
    """Score volume stability: spikes > 2.5x are penalized."""
    if volume_spike_ratio <= Decimal("1.2"):
        return Decimal("100")
    if volume_spike_ratio >= Decimal("2.5"):
        return Decimal("0")
    return _clamp(((Decimal("2.5") - volume_spike_ratio) / Decimal("1.3")) * Decimal("100"))


def _score_spread(spread_pct: Decimal | None, max_spread_pct: Decimal) -> Decimal:
    """Score liquidity spread: tight spread <= 0.05% gives 100, >= max_spread gives 0."""
    if spread_pct is None or spread_pct < 0:
        return Decimal("0")
    tight_thresh = Decimal("0.0005")
    if spread_pct <= tight_thresh:
        return Decimal("100")
    if spread_pct >= max_spread_pct:
        return Decimal("0")
    span = max_spread_pct - tight_thresh
    if span <= 0:
        return Decimal("0")
    return _clamp(((max_spread_pct - spread_pct) / span) * Decimal("100"))


def calculate_range_quality(
    features: MarketFeatures,
    config: dict[str, Any],
) -> RangeQualityResult:
    """Calculate the deterministic Range Quality Score from features and configuration."""
    mi = config.get("market_intelligence", config)
    quality_cfg = mi.get("quality", {})
    liq_cfg = mi.get("liquidity", {})

    min_score = Decimal(str(quality_cfg.get("min_range_quality_score", "60")))
    max_spread = Decimal(str(liq_cfg.get("max_spread_pct", "0.003")))

    w_trend = Decimal(str(quality_cfg.get("weight_trend_stability", "0.25")))
    w_vol = Decimal(str(quality_cfg.get("weight_volatility_suitability", "0.20")))
    w_bb = Decimal(str(quality_cfg.get("weight_bb_width_suitability", "0.15")))
    w_vol_stab = Decimal(str(quality_cfg.get("weight_volume_stability", "0.10")))
    w_spread = Decimal(str(quality_cfg.get("weight_spread_suitability", "0.10")))
    w_cont = Decimal(str(quality_cfg.get("weight_range_containment", "0.20")))

    s_trend = _score_trend_stability(features.adx, features.directional_efficiency)
    s_vol = _score_volatility(features.atr_pct, features.atr_expansion_ratio)
    s_bb = _score_bb_width(features.bb_width_pct)
    s_vol_stab = _score_volume_stability(features.volume_spike_ratio)
    s_spread = _score_spread(features.spread_pct, max_spread)
    s_cont = _clamp(features.range_containment_pct * Decimal("100"))

    breakdown = RangeQualityBreakdown(
        trend_stability=round(s_trend, 2),
        volatility_suitability=round(s_vol, 2),
        bb_width_suitability=round(s_bb, 2),
        volume_stability=round(s_vol_stab, 2),
        spread_suitability=round(s_spread, 2),
        range_containment=round(s_cont, 2),
    )

    total = (
        w_trend * s_trend
        + w_vol * s_vol
        + w_bb * s_bb
        + w_vol_stab * s_vol_stab
        + w_spread * s_spread
        + w_cont * s_cont
    )
    final_score = round(_clamp(total), 2)
    is_acceptable = final_score >= min_score

    return RangeQualityResult(
        score=final_score,
        min_score=min_score,
        is_acceptable=is_acceptable,
        breakdown=breakdown,
    )
