from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3

import pytest

from config_loader import ConfigError, validate_config
from order_engine import (
    OrderIntent,
    OrderState,
    PaperOrder,
    PaperOrderEngine,
    transition_order,
    make_client_order_id,
)
from paper_accounting import (
    InsufficientPaperFunds,
    PaperAccountingEngine,
    UnsupportedFeeAsset,
)
from risk_engine import RiskDecision
from storage import (
    PaperAccountingMigrationError,
    PaperAccountingStaleState,
    get_order,
    get_paper_account_state,
    get_paper_reservation,
    get_state,
    init_db,
    OrderSubmissionError,
    save_order_submission,
)


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
FILL_TIME = NOW + timedelta(minutes=1)


def _accounting(base="2", quote="1000", maker="0.001", taker="0.001", fee_asset="USDT"):
    return PaperAccountingEngine(
        "BNB",
        "USDT",
        Decimal(base),
        Decimal(quote),
        Decimal(maker),
        Decimal(taker),
        fee_asset,
    )


def _intent(index=0, side="BUY", price="100", quantity="1", order_type="LIMIT_MAKER", generation=0):
    return OrderIntent(
        client_order_id=make_client_order_id("AG", "BNBUSDT", generation, index, side),
        symbol="BNBUSDT",
        side=side,
        order_type=order_type,
        price=Decimal(price),
        quantity=Decimal(quantity),
        time_in_force="GTC",
        grid_index=index,
        generation=generation,
        created_at=NOW,
    )


def _engine(tmp_path, **accounting_kwargs):
    path = str(tmp_path / "paper.sqlite3")
    return PaperOrderEngine(
        path,
        clock=lambda: NOW,
        accounting=_accounting(**accounting_kwargs),
    )


def _submit(
    engine,
    index=0,
    side="BUY",
    price="100",
    quantity="1",
    order_type="LIMIT_MAKER",
    lower="90",
    upper="110",
):
    return engine.submit(
        _intent(
            index=index,
            side=side,
            price=price,
            quantity=quantity,
            order_type=order_type,
        ),
        RiskDecision(True),
        Decimal(lower),
        Decimal(upper),
    )


def _fill(engine, order, fill_id, market_price, quantity, **kwargs):
    return engine.apply_fill(
        order.intent.client_order_id,
        fill_id,
        "BNBUSDT",
        Decimal(market_price),
        Decimal(quantity),
        FILL_TIME,
        **kwargs,
    )


def test_valid_paper_configuration():
    cfg = {
        "environment": {"mode": "testnet", "dry_run": True, "allow_live_execution": False},
        "symbol": "BNBUSDT",
        "timeframe": "15m",
        "grid": {"step_pct": 0.006, "hard_min_net_pct": 0.003, "preferred_net_max_pct": 0.004, "min_cells": 6, "max_levels": 40},
        "range": {"mode": "auto", "lower_price": 0, "upper_price": 0},
        "fees": {"maker_fee_fallback": 0.001, "taker_fee_fallback": 0.001, "slippage_roundtrip_pct": 0.0005},
        "paper": {"initial_base_balance": 2, "initial_quote_balance": 1000, "maker_fee": 0.001, "taker_fee": 0.001, "fee_asset": "USDT"},
        "risk": {"max_equity_drawdown_pct": 0.02, "range_break_buffer_pct": 0.01},
        "execution": {"max_open_orders": 40, "order_quote_size": 25},
    }

    validate_config(cfg)


def test_missing_paper_configuration_fails_closed():
    cfg = {
        "environment": {"mode": "testnet", "dry_run": True, "allow_live_execution": False},
        "symbol": "BNBUSDT",
        "timeframe": "15m",
        "grid": {"step_pct": 0.006, "hard_min_net_pct": 0.003, "preferred_net_max_pct": 0.004, "min_cells": 6, "max_levels": 40},
        "range": {"mode": "auto", "lower_price": 0, "upper_price": 0},
        "fees": {"maker_fee_fallback": 0.001, "taker_fee_fallback": 0.001, "slippage_roundtrip_pct": 0.0005},
        "risk": {"max_equity_drawdown_pct": 0.02, "range_break_buffer_pct": 0.01},
        "execution": {"max_open_orders": 40, "order_quote_size": 25},
    }

    with pytest.raises(ConfigError, match="initial_base_balance"):
        validate_config(cfg)


