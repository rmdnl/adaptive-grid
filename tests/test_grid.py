"""Grid tests: arithmetic/geometric construction, ATR step, executable
economics with fees/slippage, quantization, and rejected grids."""

from __future__ import annotations

import pytest

import grid
from conftest import make_config
from grid import ExchangeFilters, net_profit_pct, quantize_price_ceil, quantize_price_floor, quantize_qty_ceil


def filters(tick=0.01, step=0.00001, min_notional=10.0, min_qty=0.0) -> ExchangeFilters:
    return ExchangeFilters(tick, step, min_notional, min_qty)


# ----- quantization helpers -----

def test_price_quantization_floor_and_ceil():
    assert quantize_price_floor(99.123, 0.01) == pytest.approx(99.12)
    assert quantize_price_ceil(99.123, 0.01) == pytest.approx(99.13)
    assert quantize_price_floor(100.0, 0.01) == pytest.approx(100.0)


def test_qty_quantization_ceil():
    assert quantize_qty_ceil(0.0002014, 0.00001) == pytest.approx(0.00021)
    assert quantize_qty_ceil(0.2, 0.1) == pytest.approx(0.2)


# ----- conservative net profit -----

def test_net_profit_subtracts_both_fees_and_slippage():
    # gross 0.6%: net = 0.006 - 0.0015 * 2.006 = 0.002991
    assert net_profit_pct(0.006, 0.001, 0.001, 0.0005) == pytest.approx(0.002991, abs=1e-9)


def test_net_profit_at_half_percent_gross_is_below_net_minimum():
    # gross 0.5%: net = 0.005 - 0.0015 * 2.005 = 0.0019925 < 0.002
    assert net_profit_pct(0.005, 0.001, 0.001, 0.0005) == pytest.approx(0.0019925, abs=1e-9)
    assert net_profit_pct(0.005, 0.001, 0.001, 0.0005) < 0.002


def test_net_profit_uses_worst_fee_rate():
    # taker fee 0.002 dominates maker 0.001
    expected = 0.006 - (0.002 + 0.0005) * 2.006
    assert net_profit_pct(0.006, 0.001, 0.002, 0.0005) == pytest.approx(expected, abs=1e-9)


# ----- arithmetic grid -----

def test_arithmetic_grid_levels_and_economics(cfg):
    # Use a cfg with appropriate lower/upper bounds for the test price (50000)
    test_cfg = make_config(
        lower_price={"BTC/USDT": 47000.0},
        upper_price={"BTC/USDT": 70000.0},
        total_quote_budget={"BTC/USDT": 500.0},
    )
    plan = grid.build_grid("BTC/USDT", "arithmetic", 50000.0, 350.0, filters(), test_cfg)
    assert plan.executable is True
    assert plan.block_reason is None
    assert plan.mode == "arithmetic"
    assert plan.step == pytest.approx(350.0)
    assert len(plan.levels) == test_cfg.total_grids
    first = plan.levels[0]
    assert first.buy_price == pytest.approx(49650.0)
    assert first.sell_price == pytest.approx(50000.0)
    assert first.gross_pct == pytest.approx(350.0 / 49650.0, abs=1e-12)
    assert first.net_pct == pytest.approx(
        net_profit_pct(350.0 / 49650.0, 0.001, 0.001, 0.0005), abs=1e-12
    )
    # deepest level is the lowest boundary
    assert plan.lower_price == pytest.approx(50000.0 - 5 * 350.0)
    assert plan.lower_price == pytest.approx(min(l.buy_price for l in plan.levels))


def test_arithmetic_grid_step_is_atr_times_multiplier():
    from conftest import make_config
    test_cfg = make_config(
        grid_step_atr_multiplier=2.0,
        lower_price={"BTC/USDT": 46000.0},
        upper_price={"BTC/USDT": 70000.0},
        total_quote_budget={"BTC/USDT": 500.0},
    )
    plan = grid.build_grid("BTC/USDT", "arithmetic", 50000.0, 350.0, filters(), test_cfg)
    assert plan.levels[0].buy_price == pytest.approx(50000.0 - 700.0)


# ----- geometric grid -----

def test_geometric_grid_levels(cfg):
    test_cfg = make_config(
        lower_price={"SOL/USDT": 90.0},
        upper_price={"SOL/USDT": 120.0},
        total_quote_budget={"SOL/USDT": 500.0},
    )
    plan = grid.build_grid("SOL/USDT", "geometric", 100.0, 1.0, filters(), test_cfg)
    assert plan.executable is True
    assert plan.mode == "geometric"
    first = plan.levels[0]
    assert first.buy_price == pytest.approx(99.0)
    assert first.sell_price == pytest.approx(99.99)  # 99 * 1.01, ceiled to tick
    assert first.gross_pct == pytest.approx(0.01, abs=1e-9)
    second = plan.levels[1]
    assert second.buy_price == pytest.approx(98.01)  # 100 * 0.99^2


# ----- fees/slippage/min-notional handling -----

def test_qty_meets_min_notional(cfg):
    test_cfg = make_config(
        lower_price={"BTC/USDT": 47000.0},
        upper_price={"BTC/USDT": 70000.0},
        total_quote_budget={"BTC/USDT": 500.0},
    )
    plan = grid.build_grid("BTC/USDT", "arithmetic", 50000.0, 350.0, filters(), test_cfg)
    for level in plan.levels:
        assert level.qty * level.buy_price >= 10.0


