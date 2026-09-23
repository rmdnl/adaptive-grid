"""Comprehensive tests for fee_model.py — D, EffectiveFees, _rate, _sum_components, effective_fees."""
from __future__ import annotations

from decimal import Decimal

import pytest

from fee_model import D, EffectiveFees, effective_fees, _rate, _sum_components


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _full_payload(
    std_maker: str = "0.0010",
    std_taker: str = "0.0012",
    spc_maker: str = "0.0001",
    spc_taker: str = "0.0001",
    tax_maker: str = "0.0000",
    tax_taker: str = "0.0002",
    include_discount: bool = False,
    discount_enabled: bool = True,
    discount_value: str = "0.25",
) -> dict:
    """Build a complete commission payload."""
    payload = {
        "standardCommission": {"maker": std_maker, "taker": std_taker},
        "specialCommission": {"maker": spc_maker, "taker": spc_taker},
        "taxCommission": {"maker": tax_maker, "taker": tax_taker},
    }
    if include_discount:
        payload["discount"] = {"enabledForAccount": discount_enabled, "discount": discount_value}
    return payload


# ===========================================================================
# A. D() helper
# ===========================================================================
class TestD:
    def test_string(self):
        assert D("0.001") == Decimal("0.001")

    def test_int(self):
        assert D(5) == Decimal("5")

    def test_float(self):
        assert D(0.001) == Decimal("0.001")

    def test_zero(self):
        assert D(0) == Decimal("0")
        assert D("0") == Decimal("0")

    def test_negative(self):
        assert D(-0.001) == Decimal("-0.001")

    def test_decimal_passthrough(self):
        assert D(Decimal("0.001")) == Decimal("0.001")

    def test_large_number(self):
        assert D("999999999.99999999") == Decimal("999999999.99999999")

    def test_scientific_notation_string(self):
        assert D("1e-3") == Decimal("0.001")


# ===========================================================================
# B. EffectiveFees dataclass
# ===========================================================================
class TestEffectiveFees:
    def test_is_frozen(self):
        fees = EffectiveFees(
            maker=Decimal("0.001"), taker=Decimal("0.002"), source="FALLBACK"
        )
        with pytest.raises(AttributeError):
            fees.maker = Decimal("0.999")

    def test_all_fields_accessible(self):
        fees = EffectiveFees(
            maker=Decimal("0.001"), taker=Decimal("0.002"), source="TEST"
        )
        assert fees.maker == Decimal("0.001")
        assert fees.taker == Decimal("0.002")
        assert fees.source == "TEST"

    def test_equality(self):
        a = EffectiveFees(Decimal("1"), Decimal("2"), "SRC")
        b = EffectiveFees(Decimal("1"), Decimal("2"), "SRC")
        assert a == b

    def test_inequality_different_maker(self):
        a = EffectiveFees(Decimal("1"), Decimal("2"), "SRC")
        b = EffectiveFees(Decimal("3"), Decimal("2"), "SRC")
        assert a != b

    def test_inequality_different_source(self):
        a = EffectiveFees(Decimal("1"), Decimal("2"), "SRC_A")
        b = EffectiveFees(Decimal("1"), Decimal("2"), "SRC_B")
        assert a != b

    def test_taker_also_frozen(self):
        fees = EffectiveFees(Decimal("1"), Decimal("2"), "SRC")
        with pytest.raises(AttributeError):
            fees.taker = Decimal("3")

    def test_source_also_frozen(self):
        fees = EffectiveFees(Decimal("1"), Decimal("2"), "SRC")
        with pytest.raises(AttributeError):
            fees.source = "OTHER"


