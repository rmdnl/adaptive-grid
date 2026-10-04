"""Bot cycle tests (offline): entry/placement, fill spawning, exits with
verification, cooldown, 15m boundary gate, drawdown kill, restart safety.

Uses a stub market (no network) and the dry-run executor, mirroring the
default runtime mode.
"""

from __future__ import annotations

import pytest

from bot import Bot, CycleView
from conftest import make_config
from exchange import DryRunExecutor
from grid import ExchangeFilters
from state import StateStore
from strategy import IndicatorSnapshot

FILTERS = ExchangeFilters(tick_size=0.01, step_size=0.00001, min_notional=10.0, min_qty=0.0)


def snap_entry(last_close=50000.0, atr=350.0, **overrides) -> IndicatorSnapshot:
    values = dict(
        symbol="BTC/USDT", last_close=last_close, adx=15.0, rsi=30.0,
        percent_b=-0.1, volume_osc=0.2, zscore=0.5, atr=atr,
    )
    values.update(overrides)
    return IndicatorSnapshot(**values)


def snap_insufficient() -> IndicatorSnapshot:
    return IndicatorSnapshot(symbol="BTC/USDT")


class StubMarket:
    """Same interface as MarketData, but returns canned cycle views.
    Symbols without an explicit view report insufficient data (realistic)."""

    def __init__(self):
        self.views = {}
        self.filters_obj = FILTERS

    def set(self, symbol, snapshot, close_15m=None, candle=None):
        self.views[symbol] = CycleView(snapshot, close_15m, candle)

    def snapshot(self, symbol, cfg, now_ms):
        if symbol in self.views:
            return self.views[symbol]
        return CycleView(IndicatorSnapshot(symbol=symbol), None, None)

    def filters(self, symbol):
        return self.filters_obj


NO_FILL_CANDLE = {"high": 49990.0, "low": 49900.0, "close": 49950.0}


