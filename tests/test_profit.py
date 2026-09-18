from decimal import Decimal
from profit_model import net_pct_from_step, passes, profit_class

def test_locked_grid_net_is_above_hard_min():
    value=net_pct_from_step("0.006","0.001","0.001","0.0005")
    assert Decimal("0.003") < value < Decimal("0.004")

def test_profit_gate_bounds():
    assert passes("0.0035","0.003")
    assert not passes("0.0029","0.003")

def test_profit_class():
    assert profit_class("0.0029","0.003","0.004") == "BLOCK"
    assert profit_class("0.0035","0.003","0.004") == "PREFERRED"
    assert profit_class("0.0045","0.003","0.004") == "PASS_ABOVE_TARGET"
