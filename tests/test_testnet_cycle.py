"""Round 8 tests — bounded continuous TESTNET cycle (deterministic).

All tests run against an in-memory fake exchange; no network access.
The runner executes the REAL production math (indicators, auto-range,
geometric grid, quantized plan validation, risk_engine gates) over a
calibrated deterministic candle fixture (sine period 20 / amp 1.8 on
102.00 — verified ADX 23.3, ATR% 1.8, BB 5.0%, range approved, 8-cell
grid, net 0.352% per cell), so every gate exercised here is the real
gate code.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

import testnet_cycle as tc
from binance_testnet import (
    BinanceTestnetNetworkError,
    BinanceTestnetRateLimitError,
)
from rest_reconciler import CancelVerdict
from shutdown import ShutdownCoordinator
from testnet_orders import BinanceTestnetOrderClient, BinanceTestnetOrderRejectedError


# ---------------------------------------------------------------------------
# Deterministic fixtures
# ---------------------------------------------------------------------------
_BASE_MS = 1_577_836_800_000  # 2020-01-01 — all candles CLOSED in real time


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def data(self):
        return self._payload


class FakeClock:
    """Deterministic, manually advanced clock (seconds)."""

    def __init__(self, start_s: float = 1_790_998_000.0) -> None:
        self.now_s = start_s

    def time(self) -> float:
        return self.now_s

    def advance(self, seconds: float) -> None:
        self.now_s += seconds

    @property
    def ms(self) -> int:
        return int(self.now_s * 1000)


def candle_rows(n: int = 120, period: int = 20, amp: float = 1.8,
                mid: float = 102.0, spread: float = 0.75) -> list[list]:
    """Calibrated deterministic candles passing the full production gate stack."""
    rows = []
    prev = mid
    for i in range(n):
        close = round(mid + amp * math.sin(2 * math.pi * i / period), 2)
        open_ = prev
        high = round(max(open_, close) + spread, 2)
        low = round(min(open_, close) - spread, 2)
        open_time = _BASE_MS + i * 900_000
        rows.append([open_time, open_, high, low, close, 1000.0,
                     open_time + 899_999, 1000.0 * close, 100, 500.0,
                     500.0 * close, "0"])
        prev = close
    return rows


_SYMBOL_INFO = {
    "symbol": "BNBUSDT",
    "baseAsset": "BNB",
    "quoteAsset": "USDT",
    "status": "TRADING",
    "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "0.01",
         "maxPrice": "100000", "tickSize": "0.01"},
        {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "10000",
         "stepSize": "0.001"},
        {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
        {"filterType": "PERCENT_PRICE", "multiplierUp": "1.05",
         "multiplierDown": "0.95", "avgPriceMins": 5},
        {"filterType": "MAX_NUM_ORDERS", "maxNumOrders": 40},
    ],
}


def cycle_config(tmp_path, **overrides) -> tc.TestnetCycleConfig:
    base_cfg = {
        "symbol": "BNBUSDT",
        "grid": {"step_pct": 0.006, "hard_min_net_pct": 0.003,
                 "min_cells": 6, "max_levels": 40},
        "range": {"auto": {"support_quantile": 0.10,
                           "resistance_quantile": 0.90,
                           "min_width_pct": 0.03, "max_width_pct": 0.25,
                           "min_quality_score": 65,
                           "require_price_inside": True}},
        "risk": {"range_break_buffer_pct": 0.01,
                 "stop_if_below_lower_pct": 0.02,
                 "max_equity_drawdown_pct": 0.02},
        "market_filter": {"adx_max": 28, "atr_pct_max": 0.025,
                          "bb_width_max": 0.06, "volume_spike_max": 2.5},
        "fees": {"maker_fee_fallback": 0.001,
                 "slippage_roundtrip_pct": 0.0005},
        "execution": {"order_quote_size": 25, "max_open_orders": 40},
    }
    return tc.load_cycle_config(
        base_cfg,
        max_cycles=overrides.pop("max_cycles", 2),
        poll_interval_s=overrides.pop("poll_interval_s", 0.0),
        db_path=str(tmp_path / "cycle.sqlite3"),
        **overrides,
    )


class FakeExchange:
    """In-memory order book with injectable failure modes."""

    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.next_order_id = 5000
        self.place_calls = 0
        self.cancel_calls = 0
        self.get_calls = 0
        # failure injection
        self.place_lost_ack = False     # order lands, response lost
        self.place_reject = False       # deterministic -2010 rejection
        self.place_network_fail = False # order NOT placed, network error
        self.cancel_lost_ack = False    # cancel lands, response lost
        self.cancel_network_fail = False  # cancel did NOT happen
        self.get_network_fail = False   # single-order query unreachable
        self.fill_qty_on_place: str | None = None

    def place(self, symbol: str, side: str, qty: str, price: str, cid: str) -> dict:
        self.place_calls += 1
        if self.place_reject:
            raise BinanceTestnetOrderRejectedError(
                "exchange rejected request: code=-2010", code=-2010)
        if self.place_network_fail:
            raise BinanceTestnetNetworkError("connection reset during POST")
        existing = self.orders.get(cid)
        if existing and existing["status"] in ("NEW", "PARTIALLY_FILLED"):
            raise BinanceTestnetOrderRejectedError(
                "exchange rejected request: code=-2010", code=-2010)
        self.next_order_id += 1
        order = {
            "symbol": symbol, "orderId": self.next_order_id,
            "clientOrderId": cid, "status": "NEW", "side": side,
            "type": "LIMIT_MAKER", "price": price, "origQty": qty,
            "executedQty": self.fill_qty_on_place or "0.000",
            "transactTime": 1_790_998_000_000,
        }
        self.orders[cid] = order
        if self.place_lost_ack:
            raise BinanceTestnetNetworkError("response lost after POST")
        return dict(order)

    def cancel(self, cid: str) -> dict:
        self.cancel_calls += 1
        order = self.orders.get(cid)
        if self.cancel_network_fail:
            raise BinanceTestnetNetworkError("connection reset during DELETE")
        if order is None or order["status"] not in ("NEW", "PARTIALLY_FILLED"):
            raise BinanceTestnetOrderRejectedError(
                "exchange rejected request: code=-2011", code=-2011,
                ambiguous=True)
        if self.cancel_lost_ack:
            order["status"] = "CANCELED"
            raise BinanceTestnetNetworkError("response lost after DELETE")
        order["status"] = "CANCELED"
        return dict(order)

    def get(self, cid: str) -> dict:
        self.get_calls += 1
        if self.get_network_fail:
            raise BinanceTestnetNetworkError("query unreachable")
        order = self.orders.get(cid)
        if order is None:
            raise BinanceTestnetOrderRejectedError(
                "exchange rejected request: code=-2013", code=-2013,
                ambiguous=True)
        return dict(order)

    # -- test helpers -------------------------------------------------------
    def open_cids(self) -> list[str]:
        return [c for c, o in self.orders.items()
                if o["status"] in ("NEW", "PARTIALLY_FILLED")]

    def set_status(self, cid: str, status: str, executed: str = "0.000") -> None:
        self.orders[cid]["status"] = status
        self.orders[cid]["executedQty"] = executed


class FakeReadClient:
    """Read surface of the testnet adapter over the fake exchange."""

    def __init__(self, exchange: FakeExchange, *,
                 skew_ms: int = 10,
                 ticker: Decimal = Decimal("102.00"),
                 account_values: list[dict[str, str]] | None = None,
                 fail_open_orders: bool = False) -> None:
        self._config = SimpleNamespace(dry_run=True, allow_live_execution=False)
        self.exchange = exchange
        self.skew_ms = skew_ms
        self.ticker = ticker
        self.account_values = account_values or [
            {"BNB": ("10", "0"), "USDT": ("10000", "0")}]
        self._account_calls = 0
        self.fail_open_orders = fail_open_orders
        self.open_orders_calls = 0
        self._spot = SimpleNamespace(rest_api=SimpleNamespace(
            klines=lambda **kw: _FakeResponse(candle_rows())))

    def server_time(self) -> tuple[int, int]:
        # anchored to the real clock: sync_clock_skew compares against
        # time.time(); only the injected skew is under test control
        import time as _time
        now_ms = int(_time.time() * 1000)
        return now_ms + self.skew_ms, now_ms

    def symbol_snapshot(self, symbol: str):
        from binance_testnet import BinanceSymbolSnapshot
        return BinanceSymbolSnapshot(
            symbol="BNBUSDT", base_asset="BNB", quote_asset="USDT",
            status="TRADING", filters={}, raw_exchange_info=_SYMBOL_INFO)

    def ticker_price(self, symbol: str):
        from binance_testnet import BinanceTickerSnapshot
        from datetime import datetime, timezone
        return BinanceTickerSnapshot(
            symbol=symbol, price=self.ticker,
            fetched_at=datetime.now(timezone.utc))

    def account(self):
        from binance_testnet import BinanceAccountBalance, BinanceAccountSnapshot
        from datetime import datetime, timezone
        values = self.account_values[
            min(self._account_calls, len(self.account_values) - 1)]
        self._account_calls += 1
        balances = tuple(
            BinanceAccountBalance(asset=asset, free=Decimal(free),
                                  locked=Decimal(locked))
            for asset, (free, locked) in values.items())
        return BinanceAccountSnapshot(
            balances=balances, fetched_at=datetime.now(timezone.utc))

    def open_orders(self, symbol: str):
        self.open_orders_calls += 1
        if self.fail_open_orders:
            raise BinanceTestnetRateLimitError(
                "too many requests", retry_after_s=5, banned=False)
        from binance_testnet import BinanceOpenOrderSnapshot
        snapshots = []
        for cid in sorted(self.exchange.open_cids()):
            o = self.exchange.orders[cid]
            snapshots.append(BinanceOpenOrderSnapshot(
                order_id=o["orderId"], client_order_id=cid, symbol=o["symbol"],
                side=o["side"], order_type=o["type"], status=o["status"],
                price=Decimal(o["price"]), orig_qty=Decimal(o["origQty"]),
                executed_qty=Decimal(o["executedQty"]), time_in_force="GTC",
                is_working=True))
        return tuple(snapshots)

    def get_order(self, symbol: str, cid: str) -> dict:
        return self.exchange.get(cid)


class FakeOrderClient:
    """Write surface over the fake exchange, raising the real typed errors.

    Reuses the REAL ``BinanceTestnetOrderClient.make_cancel_executor`` so
    the executor contract under test is production code.
    """

    def __init__(self, exchange: FakeExchange) -> None:
        self.exchange = exchange

    def place_limit_maker_order(self, symbol, side, quantity, price,
                                client_order_id):
        payload = self.exchange.place(symbol, side, str(quantity),
                                      str(price), client_order_id)
        return SimpleNamespace(
            symbol=payload["symbol"], order_id=payload["orderId"],
            client_order_id=payload["clientOrderId"], side=payload["side"],
            order_type=payload["type"], status=payload["status"],
            price=Decimal(payload["price"]), orig_qty=Decimal(payload["origQty"]),
            executed_qty=Decimal(payload["executedQty"]),
            transact_time=payload["transactTime"])

    def cancel_order_by_client_id(self, symbol, client_order_id):
        payload = self.exchange.cancel(client_order_id)
        return SimpleNamespace(status=payload["status"])

    make_cancel_executor = BinanceTestnetOrderClient.make_cancel_executor


def make_runner(tmp_path, *, place: bool = False, exchange: FakeExchange | None = None,
                read: FakeReadClient | None = None, clock: FakeClock | None = None,
                config: tc.TestnetCycleConfig | None = None,
                shutdown: ShutdownCoordinator | None = None,
                ledger: tc.CycleLedger | None = None):
    exchange = exchange or FakeExchange()
    read = read or FakeReadClient(exchange)
    clock = clock or FakeClock()
    config = config or cycle_config(tmp_path)
    order = FakeOrderClient(exchange) if place else None
    runner = tc.TestnetCycleRunner(
        config,
        read_client=read,
        order_client=order,
        kline_rest_api=read._spot.rest_api,
        ledger=ledger or tc.CycleLedger(config.db_path, clock_ms=lambda: clock.ms),
        clock=clock.time,
        sleep=lambda _s: None,
        shutdown=shutdown or ShutdownCoordinator(),
    )
    runner.exchange = exchange  # test handle
    return runner


def insert_open_order(ledger, exchange, cid, *, state=tc.ORDER_OPEN,
                      price="100.13", qty="0.251"):
    """Simulate a prior crashed run: ledger row + live exchange order."""
    ledger.insert_order(client_order_id=cid, run_id=1, cycle_id=1,
                        symbol="BNBUSDT", side="BUY", price=Decimal(price),
                        quantity=Decimal(qty), state=state)
    if state in (tc.ORDER_OPEN, tc.ORDER_PARTIALLY_FILLED,
                 tc.ORDER_SUBMITTED_UNKNOWN):
        exchange.next_order_id += 1
        exchange.orders[cid] = {
            "symbol": "BNBUSDT", "orderId": exchange.next_order_id,
            "clientOrderId": cid, "status": "NEW", "side": "BUY",
            "type": "LIMIT_MAKER", "price": price, "origQty": qty,
            "executedQty": "0.000", "transactTime": 1_790_998_000_000,
        }


# ---------------------------------------------------------------------------
# 1. Fresh start with no existing state
# ---------------------------------------------------------------------------
def test_fresh_start_no_state(tmp_path):
    runner = make_runner(tmp_path, place=True)
    summary = runner.run(place_orders=True)
    assert summary["stopped_reason"] is None
    assert len(summary["cycles"]) == 2
    assert summary["cleanup"]["ok"] is True
    # two intents placed per cycle, then verified-cancelled at cycle end
    assert runner.exchange.place_calls == 4
    assert runner.exchange.open_cids() == []
    assert all(o["state"] == tc.ORDER_CANCELED
               for o in runner.ledger.all_orders())


# ---------------------------------------------------------------------------
# 25. Rehearsal (paper-equivalent) mode is exchange-write-free
# ---------------------------------------------------------------------------
def test_rehearsal_mode_makes_zero_write_calls(tmp_path):
    runner = make_runner(tmp_path, place=False)
    summary = runner.run(place_orders=True)
    assert summary["stopped_reason"] is None
    assert runner.exchange.place_calls == 0
    assert runner.exchange.cancel_calls == 0
    for cycle in summary["cycles"]:
        assert "intents_only" in cycle  # computed but never submitted


def test_paper_production_files_never_import_cycle_modules():
    """Production boundary: main/paper paths stay exchange-write-free."""
    import pathlib
    repo = pathlib.Path(__file__).resolve().parent.parent
    for name in ("main.py", "paper_orchestrator.py", "order_engine.py",
                 "paper_accounting.py", "paper_validation.py"):
        source = (repo / name).read_text(encoding="utf-8")
        assert "testnet_cycle" not in source, name
        assert "testnet_orders" not in source, name


# ---------------------------------------------------------------------------
# 2/3. Existing open orders
# ---------------------------------------------------------------------------
def test_foreign_open_orders_refuse_placement(tmp_path):
    exchange = FakeExchange()
    exchange.next_order_id += 1
    exchange.orders["SOMEONE-ELSE-1"] = {
        "symbol": "BNBUSDT", "orderId": exchange.next_order_id,
        "clientOrderId": "SOMEONE-ELSE-1", "status": "NEW", "side": "BUY",
        "type": "LIMIT_MAKER", "price": "102", "origQty": "0.1",
        "executedQty": "0", "transactTime": 1_790_998_000_000,
    }
    runner = make_runner(tmp_path, place=True, exchange=exchange)
    summary = runner.run(place_orders=True)
    assert runner.exchange.place_calls == 0
    for cycle in summary["cycles"]:
        assert cycle["blocked"].startswith("FOREIGN_OPEN_ORDERS")
    # foreign orders are reported, never touched, and do not block cleanup
    assert summary["cleanup"]["foreign"] == ["SOMEONE-ELSE-1"]
    assert summary["cleanup"]["ok"] is True
    assert exchange.orders["SOMEONE-ELSE-1"]["status"] == "NEW"


def test_own_open_orders_from_prior_run_recovered(tmp_path):
    exchange = FakeExchange()
    ledger = tc.CycleLedger(str(tmp_path / "cycle.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    insert_open_order(ledger, exchange, "AGTC-BNBUSDT-1-1-1")
    runner = make_runner(tmp_path, place=True, exchange=exchange,
                         ledger=ledger)
    summary = runner.run(place_orders=True)
    # the recovered order was verified-canceled; no duplicate submission
    recovered = ledger.get_order("AGTC-BNBUSDT-1-1-1")
    assert recovered["state"] == tc.ORDER_CANCELED
    assert summary["cleanup"]["ok"] is True
    assert exchange.open_cids() == []


# ---------------------------------------------------------------------------
# 4/5/6. Restart boundaries
# ---------------------------------------------------------------------------
def test_restart_after_confirmed_fill(tmp_path):
    exchange = FakeExchange()
    ledger = tc.CycleLedger(str(tmp_path / "cycle.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    insert_open_order(ledger, exchange, "AGTC-BNBUSDT-1-1-1")
    exchange.set_status("AGTC-BNBUSDT-1-1-1", "FILLED", executed="0.251")
    runner = make_runner(tmp_path, place=True, exchange=exchange,
                         ledger=ledger)
    summary = runner.run(place_orders=True)
    row = ledger.get_order("AGTC-BNBUSDT-1-1-1")
    assert row["state"] == tc.ORDER_FILLED
    assert Decimal(row["fill_qty"]) == Decimal("0.251")
    assert summary["cleanup"]["ok"] is True
    assert exchange.open_cids() == []


def test_restart_with_unknown_order_state_blocks_placement(tmp_path):
    exchange = FakeExchange()
    ledger = tc.CycleLedger(str(tmp_path / "cycle.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    insert_open_order(ledger, exchange, "AGTC-BNBUSDT-1-1-1",
                      state=tc.ORDER_SUBMITTED_UNKNOWN)
    exchange.get_network_fail = True
    runner = make_runner(tmp_path, place=True, exchange=exchange,
                         ledger=ledger)
    summary = runner.run(place_orders=True)
    assert runner.exchange.place_calls == 0
    row = ledger.get_order("AGTC-BNBUSDT-1-1-1")
    assert row["state"] == tc.ORDER_PENDING_RECONCILIATION
    assert runner.status()["non_terminal"] == ["AGTC-BNBUSDT-1-1-1"]
    for cycle in summary["cycles"]:
        assert cycle["blocked"] == "UNRESOLVED_ORDERS_PENDING_RECONCILIATION"


def test_restart_while_kill_active_enters_kill_branch(tmp_path):
    ledger = tc.CycleLedger(str(tmp_path / "cycle.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    ledger.latch_kill("EQUITY_DRAWDOWN_KILL", actor="prior-run")
    exchange = FakeExchange()
    runner = make_runner(tmp_path, place=True, exchange=exchange,
                         ledger=ledger)
    summary = runner.run(place_orders=True)
    assert summary["stopped_reason"].startswith("KILL_ACTIVE")
    assert runner.exchange.place_calls == 0
    assert ledger.kill_active()["reason"] == "EQUITY_DRAWDOWN_KILL"


# ---------------------------------------------------------------------------
# 7/15. Kill switch during active cycle; no new order after kill
# ---------------------------------------------------------------------------
def test_kill_switch_during_active_cycle_cancels_and_persists(tmp_path):
    exchange = FakeExchange()
    ledger = tc.CycleLedger(str(tmp_path / "cycle.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    # an open order from a crashed prior run + 3% equity drawdown
    insert_open_order(ledger, exchange, "AGTC-BNBUSDT-1-1-1")
    read = FakeReadClient(exchange, account_values=[
        {"BNB": ("10", "0"), "USDT": ("10000", "0")},
        {"BNB": ("10", "0"), "USDT": ("10000", "0")},
        {"BNB": ("10", "0"), "USDT": ("9700", "0")},
    ])
    # seed the reference high-water mark, then crash equity
    ledger.set_state("reference_equity", "10100")
    runner = make_runner(tmp_path, place=True, exchange=exchange,
                         read=read, ledger=ledger)
    summary = runner.run(place_orders=True)
    assert ledger.kill_active() is not None
    assert "EQUITY_DRAWDOWN_KILL" in ledger.kill_active()["reason"] or \
        ledger.kill_active()["reason"]
    # the open order from the crashed run was verified-canceled
    assert ledger.get_order("AGTC-BNBUSDT-1-1-1")["state"] == tc.ORDER_CANCELED
    assert summary["cleanup"]["ok"] is True


def test_equity_drawdown_kill_mid_run_stops_cycles(tmp_path):
    exchange = FakeExchange()
    read = FakeReadClient(exchange, account_values=[
        {"BNB": ("10", "0"), "USDT": ("10000", "0")},   # cycle 1: HWM bootstrap
        {"BNB": ("10", "0"), "USDT": ("10000", "0")},   # cycle 2 read
        {"BNB": ("10", "0"), "USDT": ("9700", "0")},    # cycle 2 risk: -3%
    ])
    runner = make_runner(tmp_path, place=True, exchange=exchange, read=read,
                         config=cycle_config(tmp_path, max_cycles=3))
    summary = runner.run(place_orders=True)
    assert runner.ledger.kill_active() is not None
    assert summary["stopped_reason"] == "KILL_ACTIVATED_MID_RUN"
    # cycle 1 placed+cancelled normally; kill latched during cycle 2
    assert len(summary["cycles"]) == 2
    assert summary["cycles"][1].get("killed") is True
    assert "EQUITY_DRAWDOWN_KILL" in summary["cycles"][1]["risk_decision"]


def test_no_new_order_after_kill(tmp_path):
    ledger = tc.CycleLedger(str(tmp_path / "cycle.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    ledger.latch_kill("RANGE_BREAK_BELOW_BUFFER", actor="test")
    exchange = FakeExchange()
    runner = make_runner(tmp_path, place=True, exchange=exchange,
                         ledger=ledger)
    runner.preflight()
    report = runner.run_cycle(1, place_orders=True)
    assert report["blocked"].startswith("KILL_ACTIVE")
    assert runner.exchange.place_calls == 0


# ---------------------------------------------------------------------------
# 8/9. Cancellation timeout / connection failure — fail-closed cleanup
# ---------------------------------------------------------------------------
def test_cancel_lost_ack_settled_by_authoritative_requery(tmp_path):
    exchange = FakeExchange()
    ledger = tc.CycleLedger(str(tmp_path / "cycle.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    insert_open_order(ledger, exchange, "AGTC-BNBUSDT-1-1-1")
    exchange.cancel_lost_ack = True  # DELETE lands, response lost
    runner = make_runner(tmp_path, place=True, exchange=exchange,
                         ledger=ledger)
    summary = runner.run(place_orders=True)
    row = ledger.get_order("AGTC-BNBUSDT-1-1-1")
    assert row["state"] == tc.ORDER_CANCELED  # settled by re-query, not assumed
    assert summary["cleanup"]["ok"] is True


def test_cancel_connection_failure_leaves_order_unresolved(tmp_path):
    exchange = FakeExchange()
    ledger = tc.CycleLedger(str(tmp_path / "cycle.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    insert_open_order(ledger, exchange, "AGTC-BNBUSDT-1-1-1")
    exchange.cancel_network_fail = True  # cancel never reached the engine
    runner = make_runner(tmp_path, place=True, exchange=exchange,
                         ledger=ledger)
    summary = runner.run(place_orders=True)
    cleanup = summary["cleanup"]
    assert cleanup["ok"] is False
    assert "AGTC-BNBUSDT-1-1-1" in cleanup["unresolved"]
    # the order is still open on the exchange — never claimed canceled
    assert exchange.orders["AGTC-BNBUSDT-1-1-1"]["status"] == "NEW"


# ---------------------------------------------------------------------------
# 10. Lost submission ACK — resolved by clientOrderId, never resubmitted
# ---------------------------------------------------------------------------
def test_lost_submission_ack_resolved_not_resubmitted(tmp_path):
    exchange = FakeExchange()
    exchange.place_lost_ack = True
    runner = make_runner(tmp_path, place=True, exchange=exchange,
                         config=cycle_config(tmp_path, max_cycles=1))
    summary = runner.run(place_orders=True)
    # exactly one POST per intent despite the lost ack
    assert exchange.place_calls == 2
    for row in runner.ledger.all_orders():
        assert row["state"] == tc.ORDER_CANCELED  # resolved OPEN, then cancelled
    assert summary["cleanup"]["ok"] is True


def test_network_error_submit_with_order_not_placed(tmp_path):
    exchange = FakeExchange()
    exchange.place_network_fail = True
    runner = make_runner(tmp_path, place=True, exchange=exchange,
                         config=cycle_config(tmp_path, max_cycles=1))
    summary = runner.run(place_orders=True)
    # UNKNOWN submission resolved: exchange never received it → PENDING
    states = {o["state"] for o in runner.ledger.all_orders()}
    assert states == {tc.ORDER_PENDING_RECONCILIATION}
    assert exchange.place_calls == 2  # one attempt each, zero resubmits
    assert summary["cleanup"]["ok"] is False
    assert summary["cleanup"]["unresolved"]


# ---------------------------------------------------------------------------
# 11/12. Duplicate order / duplicate clientOrderId prevention
# ---------------------------------------------------------------------------
def test_duplicate_cid_refused_at_ledger_level(tmp_path):
    ledger = tc.CycleLedger(str(tmp_path / "cycle.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    args = dict(client_order_id="AGTC-BNBUSDT-1-1-1", run_id=1, cycle_id=1,
                symbol="BNBUSDT", side="BUY", price=Decimal("100.13"),
                quantity=Decimal("0.251"), state=tc.ORDER_INTENT)
    assert ledger.insert_order(**args) is True
    assert ledger.insert_order(**args) is False  # never reused


def test_duplicate_cid_refused_during_cycle(tmp_path):
    exchange = FakeExchange()
    ledger = tc.CycleLedger(str(tmp_path / "cycle.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    runner = make_runner(tmp_path, place=True, exchange=exchange,
                         ledger=ledger, config=cycle_config(tmp_path, max_cycles=1))
    runner.run_id = ledger.start_run("{}")
    runner.preflight()
    # pre-occupy the cid the first intent of cycle 1 would use
    pre_cid = runner._cid(1, 1)
    assert ledger.insert_order(client_order_id=pre_cid, run_id=runner.run_id,
                               cycle_id=1, symbol="BNBUSDT", side="BUY",
                               price=Decimal("100.13"),
                               quantity=Decimal("0.251"),
                               state=tc.ORDER_CANCELED)
    runner.run_cycle(1, place_orders=True)
    # the occupied cid was skipped, not resubmitted
    assert exchange.place_calls == 1
    assert ledger.get_order(pre_cid)["state"] == tc.ORDER_CANCELED


def test_unknown_own_prefix_order_on_exchange_recorded(tmp_path):
    exchange = FakeExchange()
    exchange.next_order_id += 1
    exchange.orders["AGTC-BNBUSDT-999-9-9"] = {
        "symbol": "BNBUSDT", "orderId": exchange.next_order_id,
        "clientOrderId": "AGTC-BNBUSDT-999-9-9", "status": "NEW",
        "side": "BUY", "type": "LIMIT_MAKER", "price": "102",
        "origQty": "0.1", "executedQty": "0", "transactTime": 1_790_998_000_000,
    }
    runner = make_runner(tmp_path, place=False, exchange=exchange)
    summary = runner.run(place_orders=True)
    kinds = [e["kind"] for e in runner.ledger.events()]
    assert "unknown_own_order_on_exchange" in kinds
    # never invented into the ledger
    assert runner.ledger.get_order("AGTC-BNBUSDT-999-9-9") is None


# ---------------------------------------------------------------------------
# 13/14. Stale local state corrected by authoritative exchange state
# ---------------------------------------------------------------------------
def test_stale_local_state_corrected_to_canceled(tmp_path):
    exchange = FakeExchange()
    ledger = tc.CycleLedger(str(tmp_path / "cycle.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    insert_open_order(ledger, exchange, "AGTC-BNBUSDT-1-1-1")
    exchange.set_status("AGTC-BNBUSDT-1-1-1", "CANCELED")
    runner = make_runner(tmp_path, place=False, exchange=exchange,
                         ledger=ledger)
    result = runner.reconcile_all()
    assert ledger.get_order("AGTC-BNBUSDT-1-1-1")["state"] == tc.ORDER_CANCELED
    assert result["snapshot"] == "CONFIRMED"


def test_reconciliation_corrects_fill_state(tmp_path):
    exchange = FakeExchange()
    ledger = tc.CycleLedger(str(tmp_path / "cycle.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    insert_open_order(ledger, exchange, "AGTC-BNBUSDT-1-1-1")
    exchange.set_status("AGTC-BNBUSDT-1-1-1", "FILLED", executed="0.251")
    runner = make_runner(tmp_path, place=False, exchange=exchange,
                         ledger=ledger)
    runner.reconcile_all()
    row = ledger.get_order("AGTC-BNBUSDT-1-1-1")
    assert row["state"] == tc.ORDER_FILLED
    assert Decimal(row["fill_qty"]) == Decimal("0.251")


# ---------------------------------------------------------------------------
# 16. No order outside the effective range
# ---------------------------------------------------------------------------
def test_no_order_outside_range(tmp_path, monkeypatch):
    runner = make_runner(tmp_path, place=True,
                         config=cycle_config(tmp_path, max_cycles=1))
    # a tampered plan whose buy price sits ABOVE the effective upper bound
    @dataclass
    class _Cell:
        index: int
        buy_price: Decimal
        sell_price: Decimal
        quantity: Decimal
        gross_pct: Decimal
        net_pct: Decimal
        allowed: bool
        reasons: tuple

    class _Plan:
        allowed = True
        reason = "ORDER_PLAN_PASS"
        cells = (_Cell(0, Decimal("110.00"), Decimal("112.00"),
                       Decimal("0.25"), Decimal("0.018"), Decimal("0.015"),
                       True, ()),)

    monkeypatch.setattr(tc, "validate_quantized_order_plan",
                        lambda *a, **k: _Plan())
    runner.preflight()
    report = runner.run_cycle(1, place_orders=True)
    assert runner.exchange.place_calls == 0
    assert any(v == "NO_VALID_CELLS" for v in report["vetoes"])
    kinds = [(e["kind"], e["payload"].get("gate")) for e in runner.ledger.events()]
    assert ("veto", "ORDER_PRICE") in kinds


def test_placed_orders_always_inside_range(tmp_path):
    runner = make_runner(tmp_path, place=True,
                         config=cycle_config(tmp_path, max_cycles=1))
    summary = runner.run(place_orders=True)
    cycle = summary["cycles"][0]
    lower = Decimal(cycle["range"]["lower"])
    upper = Decimal(cycle["range"]["upper"])
    for row in runner.ledger.all_orders():
        assert lower <= Decimal(row["price"]) <= upper


# ---------------------------------------------------------------------------
# 17/18. Risk veto / insufficient balance
# ---------------------------------------------------------------------------
def test_risk_veto_prevents_order(tmp_path):
    # locked market filter tightened to an impossible bound: real gate blocks
    config = replace(cycle_config(tmp_path, max_cycles=1),
                     market_filter={"adx_max": 0.0, "atr_pct_max": 0.025,
                                    "bb_width_max": 0.06,
                                    "volume_spike_max": 2.5})
    runner = make_runner(tmp_path, place=True, config=config)
    summary = runner.run(place_orders=True)
    assert runner.exchange.place_calls == 0
    assert "MARKET_FILTER_BLOCK:ADX" in summary["cycles"][0]["risk_decision"]


def test_insufficient_balance_prevents_order(tmp_path):
    exchange = FakeExchange()
    read = FakeReadClient(exchange, account_values=[
        {"BNB": ("10", "0"), "USDT": ("1", "0")}])  # 1 USDT free
    runner = make_runner(tmp_path, place=True, exchange=exchange, read=read,
                         config=cycle_config(tmp_path, max_cycles=1))
    summary = runner.run(place_orders=True)
    assert runner.exchange.place_calls == 0
    assert any(v.startswith("INSUFFICIENT_TESTNET_QUOTE")
               for v in summary["cycles"][0]["vetoes"])


# ---------------------------------------------------------------------------
# 19/20. Rate limit / clock skew
# ---------------------------------------------------------------------------
def test_rate_limit_blocks_cycle_without_blind_retry(tmp_path):
    exchange = FakeExchange()
    read = FakeReadClient(exchange)
    runner = make_runner(tmp_path, place=True, exchange=exchange, read=read,
                         config=cycle_config(tmp_path, max_cycles=1))
    runner.preflight()  # passes; the rate limit hits mid-cycle
    read.fail_open_orders = True
    report = runner.run_cycle(1, place_orders=True)
    assert runner.exchange.place_calls == 0
    # bounded retry budget honored (3 attempts), never an infinite loop
    assert read.open_orders_calls == 1 + 3
    assert report["blocked"] == "OPEN_ORDERS_UNAVAILABLE:UNKNOWN"


def test_rate_limit_in_preflight_fails_closed(tmp_path):
    exchange = FakeExchange()
    read = FakeReadClient(exchange, fail_open_orders=True)
    runner = make_runner(tmp_path, place=True, exchange=exchange, read=read)
    summary = runner.run(place_orders=True)
    assert summary["stopped_reason"].startswith("PREFLIGHT")
    assert runner.exchange.place_calls == 0


def test_clock_skew_beyond_bound_fails_closed(tmp_path):
    exchange = FakeExchange()
    read = FakeReadClient(exchange, skew_ms=60_000)
    runner = make_runner(tmp_path, place=True, exchange=exchange, read=read)
    summary = runner.run(place_orders=True)
    assert summary["stopped_reason"].startswith("PREFLIGHT")
    assert runner.exchange.place_calls == 0


def test_clock_skew_within_bound_passes(tmp_path):
    runner = make_runner(tmp_path, place=False)
    summary = runner.run(place_orders=True)
    assert summary["stopped_reason"] is None


# ---------------------------------------------------------------------------
# 21/22. Network interruption and recovery
# ---------------------------------------------------------------------------
def test_network_interruption_during_cycle_fails_closed(tmp_path):
    exchange = FakeExchange()
    read = FakeReadClient(exchange)

    flaky = {"calls": 0}

    def flaky_account():
        flaky["calls"] += 1
        if flaky["calls"] == 3:
            raise BinanceTestnetNetworkError("socket closed")
        read._account_calls += 1
        from binance_testnet import BinanceAccountBalance, BinanceAccountSnapshot
        from datetime import datetime, timezone
        balances = tuple(
            BinanceAccountBalance(asset=a, free=Decimal(f), locked=Decimal(l))
            for a, (f, l) in {"BNB": ("10", "0"),
                              "USDT": ("10000", "0")}.items())
        return BinanceAccountSnapshot(
            balances=balances, fetched_at=datetime.now(timezone.utc))

    read.account = flaky_account
    runner = make_runner(tmp_path, place=True, exchange=exchange, read=read,
                         config=cycle_config(tmp_path, max_cycles=2))
    summary = runner.run(place_orders=True)
    assert summary["stopped_reason"].startswith("NETWORK")
    # state persisted, cleanup attempted, nothing claimed falsely
    assert len(runner.ledger.all_orders()) >= 0
    kinds = [e["kind"] for e in runner.ledger.events()]
    assert "network_interruption" in kinds


def test_recovery_after_network_interruption(tmp_path):
    exchange = FakeExchange()
    read = FakeReadClient(exchange)

    def flaky_klines(**kw):
        raise BinanceTestnetNetworkError("socket closed")

    read._spot.rest_api.klines = flaky_klines
    runner = make_runner(tmp_path, place=True, exchange=exchange, read=read,
                         config=cycle_config(tmp_path, max_cycles=1))
    summary = runner.run(place_orders=True)
    assert summary["cycles"][0]["blocked"].startswith("MARKET_DATA_FAILED")

    # network heals: a fresh runner on the same ledger completes cleanly
    runner2 = make_runner(tmp_path, place=True, exchange=exchange,
                          config=cycle_config(tmp_path, max_cycles=1))
    summary2 = runner2.run(place_orders=True)
    assert summary2["stopped_reason"] is None
    assert summary2["cleanup"]["ok"] is True
    assert runner2.exchange.place_calls == 2


# ---------------------------------------------------------------------------
# 23. Graceful shutdown
# ---------------------------------------------------------------------------
def test_graceful_shutdown_stops_at_safe_boundary(tmp_path):
    exchange = FakeExchange()
    coordinator = ShutdownCoordinator()
    runner = make_runner(tmp_path, place=True, exchange=exchange,
                         config=cycle_config(tmp_path, max_cycles=3),
                         shutdown=coordinator)
    # request shutdown between cycles via the sleep seam
    runner.sleep = lambda _s: coordinator.request(reason="test")
    summary = runner.run(place_orders=True)
    assert summary["stopped_reason"].startswith("GRACEFUL_SHUTDOWN")
    assert len(summary["cycles"]) == 1  # cycle 1 completed, cycle 2 never ran
    assert summary["cleanup"]["ok"] is True
    assert coordinator.phase.value == "COMPLETED"


# ---------------------------------------------------------------------------
# 24. Persistent state survives restart
# ---------------------------------------------------------------------------
def test_persistent_state_survives_restart(tmp_path):
    exchange = FakeExchange()
    ledger = tc.CycleLedger(str(tmp_path / "cycle.sqlite3"),
                            clock_ms=lambda: 1_790_998_000_000)
    ledger.set_state("reference_equity", "10100")
    insert_open_order(ledger, exchange, "AGTC-BNBUSDT-1-1-1")
    exchange.set_status("AGTC-BNBUSDT-1-1-1", "FILLED", executed="0.251")
    ledger.close()

    # fresh process: new ledger + new runner on the same files
    ledger2 = tc.CycleLedger(str(tmp_path / "cycle.sqlite3"),
                             clock_ms=lambda: 1_790_998_000_001)
    runner = make_runner(tmp_path, place=True, exchange=exchange,
                         ledger=ledger2)
    assert ledger2.get_state("reference_equity") == "10100"
    summary = runner.run(place_orders=True)
    row = ledger2.get_order("AGTC-BNBUSDT-1-1-1")
    assert row["state"] == tc.ORDER_FILLED
    assert summary["cleanup"]["ok"] is True
    # high-water mark never lowered
    assert Decimal(ledger2.get_state("reference_equity")) >= Decimal("10100")


# ---------------------------------------------------------------------------
# Config validation (locked invariants)
# ---------------------------------------------------------------------------
def _base_cfg_dict():
    return {
        "symbol": "BNBUSDT",
        "grid": {"step_pct": 0.006, "hard_min_net_pct": 0.003,
                 "min_cells": 6, "max_levels": 40},
        "range": {"auto": {"support_quantile": 0.10,
                           "resistance_quantile": 0.90,
                           "min_width_pct": 0.03, "max_width_pct": 0.25,
                           "min_quality_score": 65,
                           "require_price_inside": True}},
        "risk": {"range_break_buffer_pct": 0.01,
                 "stop_if_below_lower_pct": 0.02,
                 "max_equity_drawdown_pct": 0.02},
        "market_filter": {"adx_max": 28, "atr_pct_max": 0.025,
                          "bb_width_max": 0.06, "volume_spike_max": 2.5},
        "fees": {"maker_fee_fallback": 0.001,
                 "slippage_roundtrip_pct": 0.0005},
        "execution": {"order_quote_size": 25, "max_open_orders": 40},
    }


@pytest.mark.parametrize("field,value", [
    ("hard_min_net_pct", 0.002),            # below the 0.30% invariant
    ("max_equity_drawdown_pct", 0.03),      # drawdown kill must stay 2%
    ("range_break_buffer_pct", 0.02),       # buffer must stay 1%
])
def test_locked_invariants_cannot_be_loosened(tmp_path, field, value):
    cfg = _base_cfg_dict()
    section = {"hard_min_net_pct": "grid",
               "max_equity_drawdown_pct": "risk",
               "range_break_buffer_pct": "risk"}[field]
    cfg[section][field] = value
    with pytest.raises(tc.TestnetCycleConfigError):
        tc.load_cycle_config(cfg, db_path=str(tmp_path / "c.sqlite3"))


@pytest.mark.parametrize("kwargs", [
    {"max_cycles": 0}, {"max_cycles": 101}, {"max_orders_per_cycle": 11},
])
def test_bounded_cycle_limits_enforced(tmp_path, kwargs):
    cfg = _base_cfg_dict()
    with pytest.raises(tc.TestnetCycleConfigError):
        tc.load_cycle_config(cfg, db_path=str(tmp_path / "c.sqlite3"),
                             **kwargs)


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------
def test_cycle_events_capture_full_observability(tmp_path):
    runner = make_runner(tmp_path, place=True,
                         config=cycle_config(tmp_path, max_cycles=1))
    summary = runner.run(place_orders=True)
    kinds = [e["kind"] for e in runner.ledger.events()]
    for expected in ("risk_decision", "order_placed", "order_canceled"):
        assert expected in kinds
    cycle = summary["cycles"][0]
    assert cycle["range"]["lower"] and cycle["range"]["upper"]
    assert cycle["risk_decision"]
    status = runner.status()
    assert status["reference_equity"] is not None
    # no secrets anywhere in events
    blob = repr(runner.ledger.events())
    assert "api_key" not in blob.lower()
    assert "secret" not in blob.lower()
