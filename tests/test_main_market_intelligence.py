"""Tests for Phase 4 Market Intelligence integration with main.py."""

from datetime import datetime, timezone
from decimal import Decimal
import sqlite3
import pytest

from grid_eligibility import (
    BlockingReason,
    GridEligibilityDecision,
    GridEligibilityStatus,
)
import main
from market_data import AccountSnapshot, MarketQuote, TickerSnapshot
from market_regime import MarketRegime
from symbol_rules import SymbolRules
from storage import get_state
from range_quality import RangeQualityBreakdown, RangeQualityResult
from tests.test_market_features import make_deterministic_candles


def _symbol_info():
    return {
        "symbol": "BTCUSDT",
        "baseAsset": "BTC",
        "quoteAsset": "USDT",
        "status": "TRADING",
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "100000", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "10000", "stepSize": "0.001"},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "5000", "stepSize": "0.001"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
            {"filterType": "NOTIONAL", "minNotional": "10", "maxNotional": "100000"},
            {"filterType": "PERCENT_PRICE", "multiplierUp": "1.05", "multiplierDown": "0.95", "avgPriceMins": 5},
            {"filterType": "MAX_NUM_ORDERS", "maxNumOrders": 40},
        ],
    }


def _config(tmp_path, dry_run=True):
    return {
        "environment": {
            "mode": "testnet",
            "dry_run": dry_run,
            "allow_live_execution": False,
        },
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
        "market_intelligence": {
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
            "sqlite_path": str(tmp_path / "grid.sqlite3"),
            "log_path": str(tmp_path / "grid.log"),
            "csv_path": str(tmp_path / "trades.csv"),
        },
    }


def _install_stubs(monkeypatch, tmp_path, intelligence_decision=None):
    candles_df = make_deterministic_candles(70, base_price=100.0)
    monkeypatch.setattr(main, "load_dotenv", lambda: None)
    monkeypatch.setattr(main, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(main, "make_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(main, "fetch_symbol_info", lambda client, symbol: _symbol_info())
    monkeypatch.setattr(main, "fetch_klines", lambda client, symbol, interval, limit, drop_incomplete=True: candles_df)
    monkeypatch.setattr(main, "enrich", lambda df: candles_df)
    monkeypatch.setattr(
        main,
        "latest_valid_row",
        lambda df: {
            "close": 100.0,
            "adx": 15.0,
            "atr": 1.0,
            "atr_pct": 0.01,
            "bb_width": 0.03,
            "volume_ratio": 1.0,
            "rsi": 50.0,
            "open_time": candles_df["open_time"].iloc[-1],
        },
    )
    monkeypatch.setattr(
        main,
        "fetch_ticker_price",
        lambda client, symbol: TickerSnapshot("BTCUSDT", Decimal("100.0"), datetime.now(timezone.utc)),
    )
    monkeypatch.setattr(
        main,
        "fetch_book_ticker",
        lambda client, symbol: MarketQuote(
            symbol="BTCUSDT",
            bid_price=Decimal("99.98"),
            ask_price=Decimal("100.02"),
            bid_qty=Decimal("10"),
            ask_qty=Decimal("10"),
            fetched_at=datetime.now(timezone.utc),
        ),
    )
    monkeypatch.setattr(
        main,
        "fetch_account_snapshot",
        lambda client, base, quote: AccountSnapshot(
            base_asset="BTC",
            base_free=Decimal("1.0"),
            base_locked=Decimal("0"),
            quote_asset="USDT",
            quote_free=Decimal("1000"),
            quote_locked=Decimal("0"),
            fetched_at=datetime.now(timezone.utc),
        ),
    )
    monkeypatch.setattr(main, "fetch_open_orders", lambda client, symbol: [])
    monkeypatch.setattr(main, "fetch_account_commission", lambda client, symbol: (None, "FALLBACK"))
    # PATCH 1 (F-H1): session-local reference equity is now the persisted
    # paper_reference_equity key in the fresh per-test DB — nothing to reset.

    if intelligence_decision is not None:
        monkeypatch.setattr(main, "evaluate_grid_eligibility", lambda *args, **kwargs: intelligence_decision)


def test_main_grid_allowed(monkeypatch, tmp_path):
    """When market intelligence allows grid, paper orders are submitted successfully."""
    allowed_decision = GridEligibilityDecision(
        status=GridEligibilityStatus.GRID_ALLOWED,
        allowed=True,
        reasons=(),
        regime=MarketRegime.RANGE,
        range_quality_score=Decimal("85.0"),
        features=None,
        diagnostics={},
    )
    _install_stubs(monkeypatch, tmp_path, intelligence_decision=allowed_decision)
    exit_code = main.main()
    assert exit_code == 0

    # Verify orders in SQLite
    db_path = tmp_path / "grid.sqlite3"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT count(*) FROM orders").fetchone()[0]
        assert rows > 0

        # Verify state
        last_intel = get_state(db_path, "last_market_intelligence")
        assert last_intel is not None
        assert "GRID_ALLOWED" in last_intel


def test_main_grid_blocked_by_market_intelligence(monkeypatch, tmp_path):
    """When market intelligence blocks grid, paper orders are NOT submitted and risk event is recorded."""
    blocked_decision = GridEligibilityDecision(
        status=GridEligibilityStatus.GRID_BLOCKED,
        allowed=False,
        reasons=(BlockingReason.TREND_TOO_STRONG, BlockingReason.SPREAD_TOO_WIDE),
        regime=MarketRegime.TREND_UP,
        range_quality_score=Decimal("42.0"),
        features=None,
        diagnostics={"reason": "test trend block"},
    )
    _install_stubs(monkeypatch, tmp_path, intelligence_decision=blocked_decision)
    exit_code = main.main()
    assert exit_code == 0

    # Verify NO paper orders placed
    db_path = tmp_path / "grid.sqlite3"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT count(*) FROM orders").fetchone()[0]
        assert rows == 0

        # Verify risk event has the blocking reason
        risk_event = conn.execute("SELECT allowed, reason FROM risk_events ORDER BY id DESC LIMIT 1").fetchone()
        assert risk_event[0] == 0  # not allowed
        assert "MARKET_INTELLIGENCE:GRID_BLOCKED:TREND_TOO_STRONG|SPREAD_TOO_WIDE" in risk_event[1]
