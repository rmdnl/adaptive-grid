"""Multi-symbol orchestration: specification and regression tests.

Covers the v4.0 multi-symbol repairs:

- fail-closed .env-only credential selection (testnet only, live refused);
- per-symbol database paths;
- lifecycle ``close_active_plan`` (audited, idempotent CLOSE transition);
- paper liquidation accounting (SELL math, idempotency, no-inventory no-op);
- strategy auto-exit = cancel all + liquidate + close plan + cooldown,
  WITHOUT latching the permanent kill state;
- entry blocked / cooldown are healthy outcomes (success=True);
- auto-range approval is mandatory before grid work;
- the dedicated 15m lower-boundary gate fails closed on missing data;
- equity drawdown kill latches the kill state;
- per-symbol failure isolation in ``run_once``;
- the paper-cycle clock is anchored to the last closed candle.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

import multi_symbol_main as msm
from config_loader import ConfigError, resolve_binance_credentials
from grid_lifecycle import LifecycleAction, LifecycleManager, LifecycleState
from grid_planner import AdaptiveGridPlan, MarketRegime, PlanDecision
from market_data import AccountSnapshot, TickerSnapshot
from storage import (
    ensure_paper_account_state,
    get_kill_state,
    get_paper_account_state,
    get_state,
    init_db,
    record_paper_liquidation,
    set_state,
)
from paper_accounting import PaperAccountState


def _liquidate(db, *, event_id="autoexit-BNBUSDT-1", quantity="2",
               price="110", now=None):
    return record_paper_liquidation(
        db,
        event_id=event_id,
        symbol="BNBUSDT",
        price=Decimal(price),
        quantity=Decimal(quantity),
        taker_fee=Decimal("0.001"),
        fee_asset="USDT",
        reason="AUTO_EXIT: RSI 75 >= 70",
        now=now or datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------

def _symbol_info():
    return {
        "symbol": "BNBUSDT",
        "baseAsset": "BNB",
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


def _df(rows: int = 60) -> pd.DataFrame:
    """Synthetic closed-candle frame with precomputed indicator columns."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    records = []
    for i in range(rows):
        ts = base + timedelta(hours=4 * i)
        price = 90.0 + i * (20.0 / max(rows - 1, 1))
        records.append({
            "open_time": ts,
            "close_time": ts + timedelta(hours=4),
            "open": price, "high": price * 1.01, "low": price * 0.99,
            "close": price, "volume": 100.0,
            "quote_volume": price * 100.0,
            "trades": 10, "taker_base": 50.0, "taker_quote": 50.0,
            "ignore": 0,
            "adx": 10.0, "atr_pct": 0.006, "bb_width": 0.001,
            "volume_ratio": 1.0,
        })
    return pd.DataFrame(records)


def _ticker():
    return TickerSnapshot("BNBUSDT", Decimal("100.01"), datetime.now(timezone.utc))


def _account_snapshot(base_free: str = "0"):
    return AccountSnapshot(
        base_asset="BNB", base_free=Decimal(base_free), base_locked=Decimal("0"),
        quote_asset="USDT", quote_free=Decimal("1000"), quote_locked=Decimal("0"),
        fetched_at=datetime.now(timezone.utc),
    )


def _features(*, adx: str = "10", rsi: str = "30", close: str = "100",
              bb_lower: str = "100", bb_upper: str = "104",
              volume_oscillator: str = "0.5", z_score: str = "0"):
    from market_features import MarketFeatures
    return MarketFeatures(
        symbol="BNBUSDT",
        close_price=Decimal(close),
        atr=Decimal("0.6"), atr_pct=Decimal("0.006"),
        adx=Decimal(adx), plus_di=Decimal("20"), minus_di=Decimal("20"),
        bb_middle=Decimal("102"),
        bb_upper=Decimal(bb_upper), bb_lower=Decimal(bb_lower),
        bb_width=Decimal("4"), bb_width_pct=Decimal("0.04"),
        current_volume=Decimal("100"), baseline_volume=Decimal("100"),
        volume_spike_ratio=Decimal("1.0"),
        directional_efficiency=Decimal("0.2"),
        atr_expansion_ratio=Decimal("1.0"),
        range_containment_pct=Decimal("0.95"),
        penetration_count=0, spread=None, spread_pct=None,
        rsi=Decimal(rsi), volume_oscillator=Decimal(volume_oscillator),
        z_score=Decimal(z_score),
    )


