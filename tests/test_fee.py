from decimal import Decimal
from fee_model import effective_fees

def test_conservative_fee_sums_standard_special_tax():
    payload={
        "standardCommission":{"maker":"0.0010","taker":"0.0012"},
        "specialCommission":{"maker":"0.0001","taker":"0.0001"},
        "taxCommission":{"maker":"0.0000","taker":"0.0002"},
        "discount":{"enabledForAccount":True,"discount":"0.25"},
    }
    fees=effective_fees(payload,0.009,0.009)
    assert fees.maker == Decimal("0.0011")
    assert fees.taker == Decimal("0.0015")
    assert fees.source == "ACCOUNT_COMMISSION_CONSERVATIVE"

def test_incomplete_commission_uses_fallback():
    fees=effective_fees({"standardCommission":{"maker":"0.001","taker":"0.001"}},0.001,0.001)
    assert fees.source.startswith("FALLBACK:")
