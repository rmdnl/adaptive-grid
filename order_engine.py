from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import re
from typing import Callable

from risk_engine import RiskDecision
from paper_accounting import PaperAccountingEngine
from recovery import RecoveryResult, RecoveryUnhealthyError, recover_paper_state
from storage import (
    FillIdentityMismatch,
    get_fill,
    get_order,
    init_db,
    save_order,
    save_order_submission,
    save_paper_fill,
    ensure_paper_account_state,
    get_paper_account_state,
    get_paper_reservation,
)


_CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,36}$")
_FILL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SUPPORTED_ORDER_TYPES = frozenset({"LIMIT", "LIMIT_MAKER"})


class OrderIntentValidationError(ValueError):
    """Raised when a paper order intent is unsafe or malformed."""


class InvalidOrderStateTransition(ValueError):
    """Raised when an order lifecycle transition is not allowed."""


class DuplicateOrder(RuntimeError):
    """Raised when local state already owns a deterministic order identity."""


class RiskVeto(RuntimeError):
    """Raised when the existing risk engine vetoes paper submission."""


class OrderPriceOutOfRange(ValueError):
    """Raised when an order is outside the Phase 1 effective executable range."""


class OrderIdentityMismatch(OrderIntentValidationError):
    """Raised when an intent ID does not encode its configured grid identity."""


class PaperFillValidationError(ValueError):
    """Raised when a paper-fill request is malformed."""


class PaperFillStateError(PaperFillValidationError):
    """Raised when an order state cannot accept a paper fill."""


class PaperFillSymbolMismatch(PaperFillValidationError):
    """Raised when a paper-fill symbol does not match its order."""


class OrderState(str, Enum):
    PLANNED = "PLANNED"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


_ALLOWED_TRANSITIONS = {
    OrderState.PLANNED: frozenset({OrderState.SUBMITTED}),
    OrderState.SUBMITTED: frozenset({OrderState.OPEN, OrderState.REJECTED}),
    OrderState.OPEN: frozenset({OrderState.PARTIALLY_FILLED, OrderState.FILLED,
                              OrderState.CANCELED, OrderState.REJECTED}),
    OrderState.PARTIALLY_FILLED: frozenset({OrderState.FILLED, OrderState.CANCELED,
                                             OrderState.REJECTED}),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELED: frozenset(),
    OrderState.REJECTED: frozenset(),
}


def _decimal(value: object, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise OrderIntentValidationError(f"{field} must be a finite positive Decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OrderIntentValidationError(f"{field} must be a finite positive Decimal") from exc
    if not result.is_finite() or result <= 0:
        raise OrderIntentValidationError(f"{field} must be a finite positive Decimal")
    return result


def make_client_order_id(
    prefix: str,
    symbol: str,
    generation: int,
    grid_index: int,
    side: str,
) -> str:
    """Build a stable, generation-aware Binance-safe grid identity.

    Format: ``{prefix}-{symbol}-G{generation:05d}-{grid_index:05d}-{side[0]}``
    e.g. ``AG-BTCUSDT-G00002-00001-B``

    The ``G`` marker between symbol and grid index makes the new format
    structurally distinct from the legacy ``{prefix}-{symbol}-{index:05d}-{side}``
    format (which has no ``G``), so new IDs can never collide with legacy ones.
    """
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9]+", prefix):
        raise OrderIntentValidationError("client-order prefix must be ASCII alphanumeric")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]+", symbol):
        raise OrderIntentValidationError("symbol must be uppercase ASCII alphanumeric")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise OrderIntentValidationError("generation must be a non-negative integer")
    if not isinstance(grid_index, int) or isinstance(grid_index, bool) or grid_index < 0:
        raise OrderIntentValidationError("grid_index must be a non-negative integer")
    if side not in {"BUY", "SELL"}:
        raise OrderIntentValidationError("side must be BUY or SELL")
    result = f"{prefix}-{symbol}-G{generation:05d}-{grid_index:05d}-{side[0]}"
    if not _CLIENT_ORDER_ID_RE.fullmatch(result):
        raise OrderIntentValidationError("generated client_order_id is not Binance-safe")
    return result


