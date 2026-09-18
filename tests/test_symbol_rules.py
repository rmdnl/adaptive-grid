from decimal import Decimal
import pytest

from symbol_rules import (
    floor_to_step,
    parse_symbol_info,
    quantize_price,
    quantize_quantity,
    validate_notional,
    SymbolRuleError,
)


def sample():
    return {
        "symbol": "BNBUSDT",
        "baseAsset": "BNB",
        "quoteAsset": "USDT",
        "status": "TRADING",
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0.01000000", "maxPrice": "100000.00000000", "tickSize": "0.01000000"},
            {"filterType": "LOT_SIZE", "minQty": "0.00100000", "maxQty": "10000.00000000", "stepSize": "0.00100000"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "5.00000000"},
        ],
    }


def test_parse_and_round():
    rules = parse_symbol_info(sample())
    assert rules.tick_size == Decimal("0.01")
    assert quantize_price("650.129", rules) == Decimal("650.12")
    assert quantize_quantity("1.2349", rules) == Decimal("1.234")


def test_notional_guard():
    rules = parse_symbol_info(sample())
    with pytest.raises(SymbolRuleError):
        validate_notional("10", "0.1", rules)


def test_floor():
    assert floor_to_step("1.999", Decimal("0.01")) == Decimal("1.99")