def _config(tmp_path, *, timeframe: str = "4h") -> dict:
    return {
        "environment": {
            "mode": "testnet", "dry_run": True, "allow_live_execution": False,
        },
        "symbols": "BNBUSDT",
        "timeframe": timeframe,
        "grid": {
            "mode_by_symbol": {"BNBUSDT": "arithmetic"},
            "min_gross_profit_pct": Decimal("0.005"),
            "hard_min_net_pct": Decimal("0.003"),
            "preferred_net_max_pct": Decimal("0.004"),
            "min_cells": 6,
            "max_levels": 40,
        },
        "range": {
            "mode": "manual",
            "lower_price": Decimal("98"),
            "upper_price": Decimal("103"),
            "lookback": 200,
            "buffer_pct": Decimal("0.01"),
            "auto": {},
        },
        "strategy": {
            "entry": {
                "adx_max": Decimal("20"), "rsi_max": Decimal("35"),
                "bb_percent_b_max": Decimal("0"),
                "volume_oscillator_min": Decimal("0"),
            },
            "exit": {
                "rsi_min": Decimal("70"), "adx_min": Decimal("25"),
                "bb_percent_b_min": Decimal("1"),
                "zscore_threshold": Decimal("2.5"),
            },
            "cooldown_hours": 3,
        },
        "market_filter": {
            "adx_max": Decimal("28"), "atr_pct_max": Decimal("0.025"),
            "bb_width_max": Decimal("0.06"), "volume_spike_max": Decimal("2.5"),
        },
        "execution": {
            "prefer_limit_maker": True, "stale_order_minutes": 30,
            "max_open_orders": 40, "order_quote_size": Decimal("25"),
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
            "maker_fee": Decimal("0.001"), "taker_fee": Decimal("0.001"),
            "fee_asset": "USDT",
        },
        "risk": {
            "max_equity_drawdown_pct": Decimal("0.02"),
            "range_break_buffer_pct": Decimal("0.01"),
            "daily_profit_lock_pct": Decimal("0.01"),
            "cooldown_minutes": 30,
            "stop_if_below_lower_pct": Decimal("0.02"),
        },
        "logging": {
            "sqlite_path": str(tmp_path / "grid.sqlite3"),
            "log_path": str(tmp_path / "grid.log"),
            "csv_path": str(tmp_path / "trades.csv"),
        },
    }


def _install_runner_stubs(monkeypatch, tmp_path, *, features=None,
                          open_orders=(), account=None, symbol="BNBUSDT"):
    """Stub the multi-symbol cycle's market-facing inputs on tmp DBs."""
    cfg = _config(tmp_path)
    monkeypatch.setattr(msm, "fetch_symbol_info",
                        lambda client, sym: _symbol_info())
    monkeypatch.setattr(msm, "fetch_klines",
                        lambda client, sym, interval, limit, drop_incomplete=True: _df())
    monkeypatch.setattr(msm, "enrich", lambda df: df)
    monkeypatch.setattr(msm, "latest_valid_row",
                        lambda df: {
                            "close": Decimal("100.00"),
                            "atr_pct": Decimal("0.006"),
                            "adx": Decimal("10"),
                            "bb_width": Decimal("0.001"),
                            "volume_ratio": Decimal("1.0"),
                            "rsi": Decimal("50"),
                            "volume_oscillator": Decimal("0.5"),
                            "z_score": Decimal("0"),
                        })
    monkeypatch.setattr(msm, "fetch_ticker_price", lambda client, sym: _ticker())
    monkeypatch.setattr(msm, "fetch_book_ticker",
                        lambda client, sym: (_ for _ in ()).throw(
                            RuntimeError("no book ticker in test")))
    monkeypatch.setattr(msm, "fetch_account_snapshot",
                        lambda client, base, quote: (
                            account if account is not None
                            else _account_snapshot()))
    monkeypatch.setattr(msm, "fetch_account_commission",
                        lambda client, sym: (None, "FALLBACK"))
    monkeypatch.setattr(msm, "fetch_open_orders", lambda client, sym: open_orders)
    if features is not None:
        # Market-intelligence section present so features are evaluated.
        cfg["market_intelligence"] = {
            "timeframe": cfg["timeframe"], "min_candles": 60,
            "max_candle_age_seconds": 14400,
            "atr_period": 14, "adx_period": 14, "bb_length": 20,
            "bb_std_mult": 2.0, "volume_baseline_period": 20,
            "range_stability_period": 20,
            "regime": {"adx_trend_min": 25, "atr_expansion_ratio": 1.5,
                       "price_range_inclusion_min": 0.90,
                       "directional_efficiency_max": 0.60},
            "liquidity": {"max_spread_pct": 0.003,
                          "max_quote_ticker_age_seconds": 10},
            "quality": {"min_range_quality_score": 60,
                        "weight_trend_stability": 0.25,
                        "weight_volatility_suitability": 0.20,
                        "weight_bb_width_suitability": 0.15,
                        "weight_volume_stability": 0.10,
                        "weight_spread_suitability": 0.10,
                        "weight_range_containment": 0.20},
        }
        monkeypatch.setattr(msm, "calculate_market_features",
                            lambda **kwargs: features)
        monkeypatch.setattr(msm, "classify_market_regime",
                            lambda features, cfg: (MarketRegime.RANGE, None))
        monkeypatch.setattr(msm, "calculate_range_quality",
                            lambda features, cfg: SimpleNamespace(
                                score=Decimal("80")))
    return cfg


