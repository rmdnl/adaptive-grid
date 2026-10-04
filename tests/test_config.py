"""Config tests: .env-only loading, validation, live-safety gates."""

from __future__ import annotations

import pytest

from config import ConfigError, load_config
from conftest import make_config, write_env_file


def test_loads_valid_env_file(tmp_path):
    path = write_env_file(tmp_path / ".env")
    cfg = load_config(path)
    assert cfg.binance_env == "testnet"
    assert cfg.dry_run is True
    assert cfg.allow_live_execution is False
    assert cfg.pair_list == ("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT")
    assert cfg.indicator_timeframe == "4h"
    assert cfg.adx_period == 14
    assert cfg.bb_std == 2.0
    assert cfg.entry_adx_max == 20.0
    assert cfg.exit_zscore_abs_max == 2.5
    assert cfg.grid_gross_min == 0.005
    assert cfg.min_net_profit_per_grid == 0.002
    assert cfg.maker_fee == 0.001
    assert cfg.slippage_estimate == 0.0005
    assert cfg.max_drawdown_percent == 2.0
    assert cfg.stop_if_below_lower_percent == 2.0
    assert cfg.cooldown_hours == 3.0
    assert cfg.max_drawdown == 0.02
    assert cfg.stop_if_below_lower == 0.02


def test_missing_env_file_fails(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "does_not_exist.env"))


def test_defaults_for_optional_safety_keys(tmp_path):
    # DRY_RUN / ALLOW_LIVE_EXECUTION / BINANCE_ENV default to safe values.
    path = write_env_file(
        tmp_path / ".env",
        **{"BINANCE_ENV": None, "DRY_RUN": None, "ALLOW_LIVE_EXECUTION": None},
    )
    cfg = load_config(path)
    assert cfg.dry_run is True
    assert cfg.allow_live_execution is False
    assert cfg.binance_env == "testnet"


def test_missing_required_key_fails(tmp_path):
    path = write_env_file(tmp_path / ".env", RSI_PERIOD=None)
    with pytest.raises(ConfigError, match="RSI_PERIOD"):
        load_config(path)


def test_missing_key_error_lists_all_problems(tmp_path):
    path = write_env_file(tmp_path / ".env", RSI_PERIOD=None, ATR_PERIOD=None)
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "RSI_PERIOD" in str(exc.value)
    assert "ATR_PERIOD" in str(exc.value)


def test_invalid_bool_fails(tmp_path):
    path = write_env_file(tmp_path / ".env", DRY_RUN="yes")
    with pytest.raises(ConfigError, match="DRY_RUN"):
        load_config(path)


def test_invalid_timeframe_fails(tmp_path):
    path = write_env_file(tmp_path / ".env", INDICATOR_TIMEFRAME="4hh")
    with pytest.raises(ConfigError, match="INDICATOR_TIMEFRAME"):
        load_config(path)


def test_timeframe_1h_and_4h_both_accepted(tmp_path):
    for tf in ("1h", "4h"):
        path = write_env_file(tmp_path / f".env_{tf}", INDICATOR_TIMEFRAME=tf)
        assert load_config(path).indicator_timeframe == tf


def test_invalid_number_fails(tmp_path):
    path = write_env_file(tmp_path / ".env", ATR_PERIOD="fourteen")
    with pytest.raises(ConfigError, match="ATR_PERIOD"):
        load_config(path)


def test_invalid_pair_list_fails(tmp_path):
    path = write_env_file(tmp_path / ".env", PAIR_LIST="BTCUSDT")
    with pytest.raises(ConfigError, match="PAIR_LIST"):
        load_config(path)


def test_no_yaml_configuration_source(tmp_path):
    # A config.yaml placed next to .env must be ignored entirely: .env is
    # the single source of truth.
    (tmp_path / "config.yaml").write_text(
        "DRY_RUN: false\nALLOW_LIVE_EXECUTION: true\nBINANCE_ENV: live\n",
        encoding="utf-8",
    )
    path = write_env_file(tmp_path / ".env")  # dry_run=true, gates closed
    cfg = load_config(path)
    assert cfg.dry_run is True
    assert cfg.allow_live_execution is False
    assert cfg.binance_env == "testnet"


