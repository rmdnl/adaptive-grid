"""Grid construction and executable economics validation.

Grid step = GRID_STEP_ATR_MULTIPLIER x ATR. The EXECUTABLE economics —
computed after exchange tick-size / step-size / min-notional quantization,
with buy prices rounded DOWN and sell prices rounded UP (conservative) —
are authoritative. A grid that cannot clear the configured gross minimum
(0.50%) and net minimum (0.20%, after buy fee, sell fee and estimated
slippage on both sides) is BLOCKED, never widened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import List, Optional

GRID_LEVELS = 5  # buy levels placed below price; each filled buy spawns one sell


@dataclass(frozen=True)
class ExchangeFilters:
    tick_size: float
    step_size: float
    min_notional: float
    min_qty: float = 0.0


@dataclass(frozen=True)
class GridLevel:
    index: int
    buy_price: float
    sell_price: float
    qty: float
    gross_pct: float
    net_pct: float


@dataclass(frozen=True)
class GridPlan:
    symbol: str
    mode: str
    step: float
    levels: List[GridLevel] = field(default_factory=list)
    lower_price: Optional[float] = None
    gross_pct: float = 0.0   # worst-level executable gross (fraction)
    net_pct: float = 0.0     # worst-level executable net (fraction)
    executable: bool = False
    block_reason: Optional[str] = None


def _dec(value: float) -> Decimal:
    return Decimal(str(value))


def quantize_price_floor(price: float, tick: float) -> float:
    tick_dec = _dec(tick)
    return float((_dec(price) / tick_dec).to_integral_value(rounding=ROUND_FLOOR) * tick_dec)


def quantize_price_ceil(price: float, tick: float) -> float:
    tick_dec = _dec(tick)
    return float((_dec(price) / tick_dec).to_integral_value(rounding=ROUND_CEILING) * tick_dec)


def quantize_qty_floor(qty: float, step: float) -> float:
    step_dec = _dec(step)
    return float((_dec(qty) / step_dec).to_integral_value(rounding=ROUND_FLOOR) * step_dec)


def quantize_qty_ceil(qty: float, step: float) -> float:
    step_dec = _dec(step)
    return float((_dec(qty) / step_dec).to_integral_value(rounding=ROUND_CEILING) * step_dec)


def net_profit_pct(gross_pct: float, maker_fee: float, taker_fee: float, slippage: float) -> float:
    """Conservative net profit per completed grid as a fraction.

    Buy at B, sell at B*(1+gross). Both sides pay the worst fee rate
    (max of maker/taker) and the estimated slippage on notional. Sell
    notional is (1+gross) x buy notional, so the two-side cost scale is
    (2 + gross).
    """
    fee = max(maker_fee, taker_fee)
    return gross_pct - (fee + slippage) * (2.0 + gross_pct)


def _blocked(symbol: str, mode: str, step: float, reason: str) -> GridPlan:
    return GridPlan(symbol=symbol, mode=mode, step=step, executable=False, block_reason=reason)


def build_grid(
    symbol: str,
    mode: str,
    price: Optional[float],
    atr_value: Optional[float],
    filters: ExchangeFilters,
    cfg,
) -> GridPlan:
    if mode not in ("arithmetic", "geometric"):
        return _blocked(symbol, mode, 0.0, "invalid_mode")
    if price is None or price <= 0 or atr_value is None or atr_value <= 0:
        return _blocked(symbol, mode, 0.0, "insufficient_data")
    if filters.tick_size <= 0 or filters.step_size <= 0:
        return _blocked(symbol, mode, 0.0, "invalid_filters")

    step = atr_value * cfg.grid_step_atr_multiplier
    ratio: Optional[Decimal] = None
    if mode == "geometric":
        ratio = _dec(step) / _dec(price)
        if ratio <= 0:
            return _blocked(symbol, mode, step, "no_valid_levels")

    levels: List[GridLevel] = []
    for k in range(1, GRID_LEVELS + 1):
        # Raw levels are computed in Decimal to avoid float drift before
        # tick quantization.
        if mode == "geometric":
            raw_buy_dec = _dec(price) * (Decimal(1) - ratio) ** k
        else:
            raw_buy_dec = _dec(price) - k * _dec(step)
        if raw_buy_dec <= 0:
            break
        raw_buy = float(raw_buy_dec)
        buy_price = quantize_price_floor(raw_buy, filters.tick_size)
        if buy_price <= 0:
            break
        if mode == "geometric":
            raw_sell = buy_price * (1.0 + float(ratio))
        else:
            raw_sell = buy_price + step
        sell_price = quantize_price_ceil(raw_sell, filters.tick_size)
        if sell_price <= buy_price:
            break

        # Smallest compliant quantity: >= minNotional and >= minQty.
        qty = quantize_qty_ceil(filters.min_notional / buy_price, filters.step_size)
        if filters.min_qty > 0 and qty < filters.min_qty:
            qty = quantize_qty_ceil(filters.min_qty, filters.step_size)
        if qty <= 0:
            break
        # Guard against float drift: notional must really satisfy the filter.
        while qty * buy_price < filters.min_notional:
            qty = quantize_qty_ceil(qty + filters.step_size, filters.step_size)

        exec_gross = (sell_price - buy_price) / buy_price
        exec_net = net_profit_pct(exec_gross, cfg.maker_fee, cfg.taker_fee, cfg.slippage_estimate)
        levels.append(GridLevel(k, buy_price, sell_price, qty, exec_gross, exec_net))

    if not levels:
        return _blocked(symbol, mode, step, "no_valid_levels")

    # The worst (least profitable) executable level decides the gate.
    worst = min(levels, key=lambda lvl: lvl.net_pct)
    if worst.gross_pct < cfg.grid_gross_min:
        return _blocked(symbol, mode, step, "gross_below_minimum")
    if worst.net_pct < cfg.min_net_profit_per_grid:
        return _blocked(symbol, mode, step, "net_below_minimum")

    return GridPlan(
        symbol=symbol,
        mode=mode,
        step=step,
        levels=levels,
        lower_price=min(lvl.buy_price for lvl in levels),
        gross_pct=worst.gross_pct,
        net_pct=worst.net_pct,
        executable=True,
        block_reason=None,
    )
