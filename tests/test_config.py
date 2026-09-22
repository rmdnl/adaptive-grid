"""Tests for config_loader validation."""

from decimal import Decimal
import pytest

from config_loader import ConfigError, validate_config


def _base_config():
    """Minimal valid config for grid trading."""
    return {
        "environment": {"mode": "testnet", "dry_run": True, "allow_live_execution": False},
        "symbol": "BTCUSDT",
        "timeframe": "15m",
        "grid": {
            "step_pct": Decimal("0.006"),
            "min_cells": 3,
            "max_levels": 10,
            "hard_min_net_pct": Decimal("0.003"),
            "preferred_net_max_pct": Decimal("0.005"),
        },
        "range": {
            "mode": "manual",
            "lower_price": Decimal("98"),
            "upper_price": Decimal("103"),
            "lookback": 200,
            "buffer_pct": Decimal("0.01"),
            "auto": {},
        },
        "market_filter": {
            "adx_max": Decimal("28"),
            "atr_pct_max": Decimal("0.025"),
            "bb_width_max": Decimal("0.06"),
            "volume_spike_max": Decimal("2.5"),
        },
        "execution": {
            "prefer_limit_maker": True,
            "stale_order_minutes": 30,
            "max_open_orders": 40,
            "order_quote_size": Decimal("25"),
            "total_quote_budget": Decimal("0"),
            "max_inventory_pct": Decimal("0.70"),
        },
        "fees": {
            "maker_fee_fallback": Decimal("0.001"),
            "taker_fee_fallback": Decimal("0.001"),
            "slippage_roundtrip_pct": Decimal("0.0005"),
        },
        "paper": {
            "initial_base_balance": Decimal("2"),
            "initial_quote_balance": Decimal("1000"),
            "maker_fee": Decimal("0.001"),
            "taker_fee": Decimal("0.001"),
            "fee_asset": "USDT",
        },
        "risk": {
            "max_equity_drawdown_pct": Decimal("0.02"),
            "range_break_buffer_pct": Decimal("0.01"),
            "daily_profit_lock_pct": Decimal("0.01"),
            "cooldown_minutes": 30,
        },
        "logging": {
            "sqlite_path": "grid.sqlite3",
            "log_path": "grid.log",
            "csv_path": "trades.csv",
        },
    }


def _planner_section(**overrides):
    """Return a valid adaptive_planner section with optional overrides."""
    section = {
        "cooldown_candles": 4,
        "hysteresis": {
            "range_change_pct": Decimal("0.02"),
            "step_change_pct": Decimal("0.10"),
            "grid_count_change": 3,
            "quality_degradation": Decimal("5"),
            "regime_change": True,
        },
    }
    if "cooldown_candles" in overrides:
        section["cooldown_candles"] = overrides["cooldown_candles"]
    if "hysteresis" in overrides:
        section["hysteresis"] = overrides["hysteresis"]
    return section


def _intelligence_section():
    return {
        "timeframe": "15m",
        "min_candles": 60,
        "max_candle_age_seconds": 3600,
        "atr_period": 14,
        "adx_period": 14,
        "bb_length": 20,
        "bb_std_mult": 2.0,
        "volume_baseline_period": 20,
        "range_stability_period": 20,
        "regime": {
            "adx_trend_min": 25,
            "atr_expansion_ratio": 1.5,
            "price_range_inclusion_min": 0.90,
            "directional_efficiency_max": 0.60,
        },
        "liquidity": {
            "max_spread_pct": 0.003,
            "max_quote_ticker_age_seconds": 10,
        },
        "quality": {
            "min_range_quality_score": 60,
            "weight_trend_stability": 0.25,
            "weight_volatility_suitability": 0.20,
            "weight_bb_width_suitability": 0.15,
            "weight_volume_stability": 0.10,
            "weight_spread_suitability": 0.10,
            "weight_range_containment": 0.20,
        },
    }


# --- Baseline validation (no adaptive_planner) ---

def test_base_config_passes():
    cfg = _base_config()
    # Should not raise
    validate_config(cfg)


