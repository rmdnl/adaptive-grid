"""Inventory / accounting invariant tests.

After every step of realistic operation the ledger is replayed and the
following invariants are asserted:

- inventory >= 0
- inventory == sum(BUY qty) - sum(SELL qty)   (dust tolerance)
- weighted average cost == sum(buy notional) / sum(buy qty)  (while held)
- realized PnL == replayed SELL trades against the replayed average cost
- fees == sum of per-trade fees (never double-counted)
- filled_qty <= order qty; child_sell_qty <= filled BUY quantity
- sum of child SELL quantities == child_sell_qty of the parent
- remaining open SELL quantity covers held inventory (normal operation)
"""

from __future__ import annotations

import sqlite3

import pytest

from conftest import FakeSpot, make_config
from exchange import DryRunExecutor, LiveExecutor
from state import StateStore


def _all_rows(store, sql, params=()):
    conn = sqlite3.connect(store.path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows


def _assert_invariants(store, symbol, expect_covered=True):
    fills = _all_rows(store, "SELECT * FROM fills WHERE symbol=? ORDER BY id", (symbol,))
    buys = [f for f in fills if f["side"] == "BUY"]
    sells = [f for f in fills if f["side"] == "SELL"]
    orders = _all_rows(store, "SELECT * FROM orders WHERE symbol=?", (symbol,))
    st = store.get_symbol(symbol)
    inventory = float(st.inventory_qty or 0.0)

    # inventory is never negative and always equals bought - sold
    assert inventory >= 0.0
    assert inventory == pytest.approx(
        sum(f["qty"] for f in buys) - sum(f["qty"] for f in sells), abs=1e-9
    )

    # weighted average cost over all buys, while holding inventory
    total_buy_qty = sum(f["qty"] for f in buys)
    if inventory > 0 and total_buy_qty > 0:
        expected_avg = sum(f["qty"] * f["price"] for f in buys) / total_buy_qty
        assert st.avg_cost == pytest.approx(expected_avg, abs=1e-6)

    # replay the ledger: realized PnL and final inventory must match
    replay_inv, replay_avg, replay_pnl = 0.0, 0.0, 0.0
    for f in fills:
        if f["side"] == "BUY":
            replay_avg = (replay_inv * replay_avg + f["qty"] * f["price"]) / (replay_inv + f["qty"])
            replay_inv += f["qty"]
        else:
            sold = min(f["qty"], replay_inv)
            replay_pnl += sold * (f["price"] - replay_avg)
            replay_inv -= sold
    assert store.sum_realized_pnl(symbol) == pytest.approx(replay_pnl, abs=1e-9)
    assert replay_inv == pytest.approx(inventory, abs=1e-9)

    # fees are the per-trade fees, exactly once each
    assert store.sum_fees(symbol) == pytest.approx(sum(f["fee"] for f in fills), abs=1e-12)

    # per-order invariants
    for order in orders:
        assert (order["filled_qty"] or 0.0) <= order["qty"] + 1e-9
        if order["side"] == "BUY":
            converted = order["child_sell_qty"] or 0.0
            assert converted <= (order["filled_qty"] or 0.0) + 1e-9
            children = [
                c for c in orders if c.get("parent_order_id") == order["id"]
            ]
            assert sum(c["qty"] for c in children) == pytest.approx(converted, abs=1e-9)

    # open sell quantity covers held inventory in normal operation
    covered = sum(
        o["qty"] - (o["filled_qty"] or 0.0)
        for o in orders
        if o["side"] == "SELL" and o["status"] in ("NEW", "PARTIALLY_FILLED")
    )
    if expect_covered:
        assert covered >= inventory - 1e-9
    return inventory, covered


def test_dry_run_full_grid_cycle_invariants(tmp_path):
    """Grid placement -> two BUY fills -> child sells -> SELL fill ->
    BUY renewal -> second completed cycle -> restart + duplicate
    reconciliation. Invariants hold after every step."""
    store = StateStore(str(tmp_path / "state.db"))
    store.ensure_symbols(["BTC/USDT"])
    cfg = make_config()
    executor = DryRunExecutor(cfg, store)
    symbol = "BTC/USDT"

    executor.place_limit(symbol, "BUY", 100.0, 1.0, target_sell_price=101.0)
    executor.place_limit(symbol, "BUY", 90.0, 2.0, target_sell_price=91.0)

    # both buys fill in one candle
    fill_candle = {"high": 99.0, "low": 89.0, "close": 95.0}
    executor.sync_fills(symbol, fill_candle)
    _assert_invariants(store, symbol)
    assert store.get_symbol(symbol).inventory_qty == pytest.approx(3.0)
    assert store.get_symbol(symbol).avg_cost == pytest.approx(100.0 * 1.0 / 3.0 + 90.0 * 2.0 / 3.0)

    # duplicate reconciliation changes nothing
    executor.sync_fills(symbol, fill_candle)
    _assert_invariants(store, symbol)

    # the deeper child sell (2.0 @ 91) fills first; grid renews that level
    rise_candle = {"high": 94.0, "low": 91.5, "close": 92.0}
    executor.sync_fills(symbol, rise_candle)
    _assert_invariants(store, symbol)
    assert store.get_symbol(symbol).inventory_qty == pytest.approx(1.0)

    # restart: fresh store/executor over the same database, continue with a
    # duplicate reconciliation of the same candle
    reopened = StateStore(store.path)
    restarted = DryRunExecutor(cfg, reopened)
    restarted.sync_fills(symbol, rise_candle)
    _assert_invariants(reopened, symbol)

    # the remaining child sell (1.0 @ 101) fills: inventory fully sold
    exit_candle = {"high": 102.0, "low": 99.0, "close": 101.5}
    restarted.sync_fills(symbol, exit_candle)
    inv, _covered = _assert_invariants(reopened, symbol)
    assert inv == pytest.approx(0.0)
    assert reopened.count_completed_grids(symbol) == 2
    # realized PnL: (101-93.33)*1.0 + (91-93.33)*2.0
    expected = 1.0 * (101.0 - (100.0 + 180.0) / 3.0) + 2.0 * (91.0 - (100.0 + 180.0) / 3.0)
    assert reopened.sum_realized_pnl(symbol) == pytest.approx(expected, abs=1e-9)


def test_live_partial_fill_invariants(tmp_path):
    """Partial BUY fills, partial SELL fill and duplicate reconciliation
    under the live executor keep every invariant intact."""
    store = StateStore(str(tmp_path / "state.db"))
    store.ensure_symbols(["BTC/USDT"])
    spot = FakeSpot()
    executor = LiveExecutor(make_config(dry_run=False), spot, store)
    symbol = "BTC/USDT"

    buy_id = executor.place_limit(symbol, "BUY", 100.0, 1.0, target_sell_price=101.0)
    cid = store.get_order(buy_id)["client_order_id"]

    spot.fill(cid, 0.3, 100.0, fee=0.03)
    executor.sync_fills(symbol, None)
    _assert_invariants(store, symbol)

    spot.fill(cid, 0.2, 100.0, fee=0.02)
    executor.sync_fills(symbol, None)
    executor.sync_fills(symbol, None)  # duplicate reconciliation
    _assert_invariants(store, symbol)
    assert store.get_symbol(symbol).inventory_qty == pytest.approx(0.5)

    spot.fill(cid, 0.5, 100.0, fee=0.05)
    executor.sync_fills(symbol, None)
    _assert_invariants(store, symbol)
    assert store.get_symbol(symbol).inventory_qty == pytest.approx(1.0)

    # the child sell (1.0 @ 101) partially fills
    sell_id = [
        o for o in _all_rows(store, "SELECT * FROM orders")
        if o["parent_order_id"] == buy_id and o["side"] == "SELL"
    ][0]["id"]
    sell_cid = store.get_order(sell_id)["client_order_id"]
    spot.fill(sell_cid, 0.2, 101.0, fee=0.02)
    executor.sync_fills(symbol, None)
    _assert_invariants(store, symbol)
    assert store.get_symbol(symbol).inventory_qty == pytest.approx(0.8)
    # realized PnL only from the executed 0.2
    assert store.sum_realized_pnl(symbol) == pytest.approx(0.2 * 1.0, abs=1e-9)


def test_avg_cost_across_multiple_orders_and_restarts(tmp_path):
    """Weighted average cost stays exact across multiple buys at different
    prices, partial sells, a restart, and duplicate reconciliation."""
    store = StateStore(str(tmp_path / "state.db"))
    store.ensure_symbols(["ETH/USDT"])
    spot = FakeSpot()
    cfg = make_config(dry_run=False)
    executor = LiveExecutor(cfg, spot, store)
    symbol = "ETH/USDT"

    b1 = executor.place_limit(symbol, "BUY", 100.0, 1.0, target_sell_price=101.0)
    b2 = executor.place_limit(symbol, "BUY", 200.0, 1.0, target_sell_price=201.0)
    for order_id in (b1, b2):
        cid = store.get_order(order_id)["client_order_id"]
        spot.fill(cid, 1.0, store.get_order(order_id)["price"], fee=0.0)
    executor.sync_fills(symbol, None)
    _assert_invariants(store, symbol)
    assert store.get_symbol(symbol).avg_cost == pytest.approx(150.0)

    # restart, then partially sell at a profit
    reopened = StateStore(store.path)
    restarted = LiveExecutor(cfg, spot, reopened)
    sell_id = [
        o for o in _all_rows(reopened, "SELECT * FROM orders")
        if o["side"] == "SELL"
    ][0]["id"]
    sell_cid = reopened.get_order(sell_id)["client_order_id"]
    spot.fill(sell_cid, 0.4, 201.0, fee=0.0)
    restarted.sync_fills(symbol, None)
    restarted.sync_fills(symbol, None)  # duplicate reconciliation
    _assert_invariants(reopened, symbol)

    st = reopened.get_symbol(symbol)
    assert st.inventory_qty == pytest.approx(1.6)
    assert st.avg_cost == pytest.approx(150.0)  # sells never change avg cost
    assert reopened.sum_realized_pnl(symbol) == pytest.approx(0.4 * (201.0 - 150.0), abs=1e-9)