def _make_runner(monkeypatch, tmp_path, cfg, symbol="BNBUSDT"):
    db_path = msm._symbol_db_path(cfg["logging"]["sqlite_path"], symbol)
    from shutdown import ShutdownCoordinator
    runner = msm.SymbolCycleRunner(symbol, cfg, db_path, object(),
                                   logging.getLogger("test"),
                                   ShutdownCoordinator())
    return runner, db_path


def _seed_active_plan(db_path: str, plan_id: str = "test-plan-0001") -> str:
    manager = LifecycleManager(db_path)
    plan = AdaptiveGridPlan(
        plan_id=plan_id, pair="BNBUSDT", regime=MarketRegime.RANGE,
        range_quality_score=Decimal("85"),
        candidate_lower=Decimal("98"), candidate_upper=Decimal("103"),
        grid_type="GEOMETRIC", grid_step=Decimal("0.006"), grid_count=8,
        levels=(), total_quote_budget=Decimal("0"),
        buy_quote_budget=Decimal("0"),
        required_base_inventory=Decimal("2"),
        available_base_inventory=Decimal("2"),
        inventory_sufficient=True,
        estimated_net_profit_per_grid=Decimal("0.0035"),
        decision=PlanDecision.GRID_ALLOWED, reasons=(),
    )
    manager.activate_plan(plan, {"execution": {"order_quote_size": 25},
                                 "fees": {"maker_fee_fallback": 0.001}})
    return plan.plan_id


def _seed_paper_account(db_path: str, base_free: str = "2") -> None:
    ensure_paper_account_state(db_path, PaperAccountState(
        base_asset="BNB", quote_asset="USDT",
        base_free=Decimal(base_free), base_reserved=Decimal("0"),
        quote_free=Decimal("1000"), quote_reserved=Decimal("0"),
        average_cost=Decimal("100"), realized_pnl=Decimal("0"),
        total_fees=Decimal("0"),
        updated_at=datetime.now(timezone.utc),
    ))


# ---------------------------------------------------------------------------
# Credential selection (fail-closed, .env only)
# ---------------------------------------------------------------------------

def test_credentials_testnet_ok(monkeypatch):
    monkeypatch.setenv("BINANCE_ENV", "testnet")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "k")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "s")
    env, key, secret = resolve_binance_credentials({"environment": {"mode": "testnet"}})
    assert env == "testnet" and key == "k" and secret == "s"


def test_credentials_refuse_live(monkeypatch):
    monkeypatch.setenv("BINANCE_ENV", "live")
    monkeypatch.setenv("BINANCE_LIVE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_LIVE_API_SECRET", "s")
    with pytest.raises(ConfigError, match="live execution is not implemented"):
        resolve_binance_credentials({"environment": {"mode": "testnet"}})


def test_credentials_require_testnet_keys(monkeypatch):
    monkeypatch.setenv("BINANCE_ENV", "testnet")
    monkeypatch.delenv("BINANCE_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET_API_SECRET", raising=False)
    with pytest.raises(ConfigError, match="BINANCE_TESTNET_API_KEY"):
        resolve_binance_credentials({"environment": {"mode": "testnet"}})


def test_credentials_refuse_mode_mismatch(monkeypatch):
    monkeypatch.setenv("BINANCE_ENV", "testnet")
    with pytest.raises(ConfigError, match="environment.mode"):
        resolve_binance_credentials({"environment": {"mode": "live"}})


