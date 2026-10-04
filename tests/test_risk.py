"""Risk tests: 2% drawdown kill, kill persistence, 15m lower-boundary
protection, order vetoes, fail-closed behavior."""

from __future__ import annotations

import pytest

import risk as risk_mod
from risk import RiskEngine
from state import StateStore


@pytest.fixture
def store(tmp_path):
    return StateStore(str(tmp_path / "state.db"))


@pytest.fixture
def engine(cfg, store):
    return RiskEngine(cfg, store)


# ----- drawdown kill -----

def test_drawdown_at_two_percent_breaches(engine):
    assert engine.drawdown_breach(98.0, 100.0) is True      # (100-98)/100 = 2%


def test_drawdown_just_below_two_percent_does_not_breach(engine):
    assert engine.drawdown_breach(98.01, 100.0) is False


def test_drawdown_with_zero_reference_is_safe(engine):
    assert engine.drawdown_breach(-5.0, 0.0) is False


def test_drawdown_with_profit_above_reference_is_safe(engine):
    assert engine.drawdown_breach(105.0, 100.0) is False


def test_global_kill_persists_across_restart(engine, store, tmp_path):
    engine.trigger_global_kill("max_drawdown_breach dd=2.31%")
    assert store.global_kill() == (True, "max_drawdown_breach dd=2.31%")

    reopened = StateStore(str(tmp_path / "state.db"))
    assert reopened.global_kill() == (True, "max_drawdown_breach dd=2.31%")


def test_kill_switch_records_risk_event(engine, store):
    engine.trigger_global_kill("test reason")
    events = store.recent_risk_events()
    assert any(e["event"] == "kill_switch" and "test reason" in e["details"] for e in events)


# ----- order vetoes -----

def test_no_veto_in_normal_conditions(engine):
    assert engine.order_veto("BTC/USDT").allowed is True


def test_veto_every_order_when_global_kill_active(engine):
    engine.trigger_global_kill("drawdown")
    decision = engine.order_veto("BTC/USDT")
    assert decision.allowed is False
    assert decision.reason.startswith("global_kill:")


def test_veto_orders_for_stopped_symbol(engine, store):
    store.ensure_symbols(["BTC/USDT", "ETH/USDT"])
    store.stop_symbol("BTC/USDT", "lower_boundary_breach")
    assert engine.order_veto("BTC/USDT").allowed is False
    assert engine.order_veto("BTC/USDT").reason == "symbol_stopped"
    # other symbols unaffected
    assert engine.order_veto("ETH/USDT").allowed is True


# ----- 15m lower-boundary protection -----

def test_boundary_breach_at_exact_threshold(engine):
    # close <= lower * (1 - 2%) -> 98.0 <= 98.0 -> breach
    assert engine.boundary_status(98.0, 100.0) == risk_mod.BREACH


def test_boundary_ok_just_above_threshold(engine):
    assert engine.boundary_status(98.01, 100.0) == risk_mod.OK


def test_boundary_uses_closed_candle_close_only(engine):
    # Only the CLOSED candle close is ever passed in; an intrabar wick
    # cannot trigger the gate by construction.
    assert engine.boundary_status(97.0, 100.0) == risk_mod.BREACH
    assert engine.boundary_status(99.0, 100.0) == risk_mod.OK


def test_boundary_fail_closed_on_missing_data(engine):
    assert engine.boundary_status(None, 100.0) == risk_mod.UNKNOWN
    assert engine.boundary_status(98.0, None) == risk_mod.UNKNOWN
    assert engine.boundary_status(0.0, 100.0) == risk_mod.UNKNOWN
    assert engine.boundary_status(98.0, -1.0) == risk_mod.UNKNOWN


def test_stop_symbol_persists(engine, store):
    store.ensure_symbols(["BTC/USDT"])
    engine.stop_symbol("BTC/USDT", "lower_boundary_breach")
    st = store.get_symbol("BTC/USDT")
    assert st.risk_status == "stopped"
    assert st.strategy_state == "STOPPED"
    assert st.exit_reason == "lower_boundary_breach"
    # survives restart
    reopened = StateStore(store.path)
    assert reopened.get_symbol("BTC/USDT").risk_status == "stopped"