@pytest.mark.parametrize("field", ["initial_base_balance", "initial_quote_balance"])
def test_invalid_paper_balance_fails_closed(field):
    cfg = {
        "environment": {"mode": "testnet", "dry_run": True, "allow_live_execution": False},
        "symbol": "BNBUSDT",
        "timeframe": "15m",
        "grid": {"step_pct": 0.006, "hard_min_net_pct": 0.003, "preferred_net_max_pct": 0.004, "min_cells": 6, "max_levels": 40},
        "range": {"mode": "auto", "lower_price": 0, "upper_price": 0},
        "fees": {"maker_fee_fallback": 0.001, "taker_fee_fallback": 0.001, "slippage_roundtrip_pct": 0.0005},
        "paper": {"initial_base_balance": 2, "initial_quote_balance": 1000, "maker_fee": 0.001, "taker_fee": 0.001, "fee_asset": "USDT"},
        "risk": {"max_equity_drawdown_pct": 0.02, "range_break_buffer_pct": 0.01},
        "execution": {"max_open_orders": 40, "order_quote_size": 25},
    }
    cfg["paper"][field] = -1

    with pytest.raises(ConfigError, match=field):
        validate_config(cfg)


def test_buy_reservation_sufficient_quote(tmp_path):
    engine = _engine(tmp_path, base="0", quote="1000")
    order = _submit(engine, price="100", quantity="1")

    state = get_paper_account_state(engine.db_path)
    reservation = get_paper_reservation(engine.db_path, order.intent.client_order_id)

    assert state["quote_free"] == Decimal("900")
    assert state["quote_reserved"] == Decimal("100")
    assert reservation["asset"] == "USDT"
    assert reservation["original_amount"] == Decimal("100")
    assert reservation["remaining_amount"] == Decimal("100")


def test_buy_reservation_insufficient_quote(tmp_path):
    engine = _engine(tmp_path, base="0", quote="99")

    with pytest.raises(InsufficientPaperFunds, match="USDT"):
        _submit(engine, price="100", quantity="1")

    assert get_paper_account_state(engine.db_path)["quote_free"] == Decimal("99")


def test_sell_reservation_sufficient_base(tmp_path):
    engine = _engine(tmp_path, base="2", quote="0")
    order = _submit(engine, side="SELL", price="100", quantity="1")

    state = get_paper_account_state(engine.db_path)
    reservation = get_paper_reservation(engine.db_path, order.intent.client_order_id)

    assert state["base_free"] == Decimal("1")
    assert state["base_reserved"] == Decimal("1")
    assert reservation["asset"] == "BNB"
    assert reservation["original_amount"] == Decimal("1")


def test_sell_reservation_insufficient_base(tmp_path):
    engine = _engine(tmp_path, base="0", quote="1000")

    with pytest.raises(InsufficientPaperFunds, match="BNB"):
        _submit(engine, side="SELL", price="100", quantity="1")

    assert get_paper_account_state(engine.db_path)["base_free"] == Decimal("0")


def test_duplicate_submission_does_not_double_reserve(tmp_path):
    engine = _engine(tmp_path, base="2", quote="1000")
    order = _submit(engine, price="100", quantity="1")

    with pytest.raises(Exception, match="Duplicate"):
        engine.submit(order.intent, RiskDecision(True), Decimal("90"), Decimal("110"))

    assert get_paper_account_state(engine.db_path)["quote_reserved"] == Decimal("100")