def parse_generation_from_client_order_id(client_order_id: str) -> int | None:
    """Extract the generation from a generation-aware client_order_id.

    Returns ``None`` for legacy-format IDs (no ``G`` marker), which is the
    read-only compatibility path: legacy orders are read as-is but never
    reused by new submissions.
    """
    if not isinstance(client_order_id, str):
        return None
    m = re.fullmatch(
        r"[A-Za-z0-9]+-[A-Z0-9]+-G(\d{1,5})-\d{5}-[BS]",
        client_order_id,
    )
    if m is None:
        return None
    return int(m.group(1))


@dataclass(frozen=True)
class OrderIntent:
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    price: Decimal
    quantity: Decimal
    time_in_force: str
    grid_index: int
    generation: int
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.client_order_id, str) or not _CLIENT_ORDER_ID_RE.fullmatch(self.client_order_id):
            raise OrderIntentValidationError("client_order_id must be non-empty and Binance-safe")
        if not isinstance(self.symbol, str) or not re.fullmatch(r"[A-Z0-9]+", self.symbol):
            raise OrderIntentValidationError("symbol must be non-empty uppercase ASCII alphanumeric")
        if self.side not in {"BUY", "SELL"}:
            raise OrderIntentValidationError("side must be BUY or SELL")
        if self.order_type not in _SUPPORTED_ORDER_TYPES:
            raise OrderIntentValidationError("order_type is unsupported")
        if (self.order_type, self.time_in_force) not in {
            ("LIMIT", "GTC"), ("LIMIT_MAKER", "GTC")
        }:
            raise OrderIntentValidationError(
                "supported paper orders require LIMIT/LIMIT_MAKER with GTC"
            )
        if not isinstance(self.grid_index, int) or isinstance(self.grid_index, bool) or self.grid_index < 0:
            raise OrderIntentValidationError("grid_index must be a non-negative integer")
        if not isinstance(self.generation, int) or isinstance(self.generation, bool) or self.generation < 0:
            raise OrderIntentValidationError("generation must be a non-negative integer")
        # Invariant: generation-aware IDs (with G marker) must encode the same
        # generation as the intent field; a mismatch is a tampered ID.
        # Legacy IDs (no G marker) are read-only compatibility: they carry no
        # generation marker and are treated as generation 0.
        encoded = parse_generation_from_client_order_id(self.client_order_id)
        if encoded is not None and encoded != self.generation:
            raise OrderIntentValidationError(
                f"client_order_id encodes generation {encoded} but intent "
                f"generation is {self.generation}"
            )
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise OrderIntentValidationError("created_at must be timezone-aware")
        object.__setattr__(self, "price", _decimal(self.price, "price"))
        object.__setattr__(self, "quantity", _decimal(self.quantity, "quantity"))


@dataclass(frozen=True)
class PaperOrder:
    intent: OrderIntent
    state: OrderState
    updated_at: datetime
    executed_qty: Decimal = Decimal("0")

    @property
    def remaining_qty(self) -> Decimal:
        return self.intent.quantity - self.executed_qty


@dataclass(frozen=True)
class PaperFill:
    fill_id: str
    client_order_id: str
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    executed_qty: Decimal
    remaining_qty: Decimal
    state: OrderState
    filled_at: datetime


@dataclass(frozen=True)
class PaperFillResult:
    order: PaperOrder
    fill: PaperFill | None
    applied: bool
    idempotent: bool = False


def transition_order(order: PaperOrder, target: OrderState, updated_at: datetime) -> PaperOrder:
    if not isinstance(target, OrderState):
        raise InvalidOrderStateTransition(f"Unknown target state: {target!r}")
    if target not in _ALLOWED_TRANSITIONS[order.state]:
        raise InvalidOrderStateTransition(f"{order.state.value} -> {target.value} is not allowed")
    if not isinstance(updated_at, datetime) or updated_at.tzinfo is None:
        raise InvalidOrderStateTransition("updated_at must be timezone-aware")
    return replace(order, state=target, updated_at=updated_at)


class PaperStateUnhealthyError(RuntimeError):
    """Raised when paper state reconciliation fails and execution is gated."""

    def __init__(self, result: RecoveryResult):
        self.result = result
        messages = "; ".join(str(e) for e in result.errors)
        super().__init__(
            f"Paper state is unhealthy; execution gated: {messages}"
        )


