"""Tests for the read-only paper-state recovery and reconciliation layer.

These tests exercise ``recover_paper_state`` against hand-crafted SQLite
databases so we can verify each invariant in isolation.  Recovery is purely
read-only: it must never mutate the database.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from order_engine import (
    OrderIntent,
    OrderState,
    PaperOrder,
    PaperOrderEngine,
    PaperStateUnhealthyError,
    make_client_order_id,
)
from paper_accounting import PaperAccountingEngine
from recovery import (
    RecoveryErrorCode,
    RecoveryUnhealthyError,
    recover_paper_state,
)
from risk_engine import RiskDecision
from storage import connect, init_db


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _accounting():
    return PaperAccountingEngine(
        "BNB",
        "USDT",
        Decimal("2"),
        Decimal("1000"),
        Decimal("0.001"),
        Decimal("0.001"),
        "USDT",
    )


def _intent(index=0, side="BUY", price="100", quantity="1", generation=0):
    return OrderIntent(
        client_order_id=make_client_order_id("AG", "BNBUSDT", generation, index, side),
        symbol="BNBUSDT",
        side=side,
        order_type="LIMIT_MAKER",
        price=Decimal(price),
        quantity=Decimal(quantity),
        time_in_force="GTC",
        grid_index=index,
        generation=generation,
        created_at=NOW,
    )


def _empty_db(tmp_path):
    path = str(tmp_path / "recovery.sqlite3")
    init_db(path)
    return path


def _engine(tmp_path):
    path = str(tmp_path / "engine.sqlite3")
    return PaperOrderEngine(
        path, clock=lambda: NOW, accounting=_accounting()
    )


def _submit(engine, index=0, side="BUY", price="100", quantity="1"):
    intent = _intent(index=index, side=side, price=price, quantity=quantity)
    return engine.submit(
        intent, RiskDecision(True), Decimal("90"), Decimal("110")
    ), intent


def _seed_account_state(path, state=None):
    """Insert or overwrite the singleton paper_account_state row."""
    if state is None:
        state = _accounting().initial_state()
    con = connect(path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO paper_account_state("
            "id, base_asset, quote_asset, base_free, base_reserved, "
            "quote_free, quote_reserved, average_cost, realized_pnl, "
            "total_fees, updated_at"
            ") VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                state.base_asset,
                state.quote_asset,
                str(state.base_free),
                str(state.base_reserved),
                str(state.quote_free),
                str(state.quote_reserved),
                str(state.average_cost),
                str(state.realized_pnl),
                str(state.total_fees),
                state.updated_at.isoformat(),
            ),
        )
        con.commit()
    finally:
        con.close()


def _raw_insert(path, table, columns, values):
    con = connect(path)
    try:
        placeholders = ",".join("?" * len(values))
        con.execute(
            f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders})",
            values,
        )
        con.commit()
    finally:
        con.close()


def _execute(path, sql, params=()):
    con = connect(path)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Healthy state
# ---------------------------------------------------------------------------


def test_empty_database_without_account_state_is_unhealthy(tmp_path):
    path = _empty_db(tmp_path)

    result = recover_paper_state(path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.MISSING_ACCOUNT_STATE
        for e in result.errors
    )


def test_fresh_account_state_no_orders_is_healthy(tmp_path):
    path = _empty_db(tmp_path)
    _seed_account_state(path)

    result = recover_paper_state(path)

    assert result.healthy is True
    assert result.errors == ()
    assert result.recovered_orders == 0
    assert result.recovered_reservations == 0
    assert result.recovered_fills == 0
    assert result.account_state_valid is True


def test_single_open_buy_order_with_reservation_is_healthy(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    result = recover_paper_state(engine.db_path)

    assert result.healthy is True, [str(e) for e in result.errors]
    assert result.recovered_orders == 1
    assert result.recovered_reservations == 1


def test_single_open_sell_order_with_reservation_is_healthy(tmp_path):
    engine = _engine(tmp_path)
    # Seed a base balance so SELL has inventory to reserve.
    _submit(engine, side="SELL", price="110", quantity="1")

    result = recover_paper_state(engine.db_path)

    assert result.healthy is True, [str(e) for e in result.errors]
    assert result.recovered_orders == 1


# ---------------------------------------------------------------------------
# Reservation invariants
# ---------------------------------------------------------------------------


def test_orphan_reservation_is_reported(tmp_path):
    path = _empty_db(tmp_path)
    _seed_account_state(path)
    # Insert a reservation for an order that does not exist.  Adjust the
    # account state so quote_reserved matches the orphan reservation sum.
    _raw_insert(
        path,
        "paper_reservations",
        [
            "client_order_id", "side", "asset",
            "original_amount", "remaining_amount",
            "created_at", "updated_at",
        ],
        [
            "AG-BNBUSDT-00000-B", "BUY", "USDT",
            "100", "100", NOW.isoformat(), NOW.isoformat(),
        ],
    )
    state = _accounting().initial_state()
    new_state = replace(
        state,
        quote_free=state.quote_free - Decimal("100"),
        quote_reserved=Decimal("100"),
    )
    _seed_account_state(path, new_state)

    result = recover_paper_state(path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.ORPHAN_RESERVATION for e in result.errors
    )


def test_reservation_side_mismatch_is_reported(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _execute(
        engine.db_path,
        "UPDATE paper_reservations SET side='SELL' WHERE client_order_id=?",
        (order.intent.client_order_id,),
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.RESERVATION_SIDE_MISMATCH
        for e in result.errors
    )


def test_reservation_remaining_exceeds_original_is_reported(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _execute(
        engine.db_path,
        "UPDATE paper_reservations SET remaining_amount='999' "
        "WHERE client_order_id=?",
        (order.intent.client_order_id,),
    )
    # Make the account state match so only the reservation-level invariant fires.
    _execute(
        engine.db_path,
        "UPDATE paper_account_state SET quote_reserved='999'"
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.RESERVATION_EXCEEDS_ORDER
        for e in result.errors
    )


def test_terminal_order_nonzero_reservation_is_reported(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _execute(
        engine.db_path,
        "UPDATE orders SET status='FILLED' WHERE client_order_id=?",
        (order.intent.client_order_id,),
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.RESERVATION_TERMINAL_NONZERO
        for e in result.errors
    )


def test_reservation_for_cancelled_is_reported(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _execute(
        engine.db_path,
        "UPDATE orders SET status='CANCELED' WHERE client_order_id=?",
        (order.intent.client_order_id,),
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.RESERVATION_FOR_CANCELLED
        for e in result.errors
    )


def test_reservation_for_rejected_is_reported(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _execute(
        engine.db_path,
        "UPDATE orders SET status='REJECTED' WHERE client_order_id=?",
        (order.intent.client_order_id,),
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.RESERVATION_FOR_REJECTED
        for e in result.errors
    )


def test_open_order_zero_reservation_is_reported(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _execute(
        engine.db_path,
        "UPDATE paper_reservations SET remaining_amount='0' "
        "WHERE client_order_id=?",
        (order.intent.client_order_id,),
    )
    _execute(
        engine.db_path,
        "UPDATE paper_account_state SET quote_reserved='0'"
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.RESERVATION_OPEN_ZERO
        for e in result.errors
    )


def test_reservation_asset_empty_is_reported(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _execute(
        engine.db_path,
        "UPDATE paper_reservations SET asset='' WHERE client_order_id=?",
        (order.intent.client_order_id,),
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.RESERVATION_SYMBOL_MISMATCH
        for e in result.errors
    )


# ---------------------------------------------------------------------------
# Fill invariants
# ---------------------------------------------------------------------------


def test_orphan_fill_is_reported(tmp_path):
    path = _empty_db(tmp_path)
    _seed_account_state(path)
    _raw_insert(
        path,
        "fills",
        [
            "trade_id", "order_id", "symbol", "side",
            "price", "quantity", "fee", "fee_asset",
            "event_time", "resulting_state",
            "executed_qty", "remaining_qty",
        ],
        [
            "fill-1", "AG-BNBUSDT-00000-B", "BNBUSDT", "BUY",
            "100", "1", "0.1", "USDT",
            NOW.isoformat(), "FILLED",
            "1", "0",
        ],
    )

    result = recover_paper_state(path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.ORPHAN_FILL for e in result.errors
    )


def test_fill_symbol_mismatch_is_reported(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _raw_insert(
        engine.db_path,
        "fills",
        [
            "trade_id", "order_id", "symbol", "side",
            "price", "quantity", "fee", "fee_asset",
            "event_time", "resulting_state",
            "executed_qty", "remaining_qty",
        ],
        [
            "fill-1", order.intent.client_order_id, "ETHUSDT", "BUY",
            "100", "1", "0.1", "USDT",
            NOW.isoformat(), "FILLED",
            "1", "0",
        ],
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.FILL_SYMBOL_MISMATCH for e in result.errors
    )


def test_fill_side_mismatch_is_reported(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _raw_insert(
        engine.db_path,
        "fills",
        [
            "trade_id", "order_id", "symbol", "side",
            "price", "quantity", "fee", "fee_asset",
            "event_time", "resulting_state",
            "executed_qty", "remaining_qty",
        ],
        [
            "fill-1", order.intent.client_order_id, "BNBUSDT", "SELL",
            "100", "1", "0.1", "USDT",
            NOW.isoformat(), "FILLED",
            "1", "0",
        ],
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.FILL_SIDE_MISMATCH for e in result.errors
    )


def test_fill_exceeds_quantity_is_reported(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _raw_insert(
        engine.db_path,
        "fills",
        [
            "trade_id", "order_id", "symbol", "side",
            "price", "quantity", "fee", "fee_asset",
            "event_time", "resulting_state",
            "executed_qty", "remaining_qty",
        ],
        [
            "fill-1", order.intent.client_order_id, "BNBUSDT", "BUY",
            "100", "1.5", "0.15", "USDT",
            NOW.isoformat(), "FILLED",
            "1.5", "0",
        ],
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.FILL_EXCEEDS_QUANTITY
        for e in result.errors
    )


def test_fill_cumulative_mismatch_is_reported(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _raw_insert(
        engine.db_path,
        "fills",
        [
            "trade_id", "order_id", "symbol", "side",
            "price", "quantity", "fee", "fee_asset",
            "event_time", "resulting_state",
            "executed_qty", "remaining_qty",
        ],
        [
            "fill-1", order.intent.client_order_id, "BNBUSDT", "BUY",
            "100", "1", "0.1", "USDT",
            NOW.isoformat(), "FILLED",
            "1", "0",
        ],
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.FILL_CUMULATIVE_MISMATCH
        for e in result.errors
    )


def test_fill_state_impossible_is_reported(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _raw_insert(
        engine.db_path,
        "fills",
        [
            "trade_id", "order_id", "symbol", "side",
            "price", "quantity", "fee", "fee_asset",
            "event_time", "resulting_state",
            "executed_qty", "remaining_qty",
        ],
        [
            "fill-1", order.intent.client_order_id, "BNBUSDT", "BUY",
            "100", "1", "0.1", "USDT",
            NOW.isoformat(), "OPEN",
            "1", "0",
        ],
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.FILL_STATE_IMPOSSIBLE
        for e in result.errors
    )


def test_fill_for_cancelled_order_is_reported(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _execute(
        engine.db_path,
        "UPDATE orders SET status='CANCELED' WHERE client_order_id=?",
        (order.intent.client_order_id,),
    )
    _execute(
        engine.db_path,
        "UPDATE paper_reservations SET remaining_amount='0' "
        "WHERE client_order_id=?",
        (order.intent.client_order_id,),
    )
    _execute(
        engine.db_path,
        "UPDATE paper_account_state SET quote_reserved='0'"
    )

    _raw_insert(
        engine.db_path,
        "fills",
        [
            "trade_id", "order_id", "symbol", "side",
            "price", "quantity", "fee", "fee_asset",
            "event_time", "resulting_state",
            "executed_qty", "remaining_qty",
        ],
        [
            "fill-1", order.intent.client_order_id, "BNBUSDT", "BUY",
            "100", "1", "0.1", "USDT",
            NOW.isoformat(), "FILLED",
            "1", "0",
        ],
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.FILL_FOR_CANCELLED for e in result.errors
    )


def test_fill_for_rejected_order_is_reported(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _execute(
        engine.db_path,
        "UPDATE orders SET status='REJECTED' WHERE client_order_id=?",
        (order.intent.client_order_id,),
    )
    _execute(
        engine.db_path,
        "UPDATE paper_reservations SET remaining_amount='0' "
        "WHERE client_order_id=?",
        (order.intent.client_order_id,),
    )
    _execute(
        engine.db_path,
        "UPDATE paper_account_state SET quote_reserved='0'"
    )

    _raw_insert(
        engine.db_path,
        "fills",
        [
            "trade_id", "order_id", "symbol", "side",
            "price", "quantity", "fee", "fee_asset",
            "event_time", "resulting_state",
            "executed_qty", "remaining_qty",
        ],
        [
            "fill-1", order.intent.client_order_id, "BNBUSDT", "BUY",
            "100", "1", "0.1", "USDT",
            NOW.isoformat(), "FILLED",
            "1", "0",
        ],
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.FILL_FOR_REJECTED for e in result.errors
    )


def test_partial_qty_zero_is_reported(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="2")

    _execute(
        engine.db_path,
        "UPDATE orders SET status='PARTIALLY_FILLED' "
        "WHERE client_order_id=?",
        (order.intent.client_order_id,),
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.PARTIAL_QTY_ZERO for e in result.errors
    )


# ---------------------------------------------------------------------------
# Account-state invariants
# ---------------------------------------------------------------------------


def test_base_reserved_mismatch_is_reported(tmp_path):
    engine = _engine(tmp_path)
    _submit(engine, side="BUY", price="100", quantity="1")

    _execute(
        engine.db_path,
        "UPDATE paper_account_state SET base_reserved='999'"
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.BASE_RESERVED_MISMATCH
        for e in result.errors
    )


def test_quote_reserved_mismatch_is_reported(tmp_path):
    engine = _engine(tmp_path)
    _submit(engine, side="BUY", price="100", quantity="1")

    _execute(
        engine.db_path,
        "UPDATE paper_account_state SET quote_reserved='0'"
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.QUOTE_RESERVED_MISMATCH
        for e in result.errors
    )


def test_negative_balance_is_reported(tmp_path):
    engine = _engine(tmp_path)

    _execute(
        engine.db_path,
        "UPDATE paper_account_state SET base_free='-1'"
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.NEGATIVE_BALANCE for e in result.errors
    )


def test_malformed_decimal_is_reported(tmp_path):
    engine = _engine(tmp_path)

    _execute(
        engine.db_path,
        "UPDATE paper_account_state SET base_free='not-a-number'"
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.MALFORMED_DECIMAL for e in result.errors
    )


# ---------------------------------------------------------------------------
# Accounting event invariants
# ---------------------------------------------------------------------------


def test_account_event_unknown_order_is_reported(tmp_path):
    path = _empty_db(tmp_path)
    _seed_account_state(path)
    _raw_insert(
        path,
        "paper_accounting_events",
        ["event_id", "event_type", "client_order_id", "payload_json", "created_at"],
        [
            "reserve:ghost", "RESERVE", "AG-BNBUSDT-00000-B",
            json.dumps({"side": "BUY", "asset": "USDT", "amount": "100"}),
            NOW.isoformat(),
        ],
    )

    result = recover_paper_state(path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.ACCOUNT_EVENT_MISMATCH
        for e in result.errors
    )


def test_account_event_invalid_json_is_reported(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _raw_insert(
        engine.db_path,
        "paper_accounting_events",
        ["event_id", "event_type", "client_order_id", "payload_json", "created_at"],
        [
            "reserve:bad", "RESERVE", order.intent.client_order_id,
            "{not-json", NOW.isoformat(),
        ],
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.ACCOUNT_EVENT_MISMATCH
        for e in result.errors
    )


def test_account_event_reserve_missing_payload_key(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _raw_insert(
        engine.db_path,
        "paper_accounting_events",
        ["event_id", "event_type", "client_order_id", "payload_json", "created_at"],
        [
            "reserve:bad2", "RESERVE", order.intent.client_order_id,
            json.dumps({"side": "BUY", "asset": "USDT"}),  # missing 'amount'
            NOW.isoformat(),
        ],
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.ACCOUNT_EVENT_MISMATCH
        for e in result.errors
    )


def test_account_event_fill_missing_payload_key(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _raw_insert(
        engine.db_path,
        "paper_accounting_events",
        ["event_id", "event_type", "client_order_id", "payload_json", "created_at"],
        [
            "fill:bad", "FILL", order.intent.client_order_id,
            json.dumps({"side": "BUY"}),  # missing keys
            NOW.isoformat(),
        ],
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.ACCOUNT_EVENT_MISMATCH
        for e in result.errors
    )


def test_account_event_release_missing_payload_key(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _raw_insert(
        engine.db_path,
        "paper_accounting_events",
        ["event_id", "event_type", "client_order_id", "payload_json", "created_at"],
        [
            "release:bad", "RELEASE", order.intent.client_order_id,
            json.dumps({"side": "BUY"}),  # missing keys
            NOW.isoformat(),
        ],
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.ACCOUNT_EVENT_MISMATCH
        for e in result.errors
    )


# ---------------------------------------------------------------------------
# Order internal consistency
# ---------------------------------------------------------------------------


def test_filled_qty_mismatch_is_reported(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _execute(
        engine.db_path,
        "UPDATE orders SET status='FILLED', executed_qty='0', "
        "remaining_qty='1' WHERE client_order_id=?",
        (order.intent.client_order_id,),
    )
    _execute(
        engine.db_path,
        "UPDATE paper_reservations SET remaining_amount='0' "
        "WHERE client_order_id=?",
        (order.intent.client_order_id,),
    )
    _execute(
        engine.db_path,
        "UPDATE paper_account_state SET quote_reserved='0'"
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.FILLED_QTY_MISMATCH for e in result.errors
    )


def test_unknown_order_state_is_reported(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _execute(
        engine.db_path,
        "UPDATE orders SET status='WHAT' WHERE client_order_id=?",
        (order.intent.client_order_id,),
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.UNKNOWN_ORDER_STATE for e in result.errors
    )


def test_planned_order_with_nonzero_executed_is_reported(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _execute(
        engine.db_path,
        "UPDATE orders SET status='PLANNED', executed_qty='1' "
        "WHERE client_order_id=?",
        (order.intent.client_order_id,),
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.FILL_STATE_IMPOSSIBLE
        for e in result.errors
    )


def test_remaining_qty_inconsistent_with_executed(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _execute(
        engine.db_path,
        "UPDATE orders SET executed_qty='1', remaining_qty='999' "
        "WHERE client_order_id=?",
        (order.intent.client_order_id,),
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.FILL_CUMULATIVE_MISMATCH
        for e in result.errors
    )


def test_executed_qty_exceeds_quantity(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _execute(
        engine.db_path,
        "UPDATE orders SET executed_qty='5' WHERE client_order_id=?",
        (order.intent.client_order_id,),
    )

    result = recover_paper_state(engine.db_path)

    assert result.healthy is False
    assert any(
        e.code is RecoveryErrorCode.FILL_EXCEEDS_QUANTITY
        for e in result.errors
    )


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------


def test_recovery_does_not_mutate_database(tmp_path):
    engine = _engine(tmp_path)
    _submit(engine, side="BUY", price="100", quantity="1")

    con_before = connect(engine.db_path)
    try:
        orders_before = con_before.execute(
            "SELECT count(*) FROM orders"
        ).fetchone()[0]
        state_before = con_before.execute(
            "SELECT quote_free FROM paper_account_state WHERE id=1"
        ).fetchone()[0]
    finally:
        con_before.close()

    recover_paper_state(engine.db_path)

    con_after = connect(engine.db_path)
    try:
        orders_after = con_after.execute(
            "SELECT count(*) FROM orders"
        ).fetchone()[0]
        state_after = con_after.execute(
            "SELECT quote_free FROM paper_account_state WHERE id=1"
        ).fetchone()[0]
    finally:
        con_after.close()

    assert orders_before == orders_after == 1
    assert state_before == state_after


# ---------------------------------------------------------------------------
# RecoveryResult API
# ---------------------------------------------------------------------------


def test_recovery_result_raise_if_unhealthy_raises(tmp_path):
    path = _empty_db(tmp_path)
    result = recover_paper_state(path)

    assert result.healthy is False
    with pytest.raises(RecoveryUnhealthyError, match="reconciliation failed"):
        result.raise_if_unhealthy()


def test_recovery_result_raise_if_unhealthy_passes_when_healthy(tmp_path):
    engine = _engine(tmp_path)

    result = recover_paper_state(engine.db_path)

    assert result.healthy is True
    # Must not raise.
    result.raise_if_unhealthy()


def test_recovery_result_counts_match_recovered_entities(tmp_path):
    engine = _engine(tmp_path)
    _submit(engine, index=0, side="BUY", price="100", quantity="1")
    _submit(engine, index=1, side="SELL", price="110", quantity="1")

    result = recover_paper_state(engine.db_path)

    assert result.healthy is True
    assert result.recovered_orders == 2
    assert result.recovered_reservations == 2
    assert result.recovered_fills == 0
    assert result.account_state_valid is True


# ---------------------------------------------------------------------------
# Integration with PaperOrderEngine gate
# ---------------------------------------------------------------------------


def test_engine_reconcile_on_init_reports_healthy_state(tmp_path):
    engine = _engine(tmp_path)

    assert engine.recovery_result is not None
    assert engine.recovery_result.healthy is True


def test_engine_reconcile_after_corruption_reports_unhealthy(tmp_path):
    engine = _engine(tmp_path)

    _execute(
        engine.db_path,
        "UPDATE paper_account_state SET base_free='-1'"
    )
    result = engine.reconcile()

    assert result.healthy is False


def test_engine_blocks_submission_when_unhealthy(tmp_path):
    engine = _engine(tmp_path)
    _execute(
        engine.db_path,
        "UPDATE paper_account_state SET base_free='-1'"
    )
    engine.reconcile()
    assert engine.recovery_result.healthy is False

    intent = _intent()
    with pytest.raises(PaperStateUnhealthyError):
        engine.submit(
            intent, RiskDecision(True), Decimal("90"), Decimal("110")
        )


def test_engine_blocks_fill_when_unhealthy(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _execute(
        engine.db_path,
        "UPDATE paper_account_state SET base_free='-1'"
    )
    engine.reconcile()
    assert engine.recovery_result.healthy is False

    with pytest.raises(PaperStateUnhealthyError):
        engine.apply_fill(
            order.intent.client_order_id,
            "fill-1",
            "BNBUSDT",
            Decimal("99"),
            Decimal("1"),
            NOW,
        )


def test_engine_blocks_transition_when_unhealthy(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    _execute(
        engine.db_path,
        "UPDATE paper_account_state SET base_free='-1'"
    )
    engine.reconcile()
    assert engine.recovery_result.healthy is False

    with pytest.raises(PaperStateUnhealthyError):
        engine.transition(order.intent.client_order_id, OrderState.CANCELED)


def test_engine_recovers_after_reconcile_restores_health(tmp_path):
    engine = _engine(tmp_path)
    order, _ = _submit(engine, side="BUY", price="100", quantity="1")

    # Break the state, then repair it.
    _execute(
        engine.db_path,
        "UPDATE paper_account_state SET base_free='-1'"
    )
    assert engine.reconcile().healthy is False

    _execute(
        engine.db_path,
        "UPDATE paper_account_state SET base_free='2'"
    )
    result = engine.reconcile()
    assert result.healthy is True

    # Submission must work again.
    new_intent = _intent(index=1, side="BUY", price="105", quantity="1")
    new_order = engine.submit(
        new_intent, RiskDecision(True), Decimal("90"), Decimal("110")
    )
    assert new_order.state is OrderState.OPEN


def test_accounting_less_engine_does_not_gate(tmp_path):
    """Engines without accounting skip reconciliation entirely."""
    path = str(tmp_path / "noacct.sqlite3")
    engine = PaperOrderEngine(path, clock=lambda: NOW)

    assert engine.recovery_result is None
    # Submitting should work without a reconciliation gate.
    intent = _intent()
    order = engine.submit(
        intent, RiskDecision(True), Decimal("90"), Decimal("110")
    )
    assert order.state is OrderState.OPEN