def test_min_qty_is_respected(cfg):
    f = filters(min_qty=0.05)
    test_cfg = make_config(
        lower_price={"ETH/USDT": 180.0},
        upper_price={"ETH/USDT": 300.0},
        total_quote_budget={"ETH/USDT": 500.0},
    )
    plan = grid.build_grid("ETH/USDT", "arithmetic", 200.0, 2.0, f, test_cfg)
    for level in plan.levels:
        assert level.qty >= 0.05


# ----- rejected grids -----

def test_grid_blocked_when_gross_below_minimum():
    # step/price ~ 0.49% < 0.5% gross gate
    test_cfg = make_config(
        lower_price={"BTC/USDT": 9000.0},
        upper_price={"BTC/USDT": 15000.0},
        total_quote_budget={"BTC/USDT": 500.0},
    )
    plan = grid.build_grid("BTC/USDT", "arithmetic", 10000.0, 49.0, filters(), test_cfg)
    assert plan.executable is False
    assert plan.block_reason == "gross_below_minimum"


def test_grid_blocked_when_executable_net_below_minimum():
    # gross just above 0.5% (0.005005) but quantized executable net
    # = 0.9985*gross - 0.003 ~= 0.0019974 < 0.002
    test_cfg = make_config(
        lower_price={"BTC/USDT": 9000.0},
        upper_price={"BTC/USDT": 15000.0},
        total_quote_budget={"BTC/USDT": 500.0},
    )
    plan = grid.build_grid("BTC/USDT", "arithmetic", 10000.0, 49.7998, filters(), test_cfg)
    assert plan.executable is False
    assert plan.block_reason == "net_below_minimum"


def test_net_exactly_at_minimum_is_rejected():
    """Invariant: executable NET must be STRICTLY greater than the minimum.
    A grid whose worst level nets exactly the configured floor is REJECTED;
    only net > floor passes. Sweep ATR finely, and for every grid the gate
    accepts, verify that raising the floor to exactly that net blocks it."""
    test_cfg = make_config(
        lower_price={"BTC/USDT": 9000.0},
        upper_price={"BTC/USDT": 15000.0},
        total_quote_budget={"BTC/USDT": 500.0},
        min_net_profit_per_grid=0.002,
    )
    checked = 0
    for i in range(4000):
        atr = 50.0 + i * 0.0001
        plan = grid.build_grid("BTC/USDT", "arithmetic", 10000.0, atr, filters(), test_cfg)
        if not plan.executable:
            continue
        assert plan.net_pct > test_cfg.min_net_profit_per_grid, (
            f"grid with net <= floor was accepted (atr={atr}, net={plan.net_pct})"
        )
        stricter = make_config(
            lower_price={"BTC/USDT": 9000.0},
            upper_price={"BTC/USDT": 15000.0},
            total_quote_budget={"BTC/USDT": 500.0},
            min_net_profit_per_grid=plan.net_pct,
        )
        recheck = grid.build_grid("BTC/USDT", "arithmetic", 10000.0, atr, filters(), stricter)
        assert recheck.executable is False
        assert recheck.block_reason == "net_below_minimum"
        checked += 1
        if checked >= 25:
            break
    assert checked >= 25, "sweep never produced enough executable grids to check the strict gate"


def test_grid_blocked_on_missing_atr():
    test_cfg = make_config(
        lower_price={"BTC/USDT": 47000.0},
        upper_price={"BTC/USDT": 70000.0},
        total_quote_budget={"BTC/USDT": 500.0},
    )
    plan = grid.build_grid("BTC/USDT", "arithmetic", 50000.0, None, filters(), test_cfg)
    assert plan.executable is False
    assert plan.block_reason == "insufficient_data"


def test_grid_blocked_on_invalid_mode():
    test_cfg = make_config(
        lower_price={"BTC/USDT": 47000.0},
        upper_price={"BTC/USDT": 70000.0},
        total_quote_budget={"BTC/USDT": 500.0},
    )
    plan = grid.build_grid("BTC/USDT", "diagonal", 50000.0, 350.0, filters(), test_cfg)
    assert plan.executable is False
    assert plan.block_reason == "invalid_mode"


def test_grid_blocked_on_invalid_filters(cfg):
    plan = grid.build_grid("BTC/USDT", "arithmetic", 50000.0, 350.0, filters(tick=0.0), cfg)
    assert plan.executable is False
    assert plan.block_reason == "invalid_filters"


def test_executable_economics_are_authoritative_after_quantization(cfg):
    # Quantization (buy floored, sell ceiled) can only help gross here, so a
    # passing plan must recompute gross/net from the quantized prices.
    plan = grid.build_grid("BTC/USDT", "arithmetic", 50000.0, 350.0, filters(), cfg)
    level = plan.levels[0]
    exec_gross = (level.sell_price - level.buy_price) / level.buy_price
    assert level.gross_pct == pytest.approx(exec_gross, abs=1e-12)
    assert level.net_pct == pytest.approx(
        net_profit_pct(exec_gross, cfg.maker_fee, cfg.taker_fee, cfg.slippage_estimate),
        abs=1e-12,
    )
    # the plan reports the worst level, never the best
    assert plan.net_pct == pytest.approx(min(l.net_pct for l in plan.levels), abs=1e-12)


def test_buy_sell_price_ordering_for_both_modes(cfg):
    """Every level orders strictly: 0 < buy < sell, for arithmetic and
    geometric grids alike."""
    cases = (
        ("BTC/USDT", "arithmetic", 50000.0, 350.0),
        ("SOL/USDT", "geometric", 100.0, 1.0),
    )
    for symbol, mode, price, atr_value in cases:
        plan = grid.build_grid(symbol, mode, price, atr_value, filters(), cfg)
        assert plan.executable is True
        for level in plan.levels:
            assert 0 < level.buy_price < level.sell_price
