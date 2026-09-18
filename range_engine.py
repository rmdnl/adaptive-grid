from dataclasses import dataclass
import numpy as np

@dataclass
class RangeCandidate:
    lower: float
    upper: float
    quality: float
    reason: str

def auto_range(prices, min_width_pct=0.03, max_width_pct=0.25):
    if len(prices) < 20:
        raise ValueError("Not enough prices for auto-range")

    arr = np.asarray(prices, dtype=float)
    lower = float(np.quantile(arr, 0.10))
    upper = float(np.quantile(arr, 0.90))
    width = (upper/lower) - 1

    if width < min_width_pct or width > max_width_pct:
        return RangeCandidate(lower, upper, 0.0, "RANGE_WIDTH_OUTSIDE_LIMIT")

    # Baseline quality score. Production analyzer can add ADX/ATR/BB/orderbook inputs.
    quality = 70.0
    return RangeCandidate(lower, upper, quality, "AUTO_RANGE_CANDIDATE")
