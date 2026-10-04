"""Execution session tests: paper capital derivation from the testnet
wallet, session persistence/resume, mode isolation, and explicit reset."""

from __future__ import annotations

import logging

import pytest

from bot import Bot, CycleView, SessionError
from conftest import FakeSpot, make_config
from exchange import DryRunExecutor, ExchangeError
from state import StateStore


class BalanceSpot:
    """Minimal spot stand-in exposing only the wallet balance."""

    def __init__(self, usdt: float):
        self.usdt = usdt
        self.balance_calls = 0

    def get_balance(self, asset: str) -> float:
        self.balance_calls += 1
        return self.usdt


class NoMarket:
    def snapshot(self, symbol, cfg, now_ms):
        return CycleView.__new__(CycleView)

    def filters(self, symbol):
        raise AssertionError("paper session init must not fetch filters")


def _bot(tmp_path, cfg, spot=None, name="state.db"):
    store = StateStore(str(tmp_path / name))
    bot = Bot(cfg, store, NoMarket(), DryRunExecutor(cfg, store), spot=spot)
    return bot, store


# ----- paper capital derivation -----

def test_paper_capital_from_testnet_balance_10000(tmp_path):
    bot, store = _bot(tmp_path, make_config(start_equity=0.0), BalanceSpot(10000.0))
    assert store.get_meta_float("session_start_equity") == pytest.approx(10000.0)
    assert store.get_meta_float("session_initial_cash") == pytest.approx(10000.0)
    assert store.get_meta_float("reference_equity") == pytest.approx(10000.0)
    assert store.get_meta_float("wallet_usdt") == pytest.approx(10000.0)
    assert store.get_meta("session_mode") == "paper"
    assert store.get_meta("session_env") == "testnet"
    assert store.get_meta("session_id")


def test_paper_capital_from_testnet_balance_fractional(tmp_path):
    bot, store = _bot(tmp_path, make_config(start_equity=0.0), BalanceSpot(2375.42))
    assert store.get_meta_float("session_start_equity") == pytest.approx(2375.42)


def test_configured_start_equity_overrides_testnet_balance(tmp_path):
    bot, store = _bot(tmp_path, make_config(start_equity=500.0), BalanceSpot(10000.0))
    assert store.get_meta_float("session_start_equity") == pytest.approx(500.0)


def test_zero_testnet_balance_fails_closed(tmp_path):
    with pytest.raises(SessionError, match="USDT balance is 0"):
        _bot(tmp_path, make_config(start_equity=0.0), BalanceSpot(0.0))


def test_no_capital_source_fails_closed(tmp_path):
    with pytest.raises(SessionError, match="no session capital"):
        _bot(tmp_path, make_config(start_equity=0.0), spot=None)


def test_balance_query_failure_fails_closed(tmp_path):
    class BrokenSpot:
        def get_balance(self, asset):
            raise ExchangeError("testnet unreachable")

    with pytest.raises(SessionError, match="testnet unreachable"):
        _bot(tmp_path, make_config(start_equity=0.0), BrokenSpot())


# ----- session persistence / resume -----

def test_restart_resumes_session_without_resetting_capital(tmp_path):
    spot = BalanceSpot(10000.0)
    bot, store = _bot(tmp_path, make_config(start_equity=0.0), spot)
    session_id = store.get_meta("session_id")

    spot.usdt = 999.0  # wallet changed; must NOT be re-imported as capital
    restarted, store2 = _bot(tmp_path, make_config(start_equity=0.0), spot)

    assert store2.get_meta("session_id") == session_id
    assert store2.get_meta_float("session_start_equity") == pytest.approx(10000.0)
    assert store2.get_meta_float("reference_equity") == pytest.approx(10000.0)
    assert spot.balance_calls == 1  # queried only for the fresh session


def test_equity_anchors_to_session_capital_not_config_default(tmp_path):
    bot, store = _bot(tmp_path, make_config(start_equity=0.0), BalanceSpot(10000.0))
    bot.run_once()
    # no fills: equity equals the session capital, not any hardcoded value
    assert store.get_meta_float("equity") == pytest.approx(10000.0)
    assert store.get_meta_float("reference_equity") == pytest.approx(10000.0)


# ----- mode / environment isolation -----

def test_paper_to_testnet_switch_is_blocked(tmp_path):
    bot, store = _bot(tmp_path, make_config(), BalanceSpot(10000.0))
    switched = make_config(
        dry_run=False, execution_mode="testnet",
        testnet_api_key="tk", testnet_api_secret="ts",
    )
    with pytest.raises(SessionError, match="refusing to start"):
        _bot(tmp_path, switched, spot=FakeSpot(), name="state.db")


