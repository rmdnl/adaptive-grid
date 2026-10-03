import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd
import pytest

import main
from config_loader import ConfigError, validate_config
from market_data import AccountSnapshot, OpenOrder, TickerSnapshot
from risk_engine import RiskDecision
from storage import get_state


def _candles_df():
    """Real closed-candle DataFrame so the orchestrator's market-freshness
    gate sees valid candle data (the FIX 4B path routes paper execution
    through PaperSession.run_cycle, which validates kline_df)."""
    now = datetime.now(timezone.utc)
    return pd.DataFrame([
        {
            "open_time": now - pd.Timedelta(minutes=30),
            "close_time": now - pd.Timedelta(minutes=15),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.01,
            "volume": 100.0,
        },
    ])


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


def _latest_row():
    return {
        "close": Decimal("100.00"),
        "atr_pct": Decimal("0.001"),
        "adx": Decimal("10"),
        "bb_width": Decimal("0.001"),
        "volume_ratio": Decimal("1.0"),
        "rsi": Decimal("50"),
        "volume_oscillator": Decimal("0.5"),  # Positive to pass entry check
        "z_score": Decimal("0"),  # Neutral to pass exit check
    }


def _ticker():
    return TickerSnapshot(
        symbol="BNBUSDT",
        price=Decimal("100.01"),
        fetched_at=datetime.now(timezone.utc),
    )


def _account_snapshot():
    return AccountSnapshot(
        base_asset="BNB",
        base_free=Decimal("1.0"),
        base_locked=Decimal("0"),
        quote_asset="USDT",
        quote_free=Decimal("1000"),
        quote_locked=Decimal("0"),
        fetched_at=datetime.now(timezone.utc),
    )


def _open_order():
    return OpenOrder(
        order_id=12345,
        client_order_id="BINANCE-OPEN-1",
        symbol="BNBUSDT",
        side="BUY",
        order_type="LIMIT",
        status="NEW",
        price=Decimal("100.00"),
        orig_qty=Decimal("0.25"),
        executed_qty=Decimal("0"),
        time_in_force="GTC",
        is_working=True,
    )


def _config(tmp_path, dry_run=True):
    return {
        "environment": {
            "mode": "testnet",
            "dry_run": dry_run,
            "allow_live_execution": False,
        },
        "symbols": "BNBUSDT",
        "timeframe": "4h",
        "grid": {
            "mode_by_symbol": {
                "BNBUSDT": "arithmetic"
            },
            "step_pct": Decimal("0.006"),  # Legacy field for main.py compatibility
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
                "adx_max": Decimal("20"),
                "rsi_max": Decimal("35"),
                "bb_percent_b_max": Decimal("0"),
                "volume_oscillator_min": Decimal("0"),
            },
            "exit": {
                "rsi_min": Decimal("70"),
                "adx_min": Decimal("25"),
                "bb_percent_b_min": Decimal("1"),
                "zscore_threshold": Decimal("2.5"),
            },
            "cooldown_hours": 3,
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
            "stop_if_below_lower_pct": Decimal("0.02"),
        },
        "logging": {
            "sqlite_path": str(tmp_path / "grid.sqlite3"),
            "log_path": str(tmp_path / "grid.log"),
            "csv_path": str(tmp_path / "trades.csv"),
        },
    }


def _install_main_stubs(monkeypatch, tmp_path, dry_run=True, open_orders=(), risk_allowed=True):
    monkeypatch.setattr(main, "load_dotenv", lambda: None)
    monkeypatch.setattr(main, "load_config", lambda: _config(tmp_path, dry_run=dry_run))
    monkeypatch.setattr(main, "make_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(main, "fetch_symbol_info", lambda client, symbol: _symbol_info())
    monkeypatch.setattr(main, "fetch_klines", lambda client, symbol, interval, limit, drop_incomplete=True: _candles_df())
    monkeypatch.setattr(main, "enrich", lambda df: None)
    monkeypatch.setattr(main, "latest_valid_row", lambda df: _latest_row())
    monkeypatch.setattr(main, "fetch_ticker_price", lambda client, symbol: _ticker())
    monkeypatch.setattr(main, "fetch_account_snapshot", lambda client, base_asset, quote_asset: _account_snapshot())
    monkeypatch.setattr(main, "fetch_open_orders", lambda client, symbol: open_orders)
    monkeypatch.setattr(main, "fetch_account_commission", lambda client, symbol: (None, "FALLBACK"))
    if not risk_allowed:
        monkeypatch.setattr(
            main,
            "market_gate",
            lambda last, cfg: RiskDecision(False, ("MARKET_FILTER_BLOCK:ADX",)),
        )
    # PATCH 1 (F-H1): the session-local reference equity was replaced by the
    # persisted ``paper_reference_equity`` bot_state key.  Each test uses a
    # fresh tmp_path DB, so there is no reference to reset here.


def _order_rows(db_path):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT client_order_id, side, status FROM orders"
        ).fetchall()


