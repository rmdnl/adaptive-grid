import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

import main
from config_loader import ConfigError, validate_config
from market_data import AccountSnapshot, OpenOrder, TickerSnapshot
from order_engine import OrderPriceOutOfRange, PaperOrderEngine
from risk_engine import RiskDecision
from storage import get_state
from symbol_rules import OrderPlanCell, OrderPlanValidation


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
        "symbol": "BNBUSDT",
        "timeframe": "15m",
        "grid": {
            "step_pct": Decimal("0.006"),
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
    monkeypatch.setattr(main, "fetch_klines", lambda client, symbol, interval, limit, drop_incomplete=True: object())
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
    monkeypatch.setattr(main, "_SESSION_REFERENCE_EQUITY", None)


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


def test_paper_order_inside_effective_range_is_accepted(tmp_path):
    cell = OrderPlanCell(
        index=0,
        buy_price=Decimal("100.50"),
        sell_price=Decimal("101.50"),
        quantity=Decimal("0.25"),
        gross_pct=Decimal("0.006"),
        net_pct=Decimal("0.004"),
        allowed=True,
        reasons=(),
    )
    plan = OrderPlanValidation(
        allowed=True,
        reason="ORDER_PLAN_PASS",
        cells=(cell,),
        planned_open_orders=1,
        min_net_pct=Decimal("0.004"),
        effective_upper=Decimal("102.00"),
    )
    engine = PaperOrderEngine(str(tmp_path / "paper.sqlite3"))

    orders = main._submit_paper_orders(
        engine,
        plan,
        "BNBUSDT",
        RiskDecision(True),
        Decimal("100.00"),
        "LIMIT_MAKER",
        "AG",
        True,
    )

    assert len(orders) == 2
    assert {order.intent.side for order in orders} == {"BUY", "SELL"}
    assert {order.state.value for order in orders} == {"OPEN"}


def test_paper_order_outside_effective_range_is_rejected(tmp_path):
    cell = OrderPlanCell(
        index=0,
        buy_price=Decimal("99.00"),
        sell_price=Decimal("101.00"),
        quantity=Decimal("0.25"),
        gross_pct=Decimal("0.006"),
        net_pct=Decimal("0.004"),
        allowed=True,
        reasons=(),
    )
    plan = OrderPlanValidation(
        allowed=True,
        reason="ORDER_PLAN_PASS",
        cells=(cell,),
        planned_open_orders=1,
        min_net_pct=Decimal("0.004"),
        effective_upper=Decimal("101.00"),
    )
    engine = PaperOrderEngine(str(tmp_path / "paper.sqlite3"))

    with pytest.raises(OrderPriceOutOfRange):
        main._submit_paper_orders(
            engine,
            plan,
            "BNBUSDT",
            RiskDecision(True),
            Decimal("100.00"),
            "LIMIT_MAKER",
            "AG",
            True,
        )


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
