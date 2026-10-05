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


@dataclass(frozen=True)
class ExchangeFilters:
    tick_size: float
    step_size: float
    min_notional: float
    min_qty: float = 0.0
    # Additional filters from Binance (Phase 4 hardening)
    max_price: Optional[float] = None
    max_qty: Optional[float] = None
    # NOTIONAL filter (Binance exchangeInfo NOTIONAL / MIN_NOTIONAL):
    # min_notional is required and always enforced.
    # max_notional is enforced for LIMIT_MAKER orders when present.
    # apply_min_to_market / apply_max_to_market flags from exchangeInfo
    # indicate whether the filter applies to MARKET orders only —
    # for LIMIT_MAKER we enforce both min and max regardless of these flags.
    max_notional: Optional[float] = None
    apply_min_to_market: Optional[bool] = None
    apply_max_to_market: Optional[bool] = None
    # PERCENT_PRICE_BY_SIDE (parsed and enforced when the exchange provides
    # the filter; None = no percent-price constraint for this symbol).
    # BUY prices must stay within [ref*bid_multiplier_down, ref*bid_multiplier_up],
    # SELL prices within [ref*ask_multiplier_down, ref*ask_multiplier_up],
    # where ref is the exchange's weighted-average price over avg_price_mins.
    bid_multiplier_up: Optional[float] = None
    bid_multiplier_down: Optional[float] = None
    ask_multiplier_up: Optional[float] = None
    ask_multiplier_down: Optional[float] = None
    avg_price_mins: Optional[int] = None


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
    dropped_levels: int = 0   # levels dropped for PERCENT_PRICE_BY_SIDE
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


def price_band(
    filters: ExchangeFilters, side: str, reference_price: Optional[float]
) -> Optional[tuple]:
    """Effective allowed price band for one side from PERCENT_PRICE_BY_SIDE
    and the exchange's weighted-average reference price. None when the
    filter is absent or no reference price is available."""
    if reference_price is None or reference_price <= 0:
        return None
    if side == "BUY":
        lo, hi = filters.bid_multiplier_down, filters.bid_multiplier_up
    elif side == "SELL":
        lo, hi = filters.ask_multiplier_down, filters.ask_multiplier_up
    else:
        return None
    if lo is None or hi is None or lo <= 0 or hi <= 0:
        return None
    return (reference_price * lo, reference_price * hi)


def validate_price(
    filters: ExchangeFilters, side: str, price: float, reference_price: Optional[float]
) -> Optional[str]:
    """Deterministic PERCENT_PRICE_BY_SIDE check. Returns the exact violated
    condition, or None when the price is inside the allowed band (a missing
    filter or reference means no constraint can be evaluated)."""
    band = price_band(filters, side, reference_price)
    if band is None:
        return None
    lo, hi = band
    tol = max(1e-12, abs(reference_price) * 1e-9)
    if price < lo - tol:
        return (
            f"price {price} below {side} minimum {lo:.10g} "
            f"(reference {reference_price:.10g} x bid/ask multiplier down)"
        )
    if price > hi + tol:
        return (
            f"price {price} above {side} maximum {hi:.10g} "
            f"(reference {reference_price:.10g} x bid/ask multiplier up)"
        )
    return None


def _blocked(symbol: str, mode: str, step: float, reason: str) -> GridPlan:
    return GridPlan(symbol=symbol, mode=mode, step=step, executable=False, block_reason=reason)


