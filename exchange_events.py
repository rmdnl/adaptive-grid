"""Roadmap E: deterministic, paper-only exchange-event handling.

This module is the exchange-side event / reconciliation layer.  It is
deliberately **not** a live implementation: it defines the deterministic
interfaces and state machine that a future Binance user-data stream would
drive, and it maps exchange events onto the existing ``PaperOrderEngine``
and ``CancelController``.  It never places or cancels orders on an
exchange, never touches a live client, and never releases the kill latch.

Determinism and safety properties (all covered by ``tests/test_roadmap_e.py``):

* **Duplicate events** — an event_id already recorded is an idempotent
  no-op; the same exchange fill can be replayed any number of times.
* **Out-of-order events** — a sequence below the stream's watermark is
  recorded as OUT_OF_ORDER and NOT applied (fail-closed: we do not know the
  intermediate state), and it flags that reconciliation is required.
* **Sequence gaps** — a sequence beyond the watermark is recorded as
  SEQUENCE_GAP and NOT applied; it flags reconciliation.  The watermark is
  a monotonic high-water mark (never moved backwards).
* **Partial / full fills** — mapped to ``PaperOrderEngine.apply_fill``;
  a fill that exceeds the remaining quantity is rejected by the engine
  (existing invariant).
* **Cancel confirmation / already-cancelled** — mapped to the
  ``CancelController`` reconcile path; an already-cancelled order is a
  no-op.
* **Unknown order state / unknown order** — recorded and NOT applied; a
  fill on an order that does not exist locally is flagged, never invented.
* **Stale local state** — ``reconcile()`` replaces local order state from an
  authoritative exchange snapshot (injected) and resets the watermark, so a
  reconnect rebuilds state deterministically.
* **Restart recovery** — the watermark and the recorded events persist in
  SQLite (``exchange_sequence`` / ``exchange_events``); a fresh process
  resumes from the persisted watermark and re-applies nothing already seen.
* **Convergence** — repeated application of the same event set, and repeated
  reconciliation from the same snapshot, produce identical local state.
* **Kill-state interaction** — the applier never places orders (structural:
  it has no submit path) and never releases the kill latch; while the kill
  is active it only records cancels/fills so the operator release path sees
  an accurate pending set.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Callable, Optional

from cancel_controller import CancelController
from order_engine import (
    OrderState,
    PaperOrderEngine,
)
from storage import (
    advance_exchange_sequence,
    get_exchange_event,
    get_exchange_sequence,
    has_exchange_sequence,
    record_exchange_event,
    set_exchange_sequence,
)


class ExchangeEventType(str, Enum):
    """Exchange-side event kinds this deterministic layer understands.

    Only read-side / outcome-side events.  There is deliberately no event
    type that places or modifies an order: the applier cannot create exposure.
    """

    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    FILL = "FILL"                 # one fill (partial or full)
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"           # unfillable cancel; treated as terminal
    UNKNOWN = "UNKNOWN"


class IgnoreReason(str, Enum):
    """Why an event was recorded but NOT applied to local state."""

    DUPLICATE = "DUPLICATE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    UNKNOWN_ORDER = "UNKNOWN_ORDER"
    STALE_LOCAL_STATE = "STALE_LOCAL_STATE"
    KILL_LATCH_ACTIVE = "KILL_LATCH_ACTIVE"
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    ALREADY_APPLIED = "ALREADY_APPLIED"


@dataclass(frozen=True)
class ExchangeEvent:
    """One exchange-side event, as a user-data stream would deliver it.

    ``seq`` is the stream's monotonic sequence number (per scope).  ``raw``
    carries the deterministic payload (fill qty, price, cancel reason, ...).
    """

    event_id: str
    event_type: ExchangeEventType
    client_order_id: str
    exchange_order_id: Optional[str]
    seq: int
    raw: dict = field(default_factory=dict)

    def fill_quantity(self) -> Decimal:
        return _dec(self.raw.get("quantity"))

    def fill_price(self) -> Decimal:
        return _dec(self.raw.get("price"))

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "client_order_id": self.client_order_id,
            "exchange_order_id": self.exchange_order_id,
            "seq": self.seq,
            "raw": self.raw,
        }


def _dec(value) -> Decimal:
    if value is None:
        raise ValueError("required decimal value is missing")
    if isinstance(value, Decimal):
        parsed = value
    else:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"invalid decimal value: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError("decimal value must be finite")
    return parsed


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of applying one event."""

    event_id: str
    event_type: str
    applied: bool
    ignore_reason: Optional[str]
    local_state_after: Optional[str]
    reconciliation_required: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "applied": self.applied,
            "ignore_reason": self.ignore_reason,
            "local_state_after": self.local_state_after,
            "reconciliation_required": self.reconciliation_required,
            "detail": self.detail,
        }


