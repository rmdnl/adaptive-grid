"""Active-symbol filtering tests: the dashboard shows only the configured
symbols of the active runtime session, while historical state stays intact."""

from __future__ import annotations

import json

import pytest

from dashboard import build_payload
from state import StateStore


def _stamp_configured(store, symbols):
    store.set_meta(
        "configured_symbols", json.dumps(symbols, separators=(",", ":"))
    )


def _history_btc(store, symbol="BTC/USDT"):
    """Seed a historical symbol row plus orders and fills for it."""
    store.ensure_symbols([symbol])
    buy = store.create_order("btc-hist-b", symbol, "BUY", "LIMIT_MAKER", 49650.0, 0.1, "dry_run")
    store.update_order_status(buy, "FILLED", 0.1)
    store.record_fill(buy, symbol, "BUY", 49650.0, 0.1, 0.0104, trade_id="hist-tb")
    sell = store.create_order("btc-hist-s", symbol, "SELL", "LIMIT_MAKER", 50000.0, 0.1, "dry_run")
    store.update_order_status(sell, "FILLED", 0.1)
    store.record_fill(sell, symbol, "SELL", 50000.0, 0.1, 0.0105, trade_id="hist-ts")
    store.update_symbol(symbol, inventory_qty=0.0, avg_cost=0.0, strategy_state="ACTIVE")


def test_dashboard_shows_only_configured_symbols(tmp_path):
    """DB contains BTC/USDT historical state; the active session configures
    NEAR/ETH/SOL/BNB. The payload returns exactly those four — no BTC."""
    path = str(tmp_path / "state.db")
    store = StateStore(path)
    _history_btc(store)  # historical BTC row + orders + fills

    configured = ["NEAR/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"]
    store.ensure_symbols(configured)
    for sym in configured:
        store.update_symbol(sym, strategy_state="ACTIVE")
    _stamp_configured(store, configured)

    payload = build_payload(path)
    shown = [s["symbol"] for s in payload["symbols"]]
    assert shown == configured  # exact PAIR_LIST order, no BTC, no extras

    # global flag reflects an active configuration
    assert payload["global"]["configured_symbols"] == configured
    assert payload["global"]["active_symbol_config"] is True


def test_historical_symbol_remains_in_database(tmp_path):
    path = str(tmp_path / "state.db")
    store = StateStore(path)
    _history_btc(store)
    configured = ["NEAR/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"]
    store.ensure_symbols(configured)
    _stamp_configured(store, configured)

    assert build_payload(path)["global"]["active_symbol_config"] is True
    # BTC is still persisted in the DB (dashboard just hides it)
    reopened = StateStore(path, read_only=True)
    all_rows = [s.symbol for s in reopened.all_symbols()]
    assert "BTC/USDT" in all_rows
    # and its PnL is still accounted globally
    assert reopened.sum_realized_pnl("BTC/USDT") == pytest.approx(0.1 * 350.0)


def test_historical_orders_and_fills_untouched(tmp_path):
    path = str(tmp_path / "state.db")
    store = StateStore(path)
    _history_btc(store)
    configured = ["NEAR/USDT"]
    store.ensure_symbols(configured)
    _stamp_configured(store, configured)

    build_payload(path)  # dashboard read must not mutate anything
    reopened = StateStore(path, read_only=True)
    orders = reopened.symbol_orders("BTC/USDT")
    assert len(orders) == 2
    assert reopened.count_open_orders() == 0
    assert reopened.sum_realized_pnl("BTC/USDT") == pytest.approx(0.1 * 350.0)
    assert reopened.sum_fees("BTC/USDT") == pytest.approx(0.0209)


def test_changing_configured_symbols_changes_dashboard_list(tmp_path):
    path = str(tmp_path / "state.db")
    store = StateStore(path)
    _history_btc(store)
    for sym in ("NEAR/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"):
        store.ensure_symbols([sym])
        store.update_symbol(sym, strategy_state="ACTIVE")

    # First session: BTC configured
    _stamp_configured(store, ["BTC/USDT"])
    assert [s["symbol"] for s in build_payload(path)["symbols"]] == ["BTC/USDT"]

    # A later restart with a different PAIR_LIST updates the persisted list
    _stamp_configured(store, ["NEAR/USDT", "ETH/USDT"])
    shown = [s["symbol"] for s in build_payload(path)["symbols"]]
    assert shown == ["NEAR/USDT", "ETH/USDT"]
    # the historical BTC row is still in the DB, just not displayed
    assert "BTC/USDT" in [s.symbol for s in StateStore(path, read_only=True).all_symbols()]