def test_default_config_never_permits_live(tmp_path):
    path = write_env_file(tmp_path / ".env")
    cfg = load_config(path)
    assert cfg.allow_live is False


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"DRY_RUN": "false", "ALLOW_LIVE_EXECUTION": "true", "BINANCE_ENV": "live",
          "BINANCE_LIVE_API_KEY": "k", "BINANCE_LIVE_API_SECRET": "s"}, True),   # all three gates explicit
        ({"DRY_RUN": "true", "ALLOW_LIVE_EXECUTION": "true", "BINANCE_ENV": "live"}, False),  # dry-run still on
        ({"DRY_RUN": "false", "ALLOW_LIVE_EXECUTION": "false", "BINANCE_ENV": "live"}, None), # contradictory -> ConfigError
        ({"DRY_RUN": "false", "ALLOW_LIVE_EXECUTION": "true", "BINANCE_ENV": "testnet",
          "BINANCE_TESTNET_API_KEY": "tk", "BINANCE_TESTNET_API_SECRET": "ts"}, False),         # testnet env
        ({"DRY_RUN": "true", "ALLOW_LIVE_EXECUTION": "false", "BINANCE_ENV": "testnet"}, False),# full defaults
    ],
)
def test_live_requires_all_three_gates(tmp_path, overrides, expected):
    path = write_env_file(tmp_path / ".env", **overrides)
    if expected is None:
        with pytest.raises(ConfigError, match="refusing"):
            load_config(path)
    else:
        assert load_config(path).allow_live is expected


def test_live_credentials_not_exposed_unless_gated(tmp_path):
    path = write_env_file(
        tmp_path / ".env",
        BINANCE_TESTNET_API_KEY="test-key",
        BINANCE_TESTNET_API_SECRET="test-secret",
        BINANCE_LIVE_API_KEY="live-key",
        BINANCE_LIVE_API_SECRET="live-secret",
    )
    cfg = load_config(path)
    assert cfg.api_credentials == ("test-key", "test-secret")

    live_cfg = make_config(
        dry_run=False, allow_live_execution=True, binance_env="live",
        live_api_key="live-key", live_api_secret="live-secret",
    )
    assert live_cfg.allow_live is True
    assert live_cfg.api_credentials == ("live-key", "live-secret")


def test_live_gate_with_empty_live_credentials_fails(tmp_path):
    path = write_env_file(
        tmp_path / ".env",
        DRY_RUN="false",
        ALLOW_LIVE_EXECUTION="true",
        BINANCE_ENV="live",
    )
    with pytest.raises(ConfigError, match="live execution enabled"):
        load_config(path)


def test_real_testnet_execution_requires_testnet_credentials(tmp_path):
    path = write_env_file(tmp_path / ".env", DRY_RUN="false")
    with pytest.raises(ConfigError, match="testnet credentials"):
        load_config(path)


def test_gross_minimum_cannot_go_below_specification_floor(tmp_path):
    path = write_env_file(tmp_path / ".env", GRID_GROSS_MIN="0.004")
    with pytest.raises(ConfigError, match="GRID_GROSS_MIN"):
        load_config(path)


def test_net_minimum_cannot_go_below_specification_floor(tmp_path):
    path = write_env_file(tmp_path / ".env", MIN_NET_PROFIT_PER_GRID="0.001")
    with pytest.raises(ConfigError, match="MIN_NET_PROFIT_PER_GRID"):
        load_config(path)


def test_drawdown_hard_limit_two_percent(tmp_path):
    path = write_env_file(tmp_path / ".env", MAX_DRAWDOWN_PERCENT="5")
    with pytest.raises(ConfigError, match="MAX_DRAWDOWN_PERCENT"):
        load_config(path)


def test_lower_boundary_stop_capped_at_two_percent(tmp_path):
    path = write_env_file(tmp_path / ".env", STOP_IF_BELOW_LOWER_PERCENT="3")
    with pytest.raises(ConfigError, match="STOP_IF_BELOW_LOWER_PERCENT"):
        load_config(path)


def test_negative_fee_rejected(tmp_path):
    path = write_env_file(tmp_path / ".env", MAKER_FEE="-0.001")
    with pytest.raises(ConfigError, match="MAKER_FEE"):
        load_config(path)


def test_grid_mode_mapping():
    cfg = make_config()
    assert cfg.grid_mode("BTC/USDT") == "arithmetic"
    assert cfg.grid_mode("ETH/USDT") == "arithmetic"
    assert cfg.grid_mode("BNB/USDT") == "arithmetic"
    assert cfg.grid_mode("SOL/USDT") == "geometric"
    assert cfg.grid_mode("DOGE/USDT") == "arithmetic"


def test_start_equity_defaults_and_validates(tmp_path):
    path = write_env_file(tmp_path / ".env")  # key omitted -> safe default
    assert load_config(path).start_equity == 1000.0
    path = write_env_file(tmp_path / ".env", START_EQUITY="250")
    assert load_config(path).start_equity == 250.0
    path = write_env_file(tmp_path / ".env", START_EQUITY="zero")
    with pytest.raises(ConfigError, match="START_EQUITY"):
        load_config(path)
    path = write_env_file(tmp_path / ".env", START_EQUITY="0")
    with pytest.raises(ConfigError, match="START_EQUITY"):
        load_config(path)