class ReconciliationRequired(RuntimeError):
    """Raised when the applier cannot safely continue without a REST
    reconciliation (sequence gap, out-of-order, unknown order, stale state)."""


class ExchangeEventValidationError(ValueError):
    """Raised when an event is malformed and cannot be safely recorded."""


class RestReconciler:
    """Abstract, read-only exchange-snapshot reconciliation seam.

    A concrete live implementation would pull REST order/fill snapshots from
    Binance (testnet or live) and hand them back as plain data.  That
    implementation is intentionally NOT present in this repository (no live
    path).  Tests inject an in-memory deterministic provider.  The applier
    only *reads* the snapshot; it never mutates exchange state.
    """

    def snapshot(self, symbol: str) -> dict:
        """Return an authoritative local-order snapshot from the exchange.

        Deterministic shape (all Decimal/string, no wall-clock):
        ``{"orders": [{"client_order_id", "state", "executed_qty",
        "quantity"}, ...]}`` where ``state`` is one of the exchange-side
        states (``OPEN``, ``PARTIALLY_FILLED``, ``FILLED``, ``CANCELED``,
        ``REJECTED``).
        """
        raise NotImplementedError


class ExchangeEventApplier:
    """Deterministic, paper-only applier mapping exchange events onto the
    existing ``PaperOrderEngine`` and ``CancelController``.

    The applier owns a per-scope sequence watermark and a persisted event
    log so restarts resume cleanly.  It has NO order-placement path: it can
    only record fills, cancels, and rejections that the engine already
    models.
    """

    def __init__(
        self,
        db_path: str,
        engine: PaperOrderEngine,
        controller: CancelController,
        scope: str = "default",
        rest_reconciler: Optional[RestReconciler] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self.db_path = db_path
        self._engine = engine
        self._controller = controller
        self.scope = scope
        self._rest = rest_reconciler
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- watermark ---------------------------------------------------------
    @property
    def watermark(self) -> int:
        return get_exchange_sequence(self.db_path, self.scope)

    def _advance_watermark(self, seq: int) -> None:
        # Monotonic high-water mark; never moves backwards.
        advance_exchange_sequence(self.db_path, self.scope, seq)

    def _record(
        self,
        event: ExchangeEvent,
        applied: bool,
        ignore_reason: Optional[IgnoreReason] = None,
        detail: str = "",
    ) -> None:
        record_exchange_event(
            self.db_path,
            event.event_id,
            event.event_type.value,
            event.client_order_id,
            event.raw,
            event.seq,
            applied=applied,
            ignored_reason=ignore_reason.value if ignore_reason else None,
            exchange_order_id=event.exchange_order_id,
            ts=self._clock().isoformat(),
        )

    # -- apply ------------------------------------------------------------
    def apply(self, event: ExchangeEvent) -> ApplyResult:
        """Apply one exchange event deterministically.  Fail-closed.

        Order of checks:
        1. malformed payload (non-finite quantity/price on a FILL) is a hard
           error — never partially apply a corrupt event.
        2. duplicate event_id (already recorded) is an idempotent no-op.
        3. sequence watermark: below -> OUT_OF_ORDER; above -> SEQUENCE_GAP
           (both require reconciliation); equal -> apply.
        4. unknown local order (event references an order we never had) is
           recorded and NOT applied; it requires reconciliation.
        5. the effect is applied to the engine / controller and the
           watermark advances only on a successfully applied, in-order event.
        """
        # 1. Validate the payload up front (a FILL with a bad quantity must
        #    never be applied or advanced).
        if event.event_type is ExchangeEventType.FILL:
            try:
                event.fill_quantity()
                event.fill_price()
            except ValueError as exc:
                self._record(event, False, IgnoreReason.INVALID_PAYLOAD, str(exc))
                raise ExchangeEventValidationError(str(exc))

        # 2. Duplicate: the event was already recorded (any outcome).  Return
        #    the stored outcome; do not re-mutate state and do not re-advance
        #    the watermark.
        existing = get_exchange_event(self.db_path, event.event_id)
        if existing is not None:
            stored_applied = bool(existing.get("applied"))
            stored_reason = existing.get("ignored_reason")
            return ApplyResult(
                event.event_id,
                event.event_type.value,
                applied=stored_applied,
                ignore_reason=stored_reason,
                local_state_after=None,
                reconciliation_required=not stored_applied,
                detail="duplicate event: idempotent no-op",
            )

        wm = self.watermark
        has_baseline = has_exchange_sequence(self.db_path, self.scope)

        # 3. Sequence watermark — fail-closed contiguity model:
        #    * No baseline yet: the first observed event ESTABLISHES the
        #      stream position (recorded unconditionally; its effect is still
        #      validated below, and a rejected effect is flagged for
        #      reconciliation).
        #    * With a baseline: in-order means seq == watermark + 1.
        #        seq <= watermark      -> OUT_OF_ORDER (replayed slot)
        #        seq >  watermark + 1  -> SEQUENCE_GAP (missed events)
        #      Neither is applied and both require REST reconciliation.
        in_order = True
        if not has_baseline:
            self._advance_watermark(event.seq)  # establish the baseline row
        elif event.seq <= wm:
            self._record(event, False, IgnoreReason.OUT_OF_ORDER,
                         f"seq {event.seq} <= watermark {wm}")
            return ApplyResult(
                event.event_id, event.event_type.value, False,
                IgnoreReason.OUT_OF_ORDER.value, None, True,
                detail="out-of-order/replayed event: reconciliation required",
            )
        elif event.seq > wm + 1:
            self._record(event, False, IgnoreReason.SEQUENCE_GAP,
                         f"seq {event.seq} > watermark+1 {wm + 1}")
            return ApplyResult(
                event.event_id, event.event_type.value, False,
                IgnoreReason.SEQUENCE_GAP.value, None, True,
                detail="sequence gap: reconciliation required",
            )

        # 4. Unknown local order (SUBMITTED/OPEN/UNKNOWN are informational;
        #    FILL/CANCELED/REJECTED/EXPIRED mutate local state and require the
        #    order to exist locally).
        order = self._engine.get(event.client_order_id)
        mutating = event.event_type in {
            ExchangeEventType.FILL, ExchangeEventType.CANCELED,
            ExchangeEventType.REJECTED, ExchangeEventType.EXPIRED,
        }
        if mutating and order is None:
            self._record(event, False, IgnoreReason.UNKNOWN_ORDER,
                         f"local order {event.client_order_id} not found")
            return ApplyResult(
                event.event_id, event.event_type.value, False,
                IgnoreReason.UNKNOWN_ORDER.value, None, True,
                detail="unknown local order: reconciliation required",
            )

        # 5. Apply the effect.  On an in-order event the watermark advances
        #    ONLY when the effect was actually applied: a failed apply keeps
        #    the mark so the next event shows a GAP and reconciliation is
        #    required (a silent advance would hide the missed state change).
        result = self._apply_effect(event, order)
        if in_order and result.applied:
            self._advance_watermark(event.seq)
        return result

    def _apply_effect(self, event: ExchangeEvent, order) -> ApplyResult:
        t = event.event_type
        # Informational / already-local outcomes: no local mutation.
        if t in {ExchangeEventType.SUBMITTED, ExchangeEventType.OPEN}:
            self._record(event, True, None, "informational open/submitted")
            return ApplyResult(
                event.event_id, t.value, True, None,
                order.state.value if order else None, False,
                detail="informational; no local mutation",
            )
        if t is ExchangeEventType.UNKNOWN:
            # An event we cannot classify is NOT applied; it needs the REST
            # snapshot to resolve.  Recorded, reconciliation required.
            self._record(event, False, IgnoreReason.STALE_LOCAL_STATE,
                         "unclassifiable exchange event")
            return ApplyResult(
                event.event_id, t.value, False,
                IgnoreReason.STALE_LOCAL_STATE.value, None, True,
                detail="unknown event kind: reconciliation required",
            )

        if t is ExchangeEventType.FILL:
            return self._apply_fill(event, order)
        if t is ExchangeEventType.CANCELED:
            return self._apply_cancel(event, order, ExchangeEventType.CANCELED)
        if t is ExchangeEventType.REJECTED:
            return self._apply_cancel(event, order, ExchangeEventType.REJECTED)
        if t is ExchangeEventType.EXPIRED:
            # Expired-but-unfilled is terminal like a cancel (no fills).
            return self._apply_cancel(event, order, ExchangeEventType.EXPIRED)

        self._record(event, False, IgnoreReason.STALE_LOCAL_STATE,
                     f"unsupported event type {t.value}")
        return ApplyResult(
            event.event_id, t.value, False,
            IgnoreReason.STALE_LOCAL_STATE.value, None, True,
            detail="unsupported event type",
        )

    def _apply_fill(self, event: ExchangeEvent, order) -> ApplyResult:
        # A fill on an already-terminal order:
        #   * FILLED -> the fill already happened; a repeated fill event is a
        #     clean idempotent no-op (applied=True, no reconciliation needed).
        #   * CANCELED / REJECTED -> the order left the book; a late fill is a
        #     contradiction that REST reconciliation must settle.
        if order.state is OrderState.FILLED:
            self._record(event, True, None,
                         "fill on FILLED order: idempotent no-op")
            return ApplyResult(
                event.event_id, ExchangeEventType.FILL.value, True, None,
                order.state.value, False,
                detail="already filled: idempotent no-op",
            )
        if order.state in {OrderState.CANCELED, OrderState.REJECTED}:
            self._record(event, False, IgnoreReason.ALREADY_APPLIED,
                         f"fill on terminal state {order.state.value}")
            return ApplyResult(
                event.event_id, ExchangeEventType.FILL.value, False,
                IgnoreReason.ALREADY_APPLIED.value, order.state.value, True,
                detail="fill on a canceled/rejected order: reconcile",
            )
        try:
            result = self._engine.apply_fill(
                event.client_order_id,
                fill_id=event.event_id,
                symbol=order.intent.symbol,
                market_price=event.fill_price(),
                quantity=event.fill_quantity(),
            )
        except Exception as exc:
            # A fill the engine refuses (exceeds remaining qty, bad price,
            # identity mismatch, ...) is NOT success: record it and flag
            # reconciliation so the REST snapshot settles the true state.
            self._record(event, False, IgnoreReason.STALE_LOCAL_STATE,
                         f"fill refused: {exc}")
            return ApplyResult(
                event.event_id, ExchangeEventType.FILL.value, False,
                IgnoreReason.STALE_LOCAL_STATE.value, order.state.value, True,
                detail=f"fill refused by engine: {exc}",
            )
        applied = result.applied or result.idempotent
        self._record(
            event, applied,
            None if applied else IgnoreReason.ALREADY_APPLIED,
            "fill applied" if applied else "fill not applied: reconcile",
        )
        return ApplyResult(
            event.event_id, ExchangeEventType.FILL.value, applied,
            None if applied else IgnoreReason.ALREADY_APPLIED.value,
            result.order.state.value,
            reconciliation_required=not applied,
            detail="fill" + (" (idempotent)" if result.idempotent and not result.applied else ""),
        )

    def _apply_cancel(self, event: ExchangeEvent, order, kind: ExchangeEventType) -> ApplyResult:
        # A cancel/reject/expired on an already-terminal order is a no-op
        # (already reconciled); on an open order it transitions locally and
        # releases the reservation.
        target = (
            OrderState.CANCELED if kind is ExchangeEventType.CANCELED
            else (OrderState.REJECTED if kind is ExchangeEventType.REJECTED
                  else OrderState.CANCELED)
        )
        if order.state in {OrderState.CANCELED, OrderState.REJECTED, OrderState.FILLED}:
            self._record(event, True, None,
                         f"{kind.value} on terminal state {order.state.value}")
            return ApplyResult(
                event.event_id, kind.value, True, None, order.state.value,
                False, detail="already terminal: no-op",
            )
        try:
            self._engine.transition(event.client_order_id, target)
        except Exception as exc:
            # A transition we cannot perform (e.g. a fill raced ahead, or
            # state moved) is not treated as success: record and flag
            # reconciliation so the REST snapshot settles it.
            self._record(event, False, IgnoreReason.STALE_LOCAL_STATE, str(exc))
            return ApplyResult(
                event.event_id, kind.value, False,
                IgnoreReason.STALE_LOCAL_STATE.value, order.state.value, True,
                detail=f"cancel transition failed: {exc}",
            )
        self._record(event, True, None, f"{kind.value} applied -> {target.value}")
        return ApplyResult(
            event.event_id, kind.value, True, None, target.value,
            False, detail=f"{kind.value} -> {target.value}",
        )

    # -- reconciliation ----------------------------------------------------
    def reconciliation_required(self) -> bool:
        """True when any recorded event was skipped (needs a REST snapshot)."""
        from storage import connect

        con = connect(self.db_path)
        try:
            row = con.execute(
                "SELECT COUNT(*) AS n FROM exchange_events WHERE applied = 0"
            ).fetchone()
            return int(row["n"]) > 0
        finally:
            con.close()

    def reconcile(self, symbol: str) -> dict:
        """Replace local order state from an authoritative REST snapshot.

        Deterministic convergence: given the same snapshot, reconciliation is
        idempotent — it walks every locally-known order, aligns its state to
        the exchange state (releasing reservations on terminal states via the
        controller), and resets the stream watermark so the next live event
        starts clean.  It does NOT create new local orders that only the
        exchange knows about; those are flagged for operator review.

        Requires a ``rest_reconciler`` to be configured; without one this
        raises (fail-closed: reconciliation cannot be assumed).
        """
        if self._rest is None:
            raise ReconciliationRequired(
                "No REST reconciler configured; cannot reconcile "
                "(fail-closed). Configure a read-only reconciler to run "
                "reconciliation."
            )
        snapshot = self._rest.snapshot(symbol)
        orders = snapshot.get("orders", []) if isinstance(snapshot, dict) else []

        state_map = {
            "OPEN": OrderState.OPEN,
            "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
            "FILLED": OrderState.FILLED,
            "CANCELED": OrderState.CANCELED,
            "REJECTED": OrderState.REJECTED,
            "EXPIRED": OrderState.CANCELED,
        }
        aligned: list[str] = []
        unknown_exchange_orders: list[str] = []
        reconciled_fills: list[str] = []

        for ex in orders:
            cid = ex.get("client_order_id")
            ex_state = ex.get("state")
            if cid is None or ex_state not in state_map:
                unknown_exchange_orders.append(str(cid))
                continue
            target = state_map[ex_state]
            local = self._engine.get(cid)
            if local is None:
                # Exchange knows an order we never tracked locally.  Never
                # invent local exposure: flag it for the operator.
                unknown_exchange_orders.append(cid)
                continue
            if local.state != target:
                # Align local state to the exchange target.
                if target in {OrderState.CANCELED, OrderState.REJECTED}:
                    try:
                        self._engine.transition(cid, target)
                    except Exception:
                        # A terminal alignment that cannot be transitioned
                        # (e.g. already FILLED) is not forced; the fill path
                        # owns that order.  Flag it for reconcile.
                        reconciled_fills.append(cid)
                elif target is OrderState.FILLED and local.state in {
                    OrderState.OPEN, OrderState.PARTIALLY_FILLED,
                }:
                    # A full fill is applied through the fill engine so the
                    # accounting stays authoritative.  fill_id is paper-safe
                    # (no ':' / no upper-case state token) and deterministic.
                    executed = Decimal(
                        str(ex.get("executed_qty") or local.intent.quantity)
                    )
                    ex_price = ex.get("price")
                    price = (
                        Decimal(str(ex_price))
                        if ex_price is not None
                        else local.intent.price
                    )
                    try:
                        self._engine.apply_fill(
                            cid,
                            fill_id=f"reconcile-fill-{cid}",
                            symbol=local.intent.symbol,
                            market_price=price,
                            quantity=executed,
                        )
                    except Exception as exc:
                        reconciled_fills.append(cid)
            # Convergence: after alignment, the local order MATCHES the
            # exchange target.  Reporting it in `aligned` makes repeated
            # reconciles from the same snapshot produce identical output.
            now_local = self._engine.get(cid)
            if now_local is not None and now_local.state == target:
                aligned.append(cid)

        # Re-baseline the watermark so the next live event stream starts
        # from the reconciled exchange state.  This is the ONLY non-monotonic
        # write to the watermark, reserved for an authoritative REST
        # reconciliation; ordinary event application only ever advances it.
        set_exchange_sequence(self.db_path, self.scope, 0)

        return {
            "aligned": aligned,
            "unknown_exchange_orders": unknown_exchange_orders,
            "reconciled_fills": reconciled_fills,
            "reconciliation_required_after": self.reconciliation_required(),
        }

    # -- structural guard --------------------------------------------------
    def place_order(self, *args, **kwargs):
        """Placeholder that refuses all order placement.

        The applier is read/outcome-only.  This explicit method documents
        and enforces the invariant: there is NO path from this layer to
        placing an order.
        """
        raise ReconciliationRequired(
            "ExchangeEventApplier never places orders (read/outcome-only); "
            "order placement is owned by the risk-gated paper cycle."
        )
