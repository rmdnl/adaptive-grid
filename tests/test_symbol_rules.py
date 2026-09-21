from decimal import Decimal
import pytest
from symbol_rules import (
    floor_to_step, parse_symbol_info, quantize_price, quantize_quantity,
    validate_notional, validate_percent_price, validate_quantized_order_plan,
    SymbolRuleError,
)
from grid_engine import GridLevel

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

def test_quantized_order_plan_applies_price_lot_notional_and_profit_rules():
    rules = parse_symbol_info(sample())
    levels = [GridLevel(0, Decimal("100.009")), GridLevel(1, Decimal("100.609"))]

    plan = validate_quantized_order_plan(
        levels, rules, "25", "100", "0.001", "0.001", "0.0005", "0.003", 40
    )

    assert plan.allowed
    cell = plan.cells[0]
    assert cell.buy_price == Decimal("100.00")
    assert cell.sell_price == Decimal("100.60")
    assert cell.quantity == Decimal("0.250")
    assert cell.gross_pct == Decimal("0.006")
    assert cell.net_pct > Decimal("0.003")
    assert plan.effective_upper == Decimal("100.60")

def test_quantized_order_plan_rejects_min_notional_and_notional_maximum():
    rules = parse_symbol_info(sample())
    levels = [GridLevel(0, Decimal("100")), GridLevel(1, Decimal("100.60"))]

    too_small = validate_quantized_order_plan(
        levels, rules, "5", "100", "0.001", "0.001", "0.0005", "0.003", 40
    )
    assert not too_small.allowed
    assert "Notional below minimum" in too_small.reason

    too_large = validate_quantized_order_plan(
        levels, rules, "100001", "100", "0.001", "0.001", "0.0005", "0.003", 40
    )
    assert not too_large.allowed
    assert "Notional above maximum" in too_large.reason

def test_quantized_order_plan_rejects_percent_price_and_order_limit():
    rules = parse_symbol_info(sample())
    levels = [
        GridLevel(0, Decimal("100")),
        GridLevel(1, Decimal("100.60")),
        GridLevel(2, Decimal("101.21")),
    ]

    percent_block = validate_quantized_order_plan(
        levels, rules, "25", "90", "0.001", "0.001", "0.0005", "0.003", 40
    )
    assert not percent_block.allowed
    assert "PERCENT_PRICE" in percent_block.reason

    limit_block = validate_quantized_order_plan(
        levels, rules, "25", "100", "0.001", "0.001", "0.0005", "0.003", 1
    )
    assert not limit_block.allowed
    assert limit_block.reason == "MAX_OPEN_ORDERS_PLAN_EXCEEDED"

    exchange_limited = sample()
    exchange_limited["filters"] = [
        {"filterType": "MAX_NUM_ORDERS", "maxNumOrders": 1}
        if item["filterType"] == "MAX_NUM_ORDERS" else item
        for item in exchange_limited["filters"]
    ]
    exchange_limit_block = validate_quantized_order_plan(
        levels, parse_symbol_info(exchange_limited), "25", "100",
        "0.001", "0.001", "0.0005", "0.003", 40,
    )
    assert not exchange_limit_block.allowed
    assert exchange_limit_block.reason == "MAX_OPEN_ORDERS_PLAN_EXCEEDED"

def test_quantized_order_plan_applies_percent_price_by_side():
    payload = sample()
    payload["filters"].append({
        "filterType": "PERCENT_PRICE_BY_SIDE",
        "bidMultiplierUp": "1.001", "bidMultiplierDown": "0.95",
        "askMultiplierUp": "1.05", "askMultiplierDown": "0.95", "avgPriceMins": 5,
    })
    plan = validate_quantized_order_plan(
        [GridLevel(0, Decimal("100")), GridLevel(1, Decimal("100.60"))],
        parse_symbol_info(payload), "25", "99.90",
        "0.001", "0.001", "0.0005", "0.003", 40,
    )

    assert not plan.allowed
    assert "PERCENT_PRICE_BY_SIDE BUY" in plan.reason

def test_quantized_order_plan_blocks_cell_below_hard_min_after_rounding():
    rules = parse_symbol_info(sample())
    levels = [GridLevel(0, Decimal("100.009")), GridLevel(1, Decimal("100.309"))]

    plan = validate_quantized_order_plan(
        levels, rules, "25", "100", "0.001", "0.001", "0.0005", "0.003", 40
    )

    assert not plan.allowed
    assert plan.cells[0].gross_pct == Decimal("0.003")
    assert plan.cells[0].net_pct < Decimal("0.003")
    assert "NET_PROFIT_BELOW_HARD_MIN_AFTER_QUANTIZATION" in plan.reason
