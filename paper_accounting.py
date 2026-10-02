from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


class PaperAccountingError(RuntimeError):
    """Raised when paper accounting cannot be safely applied."""


class PaperAccountingValidationError(PaperAccountingError, ValueError):
    """Raised when paper accounting inputs are invalid."""


class InsufficientPaperFunds(PaperAccountingError):
    """Raised when a paper order or fill exceeds available paper funds."""


class UnsupportedFeeAsset(PaperAccountingError):
    """Raised when a fee asset has no explicit paper-accounting path."""


def _decimal(value: Any, field: str, *, allow_zero: bool = True) -> Decimal:
    if value is None or isinstance(value, bool):
        raise PaperAccountingValidationError(f"{field} must be a finite Decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PaperAccountingValidationError(f"{field} must be a finite Decimal") from exc
    if not result.is_finite() or (result < 0 if allow_zero else result <= 0):
        raise PaperAccountingValidationError(f"{field} must be a finite non-negative Decimal")
    return result


@dataclass(frozen=True)
class PaperAccountState:
    base_asset: str
    quote_asset: str
    base_free: Decimal
    base_reserved: Decimal
    quote_free: Decimal
    quote_reserved: Decimal
    average_cost: Decimal
    realized_pnl: Decimal
    total_fees: Decimal
    updated_at: datetime

    def equity(self, mark_price: Decimal) -> Decimal:
        price = _decimal(mark_price, "mark_price", allow_zero=False)
        return (
            self.quote_free
            + self.quote_reserved
            + (self.base_free + self.base_reserved) * price
        )


@dataclass(frozen=True)
class PaperReservation:
    client_order_id: str
    side: str
    asset: str
    original_amount: Decimal
    remaining_amount: Decimal
    created_at: datetime
    updated_at: datetime

    @property
    def consumed_amount(self) -> Decimal:
        return self.original_amount - self.remaining_amount


@dataclass(frozen=True)
class PaperAccountingUpdate:
    old_state: PaperAccountState
    new_state: PaperAccountState
    old_reservation: PaperReservation | None
    reservation: PaperReservation | None
    event_id: str
    event_type: str
    client_order_id: str
    payload: dict[str, Any]


class PaperAccountingEngine:
    """Deterministic local paper accounting for already-validated paper orders."""

    def __init__(
        self,
        base_asset: str,
        quote_asset: str,
        initial_base_balance: Decimal,
        initial_quote_balance: Decimal,
        maker_fee: Decimal,
        taker_fee: Decimal,
        fee_asset: str,
    ):
        self.base_asset = str(base_asset).upper()
        self.quote_asset = str(quote_asset).upper()
        self.fee_asset = str(fee_asset).upper()
        if not self.base_asset or not self.quote_asset:
            raise PaperAccountingValidationError("base_asset and quote_asset are required")
        if self.base_asset == self.quote_asset:
            raise PaperAccountingValidationError("base_asset and quote_asset must differ")
        if self.fee_asset not in {self.base_asset, self.quote_asset}:
            raise UnsupportedFeeAsset(
                f"Unsupported paper fee asset {self.fee_asset!r}; "
                f"supported assets are {self.base_asset} and {self.quote_asset}"
            )

        self.initial_base_balance = _decimal(
            initial_base_balance, "initial_base_balance"
        )
        self.initial_quote_balance = _decimal(
            initial_quote_balance, "initial_quote_balance"
        )
        self.maker_fee = _decimal(maker_fee, "maker_fee")
        self.taker_fee = _decimal(taker_fee, "taker_fee")
        self._initial_state = PaperAccountState(
            base_asset=self.base_asset,
            quote_asset=self.quote_asset,
            base_free=self.initial_base_balance,
            base_reserved=Decimal("0"),
            quote_free=self.initial_quote_balance,
            quote_reserved=Decimal("0"),
            average_cost=Decimal("0"),
            realized_pnl=Decimal("0"),
            total_fees=Decimal("0"),
            updated_at=datetime.now(timezone.utc),
        )

    def initial_state(self) -> PaperAccountState:
        return self._initial_state

    def fee_rate_for_order(self, order_type: str) -> Decimal:
        normalized = str(order_type).upper()
        if normalized == "LIMIT_MAKER":
            return self.maker_fee
        if normalized == "LIMIT":
            return self.taker_fee
        raise PaperAccountingValidationError(f"Unsupported paper order type: {order_type!r}")

    def prepare_reservation(
        self, state: PaperAccountState, order, now: datetime
    ) -> PaperAccountingUpdate:
        if order.intent.side == "BUY":
            asset = self.quote_asset
            amount = order.intent.price * order.intent.quantity
            free_before = state.quote_free
            reserved_before = state.quote_reserved
            free_after = free_before - amount
            reserved_after = reserved_before + amount
            if free_after < 0:
                raise InsufficientPaperFunds(
                    f"Insufficient paper {self.quote_asset} for BUY reservation"
                )
            new_state = replace(
                state,
                quote_free=free_after,
                quote_reserved=reserved_after,
                updated_at=now,
            )
        elif order.intent.side == "SELL":
            asset = self.base_asset
            amount = order.intent.quantity
            free_before = state.base_free
            reserved_before = state.base_reserved
            free_after = free_before - amount
            reserved_after = reserved_before + amount
            if free_after < 0:
                raise InsufficientPaperFunds(
                    f"Insufficient paper {self.base_asset} for SELL reservation"
                )
            new_state = replace(
                state,
                base_free=free_after,
                base_reserved=reserved_after,
                updated_at=now,
            )
        else:
            raise PaperAccountingValidationError(
                f"Unsupported paper order side: {order.intent.side!r}"
            )

        reservation = PaperReservation(
            client_order_id=order.intent.client_order_id,
            side=order.intent.side,
            asset=asset,
            original_amount=amount,
            remaining_amount=amount,
            created_at=now,
            updated_at=now,
        )
        payload = {
            "side": order.intent.side,
            "asset": asset,
            "amount": str(amount),
        }
        return PaperAccountingUpdate(
            old_state=state,
            new_state=new_state,
            old_reservation=None,
            reservation=reservation,
            event_id=f"reserve:{order.intent.client_order_id}",
            event_type="RESERVE",
            client_order_id=order.intent.client_order_id,
            payload=payload,
        )

    def prepare_fill_accounting(
        self,
        state: PaperAccountState,
        reservation: PaperReservation,
        order_before,
        order_after,
        fill,
        fee_rate: Decimal | None = None,
        fee_asset: str | None = None,
    ) -> PaperAccountingUpdate:
        selected_fee_rate = (
            self.fee_rate_for_order(order_before.intent.order_type)
            if fee_rate is None
            else _decimal(fee_rate, "fee_rate")
        )
        selected_fee_asset = (
            self.fee_asset if fee_asset is None else str(fee_asset).upper()
        )
        if selected_fee_asset not in {self.base_asset, self.quote_asset}:
            raise UnsupportedFeeAsset(
                f"Unsupported paper fee asset {selected_fee_asset!r}"
            )

        gross_quote = fill.price * fill.quantity
        if selected_fee_asset == self.quote_asset:
            fee_amount = gross_quote * selected_fee_rate
            fee_quote_value = fee_amount
            base_fee_amount = Decimal("0")
        else:
            base_fee_amount = fill.quantity * selected_fee_rate
            fee_amount = base_fee_amount
            fee_quote_value = base_fee_amount * fill.price

        if order_before.intent.side == "BUY":
            base_increase = (
                fill.quantity
                if selected_fee_asset == self.quote_asset
                else fill.quantity - base_fee_amount
            )
            if base_increase <= 0:
                raise PaperAccountingValidationError(
                    "Base-asset fee consumes the entire BUY fill quantity"
                )

            old_total_base = state.base_free + state.base_reserved
            inventory_cost = gross_quote
            if selected_fee_asset == self.quote_asset:
                inventory_cost += fee_amount
            new_total_base = old_total_base + base_increase
            if new_total_base <= 0:
                raise PaperAccountingValidationError("BUY fill produces no base inventory")
            new_average_cost = (
                state.average_cost * old_total_base + inventory_cost
            ) / new_total_base

            quote_reserved_after = state.quote_reserved - gross_quote
            if quote_reserved_after < 0:
                raise InsufficientPaperFunds(
                    "BUY fill consumes more quote reservation than exists"
                )
            quote_free_after = state.quote_free
            if selected_fee_asset == self.quote_asset:
                quote_free_after -= fee_amount
                if quote_free_after < 0:
                    raise InsufficientPaperFunds(
                        f"Insufficient free paper {self.quote_asset} to pay BUY fee"
                    )
            base_free_after = state.base_free + base_increase

            remaining_reservation = reservation.remaining_amount - gross_quote
            if remaining_reservation < 0:
                raise InsufficientPaperFunds(
                    "BUY fill exceeds the original quote reservation"
                )
            # PATCH 5A (reservation settlement): a terminal BUY fill must not
            # leave a live per-order reservation behind (recovery invariant
            # RESERVATION_TERMINAL_NONZERO).  The quote that was reserved at
            # the limit price but not spent at the (better) fill price is a
            # surplus credit; release it back to the free quote balance so a
            # legitimately completed order settles its reservation to zero and
            # the post-cycle state reconciles healthy.  Partial fills keep
            # their residual (their order is still active).
            if order_after.state.value == "FILLED" and remaining_reservation > 0:
                quote_free_after += remaining_reservation
                quote_reserved_after -= remaining_reservation
                if quote_reserved_after < 0:
                    raise InsufficientPaperFunds(
                        "BUY fill settlement exceeds reserved quote"
                    )
                remaining_reservation = Decimal("0")
            new_state = replace(
                state,
                base_free=base_free_after,
                quote_free=quote_free_after,
                quote_reserved=quote_reserved_after,
                average_cost=new_average_cost,
                total_fees=state.total_fees + fee_quote_value,
                updated_at=fill.filled_at,
            )
        elif order_before.intent.side == "SELL":
            base_reserved_after = state.base_reserved - fill.quantity
            if base_reserved_after < 0:
                raise InsufficientPaperFunds(
                    "SELL fill consumes more base reservation than exists"
                )

            base_free_after = state.base_free
            if selected_fee_asset == self.base_asset:
                base_free_after -= base_fee_amount
                if base_free_after < 0:
                    raise InsufficientPaperFunds(
                        f"Insufficient free paper {self.base_asset} to pay SELL fee"
                    )

            quote_free_after = state.quote_free + (
                gross_quote - fee_amount
                if selected_fee_asset == self.quote_asset
                else gross_quote
            )
            economic_proceeds = gross_quote - (
                fee_amount
                if selected_fee_asset == self.quote_asset
                else Decimal("0")
            )
            cost_basis = (
                fill.quantity + base_fee_amount
                if selected_fee_asset == self.base_asset
                else fill.quantity
            ) * state.average_cost
            realized_pnl_delta = economic_proceeds - cost_basis

            new_state = replace(
                state,
                base_free=base_free_after,
                base_reserved=base_reserved_after,
                quote_free=quote_free_after,
                realized_pnl=state.realized_pnl + realized_pnl_delta,
                total_fees=state.total_fees + fee_quote_value,
                updated_at=fill.filled_at,
            )
            remaining_reservation = reservation.remaining_amount - fill.quantity
            if remaining_reservation < 0:
                raise InsufficientPaperFunds(
                    "SELL fill exceeds the original base reservation"
                )
        else:
            raise PaperAccountingValidationError(
                f"Unsupported paper order side: {order_before.intent.side!r}"
            )

        new_reservation = replace(
            reservation,
            remaining_amount=remaining_reservation,
            updated_at=fill.filled_at,
        )
        payload = {
            "side": order_before.intent.side,
            "fill_price": str(fill.price),
            "fill_quantity": str(fill.quantity),
            "fee_asset": selected_fee_asset,
            "fee_amount": str(fee_amount),
            "fee_quote_value": str(fee_quote_value),
            "resulting_state": order_after.state.value,
        }
        return PaperAccountingUpdate(
            old_state=state,
            new_state=new_state,
            old_reservation=reservation,
            reservation=new_reservation,
            event_id=f"fill:{fill.fill_id}",
            event_type="FILL",
            client_order_id=order_before.intent.client_order_id,
            payload=payload,
        )

    def prepare_release(
        self,
        state: PaperAccountState,
        reservation: PaperReservation,
        order,
        target_state,
        now: datetime,
    ) -> PaperAccountingUpdate:
        release_amount = reservation.remaining_amount
        if release_amount < 0:
            raise PaperAccountingValidationError("Reservation remaining amount is negative")

        if reservation.side == "BUY":
            new_state = replace(
                state,
                quote_reserved=state.quote_reserved - release_amount,
                quote_free=state.quote_free + release_amount,
                updated_at=now,
            )
        elif reservation.side == "SELL":
            new_state = replace(
                state,
                base_reserved=state.base_reserved - release_amount,
                base_free=state.base_free + release_amount,
                updated_at=now,
            )
        else:
            raise PaperAccountingValidationError(
                f"Unsupported reservation side: {reservation.side!r}"
            )

        if new_state.base_free < 0 or new_state.base_reserved < 0:
            raise PaperAccountingValidationError("Paper base balance became negative")
        if new_state.quote_free < 0 or new_state.quote_reserved < 0:
            raise PaperAccountingValidationError("Paper quote balance became negative")

        new_reservation = replace(
            reservation,
            remaining_amount=Decimal("0"),
            updated_at=now,
        )
        payload = {
            "side": reservation.side,
            "asset": reservation.asset,
            "released_amount": str(release_amount),
            "target_state": target_state.value,
        }
        return PaperAccountingUpdate(
            old_state=state,
            new_state=new_state,
            old_reservation=reservation,
            reservation=new_reservation,
            event_id=f"release:{order.intent.client_order_id}:{target_state.value}",
            event_type="RELEASE",
            client_order_id=order.intent.client_order_id,
            payload=payload,
        )
