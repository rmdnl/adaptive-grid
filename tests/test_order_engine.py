from datetime import datetime, timezone
from decimal import Decimal
import re
import sqlite3

import pytest

from order_engine import (
    DuplicateOrder,
    InvalidOrderStateTransition,
    OrderIntent,
    OrderIdentityMismatch,
    OrderIntentValidationError,
    OrderPriceOutOfRange,
    OrderState,
    PaperOrder,
    PaperOrderEngine,
    RiskVeto,
    make_client_order_id,
    transition_order,
)
from risk_engine import RiskDecision, equity_dd_kill, open_orders_gate, profit_gate, strict_order_price_gate
from storage import OrderPersistenceError, get_order, init_db, save_order


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _intent(index=12, side="BUY", **changes):
    values = {
        "client_order_id": make_client_order_id("AG", "BTCUSDT", index, side),
        "symbol": "BTCUSDT", "side": side, "order_type": "LIMIT",
        "price": Decimal("100"), "quantity": Decimal("0.25"),
        "time_in_force": "GTC", "grid_index": index, "created_at": NOW,
    }
    values.update(changes)
    return OrderIntent(**values)


def _engine(tmp_path):
    return PaperOrderEngine(str(tmp_path / "grid.sqlite3"), clock=lambda: NOW)


def test_valid_immutable_order_intent():
    intent = _intent()
    assert intent.price == Decimal("100")
    with pytest.raises(Exception):
        intent.price = Decimal("101")


@pytest.mark.parametrize(("field", "value"), [
    ("side", "HOLD"), ("price", Decimal("0")), ("quantity", Decimal("-1")),
])
def test_invalid_order_intent_is_rejected(field, value):
    with pytest.raises(OrderIntentValidationError):
        _intent(**{field: value})


@pytest.mark.parametrize("terminal", [OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED])
@pytest.mark.parametrize("target", list(OrderState))
def test_every_terminal_state_transition_is_rejected(terminal, target):
    with pytest.raises(InvalidOrderStateTransition):
        transition_order(PaperOrder(_intent(), terminal, NOW), target, NOW)


def test_client_order_id_is_deterministic_safe_and_distinguishes_grid_and_side():
    first = make_client_order_id("AG", "BTCUSDT", 12, "BUY")
    assert first == make_client_order_id("AG", "BTCUSDT", 12, "BUY")
    assert first != make_client_order_id("AG", "BTCUSDT", 13, "BUY")
    assert first != make_client_order_id("AG", "BTCUSDT", 12, "SELL")
    assert re.fullmatch(r"[A-Za-z0-9_-]+", first)
    assert len(first) <= 36


@pytest.mark.parametrize("change", [
    {"symbol": "ETHUSDT"},
    {"grid_index": 13},
    {"side": "SELL"},
])
def test_submission_rejects_client_id_that_claims_wrong_grid_identity(tmp_path, change):
    engine = _engine(tmp_path)

    original = _intent()

    values = {
        "client_order_id": original.client_order_id,
        "symbol": original.symbol,
        "side": original.side,
        "order_type": original.order_type,
        "price": original.price,
        "quantity": original.quantity,
        "time_in_force": original.time_in_force,
        "grid_index": original.grid_index,
        "created_at": original.created_at,
    }
    values.update(change)

    mismatched = OrderIntent(**values)

    with pytest.raises(OrderIdentityMismatch):
        engine.submit(
            mismatched,
            RiskDecision(True),
            Decimal("90"),
            Decimal("110"),
        )

    assert engine.get(original.client_order_id) is None


def test_submission_uses_configured_client_order_prefix(tmp_path):
    engine = PaperOrderEngine(
        str(tmp_path / "grid.sqlite3"), clock=lambda: NOW, client_order_prefix="PAPER"
    )
    intent = _intent(client_order_id=make_client_order_id("PAPER", "BTCUSDT", 12, "BUY"))
    assert engine.submit(intent, RiskDecision(True), Decimal("90"), Decimal("110")).state is OrderState.OPEN


@pytest.mark.parametrize(("order_type", "time_in_force"), [
    ("LIMIT", "IOC"), ("LIMIT_MAKER", "FOK"), ("LIMIT", "gtc"),
])
def test_unsupported_time_in_force_is_rejected(order_type, time_in_force):
    with pytest.raises(OrderIntentValidationError, match="require LIMIT/LIMIT_MAKER with GTC"):
        _intent(order_type=order_type, time_in_force=time_in_force)


@pytest.mark.parametrize(("source", "target"), [
    (OrderState.PLANNED, OrderState.SUBMITTED),
    (OrderState.SUBMITTED, OrderState.OPEN),
    (OrderState.SUBMITTED, OrderState.REJECTED),
    (OrderState.OPEN, OrderState.PARTIALLY_FILLED),
    (OrderState.OPEN, OrderState.FILLED),
    (OrderState.OPEN, OrderState.CANCELED),
    (OrderState.PARTIALLY_FILLED, OrderState.FILLED),
    (OrderState.PARTIALLY_FILLED, OrderState.CANCELED),
])
def test_every_allowed_state_transition(source, target):
    order = PaperOrder(_intent(), source, NOW)
    assert transition_order(order, target, NOW).state == target


@pytest.mark.parametrize(("source", "target"), [
    (OrderState.FILLED, OrderState.OPEN), (OrderState.FILLED, OrderState.CANCELED),
    (OrderState.CANCELED, OrderState.OPEN), (OrderState.REJECTED, OrderState.OPEN),
    (OrderState.PLANNED, OrderState.FILLED), (OrderState.OPEN, OrderState.SUBMITTED),
])
def test_invalid_state_transitions_fail_closed(source, target):
    with pytest.raises(InvalidOrderStateTransition):
        transition_order(PaperOrder(_intent(), source, NOW), target, NOW)


