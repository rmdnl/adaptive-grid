from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math

import pandas as pd


def D(value) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True)
class RangeCandidate:
    lower: Decimal
    upper: Decimal
    quality: float
    width_pct: Decimal
    approved: bool
    reason: str
    position_in_range: float


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(value)))


def _score_low_is_good(value: float, threshold: float, deadband: float) -> float:
    if not math.isfinite(value):
        return 0.0
    if value <= threshold:
        return 100.0
    if value >= threshold + deadband:
        return 0.0
    return _clamp((threshold + deadband - value) / deadband * 100.0)


def auto_range(
    df: pd.DataFrame,
    support_quantile=0.10,
    resistance_quantile=0.90,
    min_width_pct=0.03,
    max_width_pct=0.25,
    min_quality_score=65,
    require_price_inside=True,
) -> RangeCandidate:
    if len(df) < 50:
        raise ValueError("Not enough closed candles for auto-range")

    required = {"high", "low", "close", "adx", "atr_pct", "bb_width", "volume_ratio"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing range inputs: {sorted(missing)}")

    highs = pd.to_numeric(df["high"], errors="coerce").dropna()
    lows = pd.to_numeric(df["low"], errors="coerce").dropna()
    if highs.empty or lows.empty:
        raise ValueError("No valid high/low data")

    lower = Decimal(str(float(lows.quantile(support_quantile))))
    upper = Decimal(str(float(highs.quantile(resistance_quantile))))
    if lower <= 0 or upper <= lower:
        return RangeCandidate(Decimal("0"), Decimal("0"), 0.0, Decimal("0"), False, "INVALID_RANGE", 0.0)

    width = upper / lower - Decimal("1")
    min_w = D(min_width_pct)
    max_w = D(max_width_pct)
    if width < min_w or width > max_w:
        return RangeCandidate(lower, upper, 0.0, width, False, "RANGE_WIDTH_OUTSIDE_LIMIT", 0.0)

    last = df.iloc[-1]
    price = float(last["close"])
    lo_f, hi_f = float(lower), float(upper)
    if hi_f <= lo_f:
        return RangeCandidate(lower, upper, 0.0, width, False, "INVALID_RANGE", 0.0)

    position = (price - lo_f) / (hi_f - lo_f)
    position_score = 100.0 if 0.10 <= position <= 0.90 else _clamp(100 - abs(position - 0.5) * 200)

    adx_value = float(last["adx"])
    atr_value = float(last["atr_pct"])
    bb_value = float(last["bb_width"])
    vol_value = float(last["volume_ratio"])

    width_mid = (float(min_w) + float(max_w)) / 2
    width_score = _clamp(100 - abs(float(width) - width_mid) / max(width_mid, 1e-9) * 100)

    adx_score = _score_low_is_good(adx_value, 28.0, 12.0)
    atr_score = _score_low_is_good(atr_value, 0.025, 0.015)
    bb_score = _score_low_is_good(bb_value, 0.06, 0.04)
    volume_score = _score_low_is_good(vol_value, 2.5, 1.5)

    quality = round(
        0.25 * width_score
        + 0.25 * adx_score
        + 0.20 * atr_score
        + 0.15 * bb_score
        + 0.10 * volume_score
        + 0.05 * position_score,
        2,
    )

    inside = lo_f <= price <= hi_f
    approved = quality >= float(min_quality_score)
    reason = "AUTO_RANGE_APPROVED"

    if require_price_inside and not inside:
        approved = False
        reason = "CURRENT_PRICE_OUTSIDE_RANGE"
    elif not approved:
        reason = "RANGE_QUALITY_TOO_LOW"

    return RangeCandidate(
        lower=lower,
        upper=upper,
        quality=quality,
        width_pct=width,
        approved=approved,
        reason=reason,
        position_in_range=round(position, 4),
    )
