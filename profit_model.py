from __future__ import annotations

from decimal import Decimal, getcontext

getcontext().prec = 40


def D(value) -> Decimal:
    return Decimal(str(value))


def net_pct_from_prices(
    buy_price,
    sell_price,
    buy_fee,
    sell_fee,
    roundtrip_slippage,
) -> Decimal:
    buy = D(buy_price)
    sell = D(sell_price)
    if buy <= 0 or sell <= 0:
        raise ValueError("Prices must be positive")
    if buy_fee < 0 or sell_fee < 0 or roundtrip_slippage < 0:
        raise ValueError("Fees/slippage cannot be negative")

    gross_multiplier = sell / buy
    return (
        gross_multiplier
        * (Decimal("1") - D(buy_fee))
        * (Decimal("1") - D(sell_fee))
        * (Decimal("1") - D(roundtrip_slippage))
        - Decimal("1")
    )


def net_pct_from_step(step_pct, buy_fee, sell_fee, roundtrip_slippage) -> Decimal:
    step = D(step_pct)
    if step <= 0:
        raise ValueError("step_pct must be > 0")
    return net_pct_from_prices(
        Decimal("1"),
        Decimal("1") + step,
        D(buy_fee),
        D(sell_fee),
        D(roundtrip_slippage),
    )


def passes(net_value, hard_min) -> bool:
    return D(net_value) >= D(hard_min)


def profit_class(net_value, hard_min, preferred_max) -> str:
    n = D(net_value)
    lo = D(hard_min)
    hi = D(preferred_max)
    if n < lo:
        return "BLOCK"
    if n <= hi:
        return "PREFERRED"
    return "PASS_ABOVE_TARGET"
