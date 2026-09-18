from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

@dataclass(frozen=True)
class GridLevel:
    index: int
    price: float

def build_geometric_grid(lower: float, upper: float, step_pct: float):
    if lower <= 0 or upper <= lower or step_pct <= 0:
        raise ValueError("Invalid range or step")
    factor = 1.0 + step_pct
    levels = []
    i = 0
    while True:
        p = lower * (factor ** i)
        if p > upper * (1 + 1e-12):
            break
        levels.append(GridLevel(i, p))
        i += 1
        if i > 10000:
            raise RuntimeError("Grid level safety limit exceeded")
    return levels, levels[-1].price

def expected_net_pct(step_pct, buy_fee, sell_fee, roundtrip_slippage):
    return (1 + step_pct) * (1-buy_fee) * (1-sell_fee) * (1-roundtrip_slippage) - 1

def range_break(lower, upper, price, buffer_pct):
    return price < lower*(1-buffer_pct) or price > upper*(1+buffer_pct)

def inside_range(lower, upper, price):
    return lower <= price <= upper
