"""Shared test fixtures for the clean adaptive-grid suite.

All tests are deterministic and offline: no network access, no real
Binance calls, no secrets.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from config import Config  # noqa: E402

ENV_DEFAULTS = {
    "BINANCE_ENV": "testnet",
    "EXECUTION_MODE": "paper",
    "DRY_RUN": "true",
    "ALLOW_LIVE_EXECUTION": "false",
    "PAIR_LIST": "BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT",
    "INDICATOR_TIMEFRAME": "1h",
    "ADX_PERIOD": "14",
    "RSI_PERIOD": "14",
    "STOCH_RSI_LENGTH": "14",
    "STOCH_SMOOTH_K": "3",
    "STOCH_SMOOTH_D": "3",
    "ATR_PERIOD": "14",
    "ENTRY_ADX_MAX": "20",
    "ADX_REGIME_LOOKBACK": "3",
    "ENTRY_STOCH_K_MAX": "0.3",
    "EXIT_ADX_MIN": "25",
    "EXIT_STOCH_K_MAX": "0.8",
    "ATR_GRID_MULTIPLIER": "1.0",
    "GRID_GROSS_MIN": "0.005",
    "MIN_NET_PROFIT_PER_GRID": "0.002",
    "MAKER_FEE": "0.001",
    "TAKER_FEE": "0.001",
    "SLIPPAGE_ESTIMATE": "0.0005",
    "MAX_DRAWDOWN_PERCENT": "2",
    "STOP_IF_BELOW_LOWER_PERCENT": "2",
    "MIN_HOURS_BETWEEN_ENTRIES": "48",
    "MIN_STEP_PERCENT": "0.006",
    "SOFT_COOLDOWN_HOURS": "1",
    "HARD_COOLDOWN_HOURS": "2",
    "HOLD_MAX_HOURS": "72",
    "BINANCE_TESTNET_API_KEY": "",
    "BINANCE_TESTNET_API_SECRET": "",
    "BINANCE_API_KEY": "",
    "BINANCE_API_SECRET": "",
    "LOWER_PRICE": '{"BTC/USDT": 48000.0, "ETH/USDT": 2800.0, "SOL/USDT": 80.0, "BNB/USDT": 480.0}',
    "UPPER_PRICE": '{"BTC/USDT": 70000.0, "ETH/USDT": 4000.0, "SOL/USDT": 200.0, "BNB/USDT": 700.0}',
    "TOTAL_GRIDS": "5",
    "TOTAL_QUOTE_BUDGET": '{"BTC/USDT": 500.0, "ETH/USDT": 500.0, "SOL/USDT": 500.0, "BNB/USDT": 500.0}',
}


def make_config(**overrides) -> Config:
    """Build a validated Config object directly (defaults match .env.example)."""
    fields = dict(
        binance_env="testnet",
        execution_mode="paper",
        dry_run=True,
        allow_live_execution=False,
        pair_list=("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"),
        indicator_timeframe="1h",
        adx_period=14,
        rsi_period=14,
        stoch_rsi_length=14,
        stoch_smooth_k=3,
        stoch_smooth_d=3,
        atr_period=14,
        entry_adx_max=20.0,
        entry_stoch_k_max=0.3,
        adx_regime_lookback=3,
        exit_adx_min=25.0,
        exit_stoch_k_max=0.8,
        grid_step_atr_multiplier=1.0,  # ATR_GRID_MULTIPLIER in .env
        grid_gross_min=0.005,
        min_net_profit_per_grid=0.002,
        maker_fee=0.001,
        taker_fee=0.001,
        slippage_estimate=0.0005,
        max_drawdown_percent=2.0,
        stop_if_below_lower_percent=2.0,
        min_hours_between_entries=48.0,
        min_step_percent=0.006,
        soft_cooldown_hours=1.0,
        hard_cooldown_hours=2.0,
        hold_max_hours=72.0,
        start_equity=1000.0,
        max_market_data_age_seconds=21600.0,
        # Adaptive grid parameters - default to FALSE for backward compatibility tests
        adaptive_grid=False,
        min_grids=4,
        max_grids=5,
        quote_reserve_percent=20.0,
        max_quote_allocation_percent=80.0,
        lower_price={"BTC/USDT": 48000.0, "ETH/USDT": 2800.0, "SOL/USDT": 80.0, "BNB/USDT": 480.0},
        upper_price={"BTC/USDT": 70000.0, "ETH/USDT": 4000.0, "SOL/USDT": 200.0, "BNB/USDT": 700.0},
        total_grids=5,
        total_quote_budget={"BTC/USDT": 500.0, "ETH/USDT": 500.0, "SOL/USDT": 500.0, "BNB/USDT": 500.0},
        testnet_api_key="",
        testnet_api_secret="",
        live_api_key="",
        live_api_secret="",
        env_file="test",
    )
    fields.update(overrides)
    return Config(**fields)


def write_env_file(path, **overrides) -> str:
    """Write a .env file (ENV_DEFAULTS with overrides) and return its path.
    An override of None omits the key entirely (tests a missing key)."""
    values = dict(ENV_DEFAULTS)
    values.update(overrides)
    lines = [f"{k}={v}" for k, v in values.items() if v is not None]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return str(path)


@pytest.fixture
def cfg():
    return make_config()


def make_candle(close, high, low, volume, open_time, close_time, open_=None):
    return {
        "open_time": open_time,
        "open": open_ if open_ is not None else close,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "close_time": close_time,
    }


class FakeSpot:
    """Scriptable BinanceSpot stand-in (same API surface as BinanceSpot)
    for offline LiveExecutor tests."""

    def __init__(self, adjust_balance=True):
        import exchange as _exchange
        self._exchange = _exchange
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
                "quoteQty": price * qty,
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
            raise self._exchange.ExchangeError("timeout: response lost")
        if mode == "pre":
            raise self._exchange.ExchangeError("network error before submission")
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
        order = self.orders[cid]
        if order["status"] in ("FILLED", "CANCELED", "EXPIRED", "REJECTED"):
            # real Binance behaviour: cancelling a terminal order -> -2011
            raise self._exchange.ExchangeError("-2011 Unknown order sent")
        order["status"] = "CANCELED"
        return dict(order)

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
            raise self._exchange.ExchangeError("balance unavailable")
        return {
            "balances": [
                {"asset": a, "free": v, "locked": 0.0} for a, v in self.balances.items()
            ]
        }

    def get_balance(self, asset):
        if self.fail_balance:
            raise self._exchange.ExchangeError("balance unavailable")
        return float(self.balances.get(asset, 0.0))

    def get_filters(self, symbol):
        from grid import ExchangeFilters
        return ExchangeFilters(
            tick_size=0.01,
            step_size=0.00001,
            min_notional=10.0,
            min_qty=0.0,
        )


def wait_for_server(server, timeout_s: float = 10.0):
    """Remove the thread-start race in the dashboard socket tests: after
    `thread.start()` the accept loop is not guaranteed to be listening yet,
    so the first HTTP request intermittently hits ConnectionRefused under
    load (notably on Windows). Poll an accepted connection until it can be
    established (bounded), so the assertions on response codes stay strict.

    Wildcard binds (0.0.0.0 / ::) must be polled via the loopback address:
    connecting directly to a wildcard is unreliable cross-platform."""
    import socket
    import time as _time

    bound_host, port = server.server_address
    host = "127.0.0.1" if bound_host in ("0.0.0.0", "::", "") else bound_host
    deadline = _time.monotonic() + timeout_s
    last = None
    while _time.monotonic() < deadline:
        try:
            conn = socket.create_connection((host, port), timeout=0.25)
            conn.close()
            return
        except OSError as exc:
            last = exc
            _time.sleep(0.05)
    raise TimeoutError(f"server on {bound_host}:{port} not accepting connections: {last}")