class PaperOrderEngine:
    """Deterministic local-only execution state machine; it has no Binance client."""

    def __init__(
        self,
        db_path: str,
        clock: Callable[[], datetime] | None = None,
        client_order_prefix: str = "AG",
        accounting: PaperAccountingEngine | None = None,
        reconcile_on_init: bool = True,
    ):
        self.db_path = db_path
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if not isinstance(client_order_prefix, str) or not re.fullmatch(
            r"[A-Za-z0-9]+", client_order_prefix
        ):
            raise OrderIntentValidationError(
                "client_order_prefix must be ASCII alphanumeric"
            )
        self.client_order_prefix = client_order_prefix
        self.accounting = accounting
        init_db(db_path)
        self._recovery_result: RecoveryResult | None = None
        if self.accounting is not None:
            ensure_paper_account_state(db_path, self.accounting.initial_state())
            # Recovery/reconciliation is part of the paper-accounting layer,
            # which seeds and owns ``paper_account_state``.  Automatic
            # reconciliation and its fail-closed gate only apply then;
            # accounting-less engines keep running the deterministic order and
            # fill state machine without a reconciliation gate.
            if reconcile_on_init:
                self.reconcile()

    def reconcile(self, con=None) -> RecoveryResult:
        """Run a read-only recovery and reconciliation, caching the result.

        When the result is unhealthy, ``submit``, ``apply_fill``, and
        ``transition`` will raise ``PaperStateUnhealthyError``.

        When ``con`` is supplied (cycle-transaction join) the reconciliation
        reads through the caller's open connection so it validates the
        cycle's uncommitted post-mutation state; when ``con`` is None it
        reads committed state through private connections (standalone).
        """
        self._recovery_result = recover_paper_state(self.db_path, con=con)
        return self._recovery_result

    @property
    def recovery_result(self) -> RecoveryResult | None:
        """The most recent reconciliation result, or ``None`` if not yet run."""
        return self._recovery_result

    def _ensure_healthy(self) -> None:
        """Gate: raise if the last reconciliation was unhealthy."""
        result = self._recovery_result
        if result is not None and not result.healthy:
            raise PaperStateUnhealthyError(result)

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("paper-order clock must return a timezone-aware datetime")
        return now

    def get(self, client_order_id: str, con=None) -> PaperOrder | None:
        row = get_order(self.db_path, client_order_id, con=con)
        if row is None:
            return None
        # Read-only compatibility: parse generation from the persisted ID.
        # New generation-aware IDs (with G marker) return their generation.
        # Legacy IDs (no G marker) fall back to generation 0 so they remain
        # readable; they are never reused by new submissions.
        generation = parse_generation_from_client_order_id(client_order_id)
        if generation is None:
            generation = 0
        intent = OrderIntent(
            client_order_id=row["client_order_id"], symbol=row["symbol"], side=row["side"],
            order_type=row["order_type"], price=Decimal(row["price"]),
            quantity=Decimal(row["quantity"]), time_in_force=row["time_in_force"],
            grid_index=int(row["grid_index"]), generation=generation,
            created_at=datetime.fromisoformat(row["created_at"]),
        )
        return PaperOrder(
            intent,
            OrderState(row["status"]),
            datetime.fromisoformat(row["updated_at"]),
            Decimal(row["executed_qty"]),
        )

    def submit(
        self,
        intent: OrderIntent,
        risk_decision: RiskDecision,
        lower_price: Decimal,
        effective_upper: Decimal,
        con=None,
    ) -> PaperOrder:
        self._ensure_healthy()
        if not isinstance(intent, OrderIntent):
            raise OrderIntentValidationError("A valid OrderIntent is required")
        expected_client_order_id = make_client_order_id(
            self.client_order_prefix, intent.symbol,
            intent.generation, intent.grid_index, intent.side
        )
        if intent.client_order_id != expected_client_order_id:
            raise OrderIdentityMismatch(
                f"client_order_id {intent.client_order_id!r} does not match "
                f"configured grid identity {expected_client_order_id!r}"
            )
        if not isinstance(risk_decision, RiskDecision):
            raise RiskVeto("A valid RiskDecision is required before paper submission")
        if not risk_decision.allowed:
            raise RiskVeto(risk_decision.reason)
        lower = _decimal(lower_price, "lower_price")
        upper = _decimal(effective_upper, "effective_upper")
        if upper < lower or not lower <= intent.price <= upper:
            raise OrderPriceOutOfRange(
                f"Order price {intent.price} is outside effective range {lower}..{upper}"
            )
        existing = self.get(intent.client_order_id)
        if existing is not None:
            raise DuplicateOrder(
                f"Duplicate client_order_id {intent.client_order_id} in local state {existing.state.value}"
            )
        planned = PaperOrder(intent, OrderState.PLANNED, self._now())
        submitted = transition_order(planned, OrderState.SUBMITTED, self._now())
        opened = transition_order(submitted, OrderState.OPEN, self._now())
        accounting_update = None
        if self.accounting is not None:
            state = get_paper_account_state(self.db_path, con=con)
            if state is None:
                raise RuntimeError("Paper account state is missing")
            state_obj = PaperOrderEngine._state_from_dict(state)
            accounting_update = self.accounting.prepare_reservation(
                state_obj, opened, self._now()
            )
        save_order_submission(
            self.db_path, planned, submitted, opened, accounting_update, con=con
        )
        return opened

    def apply_fill(
        self,
        client_order_id: str,
        fill_id: str,
        symbol: str,
        market_price: Decimal,
        quantity: Decimal,
        filled_at: datetime | None = None,
        fee_rate: Decimal | None = None,
        fee_asset: str | None = None,
        con=None,
    ) -> PaperFillResult:
        self._ensure_healthy()
        if not isinstance(fill_id, str) or not _FILL_ID_RE.fullmatch(fill_id):
            raise PaperFillValidationError("fill_id must be non-empty and paper-safe")

        order = self.get(client_order_id, con=con)
        if order is None:
            raise KeyError(f"Unknown paper order: {client_order_id}")
        if symbol != order.intent.symbol:
            raise PaperFillSymbolMismatch(
                f"Fill symbol {symbol!r} does not match order symbol {order.intent.symbol!r}"
            )

        try:
            price = _decimal(market_price, "market_price")
            fill_quantity = _decimal(quantity, "fill_quantity")
        except OrderIntentValidationError as exc:
            raise PaperFillValidationError(str(exc)) from exc
        if filled_at is None:
            filled_at = self._now()
        elif not isinstance(filled_at, datetime) or filled_at.tzinfo is None:
            raise PaperFillValidationError("filled_at must be timezone-aware")

        existing_fill = get_fill(self.db_path, fill_id, con=con)
        if existing_fill is not None:
            same_event = (
                existing_fill["order_id"] == client_order_id
                and existing_fill["symbol"] == symbol
                and existing_fill["side"] == order.intent.side
                and Decimal(existing_fill["price"]) == price
                and Decimal(existing_fill["quantity"]) == fill_quantity
            )
            if not same_event:
                raise FillIdentityMismatch(
                    f"Fill identity {fill_id!r} was already used with different semantics"
                )
            existing_fill_obj = PaperFill(
                fill_id=existing_fill["trade_id"],
                client_order_id=existing_fill["order_id"],
                symbol=existing_fill["symbol"],
                side=existing_fill["side"],
                price=Decimal(existing_fill["price"]),
                quantity=Decimal(existing_fill["quantity"]),
                executed_qty=Decimal(existing_fill["executed_qty"]),
                remaining_qty=Decimal(existing_fill["remaining_qty"]),
                state=OrderState(existing_fill["resulting_state"]),
                filled_at=datetime.fromisoformat(existing_fill["event_time"]),
            )
            return PaperFillResult(
                order=order,
                fill=existing_fill_obj,
                applied=False,
                idempotent=True,
            )

        if order.state not in {OrderState.OPEN, OrderState.PARTIALLY_FILLED}:
            raise PaperFillStateError(
                f"Order state {order.state.value} cannot accept a paper fill"
            )

        side = order.intent.side
        if (side == "BUY" and price > order.intent.price) or (
            side == "SELL" and price < order.intent.price
        ):
            return PaperFillResult(order=order, fill=None, applied=False)

        if fill_quantity > order.remaining_qty:
            raise PaperFillValidationError(
                f"Fill quantity {fill_quantity} exceeds remaining quantity {order.remaining_qty}"
            )

        executed_qty = order.executed_qty + fill_quantity
        remaining_qty = order.intent.quantity - executed_qty
        target_state = (
            OrderState.FILLED if remaining_qty == 0 else OrderState.PARTIALLY_FILLED
        )
        filled_order = replace(
            order,
            state=target_state,
            updated_at=filled_at,
            executed_qty=executed_qty,
        )
        fill = PaperFill(
            fill_id=fill_id,
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            price=price,
            quantity=fill_quantity,
            executed_qty=executed_qty,
            remaining_qty=remaining_qty,
            state=target_state,
            filled_at=filled_at,
        )

        accounting_update = None
        if self.accounting is not None:
            state = get_paper_account_state(self.db_path, con=con)
            reservation = get_paper_reservation(
                self.db_path, client_order_id, con=con
            )
            if state is None or reservation is None:
                raise RuntimeError("Paper accounting state or reservation is missing")
            state_obj = PaperOrderEngine._state_from_dict(state)
            reservation_obj = PaperOrderEngine._reservation_from_dict(reservation)
            accounting_update = self.accounting.prepare_fill_accounting(
                state_obj,
                reservation_obj,
                order,
                filled_order,
                fill,
                fee_rate=fee_rate,
                fee_asset=fee_asset,
            )
        applied = save_paper_fill(
            self.db_path, order, filled_order, fill, accounting_update, con=con
        )
        if not applied:
            existing = get_fill(self.db_path, fill_id, con=con)
            if existing is None:
                raise FillIdentityMismatch(
                    f"Fill identity {fill_id!r} exists but its event cannot be read"
                )
            existing_fill = PaperFill(
                fill_id=existing["trade_id"],
                client_order_id=existing["order_id"],
                symbol=existing["symbol"],
                side=existing["side"],
                price=Decimal(existing["price"]),
                quantity=Decimal(existing["quantity"]),
                executed_qty=Decimal(existing["executed_qty"]),
                remaining_qty=Decimal(existing["remaining_qty"]),
                state=OrderState(existing["resulting_state"]),
                filled_at=datetime.fromisoformat(existing["event_time"]),
            )
            return PaperFillResult(
                order=self.get(client_order_id, con=con) or filled_order,
                fill=existing_fill,
                applied=False,
                idempotent=True,
            )

        return PaperFillResult(
            order=filled_order,
            fill=fill,
            applied=True,
        )

    @staticmethod
    def _state_from_dict(state):
        from paper_accounting import PaperAccountState

        return PaperAccountState(
            base_asset=state["base_asset"],
            quote_asset=state["quote_asset"],
            base_free=state["base_free"],
            base_reserved=state["base_reserved"],
            quote_free=state["quote_free"],
            quote_reserved=state["quote_reserved"],
            average_cost=state["average_cost"],
            realized_pnl=state["realized_pnl"],
            total_fees=state["total_fees"],
            updated_at=state["updated_at"],
        )

    @staticmethod
    def _reservation_from_dict(reservation):
        from paper_accounting import PaperReservation

        return PaperReservation(
            client_order_id=reservation["client_order_id"],
            side=reservation["side"],
            asset=reservation["asset"],
            original_amount=reservation["original_amount"],
            remaining_amount=reservation["remaining_amount"],
            created_at=reservation["created_at"],
            updated_at=reservation["updated_at"],
        )

    def transition(
        self, client_order_id: str, target: OrderState, con=None,
    ) -> PaperOrder:
        self._ensure_healthy()
        order = self.get(client_order_id, con=con)
        if order is None:
            raise KeyError(f"Unknown paper order: {client_order_id}")
        updated = transition_order(order, target, self._now())
        accounting_update = None
        if self.accounting is not None and target in {
            OrderState.CANCELED,
            OrderState.REJECTED,
        }:
            reservation = get_paper_reservation(self.db_path, client_order_id, con=con)
            if reservation is not None:
                state = get_paper_account_state(self.db_path, con=con)
                if state is None:
                    raise RuntimeError("Paper account state is missing")
                state_obj = PaperOrderEngine._state_from_dict(state)
                reservation_obj = PaperOrderEngine._reservation_from_dict(reservation)
                accounting_update = self.accounting.prepare_release(
                    state_obj,
                    reservation_obj,
                    updated,
                    target,
                    self._now(),
                )
        save_order(
            self.db_path,
            updated,
            accounting_update,
            expected_order=order,
            con=con,
        )
        return updated
