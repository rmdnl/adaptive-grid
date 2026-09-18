from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any


def D(value) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    max_notional: Decimal


class SymbolRuleError(ValueError):
    pass


def _filter(filters: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    for item in filters:
        if item.get("filterType") == kind:
            return item
    return {}


def parse_symbol_info(info: dict[str, Any]) -> SymbolRules:
    filters = info.get("filters", [])
    price = _filter(filters, "PRICE_FILTER")
    lot = _filter(filters, "LOT_SIZE")
    notional = _filter(filters, "NOTIONAL") or _filter(filters, "MIN_NOTIONAL")

    return SymbolRules(
        symbol=str(info["symbol"]).upper(),
        base_asset=str(info["baseAsset"]),
        quote_asset=str(info["quoteAsset"]),
        status=str(info.get("status", "")),
        tick_size=D(price.get("tickSize", "0")),
        min_price=D(price.get("minPrice", "0")),
        max_price=D(price.get("maxPrice", "0")),
        step_size=D(lot.get("stepSize", "0")),
        min_qty=D(lot.get("minQty", "0")),
        max_qty=D(lot.get("maxQty", "0")),
        min_notional=D(
            notional.get("minNotional", "0")
        ),
        max_notional=D(
            notional.get("maxNotional", "0")
        ),
    )


def floor_to_step(value, step: Decimal) -> Decimal:
    value = D(value)
    if step <= 0:
        return value
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def quantize_price(price, rules: SymbolRules) -> Decimal:
    p = floor_to_step(price, rules.tick_size)
    if rules.min_price and p < rules.min_price:
        raise SymbolRuleError("Price below minPrice")
    if rules.max_price and p > rules.max_price:
        raise SymbolRuleError("Price above maxPrice")
    return p


def quantize_quantity(quantity, rules: SymbolRules) -> Decimal:
    q = floor_to_step(quantity, rules.step_size)
    if rules.min_qty and q < rules.min_qty:
        raise SymbolRuleError("Quantity below minQty")
    if rules.max_qty and q > rules.max_qty:
        raise SymbolRuleError("Quantity above maxQty")
    return q


def validate_notional(price, quantity, rules: SymbolRules) -> Decimal:
    notional = D(price) * D(quantity)
    if rules.min_notional and notional < rules.min_notional:
        raise SymbolRuleError("Notional below minimum")
    if rules.max_notional and notional > rules.max_notional:
        raise SymbolRuleError("Notional above maximum")
    return notional
