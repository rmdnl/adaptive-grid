from dataclasses import dataclass
import numpy as np
import pandas as pd

@dataclass(frozen=True)
class RangeCandidate:
    lower: float
    upper: float
    quality: float
    width_pct: float
    reason: str

def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, float(x)))

def auto_range(df: pd.DataFrame, support_quantile=0.10,
               resistance_quantile=0.90, min_width_pct=0.03,
               max_width_pct=0.25, min_quality_score=65):
    if len(df) < 30:
        raise ValueError("Not enough candles for auto-range")

    closes = df["close"].astype(float)
    lower = float(closes.quantile(support_quantile))
    upper = float(closes.quantile(resistance_quantile))
    if lower <= 0 or upper <= lower:
        return RangeCandidate(0, 0, 0, 0, "INVALID_RANGE")

    width = upper / lower - 1
    width_score = 100 if min_width_pct <= width <= max_width_pct else 0
    if width_score == 0:
        return RangeCandidate(lower, upper, 0, width, "RANGE_WIDTH_OUTSIDE_LIMIT")

    last = df.iloc[-1]
    adx_score = 100 if pd.notna(last.get("adx")) else 50
    if pd.notna(last.get("adx")):
        adx_score = _clamp((30 - float(last["adx"])) / 15 * 100)

    atr_score = 100 if pd.isna(last.get("atr_pct")) else _clamp((0.03 - float(last["atr_pct"])) / 0.02 * 100)
    bb_score = 100 if pd.isna(last.get("bb_width")) else _clamp((0.08 - float(last["bb_width"])) / 0.06 * 100)
    volume_score = 100 if pd.isna(last.get("volume_ratio")) else _clamp((2.5 - float(last["volume_ratio"])) / 1.5 * 100)

    quality = 0.35*width_score + 0.25*adx_score + 0.20*atr_score + 0.15*bb_score + 0.05*volume_score
    quality = round(_clamp(quality), 2)
    reason = "AUTO_RANGE_APPROVED" if quality >= min_quality_score else "RANGE_QUALITY_TOO_LOW"
    return RangeCandidate(lower, upper, quality, width, reason)
