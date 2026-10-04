"""Grid Engine — Arithmetic & Geometric grid building with ATR-based dynamic step."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import Iterable, Literal

from profit_model import net_pct_from_prices, passes

getcontext().prec = 40


def D(value) -> Decimal:
    return Decimal(str(value))


GridMode = Literal["arithmetic", "geometric"]


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


@dataclass(frozen=True)
class GridBuildResult:
    """Result of grid building with all metadata."""
    levels: tuple[GridLevel, ...]
    effective_upper: Decimal
    mode: GridMode
    step_pct: Decimal
    cells: int


def build_arithmetic_grid(lower: Decimal, upper: Decimal, step_pct: Decimal, min_cells: int = 1, max_levels: int = 200) -> tuple[list[GridLevel], Decimal]:
    """Build arithmetic (fixed-step) grid levels.
    
    Each level is spaced by a fixed percentage step from the previous.
    price[i] = lower * (1 + step_pct * i)
    """
    lo, hi, step = D(lower), D(upper), D(step_pct)
    if lo <= 0 or hi <= lo or step <= 0:
        raise ValueError("Invalid range or step")
    if min_cells < 1:
        raise ValueError("min_cells must be >= 1")
    if max_levels <= min_cells:
        raise ValueError("max_levels must be > min_cells")

    levels = []
    i = 0
    while True:
        price = lo * (D("1") + step * D(str(i)))
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


def build_geometric_grid(lower: Decimal, upper: Decimal, step_pct: Decimal, min_cells: int = 1, max_levels: int = 200) -> tuple[list[GridLevel], Decimal]:
    """Build geometric (compounding-step) grid levels.
    
    Each level is spaced by a multiplicative factor from the previous.
    price[i] = lower * (1 + step_pct) ** i
    """
    lo, hi, step = D(lower), D(upper), D(step_pct)
    if lo <= 0 or hi <= lo or step <= 0:
        raise ValueError("Invalid range or step")
    if min_cells < 1:
        raise ValueError("min_cells must be >= 1")
    if max_levels <= min_cells:
        raise ValueError("max_levels must be > min_cells")

    factor = D("1") + step
    levels = []
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


def build_grid(
    lower: Decimal,
    upper: Decimal,
    step_pct: Decimal,
    mode: GridMode,
    min_cells: int = 1,
    max_levels: int = 200,
) -> GridBuildResult:
    """Build grid with specified mode (arithmetic or geometric)."""
    if mode == "arithmetic":
        levels, effective_upper = build_arithmetic_grid(lower, upper, step_pct, min_cells, max_levels)
    else:
        levels, effective_upper = build_geometric_grid(lower, upper, step_pct, min_cells, max_levels)
    
    return GridBuildResult(
        levels=tuple(levels),
        effective_upper=effective_upper,
        mode=mode,
        step_pct=step_pct,
        cells=len(levels) - 1,
    )


def atr_grid_step_pct(atr_pct: Decimal, multiplier: Decimal = Decimal("1.0")) -> Decimal:
    """Locked grid step = GRID_STEP_ATR_MULTIPLIER x ATR(14) of price.

    The multiplier defaults to 1.0 (locked specification).  The locked
    specification forbids substituting a fixed percentage step and forbids
    silently capping ATR: when the ATR step produces an economically invalid
    grid (gross < 0.5% or executable net < 0.3%), the callers must BLOCK the
    grid and record the reason — never widen it artificially.

    Fail-closed: a non-finite or non-positive ATR raises ValueError so the
    caller blocks with an explicit reason instead of deploying a bogus grid.
    """
    value = D(atr_pct) * D(multiplier)
    if not value.is_finite() or value <= 0:
        raise ValueError(f"ATR percentage is invalid: {atr_pct!r}")
    return value


def validate_grid_profit(
    levels: Iterable[GridLevel],
    buy_fee: Decimal,
    sell_fee: Decimal,
    roundtrip_slippage: Decimal,
    hard_min: Decimal,
) -> GridValidation:
    """Validate that all grid cells meet minimum net profit requirement."""
    level_list = list(levels)
    if len(level_list) < 2:
        return GridValidation(False, "LESS_THAN_ONE_CELL", D("0"), 0)
    nets = [
        net_pct_from_prices(a.price, b.price, buy_fee, sell_fee, roundtrip_slippage)
        for a, b in zip(level_list[:-1], level_list[1:])
    ]
    minimum = min(nets)
    allowed = passes(minimum, hard_min)
    return GridValidation(
        allowed, "NET_PROFIT_PASS" if allowed else "NET_PROFIT_BELOW_HARD_MIN",
        minimum, len(level_list) - 1
    )


def range_break(lower: Decimal, upper: Decimal, price: Decimal, buffer_pct: Decimal) -> bool:
    """Check if price has broken outside range with buffer."""
    lo, hi, px, buf = map(D, (lower, upper, price, buffer_pct))
    return px < lo * (D("1") - buf) or px > hi * (D("1") + buf)


def inside_range(lower: Decimal, upper: Decimal, price: Decimal) -> bool:
    """Check if price is inside range (inclusive)."""
    lo, hi, px = map(D, (lower, upper, price))
    return lo <= px <= hi