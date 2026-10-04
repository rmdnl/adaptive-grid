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
