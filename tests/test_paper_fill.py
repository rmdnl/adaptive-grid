from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3

import pytest

from order_engine import (
    OrderIntent,
    OrderState,
    PaperFillStateError,
    PaperFillSymbolMismatch,
    PaperFillValidationError,
    PaperOrder,
    PaperOrderEngine,
    make_client_order_id,
)
from risk_engine import RiskDecision
from storage import FillIdentityMismatch, get_fill, init_db, save_order


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
FILL_TIME = NOW + timedelta(minutes=1)


def _intent(index=0, side="BUY", price="100", quantity="2"):
    return OrderIntent(
        client_order_id=make_client_order_id("AG", "BNBUSDT", index, side),
        symbol="BNBUSDT",
        side=side,
        order_type="LIMIT_MAKER",
        price=Decimal(price),
        quantity=Decimal(quantity),
        time_in_force="GTC",
        grid_index=index,
        created_at=NOW,
    )


def _engine(tmp_path, side="BUY", price="100", quantity="2", index=0):
    engine = PaperOrderEngine(str(tmp_path / "paper.sqlite3"), clock=lambda: NOW)
    engine.submit(
        _intent(index=index, side=side, price=price, quantity=quantity),
        RiskDecision(True),
        Decimal("90"),
        Decimal("110"),
    )
    return engine


def _fill_id(index):
    return f"fill-{index:05d}"


def _apply(engine, side="BUY", price="100", quantity="2", market_price="100", fill_quantity="1", index=0):
    return engine.apply_fill(
        make_client_order_id("AG", "BNBUSDT", index, side),
        _fill_id(index),
        "BNBUSDT",
        Decimal(market_price),
        Decimal(fill_quantity),
        FILL_TIME,
    )


def test_buy_full_fill(tmp_path):
    engine = _engine(tmp_path, side="BUY", quantity="2")

    result = _apply(engine, side="BUY", quantity="2", fill_quantity="2", market_price="99")

    assert result.applied is True
    assert result.order.state is OrderState.FILLED
    assert result.order.executed_qty == Decimal("2")
    assert result.order.remaining_qty == Decimal("0")


def test_sell_full_fill(tmp_path):
    engine = _engine(tmp_path, side="SELL", quantity="2")

    result = _apply(engine, side="SELL", quantity="2", fill_quantity="2", market_price="101")

    assert result.applied is True
    assert result.order.state is OrderState.FILLED
    assert result.order.executed_qty == Decimal("2")
    assert result.order.remaining_qty == Decimal("0")


def test_buy_partial_fill(tmp_path):
    engine = _engine(tmp_path, side="BUY", quantity="2")

    result = _apply(engine, side="BUY", quantity="2", fill_quantity="1", market_price="99")

    assert result.applied is True
    assert result.order.state is OrderState.PARTIALLY_FILLED
    assert result.order.executed_qty == Decimal("1")
    assert result.order.remaining_qty == Decimal("1")


def test_sell_partial_fill(tmp_path):
    engine = _engine(tmp_path, side="SELL", quantity="2")

    result = _apply(engine, side="SELL", quantity="2", fill_quantity="1", market_price="101")

    assert result.applied is True
    assert result.order.state is OrderState.PARTIALLY_FILLED
    assert result.order.executed_qty == Decimal("1")
    assert result.order.remaining_qty == Decimal("1")


def test_exact_price_buy_fill(tmp_path):
    engine = _engine(tmp_path, side="BUY", quantity="2")

    result = _apply(engine, side="BUY", quantity="2", fill_quantity="2", market_price="100")

    assert result.applied is True
    assert result.order.state is OrderState.FILLED


def test_exact_price_sell_fill(tmp_path):
    engine = _engine(tmp_path, side="SELL", quantity="2")

    result = _apply(engine, side="SELL", quantity="2", fill_quantity="2", market_price="100")

    assert result.applied is True
    assert result.order.state is OrderState.FILLED


