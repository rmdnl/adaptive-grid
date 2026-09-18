from profit_model import net_pct, passes

def test_net():
    value = net_pct(0.006, 0.001, 0.001, 0.0005)
    assert value > 0.003
    assert value < 0.004

def test_pass():
    assert passes(0.0035, 0.003)
    assert not passes(0.0029, 0.003)