def test_buy_full_fill_quote_fee_and_average_cost(tmp_path):
    engine = _engine(tmp_path, base="0", quote="1000")
    order = _submit(engine, price="100", quantity="1")
    result = _fill(engine, order, "fill-1", "100", "1")

    state = get_paper_account_state(engine.db_path)

    assert result.order.state is OrderState.FILLED
    assert state["base_free"] == Decimal("1")
    assert state["base_reserved"] == Decimal("0")
    assert state["quote_free"] == Decimal("899.9")
    assert state["quote_reserved"] == Decimal("0")
    assert state["average_cost"] == Decimal("100.1")
    assert state["total_fees"] == Decimal("0.1")


def test_buy_partial_fill_consumes_partial_reservation(tmp_path):
    engine = _engine(tmp_path, base="0", quote="1000")
    order = _submit(engine, price="100", quantity="2")
    result = _fill(engine, order, "fill-1", "100", "1")

    state = get_paper_account_state(engine.db_path)
    reservation = get_paper_reservation(engine.db_path, order.intent.client_order_id)

    assert result.order.state is OrderState.PARTIALLY_FILLED
    assert state["base_free"] == Decimal("1")
    assert state["quote_free"] == Decimal("799.9")
    assert state["quote_reserved"] == Decimal("100")
    assert reservation["remaining_amount"] == Decimal("100")


def test_sell_full_fill_realized_pnl_and_average_cost_unchanged(tmp_path):
    engine = _engine(tmp_path, base="0", quote="1000")
    buy = _submit(engine, index=0, price="100", quantity="1")
    _fill(engine, buy, "fill-buy", "100", "1")
    sell = _submit(engine, index=1, side="SELL", price="110", quantity="1")
    result = _fill(engine, sell, "fill-sell", "110", "1")

    state = get_paper_account_state(engine.db_path)

    assert result.order.state is OrderState.FILLED
    assert state["base_free"] == Decimal("0")
    assert state["quote_free"] == Decimal("1009.79")
    assert state["average_cost"] == Decimal("100.1")
    assert state["realized_pnl"] == Decimal("9.79")
    assert state["total_fees"] == Decimal("0.21")


def test_sell_partial_fill_and_multiple_sells(tmp_path):
    engine = _engine(tmp_path, base="0", quote="1000")
    buy = _submit(engine, index=0, price="100", quantity="2")
    _fill(engine, buy, "fill-buy", "100", "2")
    sell = _submit(
        engine,
        index=1,
        side="SELL",
        price="120",
        quantity="2",
        upper="130",
    )
    first = _fill(engine, sell, "fill-sell-1", "120", "1")
    second = _fill(engine, sell, "fill-sell-2", "130", "1")

    state = get_paper_account_state(engine.db_path)

    assert first.order.state is OrderState.PARTIALLY_FILLED
    assert second.order.state is OrderState.FILLED
    assert state["base_free"] == Decimal("0")
    assert state["average_cost"] == Decimal("100.1")
    assert state["realized_pnl"] == Decimal("49.55")


def test_multiple_buys_update_weighted_average_cost(tmp_path):
    engine = _engine(tmp_path, base="0", quote="1000")
    first = _submit(engine, index=0, price="100", quantity="1")
    second = _submit(engine, index=1, price="120", quantity="1", upper="130")
    _fill(engine, first, "fill-1", "100", "1")
    _fill(engine, second, "fill-2", "120", "1")

    state = get_paper_account_state(engine.db_path)

    assert state["base_free"] == Decimal("2")
    assert state["average_cost"] == Decimal("110.11")


