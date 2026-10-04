"""Execution accounting regression tests (offline, live path).

Uses a scriptable fake Binance spot API to exercise LiveExecutor:
trade-id idempotency, partial fills, child-sell conversion, liquidation
retries with tracked client order ids, and balance verification.
"""

from __future__ import annotations

import sqlite3

import pytest

from conftest import make_config
from exchange import ExchangeError, LiveExecutor, OrderUnknownState
from state import StateStore


class FakeSpot:
    """Scriptable BinanceSpot stand-in (same API surface as BinanceSpot)."""

    def __init__(self, adjust_balance=True):
        self.orders = {}            # cid -> remote order dict
        self.trades = {}            # orderId -> [trade dicts]
        self.order_seq = 1000
        self.trade_seq = 5000
        self.submit_calls = []      # (type, cid, qty) for every accepted submission
        self.fail_next_submit = None  # "lost" | "pre" | None
        self.market_fill_fractions = []  # per-market-order executed fraction
        self.adjust_balance = adjust_balance
        self.balances = {}
        self.fail_balance = False

    # ----- test scripting helpers -----

    def _register(self, cid, symbol, side, order_type, price, qty):
        self.order_seq += 1
        self.orders[cid] = {
            "symbol": symbol, "orderId": self.order_seq, "clientOrderId": cid,
            "side": side, "type": order_type, "origQty": float(qty),
            "price": price, "status": "NEW", "executedQty": 0.0,
        }
        return self.orders[cid]

    def fill(self, cid, qty, price, fee=0.0, fee_asset="USDT"):
        """Simulate an execution (full or partial) of a remote order."""
        order = self.orders[cid]
        self.trade_seq += 1
        self.trades.setdefault(order["orderId"], []).append(
            {
                "id": str(self.trade_seq),
                "orderId": order["orderId"],
                "price": price,
                "qty": qty,
                "commission": fee,
                "commissionAsset": fee_asset,
            }
        )
        order["executedQty"] = float(order["executedQty"]) + float(qty)
        if order["executedQty"] >= float(order["origQty"]) - 1e-12:
            order["status"] = "FILLED"
        else:
            order["status"] = "PARTIALLY_FILLED"
        if self.adjust_balance and order["side"] == "SELL":
            base = order["symbol"].split("/")[0]
            if base in self.balances:
                self.balances[base] = self.balances[base] - float(qty)
        return order

    def _submit(self, order_type, symbol, side, price, qty, cid):
        mode = self.fail_next_submit
        self.fail_next_submit = None
        if mode == "lost":
            # Request reached the exchange; the response was lost.
            self._register(cid, symbol, side, order_type, price, qty)
            raise ExchangeError("timeout: response lost")
        if mode == "pre":
            raise ExchangeError("network error before submission")
        self.submit_calls.append((order_type, cid, float(qty)))
        order = self._register(cid, symbol, side, order_type, price, qty)
        if order_type == "MARKET":
            fraction = self.market_fill_fractions.pop(0) if self.market_fill_fractions else 1.0
            executed = float(qty) * fraction
            if executed > 0:
                self.fill(cid, executed, 100.0)
            else:
                order["status"] = "FILLED"  # nothing executable, terminal
        return dict(self.orders[cid])

    # ----- BinanceSpot API surface -----

    def create_limit_maker_order(self, symbol, side, price, qty, cid):
        return self._submit("LIMIT_MAKER", symbol, side, price, qty, cid)

    def create_market_order(self, symbol, side, qty, cid):
        return self._submit("MARKET", symbol, side, None, qty, cid)

    def cancel_order(self, symbol, cid):
        self.orders[cid]["status"] = "CANCELED"
        return dict(self.orders[cid])

    def get_order(self, symbol, cid):
        order = self.orders.get(cid)
        return dict(order) if order else None

    def get_open_orders(self, symbol=None):
        return [
            dict(o)
            for o in self.orders.values()
            if o["status"] in ("NEW", "PARTIALLY_FILLED")
        ]

    def get_my_trades(self, symbol, order_id=None):
        return [dict(t) for t in self.trades.get(order_id, [])]

    def get_account(self):
        if self.fail_balance:
            raise ExchangeError("balance unavailable")
        return {
            "balances": [
                {"asset": a, "free": v, "locked": 0.0} for a, v in self.balances.items()
            ]
        }

    def get_balance(self, asset):
        if self.fail_balance:
            raise ExchangeError("balance unavailable")
        return float(self.balances.get(asset, 0.0))