def test_symbol_db_path():
    path = msm._symbol_db_path("./data/grid_bot.sqlite3", "BTCUSDT")
    assert path.endswith("grid_bot_BTCUSDT.sqlite3")


# ---------------------------------------------------------------------------
# Lifecycle close_active_plan
# ---------------------------------------------------------------------------

def test_close_active_plan_closes_and_audits(tmp_path):
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    plan_id = _seed_active_plan(db)
    manager = LifecycleManager(db)
    assert manager.get_current_state() is LifecycleState.ACTIVE

    assert manager.close_active_plan(reason="AUTO_EXIT",
                                     details={"price": "100.01"}) is True
    assert manager.get_current_state() is LifecycleState.NO_ACTIVE_GRID
    assert manager.get_active_plan() is None
    row = sqlite3.connect(db).execute(
        "SELECT lifecycle_state FROM active_plans WHERE plan_id=?",
        (plan_id,)).fetchone()
    assert row[0] == "CLOSED"
    transitions = manager.get_transition_history(limit=10)
    assert any(t.action is LifecycleAction.CLOSE and t.to_state is LifecycleState.NO_ACTIVE_GRID
               for t in transitions)


def test_close_active_plan_is_idempotent(tmp_path):
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    _seed_active_plan(db)
    manager = LifecycleManager(db)
    assert manager.close_active_plan(reason="AUTO_EXIT") is True
    assert manager.close_active_plan(reason="AUTO_EXIT") is False


def test_close_active_plan_noop_without_plan(tmp_path):
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    manager = LifecycleManager(db)
    assert manager.close_active_plan(reason="AUTO_EXIT") is False


# ---------------------------------------------------------------------------
# Paper liquidation accounting
# ---------------------------------------------------------------------------

def test_liquidation_sell_math_quote_fee(tmp_path):
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    _seed_paper_account(db, base_free="2")
    now = datetime.now(timezone.utc)
    summary = _liquidate(db, now=now)
    assert summary["liquidated"] is True
    assert summary["idempotent"] is False
    assert Decimal(summary["gross_quote"]) == Decimal("220")
    assert Decimal(summary["fee_amount"]) == Decimal("0.22")
    assert Decimal(summary["realized_pnl_delta"]) == Decimal("19.78")

    state = get_paper_account_state(db)
    assert state["base_free"] == Decimal("0")
    assert state["quote_free"] == Decimal("1219.78")
    assert state["realized_pnl"] == Decimal("19.78")
    assert state["total_fees"] == Decimal("0.22")
    event = sqlite3.connect(db).execute(
        "SELECT event_type FROM paper_accounting_events WHERE "
        "event_type='LIQUIDATION'").fetchone()
    assert event is not None


def test_liquidation_no_inventory_is_noop(tmp_path):
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    _seed_paper_account(db, base_free="0")
    summary = _liquidate(db)
    assert summary["liquidated"] is False
    assert summary["reason"] == "NO_BASE_INVENTORY"


def test_liquidation_replay_is_idempotent(tmp_path):
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    _seed_paper_account(db, base_free="2")
    now = datetime.now(timezone.utc)
    first = _liquidate(db, now=now)
    assert first["liquidated"] is True
    state_after_first = get_paper_account_state(db)
    # Same event id + same semantics -> acknowledged as already applied.
    replay = record_paper_liquidation(
        db,
        event_id="autoexit-BNBUSDT-1",
        symbol="BNBUSDT",
        price=Decimal("110"),
        quantity=Decimal("0"),  # nothing free left anyway
        taker_fee=Decimal("0.001"),
        fee_asset="USDT",
        reason="AUTO_EXIT: RSI 75 >= 70",
        now=now,
    )
    assert replay["liquidated"] is False
    assert get_paper_account_state(db) == state_after_first


def test_liquidation_requires_nonempty_reason(tmp_path):
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    _seed_paper_account(db, base_free="2")
    with pytest.raises(ValueError, match="reason"):
        record_paper_liquidation(
            db, event_id="e", symbol="BNBUSDT", price=Decimal("110"),
            quantity=Decimal("1"), taker_fee=Decimal("0.001"),
            fee_asset="USDT", reason="  ")


# ---------------------------------------------------------------------------
# Paper clock anchoring
# ---------------------------------------------------------------------------

def test_paper_clock_anchored_to_last_closed_candle():
    df = _df(rows=3)
    clock = msm._paper_clock(df, "4h")
    anchored = clock()
    last_close = df["close_time"].iloc[-1].to_pydatetime()
    assert anchored == last_close + timedelta(hours=4)