def test_risk_allow_submits_paper_orders(monkeypatch, tmp_path):
    _install_main_stubs(monkeypatch, tmp_path)

    assert main.main() == 0

    rows = _order_rows(tmp_path / "grid.sqlite3")
    assert rows
    assert {row[1] for row in rows} == {"BUY", "SELL"}
    assert {row[2] for row in rows} == {"OPEN"}
    assert len({row[0] for row in rows}) == len(rows)


def test_risk_deny_does_not_submit_paper_orders(monkeypatch, tmp_path):
    _install_main_stubs(monkeypatch, tmp_path, risk_allowed=False)

    assert main.main() == 0

    db_path = tmp_path / "grid.sqlite3"
    assert _order_rows(db_path) == []
    risk_state = json.loads(get_state(db_path, "last_risk_decision"))
    assert risk_state["allowed"] is False
    assert "MARKET_FILTER_BLOCK:ADX" in risk_state["reason"]


def test_repeated_main_execution_does_not_duplicate_paper_orders(monkeypatch, tmp_path):
    _install_main_stubs(monkeypatch, tmp_path)
    db_path = tmp_path / "grid.sqlite3"

    assert main.main() == 0
    first_rows = _order_rows(db_path)
    assert main.main() == 0
    second_rows = _order_rows(db_path)

    assert len(second_rows) == len(first_rows)
    assert {row[0] for row in second_rows} == {row[0] for row in first_rows}


def test_main_paper_cycle_submits_open_orders(monkeypatch, tmp_path):
    """FIX 4B: main.py routes paper execution through the orchestrator cycle.

    An allowed, in-range run must produce OPEN paper orders via
    ``PaperSession.run_cycle`` (replacing the removed per-order
    ``_submit_paper_orders`` helper).  Orders carry the generation-aware
    ``AG-...`` client-order identity owned by the orchestrator.
    """
    _install_main_stubs(monkeypatch, tmp_path)

    assert main.main() == 0

    db_path = tmp_path / "grid.sqlite3"
    rows = _order_rows(db_path)
    assert rows
    assert {row[1] for row in rows} == {"BUY", "SELL"}
    assert {row[2] for row in rows} == {"OPEN"}
    # Every client_order_id is generation-marked and unique (orchestrator-issued).
    ids = [row[0] for row in rows]
    assert len(ids) == len(set(ids))
    assert all(cid.startswith("AG-BNBUSDT-G") for cid in ids)

    # The cycle result is surfaced through persisted state.
    state = json.loads(get_state(db_path, "last_paper_orders"))
    assert state["success"] is True
    assert state["orders_submitted"] == len(rows)
    assert state["recovery_healthy"] is True
    assert state["plan_decision"] == "GRID_ALLOWED"
    assert state["lifecycle_transition"] == "ACTIVE"


def test_main_paper_cycle_out_of_range_submits_no_orders(monkeypatch, tmp_path):
    """FIX 4B adversarial: an out-of-range/zero-cell plan submits ZERO orders.

    A configured range too narrow to produce a valid grid is blocked upstream
    by main.py's own gates (combined.allowed=False), so the orchestrator cycle
    is never entered — no partial order set can be created.
    """
    _install_main_stubs(monkeypatch, tmp_path)
    db_path = tmp_path / "grid.sqlite3"
    # Narrow the manual range below a single grid cell → no valid grid.
    cfg = _config(tmp_path)
    cfg["range"]["lower_price"] = Decimal("100")
    cfg["range"]["upper_price"] = Decimal("100.5")
    monkeypatch.setattr(main, "load_config", lambda: cfg)

    assert main.main() == 0

    with sqlite3.connect(db_path) as connection:
        orders = connection.execute("SELECT count(*) FROM orders").fetchone()[0]
        risk = json.loads(get_state(db_path, "last_risk_decision"))
    assert orders == 0
    assert risk["allowed"] is False
    assert "GRID" in risk["reason"]