def _make_env(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    cfg = make_config(dry_run=False)
    spot = FakeSpot()
    executor = LiveExecutor(cfg, spot, store)
    return store, executor, spot


def fills_row_count(store):
    conn = sqlite3.connect(store.path)
    n = conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    conn.close()
    return n


def child_sells(store, parent_id):
    return [
        o for o in _all_orders(store)
        if o["parent_order_id"] == parent_id and o["side"] == "SELL"
    ]


def _all_orders(store):
    conn = sqlite3.connect(store.path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM orders").fetchall()]
    conn.close()
    return rows


def test_one_trade_recorded_exactly_once(tmp_path):
    store, executor, spot = _make_env(tmp_path)
    local_id = executor.place_limit("BTC/USDT", "BUY", 100.0, 1.0, target_sell_price=101.0)
    cid = store.get_order(local_id)["client_order_id"]
    spot.fill(cid, 1.0, 100.0, fee=0.1)

    executor.sync_fills("BTC/USDT", None)

    st = store.get_symbol("BTC/USDT")
    assert st.inventory_qty == pytest.approx(1.0)
    assert st.avg_cost == pytest.approx(100.0)
    assert store.sum_fees("BTC/USDT") == pytest.approx(0.1)
    assert fills_row_count(store) == 1
    # the acquired quantity spawned exactly one child sell
    children = child_sells(store, local_id)
    assert len(children) == 1
    assert children[0]["qty"] == pytest.approx(1.0)
    assert children[0]["price"] == pytest.approx(101.0)


def test_reconciling_same_trade_twice_does_not_duplicate(tmp_path):
    store, executor, spot = _make_env(tmp_path)
    local_id = executor.place_limit("BTC/USDT", "BUY", 100.0, 1.0, target_sell_price=101.0)
    cid = store.get_order(local_id)["client_order_id"]
    spot.fill(cid, 1.0, 100.0, fee=0.1)

    executor.sync_fills("BTC/USDT", None)
    executor.sync_fills("BTC/USDT", None)  # repeated reconciliation

    assert store.get_symbol("BTC/USDT").inventory_qty == pytest.approx(1.0)
    assert store.sum_fees("BTC/USDT") == pytest.approx(0.1)
    assert fills_row_count(store) == 1
    assert len(child_sells(store, local_id)) == 1


def test_reconciliation_after_restart_does_not_duplicate(tmp_path):
    store, executor, spot = _make_env(tmp_path)
    local_id = executor.place_limit("BTC/USDT", "BUY", 100.0, 1.0, target_sell_price=101.0)
    cid = store.get_order(local_id)["client_order_id"]
    spot.fill(cid, 1.0, 100.0, fee=0.1)
    executor.sync_fills("BTC/USDT", None)

    # restart: fresh store and executor instances over the same database
    reopened = StateStore(store.path)
    restarted = LiveExecutor(make_config(dry_run=False), spot, reopened)
    restarted.sync_fills("BTC/USDT", None)

    assert reopened.get_symbol("BTC/USDT").inventory_qty == pytest.approx(1.0)
    assert reopened.sum_fees("BTC/USDT") == pytest.approx(0.1)
    assert fills_row_count(reopened) == 1


def test_partial_buy_accounting(tmp_path):
    store, executor, spot = _make_env(tmp_path)
    local_id = executor.place_limit("BTC/USDT", "BUY", 100.0, 1.0, target_sell_price=101.0)
    cid = store.get_order(local_id)["client_order_id"]
    spot.fill(cid, 0.4, 100.0, fee=0.04)

    executor.sync_fills("BTC/USDT", None)

    order = store.get_order(local_id)
    assert order["status"] == "PARTIALLY_FILLED"
    assert order["filled_qty"] == pytest.approx(0.4)
    st = store.get_symbol("BTC/USDT")
    assert st.inventory_qty == pytest.approx(0.4)   # actual executed, not planned 1.0
    assert store.sum_fees("BTC/USDT") == pytest.approx(0.04)
    # child sell only for the acquired quantity, and only one
    children = child_sells(store, local_id)
    assert len(children) == 1
    assert children[0]["qty"] == pytest.approx(0.4)


def test_partial_sell_accounting(tmp_path):
    store, executor, spot = _make_env(tmp_path)
    buy_id = executor.place_limit("BTC/USDT", "BUY", 100.0, 1.0, target_sell_price=101.0)
    buy_cid = store.get_order(buy_id)["client_order_id"]
    spot.fill(buy_cid, 1.0, 100.0, fee=0.1)
    executor.sync_fills("BTC/USDT", None)

    sell_id = child_sells(store, buy_id)[0]["id"]
    sell_cid = store.get_order(sell_id)["client_order_id"]
    spot.fill(sell_cid, 0.3, 101.0, fee=0.03)
    executor.sync_fills("BTC/USDT", None)

    sell_row = store.get_order(sell_id)
    assert sell_row["status"] == "PARTIALLY_FILLED"
    assert sell_row["filled_qty"] == pytest.approx(0.3)
    st = store.get_symbol("BTC/USDT")
    assert st.inventory_qty == pytest.approx(0.7)
    # realized PnL only for the executed 0.3 at 101 against avg cost 100
    assert store.sum_realized_pnl("BTC/USDT") == pytest.approx(0.3 * 1.0)
    # no grid renewal before the sell is fully filled
    buys = [o for o in _all_orders(store) if o["side"] == "BUY"]
    assert len(buys) == 1


def test_multiple_fills_of_one_order(tmp_path):
    store, executor, spot = _make_env(tmp_path)
    local_id = executor.place_limit("BTC/USDT", "BUY", 100.0, 1.0, target_sell_price=101.0)
    cid = store.get_order(local_id)["client_order_id"]

    for qty, fee in ((0.3, 0.03), (0.2, 0.02), (0.5, 0.05)):
        spot.fill(cid, qty, 100.0, fee=fee)
        executor.sync_fills("BTC/USDT", None)

    order = store.get_order(local_id)
    assert order["status"] == "FILLED"
    assert order["filled_qty"] == pytest.approx(1.0)
    st = store.get_symbol("BTC/USDT")
    assert st.inventory_qty == pytest.approx(1.0)
    assert st.avg_cost == pytest.approx(100.0)
    assert store.sum_fees("BTC/USDT") == pytest.approx(0.1)
    assert fills_row_count(store) == 3
    # child sells issued per fill event total exactly the acquired quantity
    children = child_sells(store, local_id)
    assert sum(c["qty"] for c in children) == pytest.approx(1.0)


def test_no_duplicate_child_sell_after_repeated_reconciliation(tmp_path):
    store, executor, spot = _make_env(tmp_path)
    local_id = executor.place_limit("BTC/USDT", "BUY", 100.0, 1.0, target_sell_price=101.0)
    cid = store.get_order(local_id)["client_order_id"]
    for qty in (0.3, 0.2, 0.5):
        spot.fill(cid, qty, 100.0)
        executor.sync_fills("BTC/USDT", None)

    for _ in range(3):
        executor.sync_fills("BTC/USDT", None)

    children = child_sells(store, local_id)
    assert sum(c["qty"] for c in children) == pytest.approx(1.0)
    parent = store.get_order(local_id)
    assert parent["child_sell_qty"] == pytest.approx(1.0)
    total_orders = len(_all_orders(store))
    executor.sync_fills("BTC/USDT", None)
    assert len(_all_orders(store)) == total_orders  # nothing new spawned


def test_partial_then_final_fill(tmp_path):
    store, executor, spot = _make_env(tmp_path)
    local_id = executor.place_limit("BTC/USDT", "BUY", 100.0, 1.0, target_sell_price=101.0)
    cid = store.get_order(local_id)["client_order_id"]

    spot.fill(cid, 0.4, 100.0)
    executor.sync_fills("BTC/USDT", None)
    spot.fill(cid, 0.6, 100.0)
    executor.sync_fills("BTC/USDT", None)

    order = store.get_order(local_id)
    assert order["status"] == "FILLED"
    assert store.get_symbol("BTC/USDT").inventory_qty == pytest.approx(1.0)
    children = child_sells(store, local_id)
    assert sum(c["qty"] for c in children) == pytest.approx(1.0)  # 0.4 + 0.6
    assert fills_row_count(store) == 2


def test_network_error_after_successful_submission(tmp_path):
    store, executor, spot = _make_env(tmp_path)
    spot.fail_next_submit = "lost"  # request reaches exchange, response lost

    local_id = executor.place_limit("BTC/USDT", "BUY", 100.0, 1.0, target_sell_price=101.0)

    order = store.get_order(local_id)
    assert order["status"] == "NEW"          # adopted via reconciliation
    assert len(spot.orders) == 1             # exactly one exchange order
    assert spot.submit_calls == []           # no blind re-submission


def test_network_error_before_submission_fails_closed(tmp_path):
    store, executor, spot = _make_env(tmp_path)
    spot.fail_next_submit = "pre"  # nothing reached the exchange

    with pytest.raises(OrderUnknownState):
        executor.place_limit("BTC/USDT", "BUY", 100.0, 1.0, target_sell_price=101.0)

    order = store.get_order_by_client_id(_last_local_cid(store))
    assert order["status"] == "UNKNOWN"
    assert len(spot.orders) == 0


def _last_local_cid(store):
    rows = _all_orders(store)
    return rows[-1]["client_order_id"]


def test_local_open_order_missing_on_exchange_fails_closed(tmp_path):
    store, executor, spot = _make_env(tmp_path)
    executor.place_limit("BTC/USDT", "BUY", 100.0, 1.0, target_sell_price=101.0)
    spot.orders.clear()  # exchange knows nothing about it

    with pytest.raises(OrderUnknownState):
        executor.sync_fills("BTC/USDT", None)

    rows = _all_orders(store)
    assert rows[0]["status"] == "UNKNOWN"


def test_liquidation_retry_uses_tracked_client_ids(tmp_path):
    store, executor, spot = _make_env(tmp_path)
    spot.balances = {"BTC": 1.0}
    spot.market_fill_fractions = [0.6, 1.0]

    ok = executor.place_market_sell("BTC/USDT", 1.0, ref_price=100.0)

    assert ok is True
    market_calls = [c for c in spot.submit_calls if c[0] == "MARKET"]
    assert len(market_calls) == 2
    assert market_calls[0][2] == pytest.approx(1.0)   # first attempt: full remainder
    assert market_calls[1][2] == pytest.approx(0.4)   # second attempt: actual remainder
    assert market_calls[0][1] != market_calls[1][1]   # distinct tracked ids
    rows = [o for o in _all_orders(store) if o["type"] == "MARKET"]
    assert [(r["qty"], r["filled_qty"]) for r in rows] == [
        (pytest.approx(1.0), pytest.approx(0.6)),
        (pytest.approx(0.4), pytest.approx(0.4)),
    ]
    assert fills_row_count(store) == 2


def test_partial_liquidation_accounts_only_executed(tmp_path):
    store, executor, spot = _make_env(tmp_path)
    spot.balances = {"BTC": 2.0}
    spot.market_fill_fractions = [0.5]

    ok = executor.place_market_sell("BTC/USDT", 2.0, ref_price=100.0)

    assert ok is True
    market_calls = [c for c in spot.submit_calls if c[0] == "MARKET"]
    assert [c[2] for c in market_calls] == [pytest.approx(2.0), pytest.approx(1.0)]
    assert spot.balances["BTC"] == pytest.approx(0.0)
    st = store.get_symbol("BTC/USDT")
    assert st.inventory_qty == pytest.approx(0.0)


def test_liquidation_incomplete_fails_closed(tmp_path):
    store, executor, spot = _make_env(tmp_path)
    spot.market_fill_fractions = [0.0, 0.0, 0.0]  # nothing ever executes

    ok = executor.place_market_sell("BTC/USDT", 1.0, ref_price=100.0)

    assert ok is False
    events = store.recent_risk_events()
    assert any(e["event"] == "liquidation_incomplete" for e in events)
    assert len([o for o in _all_orders(store) if o["type"] == "MARKET"]) == 3


def test_liquidation_balance_mismatch_fails_closed(tmp_path):
    store, executor, spot = _make_env(tmp_path)
    spot.adjust_balance = False  # exchange reports FILLED but wallet unchanged
    spot.balances = {"BTC": 5.0}

    ok = executor.place_market_sell("BTC/USDT", 1.0, ref_price=100.0)

    assert ok is False
    events = store.recent_risk_events()
    assert any(e["event"] == "liquidation_balance_mismatch" for e in events)


def test_liquidation_unknown_state_fails_closed(tmp_path):
    store, executor, spot = _make_env(tmp_path)
    spot.fail_next_submit = "pre"

    with pytest.raises(OrderUnknownState):
        executor.place_market_sell("BTC/USDT", 1.0, ref_price=100.0)

    rows = [o for o in _all_orders(store) if o["type"] == "MARKET"]
    assert rows[0]["status"] == "UNKNOWN"


def test_accounting_continues_correctly_after_restart(tmp_path):
    store, executor, spot = _make_env(tmp_path)
    local_id = executor.place_limit("BTC/USDT", "BUY", 100.0, 1.0, target_sell_price=101.0)
    cid = store.get_order(local_id)["client_order_id"]
    spot.fill(cid, 0.4, 100.0, fee=0.04)
    executor.sync_fills("BTC/USDT", None)

    # restart and reconcile, then the order completes
    reopened = StateStore(store.path)
    restarted = LiveExecutor(make_config(dry_run=False), spot, reopened)
    restarted.sync_fills("BTC/USDT", None)
    spot.fill(cid, 0.6, 100.0, fee=0.06)
    restarted.sync_fills("BTC/USDT", None)

    st = reopened.get_symbol("BTC/USDT")
    assert st.inventory_qty == pytest.approx(1.0)
    assert st.avg_cost == pytest.approx(100.0)
    assert reopened.sum_fees("BTC/USDT") == pytest.approx(0.1)
    children = [
        o for o in _all_orders(reopened)
        if o["parent_order_id"] == local_id and o["side"] == "SELL"
    ]
    assert sum(c["qty"] for c in children) == pytest.approx(1.0)