@pytest.fixture
def env(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    market = StubMarket()
    cfg = make_config()
    bot = Bot(cfg, store, market, DryRunExecutor(cfg, store))
    return bot, store, market


def test_entry_places_grid_and_sets_active(env):
    bot, store, market = env
    market.set("BTC/USDT", snap_entry(), close_15m=49000.0, candle=NO_FILL_CANDLE)
    bot.run_once()
    st = store.get_symbol("BTC/USDT")
    assert st.strategy_state == "ACTIVE"
    assert store.count_open_orders("BTC/USDT") == 5
    assert st.grid_mode == "arithmetic"
    assert st.grid_lower == pytest.approx(48250.0)
    assert st.exit_status == 0
    buys = store.open_orders("BTC/USDT")
    assert all(o["side"] == "BUY" for o in buys)
    assert all(o["target_sell_price"] > o["price"] for o in buys)


def test_insufficient_data_means_waiting(env):
    bot, store, market = env
    market.set("BTC/USDT", snap_insufficient())
    bot.run_once()
    st = store.get_symbol("BTC/USDT")
    assert st.strategy_state == "WAITING"
    assert store.count_open_orders("BTC/USDT") == 0


def test_entry_blocked_state_records_blocker(env):
    bot, store, market = env
    # volume oscillator below the entry minimum: blocks entry without
    # touching any exit condition (so exit priority does not mask it)
    market.set("BTC/USDT", snap_entry(volume_osc=-0.5))
    bot.run_once()
    st = store.get_symbol("BTC/USDT")
    assert st.strategy_state == "ENTRY_BLOCKED"
    assert st.entry_blocker == "volume_osc_not_positive"


def test_adx_exit_condition_blocks_entry_with_exit_priority(env):
    # ADX 30 is simultaneously an entry blocker AND an exit condition:
    # exit has priority, and a fresh symbol must not enter either.
    bot, store, market = env
    market.set("BTC/USDT", snap_entry(adx=30.0))
    bot.run_once()
    st = store.get_symbol("BTC/USDT")
    assert st.strategy_state == "ENTRY_BLOCKED"
    assert st.entry_blocker == "exit_priority"


def test_grid_blocked_state_records_reason(env):
    bot, store, market = env
    market.set("BTC/USDT", snap_entry(atr=1.0))  # 0.002% step < 0.5% gross
    bot.run_once()
    st = store.get_symbol("BTC/USDT")
    assert st.strategy_state == "GRID_BLOCKED"
    assert st.block_reason == "gross_below_minimum"
    assert store.count_open_orders("BTC/USDT") == 0


def test_buy_fill_spawns_sell_then_completed_grid_renews_buy(env):
    bot, store, market = env
    market.set("BTC/USDT", snap_entry(), close_15m=49000.0, candle=NO_FILL_CANDLE)
    bot.run_once()
    # candle dips to the first buy level -> buy fills, sell spawns
    fill_candle = {"high": 49990.0, "low": 49600.0, "close": 49900.0}
    market.set("BTC/USDT", snap_entry(), close_15m=49000.0, candle=fill_candle)
    bot.run_once()
    st = store.get_symbol("BTC/USDT")
    assert st.strategy_state == "ACTIVE"
    assert st.inventory_qty > 0
    sells = [o for o in store.open_orders("BTC/USDT") if o["side"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["price"] == pytest.approx(50000.0)

    # candle rises to the sell target -> sell fills, grid renews the buy
    rise_candle = {"high": 50050.0, "low": 49700.0, "close": 50010.0}
    market.set("BTC/USDT", snap_entry(), close_15m=49000.0, candle=rise_candle)
    bot.run_once()
    st = store.get_symbol("BTC/USDT")
    assert st.inventory_qty == pytest.approx(0.0)
    assert store.count_completed_grids("BTC/USDT") == 1
    assert store.sum_realized_pnl("BTC/USDT") == pytest.approx(0.00021 * 350.0, abs=1e-9)
    assert store.get_meta_float("equity") > 0
    buys = [o for o in store.open_orders("BTC/USDT") if o["side"] == "BUY"]
    assert len(buys) == 5


def test_exit_liquidates_verifies_and_enters_cooldown(env):
    bot, store, market = env
    market.set("BTC/USDT", snap_entry(), close_15m=49000.0, candle=NO_FILL_CANDLE)
    bot.run_once()
    market.set("BTC/USDT", snap_entry(rsi=75.0), close_15m=49000.0, candle=NO_FILL_CANDLE)
    bot.run_once()
    st = store.get_symbol("BTC/USDT")
    assert st.strategy_state == "COOLDOWN"
    assert st.exit_status == 1
    assert st.exit_reason == "rsi_overbought"
    assert st.cooldown_until is not None
    assert store.count_open_orders("BTC/USDT") == 0
    assert any(e["event"] == "auto_exit" for e in store.recent_risk_events())


def test_cooldown_blocks_reentry_and_survives_restart(env, tmp_path):
    bot, store, market = env
    market.set("BTC/USDT", snap_entry(), close_15m=49000.0, candle=NO_FILL_CANDLE)
    bot.run_once()
    market.set("BTC/USDT", snap_entry(rsi=75.0), close_15m=49000.0, candle=NO_FILL_CANDLE)
    bot.run_once()

    restarted = Bot(bot.cfg, store, market, DryRunExecutor(bot.cfg, store))
    market.set("BTC/USDT", snap_entry(), close_15m=49000.0, candle=NO_FILL_CANDLE)
    restarted.run_once()
    assert store.get_symbol("BTC/USDT").strategy_state == "COOLDOWN"
    assert store.count_open_orders("BTC/USDT") == 0


def test_exit_with_inventory_liquidates(env):
    bot, store, market = env
    market.set("BTC/USDT", snap_entry(), close_15m=49000.0, candle=NO_FILL_CANDLE)
    bot.run_once()
    fill_candle = {"high": 49990.0, "low": 49600.0, "close": 49900.0}
    market.set("BTC/USDT", snap_entry(), close_15m=49000.0, candle=fill_candle)
    bot.run_once()
    assert store.get_symbol("BTC/USDT").inventory_qty > 0

    market.set("BTC/USDT", snap_entry(rsi=80.0), close_15m=49000.0, candle=fill_candle)
    bot.run_once()
    st = store.get_symbol("BTC/USDT")
    assert st.strategy_state == "COOLDOWN"
    assert st.inventory_qty == pytest.approx(0.0)
    assert store.count_open_orders("BTC/USDT") == 0
    sells = store.sum_realized_pnl("BTC/USDT")
    # one completed grid (0.00021 * 350) plus the liquidation sale at ~market
    assert sells > 0


def test_lower_boundary_breach_stops_symbol_without_cooldown(env):
    bot, store, market = env
    market.set("BTC/USDT", snap_entry(), close_15m=49000.0, candle=NO_FILL_CANDLE)
    bot.run_once()
    # closed 15m candle 4% below the lower boundary (48250)
    market.set("BTC/USDT", snap_entry(), close_15m=46320.0, candle=NO_FILL_CANDLE)
    bot.run_once()
    st = store.get_symbol("BTC/USDT")
    assert st.strategy_state == "STOPPED"
    assert st.risk_status == "stopped"
    assert st.exit_reason == "lower_boundary_breach"
    assert st.cooldown_until is None
    assert store.count_open_orders("BTC/USDT") == 0

    # a stopped symbol is never automatically re-entered
    market.set("BTC/USDT", snap_entry(), close_15m=49000.0, candle=NO_FILL_CANDLE)
    bot.run_once()
    assert store.get_symbol("BTC/USDT").strategy_state == "STOPPED"


def test_boundary_unknown_is_fail_closed_but_grid_continues(env):
    bot, store, market = env
    market.set("BTC/USDT", snap_entry(), close_15m=49000.0, candle=NO_FILL_CANDLE)
    bot.run_once()
    before = store.count_open_orders("BTC/USDT")
    market.set("BTC/USDT", snap_entry(), close_15m=None, candle=NO_FILL_CANDLE)
    bot.run_once()
    st = store.get_symbol("BTC/USDT")
    assert st.strategy_state == "ACTIVE"
    assert store.count_open_orders("BTC/USDT") == before  # no new orders


def test_drawdown_kill_switch_triggers_and_persists(env):
    bot, store, market = env
    market.set("BTC/USDT", snap_entry(), close_15m=49000.0, candle=NO_FILL_CANDLE)
    bot.run_once()
    # first cycle anchors the reference at the starting equity (1000);
    # a large realized loss on ETH (bought 100, sold 60) then breaches
    # the 2% hard limit
    assert store.get_meta_float("reference_equity") == pytest.approx(1000.0)
    loss_b = store.create_order("cid-loss-b", "ETH/USDT", "BUY", "MARKET", 100.0, 1.0, "dry_run")
    store.record_fill(loss_b, "ETH/USDT", "BUY", 100.0, 1.0, 0.0, trade_id="t-loss-b")
    loss_s = store.create_order("cid-loss-s", "ETH/USDT", "SELL", "MARKET", 60.0, 1.0, "dry_run")
    store.record_fill(loss_s, "ETH/USDT", "SELL", 60.0, 1.0, 0.0, trade_id="t-loss-s")
    bot.run_once()
    active, reason = store.global_kill()
    assert active is True
    assert "max_drawdown_breach" in reason
    assert store.get_symbol("BTC/USDT").strategy_state == "KILL_ACTIVE"

    # kill state survives restart and no trading resumes
    restarted = Bot(bot.cfg, store, market, DryRunExecutor(bot.cfg, store))
    market.set("ETH/USDT", snap_entry(symbol="ETH/USDT", last_close=3000.0, atr=20.0),
               close_15m=2950.0, candle=NO_FILL_CANDLE)
    restarted.run_once()
    assert store.global_kill()[0] is True
    assert store.get_symbol("ETH/USDT").strategy_state == "KILL_ACTIVE"
    assert store.count_open_orders() == 0


def test_small_pnl_fluctuation_does_not_trip_kill_switch(env):
    # unrealized -> realized conversion must not fire the drawdown kill
    bot, store, market = env
    market.set("BTC/USDT", snap_entry(), close_15m=49000.0, candle=NO_FILL_CANDLE)
    bot.run_once()
    market.set("BTC/USDT", snap_entry(), close_15m=49000.0,
               candle={"high": 49990.0, "low": 49600.0, "close": 49900.0})
    bot.run_once()
    market.set("BTC/USDT", snap_entry(), close_15m=49000.0,
               candle={"high": 50050.0, "low": 49700.0, "close": 50010.0})
    bot.run_once()
    assert store.global_kill()[0] is False
    assert store.get_symbol("BTC/USDT").strategy_state == "ACTIVE"
    assert store.count_completed_grids("BTC/USDT") == 1
    buys = [o for o in store.open_orders("BTC/USDT") if o["side"] == "BUY"]
    assert len(buys) == 5


class FaultyExecutor(DryRunExecutor):
    """Dry-run executor with injectable verification failures."""

    def __init__(self, cfg, store):
        super().__init__(cfg, store)
        self.fail_cancel = False
        self.fail_liquidate = False

    def cancel_all(self, symbol):
        if self.fail_cancel:
            return False
        return super().cancel_all(symbol)

    def place_market_sell(self, symbol, qty, ref_price):
        if self.fail_liquidate:
            return False
        return super().place_market_sell(symbol, qty, ref_price)


def _pre_kill_state(tmp_path, executor_cls=DryRunExecutor, with_inventory=False):
    """Build everything up to (but not including) the kill-triggering cycle:
    grid placed, optionally one filled buy, then a realized loss large
    enough to breach the 2% drawdown limit on the next run_once()."""
    store = StateStore(str(tmp_path / "state.db"))
    market = StubMarket()
    cfg = make_config()
    executor = executor_cls(cfg, store)
    bot = Bot(cfg, store, market, executor)
    market.set("BTC/USDT", snap_entry(), close_15m=49000.0, candle=NO_FILL_CANDLE)
    bot.run_once()
    assert store.get_meta_float("reference_equity") == pytest.approx(1000.0)
    if with_inventory:
        market.set("BTC/USDT", snap_entry(), close_15m=49000.0,
                   candle={"high": 49990.0, "low": 49600.0, "close": 49900.0})
        bot.run_once()
        assert store.get_symbol("BTC/USDT").inventory_qty > 0
    # realized loss of 40 on ETH (bought 100, sold 60) -> ~4% drawdown
    loss_b = store.create_order("cid-loss-b", "ETH/USDT", "BUY", "MARKET", 100.0, 1.0, "dry_run")
    store.record_fill(loss_b, "ETH/USDT", "BUY", 100.0, 1.0, 0.0, trade_id="t-loss-b")
    loss_s = store.create_order("cid-loss-s", "ETH/USDT", "SELL", "MARKET", 60.0, 1.0, "dry_run")
    store.record_fill(loss_s, "ETH/USDT", "SELL", 60.0, 1.0, 0.0, trade_id="t-loss-s")
    return bot, store, market


def test_global_kill_liquidates_inventory(tmp_path):
    bot, store, market = _pre_kill_state(tmp_path, with_inventory=True)
    bot.run_once()

    active, reason = store.global_kill()
    assert active is True
    assert "max_drawdown_breach" in reason
    st = store.get_symbol("BTC/USDT")
    assert st.strategy_state == "KILL_ACTIVE"
    assert st.inventory_qty == pytest.approx(0.0)          # liquidated
    assert store.count_open_orders("BTC/USDT") == 0        # and cancelled
    # liquidation sale recorded against the held inventory
    assert store.sum_realized_pnl("BTC/USDT") > 0
    assert store.count_open_orders() == 0


def test_global_kill_with_failed_cancellation_fails_closed(tmp_path):
    bot, store, market = _pre_kill_state(tmp_path, executor_cls=FaultyExecutor)
    bot.executor.fail_cancel = True
    bot.run_once()

    assert store.global_kill()[0] is True                  # kill stays latched
    st = store.get_symbol("BTC/USDT")
    assert st.strategy_state == "ERROR"
    assert store.count_open_orders("BTC/USDT") == 5        # NOT cancelled
    assert any(
        e["event"] == "cancel_verify_failed" for e in store.recent_risk_events()
    )


def test_global_kill_with_failed_liquidation_fails_closed(tmp_path):
    bot, store, market = _pre_kill_state(
        tmp_path, executor_cls=FaultyExecutor, with_inventory=True
    )
    bot.executor.fail_liquidate = True
    bot.run_once()

    assert store.global_kill()[0] is True                  # kill stays latched
    st = store.get_symbol("BTC/USDT")
    assert st.strategy_state == "ERROR"
    assert st.inventory_qty > 0                            # NOT liquidated
    assert any(
        e["event"] == "liquidation_verify_failed" for e in store.recent_risk_events()
    )


def test_dry_run_liquidation_is_deterministic(tmp_path):
    cfg = make_config()
    store = StateStore(str(tmp_path / "state.db"))
    executor = DryRunExecutor(cfg, store)
    store.ensure_symbols(["BTC/USDT"])
    buy = store.create_order("cid-b", "BTC/USDT", "BUY", "LIMIT_MAKER", 100.0, 2.0, "dry_run")
    store.record_fill(buy, "BTC/USDT", "BUY", 100.0, 2.0, 0.0, trade_id="t-b")

    assert executor.place_market_sell("BTC/USDT", 2.0, ref_price=90.0) is True
    st = store.get_symbol("BTC/USDT")
    assert st.inventory_qty == pytest.approx(0.0)
    # sale at ref * (1 - slippage): realized against avg cost 100
    sale_price = 90.0 * (1.0 - cfg.slippage_estimate)
    assert store.sum_realized_pnl("BTC/USDT") == pytest.approx(2.0 * (sale_price - 100.0))
    assert store.sum_fees("BTC/USDT") == pytest.approx(sale_price * 2.0 * cfg.taker_fee)

    # repeating the same logical liquidation does not double-account
    executor.place_market_sell("BTC/USDT", 2.0, ref_price=90.0)
    assert store.sum_realized_pnl("BTC/USDT") == pytest.approx(2.0 * (sale_price - 100.0))


def test_dry_run_accounting_is_deterministic(tmp_path):
    outcomes = []
    for run in range(2):
        store = StateStore(str(tmp_path / f"state{run}.db"))
        market = StubMarket()
        cfg = make_config()
        bot = Bot(cfg, store, market, DryRunExecutor(cfg, store))
        market.set("BTC/USDT", snap_entry(), close_15m=49000.0, candle=NO_FILL_CANDLE)
        bot.run_once()
        market.set("BTC/USDT", snap_entry(), close_15m=49000.0,
                   candle={"high": 49990.0, "low": 49600.0, "close": 49900.0})
        bot.run_once()
        market.set("BTC/USDT", snap_entry(), close_15m=49000.0,
                   candle={"high": 50050.0, "low": 49700.0, "close": 50010.0})
        bot.run_once()
        outcomes.append(
            (
                store.get_meta_float("equity"),
                store.sum_realized_pnl(),
                store.sum_fees(),
                store.count_completed_grids("BTC/USDT"),
            )
        )
    assert outcomes[0] == outcomes[1]


def test_cycle_error_marks_symbol_error_not_crash(env):
    bot, store, market = env
    bot.market.set("BTC/USDT", snap_entry(), close_15m=49000.0, candle=NO_FILL_CANDLE)

    class FailingFiltersMarket(StubMarket):
        def filters(self, symbol):
            raise RuntimeError("exchange down")

    failing = FailingFiltersMarket()
    failing.views = dict(bot.market.views)
    bot.market = failing
    bot.run_once()
    st = store.get_symbol("BTC/USDT")
    assert st.strategy_state == "ERROR"
    assert any(e["event"] == "cycle_error" for e in store.recent_risk_events())
