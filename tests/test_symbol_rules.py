from decimal import Decimal
import pytest
from symbol_rules import (
    floor_to_step, parse_symbol_info, quantize_price, quantize_quantity,
    validate_notional, validate_percent_price, SymbolRuleError
)

def sample():
    return {
        "symbol":"BNBUSDT","baseAsset":"BNB","quoteAsset":"USDT","status":"TRADING",
        "filters":[
            {"filterType":"PRICE_FILTER","minPrice":"0.01","maxPrice":"100000","tickSize":"0.01"},
            {"filterType":"LOT_SIZE","minQty":"0.001","maxQty":"10000","stepSize":"0.001"},
            {"filterType":"MARKET_LOT_SIZE","minQty":"0.001","maxQty":"5000","stepSize":"0.001"},
            {"filterType":"MIN_NOTIONAL","minNotional":"5"},
            {"filterType":"NOTIONAL","minNotional":"10","maxNotional":"100000"},
            {"filterType":"PERCENT_PRICE","multiplierUp":"1.05","multiplierDown":"0.95","avgPriceMins":5},
            {"filterType":"MAX_NUM_ORDERS","maxNumOrders":40},
        ],
    }

def test_parse_and_round():
    rules=parse_symbol_info(sample())
    assert rules.tick_size == Decimal("0.01")
    assert rules.min_notional == Decimal("10")
    assert rules.max_num_orders == 40
    assert quantize_price("650.129",rules) == Decimal("650.12")
    assert quantize_quantity("1.2349",rules) == Decimal("1.234")

def test_notional_guard_enforces_both_min_constraints():
    rules=parse_symbol_info(sample())
    with pytest.raises(SymbolRuleError):
        validate_notional("50","0.1",rules)

def test_percent_price_guard():
    rules=parse_symbol_info(sample())
    validate_percent_price("104","100","BUY",rules)
    with pytest.raises(SymbolRuleError):
        validate_percent_price("106","100","BUY",rules)

def test_floor():
    assert floor_to_step("1.999",Decimal("0.01")) == Decimal("1.99")
