from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

def D(value): return Decimal(str(value))

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
    market_step_size: Decimal
    market_min_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal
    max_notional: Decimal
    percent_multiplier_up: Decimal
    percent_multiplier_down: Decimal
    percent_avg_mins: int
    bid_multiplier_up: Decimal
    bid_multiplier_down: Decimal
    ask_multiplier_up: Decimal
    ask_multiplier_down: Decimal
    side_avg_mins: int
    max_num_orders: int
    max_num_algo_orders: int

class SymbolRuleError(ValueError): pass

def _filter(filters, kind):
    for item in filters:
        if item.get("filterType") == kind:
            return item
    return {}

def _dec(item, key, default="0"):
    value = item.get(key, default)
    return D(value)

def parse_symbol_info(info):
    filters = info.get("filters", [])
    price = _filter(filters,"PRICE_FILTER")
    lot = _filter(filters,"LOT_SIZE")
    market_lot = _filter(filters,"MARKET_LOT_SIZE")

    notional = _filter(filters,"NOTIONAL")
    min_notional = _filter(filters,"MIN_NOTIONAL")
    mins = [x for x in (_dec(notional,"minNotional"), _dec(min_notional,"minNotional")) if x > 0]
    maxs = [x for x in (_dec(notional,"maxNotional"),) if x > 0]
    min_notional_value = max(mins, default=Decimal("0"))
    max_notional_value = min(maxs, default=Decimal("0"))

    pp = _filter(filters,"PERCENT_PRICE")
    pps = _filter(filters,"PERCENT_PRICE_BY_SIDE")

    return SymbolRules(
        symbol=str(info["symbol"]).upper(),
        base_asset=str(info["baseAsset"]),
        quote_asset=str(info["quoteAsset"]),
        status=str(info.get("status","")),
        tick_size=_dec(price,"tickSize"),
        min_price=_dec(price,"minPrice"),
        max_price=_dec(price,"maxPrice"),
        step_size=_dec(lot,"stepSize"),
        min_qty=_dec(lot,"minQty"),
        max_qty=_dec(lot,"maxQty"),
        market_step_size=_dec(market_lot,"stepSize"),
        market_min_qty=_dec(market_lot,"minQty"),
        market_max_qty=_dec(market_lot,"maxQty"),
        min_notional=min_notional_value,
        max_notional=max_notional_value,
        percent_multiplier_up=_dec(pp,"multiplierUp","0"),
        percent_multiplier_down=_dec(pp,"multiplierDown","0"),
        percent_avg_mins=int(pp.get("avgPriceMins",0) or 0),
        bid_multiplier_up=_dec(pps,"bidMultiplierUp","0"),
        bid_multiplier_down=_dec(pps,"bidMultiplierDown","0"),
        ask_multiplier_up=_dec(pps,"askMultiplierUp","0"),
        ask_multiplier_down=_dec(pps,"askMultiplierDown","0"),
        side_avg_mins=int(pps.get("avgPriceMins",0) or 0),
        max_num_orders=int(_filter(filters,"MAX_NUM_ORDERS").get("maxNumOrders",0) or 0),
        max_num_algo_orders=int(_filter(filters,"MAX_NUM_ALGO_ORDERS").get("maxNumAlgoOrders",0) or 0),
    )

def floor_to_step(value, step):
    value, step = D(value), D(step)
    if step <= 0: return value
    return (value/step).to_integral_value(rounding=ROUND_DOWN)*step

def quantize_price(price, rules):
    p = floor_to_step(price, rules.tick_size)
    if rules.min_price and p < rules.min_price: raise SymbolRuleError("Price below minPrice")
    if rules.max_price and p > rules.max_price: raise SymbolRuleError("Price above maxPrice")
    return p

def quantize_quantity(quantity, rules):
    q = floor_to_step(quantity, rules.step_size)
    if rules.min_qty and q < rules.min_qty: raise SymbolRuleError("Quantity below minQty")
    if rules.max_qty and q > rules.max_qty: raise SymbolRuleError("Quantity above maxQty")
    return q

def validate_notional(price, quantity, rules):
    notional = D(price)*D(quantity)
    if rules.min_notional and notional < rules.min_notional:
        raise SymbolRuleError("Notional below minimum")
    if rules.max_notional and notional > rules.max_notional:
        raise SymbolRuleError("Notional above maximum")
    return notional

def validate_percent_price(price, weighted_avg_price, side, rules):
    px, avg = D(price), D(weighted_avg_price)
    side = str(side).upper()

    if rules.bid_multiplier_up and rules.bid_multiplier_down and side == "BUY":
        low = avg*rules.bid_multiplier_down
        high = avg*rules.bid_multiplier_up
        if not (low <= px <= high):
            raise SymbolRuleError("Price violates PERCENT_PRICE_BY_SIDE BUY")
        return
    if rules.ask_multiplier_up and rules.ask_multiplier_down and side == "SELL":
        low = avg*rules.ask_multiplier_down
        high = avg*rules.ask_multiplier_up
        if not (low <= px <= high):
            raise SymbolRuleError("Price violates PERCENT_PRICE_BY_SIDE SELL")
        return

    if rules.percent_multiplier_up and rules.percent_multiplier_down:
        low = avg*rules.percent_multiplier_down
        high = avg*rules.percent_multiplier_up
        if not (low <= px <= high):
            raise SymbolRuleError("Price violates PERCENT_PRICE")