def test_main_paper_cycle_is_atomic_rollback_on_later_order_failure(monkeypatch, tmp_path):
    """FIX 4B (F-2): a later-order failure rolls back the ENTIRE cycle.

    Inject a failure into the orchestrator's order submission so the second
    order raises.  The single cycle transaction must roll back as a whole:
    zero orders, zero reservations, zero lifecycle mutations survive, and the
    cycle reports a deterministic failure without leaking the exception.
    """
    _install_main_stubs(monkeypatch, tmp_path)
    db_path = str(tmp_path / "grid.sqlite3")

    from paper_accounting import PaperAccountingEngine
    from paper_orchestrator import PaperSession

    # Pre-build the session exactly as main.py would (same accounting + DB),
    # then inject a failure on the 2nd submission and drive run_cycle directly.
    symbol_info = _symbol_info()
    from symbol_rules import parse_symbol_info as _parse_rules
    rules = _parse_rules(symbol_info)
    accounting = PaperAccountingEngine(
        rules.base_asset,
        rules.quote_asset,
        Decimal(str(_config(tmp_path)["paper"]["initial_base_balance"])),
        Decimal(str(_config(tmp_path)["paper"]["initial_quote_balance"])),
        Decimal("0.001"),
        Decimal("0.001"),
        "USDT",
    )
    session = PaperSession(db_path, db_path, accounting, client_order_prefix="AG")
    engine = session.order_engine

    original_submit = engine.submit
    calls = {"n": 0}

    def spy_submit(intent, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("injected mid-cycle order failure (F-2 regression)")
        return original_submit(intent, *args, **kwargs)

    monkeypatch.setattr(engine, "submit", spy_submit)

    cfg = _config(tmp_path)
    from market_regime import MarketRegime
    from paper_orchestrator import PaperCycleInput
    from storage import init_db
    init_db(db_path)

    now = datetime.now(timezone.utc)
    df = pd.DataFrame([
        {
            "open_time": now - pd.Timedelta(minutes=30),
            "close_time": now - pd.Timedelta(minutes=15),
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.01,
            "volume": 100.0,
        }
    ])
    cycle_input = PaperCycleInput(
        candle_index=1,
        symbol="BNBUSDT",
        current_price=Decimal("100.01"),
        kline_df=df,
        lower_price=Decimal("98"),
        upper_price=Decimal("103"),
        active_plan=None,
        regime=MarketRegime.RANGE,
        range_quality_score=Decimal("100"),
        cfg=cfg,
        maker_fee=Decimal("0.001"),
        taker_fee=Decimal("0.001"),
        fee_asset="USDT",
        risk_decision=RiskDecision(True),
        clock=lambda: now,
        dry_run=True,
        rules=rules,
    )

    # The failure is contained inside the cycle (no exception escapes).
    result = session.run_cycle(cycle_input)

    # Deterministic failure; no exception leaked to the caller.
    assert result.success is False
    assert result.error is not None and "CYCLE_ROLLED_BACK" in result.error
    assert calls["n"] >= 2, "expected at least two submissions before failure"

    # Atomic rollback: ZERO order/lifecycle mutations survive.
    with sqlite3.connect(db_path) as connection:
        order_rows = connection.execute(
            "SELECT count(*) FROM orders").fetchone()[0]
        reservation_rows = connection.execute(
            "SELECT count(*) FROM paper_reservations").fetchone()[0]
    assert order_rows == 0
    assert reservation_rows == 0
    # Post-rollback recovery reports a healthy pre-cycle state.
    assert session.is_healthy() is True


def test_binance_open_order_reconciliation_remains_independent(monkeypatch, tmp_path):
    open_order = _open_order()
    _install_main_stubs(monkeypatch, tmp_path, open_orders=(open_order,))
    db_path = tmp_path / "grid.sqlite3"

    assert main.main() == 0

    paper_rows = _order_rows(db_path)
    assert paper_rows
    assert open_order.client_order_id not in {row[0] for row in paper_rows}
    reconciliation_state = json.loads(get_state(db_path, "last_open_order_reconciliation"))
    assert reconciliation_state == {
        "count": 1,
        "status": "VERIFIED",
        "error": None,
    }


def test_dry_run_false_is_rejected_by_main(monkeypatch, tmp_path):
    _install_main_stubs(monkeypatch, tmp_path, dry_run=False)

    with pytest.raises(RuntimeError, match="DRY_RUN must remain enabled"):
        main.main()


def test_live_trading_is_disabled_by_config(tmp_path):
    config = _config(tmp_path, dry_run=False)

    with pytest.raises(ConfigError, match="deliberately dry-run only"):
        validate_config(config)