# --- adaptive_planner section: missing / malformed ---

def test_missing_section_skipped():
    """No adaptive_planner key → validation skipped, no error."""
    cfg = _base_config()
    # Missing key is fine — just skip
    validate_config(cfg)


def test_not_dict_raises():
    cfg = _base_config()
    cfg["adaptive_planner"] = "not_a_dict"
    with pytest.raises(ConfigError, match="required"):
        validate_config(cfg)


def test_missing_cooldown_candles():
    cfg = _base_config()
    cfg["adaptive_planner"] = _planner_section()
    del cfg["adaptive_planner"]["cooldown_candles"]
    with pytest.raises(ConfigError, match="cooldown_candles"):
        validate_config(cfg)


def test_cooldown_candles_zero():
    cfg = _base_config()
    cfg["adaptive_planner"] = _planner_section(cooldown_candles=0)
    with pytest.raises(ConfigError, match="cooldown_candles"):
        validate_config(cfg)


def test_cooldown_candles_negative():
    cfg = _base_config()
    cfg["adaptive_planner"] = _planner_section(cooldown_candles=-1)
    with pytest.raises(ConfigError, match="cooldown_candles"):
        validate_config(cfg)


def test_missing_hysteresis():
    cfg = _base_config()
    cfg["adaptive_planner"] = _planner_section()
    del cfg["adaptive_planner"]["hysteresis"]
    with pytest.raises(ConfigError, match="hysteresis"):
        validate_config(cfg)


def test_hysteresis_not_dict():
    cfg = _base_config()
    cfg["adaptive_planner"] = _planner_section()
    cfg["adaptive_planner"]["hysteresis"] = "bad"
    with pytest.raises(ConfigError, match="hysteresis"):
        validate_config(cfg)


def test_range_change_pct_zero():
    cfg = _base_config()
    cfg["adaptive_planner"] = _planner_section()
    cfg["adaptive_planner"]["hysteresis"]["range_change_pct"] = Decimal("0")
    with pytest.raises(ConfigError, match="range_change_pct"):
        validate_config(cfg)


def test_range_change_pct_negative():
    cfg = _base_config()
    cfg["adaptive_planner"] = _planner_section()
    cfg["adaptive_planner"]["hysteresis"]["range_change_pct"] = Decimal("-0.01")
    with pytest.raises(ConfigError, match="range_change_pct"):
        validate_config(cfg)


def test_step_change_pct_zero():
    cfg = _base_config()
    cfg["adaptive_planner"] = _planner_section()
    cfg["adaptive_planner"]["hysteresis"]["step_change_pct"] = Decimal("0")
    with pytest.raises(ConfigError, match="step_change_pct"):
        validate_config(cfg)


def test_grid_count_change_negative():
    cfg = _base_config()
    cfg["adaptive_planner"] = _planner_section()
    cfg["adaptive_planner"]["hysteresis"]["grid_count_change"] = -1
    with pytest.raises(ConfigError, match="grid_count_change"):
        validate_config(cfg)


def test_grid_count_change_not_int():
    cfg = _base_config()
    cfg["adaptive_planner"] = _planner_section()
    cfg["adaptive_planner"]["hysteresis"]["grid_count_change"] = "abc"
    with pytest.raises(ConfigError, match="grid_count_change"):
        validate_config(cfg)


def test_quality_degradation_negative():
    cfg = _base_config()
    cfg["adaptive_planner"] = _planner_section()
    cfg["adaptive_planner"]["hysteresis"]["quality_degradation"] = Decimal("-1")
    with pytest.raises(ConfigError, match="quality_degradation"):
        validate_config(cfg)


def test_regime_change_not_bool():
    cfg = _base_config()
    cfg["adaptive_planner"] = _planner_section()
    cfg["adaptive_planner"]["hysteresis"]["regime_change"] = "yes"
    with pytest.raises(ConfigError, match="regime_change"):
        validate_config(cfg)


def test_valid_config_passes():
    cfg = _base_config()
    cfg["adaptive_planner"] = _planner_section()
    validate_config(cfg)
