from decimal import Decimal
import pytest

from grid_engine import build_geometric_grid, validate_grid_profit, range_break, inside_range


def test_grid_step_and_cells():
    levels, top = build_geometric_grid(Decimal("100"), Decimal("110"), Decimal("0.006"), min_cells=6, max_levels=40)
    assert len(levels) - 1 >= 6
    assert levels[1].price / levels[0].price == Decimal("1.006")
    assert top == levels[-1].price


def test_grid_rejects_too_short_range():
    with pytest.raises(ValueError):
        build_geometric_grid(100, 101, 0.006, min_cells=6, max_levels=40)


def test_grid_profit_validation():
    levels, _ = build_geometric_grid(100, 110, 0.006, min_cells=6, max_levels=40)
    result = validate_grid_profit(levels, 0.001, 0.001, 0.0005, 0.003)
    assert result.allowed
    assert result.cells == len(levels) - 1


def test_range_boundary():
    assert range_break(100, 110, 98.9, 0.01)
    assert not range_break(100, 110, 100, 0.01)
    assert inside_range(100, 110, 105)
