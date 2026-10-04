"""State tests: persistence, restart recovery, orders, fills, PnL, fees,
cooldown and kill state."""

from __future__ import annotations

import sqlite3

import pytest

from state import StateStore


@pytest.fixture
def store(tmp_path):
    s = StateStore(str(tmp_path / "state.db"))
    s.ensure_symbols(["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"])
    return s


def test_ensure_symbols_creates_rows_with_waiting_state(store):
    st = store.get_symbol("BTC/USDT")
    assert st is not None
    assert st.strategy_state == "WAITING"
    assert store.all_symbols()  # four configured symbols
    assert len(store.all_symbols()) == 4


def test_symbol_state_update_roundtrip(store):
    store.update_symbol(
        "BTC/USDT",
        timeframe="4h",
        last_price=50000.0,
        adx=15.0,
        rsi=30.0,
        percent_b=-0.1,
        volume_osc=0.2,
        zscore=0.5,
        atr=350.0,
        strategy_state="ACTIVE",
        grid_mode="arithmetic",
        grid_step=350.0,
        grid_lower=48250.0,
        gross_pct=0.0070,
        net_pct=0.0040,
    )
    st = store.get_symbol("BTC/USDT")
    assert st.strategy_state == "ACTIVE"
    assert st.last_price == pytest.approx(50000.0)
    assert st.adx == pytest.approx(15.0)
    assert st.percent_b == pytest.approx(-0.1)
    assert st.grid_lower == pytest.approx(48250.0)
    assert st.net_pct == pytest.approx(0.0040)


def test_unknown_update_fields_are_ignored(store):
    store.update_symbol("BTC/USDT", not_a_column=1)
    assert store.get_symbol("BTC/USDT").strategy_state == "WAITING"


def test_orders_lifecycle(store):
    oid = store.create_order("cid-1", "BTC/USDT", "BUY", "LIMIT_MAKER", 49650.0, 0.001, "dry_run")
    assert store.count_open_orders("BTC/USDT") == 1
    order = store.get_order(oid)
    assert order["client_order_id"] == "cid-1"
    assert order["status"] == "NEW"
    assert order["target_sell_price"] is None

    store.update_order_status(oid, "FILLED", 0.001)
    assert store.count_open_orders("BTC/USDT") == 0
    assert store.get_order(oid)["status"] == "FILLED"


def test_duplicate_client_order_id_rejected(store):
    store.create_order("cid-dup", "BTC/USDT", "BUY", "LIMIT_MAKER", 1.0, 1.0, "dry_run")
    with pytest.raises(sqlite3.IntegrityError):
        store.create_order("cid-dup", "ETH/USDT", "BUY", "LIMIT_MAKER", 1.0, 1.0, "dry_run")


def test_open_orders_filtering(store):
    b = store.create_order("cid-b", "BTC/USDT", "BUY", "LIMIT_MAKER", 1.0, 1.0, "dry_run")
    store.create_order("cid-e", "ETH/USDT", "BUY", "LIMIT_MAKER", 1.0, 1.0, "dry_run")
    assert store.count_open_orders() == 2
    assert len(store.open_orders("BTC/USDT")) == 1
    store.update_order_status(b, "CANCELED")
    assert store.count_open_orders() == 1
    assert store.open_orders("BTC/USDT") == []


def test_get_order_by_client_id(store):
    store.create_order("cid-x", "BTC/USDT", "BUY", "LIMIT_MAKER", 1.0, 1.0, "dry_run")
    order = store.get_order_by_client_id("cid-x")
    assert order is not None
    assert order["symbol"] == "BTC/USDT"
    assert store.get_order_by_client_id("missing") is None


def test_fills_pnl_and_fees_sums(store):
    b = store.create_order("cid-b2", "BTC/USDT", "BUY", "LIMIT_MAKER", 49650.0, 0.1, "dry_run")
    s = store.create_order("cid-s2", "BTC/USDT", "SELL", "LIMIT_MAKER", 50000.0, 0.1, "dry_run")
    store.record_fill(b, "BTC/USDT", "BUY", 49650.0, 0.1, 0.0104)
    store.record_fill(s, "BTC/USDT", "SELL", 50000.0, 0.1, 0.0105, realized_pnl=3.5)
    store.record_fill(
        store.create_order("cid-e2", "ETH/USDT", "SELL", "MARKET", 200.0, 0.05, "dry_run"),
        "ETH/USDT", "SELL", 200.0, 0.05, 0.01, realized_pnl=0.5,
    )
    assert store.sum_realized_pnl("BTC/USDT") == pytest.approx(3.5)
    assert store.sum_realized_pnl() == pytest.approx(4.0)
    assert store.sum_fees("BTC/USDT") == pytest.approx(0.0209)
    assert store.sum_fees() == pytest.approx(0.0309)
    assert store.count_completed_grids("BTC/USDT") == 1
    assert store.count_completed_grids("ETH/USDT") == 1


def test_duplicate_trade_id_is_ignored(store):
    oid = store.create_order("cid-t", "BTC/USDT", "BUY", "LIMIT_MAKER", 1.0, 1.0, "live")
    store.record_fill(oid, "BTC/USDT", "BUY", 1.0, 1.0, 0.001, trade_id="t-1")
    store.record_fill(oid, "BTC/USDT", "BUY", 1.0, 1.0, 0.001, trade_id="t-1")
    assert store.sum_fees("BTC/USDT") == pytest.approx(0.001)


def test_cooldown_persists_across_restart(store, tmp_path):
    store.set_cooldown("BTC/USDT", 1234567890.0)
    reopened = StateStore(str(tmp_path / "state.db"))
    assert reopened.get_symbol("BTC/USDT").cooldown_until == pytest.approx(1234567890.0)


def test_kill_state_persists_across_restart(store, tmp_path):
    store.set_global_kill("drawdown")
    reopened = StateStore(str(tmp_path / "state.db"))
    active, reason = reopened.global_kill()
    assert active is True
    assert reason == "drawdown"


def test_risk_events_persist(store, tmp_path):
    store.add_risk_event("BTC/USDT", "auto_exit", "rsi_overbought")
    reopened = StateStore(str(tmp_path / "state.db"))
    events = reopened.recent_risk_events()
    assert events[0]["event"] == "auto_exit"
    assert events[0]["details"] == "rsi_overbought"


def test_meta_and_equity_roundtrip(store):
    store.set_meta_float("equity", 12.34)
    store.set_meta_float("reference_equity", 20.0)
    store.set_runtime("RUNNING", 1000.0)
    assert store.get_meta_float("equity") == pytest.approx(12.34)
    assert store.get_meta_float("reference_equity") == pytest.approx(20.0)
    status, ts = store.last_runtime()
    assert status == "RUNNING"
    assert ts == pytest.approx(1000.0)


def test_restart_recovery_preserves_full_symbol_state(tmp_path):
    first = StateStore(str(tmp_path / "state.db"))
    first.ensure_symbols(["BTC/USDT"])
    first.set_symbol_state("BTC/USDT", "ACTIVE", last_price=50000.0, inventory_qty=0.5, avg_cost=49500.0)
    first.set_global_kill("drawdown")
    second = StateStore(str(tmp_path / "state.db"))
    st = second.get_symbol("BTC/USDT")
    assert st.strategy_state == "ACTIVE"
    assert st.inventory_qty == pytest.approx(0.5)
    assert st.avg_cost == pytest.approx(49500.0)
    assert second.global_kill()[0] is True


def test_database_status_ok(store):
    status = store.database_status()
    assert status["ok"] is True
    assert status["size_bytes"] > 0
