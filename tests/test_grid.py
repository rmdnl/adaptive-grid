from grid_engine import build_geometric_grid, expected_net_pct, range_break

def test_grid_step():
    levels, _ = build_geometric_grid(100, 110, 0.006)
    assert abs(levels[1].price / levels[0].price - 1.006) < 1e-10

def test_profit_math():
    n = expected_net_pct(0.006, 0.001, 0.001, 0.0005)
    assert 0.003 < n < 0.004

def test_range_break():
    assert range_break(100, 110, 98.9, 0.01)
    assert not range_break(100, 110, 100, 0.01)