def test_buy_non_fill(tmp_path):
    engine = _engine(tmp_path, side="BUY", quantity="2")

    result = _apply(engine, side="BUY", quantity="2", fill_quantity="1", market_price="100.01")

    assert result.applied is False
    assert result.fill is None
    assert result.order.state is OrderState.OPEN
    assert result.order.executed_qty == Decimal("0")
    assert get_fill(engine.db_path, _fill_id(0)) is None


def test_sell_non_fill(tmp_path):
    engine = _engine(tmp_path, side="SELL", quantity="2")

    result = _apply(engine, side="SELL", quantity="2", fill_quantity="1", market_price="99.99")

    assert result.applied is False
    assert result.fill is None
    assert result.order.state is OrderState.OPEN
    assert result.order.executed_qty == Decimal("0")
    assert get_fill(engine.db_path, _fill_id(0)) is None


def test_partial_to_partial(tmp_path):
    engine = _engine(tmp_path, side="BUY", quantity="3")

    first = _apply(engine, side="BUY", quantity="3", fill_quantity="1", market_price="99")
    second = engine.apply_fill(
        first.order.intent.client_order_id,
        _fill_id(1),
        "BNBUSDT",
        Decimal("99"),
        Decimal("1"),
        FILL_TIME,
    )

    assert second.applied is True
    assert second.order.state is OrderState.PARTIALLY_FILLED
    assert second.order.executed_qty == Decimal("2")
    assert second.order.remaining_qty == Decimal("1")


def test_partial_to_full(tmp_path):
    engine = _engine(tmp_path, side="BUY", quantity="3")

    first = _apply(engine, side="BUY", quantity="3", fill_quantity="1", market_price="99")
    second = engine.apply_fill(
        first.order.intent.client_order_id,
        _fill_id(1),
        "BNBUSDT",
        Decimal("99"),
        Decimal("2"),
        FILL_TIME,
    )

    assert second.applied is True
    assert second.order.state is OrderState.FILLED
    assert second.order.executed_qty == Decimal("3")
    assert second.order.remaining_qty == Decimal("0")


def test_overfill_is_rejected(tmp_path):
    engine = _engine(tmp_path, side="BUY", quantity="2")

    with pytest.raises(PaperFillValidationError, match="exceeds remaining"):
        _apply(engine, side="BUY", quantity="2", fill_quantity="2.001", market_price="99")


@pytest.mark.parametrize("quantity", [Decimal("0"), Decimal("-1")])
def test_zero_or_negative_fill_is_rejected(tmp_path, quantity):
    engine = _engine(tmp_path, side="BUY", quantity="2")

    with pytest.raises(PaperFillValidationError, match="fill_quantity"):
        engine.apply_fill(
            make_client_order_id("AG", "BNBUSDT", 0, "BUY"),
            _fill_id(0),
            "BNBUSDT",
            Decimal("99"),
            quantity,
            FILL_TIME,
        )


@pytest.mark.parametrize("market_price", ["not-a-number", None, Decimal("NaN")])
def test_invalid_market_price_is_rejected(tmp_path, market_price):
    engine = _engine(tmp_path, side="BUY", quantity="2")

    with pytest.raises(PaperFillValidationError, match="market_price"):
        engine.apply_fill(
            make_client_order_id("AG", "BNBUSDT", 0, "BUY"),
            _fill_id(0),
            "BNBUSDT",
            market_price,
            Decimal("1"),
            FILL_TIME,
        )


@pytest.mark.parametrize("state", [
    OrderState.PLANNED,
    OrderState.SUBMITTED,
    OrderState.FILLED,
    OrderState.CANCELED,
    OrderState.REJECTED,
])
def test_non_fillable_states_are_rejected(tmp_path, state):
    engine = PaperOrderEngine(str(tmp_path / "paper.sqlite3"), clock=lambda: NOW)
    order = PaperOrder(_intent(), state, NOW)
    save_order(engine.db_path, order)

    with pytest.raises(PaperFillStateError, match=state.value):
        engine.apply_fill(
            order.intent.client_order_id,
            _fill_id(0),
            "BNBUSDT",
            Decimal("99"),
            Decimal("1"),
            FILL_TIME,
        )


