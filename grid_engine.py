from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import Iterable

from profit_model import net_pct_from_prices, passes

getcontext().prec = 40


def D(value) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True)
class GridLevel:
    index: int
    price: Decimal


@dataclass(frozen=True)
class GridValidation:
    allowed: bool
    reason: str
    min_net_pct: Decimal
    cells: int


def build_geometric_grid(
    lower,
    upper,
    step_pct,
    min_cells: int = 1,
    max_levels: int = 200,
) -> tuple[list[GridLevel], Decimal]:
    lo = D(lower)
    hi = D(upper)
    step = D(step_pct)

    if lo <= 0 or hi <= lo or step <= 0:
        raise ValueError("Invalid range or step")
    if min_cells < 1:
        raise ValueError("min_cells must be >= 1")
    if max_levels <= min_cells:
        raise ValueError("max_levels must be > min_cells")

    factor = Decimal("1") + step
    levels: list[GridLevel] = []
    i = 0
    while True:
        price = lo * (factor ** i)
        if price > hi:
            break
        levels.append(GridLevel(i, price))
        i += 1
        if i >= max_levels:
            break

    if len(levels) < 2:
        raise ValueError("Configured range does not produce at least one grid cell")
    if len(levels) - 1 < min_cells:
        raise ValueError(
            f"Range produces only {len(levels)-1} grid cells; minimum is {min_cells}"
        )

    return levels, levels[-1].price


def validate_grid_profit(
    levels: Iterable[GridLevel],
    buy_fee,
    sell_fee,
    roundtrip_slippage,
    hard_min,
) -> GridValidation:
    level_list = list(levels)
    if len(level_list) < 2:
        return GridValidation(False, "LESS_THAN_ONE_CELL", D("0"), 0)

    nets = [
        net_pct_from_prices(
            a.price,
            b.price,
            D(buy_fee),
            D(sell_fee),
            D(roundtrip_slippage),
        )
        for a, b in zip(level_list[:-1], level_list[1:])
    ]
    minimum = min(nets)
    allowed = passes(minimum, hard_min)
    return GridValidation(
        allowed=allowed,
        reason="NET_PROFIT_PASS" if allowed else "NET_PROFIT_BELOW_HARD_MIN",
        min_net_pct=minimum,
        cells=len(level_list) - 1,
    )


def range_break(lower, upper, price, buffer_pct) -> bool:
    lo, hi, px, buf = map(D, (lower, upper, price, buffer_pct))
    return px < lo * (D("1") - buf) or px > hi * (D("1") + buf)


def inside_range(lower, upper, price) -> bool:
    lo, hi, px = map(D, (lower, upper, price))
    return lo <= px <= hi
