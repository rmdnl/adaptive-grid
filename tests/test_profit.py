from decimal import Decimal
from profit_model import net_pct_from_step, passes, profit_class
from backtest import net_pct_from_step as backtest_net_pct_from_step

def test_locked_grid_net_is_above_hard_min():
    value=net_pct_from_step("0.006","0.001","0.001","0.0005")
    assert Decimal("0.003") < value < Decimal("0.004")

def test_profit_gate_bounds():
    # STRICT gate: net must be > floor.  0.200% (== floor) is rejected.
    assert not passes("0.002","0.002")
    assert passes("0.0021","0.002")
    assert not passes("0.0019","0.002")

def test_net_profit_boundary_strict():
    """The net threshold is STRICTLY greater than 0.20%:
      0.200% -> REJECT,  0.199% -> REJECT,  0.201% -> PASS."""
    assert passes(Decimal("0.002"),   Decimal("0.002")) is False  # 0.200%
    assert passes(Decimal("0.00199"), Decimal("0.002")) is False  # 0.199%
    assert passes(Decimal("0.00201"), Decimal("0.002")) is True   # 0.201%

def test_profit_class():
    assert profit_class("0.0019","0.002","0.004") == "BLOCK"
    assert profit_class("0.002","0.002","0.004") == "BLOCK"      # == floor
    assert profit_class("0.0021","0.002","0.004") == "PREFERRED"
    assert profit_class("0.0045","0.002","0.004") == "PASS_ABOVE_TARGET"

def test_backtest_uses_available_profit_model_function():
    assert backtest_net_pct_from_step("0.006", "0.001", "0.001", "0.0005") > Decimal("0.003")