def test_quote_and_base_fee_accounting(tmp_path):
    quote_fee_engine = _engine(tmp_path / "quote", base="0", quote="1000", fee_asset="USDT")
    quote_buy = _submit(quote_fee_engine, price="100", quantity="1")
    quote_result = _fill(quote_fee_engine, quote_buy, "quote-fill", "100", "1")

    base_fee_engine = _engine(tmp_path / "base", base="0", quote="1000", fee_asset="BNB")
    base_buy = _submit(base_fee_engine, price="100", quantity="1")
    base_result = _fill(base_fee_engine, base_buy, "base-fill", "100", "1")

    quote_state = get_paper_account_state(quote_fee_engine.db_path)
    base_state = get_paper_account_state(base_fee_engine.db_path)

    assert quote_result.order.state is OrderState.FILLED
    assert base_result.order.state is OrderState.FILLED
    assert quote_state["base_free"] == Decimal("1")
    assert base_state["base_free"] == Decimal("0.999")
    assert quote_state["quote_free"] == Decimal("899.9")
    assert base_state["quote_free"] == Decimal("900")
    assert base_state["total_fees"] == Decimal("0.1")
    assert base_state["average_cost"] == Decimal("100") / Decimal("0.999")


def test_taker_fee_uses_order_type_fee_rate(tmp_path):
    engine = _engine(tmp_path, base="0", quote="1000", maker="0", taker="0.002")
    order = _submit(engine, order_type="LIMIT")
    _fill(engine, order, "taker-fill", "100", "1")

    state = get_paper_account_state(engine.db_path)

    assert state["quote_free"] == Decimal("899.8")
    assert state["average_cost"] == Decimal("100.2")
    assert state["total_fees"] == Decimal("0.2")


def test_base_fee_sell_includes_fee_inventory_cost(tmp_path):
    engine = _engine(tmp_path, base="0", quote="1000")
    buy = _submit(engine, price="100", quantity="1.001")
    _fill(engine, buy, "fee-sell-buy", "100", "1.001")
    sell = _submit(engine, index=1, side="SELL", price="110", quantity="1")
    _fill(engine, sell, "fee-sell-sell", "110", "1", fee_asset="BNB")

    state = get_paper_account_state(engine.db_path)

    assert state["base_free"] == Decimal("0")
    assert state["base_reserved"] == Decimal("0")
    assert state["quote_free"] == Decimal("1009.7999")
    assert state["average_cost"] == Decimal("100.1")
    assert state["realized_pnl"] == Decimal("9.7999")
    assert state["total_fees"] == Decimal("0.2101")


def test_unsupported_fee_asset_fails_closed(tmp_path):
    with pytest.raises(UnsupportedFeeAsset, match="BTC"):
        _engine(tmp_path, fee_asset="BTC")


def test_equity_uses_free_and_reserved_balances(tmp_path):
    engine = _engine(tmp_path, base="2", quote="1000")
    state = get_paper_account_state(engine.db_path)
    state_obj = engine.accounting.initial_state()

    assert state_obj.equity(Decimal("50")) == Decimal("1100")
    assert state_obj.equity(Decimal("100")) == Decimal("1200")


def test_invalid_mark_price_is_rejected(tmp_path):
    engine = _engine(tmp_path)
    state = engine.accounting.initial_state()

    with pytest.raises(Exception, match="mark_price"):
        state.equity(Decimal("-1"))


def test_duplicate_fill_is_idempotent_for_accounting(tmp_path):
    engine = _engine(tmp_path, base="0", quote="1000")
    order = _submit(engine, price="100", quantity="1")
    first = _fill(engine, order, "fill-1", "100", "1")
    second = _fill(engine, order, "fill-1", "100", "1")

    state = get_paper_account_state(engine.db_path)

    assert first.applied is True
    assert second.applied is False
    assert second.idempotent is True
    assert state["base_free"] == Decimal("1")
    assert state["quote_free"] == Decimal("899.9")
    assert state["total_fees"] == Decimal("0.1")


def test_cancel_releases_buy_reservation(tmp_path):
    engine = _engine(tmp_path, base="0", quote="1000")
    order = _submit(engine, price="100", quantity="1")
    engine.transition(order.intent.client_order_id, OrderState.CANCELED)

    state = get_paper_account_state(engine.db_path)
    reservation = get_paper_reservation(engine.db_path, order.intent.client_order_id)

    assert state["quote_free"] == Decimal("1000")
    assert state["quote_reserved"] == Decimal("0")
    assert reservation["remaining_amount"] == Decimal("0")


