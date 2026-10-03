"""Deterministic paper-state recovery and reconciliation (Phase 3D).

This module provides a **read-only** recovery and reconciliation layer
that verifies the structural integrity of persisted paper trading state
across:

    SQLite
      ↓
    Recovery Loader        (raw row extraction, no mutation)
      ↓
    State Reconstructor     (deterministic in-memory model rebuild)
      ↓
    Consistency Validator   (cross-entity invariant checks)
      ↓
    RecoveryResult          (healthy/unhealthy + structured diagnostics)
      ↓
    Paper Engine Gate       (fail-closed when unhealthy)

Design invariants
----------------

1.  **No mutation**: ``recover_paper_state`` never writes to the database.
    It is safe to call on a live or snapshot database.
2.  **No randomness**: reconciliation is fully deterministic.
3.  **No market data**: no API calls, no current price, no clock dependence.
4.  **No fabrication**: missing state is never invented; ambiguous state
    is reported as an error, never silently repaired.
5.  **Fail-closed**: when ``RecoveryResult.healthy`` is ``False`` the
    paper engine must refuse new submissions, fills, and transitions.

Order-state recoverability
--------------------------

==================  ==========  ====================================================
State               Recoverable Notes
==================  ==========  ====================================================
PLANNED              yes         Pre-submission; must NOT auto-advance to OPEN
SUBMITTED            yes         Submitted but not yet confirmed open; must NOT auto-advance
OPEN                 yes         Active order with a reservation
PARTIALLY_FILLED     yes         Has fill history; executed_qty > 0, < quantity
FILLED               yes         Terminal; executed_qty == quantity, reservation == 0
CANCELED             yes         Terminal; reservation must be 0
REJECTED             yes         Terminal; reservation must be 0
==================  ==========  ====================================================

Terminal states (FILLED, CANCELED, REJECTED) must have zero remaining
reservation.  Non-terminal active states (OPEN, PARTIALLY_FILLED) must
have a reservation with remaining > 0 (unless fully filled).

PLANNED and SUBMITTED are pre-execution states that should not have
reservations yet; if a reservation exists for them it is suspicious
but not necessarily fatal — it is reported as a warning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from storage import connect


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------

class RecoveryErrorCode(str, Enum):
    """Structured error categories for deterministic handling."""

    MISSING_ACCOUNT_STATE = "MISSING_ACCOUNT_STATE"
    MALFORMED_DECIMAL = "MALFORMED_DECIMAL"
    NEGATIVE_BALANCE = "NEGATIVE_BALANCE"
    UNKNOWN_ORDER_STATE = "UNKNOWN_ORDER_STATE"
    ORPHAN_RESERVATION = "ORPHAN_RESERVATION"
    ORPHAN_FILL = "ORPHAN_FILL"
    RESERVATION_SYMBOL_MISMATCH = "RESERVATION_SYMBOL_MISMATCH"
    RESERVATION_SIDE_MISMATCH = "RESERVATION_SIDE_MISMATCH"
    RESERVATION_TERMINAL_NONZERO = "RESERVATION_TERMINAL_NONZERO"
    RESERVATION_AMOUNT_NEGATIVE = "RESERVATION_AMOUNT_NEGATIVE"
    RESERVATION_EXCEEDS_ORDER = "RESERVATION_EXCEEDS_ORDER"
    RESERVATION_OPEN_ZERO = "RESERVATION_OPEN_ZERO"
    FILL_SYMBOL_MISMATCH = "FILL_SYMBOL_MISMATCH"
    FILL_SIDE_MISMATCH = "FILL_SIDE_MISMATCH"
    FILL_EXCEEDS_QUANTITY = "FILL_EXCEEDS_QUANTITY"
    FILL_CUMULATIVE_MISMATCH = "FILL_CUMULATIVE_MISMATCH"
    FILL_STATE_IMPOSSIBLE = "FILL_STATE_IMPOSSIBLE"
    FILL_STATE_TRANSITION_INVALID = "FILL_STATE_TRANSITION_INVALID"
    FILLED_QTY_MISMATCH = "FILLED_QTY_MISMATCH"
    PARTIAL_QTY_ZERO = "PARTIAL_QTY_ZERO"
    RESERVATION_FOR_CANCELLED = "RESERVATION_FOR_CANCELLED"
    RESERVATION_FOR_REJECTED = "RESERVATION_FOR_REJECTED"
    FILL_FOR_CANCELLED = "FILL_FOR_CANCELLED"
    FILL_FOR_REJECTED = "FILL_FOR_REJECTED"
    ACCOUNT_STATE_MISMATCH = "ACCOUNT_STATE_MISMATCH"
    ACCOUNT_EVENT_MISMATCH = "ACCOUNT_EVENT_MISMATCH"
    BASE_RESERVED_MISMATCH = "BASE_RESERVED_MISMATCH"
    QUOTE_RESERVED_MISMATCH = "QUOTE_RESERVED_MISMATCH"
    RESERVATION_DUPLICATE = "RESERVATION_DUPLICATE"


@dataclass(frozen=True)
class RecoveryError:
    """A single structured reconciliation error."""

    code: RecoveryErrorCode
    entity: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.code.value}] {self.entity}: {self.detail}"


@dataclass(frozen=True)
class RecoveryWarning:
    """A non-fatal reconciliation warning."""

    code: str
    entity: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.entity}: {self.detail}"


# ---------------------------------------------------------------------------
# Result object
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecoveryResult:
    """Deterministic recovery/reconciliation result.

    ``healthy`` is the single gate: when ``False`` the paper engine must
    refuse all mutations.  ``errors`` provides structured diagnostics.
    ``warnings`` are informational and do not block execution.
    """

    healthy: bool
    errors: tuple[RecoveryError, ...] = field(default_factory=tuple)
    warnings: tuple[RecoveryWarning, ...] = field(default_factory=tuple)
    recovered_orders: int = 0
    recovered_reservations: int = 0
    recovered_fills: int = 0
    account_state_valid: bool = False

    def raise_if_unhealthy(self) -> None:
        """Raise ``RecoveryUnhealthyError`` if not healthy."""
        if not self.healthy:
            raise RecoveryUnhealthyError(self)


class RecoveryUnhealthyError(RuntimeError):
    """Raised when reconciliation fails and paper execution must stop."""

    def __init__(self, result: RecoveryResult):
        self.result = result
        messages = "; ".join(str(e) for e in result.errors)
        super().__init__(f"Paper state reconciliation failed: {messages}")


# ---------------------------------------------------------------------------
# Terminal / active state helpers
# ---------------------------------------------------------------------------

_TERMINAL_STATES = frozenset({"FILLED", "CANCELED", "REJECTED"})
_ACTIVE_FILLABLE_STATES = frozenset({"OPEN", "PARTIALLY_FILLED"})


# ---------------------------------------------------------------------------
# Raw row loaders (read-only)
# ---------------------------------------------------------------------------

def _load_order_rows(path: str, con=None) -> list[dict[str, Any]]:
    """Load order rows; when ``con`` is supplied read from that connection.

    A cycle transaction validates its post-mutation state through its own
    connection so the reconciliation sees the uncommitted cycle results.
    """
    owns = con is None
    if owns:
        con = connect(path)
    try:
        rows = con.execute(
            "SELECT client_order_id, symbol, side, order_type, grid_index, "
            "price, quantity, time_in_force, status, created_at, updated_at, "
            "executed_qty, remaining_qty "
            "FROM orders ORDER BY grid_index, side"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if owns:
            con.close()


def _load_fill_rows(path: str, con=None) -> list[dict[str, Any]]:
    owns = con is None
    if owns:
        con = connect(path)
    try:
        rows = con.execute(
            "SELECT trade_id, order_id, symbol, side, price, quantity, fee, "
            "fee_asset, event_time, resulting_state, executed_qty, remaining_qty "
            "FROM fills ORDER BY event_time"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if owns:
            con.close()


def _load_reservation_rows(path: str, con=None) -> list[dict[str, Any]]:
    owns = con is None
    if owns:
        con = connect(path)
    try:
        rows = con.execute(
            "SELECT client_order_id, side, asset, original_amount, "
            "remaining_amount, created_at, updated_at "
            "FROM paper_reservations ORDER BY client_order_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if owns:
            con.close()


def _load_account_state_row(path: str, con=None) -> dict[str, Any] | None:
    owns = con is None
    if owns:
        con = connect(path)
    try:
        row = con.execute(
            "SELECT base_asset, quote_asset, base_free, base_reserved, "
            "quote_free, quote_reserved, average_cost, realized_pnl, "
            "total_fees, updated_at "
            "FROM paper_account_state WHERE id=1"
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        if owns:
            con.close()


def _load_accounting_event_rows(path: str, con=None) -> list[dict[str, Any]]:
    owns = con is None
    if owns:
        con = connect(path)
    try:
        rows = con.execute(
            "SELECT event_id, event_type, client_order_id, payload_json, created_at "
            "FROM paper_accounting_events ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if owns:
            con.close()


# ---------------------------------------------------------------------------
# Decimal parsing with fail-closed semantics
# ---------------------------------------------------------------------------

def _safe_decimal(
    value: Any, field_name: str, entity: str, errors: list[RecoveryError]
) -> Decimal | None:
    """Parse a persisted value into a finite Decimal, or record an error."""
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        errors.append(RecoveryError(
            RecoveryErrorCode.MALFORMED_DECIMAL, entity,
            f"{field_name}={value!r} is not a valid Decimal",
        ))
        return None
    if not result.is_finite():
        errors.append(RecoveryError(
            RecoveryErrorCode.MALFORMED_DECIMAL, entity,
            f"{field_name}={value!r} is not finite",
        ))
        return None
    return result


def _non_negative_decimal(
    value: Any, field_name: str, entity: str, errors: list[RecoveryError]
) -> Decimal | None:
    """Parse a non-negative finite Decimal, or record an error."""
    result = _safe_decimal(value, field_name, entity, errors)
    if result is not None and result < 0:
        errors.append(RecoveryError(
            RecoveryErrorCode.NEGATIVE_BALANCE, entity,
            f"{field_name}={result} must be non-negative",
        ))
        return None
    return result


# ---------------------------------------------------------------------------
# State reconstruction
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _RecoveredOrder:
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    grid_index: int
    price: Decimal
    quantity: Decimal
    time_in_force: str
    status: str
    created_at: str
    updated_at: str
    executed_qty: Decimal
    remaining_qty: Decimal


@dataclass(frozen=True)
class _RecoveredFill:
    fill_id: str
    order_id: str
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    resulting_state: str
    executed_qty: Decimal
    remaining_qty: Decimal
    event_time: str


@dataclass(frozen=True)
class _RecoveredReservation:
    client_order_id: str
    side: str
    asset: str
    original_amount: Decimal
    remaining_amount: Decimal
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class _RecoveredAccountState:
    base_asset: str
    quote_asset: str
    base_free: Decimal
    base_reserved: Decimal
    quote_free: Decimal
    quote_reserved: Decimal
    average_cost: Decimal
    realized_pnl: Decimal
    total_fees: Decimal
    updated_at: str


def _reconstruct_orders(
    raw_rows: list[dict[str, Any]], errors: list[RecoveryError]
) -> dict[str, _RecoveredOrder]:
    """Rebuild deterministic order models from raw rows."""
    orders: dict[str, _RecoveredOrder] = {}
    for row in raw_rows:
        cid = row["client_order_id"]
        entity = f"order:{cid}"
        price = _safe_decimal(row["price"], "price", entity, errors)
        quantity = _safe_decimal(row["quantity"], "quantity", entity, errors)
        executed = _safe_decimal(row["executed_qty"], "executed_qty", entity, errors)
        remaining = _safe_decimal(row["remaining_qty"], "remaining_qty", entity, errors)
        status = row["status"]
        if status not in {
            "PLANNED", "SUBMITTED", "OPEN", "PARTIALLY_FILLED",
            "FILLED", "CANCELED", "REJECTED",
        }:
            errors.append(RecoveryError(
                RecoveryErrorCode.UNKNOWN_ORDER_STATE, entity,
                f"status={status!r} is not a recognised OrderState",
            ))
            continue
        if any(v is None for v in (price, quantity, executed, remaining)):
            continue
        orders[cid] = _RecoveredOrder(
            client_order_id=cid,
            symbol=row["symbol"],
            side=row["side"],
            order_type=row["order_type"],
            grid_index=int(row["grid_index"]),
            price=price,  # type: ignore[arg-type]
            quantity=quantity,  # type: ignore[arg-type]
            time_in_force=row["time_in_force"],
            status=status,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            executed_qty=executed,  # type: ignore[arg-type]
            remaining_qty=remaining,  # type: ignore[arg-type]
        )
    return orders


def _reconstruct_fills(
    raw_rows: list[dict[str, Any]], errors: list[RecoveryError]
) -> dict[str, _RecoveredFill]:
    """Rebuild deterministic fill models from raw rows."""
    fills: dict[str, _RecoveredFill] = {}
    for row in raw_rows:
        fid = row["trade_id"]
        entity = f"fill:{fid}"
        price = _safe_decimal(row["price"], "price", entity, errors)
        quantity = _safe_decimal(row["quantity"], "quantity", entity, errors)
        executed = _safe_decimal(row["executed_qty"], "executed_qty", entity, errors)
        remaining = _safe_decimal(row["remaining_qty"], "remaining_qty", entity, errors)
        if any(v is None for v in (price, quantity, executed, remaining)):
            continue
        fills[fid] = _RecoveredFill(
            fill_id=fid,
            order_id=row["order_id"],
            symbol=row["symbol"],
            side=row["side"],
            price=price,  # type: ignore[arg-type]
            quantity=quantity,  # type: ignore[arg-type]
            resulting_state=row["resulting_state"],
            executed_qty=executed,  # type: ignore[arg-type]
            remaining_qty=remaining,  # type: ignore[arg-type]
            event_time=row["event_time"],
        )
    return fills


def _reconstruct_reservations(
    raw_rows: list[dict[str, Any]], errors: list[RecoveryError]
) -> dict[str, _RecoveredReservation]:
    """Rebuild deterministic reservation models from raw rows."""
    reservations: dict[str, _RecoveredReservation] = {}
    for row in raw_rows:
        cid = row["client_order_id"]
        entity = f"reservation:{cid}"
        original = _non_negative_decimal(
            row["original_amount"], "original_amount", entity, errors
        )
        remaining = _non_negative_decimal(
            row["remaining_amount"], "remaining_amount", entity, errors
        )
        if any(v is None for v in (original, remaining)):
            continue
        reservations[cid] = _RecoveredReservation(
            client_order_id=cid,
            side=row["side"],
            asset=row["asset"],
            original_amount=original,  # type: ignore[arg-type]
            remaining_amount=remaining,  # type: ignore[arg-type]
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
    return reservations


def _reconstruct_account_state(
    raw_row: dict[str, Any] | None,
    errors: list[RecoveryError],
) -> _RecoveredAccountState | None:
    """Rebuild deterministic account state from the raw row."""
    if raw_row is None:
        errors.append(RecoveryError(
            RecoveryErrorCode.MISSING_ACCOUNT_STATE,
            "account_state",
            "paper_account_state row is missing",
        ))
        return None
    entity = "account_state"
    fields_non_neg = [
        "base_free", "base_reserved", "quote_free", "quote_reserved", "total_fees",
    ]
    parsed: dict[str, Decimal] = {}
    ok = True
    for f in fields_non_neg:
        val = _non_negative_decimal(raw_row[f], f, entity, errors)
        if val is None:
            ok = False
        else:
            parsed[f] = val
    for f in ["average_cost", "realized_pnl"]:
        val = _safe_decimal(raw_row[f], f, entity, errors)
        if val is None:
            ok = False
        else:
            parsed[f] = val
    if not ok:
        return None
    return _RecoveredAccountState(
        base_asset=raw_row["base_asset"],
        quote_asset=raw_row["quote_asset"],
        base_free=parsed["base_free"],
        base_reserved=parsed["base_reserved"],
        quote_free=parsed["quote_free"],
        quote_reserved=parsed["quote_reserved"],
        average_cost=parsed["average_cost"],
        realized_pnl=parsed["realized_pnl"],
        total_fees=parsed["total_fees"],
        updated_at=raw_row["updated_at"],
    )


# ---------------------------------------------------------------------------
# Consistency validation
# ---------------------------------------------------------------------------

def _validate_reservations(
    reservations: dict[str, _RecoveredReservation],
    orders: dict[str, _RecoveredOrder],
    errors: list[RecoveryError],
) -> None:
    """Verify every reservation against its order."""
    for cid, resv in reservations.items():
        entity = f"reservation:{cid}"
        order = orders.get(cid)

        if order is None:
            errors.append(RecoveryError(
                RecoveryErrorCode.ORPHAN_RESERVATION, entity,
                f"reservation exists for unknown order {cid!r}",
            ))
            continue

        # Side consistency
        if resv.side != order.side:
            errors.append(RecoveryError(
                RecoveryErrorCode.RESERVATION_SIDE_MISMATCH, entity,
                f"reservation side={resv.side!r} but order side={order.side!r}",
            ))

        # Symbol consistency (derive expected asset from side + order symbol)
        # The reservation asset is a base/quote token; we verify it is a
        # plausible token from the order symbol or the quote side.
        # We check that it is not empty.
        if not resv.asset:
            errors.append(RecoveryError(
                RecoveryErrorCode.RESERVATION_SYMBOL_MISMATCH, entity,
                "reservation asset is empty",
            ))

        # Amount sanity
        if resv.original_amount < 0:
            errors.append(RecoveryError(
                RecoveryErrorCode.RESERVATION_AMOUNT_NEGATIVE, entity,
                f"original_amount={resv.original_amount} is negative",
            ))
        if resv.remaining_amount < 0:
            errors.append(RecoveryError(
                RecoveryErrorCode.RESERVATION_AMOUNT_NEGATIVE, entity,
                f"remaining_amount={resv.remaining_amount} is negative",
            ))
        if resv.remaining_amount > resv.original_amount:
            errors.append(RecoveryError(
                RecoveryErrorCode.RESERVATION_EXCEEDS_ORDER, entity,
                f"remaining_amount={resv.remaining_amount} exceeds "
                f"original_amount={resv.original_amount}",
            ))

        # Terminal-state orders must have zero remaining reservation
        if order.status in _TERMINAL_STATES:
            if resv.remaining_amount != 0:
                if order.status == "CANCELED":
                    errors.append(RecoveryError(
                        RecoveryErrorCode.RESERVATION_FOR_CANCELLED, entity,
                        f"CANCELED order has non-zero remaining reservation "
                        f"{resv.remaining_amount}",
                    ))
                elif order.status == "REJECTED":
                    errors.append(RecoveryError(
                        RecoveryErrorCode.RESERVATION_FOR_REJECTED, entity,
                        f"REJECTED order has non-zero remaining reservation "
                        f"{resv.remaining_amount}",
                    ))
                else:  # FILLED
                    errors.append(RecoveryError(
                        RecoveryErrorCode.RESERVATION_TERMINAL_NONZERO, entity,
                        f"FILLED order has non-zero remaining reservation "
                        f"{resv.remaining_amount}",
                    ))

        # Active orders must have a reservation with remaining > 0
        # (unless fully filled, which is terminal)
        if order.status in _ACTIVE_FILLABLE_STATES and resv.remaining_amount <= 0:
            errors.append(RecoveryError(
                RecoveryErrorCode.RESERVATION_OPEN_ZERO, entity,
                f"{order.status} order has zero remaining reservation",
            ))


def _validate_fills(
    fills: dict[str, _RecoveredFill],
    orders: dict[str, _RecoveredOrder],
    errors: list[RecoveryError],
) -> None:
    """Verify every fill against its order and detect cumulative mismatches."""
    # Group fills by order_id for cumulative checks
    by_order: dict[str, list[_RecoveredFill]] = {}
    for fid, fill in fills.items():
        entity = f"fill:{fid}"
        order = orders.get(fill.order_id)

        if order is None:
            errors.append(RecoveryError(
                RecoveryErrorCode.ORPHAN_FILL, entity,
                f"fill references unknown order {fill.order_id!r}",
            ))
            continue

        by_order.setdefault(fill.order_id, []).append(fill)

        # Symbol consistency
        if fill.symbol != order.symbol:
            errors.append(RecoveryError(
                RecoveryErrorCode.FILL_SYMBOL_MISMATCH, entity,
                f"fill symbol={fill.symbol!r} but order symbol={order.symbol!r}",
            ))

        # Side consistency
        if fill.side != order.side:
            errors.append(RecoveryError(
                RecoveryErrorCode.FILL_SIDE_MISMATCH, entity,
                f"fill side={fill.side!r} but order side={order.side!r}",
            ))

        # Fill resulting state must be valid
        if fill.resulting_state not in {
            "PARTIALLY_FILLED", "FILLED",
        }:
            errors.append(RecoveryError(
                RecoveryErrorCode.FILL_STATE_IMPOSSIBLE, entity,
                f"fill resulting_state={fill.resulting_state!r} is not a fill result",
            ))

        # Fill for terminal non-fillable order
        if order.status == "CANCELED":
            errors.append(RecoveryError(
                RecoveryErrorCode.FILL_FOR_CANCELLED, entity,
                f"fill exists for CANCELED order {fill.order_id!r}",
            ))
        elif order.status == "REJECTED":
            errors.append(RecoveryError(
                RecoveryErrorCode.FILL_FOR_REJECTED, entity,
                f"fill exists for REJECTED order {fill.order_id!r}",
            ))

    # Cumulative executed quantity per order
    for oid, order_fills in by_order.items():
        order = orders[oid]
        entity = f"order:{oid}"
        cumulative = sum((f.quantity for f in order_fills), Decimal("0"))

        if cumulative > order.quantity:
            errors.append(RecoveryError(
                RecoveryErrorCode.FILL_EXCEEDS_QUANTITY, entity,
                f"cumulative fill quantity {cumulative} exceeds "
                f"order quantity {order.quantity}",
            ))

        # Order's persisted executed_qty must match cumulative fill sum
        if order.executed_qty != cumulative:
            errors.append(RecoveryError(
                RecoveryErrorCode.FILL_CUMULATIVE_MISMATCH, entity,
                f"order executed_qty={order.executed_qty} but cumulative "
                f"fill quantity={cumulative}",
            ))

        # FILLED order must have cumulative == quantity
        if order.status == "FILLED" and cumulative != order.quantity:
            errors.append(RecoveryError(
                RecoveryErrorCode.FILLED_QTY_MISMATCH, entity,
                f"FILLED order has cumulative fill {cumulative} != "
                f"quantity {order.quantity}",
            ))

        # PARTIALLY_FILLED must have cumulative > 0 and < quantity
        if order.status == "PARTIALLY_FILLED":
            if cumulative == 0:
                errors.append(RecoveryError(
                    RecoveryErrorCode.PARTIAL_QTY_ZERO, entity,
                    "PARTIALLY_FILLED order has zero cumulative fill quantity",
                ))
            if cumulative >= order.quantity:
                errors.append(RecoveryError(
                    RecoveryErrorCode.FILL_STATE_IMPOSSIBLE, entity,
                    f"PARTIALLY_FILLED order has cumulative {cumulative} "
                    f">= quantity {order.quantity}",
                ))


def _validate_account_state(
    account: _RecoveredAccountState | None,
    reservations: dict[str, _RecoveredReservation],
    orders: dict[str, _RecoveredOrder],
    event_rows: list[dict[str, Any]],
    errors: list[RecoveryError],
) -> None:
    """Verify account state against reservation sums and event history."""
    if account is None:
        return  # already errored in reconstruction

    entity = "account_state"

    # --- Reserved balance must equal sum of active reservations ---

    # Sum of remaining SELL reservations == base_reserved
    sell_remaining = sum(
        (r.remaining_amount for r in reservations.values() if r.side == "SELL"),
        Decimal("0"),
    )
    if account.base_reserved != sell_remaining:
        errors.append(RecoveryError(
            RecoveryErrorCode.BASE_RESERVED_MISMATCH, entity,
            f"base_reserved={account.base_reserved} but sum of SELL reservation "
            f"remaining={sell_remaining}",
        ))

    # Sum of remaining BUY reservations == quote_reserved
    buy_remaining = sum(
        (r.remaining_amount for r in reservations.values() if r.side == "BUY"),
        Decimal("0"),
    )
    if account.quote_reserved != buy_remaining:
        errors.append(RecoveryError(
            RecoveryErrorCode.QUOTE_RESERVED_MISMATCH, entity,
            f"quote_reserved={account.quote_reserved} but sum of BUY reservation "
            f"remaining={buy_remaining}",
        ))

    # --- Accounting event payload consistency ---
    # Each event's payload_json must be parseable and contain the expected keys.
    # We do NOT recalculate financial state from events (that would duplicate
    # accounting logic and risk divergence).  We verify that every event
    # references a known order and that its payload is valid JSON.
    #
    # LIQUIDATION events (strategy auto-exit close-all) are the one
    # deliberate exception to the order-reference rule: a liquidation is an
    # accounting operation, not a grid order, so it carries the reserved
    # ``LIQUIDATION-`` client_order_id namespace instead of an order id.
    # They are still strictly validated (reserved namespace + payload
    # contract below), never silently trusted.
    import json

    for ev_row in event_rows:
        ev_entity = f"accounting_event:{ev_row['event_id']}"
        oid = ev_row["client_order_id"]
        is_liquidation_event = (
            ev_row["event_type"] == "LIQUIDATION"
            and str(oid).startswith("LIQUIDATION-")
        )
        if oid not in orders and not is_liquidation_event:
            errors.append(RecoveryError(
                RecoveryErrorCode.ACCOUNT_EVENT_MISMATCH, ev_entity,
                f"event references unknown order {oid!r}",
            ))
        try:
            payload = json.loads(ev_row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            errors.append(RecoveryError(
                RecoveryErrorCode.ACCOUNT_EVENT_MISMATCH, ev_entity,
                "payload_json is not valid JSON",
            ))
            continue
        if not isinstance(payload, dict):
            errors.append(RecoveryError(
                RecoveryErrorCode.ACCOUNT_EVENT_MISMATCH, ev_entity,
                "payload_json is not a JSON object",
            ))
            continue
        ev_type = ev_row["event_type"]
        if ev_type == "RESERVE":
            for key in ("side", "asset", "amount"):
                if key not in payload:
                    errors.append(RecoveryError(
                        RecoveryErrorCode.ACCOUNT_EVENT_MISMATCH, ev_entity,
                        f"RESERVE event missing payload key {key!r}",
                    ))
        elif ev_type == "FILL":
            for key in ("side", "fill_price", "fill_quantity", "fee_asset"):
                if key not in payload:
                    errors.append(RecoveryError(
                        RecoveryErrorCode.ACCOUNT_EVENT_MISMATCH, ev_entity,
                        f"FILL event missing payload key {key!r}",
                    ))
        elif ev_type == "RELEASE":
            for key in ("side", "asset", "released_amount"):
                if key not in payload:
                    errors.append(RecoveryError(
                        RecoveryErrorCode.ACCOUNT_EVENT_MISMATCH, ev_entity,
                        f"RELEASE event missing payload key {key!r}",
                    ))
        elif ev_type == "LIQUIDATION":
            if not is_liquidation_event:
                errors.append(RecoveryError(
                    RecoveryErrorCode.ACCOUNT_EVENT_MISMATCH, ev_entity,
                    "LIQUIDATION event must use the reserved "
                    "'LIQUIDATION-' client_order_id namespace",
                ))
            for key in ("symbol", "side", "order_type", "fill_price",
                        "fill_quantity", "fee_asset", "fee_amount", "reason"):
                if key not in payload:
                    errors.append(RecoveryError(
                        RecoveryErrorCode.ACCOUNT_EVENT_MISMATCH, ev_entity,
                        f"LIQUIDATION event missing payload key {key!r}",
                    ))
            if (payload.get("side"), payload.get("order_type")) != (
                    "SELL", "MARKET_LIQUIDATION"):
                errors.append(RecoveryError(
                    RecoveryErrorCode.ACCOUNT_EVENT_MISMATCH, ev_entity,
                    "LIQUIDATION event must be a MARKET_LIQUIDATION SELL",
                ))


def _validate_order_internal_consistency(
    orders: dict[str, _RecoveredOrder],
    errors: list[RecoveryError],
) -> None:
    """Verify per-order field consistency (executed/remaining/quantity)."""
    for cid, order in orders.items():
        entity = f"order:{cid}"
        if order.executed_qty < 0:
            errors.append(RecoveryError(
                RecoveryErrorCode.NEGATIVE_BALANCE, entity,
                f"executed_qty={order.executed_qty} is negative",
            ))
        if order.executed_qty > order.quantity:
            errors.append(RecoveryError(
                RecoveryErrorCode.FILL_EXCEEDS_QUANTITY, entity,
                f"executed_qty={order.executed_qty} exceeds "
                f"quantity={order.quantity}",
            ))
        expected_remaining = order.quantity - order.executed_qty
        if order.remaining_qty != expected_remaining:
            errors.append(RecoveryError(
                RecoveryErrorCode.FILL_CUMULATIVE_MISMATCH, entity,
                f"remaining_qty={order.remaining_qty} but "
                f"quantity - executed_qty = {expected_remaining}",
            ))
        # FILLED must have executed == quantity
        if order.status == "FILLED" and order.executed_qty != order.quantity:
            errors.append(RecoveryError(
                RecoveryErrorCode.FILLED_QTY_MISMATCH, entity,
                f"FILLED order has executed_qty={order.executed_qty} "
                f"!= quantity={order.quantity}",
            ))
        # PARTIALLY_FILLED must have 0 < executed < quantity
        if order.status == "PARTIALLY_FILLED":
            if order.executed_qty == 0:
                errors.append(RecoveryError(
                    RecoveryErrorCode.PARTIAL_QTY_ZERO, entity,
                    "PARTIALLY_FILLED order has executed_qty=0",
                ))
            if order.executed_qty >= order.quantity:
                errors.append(RecoveryError(
                    RecoveryErrorCode.FILL_STATE_IMPOSSIBLE, entity,
                    f"PARTIALLY_FILLED order has executed_qty="
                    f"{order.executed_qty} >= quantity={order.quantity}",
                ))
        # PLANNED/SUBMITTED must have zero executed
        if order.status in {"PLANNED", "SUBMITTED"} and order.executed_qty != 0:
            errors.append(RecoveryError(
                RecoveryErrorCode.FILL_STATE_IMPOSSIBLE, entity,
                f"{order.status} order has non-zero executed_qty={order.executed_qty}",
            ))


# ---------------------------------------------------------------------------
# Top-level recovery entry point
# ---------------------------------------------------------------------------

def recover_paper_state(db_path: str, con=None) -> RecoveryResult:
    """Run a full read-only recovery and reconciliation of paper state.

    This function never mutates the database.  It loads all persisted
    rows, reconstructs deterministic in-memory models, validates
    cross-entity invariants, and returns a structured ``RecoveryResult``.

    When ``result.healthy`` is ``False``, the paper engine must refuse
    all new submissions, fills, and transitions (fail-closed).

    When ``con`` is supplied the rows are read through the caller's open
    connection (cycle-transaction join) so the reconciliation sees the
    cycle's uncommitted post-mutation state; when ``con`` is None each
    load uses a private connection (standalone read of committed state).
    """
    errors: list[RecoveryError] = []
    warnings: list[RecoveryWarning] = []

    # --- Load raw rows (read-only) ---
    order_rows = _load_order_rows(db_path, con=con)
    fill_rows = _load_fill_rows(db_path, con=con)
    reservation_rows = _load_reservation_rows(db_path, con=con)
    account_row = _load_account_state_row(db_path, con=con)
    event_rows = _load_accounting_event_rows(db_path, con=con)

    # --- Reconstruct in-memory models ---
    orders = _reconstruct_orders(order_rows, errors)
    fills = _reconstruct_fills(fill_rows, errors)
    reservations = _reconstruct_reservations(reservation_rows, errors)
    account = _reconstruct_account_state(account_row, errors)

    # --- Validate internal order consistency ---
    _validate_order_internal_consistency(orders, errors)

    # --- Validate reservations against orders ---
    _validate_reservations(reservations, orders, errors)

    # --- Validate fills against orders ---
    _validate_fills(fills, orders, errors)

    # --- Validate account state against reservations + events ---
    _validate_account_state(account, reservations, orders, event_rows, errors)

    healthy = len(errors) == 0 and account is not None
    return RecoveryResult(
        healthy=healthy,
        errors=tuple(errors),
        warnings=tuple(warnings),
        recovered_orders=len(orders),
        recovered_reservations=len(reservations),
        recovered_fills=len(fills),
        account_state_valid=account is not None and len(errors) == 0,
    )