# ===========================================================================
# C. _rate() internal function
# ===========================================================================
class TestRate:
    def test_direct_key_match(self):
        node = {"maker": "0.001", "taker": "0.002"}
        assert _rate(node, "maker") == Decimal("0.001")
        assert _rate(node, "taker") == Decimal("0.002")

    def test_falls_back_to_makerCommission(self):
        node = {"makerCommission": "0.003"}
        assert _rate(node, "maker") == Decimal("0.003")

    def test_falls_back_to_takerCommission(self):
        node = {"takerCommission": "0.004"}
        assert _rate(node, "taker") == Decimal("0.004")

    def test_direct_key_takes_priority(self):
        """'maker' key should take precedence over 'makerCommission'."""
        node = {"maker": "0.001", "makerCommission": "0.002"}
        assert _rate(node, "maker") == Decimal("0.001")

    def test_non_dict_returns_none(self):
        assert _rate(None, "maker") is None
        assert _rate("string", "maker") is None
        assert _rate(42, "maker") is None
        assert _rate([], "maker") is None

    def test_missing_key_returns_none(self):
        assert _rate({}, "maker") is None

    def test_list_single_element(self):
        node = {"maker": ["0.005"]}
        assert _rate(node, "maker") == Decimal("0.005")

    def test_list_multiple_elements_uses_first(self):
        node = {"maker": ["0.005", "0.006", "0.007"]}
        assert _rate(node, "maker") == Decimal("0.005")

    def test_list_empty_returns_none(self):
        node = {"maker": []}
        assert _rate(node, "maker") is None

    def test_invalid_string_returns_none(self):
        node = {"maker": "not_a_number"}
        assert _rate(node, "maker") is None

    def test_none_value_in_dict(self):
        node = {"maker": None}
        # None → falls to key check → None → returns None
        assert _rate(node, "maker") is None

    def test_numeric_int_value(self):
        node = {"maker": 1}
        assert _rate(node, "maker") == Decimal("1")

    def test_numeric_float_value(self):
        node = {"maker": 0.001}
        assert _rate(node, "maker") == Decimal("0.001")

    def test_zero_value(self):
        node = {"maker": "0"}
        assert _rate(node, "maker") == Decimal("0")

    def test_negative_value(self):
        node = {"maker": "-0.001"}
        assert _rate(node, "maker") == Decimal("-0.001")


# ===========================================================================
# D. _sum_components() internal function
# ===========================================================================
class TestSumComponents:
    def test_all_three_present(self):
        payload = _full_payload()
        result = _sum_components(payload, "maker")
        assert result == Decimal("0.0011")

    def test_missing_standard_returns_none(self):
        payload = {
            "specialCommission": {"maker": "0.0001", "taker": "0.0001"},
            "taxCommission": {"maker": "0.0000", "taker": "0.0002"},
        }
        assert _sum_components(payload, "maker") is None

    def test_missing_special_returns_none(self):
        payload = {
            "standardCommission": {"maker": "0.001", "taker": "0.001"},
            "taxCommission": {"maker": "0.0000", "taker": "0.0002"},
        }
        assert _sum_components(payload, "maker") is None

    def test_missing_tax_returns_none(self):
        payload = {
            "standardCommission": {"maker": "0.001", "taker": "0.001"},
            "specialCommission": {"maker": "0.0001", "taker": "0.0001"},
        }
        assert _sum_components(payload, "maker") is None

    def test_all_missing_returns_none(self):
        assert _sum_components({}, "maker") is None

    def test_empty_sub_dict_returns_none(self):
        payload = {
            "standardCommission": {},
            "specialCommission": {"maker": "0.0001", "taker": "0.0001"},
            "taxCommission": {"maker": "0.0000", "taker": "0.0002"},
        }
        assert _sum_components(payload, "maker") is None

    def test_extra_keys_ignored(self):
        payload = _full_payload()
        payload["standardCommission"]["extra"] = "ignored"
        result = _sum_components(payload, "maker")
        assert result == Decimal("0.0011")

    def test_taker_side(self):
        payload = _full_payload()
        result = _sum_components(payload, "taker")
        assert result == Decimal("0.0015")

    def test_zero_sum(self):
        payload = _full_payload(
            std_maker="0", std_taker="0",
            spc_maker="0", spc_taker="0",
            tax_maker="0", tax_taker="0",
        )
        assert _sum_components(payload, "maker") == Decimal("0")
        assert _sum_components(payload, "taker") == Decimal("0")