def test_paper_clock_falls_back_to_wall_clock():
    clock = msm._paper_clock(None, "4h")
    before = datetime.now(timezone.utc)
    value = clock()
    after = datetime.now(timezone.utc)
    assert before <= value.replace(tzinfo=timezone.utc) <= after


# ---------------------------------------------------------------------------
# Full cycle outcomes through SymbolCycleRunner
# ---------------------------------------------------------------------------

def test_ok_path_runs_paper_cycle_and_submits_orders(monkeypatch, tmp_path):
    features = _features()  # ADX 10, RSI 30, %B 0, VolOsc 0.5 -> entry allowed
    cfg = _install_runner_stubs(monkeypatch, tmp_path, features=features)
    runner, db_path = _make_runner(monkeypatch, tmp_path, cfg)

    result = runner.run_cycle()

    assert result["success"] is True, result["error"]
    assert result["status"] == "OK"
    assert result["combined_allowed"] is True
    assert result["cycle_result"]["orders_submitted"] > 0
    # ATR-driven step floor: max(0.006, 0.005) = 0.006
    assert Decimal(result["dynamic_step_pct"]) == Decimal("0.006")


def test_entry_blocked_is_a_healthy_outcome(monkeypatch, tmp_path):
    features = _features(adx="30")  # ADX 30 >= 20 -> entry blocked
    cfg = _install_runner_stubs(monkeypatch, tmp_path, features=features)
    runner, db_path = _make_runner(monkeypatch, tmp_path, cfg)

    result = runner.run_cycle()

    assert result["success"] is True
    assert result["status"] == "ENTRY_BLOCKED"
    assert result["combined_allowed"] is False
    # Entry blocked is a decision, not a cycle failure: no paper cycle ran.
    assert "cycle_result" not in result


def test_entry_cooldown_blocks_new_entry(monkeypatch, tmp_path):
    features = _features()
    cfg = _install_runner_stubs(monkeypatch, tmp_path, features=features)
    runner, db_path = _make_runner(monkeypatch, tmp_path, cfg)
    msm._record_auto_exit_ts(db_path)  # cooldown active from "just now"

    result = runner.run_cycle()

    assert result["success"] is True
    assert result["status"] == "ENTRY_COOLDOWN"


def test_auto_exit_cancels_liquidates_closes_plan_and_cooldowns(
        monkeypatch, tmp_path):
    features = _features(rsi="75")  # RSI 75 >= 70 -> auto-exit
    cfg = _install_runner_stubs(monkeypatch, tmp_path, features=features)
    runner, db_path = _make_runner(monkeypatch, tmp_path, cfg)
    plan_id = _seed_active_plan(db_path)
    _seed_paper_account(db_path, base_free="2")

    result = runner.run_cycle()

    assert result["success"] is True
    assert result["status"] == "AUTO_EXIT"
    assert result["exit_reasons"] and "RSI" in result["exit_reasons"][0]
    # Liquidated: free base inventory sold at the market price.
    assert result["liquidation"]["liquidated"] is True
    state = get_paper_account_state(db_path)
    assert state["base_free"] == Decimal("0")
    # Active plan closed -> auto-entry can be evaluated again later.
    manager = LifecycleManager(db_path)
    assert manager.get_active_plan() is None
    assert manager.get_current_state() is LifecycleState.NO_ACTIVE_GRID
    # Cooldown timestamp recorded.
    assert msm._load_last_auto_exit_ts(db_path) is not None
    # STRATEGY exit must NOT latch the permanent kill state.
    kill = get_kill_state(db_path)
    assert kill is None or not kill.get("active")


def test_auto_exit_repeat_visit_is_flat_noop(monkeypatch, tmp_path):
    features = _features(rsi="75")
    cfg = _install_runner_stubs(monkeypatch, tmp_path, features=features)
    runner, db_path = _make_runner(monkeypatch, tmp_path, cfg)
    _seed_active_plan(db_path)
    _seed_paper_account(db_path, base_free="2")
    assert runner.run_cycle()["status"] == "AUTO_EXIT"

    # Exit conditions persist and a NEW plan was activated meanwhile: the
    # repeat exit must be a safe no-op for balances (already flat) while
    # closing the new plan and keeping the kill state untouched.
    _seed_active_plan(db_path, plan_id="test-plan-0002")
    second = runner.run_cycle()

    assert second["success"] is True
    assert second["status"] == "AUTO_EXIT"
    assert second["liquidation"]["liquidated"] is False
    assert second["liquidation"]["reason"] == "NO_BASE_INVENTORY"
    manager = LifecycleManager(db_path)
    assert manager.get_current_state() is LifecycleState.NO_ACTIVE_GRID
    # Still no kill latch, still flat.
    kill = get_kill_state(db_path)
    assert kill is None or not kill.get("active")
    assert get_paper_account_state(db_path)["base_free"] == Decimal("0")


