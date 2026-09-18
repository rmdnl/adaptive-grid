from risk_engine import (
    combine,
    cooldown_gate,
    daily_profit_lock,
    equity_dd_kill,
    inventory_gate,
    market_gate,
    open_orders_gate,
    profit_gate,
    range_gate,
)


def test_profit_gate_blocks():
    assert not profit_gate(0.0029, 0.003).allowed
    assert profit_gate(0.003, 0.003).allowed


def test_dd_kill_exact_threshold_blocks():
    assert not equity_dd_kill(0.02, 0.02).allowed
    assert equity_dd_kill(0.0199, 0.02).allowed


def test_range_gate():
    assert not range_gate(100, 110, 98.0, 0.01).allowed
    assert range_gate(100, 110, 101, 0.01).allowed


def test_inventory_and_order_limits():
    assert not inventory_gate(0.71, 0.70).allowed
    assert not open_orders_gate(40, 40).allowed


def test_cooldown_and_daily_lock():
    assert not cooldown_gate(True).allowed
    assert not daily_profit_lock(0.01, 0.01).allowed


def test_market_gate_blocks_adx():
    row = {"adx": 30, "atr_pct": 0.01, "bb_width": 0.03, "volume_ratio": 1.0}
    assert not market_gate(row, {
        "adx_max": 28,
        "atr_pct_max": 0.025,
        "bb_width_max": 0.06,
        "volume_spike_max": 2.5,
    }).allowed


def test_combine_is_veto():
    a = profit_gate(0.004, 0.003)
    b = equity_dd_kill(0.02, 0.02)
    result = combine(a, b)
    assert not result.allowed
    assert "EQUITY_DRAWDOWN_KILL" in result.reasons