def test_cancel_after_partial_fill_releases_remaining_reservation(tmp_path):
    engine = _engine(tmp_path, base="0", quote="1000")
    order = _submit(engine, price="100", quantity="2")
    _fill(engine, order, "fill-1", "100", "1")
    engine.transition(order.intent.client_order_id, OrderState.CANCELED)

    state = get_paper_account_state(engine.db_path)
    reservation = get_paper_reservation(engine.db_path, order.intent.client_order_id)

    assert state["quote_free"] == Decimal("899.9")
    assert state["quote_reserved"] == Decimal("0")
    assert reservation["remaining_amount"] == Decimal("0")


def test_accounting_state_survives_restart(tmp_path):
    engine = _engine(tmp_path, base="0", quote="1000")
    buy = _submit(engine, index=0, price="100", quantity="1")
    _fill(engine, buy, "fill-1", "100", "1")
    sell = _submit(engine, index=1, side="SELL", price="110", quantity="1")
    _fill(engine, sell, "fill-2", "110", "1")

    restarted = PaperOrderEngine(
        engine.db_path,
        clock=lambda: NOW,
        accounting=_accounting(base="0", quote="1000"),
    )
    state = get_paper_account_state(restarted.db_path)

    assert state["base_free"] == Decimal("0")
    assert state["quote_free"] == Decimal("1009.79")
    assert state["average_cost"] == Decimal("100.1")
    assert state["realized_pnl"] == Decimal("9.79")
    assert state["total_fees"] == Decimal("0.21")


def test_fill_accounting_is_atomic_and_retryable(tmp_path):
    engine = _engine(tmp_path, base="0", quote="1000")
    order = _submit(engine, price="100", quantity="1")

    with sqlite3.connect(engine.db_path) as connection:
        connection.execute("DROP TABLE paper_accounting_events")

    with pytest.raises(sqlite3.OperationalError):
        _fill(engine, order, "fill-1", "100", "1")

    state = get_paper_account_state(engine.db_path)
    assert state["base_free"] == Decimal("0")
    assert state["quote_free"] == Decimal("900")
    assert state["quote_reserved"] == Decimal("100")
    assert get_paper_reservation(engine.db_path, order.intent.client_order_id)["remaining_amount"] == Decimal("100")

    init_db(engine.db_path)
    result = _fill(engine, order, "fill-1", "100", "1")

    assert result.applied is True
    assert result.order.state is OrderState.FILLED
    assert get_paper_account_state(engine.db_path)["quote_free"] == Decimal("899.9")


def test_stale_accounting_state_fails_closed(tmp_path):
    engine = _engine(tmp_path, base="0", quote="1000")
    first = _submit(engine)

    second_intent = _intent(index=1)
    planned = PaperOrder(second_intent, OrderState.PLANNED, NOW)
    submitted = transition_order(planned, OrderState.SUBMITTED, NOW)
    opened = transition_order(submitted, OrderState.OPEN, NOW)
    stale_update = engine.accounting.prepare_reservation(
        engine.accounting.initial_state(),
        opened,
        NOW,
    )

    with pytest.raises(OrderSubmissionError) as excinfo:
        save_order_submission(
            engine.db_path,
            planned,
            submitted,
            opened,
            stale_update,
        )

    assert isinstance(excinfo.value.__cause__, PaperAccountingStaleState)

    assert get_order(engine.db_path, second_intent.client_order_id) is None
    assert get_order(engine.db_path, first.intent.client_order_id) is not None


def test_legacy_paper_activity_without_accounting_fails_closed(tmp_path):
    path = str(tmp_path / "legacy.sqlite3")
    init_db(path)
    save_order_submission(
        path,
        PaperOrder(_intent(), OrderState.PLANNED, NOW),
        PaperOrder(_intent(), OrderState.SUBMITTED, NOW),
        PaperOrder(_intent(), OrderState.OPEN, NOW),
    )

    with pytest.raises(PaperAccountingMigrationError, match="accounting state"):
        PaperOrderEngine(path, accounting=_accounting())
