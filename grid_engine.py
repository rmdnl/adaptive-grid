from dataclasses import dataclass
import math

@dataclass(frozen=True)
class GridLevel:
    index: int
    price: float

def build_geometric_grid(lower: float, upper: float, step_pct: float):
    if lower <= 0 or upper <= lower:
        raise ValueError("Invalid range")
    if step_pct <= 0:
        raise ValueError("step_pct must be positive")

    levels = []
    p = float(lower)
    i = 0
    factor = 1.0 + step_pct

    while p <= upper * (1 + 1e-12):
        levels.append(GridLevel(i, p))
        i += 1
        p = lower * (factor ** i)
        if i > 10000:
            raise RuntimeError("Grid level safety limit exceeded")

    effective_upper = levels[-1].price
    return levels, effective_upper

def expected_net_pct(step_pct, buy_fee, sell_fee, roundtrip_slippage):
    return (1 + step_pct) * (1 - buy_fee) * (1 - sell_fee) * (1 - roundtrip_slippage) - 1

def range_break(lower, upper, price, buffer_pct):
    return price < lower * (1 - buffer_pct) or price > upper * (1 + buffer_pct)