def test_missing_configured_symbols_fails_closed(tmp_path):
    """No configured_symbols key -> NO ACTIVE SYMBOL CONFIGURATION: the
    dashboard shows nothing instead of all historical symbols."""
    path = str(tmp_path / "state.db")
    store = StateStore(path)
    _history_btc(store)
    # deliberately no configured_symbols stamp

    payload = build_payload(path)
    assert payload["symbols"] == []
    assert payload["global"]["configured_symbols"] is None
    assert payload["global"]["active_symbol_config"] is False


def test_corrupt_configured_symbols_fails_closed(tmp_path):
    path = str(tmp_path / "state.db")
    store = StateStore(path)
    _history_btc(store)
    store.set_meta("configured_symbols", "{not json")
    payload = build_payload(path)
    assert payload["symbols"] == []
    assert payload["global"]["active_symbol_config"] is False


def test_configured_symbol_without_row_is_omitted(tmp_path):
    path = str(tmp_path / "state.db")
    store = StateStore(path)
    _history_btc(store)
    store.ensure_symbols(["ETH/USDT"])
    _stamp_configured(store, ["ETH/USDT", "GHOST/USDT"])
    shown = [s["symbol"] for s in build_payload(path)["symbols"]]
    assert shown == ["ETH/USDT"]  # no row -> omitted, still configured order


def test_bot_startup_updates_configured_symbols_deterministically(tmp_path):
    """Changing PAIR_LIST on a future restart must update the persisted
    list — verified through the real startup path (Bot init)."""
    import bot as bot_mod
    from conftest import make_config
    from exchange import DryRunExecutor

    store = StateStore(str(tmp_path / "state.db"))
    _history_btc(store)

    class NoMarket:
        def snapshot(self, symbol, cfg, now_ms):
            raise AssertionError("not a runtime cycle")

    # restart 1: NEAR-only configuration
    bot_mod.Bot(
        make_config(pair_list=("NEAR/USDT",)), store, NoMarket(), DryRunExecutor(make_config(), store)
    )
    shown = [s["symbol"] for s in build_payload(store.path)["symbols"]]
    assert shown == ["NEAR/USDT"]

    # restart 2: PAIR_LIST changed to ETH-only — the stored list follows
    bot_mod.Bot(
        make_config(pair_list=("ETH/USDT",)), store, NoMarket(), DryRunExecutor(make_config(), store)
    )
    shown = [s["symbol"] for s in build_payload(store.path)["symbols"]]
    assert shown == ["ETH/USDT"]
    # historical BTC row untouched
    assert "BTC/USDT" in [s.symbol for s in StateStore(store.path, read_only=True).all_symbols()]


def test_dashboard_remains_read_only(tmp_path):
    """The dashboard never writes: building payloads and querying the store
    read-only leaves the DB byte-identical."""
    path = str(tmp_path / "state.db")
    store = StateStore(path)
    _history_btc(store)
    configured = ["NEAR/USDT", "ETH/USDT"]
    store.ensure_symbols(configured)
    _stamp_configured(store, configured)
    with open(path, "rb") as f:
        before = f.read()

    build_payload(path)
    build_payload(path)
    with open(path, "rb") as f:
        after = f.read()
    assert before == after


def test_dashboard_payload_exposes_no_credentials(tmp_path):
    path = str(tmp_path / "state.db")
    store = StateStore(path)
    _history_btc(store)
    _stamp_configured(store, ["NEAR/USDT"])
    text = json.dumps(build_payload(path)).lower()
    for forbidden in ("api_key", "api_secret", "secret", "credential", "token"):
        assert forbidden not in text


def test_dashboard_source_reads_no_config_or_credentials():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "dashboard.py").read_text(encoding="utf-8")
    for forbidden in ("dotenv", "os.environ", "getenv", "load_config", "API_KEY", "API_SECRET"):
        assert forbidden not in source
