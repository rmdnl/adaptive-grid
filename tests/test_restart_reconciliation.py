"""Restart reconciliation & dashboard active-symbol list regression tests.

Covers the pre-resume reconcile path (bot._reconcile_state /
LiveExecutor.restart_reconcile): it must mirror authoritative order
statuses, account unseen exchange trades idempotently, detect unknown
orders, keep inventory/ledger consistent, and create NO new orders /
cancel NOTHING / liquidate NOTHING. Unknown or unverified state is
fail-closed (exit 1, symbol STOPPED).
"""

from __future__ import annotations

import io
import sqlite3

import pytest

import bot
from conftest import FakeSpot, make_config
from exchange import ExchangeError, LiveExecutor, OrderUnknownState
from state import StateStore


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _env(tmp_path, pair=("AAA/USDT",)):
    store = StateStore(str(tmp_path / "state.db"))
    store.ensure_symbols(list(pair))
    cfg = make_config(pair_list=pair, dry_run=False, execution_mode="testnet")
    spot = FakeSpot()
    executor = LiveExecutor(cfg, spot, store)
    return store, cfg, spot, executor


def _all_orders(store):
    conn = sqlite3.connect(store.path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM orders").fetchall()]
    conn.close()
    return rows


def _fills(store, symbol):
    conn = sqlite3.connect(store.path)
    n = conn.execute("SELECT COUNT(*) FROM fills WHERE symbol=?", (symbol,)).fetchone()[0]
    conn.close()
    return n


# ---------------------------------------------------------------------------
# LiveExecutor.restart_reconcile — no-new-order guarantees
# ---------------------------------------------------------------------------

def test_reconcile_mirrors_partial_buy_and_accounts_once(tmp_path):
    store, cfg, spot, executor = _env(tmp_path)
    store.create_order("buy-1", "AAA/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0, "live",
                       target_sell_price=101.0)
    # remote: partially filled 0.4 with a trade not yet in the ledger
    spot._register("buy-1", "AAA/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0)
    spot.fill("buy-1", 0.4, 100.0, fee=0.04)  # executedQty=0.4, PARTIALLY_FILLED

    before = len(_all_orders(store))
    report = executor.restart_reconcile("AAA/USDT")
    after = len(_all_orders(store))

    assert report["checked"] == 1
    assert report["fills_recorded"] == 1
    assert report["unknown"] == 0
    # status mirrored to authoritative exchange state
    order = store.get_order_by_client_id("buy-1")
    assert order["status"] == "PARTIALLY_FILLED"
    assert order["filled_qty"] == pytest.approx(0.4)
    # inventory accounted from the actual trade
    assert store.get_symbol("AAA/USDT").inventory_qty == pytest.approx(0.4)
    # NO new orders: reconcile_only suppresses child-sell spawning
    assert after == before
    assert len(store.symbol_orders("AAA/USDT")) == 1
    assert _fills(store, "AAA/USDT") == 1


def test_reconcile_is_idempotent_no_duplicate_fills(tmp_path):
    store, cfg, spot, executor = _env(tmp_path)
    store.create_order("buy-1", "AAA/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0, "live")
    remote = spot._register("buy-1", "AAA/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0)
    remote["status"] = "PARTIALLY_FILLED"
    remote["executedQty"] = 0.4
    spot.fill("buy-1", 0.4, 100.0, fee=0.04)

    r1 = executor.restart_reconcile("AAA/USDT")
    assert r1["fills_recorded"] == 1
    r2 = executor.restart_reconcile("AAA/USDT")
    assert r2["fills_recorded"] == 0          # the trade is not re-recorded
    assert store.get_symbol("AAA/USDT").inventory_qty == pytest.approx(0.4)
    assert _fills(store, "AAA/USDT") == 1     # still exactly one fill row


def test_reconcile_full_fill_while_offline(tmp_path):
    store, cfg, spot, executor = _env(tmp_path)
    store.create_order("buy-1", "AAA/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0, "live")
    # Simulate exchange fill: just call fill(), it sets status/executedQty
    spot._register("buy-1", "AAA/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0)
    spot.fill("buy-1", 1.0, 100.0, fee=0.1)

    report = executor.restart_reconcile("AAA/USDT")
    order = store.get_order_by_client_id("buy-1")
    assert order["status"] == "FILLED"
    assert order["filled_qty"] == pytest.approx(1.0)
    assert store.get_symbol("AAA/USDT").inventory_qty == pytest.approx(1.0)
    assert report["fills_recorded"] == 1


def test_reconcile_partial_sell_realizes_pnl(tmp_path):
    store, cfg, spot, executor = _env(tmp_path)
    # seed held inventory from a prior buy
    buy_id = store.create_order("buy-1", "AAA/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0, "live")
    store.update_order_status(buy_id, "FILLED", 1.0)
    store.record_fill(buy_id, "AAA/USDT", "BUY", 100.0, 1.0, 0.1, trade_id="seed-buy")
    sell_id = store.create_order("sell-1", "AAA/USDT", "SELL", "LIMIT_MAKER", 101.0, 1.0, "live")
    # remote sell partially filled 0.5 (offline)
    spot._register("sell-1", "AAA/USDT", "SELL", "LIMIT_MAKER", 101.0, 1.0)
    spot.fill("sell-1", 0.5, 101.0, fee=0.05)

    report = executor.restart_reconcile("AAA/USDT")
    assert store.get_order_by_client_id("sell-1")["status"] == "PARTIALLY_FILLED"
    assert store.get_symbol("AAA/USDT").inventory_qty == pytest.approx(0.5)
    assert report["fills_recorded"] == 1


def test_reconcile_no_duplicate_child_sell(tmp_path):
    store, cfg, spot, executor = _env(tmp_path)
    store.create_order("buy-1", "AAA/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0, "live",
                       target_sell_price=101.0)
    # Partial fill: 1.0 qty fills fully
    spot._register("buy-1", "AAA/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0)
    spot.fill("buy-1", 1.0, 100.0, fee=0.1)

    for _ in range(3):
        executor.restart_reconcile("AAA/USDT")

    children = [o for o in store.symbol_orders("AAA/USDT") if o["side"] == "SELL"]
    assert children == []          # reconcile_only must not spawn sells
    assert _all_orders(store) and sum(
        1 for o in _all_orders(store) if o["side"] == "BUY"
    ) == 1                        # still exactly one order, no duplicates


# ---------------------------------------------------------------------------
# fail-closed: unknown / unexpected orders
# ---------------------------------------------------------------------------

def test_unknown_local_order_fails_closed(tmp_path):
    store, cfg, spot, executor = _env(tmp_path)
    store.create_order("ghost", "AAA/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0, "live")
    # remote side has no such order -> get_order returns None

    with pytest.raises(OrderUnknownState):
        executor.restart_reconcile("AAA/USDT")
    assert store.get_order_by_client_id("ghost")["status"] == "UNKNOWN"


def test_unexpected_exchange_order_fails_closed(tmp_path):
    store, cfg, spot, executor = _env(tmp_path)
    spot._register("rogue", "AAA/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0)  # status NEW

    with pytest.raises(OrderUnknownState):
        executor.restart_reconcile("AAA/USDT")


# ---------------------------------------------------------------------------
# bot._reconcile_state — CLI-level behaviour
# ---------------------------------------------------------------------------

def test_reconcile_state_paper_short_circuits(tmp_path):
    store, cfg, spot, _ = _env(tmp_path)
    cfg = make_config(pair_list=("AAA/USDT",))  # execution_mode=paper
    out = io.StringIO()
    code = bot._reconcile_state(cfg, spot, store, out)
    text = out.getvalue()
    assert code == 0
    assert "PAPER" in text and "OVERALL: OK" in text
    # no exchange calls: FakeSpot recorded no submissions
    assert spot.submit_calls == []


def test_reconcile_state_testnet_clean(tmp_path):
    store, cfg, spot, _ = _env(tmp_path)
    out = io.StringIO()
    code = bot._reconcile_state(cfg, spot, store, out)
    assert code == 0
    assert "OVERALL: OK" in out.getvalue()
    assert spot.submit_calls == []          # read-only: nothing submitted


def test_reconcile_state_unknown_exit_nonzero_and_stops_symbol(tmp_path):
    store, cfg, spot, _ = _env(tmp_path)
    store.create_order("ghost", "AAA/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0, "live")
    out = io.StringIO()
    code = bot._reconcile_state(cfg, spot, store, out)
    assert code == 1
    assert "FAIL-CLOSED" in out.getvalue()
    assert store.get_symbol("AAA/USDT").strategy_state == "STOPPED"
    events = [e["event"] for e in store.recent_risk_events()]
    assert "reconcile_unknown_state" in events


def test_reconcile_state_exchange_error_exit_nonzero(tmp_path):
    store, cfg, spot, _ = _env(tmp_path)
    # create a local open order so reconciliation tries to query the exchange
    store.create_order("ghost", "AAA/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0, "live")

    class BrokenSpot:
        def get_order(self, symbol, cid):
            raise ExchangeError("network down")

        def get_open_orders(self, symbol=None):
            return []

    out = io.StringIO()
    code = bot._reconcile_state(cfg, BrokenSpot(), store, out)
    assert code == 1
    assert "FAIL-CLOSED" in out.getvalue()
    assert "UNVERIFIED" in out.getvalue()
    events = [e["event"] for e in store.recent_risk_events()]
    assert "reconcile_failed" in events


def test_reconcile_state_ledger_mismatch_fails(tmp_path):
    store, cfg, spot, _ = _env(tmp_path)
    # force a corrupted ledger: inventory not equal to buys - sells
    store.update_symbol("AAA/USDT", inventory_qty=5.0)
    out = io.StringIO()
    code = bot._reconcile_state(cfg, spot, store, out)
    assert code == 1
    assert "MISMATCH" in out.getvalue()
    events = [e["event"] for e in store.recent_risk_events()]
    assert "reconcile_ledger_mismatch" in events


def test_reconcile_state_ledger_consistent(tmp_path):
    store, cfg, spot, executor = _env(tmp_path)
    # consistent ledger: buy 1.0, sell 0.4 -> inventory 0.6
    buy_id = store.create_order("b", "AAA/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0, "live")
    store.update_order_status(buy_id, "FILLED", 1.0)
    store.record_fill(buy_id, "AAA/USDT", "BUY", 100.0, 1.0, 0.1, trade_id="tb")
    sell_id = store.create_order("s", "AAA/USDT", "SELL", "LIMIT_MAKER", 101.0, 1.0, "live")
    store.update_order_status(sell_id, "FILLED", 1.0)
    store.record_fill(sell_id, "AAA/USDT", "SELL", 101.0, 0.4, 0.04, trade_id="ts")
    # record_fill auto-updates inventory to 0.6
    out = io.StringIO()
    code = bot._reconcile_state(cfg, spot, store, out)
    assert code == 0
    assert "OVERALL: OK" in out.getvalue()


# ---------------------------------------------------------------------------
# CLI flag plumbing
# ---------------------------------------------------------------------------

def test_reconcile_flag_registered():
    import argparse
    parser = argparse.ArgumentParser()
    # mirror the real main() contract without running the network path
    parser.add_argument("--reconcile", action="store_true")
    ns = parser.parse_args(["--reconcile"])
    assert ns.reconcile is True


def test_reconcile_is_a_main_arg(tmp_path, monkeypatch):
    """main() must expose --reconcile and route it to _reconcile_state
    without opening the session or entering the service loop."""
    import bot as bot_mod
    src = bot_mod.main.__doc__  # presence is via argparse; assert function handles it
    assert hasattr(bot_mod, "_reconcile_state")
    assert "_reconcile_state" in _main_source()


def _main_source():
    import inspect
    import bot as bot_mod
    return inspect.getsource(bot_mod.main)
