from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import re
from typing import Callable

from risk_engine import RiskDecision
from storage import get_order, init_db, save_order


_CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,36}$")
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
    OrderState.OPEN: frozenset({OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELED}),
    OrderState.PARTIALLY_FILLED: frozenset({OrderState.FILLED, OrderState.CANCELED}),
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


def make_client_order_id(prefix: str, symbol: str, grid_index: int, side: str) -> str:
    """Build a stable Binance-safe grid identity without randomness."""
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9]+", prefix):
        raise OrderIntentValidationError("client-order prefix must be ASCII alphanumeric")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]+", symbol):
        raise OrderIntentValidationError("symbol must be uppercase ASCII alphanumeric")
    if not isinstance(grid_index, int) or isinstance(grid_index, bool) or grid_index < 0:
        raise OrderIntentValidationError("grid_index must be a non-negative integer")
    if side not in {"BUY", "SELL"}:
        raise OrderIntentValidationError("side must be BUY or SELL")
    result = f"{prefix}-{symbol}-{grid_index:05d}-{side[0]}"
    if not _CLIENT_ORDER_ID_RE.fullmatch(result):
        raise OrderIntentValidationError("generated client_order_id is not Binance-safe")
    return result


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
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise OrderIntentValidationError("created_at must be timezone-aware")
        object.__setattr__(self, "price", _decimal(self.price, "price"))
        object.__setattr__(self, "quantity", _decimal(self.quantity, "quantity"))


@dataclass(frozen=True)
class PaperOrder:
    intent: OrderIntent
    state: OrderState
    updated_at: datetime


def transition_order(order: PaperOrder, target: OrderState, updated_at: datetime) -> PaperOrder:
    if not isinstance(target, OrderState):
        raise InvalidOrderStateTransition(f"Unknown target state: {target!r}")
    if target not in _ALLOWED_TRANSITIONS[order.state]:
        raise InvalidOrderStateTransition(f"{order.state.value} -> {target.value} is not allowed")
    if not isinstance(updated_at, datetime) or updated_at.tzinfo is None:
        raise InvalidOrderStateTransition("updated_at must be timezone-aware")
    return replace(order, state=target, updated_at=updated_at)


class PaperOrderEngine:
    """Deterministic local-only execution state machine; it has no Binance client."""

    def __init__(
        self,
        db_path: str,
        clock: Callable[[], datetime] | None = None,
        client_order_prefix: str = "AG",
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
        init_db(db_path)

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("paper-order clock must return a timezone-aware datetime")
        return now

    def get(self, client_order_id: str) -> PaperOrder | None:
        row = get_order(self.db_path, client_order_id)
        if row is None:
            return None
        intent = OrderIntent(
            client_order_id=row["client_order_id"], symbol=row["symbol"], side=row["side"],
            order_type=row["order_type"], price=Decimal(row["price"]),
            quantity=Decimal(row["quantity"]), time_in_force=row["time_in_force"],
            grid_index=int(row["grid_index"]), created_at=datetime.fromisoformat(row["created_at"]),
        )
        return PaperOrder(intent, OrderState(row["status"]), datetime.fromisoformat(row["updated_at"]))

    def submit(
        self,
        intent: OrderIntent,
        risk_decision: RiskDecision,
        lower_price: Decimal,
        effective_upper: Decimal,
    ) -> PaperOrder:
        if not isinstance(intent, OrderIntent):
            raise OrderIntentValidationError("A valid OrderIntent is required")
        expected_client_order_id = make_client_order_id(
            self.client_order_prefix, intent.symbol, intent.grid_index, intent.side
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
        order = PaperOrder(intent, OrderState.PLANNED, self._now())
        save_order(self.db_path, order)
        order = transition_order(order, OrderState.SUBMITTED, self._now())
        save_order(self.db_path, order)
        order = transition_order(order, OrderState.OPEN, self._now())
        save_order(self.db_path, order)
        return order

    def transition(self, client_order_id: str, target: OrderState) -> PaperOrder:
        order = self.get(client_order_id)
        if order is None:
            raise KeyError(f"Unknown paper order: {client_order_id}")
        updated = transition_order(order, target, self._now())
        save_order(self.db_path, updated)
        return updated
