from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math
import pandas as pd

def D(value):
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

def _clamp(value, lo=0.0, hi=100.0):
    return max(lo, min(hi, float(value)))

def _score_low_is_good(value, threshold, deadband):
    if not math.isfinite(value):
        return 0.0
    if value <= threshold:
        return 100.0
    if value >= threshold + deadband:
        return 0.0
    return _clamp((threshold + deadband - value) / deadband * 100.0)

def auto_range(df, support_quantile=0.10, resistance_quantile=0.90,
               min_width_pct=0.03, max_width_pct=0.25,
               min_quality_score=65, require_price_inside=True):
    if len(df) < 50:
        raise ValueError("Not enough closed candles for auto-range")
    required = {"high","low","close","adx","atr_pct","bb_width","volume_ratio"}
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
    if width < D(min_width_pct) or width > D(max_width_pct):
        return RangeCandidate(lower, upper, 0.0, width, False, "RANGE_WIDTH_OUTSIDE_LIMIT", 0.0)

    price = float(df.iloc[-1]["close"])
    lo_f, hi_f = float(lower), float(upper)
    position = (price-lo_f)/(hi_f-lo_f)
    position_score = 100.0 if 0.10 <= position <= 0.90 else _clamp(100-abs(position-0.5)*200)

    last = df.iloc[-1]
    width_mid = (float(min_width_pct)+float(max_width_pct))/2
    width_score = _clamp(100-abs(float(width)-width_mid)/max(width_mid,1e-9)*100)
    adx_score = _score_low_is_good(float(last["adx"]),28.0,12.0)
    atr_score = _score_low_is_good(float(last["atr_pct"]),0.025,0.015)
    bb_score = _score_low_is_good(float(last["bb_width"]),0.06,0.04)
    vol_score = _score_low_is_good(float(last["volume_ratio"]),2.5,1.5)
    quality = round(
        0.25*width_score + 0.25*adx_score + 0.20*atr_score +
        0.15*bb_score + 0.10*vol_score + 0.05*position_score, 2
    )

    inside = lo_f <= price <= hi_f
    approved = quality >= float(min_quality_score)
    reason = "AUTO_RANGE_APPROVED"
    if require_price_inside and not inside:
        approved, reason = False, "CURRENT_PRICE_OUTSIDE_RANGE"
    elif not approved:
        reason = "RANGE_QUALITY_TOO_LOW"

    return RangeCandidate(lower, upper, quality, width, approved, reason, round(position,4))
