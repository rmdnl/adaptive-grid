"""Tests for Phase 5A adaptive-planner integration with main.py."""

from datetime import datetime, timezone
from decimal import Decimal
import json
import sqlite3
import pytest

from grid_eligibility import GridEligibilityDecision, GridEligibilityStatus
from grid_planner import evaluate_adaptive_grid_plan, PlanDecision
import main
from market_data import AccountSnapshot, MarketQuote, TickerSnapshot
from market_regime import MarketRegime
from storage import get_state, init_db, set_state
from tests.test_market_features import make_deterministic_candles


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
        "adaptive_planner": {
            "cooldown_candles": 4,
            "hysteresis": {
                "range_change_pct": Decimal("0.02"),
                "step_change_pct": Decimal("0.10"),
                "grid_count_change": 3,
                "quality_degradation": 5,
                "regime_change": True,
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
    monkeypatch.setattr(main, "_SESSION_REFERENCE_EQUITY", None)

    if intelligence_decision is not None:
        monkeypatch.setattr(main, "evaluate_grid_eligibility", lambda *args, **kwargs: intelligence_decision)


def _allowed_intelligence():
    return GridEligibilityDecision(
        status=GridEligibilityStatus.GRID_ALLOWED,
        allowed=True,
        reasons=(),
        regime=MarketRegime.RANGE,
        range_quality_score=Decimal("85.0"),
        features=None,
        diagnostics={},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_main_planner_fresh_grid_allowed(monkeypatch, tmp_path):
    """No active plan → planner returns GRID_ALLOWED → orders placed, state persisted."""
    _install_stubs(monkeypatch, tmp_path, intelligence_decision=_allowed_intelligence())
    exit_code = main.main()
    assert exit_code == 0

    db = tmp_path / "grid.sqlite3"
    # Paper orders submitted
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT count(*) FROM orders").fetchone()[0]
        assert rows > 0

    # last_active_plan persisted
    raw = get_state(db, "last_active_plan")
    assert raw is not None
    plan = json.loads(raw)
    assert plan["regime"] == "RANGE"
    assert "plan_id" in plan

    # last_adaptive_plan shows decision
    raw_ad = get_state(db, "last_adaptive_plan")
    assert raw_ad is not None
    ad = json.loads(raw_ad)
    assert ad["decision"] == "GRID_ALLOWED"

    # Risk event context includes adaptive plan fields
    with sqlite3.connect(db) as conn:
        ctx = conn.execute(
            "SELECT context_json FROM risk_events ORDER BY id ASC LIMIT 1"
        ).fetchone()[0]
    ctx_obj = json.loads(ctx)
    assert ctx_obj["adaptive_plan_decision"] == "GRID_ALLOWED"
    assert ctx_obj["adaptive_plan_reasons"] == []


def test_main_planner_cooldown_blocks_orders(monkeypatch, tmp_path):
    """Active plan within cooldown window → GRID_BLOCKED, no orders."""
    _install_stubs(monkeypatch, tmp_path, intelligence_decision=_allowed_intelligence())
    db = tmp_path / "grid.sqlite3"
    init_db(db)
    # Seed eval index to 10 and active plan at candle_index 9
    # After main increments: eval_index = 11, elapsed = 11 - 9 = 2 < cooldown_candles(4)
    set_state(db, "adaptive_eval_index", "10")
    set_state(db, "last_active_plan", {
        "plan_id": "plan_cooldown_test",
        "candidate_lower": "98",
        "candidate_upper": "103",
        "grid_step": "0.006",
        "grid_count": 20,
        "regime": "RANGE",
        "range_quality_score": "85.0",
        "candle_index": 9,
    })

    exit_code = main.main()
    assert exit_code == 0

    # No orders placed
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT count(*) FROM orders").fetchone()[0]
        assert rows == 0

    # Risk event shows ADAPTIVE_PLANNER:GRID_BLOCKED:COOLDOWN_ACTIVE
    with sqlite3.connect(db) as conn:
        reason = conn.execute(
            "SELECT reason FROM risk_events ORDER BY id ASC LIMIT 1"
        ).fetchone()[0]
    assert "ADAPTIVE_PLANNER:GRID_BLOCKED:COOLDOWN_ACTIVE" in reason


def test_main_planner_keep_current_plan_no_orders(monkeypatch, tmp_path):
    """Active plan exists, no hysteresis trigger → KEEP_CURRENT_PLAN, no new orders."""
    _install_stubs(monkeypatch, tmp_path, intelligence_decision=_allowed_intelligence())
    db = tmp_path / "grid.sqlite3"
    init_db(db)
    # Seed eval index to 10, active plan at candle_index 5 → elapsed 6 ≥ 4 (no cooldown)
    # Active plan params are identical to what the planner would derive → no hysteresis
    cfg = _config(tmp_path)
    fresh = evaluate_adaptive_grid_plan(
        pair="BTCUSDT",
        regime=MarketRegime.RANGE,
        range_quality_score=Decimal("85.0"),
        current_price=Decimal("100.0"),
        configured_lower=Decimal("98"),
        configured_upper=Decimal("103"),
        available_base_inventory=Decimal("2"),
        cfg=cfg,
        active_plan=None,
        current_candle_index=0,
    )
    set_state(db, "adaptive_eval_index", "10")
    set_state(db, "last_active_plan", {
        "plan_id": "plan_keep_test",
        "candidate_lower": str(fresh.candidate_lower),
        "candidate_upper": str(fresh.candidate_upper),
        "grid_step": str(fresh.grid_step),
        "grid_count": fresh.grid_count,
        "regime": fresh.regime.value,
        "range_quality_score": str(fresh.range_quality_score),
        "candle_index": 5,
    })

    exit_code = main.main()
    assert exit_code == 0

    # No new orders submitted
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT count(*) FROM orders").fetchone()[0]
        assert rows == 0

    # Decision is KEEP_CURRENT_PLAN
    raw_ad = get_state(db, "last_adaptive_plan")
    assert raw_ad is not None
    ad = json.loads(raw_ad)
    assert ad["decision"] == "KEEP_CURRENT_PLAN"

    # Active plan NOT overwritten — still has original plan_id
    raw_active = get_state(db, "last_active_plan")
    assert raw_active is not None
    active = json.loads(raw_active)
    assert active["plan_id"] == "plan_keep_test"


def test_main_planner_reconfig_no_new_orders(monkeypatch, tmp_path):
    """Active plan exists, hysteresis triggers → RECONFIGURATION_REQUIRED, no new orders, old plan kept."""
    _install_stubs(monkeypatch, tmp_path, intelligence_decision=_allowed_intelligence())
    db = tmp_path / "grid.sqlite3"
    init_db(db)
    # Seed active plan with very different range (triggers 2% threshold)
    # candle_index=5, eval_index=10 → elapsed=6 ≥ 4 (no cooldown)
    set_state(db, "adaptive_eval_index", "10")
    set_state(db, "last_active_plan", {
        "plan_id": "plan_reconfig_old",
        "candidate_lower": "90",
        "candidate_upper": "110",
        "grid_step": "0.006",
        "grid_count": 20,
        "regime": "RANGE",
        "range_quality_score": "85.0",
        "candle_index": 5,
    })

    exit_code = main.main()
    assert exit_code == 0

    # No new orders submitted
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT count(*) FROM orders").fetchone()[0]
        assert rows == 0

    # Decision is RECONFIGURATION_REQUIRED
    raw_ad = get_state(db, "last_adaptive_plan")
    assert raw_ad is not None
    ad = json.loads(raw_ad)
    assert ad["decision"] == "RECONFIGURATION_REQUIRED"

    # Old active plan NOT overwritten
    raw_active = get_state(db, "last_active_plan")
    assert raw_active is not None
    active = json.loads(raw_active)
    assert active["plan_id"] == "plan_reconfig_old"


def test_main_planner_exception_fallback(monkeypatch, tmp_path):
    """Planner throws exception → GRID_BLOCKED fallback, no orders."""
    _install_stubs(monkeypatch, tmp_path, intelligence_decision=_allowed_intelligence())
    # Make the planner raise
    monkeypatch.setattr(
        main,
        "evaluate_adaptive_grid_plan",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("test planner error")),
    )

    exit_code = main.main()
    assert exit_code == 0

    db = tmp_path / "grid.sqlite3"
    # No orders placed (combined blocked)
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT count(*) FROM orders").fetchone()[0]
        assert rows == 0

    # Risk event shows exception fallback block
    with sqlite3.connect(db) as conn:
        reason = conn.execute(
            "SELECT reason FROM risk_events ORDER BY id ASC LIMIT 1"
        ).fetchone()[0]
    assert "ADAPTIVE_PLANNER:GRID_BLOCKED:INVALID_MARKET_DATA" in reason


def test_main_planner_skipped_without_config(monkeypatch, tmp_path):
    """If adaptive_planner section missing from config, planner is skipped — old behavior preserved."""
    from tests.test_main_market_intelligence import _config as mi_config
    candles_df = make_deterministic_candles(70, base_price=100.0)
    monkeypatch.setattr(main, "load_dotenv", lambda: None)
    monkeypatch.setattr(main, "load_config", lambda: mi_config(tmp_path))
    monkeypatch.setattr(main, "make_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(main, "fetch_symbol_info", lambda client, symbol: _symbol_info())
    monkeypatch.setattr(main, "fetch_klines", lambda client, s, i, l, drop_incomplete=True: candles_df)
    monkeypatch.setattr(main, "enrich", lambda df: candles_df)
    monkeypatch.setattr(
        main, "latest_valid_row",
        lambda df: {
            "close": 100.0, "adx": 15.0, "atr": 1.0, "atr_pct": 0.01,
            "bb_width": 0.03, "volume_ratio": 1.0, "rsi": 50.0,
            "open_time": candles_df["open_time"].iloc[-1],
        },
    )
    monkeypatch.setattr(main, "fetch_ticker_price",
        lambda c, s: TickerSnapshot("BTCUSDT", Decimal("100.0"), datetime.now(timezone.utc)))
    monkeypatch.setattr(main, "fetch_book_ticker",
        lambda c, s: MarketQuote("BTCUSDT", Decimal("99.98"), Decimal("100.02"),
                                 Decimal("10"), Decimal("10"), datetime.now(timezone.utc)))
    monkeypatch.setattr(main, "fetch_account_snapshot",
        lambda c, b, q: AccountSnapshot("BTC", Decimal("1"), Decimal("0"), "USDT",
                                         Decimal("1000"), Decimal("0"), datetime.now(timezone.utc)))
    monkeypatch.setattr(main, "fetch_open_orders", lambda c, s: [])
    monkeypatch.setattr(main, "fetch_account_commission", lambda c, s: (None, "FALLBACK"))
    monkeypatch.setattr(main, "_SESSION_REFERENCE_EQUITY", None)
    monkeypatch.setattr(main, "evaluate_grid_eligibility",
        lambda *a, **kw: _allowed_intelligence())

    exit_code = main.main()
    assert exit_code == 0

    db = tmp_path / "grid.sqlite3"
    # No adaptive plan state (planner was skipped)
    assert get_state(db, "last_adaptive_plan") is None

    # Orders still placed normally
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT count(*) FROM orders").fetchone()[0]
        assert rows > 0