def test_testnet_to_paper_switch_is_blocked(tmp_path):
    cfg = make_config(
        dry_run=False, execution_mode="testnet",
        testnet_api_key="tk", testnet_api_secret="ts",
    )
    bot, store = _bot(tmp_path, cfg, spot=FakeSpot())
    with pytest.raises(SessionError, match="refusing to start"):
        _bot(tmp_path, make_config(), spot=None, name="state.db")


def test_environment_mismatch_is_blocked(tmp_path):
    bot, store = _bot(tmp_path, make_config(), BalanceSpot(10000.0))
    live_cfg = make_config(
        dry_run=False, allow_live_execution=True, binance_env="live",
        execution_mode="live", live_api_key="k", live_api_secret="s",
    )
    with pytest.raises(SessionError, match="refusing to start"):
        _bot(tmp_path, live_cfg, spot=None, name="state.db")


# ----- explicit reset -----

def test_explicit_reset_clears_session_and_allows_fresh_capital(tmp_path):
    bot, store = _bot(tmp_path, make_config(start_equity=0.0), BalanceSpot(10000.0))
    old_id = store.get_meta("session_id")

    from bot import reset_execution_session
    reset_execution_session(bot.cfg, store)  # no open orders -> allowed

    assert store.get_meta("session_id") is None
    assert store.get_meta("kill_active") is None
    spot = BalanceSpot(2375.42)
    fresh, store2 = _bot(tmp_path, make_config(start_equity=0.0), spot)
    assert store2.get_meta("session_id") != old_id
    assert store2.get_meta_float("session_start_equity") == pytest.approx(2375.42)


def test_reset_refuses_while_open_orders_exist(tmp_path):
    bot, store = _bot(tmp_path, make_config(), BalanceSpot(10000.0))
    # place a grid through a full bot cycle to create open orders
    from test_bot import StubMarket, snap_entry, NO_FILL_CANDLE  # reuse fixtures

    stub = StubMarket()
    bot.market = stub
    stub.set("BTC/USDT", snap_entry(), close_15m=49000.0, candle=NO_FILL_CANDLE)
    bot.run_once()
    assert bot.store.count_open_orders() > 0

    from bot import reset_execution_session
    with pytest.raises(SessionError, match="open orders"):
        reset_execution_session(bot.cfg, bot.store)


def test_reset_is_the_escape_hatch_for_mismatched_state(tmp_path):
    bot, store = _bot(tmp_path, make_config(), BalanceSpot(10000.0))
    from bot import reset_execution_session
    reset_execution_session(bot.cfg, store)
    # after reset, a different mode may initialize (explicit migration path)
    switched = make_config(
        dry_run=False, execution_mode="testnet",
        testnet_api_key="tk", testnet_api_secret="ts",
    )
    fresh, store2 = _bot(tmp_path, switched, spot=FakeSpot(), name="state.db")
    assert store2.get_meta("session_mode") == "testnet"


# ----- credential hygiene -----

def test_credentials_never_appear_in_logs(tmp_path, caplog):
    cfg = make_config(
        dry_run=False, execution_mode="testnet",
        testnet_api_key="SECRET-TEST-KEY-1234", testnet_api_secret="SECRET-TEST-SECRETS-5678",
    )
    with caplog.at_level(logging.DEBUG, logger="exchange"):
        spot = FakeSpot()
        from exchange import LiveExecutor
        store = StateStore(str(tmp_path / "state.db"))
        executor = LiveExecutor(cfg, spot, store)
        local_id = executor.place_limit("BTC/USDT", "BUY", 100.0, 1.0, target_sell_price=101.0)
        cid = store.get_order(local_id)["client_order_id"]
        spot.fill(cid, 1.0, 100.0, fee=0.1)
        executor.sync_fills("BTC/USDT", None)
    text = caplog.text
    assert "SECRET-TEST-KEY-1234" not in text
    assert "SECRET-TEST-SECRETS-5678" not in text


# ----- wallet telemetry is display-only -----

def test_wallet_telemetry_updates_without_touching_paper_equity(tmp_path):
    spot = BalanceSpot(10000.0)
    bot, store = _bot(
        tmp_path,
        make_config(start_equity=0.0, testnet_api_key="k", testnet_api_secret="s"),
        spot,
    )
    bot.run_once()
    spot.usdt = 5000.0  # wallet drifts on the exchange
    bot.run_once()
    # wallet telemetry follows the exchange; paper equity does not
    assert store.get_meta_float("wallet_usdt") == pytest.approx(5000.0)
    assert store.get_meta_float("equity") == pytest.approx(10000.0)
    assert store.get_meta_float("session_start_equity") == pytest.approx(10000.0)
