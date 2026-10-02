"""F-H2: fail-closed cancel-on-kill controller.

When a risk kill trigger fires (equity-drawdown kill, range-break kill), the
system must:

* attempt to cancel **all** relevant open orders,
* keep the kill state active even when a cancellation fails,
* NEVER interpret a failed/unknown cancel as a successful one,
* reconcile exchange/local state after the attempt,
* be idempotent and retry-safe,
* place NO replacement orders and block NEW order placement while the kill
  state is active,
* persist the kill state so a process restart cannot resume trading, and
* require an explicit, audited operator action to release the kill state.

The controller is built around an injectable ``canceler`` so the deterministic
paper path and a future exchange path share one reconciliation model.  A
cancel outcome that is not positively confirmed (``CONFIRMED`` or
``ALREADY_CANCELED``) is treated as UNRECONCILED: the local order state is
left untouched, its reservation is retained, and the kill state stays active.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Callable, Optional

from order_engine import (
    OrderState,
    PaperOrder,
    PaperOrderEngine,
)
from paper_accounting import PaperAccountingEngine
from storage import (
    connect,
    get_cancel_record,
    get_kill_state,
    record_kill_state_audit,
    set_kill_state,
    upsert_cancel_record,
)


class CancelOutcome(str, Enum):
    """Outcome of a single cancel attempt as reported by a canceler.

    Only ``CONFIRMED`` and ``ALREADY_CANCELED`` prove the order is gone.
    ``UNKNOWN`` (network/timeout/no definitive answer) and ``FAILED``
    (explicit rejection) are UNRECONCILED and must keep the kill state
    active — never interpret them as success.
    """

    CONFIRMED = "CONFIRMED"
    ALREADY_CANCELED = "ALREADY_CANCELED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


#: Outcomes that prove the order is no longer live on the exchange.
_CONFIRMED_GONE = frozenset({CancelOutcome.CONFIRMED, CancelOutcome.ALREADY_CANCELED})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CancelResult:
    """Per-order result of a cancel-on-kill pass."""

    client_order_id: str
    side: str
    price: str
    quantity: str
    executed_qty: str
    outcome: str
    local_state_before: str
    local_state_after: str
    order_cancelled: bool
    reservation_released: bool
    attempts: int
    reconciled: bool
    detail: str = ""


@dataclass(frozen=True)
class CancelReport:
    """Aggregate outcome of canceling all open orders in one pass."""

    overall_status: str
    considered: int
    cancelled: int
    already_canceled: int
    unknown: int
    failed: int
    results: tuple[CancelResult, ...] = field(default_factory=tuple)

    @property
    def all_reconciled(self) -> bool:
        return self.overall_status in {
            "CANCELLED", "ALREADY_CANCELED", "NO_OPEN_ORDERS",
        }

    @property
    def pending(self) -> int:
        """Orders still unreconciled (UNKNOWN or FAILED)."""
        return self.unknown + self.failed


class CancelError(RuntimeError):
    """Raised when a cancel pass cannot be completed safely."""


class ReleaseBlockedError(RuntimeError):
    """Raised when an operator release is refused because reconciliation is
    incomplete."""


class CancelController:
    """Deterministic, fail-closed cancel-on-kill state machine.

    The controller owns the *local* paper-order state and the durable kill
    latch.  It never places new orders and never resumes trading.  The
    exchange-facing cancel is injected as ``canceler``; the default local
    canceler reports ``CONFIRMED`` because, in paper/dry-run mode, there is
    no remote order to send a cancel to — the local state transition IS the
    cancellation.
    """

    _OPEN_STATES = (OrderState.OPEN, OrderState.PARTIALLY_FILLED)

    def __init__(
        self,
        db_path: str,
        cfg: dict,
        rules,
        clock: Optional[Callable[[], datetime]] = None,
        canceler: Optional[Callable[[PaperOrder], CancelOutcome]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        max_attempts: int = 3,
        retry_delay: float = 0.0,
        client_order_prefix: str = "AG",
    ):
        if max_attempts < 1:
            raise CancelError("max_attempts must be >= 1")
        if retry_delay < 0:
            raise CancelError("retry_delay must be >= 0")
        self.db_path = db_path
        self.cfg = cfg
        self.rules = rules
        self._clock = clock or _utc_now
        self._canceler = canceler
        # Injectable sleep keeps the paper path deterministic (default no-op);
        # operator drivers may pass time.sleep for real backoff.
        self._sleep = sleep if sleep is not None else (lambda _d: None)
        self.max_attempts = int(max_attempts)
        self.retry_delay = float(retry_delay)

        paper = cfg.get("paper", {})
        accounting = PaperAccountingEngine(
            rules.base_asset,
            rules.quote_asset,
            Decimal(str(paper.get("initial_base_balance", "0"))),
            Decimal(str(paper.get("initial_quote_balance", "0"))),
            Decimal(str(paper.get("maker_fee", "0"))),
            Decimal(str(paper.get("taker_fee", "0"))),
            str(paper.get("fee_asset", rules.quote_asset)),
        )
        # The engine releases reservations on CANCELED/REJECTED transitions
        # and fails closed when reconciliation is unhealthy.
        self._engine = PaperOrderEngine(
            db_path,
            clock=self._clock,
            accounting=accounting,
            client_order_prefix=client_order_prefix,
            reconcile_on_init=True,
        )

    # -- clock -------------------------------------------------------------
    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime):
            raise CancelError("clock must return a datetime")
        return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now

    # -- state listing ------------------------------------------------------
    def list_open_orders(self) -> list[PaperOrder]:
        """All local orders that still hold a live reservation or fill state.

        OPEN and PARTIALLY_FILLED orders are the ones a kill must clear.
        Terminal orders (CANCELED/FILLED/REJECTED) are ignored.  The engine's
        reconciliation is re-run first: unhealthy state raises
        PaperStateUnhealthyError (fail-closed) before any cancel work.
        """
        self._engine.reconcile()
        out: list[PaperOrder] = []
        dbcon = connect(self.db_path)
        try:
            rows = dbcon.execute(
                "SELECT client_order_id FROM orders "
                "WHERE status IN ('OPEN','PARTIALLY_FILLED')"
            ).fetchall()
        finally:
            dbcon.close()
        for row in rows:
            order = self._engine.get(row["client_order_id"])
            if order is not None:
                out.append(order)
        return out

    def unreconciled_open_orders(self) -> list[PaperOrder]:
        """Open orders whose cancel is not yet reconciled (no record, or a
        non-reconciled record).  These block an operator release."""
        pending: list[PaperOrder] = []
        for order in self.list_open_orders():
            rec = get_cancel_record(self.db_path, order.intent.client_order_id)
            if rec is None or not rec.get("reconciled"):
                pending.append(order)
        return pending

    # -- cancel pass --------------------------------------------------------
    def cancel_open_orders(
        self,
        actor: str = "operator",
        trigger_note: str = "",
        ts: Optional[str] = None,
    ) -> CancelReport:
        """Attempt to cancel every open local order.  Fail-closed.

        For each open order the (injected or default) canceler is invoked up
        to ``max_attempts`` times in this pass.  A confirmed outcome
        transitions the local order to CANCELED and releases its remaining
        reservation (partially-filled orders keep their filled portion
        accounted; only the unfilled remainder is released).  An unknown or
        failed outcome leaves local state and the reservation untouched and
        marks the order UNRECONCILED, so the kill state stays active.

        Idempotent: re-running the pass over already-CANCELED orders is a
        no-op; recorded attempts accumulate so retries are bounded and
        observable.
        """
        open_orders = self.list_open_orders()
        results: list[CancelResult] = []
        counts = {"CONFIRMED": 0, "ALREADY_CANCELED": 0, "UNKNOWN": 0, "FAILED": 0}

        for order in open_orders:
            rec = get_cancel_record(self.db_path, order.intent.client_order_id)
            base_attempts = 0 if rec is None else int(rec.get("attempts", 0) or 0)
            (
                outcome, local_after, cancelled, released,
                attempts, detail,
            ) = self._cancel_one(order, base_attempts)
            upsert_cancel_record(
                self.db_path,
                order.intent.client_order_id,
                last_outcome=outcome.value,
                attempts=attempts,
                reconciled=bool(cancelled),
                note=detail,
                ts=ts,
            )
            counts[outcome.value] = counts.get(outcome.value, 0) + 1
            results.append(CancelResult(
                client_order_id=order.intent.client_order_id,
                side=order.intent.side,
                price=str(order.intent.price),
                quantity=str(order.intent.quantity),
                executed_qty=str(order.executed_qty),
                outcome=outcome.value,
                local_state_before=order.state.value,
                local_state_after=local_after,
                order_cancelled=cancelled,
                reservation_released=released,
                attempts=attempts,
                reconciled=cancelled,
                detail=detail,
            ))

        if not open_orders:
            overall = "NO_OPEN_ORDERS"
        elif counts["UNKNOWN"] == 0 and counts["FAILED"] == 0:
            overall = "CANCELLED" if (
                counts["CONFIRMED"] or counts["ALREADY_CANCELED"]
            ) else "NO_OPEN_ORDERS"
        else:
            overall = "PENDING_RECONCILIATION"

        return CancelReport(
            overall_status=overall,
            considered=len(open_orders),
            cancelled=counts["CONFIRMED"],
            already_canceled=counts["ALREADY_CANCELED"],
            unknown=counts["UNKNOWN"],
            failed=counts["FAILED"],
            results=tuple(results),
        )

    def _cancel_one(self, order: PaperOrder, base_attempts: int):
        """Run the canceler with bounded retries; return the settled outcome.

        Returns ``(outcome, local_state_after, order_cancelled,
        reservation_released, attempts_total, detail)``.  Only confirmed
        outcomes move local state; everything else fails closed.
        """
        outcome = self._run_canceler(order)
        attempts = base_attempts + 1
        calls_this_pass = 1
        while outcome not in _CONFIRMED_GONE and calls_this_pass < self.max_attempts:
            if self.retry_delay > 0:
                self._sleep(self.retry_delay)
            outcome = self._run_canceler(order)
            attempts += 1
            calls_this_pass += 1

        if outcome in _CONFIRMED_GONE:
            already = outcome == CancelOutcome.ALREADY_CANCELED
            if order.state in self._OPEN_STATES:
                # Local transition + reservation release.  PaperStateUnhealthy
                # propagates from the engine's health gate (fail-closed).
                self._engine.transition(order.intent.client_order_id, OrderState.CANCELED)
                local_after = OrderState.CANCELED.value
                cancelled = True
                released = True
                detail = "already_canceled" if already else "confirmed_canceled"
            else:
                # Defensive: list_open_orders filters to live states.
                local_after = order.state.value
                cancelled = order.state == OrderState.CANCELED
                released = False
                detail = "already_terminal"
        else:
            # UNKNOWN / FAILED: fail closed.  Local state and reservation are
            # left untouched; the order stays open and unreconciled.
            local_after = order.state.value
            cancelled = False
            released = False
            detail = (
                "cancel_unknown_fail_closed"
                if outcome == CancelOutcome.UNKNOWN
                else "cancel_failed_fail_closed"
            )
        return (outcome, local_after, cancelled, released, attempts, detail)

    def _run_canceler(self, order: PaperOrder) -> CancelOutcome:
        if self._canceler is None:
            # Paper/dry-run: no remote order to cancel.  The local transition
            # the controller performs is the cancellation, so report CONFIRMED.
            return CancelOutcome.CONFIRMED
        raw = self._canceler(order)
        if isinstance(raw, CancelOutcome):
            return raw
        try:
            return CancelOutcome(str(raw))
        except (TypeError, ValueError):
            # A malformed canceler response is not a confirmed cancel.
            return CancelOutcome.UNKNOWN

    # -- kill-state latch ---------------------------------------------------
    def latch_kill_state(
        self,
        trigger: str,
        report: CancelReport,
        actor: str = "automatic",
        note: str = "",
        ts: Optional[str] = None,
    ) -> Optional[dict]:
        """Latches the kill state and records a cancel pass's progress.

        The final ``cancel_status`` is derived from ``report``: a pass with any
        unreconciled orders is ``PENDING_RECONCILIATION`` (fail-closed);
        otherwise the pass's own status (CANCELLED / NO_OPEN_ORDERS).  A fresh
        activation stamps ``activated_at``; re-latching preserves the original
        timestamp/trigger so the audit trail stays stable.  This is the single
        writer of the latch's progress fields, so callers never duplicate the
        state write.
        """
        prev = get_kill_state(self.db_path)
        prev_active = bool(prev and prev.get("active"))
        if not trigger:
            raise CancelError("latch_kill_state requires a non-empty trigger")
        cancel_status = (
            "PENDING_RECONCILIATION" if report.pending > 0 else report.overall_status
        )
        open_order_count = report.pending if report.pending > 0 else report.considered
        if not prev_active:
            set_kill_state(
                self.db_path,
                active=True,
                trigger=trigger,
                open_order_count=open_order_count,
                cancel_status=cancel_status,
                note=note,
                ts=ts or _utc_now().isoformat(),
            )
        else:
            set_kill_state(
                self.db_path,
                active=True,
                trigger=trigger,
                open_order_count=open_order_count,
                cancel_status=cancel_status,
                note=note,
            )
        record_kill_state_audit(
            self.db_path,
            action="ACTIVATE" if not prev_active else "RETRY",
            previous_active=prev_active,
            new_active=True,
            trigger=trigger,
            reason=trigger,
            actor=actor,
        )
        return get_kill_state(self.db_path)

    def kill_state(self) -> Optional[dict]:
        return get_kill_state(self.db_path)

    def is_kill_active(self) -> bool:
        st = get_kill_state(self.db_path)
        return bool(st and st.get("active"))

    def pre_latch(
        self,
        trigger: str,
        actor: str = "risk_engine",
        note: str = "",
        ts: Optional[str] = None,
    ) -> None:
        """Persist the kill latch BEFORE attempting cancellation.

        Crash-safety guarantee: a process that dies mid-cancel-pass still
        leaves the kill latched, so a restart re-enters the kill branch and
        re-attempts cancellation.  Idempotent; a second call on an already
        active latch is a no-op on the latch (the audit row is still written).
        """
        if not trigger:
            raise CancelError("pre_latch requires a non-empty trigger")
        prev = get_kill_state(self.db_path)
        prev_active = bool(prev and prev.get("active"))
        if prev_active:
            # Already latched: keep the existing progress fields; just confirm
            # the trigger and re-audit.  Do not clobber cancel_status.
            set_kill_state(
                self.db_path, active=True, trigger=trigger,
                open_order_count=(prev or {}).get("open_order_count"),
                cancel_status=(prev or {}).get("cancel_status") or "LATCHED",
                note=note,
            )
        else:
            # First activation: stamp activated_at now, progress pending.
            set_kill_state(
                self.db_path, active=True, trigger=trigger,
                cancel_status="LATCHED", note=note,
                ts=ts or _utc_now().isoformat(),
            )
        record_kill_state_audit(
            self.db_path,
            action="PRE_LATCH" if not prev_active else "RETRY",
            previous_active=prev_active,
            new_active=True,
            trigger=trigger,
            reason=trigger,
            actor=actor,
        )

    def reconcile_cancelled(
        self,
        client_order_id: str,
        confirmed_canceled: bool = True,
        note: str = "",
        ts: Optional[str] = None,
    ) -> CancelResult:
        """Operator-confirmed reconciliation of one unreconciled order.

        Used when a cancel came back UNKNOWN/FAILED and the operator has
        since verified on the exchange that the order is gone.  Locally
        transitions the order to CANCELED, releases its remaining
        reservation, and marks the cancel record reconciled.  Refuses to
        reconcile as canceled anything that is FILLED, and refuses to act
        without positive confirmation (fail-closed).
        """
        order = self._engine.get(client_order_id)
        if order is None:
            raise CancelError(f"Unknown paper order: {client_order_id}")
        if order.state == OrderState.FILLED:
            raise CancelError(
                f"Order {client_order_id} is FILLED and cannot be reconciled "
                "as canceled"
            )
        rec = get_cancel_record(self.db_path, client_order_id)
        prior_attempts = int(rec.get("attempts", 0) or 0) if rec else 0
        if rec is not None and rec.get("reconciled"):
            return CancelResult(
                client_order_id=client_order_id,
                side=order.intent.side,
                price=str(order.intent.price),
                quantity=str(order.intent.quantity),
                executed_qty=str(order.executed_qty),
                outcome="ALREADY_RECONCILED",
                local_state_before=order.state.value,
                local_state_after=order.state.value,
                order_cancelled=order.state == OrderState.CANCELED,
                reservation_released=False,
                attempts=prior_attempts,
                reconciled=True,
                detail="already reconciled",
            )
        if not confirmed_canceled:
            raise CancelError(
                "Refusing to reconcile as canceled when not confirmed "
                "(fail-closed)"
            )
        if order.state in self._OPEN_STATES:
            self._engine.transition(client_order_id, OrderState.CANCELED)
            local_after = OrderState.CANCELED.value
            cancelled = True
            released = True
        else:
            local_after = order.state.value
            cancelled = order.state == OrderState.CANCELED
            released = False
        upsert_cancel_record(
            self.db_path, client_order_id, "RECONCILED",
            attempts=prior_attempts + 1,
            reconciled=True,
            note=note or "operator_confirmed",
            ts=ts,
        )
        return CancelResult(
            client_order_id=client_order_id,
            side=order.intent.side,
            price=str(order.intent.price),
            quantity=str(order.intent.quantity),
            executed_qty=str(order.executed_qty),
            outcome="RECONCILED",
            local_state_before=order.state.value,
            local_state_after=local_after,
            order_cancelled=cancelled,
            reservation_released=released,
            attempts=prior_attempts + 1,
            reconciled=True,
            detail=note or "operator_confirmed",
        )

    def release(
        self,
        reason: str,
        actor: str = "operator",
        ts: Optional[str] = None,
    ) -> Optional[dict]:
        """Explicitly release the kill latch.  Fail-closed.

        Refuses when any open order is still UNRECONCILED: the bot cannot
        resume with unknown exposure.  The release only turns the latch off
        and is itself audited; placing new orders still requires the normal
        risk gate to PASS on a subsequent run.
        """
        if not reason or not str(reason).strip():
            raise ReleaseBlockedError("release requires a non-empty reason")
        if not actor or not str(actor).strip():
            raise ReleaseBlockedError("release requires a non-empty actor")
        pending = self.unreconciled_open_orders()
        if pending:
            ids = ", ".join(o.intent.client_order_id for o in pending)
            raise ReleaseBlockedError(
                f"Cannot release: {len(pending)} open order(s) are not "
                f"reconciled as canceled: {ids}. Reconcile each (cancel "
                "confirmed or operator-verified) before resuming."
            )
        prev = get_kill_state(self.db_path)
        prev_active = bool(prev and prev.get("active"))
        set_kill_state(
            self.db_path,
            active=False,
            open_order_count=0,
            cancel_status="RELEASED",
            note=(reason or "").strip(),
        )
        record_kill_state_audit(
            self.db_path,
            action="RELEASE",
            previous_active=prev_active,
            new_active=False,
            trigger=(prev or {}).get("trigger"),
            reason=str(reason).strip(),
            actor=str(actor).strip(),
        )
        return get_kill_state(self.db_path)


__all__ = [
    "CancelOutcome",
    "CancelResult",
    "CancelReport",
    "CancelController",
    "CancelError",
    "ReleaseBlockedError",
]