def build_grid(
    symbol: str,
    mode: str,
    price: Optional[float],
    atr_value: Optional[float],
    filters: ExchangeFilters,
    cfg,
    reference_price: Optional[float] = None,
) -> GridPlan:
    """Build the grid plan. `reference_price` is the exchange's weighted-
    average price (PERCENT_PRICE_BY_SIDE reference); when the symbol
    carries the filter it is REQUIRED and levels outside the BUY band are
    dropped (or the grid is blocked when no level can be placed).

    Enforces configured LOWER_PRICE / UPPER_PRICE bounds and TOTAL_GRIDS limit.
    """
    if mode not in ("arithmetic", "geometric"):
        return _blocked(symbol, mode, 0.0, "invalid_mode")
    if price is None or price <= 0 or atr_value is None or atr_value <= 0:
        return _blocked(symbol, mode, 0.0, "insufficient_data")
    if filters.tick_size <= 0 or filters.step_size <= 0:
        return _blocked(symbol, mode, 0.0, "invalid_filters")
    if filters.bid_multiplier_down is not None and reference_price is None:
        # The exchange enforces PERCENT_PRICE_BY_SIDE against its own
        # weighted-average price; refusing to place unvalidated orders.
        return _blocked(symbol, mode, 0.0, "reference_price_unavailable")

    # Hard configured range bounds (Phase 3).
    lower_bound = cfg.lower_price.get(symbol) if hasattr(cfg, "lower_price") else None
    upper_bound = cfg.upper_price.get(symbol) if hasattr(cfg, "upper_price") else None

    # Current market price must be inside the configured range.
    if lower_bound is not None and price < lower_bound:
        return _blocked(symbol, mode, 0.0, "current_price_below_lower_bound")
    if upper_bound is not None and price > upper_bound:
        return _blocked(symbol, mode, 0.0, "current_price_above_upper_bound")

    step = atr_value * cfg.grid_step_atr_multiplier
    ratio: Optional[Decimal] = None
    if mode == "geometric":
        ratio = _dec(step) / _dec(price)
        if ratio <= 0:
            return _blocked(symbol, mode, step, "no_valid_levels")

    # TOTAL_GRIDS is the authoritative production limit (Phase 1).
    # Config validation enforces TOTAL_GRIDS as mandatory; no fallback.
    total_grids = cfg.total_grids

    levels: List[GridLevel] = []
    dropped_levels = 0
    for k in range(1, total_grids + 1):
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

        # Enforce configured LOWER_PRICE: buy_price >= LOWER_PRICE.
        if lower_bound is not None and buy_price < lower_bound:
            # Deeper levels only get lower, so we can stop.
            break
        # Enforce configured UPPER_PRICE: buy_price < UPPER_PRICE.
        if upper_bound is not None and buy_price >= upper_bound:
            dropped_levels += 1
            continue

        if mode == "geometric":
            raw_sell = buy_price * (1.0 + float(ratio))
        else:
            raw_sell = buy_price + step
        sell_price = quantize_price_ceil(raw_sell, filters.tick_size)
        if sell_price <= buy_price:
            break

        # Enforce configured UPPER_PRICE: sell_price <= UPPER_PRICE.
        if upper_bound is not None and sell_price > upper_bound:
            dropped_levels += 1
            continue
        # Enforce configured LOWER_PRICE: sell_price > LOWER_PRICE.
        if lower_bound is not None and sell_price <= lower_bound:
            dropped_levels += 1
            continue

        # PERCENT_PRICE_BY_SIDE: a buy level outside the exchange's allowed
        # band would be rejected outright — deeper levels only get lower, so
        # drop it (and never place an order Binance will refuse).
        violation = validate_price(filters, "BUY", buy_price, reference_price)
        if violation is not None:
            dropped_levels += 1
            continue
        # SELL side PERCENT_PRICE_BY_SIDE validation.
        violation = validate_price(filters, "SELL", sell_price, reference_price)
        if violation is not None:
            dropped_levels += 1
            continue

        # Smallest compliant quantity: >= minNotional and >= minQty.
        qty = quantize_qty_ceil(filters.min_notional / buy_price, filters.step_size)
        if filters.min_qty > 0 and qty < filters.min_qty:
            qty = quantize_qty_ceil(filters.min_qty, filters.step_size)
        if qty <= 0:
            break
        # Guard against float drift: notional must really satisfy the filter.
        while qty * buy_price < filters.min_notional:
            qty = quantize_qty_ceil(qty + filters.step_size, filters.step_size)

        # Phase 4: Complete Binance filter validation after quantization.
        # maxPrice: both buy and sell must not exceed maxPrice.
        if filters.max_price is not None:
            if buy_price > filters.max_price:
                dropped_levels += 1
                continue
            if sell_price > filters.max_price:
                dropped_levels += 1
                continue
        # maxQty: quantity must not exceed maxQty.
        if filters.max_qty is not None and qty > filters.max_qty:
            dropped_levels += 1
            continue
        # NOTIONAL filter: enforce min and max for LIMIT_MAKER orders.
        # min_notional is always enforced (above during qty selection).
        # max_notional: enforce for LIMIT_MAKER when present, regardless of
        # apply_max_to_market flag (which indicates MARKET-only applicability).
        if filters.max_notional is not None:
            buy_notional = buy_price * qty
            sell_notional = sell_price * qty
            if buy_notional > filters.max_notional or sell_notional > filters.max_notional:
                dropped_levels += 1
                continue

        exec_gross = (sell_price - buy_price) / buy_price
        exec_net = net_profit_pct(exec_gross, cfg.maker_fee, cfg.taker_fee, cfg.slippage_estimate)
        levels.append(GridLevel(k, buy_price, sell_price, qty, exec_gross, exec_net))

    if not levels:
        return _blocked(
            symbol, mode, step,
            "percent_price_band" if dropped_levels else "no_valid_levels",
        )

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
        dropped_levels=dropped_levels,
        executable=True,
        block_reason=None,
    )