# ===========================================================================
# E. effective_fees() — public API
# ===========================================================================
class TestEffectiveFeesAPI:
    # --- Fallback paths ---
    def test_none_payload_fallback(self):
        fees = effective_fees(None, 0.001, 0.002)
        assert fees.maker == Decimal("0.001")
        assert fees.taker == Decimal("0.002")
        assert fees.source == "FALLBACK"

    def test_empty_dict_fallback(self):
        fees = effective_fees({}, 0.003, 0.004)
        assert fees.maker == Decimal("0.003")
        assert fees.taker == Decimal("0.004")
        assert fees.source == "FALLBACK"

    # --- Incomplete payload paths ---
    def test_incomplete_missing_standard(self):
        fees = effective_fees(
            {"specialCommission": {"maker": "0.001", "taker": "0.001"},
             "taxCommission": {"maker": "0.001", "taker": "0.001"}},
            0.01, 0.02,
        )
        assert fees.maker == Decimal("0.01")
        assert fees.taker == Decimal("0.02")
        assert fees.source == "FALLBACK:INCOMPLETE_ACCOUNT_COMMISSION"

    def test_incomplete_missing_special(self):
        fees = effective_fees(
            {"standardCommission": {"maker": "0.001", "taker": "0.001"},
             "taxCommission": {"maker": "0.001", "taker": "0.001"}},
            0.01, 0.02,
        )
        assert fees.source == "FALLBACK:INCOMPLETE_ACCOUNT_COMMISSION"

    def test_incomplete_missing_tax(self):
        fees = effective_fees(
            {"standardCommission": {"maker": "0.001", "taker": "0.001"},
             "specialCommission": {"maker": "0.001", "taker": "0.001"}},
            0.01, 0.02,
        )
        assert fees.source == "FALLBACK:INCOMPLETE_ACCOUNT_COMMISSION"

    def test_incomplete_bad_value_in_standard(self):
        fees = effective_fees(
            {"standardCommission": {"maker": "invalid", "taker": "0.001"},
             "specialCommission": {"maker": "0.001", "taker": "0.001"},
             "taxCommission": {"maker": "0.001", "taker": "0.001"}},
            0.01, 0.02,
        )
        assert fees.source == "FALLBACK:INCOMPLETE_ACCOUNT_COMMISSION"

    # --- Happy path ---
    def test_full_valid_payload(self):
        payload = _full_payload()
        fees = effective_fees(payload, 0.009, 0.009)
        assert fees.maker == Decimal("0.0011")
        assert fees.taker == Decimal("0.0015")
        assert fees.source == "ACCOUNT_COMMISSION_CONSERVATIVE"

    def test_different_values_per_component(self):
        payload = _full_payload(
            std_maker="0.0005", std_taker="0.0006",
            spc_maker="0.0002", spc_taker="0.0002",
            tax_maker="0.0001", tax_taker="0.0003",
        )
        fees = effective_fees(payload, 0.009, 0.009)
        assert fees.maker == Decimal("0.0008")
        assert fees.taker == Decimal("0.0011")
        assert fees.source == "ACCOUNT_COMMISSION_CONSERVATIVE"

    # --- Fallback type coercion ---
    def test_fallback_string(self):
        fees = effective_fees(None, "0.0015", "0.0025")
        assert isinstance(fees.maker, Decimal)
        assert isinstance(fees.taker, Decimal)
        assert fees.maker == Decimal("0.0015")
        assert fees.taker == Decimal("0.0025")

    def test_fallback_int(self):
        fees = effective_fees(None, 1, 2)
        assert fees.maker == Decimal("1")
        assert fees.taker == Decimal("2")

    def test_fallback_float(self):
        fees = effective_fees(None, 0.001, 0.002)
        assert fees.maker == Decimal("0.001")
        assert fees.taker == Decimal("0.002")

    def test_fallback_zero(self):
        fees = effective_fees(None, 0, 0)
        assert fees.maker == Decimal("0")
        assert fees.taker == Decimal("0")
        assert fees.source == "FALLBACK"

    def test_fallback_negative(self):
        fees = effective_fees(None, -0.001, -0.002)
        assert fees.maker == Decimal("-0.001")
        assert fees.taker == Decimal("-0.002")

    def test_fallback_large(self):
        fees = effective_fees(None, 9999.99, 8888.88)
        assert fees.maker == Decimal("9999.99")
        assert fees.taker == Decimal("8888.88")

    # --- Discount deliberately ignored ---
    def test_discount_enabled_ignored(self):
        payload = _full_payload(include_discount=True, discount_enabled=True)
        fees = effective_fees(payload, 0.009, 0.009)
        # Same result as without discount
        fees_no_disc = effective_fees(_full_payload(), 0.009, 0.009)
        assert fees.maker == fees_no_disc.maker
        assert fees.taker == fees_no_disc.taker

    def test_discount_disabled_ignored(self):
        payload = _full_payload(include_discount=True, discount_enabled=False)
        fees = effective_fees(payload, 0.009, 0.009)
        assert fees.source == "ACCOUNT_COMMISSION_CONSERVATIVE"

    def test_discount_75pct_ignored(self):
        """Even 75% discount should not change the conservative result."""
        payload = _full_payload(include_discount=True, discount_value="0.75")
        fees = effective_fees(payload, 0.009, 0.009)
        # Without discount: maker=0.0011, taker=0.0015
        assert fees.maker == Decimal("0.0011")
        assert fees.taker == Decimal("0.0015")

    # --- Key naming variants ---
    def test_makerCommission_takerCommission_keys(self):
        payload = {
            "standardCommission": {"makerCommission": "0.001", "takerCommission": "0.001"},
            "specialCommission": {"makerCommission": "0.0001", "takerCommission": "0.0001"},
            "taxCommission": {"makerCommission": "0.0000", "takerCommission": "0.0002"},
        }
        fees = effective_fees(payload, 0.009, 0.009)
        assert fees.maker == Decimal("0.0011")
        assert fees.taker == Decimal("0.0013")

    def test_mixed_key_naming(self):
        """Some components use 'maker', others use 'makerCommission'."""
        payload = {
            "standardCommission": {"maker": "0.001", "taker": "0.001"},
            "specialCommission": {"makerCommission": "0.0001", "takerCommission": "0.0001"},
            "taxCommission": {"maker": "0.0000", "taker": "0.0002"},
        }
        fees = effective_fees(payload, 0.009, 0.009)
        assert fees.maker == Decimal("0.0011")
        assert fees.taker == Decimal("0.0013")

    # --- List values ---
    def test_list_values_single_element(self):
        payload = {
            "standardCommission": {"maker": ["0.001"], "taker": ["0.001"]},
            "specialCommission": {"maker": ["0.0001"], "taker": ["0.0001"]},
            "taxCommission": {"maker": ["0.0000"], "taker": ["0.0002"]},
        }
        fees = effective_fees(payload, 0.009, 0.009)
        assert fees.maker == Decimal("0.0011")
        assert fees.taker == Decimal("0.0013")

    def test_list_values_uses_first(self):
        """List with multiple elements — only first is used."""
        payload = {
            "standardCommission": {"maker": ["0.001", "0.002"], "taker": ["0.003", "0.004"]},
            "specialCommission": {"maker": "0.0001", "taker": "0.0001"},
            "taxCommission": {"maker": "0.0000", "taker": "0.0002"},
        }
        fees = effective_fees(payload, 0.009, 0.009)
        assert fees.maker == Decimal("0.0011")
        assert fees.taker == Decimal("0.0033")

    def test_empty_list_returns_fallback(self):
        """Empty list in one component → _rate returns None → incomplete."""
        payload = {
            "standardCommission": {"maker": [], "taker": "0.001"},
            "specialCommission": {"maker": "0.001", "taker": "0.001"},
            "taxCommission": {"maker": "0.001", "taker": "0.001"},
        }
        fees = effective_fees(payload, 0.01, 0.02)
        assert fees.source == "FALLBACK:INCOMPLETE_ACCOUNT_COMMISSION"

    # --- Precision ---
    def test_high_precision_fees(self):
        payload = _full_payload(
            std_maker="0.000001", std_taker="0.000002",
            spc_maker="0.000003", spc_taker="0.000004",
            tax_maker="0.000005", tax_taker="0.000006",
        )
        fees = effective_fees(payload, 0.009, 0.009)
        assert fees.maker == Decimal("0.000009")
        assert fees.taker == Decimal("0.000012")

    def test_many_decimal_places(self):
        payload = _full_payload(
            std_maker="0.00000001", std_taker="0.00000002",
            spc_maker="0.00000003", spc_taker="0.00000004",
            tax_maker="0.00000005", tax_taker="0.00000006",
        )
        fees = effective_fees(payload, 0.009, 0.009)
        assert fees.maker == Decimal("0.00000009")
        assert fees.taker == Decimal("0.00000012")

    # --- Edge cases ---
    def test_zero_commission_all_components(self):
        payload = _full_payload(
            std_maker="0", std_taker="0",
            spc_maker="0", spc_taker="0",
            tax_maker="0", tax_taker="0",
        )
        fees = effective_fees(payload, 0.009, 0.009)
        assert fees.maker == Decimal("0")
        assert fees.taker == Decimal("0")
        assert fees.source == "ACCOUNT_COMMISSION_CONSERVATIVE"

    def test_negative_commission_allowed(self):
        payload = _full_payload(
            std_maker="-0.001", std_taker="-0.001",
            spc_maker="-0.0001", spc_taker="-0.0001",
            tax_maker="0", tax_taker="0",
        )
        fees = effective_fees(payload, 0.009, 0.009)
        assert fees.maker == Decimal("-0.0011")
        assert fees.taker == Decimal("-0.0011")
        assert fees.source == "ACCOUNT_COMMISSION_CONSERVATIVE"

    def test_extra_top_level_keys_ignored(self):
        payload = _full_payload()
        payload["extraKey"] = "should_be_ignored"
        payload["anotherExtra"] = {"nested": "also_ignored"}
        fees = effective_fees(payload, 0.009, 0.009)
        assert fees.source == "ACCOUNT_COMMISSION_CONSERVATIVE"
        assert fees.maker == Decimal("0.0011")

    def test_only_discount_key_no_commissions(self):
        """Payload with only discount → incomplete → fallback."""
        payload = {"discount": {"enabledForAccount": True, "discount": "0.25"}}
        fees = effective_fees(payload, 0.01, 0.02)
        assert fees.source == "FALLBACK:INCOMPLETE_ACCOUNT_COMMISSION"
        assert fees.maker == Decimal("0.01")

    def test_partial_taker_missing(self):
        """standardCommission has maker but no taker → taker sum returns None."""
        payload = {
            "standardCommission": {"maker": "0.001"},  # no taker
            "specialCommission": {"maker": "0.0001", "taker": "0.0001"},
            "taxCommission": {"maker": "0.0000", "taker": "0.0002"},
        }
        fees = effective_fees(payload, 0.01, 0.02)
        assert fees.source == "FALLBACK:INCOMPLETE_ACCOUNT_COMMISSION"

    def test_only_taker_side_has_values(self):
        """Maker side incomplete, taker side complete → still fallback."""
        payload = {
            "standardCommission": {"taker": "0.001"},  # no maker
            "specialCommission": {"maker": "0.0001", "taker": "0.0001"},
            "taxCommission": {"maker": "0.0000", "taker": "0.0002"},
        }
        fees = effective_fees(payload, 0.01, 0.02)
        assert fees.source == "FALLBACK:INCOMPLETE_ACCOUNT_COMMISSION"

    def test_sub_dict_not_a_dict(self):
        """If a commission section is not a dict, _rate returns None → incomplete."""
        payload = {
            "standardCommission": "not_a_dict",
            "specialCommission": {"maker": "0.0001", "taker": "0.0001"},
            "taxCommission": {"maker": "0.0000", "taker": "0.0002"},
        }
        fees = effective_fees(payload, 0.01, 0.02)
        assert fees.source == "FALLBACK:INCOMPLETE_ACCOUNT_COMMISSION"

    def test_sub_dict_is_list(self):
        """If a commission section is a list, not a dict → incomplete."""
        payload = {
            "standardCommission": [0.001, 0.001],
            "specialCommission": {"maker": "0.0001", "taker": "0.0001"},
            "taxCommission": {"maker": "0.0000", "taker": "0.0002"},
        }
        fees = effective_fees(payload, 0.01, 0.02)
        assert fees.source == "FALLBACK:INCOMPLETE_ACCOUNT_COMMISSION"

    def test_all_three_sub_dicts_invalid(self):
        payload = {
            "standardCommission": "bad",
            "specialCommission": 123,
            "taxCommission": [1, 2, 3],
        }
        fees = effective_fees(payload, 0.01, 0.02)
        assert fees.source == "FALLBACK:INCOMPLETE_ACCOUNT_COMMISSION"
        assert fees.maker == Decimal("0.01")
        assert fees.taker == Decimal("0.02")

    def test_fallback_maker_and_taker_different(self):
        fees = effective_fees(None, 0.0015, 0.0025)
        assert fees.maker != fees.taker
        assert fees.maker == Decimal("0.0015")
        assert fees.taker == Decimal("0.0025")