def test_wrong_symbol_is_rejected(tmp_path):
    engine = _engine(tmp_path, side="BUY", quantity="2")

    with pytest.raises(PaperFillSymbolMismatch, match="ETHUSDT"):
        engine.apply_fill(
            make_client_order_id("AG", "BNBUSDT", 0, "BUY"),
            _fill_id(0),
            "ETHUSDT",
            Decimal("99"),
            Decimal("1"),
            FILL_TIME,
        )


def test_duplicate_fill_event_is_idempotent(tmp_path):
    engine = _engine(tmp_path, side="BUY", quantity="2")
    client_order_id = make_client_order_id("AG", "BNBUSDT", 0, "BUY")

    first = engine.apply_fill(
        client_order_id, _fill_id(0), "BNBUSDT", Decimal("99"), Decimal("1"), FILL_TIME
    )
    second = engine.apply_fill(
        client_order_id, _fill_id(0), "BNBUSDT", Decimal("99"), Decimal("1"), FILL_TIME
    )

    assert first.applied is True
    assert second.applied is False
    assert second.idempotent is True
    assert second.order.executed_qty == Decimal("1")
    assert second.order.remaining_qty == Decimal("1")
    assert get_fill(engine.db_path, _fill_id(0)) is not None


def test_fill_identity_mismatch_is_rejected(tmp_path):
    engine = _engine(tmp_path, side="BUY", quantity="2")
    client_order_id = make_client_order_id("AG", "BNBUSDT", 0, "BUY")

    engine.apply_fill(
        client_order_id, _fill_id(0), "BNBUSDT", Decimal("99"), Decimal("1"), FILL_TIME
    )

    with pytest.raises(FillIdentityMismatch):
        engine.apply_fill(
            client_order_id, _fill_id(0), "BNBUSDT", Decimal("99"), Decimal("0.5"), FILL_TIME
        )


def test_fill_state_survives_engine_restart(tmp_path):
    engine = _engine(tmp_path, side="BUY", quantity="2")
    client_order_id = make_client_order_id("AG", "BNBUSDT", 0, "BUY")
    engine.apply_fill(
        client_order_id, _fill_id(0), "BNBUSDT", Decimal("99"), Decimal("1"), FILL_TIME
    )

    recovered = PaperOrderEngine(engine.db_path, clock=lambda: NOW).get(client_order_id)

    assert recovered.state is OrderState.PARTIALLY_FILLED
    assert recovered.executed_qty == Decimal("1")
    assert recovered.remaining_qty == Decimal("1")


def test_fill_update_and_event_are_atomic_and_retryable(tmp_path):
    engine = _engine(tmp_path, side="BUY", quantity="2")
    client_order_id = make_client_order_id("AG", "BNBUSDT", 0, "BUY")

    with sqlite3.connect(engine.db_path) as connection:
        connection.execute("DROP TABLE fills")

    with pytest.raises(sqlite3.OperationalError):
        engine.apply_fill(
            client_order_id, _fill_id(0), "BNBUSDT", Decimal("99"), Decimal("1"), FILL_TIME
        )

    unchanged = engine.get(client_order_id)
    assert unchanged.state is OrderState.OPEN
    assert unchanged.executed_qty == Decimal("0")
    assert unchanged.remaining_qty == Decimal("2")

    init_db(engine.db_path)
    retried = engine.apply_fill(
        client_order_id, _fill_id(0), "BNBUSDT", Decimal("99"), Decimal("1"), FILL_TIME
    )

    assert retried.applied is True
    assert retried.order.state is OrderState.PARTIALLY_FILLED
    assert get_fill(engine.db_path, _fill_id(0)) is not None