@pytest.mark.parametrize("state", [
    OrderState.PLANNED, OrderState.SUBMITTED, OrderState.OPEN, OrderState.PARTIALLY_FILLED,
])
def test_duplicate_active_order_identity_is_rejected(tmp_path, state):
    engine = _engine(tmp_path)
    existing = PaperOrder(_intent(), state, NOW)
    save_order(engine.db_path, existing)

    with pytest.raises(DuplicateOrder, match=state.value):
        engine.submit(_intent(), RiskDecision(True), Decimal("90"), Decimal("110"))


def test_different_grid_cell_is_not_a_duplicate(tmp_path):
    engine = _engine(tmp_path)
    engine.submit(_intent(12), RiskDecision(True), Decimal("90"), Decimal("110"))

    order = engine.submit(_intent(13), RiskDecision(True), Decimal("90"), Decimal("110"))
    assert order.state is OrderState.OPEN


def test_paper_submission_transitions_to_open_without_binance_client(tmp_path):
    engine = _engine(tmp_path)
    order = engine.submit(_intent(), RiskDecision(True), Decimal("90"), Decimal("110"))

    assert order.state is OrderState.OPEN
    assert order.state is not OrderState.FILLED
    assert engine.get(order.intent.client_order_id) == order


@pytest.mark.parametrize("risk", [
    profit_gate(Decimal("0.002"), Decimal("0.003")),
    equity_dd_kill(Decimal("0.02"), Decimal("0.02")),
    strict_order_price_gate(Decimal("90"), Decimal("110"), Decimal("111")),
    open_orders_gate(40, 40),
])
def test_risk_veto_preserves_reason_and_does_not_persist(tmp_path, risk):
    engine = _engine(tmp_path)
    intent = _intent()
    with pytest.raises(RiskVeto, match=re.escape(risk.reason)):
        engine.submit(intent, risk, Decimal("90"), Decimal("110"))
    assert engine.get(intent.client_order_id) is None


@pytest.mark.parametrize("price", [Decimal("89.99"), Decimal("110.01")])
def test_effective_range_rejects_outside_paper_order(tmp_path, price):
    engine = _engine(tmp_path)
    with pytest.raises(OrderPriceOutOfRange):
        engine.submit(_intent(price=price), RiskDecision(True), Decimal("90"), Decimal("110"))


def test_effective_range_includes_both_boundaries(tmp_path):
    engine = _engine(tmp_path)
    assert engine.submit(_intent(price=Decimal("90")), RiskDecision(True), Decimal("90"), Decimal("110")).state is OrderState.OPEN
    assert engine.submit(_intent(13, price=Decimal("110")), RiskDecision(True), Decimal("90"), Decimal("110")).state is OrderState.OPEN


def test_persisted_order_and_transitions_survive_engine_restart(tmp_path):
    engine = _engine(tmp_path)
    submitted = engine.submit(_intent(), RiskDecision(True), Decimal("90"), Decimal("110"))
    partial = engine.transition(submitted.intent.client_order_id, OrderState.PARTIALLY_FILLED)

    recovered = _engine(tmp_path).get(submitted.intent.client_order_id)
    assert recovered == partial
    assert get_order(engine.db_path, submitted.intent.client_order_id)["status"] == "PARTIALLY_FILLED"


def test_existing_order_immutable_identity_cannot_be_overwritten(tmp_path):
    engine = _engine(tmp_path)
    original = _intent()
    save_order(engine.db_path, PaperOrder(original, OrderState.OPEN, NOW))
    changed = _intent(price=Decimal("101"))

    with pytest.raises(OrderPersistenceError, match="Immutable order field mismatch.*price"):
        save_order(engine.db_path, PaperOrder(changed, OrderState.OPEN, NOW))
    assert get_order(engine.db_path, original.client_order_id)["price"] == "100"


def test_valid_existing_state_transition_updates_only_status_and_updated_at(tmp_path):
    moments = iter([
        datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 21, 12, 0, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 21, 12, 0, 2, tzinfo=timezone.utc),
        datetime(2026, 9, 21, 12, 0, 3, tzinfo=timezone.utc),
    ])
    engine = PaperOrderEngine(str(tmp_path / "grid.sqlite3"), clock=lambda: next(moments))
    submitted = engine.submit(_intent(), RiskDecision(True), Decimal("90"), Decimal("110"))
    transitioned = engine.transition(submitted.intent.client_order_id, OrderState.PARTIALLY_FILLED)
    row = get_order(engine.db_path, submitted.intent.client_order_id)

    assert transitioned.state is OrderState.PARTIALLY_FILLED
    assert row["status"] == "PARTIALLY_FILLED"
    assert row["updated_at"] == "2026-09-21T12:00:03+00:00"
    assert row["price"] == "100"
    assert row["quantity"] == "0.25"


def test_existing_pre_phase_3_database_rows_remain_readable(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE orders (client_order_id TEXT PRIMARY KEY, exchange_order_id TEXT, "
        "symbol TEXT NOT NULL, side TEXT NOT NULL, grid_index INTEGER NOT NULL, price TEXT NOT NULL, "
        "quantity TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    con.execute(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("AG-BTCUSDT-00012-B", None, "BTCUSDT", "BUY", 12, "100", "0.25", "OPEN", NOW.isoformat(), NOW.isoformat()),
    )
    con.commit()
    con.close()

    init_db(str(db))
    recovered = PaperOrderEngine(str(db), clock=lambda: NOW).get("AG-BTCUSDT-00012-B")
    assert recovered is not None
    assert recovered.intent.order_type == "LIMIT"
    assert recovered.intent.time_in_force == "GTC"
    assert recovered.state is OrderState.OPEN
