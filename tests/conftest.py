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
    "DRY_RUN": "true",
    "ALLOW_LIVE_EXECUTION": "false",
    "PAIR_LIST": "BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT",
    "INDICATOR_TIMEFRAME": "4h",
    "ADX_PERIOD": "14",
    "RSI_PERIOD": "14",
    "BB_PERIOD": "20",
    "BB_STD": "2",
    "VO_FAST": "5",
    "VO_SLOW": "10",
    "ZSCORE_PERIOD": "20",
    "ATR_PERIOD": "14",
    "ENTRY_ADX_MAX": "20",
    "ENTRY_RSI_MAX": "35",
    "ENTRY_VOLUME_OSC_MIN": "0",
    "ENTRY_BB_PERCENT_B_MAX": "0",
    "EXIT_RSI_MIN": "70",
    "EXIT_ADX_MIN": "25",
    "EXIT_BB_PERCENT_B_MIN": "1",
    "EXIT_ZSCORE_ABS_MAX": "2.5",
    "GRID_STEP_ATR_MULTIPLIER": "1.0",
    "GRID_GROSS_MIN": "0.005",
    "MIN_NET_PROFIT_PER_GRID": "0.002",
    "MAKER_FEE": "0.001",
    "TAKER_FEE": "0.001",
    "SLIPPAGE_ESTIMATE": "0.0005",
    "MAX_DRAWDOWN_PERCENT": "2",
    "STOP_IF_BELOW_LOWER_PERCENT": "2",
    "START_EQUITY": "1000",
    "COOLDOWN_HOURS": "3",
    "BINANCE_TESTNET_API_KEY": "",
    "BINANCE_TESTNET_API_SECRET": "",
    "BINANCE_LIVE_API_KEY": "",
    "BINANCE_LIVE_API_SECRET": "",
}


def make_config(**overrides) -> Config:
    """Build a validated Config object directly (defaults match .env.example)."""
    fields = dict(
        binance_env="testnet",
        dry_run=True,
        allow_live_execution=False,
        pair_list=("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"),
        indicator_timeframe="4h",
        adx_period=14,
        rsi_period=14,
        bb_period=20,
        bb_std=2.0,
        vo_fast=5,
        vo_slow=10,
        zscore_period=20,
        atr_period=14,
        entry_adx_max=20.0,
        entry_rsi_max=35.0,
        entry_volume_osc_min=0.0,
        entry_bb_percent_b_max=0.0,
        exit_rsi_min=70.0,
        exit_adx_min=25.0,
        exit_bb_percent_b_min=1.0,
        exit_zscore_abs_max=2.5,
        grid_step_atr_multiplier=1.0,
        grid_gross_min=0.005,
        min_net_profit_per_grid=0.002,
        maker_fee=0.001,
        taker_fee=0.001,
        slippage_estimate=0.0005,
        max_drawdown_percent=2.0,
        stop_if_below_lower_percent=2.0,
        cooldown_hours=3.0,
        start_equity=1000.0,
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