def test_auto_range_rejection_blocks_cycle(monkeypatch, tmp_path):
    cfg = _install_runner_stubs(monkeypatch, tmp_path, features=None)
    cfg["range"]["mode"] = "auto"
    cfg["range"]["auto"] = {
        "support_quantile": 0.10, "resistance_quantile": 0.90,
        "min_width_pct": 0.50,   # synthetic width ~22% -> outside limits
        "max_width_pct": 0.60,
        "min_quality_score": 65, "require_price_inside": True,
    }
    runner, db_path = _make_runner(monkeypatch, tmp_path, cfg)

    result = runner.run_cycle()

    assert result["success"] is True
    assert result["status"] == "RANGE_BLOCKED"
    assert "RANGE_WIDTH_OUTSIDE_LIMIT" in result["combined_reason"]
    # No grid work, no orders.
    assert "cycle_result" not in result
    assert result["kill_triggered"] is False


def test_missing_15m_close_fails_closed_without_latching(monkeypatch, tmp_path):
    features = _features()
    cfg = _install_runner_stubs(monkeypatch, tmp_path, features=features)
    monkeypatch.setattr(msm, "fetch_15m_closed_close",
                        lambda client, symbol: None)
    runner, db_path = _make_runner(monkeypatch, tmp_path, cfg)

    result = runner.run_cycle()

    assert result["success"] is True
    assert result["status"] == "RISK_BLOCKED"
    assert "LOWER_BOUNDARY_STOP_DATA_UNAVAILABLE" in result["combined_reason"]
    # A data-unavailable veto is fail-closed but NOT a kill latch.
    kill = get_kill_state(db_path)
    assert kill is None or not kill.get("active")


def test_equity_drawdown_kill_latches(monkeypatch, tmp_path):
    features = _features()
    cfg = _install_runner_stubs(monkeypatch, tmp_path, features=features)
    runner, db_path = _make_runner(monkeypatch, tmp_path, cfg)
    # Peak equity 1100 vs current 1000 -> 9.09% drawdown >= 2% kill switch.
    set_state(db_path, "paper_reference_equity", "1100")

    result = runner.run_cycle()

    assert result["success"] is True
    assert result["status"] == "KILL_TRIGGERED"
    assert result["kill_triggered"] is True
    kill = get_kill_state(db_path)
    assert kill is not None and kill.get("active") is True
    assert "EQUITY_DRAWDOWN_KILL" in kill.get("trigger", "")


def test_run_once_isolates_per_symbol_failures(monkeypatch, tmp_path):
    cfg = _install_runner_stubs(monkeypatch, tmp_path, features=None)
    cfg["_parsed_symbols"] = ["BTCUSDT", "ETHUSDT"]
    cfg["grid"]["mode_by_symbol"] = {
        "BTCUSDT": "arithmetic", "ETHUSDT": "arithmetic"}

    calls = {"btc": 0}

    def fake_symbol_info(client, symbol):
        if symbol == "BTCUSDT":
            calls["btc"] += 1
            # First call: the GLOBAL risk evaluation (succeeds). Later calls:
            # the per-symbol cycle (fails — isolated per symbol).
            if calls["btc"] > 1:
                raise RuntimeError("no exchangeInfo for BTCUSDT (simulated)")
            return _symbol_info()
        return _symbol_info()

    monkeypatch.setattr(msm, "fetch_symbol_info", fake_symbol_info)
    logger = logging.getLogger("test-run-once")
    from shutdown import ShutdownCoordinator
    exit_code = msm.run_once(cfg, logger, object(),
                             cfg["_parsed_symbols"], ShutdownCoordinator())

    assert exit_code == 0
    assert calls["btc"] == 2  # global evaluation + isolated cycle failure
    # The surviving symbol produced per-symbol state.
    eth_db = msm._symbol_db_path(cfg["logging"]["sqlite_path"], "ETHUSDT")
    assert get_state(eth_db, "last_risk_decision") is not None
