from decimal import Decimal
import pytest
from fee_model import effective_fees, EffectiveFees


def test_conservative_fee_sums_standard_special_tax():
    payload = {
        "standardCommission": {"maker": "0.0010", "taker": "0.0012"},
        "specialCommission": {"maker": "0.0001", "taker": "0.0001"},
        "taxCommission": {"maker": "0.0000", "taker": "0.0002"},
        "discount": {"enabledForAccount": True, "discount": "0.25"},
    }
    fees = effective_fees(payload, 0.009, 0.009)
    assert fees.maker == Decimal("0.0011")
    assert fees.taker == Decimal("0.0015")
    assert fees.source == "ACCOUNT_COMMISSION_CONSERVATIVE"


def test_incomplete_commission_uses_fallback():
    fees = effective_fees({"standardCommission": {"maker": "0.001", "taker": "0.001"}}, 0.001, 0.001)
    assert fees.source.startswith("FALLBACK:")


def test_none_payload_uses_fallback():
    fees = effective_fees(None, 0.001, 0.002)
    assert fees.maker == Decimal("0.001")
    assert fees.taker == Decimal("0.002")
    assert fees.source == "FALLBACK"


def test_empty_payload_uses_fallback():
    fees = effective_fees({}, 0.001, 0.002)
    assert fees.source == "FALLBACK"


def test_missing_standard_commission_uses_fallback():
    payload = {"specialCommission": {"maker": "0.0001", "taker": "0.0001"}}
    fees = effective_fees(payload, 0.001, 0.002)
    assert fees.source.startswith("FALLBACK:INCOMPLETE")


def test_missing_special_commission_uses_fallback():
    payload = {"standardCommission": {"maker": "0.001", "taker": "0.001"}}
    fees = effective_fees(payload, 0.001, 0.002)
    assert fees.source.startswith("FALLBACK:INCOMPLETE")


def test_missing_tax_commission_uses_fallback():
    payload = {
        "standardCommission": {"maker": "0.001", "taker": "0.001"},
        "specialCommission": {"maker": "0.0001", "taker": "0.0001"},
    }
    fees = effective_fees(payload, 0.001, 0.002)
    assert fees.source.startswith("FALLBACK:INCOMPLETE")


def test_partial_maker_in_standard_commission_uses_fallback():
    payload = {
        "standardCommission": {"maker": "0.001"},
        "specialCommission": {"maker": "0.0001", "taker": "0.0001"},
        "taxCommission": {"maker": "0.0000", "taker": "0.0002"},
    }
    fees = effective_fees(payload, 0.001, 0.002)
    assert fees.source.startswith("FALLBACK:INCOMPLETE")


def test_maker_commission_key_variants():
    payload = {
        "standardCommission": {"makerCommission": "0.001", "takerCommission": "0.001"},
        "specialCommission": {"maker": "0.0001", "taker": "0.0001"},
        "taxCommission": {"maker": "0.0000", "taker": "0.0002"},
    }
    fees = effective_fees(payload, 0.009, 0.009)
    assert fees.maker == Decimal("0.0011")
    assert fees.taker == Decimal("0.0013")
    assert fees.source == "ACCOUNT_COMMISSION_CONSERVATIVE"


def test_commission_values_as_list():
    payload = {
        "standardCommission": {"maker": ["0.001"], "taker": ["0.001"]},
        "specialCommission": {"maker": ["0.0001"], "taker": ["0.0001"]},
        "taxCommission": {"maker": ["0.0000"], "taker": ["0.0002"]},
    }
    fees = effective_fees(payload, 0.009, 0.009)
    assert fees.maker == Decimal("0.0011")
    assert fees.taker == Decimal("0.0013")


def test_invalid_commission_string_uses_fallback():
    payload = {
        "standardCommission": {"maker": "invalid", "taker": "0.001"},
        "specialCommission": {"maker": "0.0001", "taker": "0.0001"},
        "taxCommission": {"maker": "0.0000", "taker": "0.0002"},
    }
    fees = effective_fees(payload, 0.001, 0.002)
    assert fees.source.startswith("FALLBACK:INCOMPLETE")


def test_discount_not_applied_conservative():
    payload = {
        "standardCommission": {"maker": "0.001", "taker": "0.001"},
        "specialCommission": {"maker": "0.0001", "taker": "0.0001"},
        "taxCommission": {"maker": "0.0000", "taker": "0.0002"},
        "discount": {"enabledForAccount": True, "discount": "0.25"},
    }
    fees = effective_fees(payload, 0.009, 0.009)
    # Discount is NOT applied - conservative
    assert fees.maker == Decimal("0.0011")
    assert fees.taker == Decimal("0.0013")


def test_fallback_values_are_decimals():
    fees = effective_fees(None, "0.0015", "0.0025")
    assert isinstance(fees.maker, Decimal)
    assert isinstance(fees.taker, Decimal)
    assert fees.maker == Decimal("0.0015")
    assert fees.taker == Decimal("0.0025")


def test_effective_fees_immutable():
    fees = effective_fees(None, 0.001, 0.002)
    with pytest.raises(AttributeError):
        fees.maker = Decimal("0.999")


def test_zero_fallback_allowed():
    fees = effective_fees(None, 0, 0)
    assert fees.maker == Decimal("0")
    assert fees.taker == Decimal("0")


def test_all_commission_components_present_and_valid():
    payload = {
        "standardCommission": {"maker": "0.0005", "taker": "0.0006"},
        "specialCommission": {"maker": "0.0002", "taker": "0.0002"},
        "taxCommission": {"maker": "0.0001", "taker": "0.0003"},
    }
    fees = effective_fees(payload, 0.009, 0.009)
    assert fees.maker == Decimal("0.0008")
    assert fees.taker == Decimal("0.0011")
    assert fees.source == "ACCOUNT_COMMISSION_CONSERVATIVE"
