"""Round 6A — deterministic, fail-closed REST reconciliation seam.

Exchange-facing reconciliation for a controlled Binance Spot TESTNET phase.
Works against the read-only ``BinanceTestnetClient`` seam
(``binance_testnet``): fetch open orders, fetch individual order status via
deterministic ``clientOrderId``, and — only through an explicitly injected,
seam-gated cancel executor — attempt cancellations.  This module NEVER
places new economic orders, NEVER blindly retries a WRITE, and NEVER
interprets an uncertain exchange outcome as safe (Round 6A §3).

Failure semantics (Round 6A §3)
================================
Every exchange interaction settles to exactly one of::

    CONFIRMED   -- authoritative exchange state established
    UNKNOWN     -- exchange state could NOT be established (timeout, reset,
                   malformed response, ambiguous status, missing order,
                   429/418 budget exhausted, clock-sync failure)
    FAILED      -- deterministic, non-retryable rejection (explicit
                   authentication/permission failure, config/environment
                   error)

UNKNOWN is BLOCKING: new orders are blocked, the kill/risk veto stays
effective, and an UNKNOWN submission is NEVER resubmitted and NEVER treated
as "safe to retry" by assuming the order did not exist (§3/§4).

Retry policy (§9/§11)
======================
READ operations (open-order fetch, order-status query, server-time) are
retry-safe and get a bounded per-op retry budget with controlled backoff
that honors Retry-After on 429/418.  WRITE operations (cancel) are NOT
blindly retried: a non-confirmed cancel settles UNRECONCILED and only an
authoritative re-query can settle it (§11 read/write distinction).

Clock skew (§10)
=================
Signed requests stamp the local clock; the SDK offers no automatic clock
re-sync.  ``sync_clock_skew`` measures a bounded local-minus-server offset
from repeated server-time samples and refuses absurd values; it never
mutates the OS clock.  A request rejected with a timestamp-skew error is
retryable only after a successful re-sync, within budget.

Observability (§14)
===================
Every operation appends a structured log record (request type, symbol,
local order id, exchange order id when known, outcome, retry count, error
class, reconciliation result) via the standard logging module and an
optional JSONL sink.  Records never contain credentials — the adapter
redacts them at the boundary.

API-key permissions (§13)
=========================
The seam requires at most *account read* plus, for the explicitly
authorized testnet cancel path, *spot-trading* permission.  It NEVER
requires, requests, or uses withdrawal permissions; no withdrawal
capability exists anywhere in this module.  Credentials are read only
from the adapter's environment-sourced config (never stored in source,
never logged — the config dataclass masks ``api_key``/``api_secret`` from
``repr`` and the redaction layer strips them from exception text).

Testnet integration (§18)
=========================
Normal CI needs no network access: the tests in ``tests/test_rest_reconciler.py``
mock the SDK ``rest_api`` transport so the *real* adapter parsing, error
mapping, and reconciliation logic run deterministically against a fake
testnet.  An actual Testnet smoke test against
``https://testnet.binance.vision`` (read-only
``scripts/testnet_readonly_check.py`` + the single-order query added by
this round) requires real testnet credentials and explicit human
authorization in a later task — real Testnet order placement or cancel
execution is a separate, gated capability and is NOT wired here.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Mapping, Optional

from binance_testnet import (
    BinanceOpenOrderSnapshot,
    BinanceTestnetClient,
    BinanceTestnetConfigError,
    BinanceTestnetEnvironmentError,
    BinanceTestnetError,
    BinanceTestnetNetworkError,
    BinanceTestnetAuthenticationError,
)

logger = logging.getLogger("rest_reconciler")


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------
class Outcome(str, Enum):
    """Settled outcome of a single exchange interaction (Round 6A §3)."""

    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


class CancelVerdict(str, Enum):
    """Settled verdict of a cancel attempt (Round 6A §5).

    Only CONFIRMED_CANCELED proves the order is gone.  Every other path —
    timeout, connection reset, malformed ack, unknown order, already-filled
    order, 429/418 budget exhaustion — settles UNRECONCILED, which keeps
    the kill state active (§5: cancel UNKNOWN/FAILED/timeout ⇒ kill stays).
    """

    CONFIRMED_CANCELED = "CONFIRMED_CANCELED"
    UNRECONCILED = "UNRECONCILED"


class Op(str, Enum):
    """Exchange operation class — drives the per-op retry budget (§11).

    READ ops are retry-safe.  WRITE ops (cancel) are never blindly
    retried; a non-confirmed write settles UNRECONCILED and is re-settled
    only by an authoritative query.
    """

    READ_OPEN_ORDERS = "READ_OPEN_ORDERS"
    READ_ORDER_STATUS = "READ_ORDER_STATUS"
    WRITE_CANCEL = "WRITE_CANCEL"
    READ_SERVER_TIME = "READ_SERVER_TIME"


#: Explicit per-operation retry budgets (total attempts, first included).
#: Reads may generally be safely retried; writes must not (§11).
RETRY_BUDGET: Mapping[Op, int] = {
    Op.READ_OPEN_ORDERS: 3,
    Op.READ_ORDER_STATUS: 4,
    Op.READ_SERVER_TIME: 3,
    Op.WRITE_CANCEL: 1,
}

#: Controlled, bounded backoff schedule (seconds) for READ retries without a
#: Retry-After value.  Exponential 1s → 2s → 4s…, capped (§9).
_BACKOFF_SCHEDULE_S = (1.0, 2.0, 4.0)
_BACKOFF_CAP_S = 10.0

#: Upper bound for a Retry-After value the reconciler will honor (§9).
#: Capped so a hostile/garbage header cannot stall the fail-closed loop
#: indefinitely, but large enough to respect a legitimate ban window.
_RETRY_AFTER_CAP_S = 300.0


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OrderStatus:
    """One settled order-status resolution for a deterministic clientOrderId.

    ``authoritative`` is True only when the exchange state was positively
    established.  A still-UNKNOWN submission keeps ``authoritative=False``
    so callers block new orders (§4 step 4).
    """

    client_order_id: str
    symbol: str
    exchange_order_id: Optional[int] = None
    status: Optional[str] = None  # NEW / PARTIALLY_FILLED / FILLED / CANCELED / EXPIRED / REJECTED
    outcome: Outcome = Outcome.UNKNOWN
    authoritative: bool = False
    detail: str = ""

    @property
    def blocks_new_orders(self) -> bool:
        return not (self.authoritative and self.outcome is Outcome.CONFIRMED)


@dataclass(frozen=True)
class CancelRecord:
    """Settled result of one cancel attempt plus authoritative verification."""

    client_order_id: str
    symbol: str
    verdict: CancelVerdict
    detail: str = ""
    attempts: int = 1

    @property
    def reconciled(self) -> bool:
        """True only when the verdict proves the order is gone."""
        return self.verdict is CancelVerdict.CONFIRMED_CANCELED


@dataclass(frozen=True)
class ReconciliationResult:
    """Authoritative local-vs-exchange open-order comparison (§7).

    ``authoritative`` is True only when the exchange snapshot was
    established AND every local order matched exactly one remote order with
    a compatible status.  When False the caller MUST block new orders.
    This result carries NO destructive-action hints (§7 rule: never auto
    cancel or recreate on a single differing snapshot).
    """

    authoritative: bool
    outcome: Outcome
    matched: tuple
    local_missing_remotely: tuple
    remote_missing_locally: tuple
    status_mismatches: tuple
    duplicates: tuple
    details: tuple = ()

    @property
    def blocks_new_orders(self) -> bool:
        return not self.authoritative


@dataclass(frozen=True)
class ClockSkewReport:
    """Bounded clock-skew measurement (Round 6A §10)."""

    established: bool
    offset_ms: int = 0
    samples: int = 0
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.established


# ---------------------------------------------------------------------------
# Retry helpers (READ ops only — §9)
# ---------------------------------------------------------------------------
def _retry_wait_s(op: Op, attempt_index: int, err: Exception) -> float:
    """Bounded wait (seconds) before READ retry ``attempt_index`` (0-based).

    Honors Retry-After on rate-limit errors; otherwise exponential
    backoff from the fixed schedule, capped so a hostile Retry-After can
    never stall the loop (§9).  WRITE ops never call this — their budget
    is 1 (no blind retry, §11).
    """
    if op is Op.WRITE_CANCEL:
        raise ValueError("WRITE ops are not blindly retried (Round 6A §11)")
    ra_ms = getattr(err, "retry_after_ms", None)
    if ra_ms is not None and ra_ms > 0:
        # Respect a legitimate Retry-After within the honor cap (§9):
        # a 30s wait must NOT be shrunk to the generic 10s backoff cap.
        return min(float(ra_ms) / 1000.0, _RETRY_AFTER_CAP_S)
    return min(_BACKOFF_SCHEDULE_S[min(attempt_index, len(_BACKOFF_SCHEDULE_S) - 1)],
               _BACKOFF_CAP_S)


def _classify_deterministic(exc: Exception) -> Outcome:
    """Map a non-network adapter error to CONFIRMED|UNKNOWN|FAILED.

    Environment/config contradictions and authentication/permission
    failures are deterministic → FAILED.  Malformed/ambiguous responses are
    UNKNOWN (never "safe", §3).
    """
    if isinstance(exc, (BinanceTestnetEnvironmentError, BinanceTestnetConfigError)):
        return Outcome.FAILED
    if isinstance(exc, BinanceTestnetAuthenticationError):
        return Outcome.FAILED
    return Outcome.UNKNOWN


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------
class RestReconciler:
    """Deterministic, fail-closed exchange-reconciliation seam (Round 6A §2).

    Parameters
    ----------
    client:
        The read-only testnet adapter.  It refuses to construct unless
        ``environment=testnet``, ``dry_run=True`` and
        ``allow_live_execution=False`` (§12/§19); the reconciler re-asserts
        that barrier before use.
    cancel_executor:
        Optional injectable ``f(symbol, client_order_id) ->
        CancelVerdict`` used for kill-path cancellations (§5).  The default
        dry executor settles UNRECONCILED deterministically without any
        network call, so CI never needs live credentials (§18).
    sleep:
        Injectable sleep for backoff (deterministic tests pass a recorder;
        production passes ``time.sleep``).  Only READ retries use it.
    max_skew_ms:
        Bound on |local - server| for which a clock offset is trusted (§10);
        beyond it the sync fails closed.
    """

    def __init__(
        self,
        client: BinanceTestnetClient,
        cancel_executor: Optional[Callable[[str, str], CancelVerdict]] = None,
        sleep: Callable[[float], None] = time.sleep,
        max_skew_ms: int = 15000,
    ) -> None:
        # Re-assert the adapter-level isolation barrier (§12/§19) rather than
        # trusting construction: the reconciler may only wrap a DRY_RUN,
        # non-live, testnet client.
        cfg = getattr(client, "_config", None)
        if cfg is None:
            raise BinanceTestnetConfigError(
                "RestReconciler requires a validated BinanceTestnetClient; "
                "cannot verify testnet/dry-run isolation"
            )
        if not (cfg.dry_run and not cfg.allow_live_execution):
            raise BinanceTestnetConfigError(
                "RestReconciler requires DRY_RUN=true and "
                "ALLOW_LIVE_EXECUTION=false"
            )
        self._client = client
        self._cancel_executor = (
            cancel_executor if cancel_executor is not None else _dry_cancel_executor
        )
        self._sleep = sleep
        self._max_skew_ms = max_skew_ms
        self._log_sink: Optional[Callable[[dict], None]] = None

    # -- observability plumbing (§14) -----------------------------------------
    def set_log_sink(self, sink: Optional[Callable[[dict], None]]) -> None:
        """Attach/detach a structured-log sink (e.g. JSONL writer).

        The sink receives credential-free records only; the adapter
        redacts credentials before they reach any exception message.
        """
        self._log_sink = sink

    def _log(
        self,
        op: Op,
        symbol: str,
        cid: str,
        outcome: str,
        retries: int,
        err_class: Optional[str],
        extra: Optional[dict] = None,
    ) -> None:
        record = {
            "op": op.value,
            "symbol": symbol,
            "client_order_id": cid,
            "outcome": outcome,
            "retries": retries,
            "error_class": err_class,
        }
        if extra:
            record.update(extra)
        logger.debug("rest_reconciler %s", json.dumps(record, sort_keys=True))
        if self._log_sink is not None:
            try:
                self._log_sink(record)
            except Exception:  # observability must never break reconciliation
                logger.warning("rest_reconciler log sink failed", exc_info=True)

    # -- READ: open orders (§7 G: unavailable ⇒ UNKNOWN) -----------------------
    def fetch_open_orders(self, symbol: str) -> tuple[Outcome, list, int]:
        """Fetch the authoritative open-order snapshot.

        Returns ``(outcome, orders, attempts)``.  On success ``orders`` is a
        list of ``BinanceOpenOrderSnapshot``.  Any failure — network,
        timeout, 429/418 budget exhausted, malformed or duplicate entries —
        settles ``UNKNOWN`` (auth/config only → ``FAILED``).  A missing
        snapshot is NEVER interpreted as "no orders" (§3).
        """
        attempts = 0
        budget = RETRY_BUDGET[Op.READ_OPEN_ORDERS]
        for attempt in range(budget):
            attempts = attempt + 1
            try:
                orders = self._client.open_orders(symbol)
            except BinanceTestnetNetworkError as exc:
                if attempt + 1 >= budget:
                    self._log(Op.READ_OPEN_ORDERS, symbol, "",
                              Outcome.UNKNOWN.value, attempts, type(exc).__name__)
                    return Outcome.UNKNOWN, [], attempts
                self._sleep(_retry_wait_s(Op.READ_OPEN_ORDERS, attempt, exc))
                continue
            except BinanceTestnetError as exc:
                outcome = _classify_deterministic(exc)
                self._log(Op.READ_OPEN_ORDERS, symbol, "", outcome.value,
                           attempts, type(exc).__name__)
                return outcome, [], attempts
            except Exception as exc:
                # §16 surprise-exception guard: any unexpected failure in the
                # adapter (malformed JSON, unexpected types, SDK crash) is
                # ambiguous — fail closed to UNKNOWN, never CONFIRMED.
                self._log(Op.READ_OPEN_ORDERS, symbol, "",
                          Outcome.UNKNOWN.value, attempts,
                          type(exc).__name__, {"detail": "surprise_exception"})
                return Outcome.UNKNOWN, [], attempts
            self._log(Op.READ_OPEN_ORDERS, symbol, "", Outcome.CONFIRMED.value,
                       attempts, None, {"order_count": len(orders)})
            return Outcome.CONFIRMED, list(orders), attempts
        # Unreachable for loop bounds; kept explicit and fail-closed.
        self._log(Op.READ_OPEN_ORDERS, symbol, "", Outcome.UNKNOWN.value,
                   attempts, "budget_exhausted")
        return Outcome.UNKNOWN, [], attempts

    # -- READ: individual order status (timeout-after-submit, §4/§6) ----------
    def resolve_order(
        self,
        symbol: str,
        client_order_id: str,
        *,
        allow_confirmed_absence: bool = False,
    ) -> OrderStatus:
        """Authoritatively resolve one order by deterministic clientOrderId.

        Uses the exchange single-order query when the adapter exposes
        ``get_order``; otherwise falls back to the open-order snapshot.
        Fail-closed rules:

        * an order found in a known status (NEW / PARTIALLY_FILLED / FILLED
          / CANCELED / EXPIRED / REJECTED) → ``CONFIRMED`` with that status
          (§6);
        * a malformed or unknown status → ``UNKNOWN`` — never mapped to a
          safe state (§6);
        * a missing-order response is NEVER proof the order did not exist:
          the order may have been filled-then-archived, rejected, or
          expired (§3).  It settles ``UNKNOWN`` — the single exception is
          ``allow_confirmed_absence=True``, used only AFTER an explicitly
          confirmed cancel, where absence from the open list is the
          expected terminal state;
        * any network/timeout/budget failure → ``UNKNOWN`` (§3: no new
          orders; the caller blocks).
        """
        attempts = 0
        last_err = "budget_exhausted"
        budget = RETRY_BUDGET[Op.READ_ORDER_STATUS]
        get_order = getattr(self._client, "get_order", None)
        normalized = str(symbol).upper()
        for attempt in range(budget):
            attempts = attempt + 1
            try:
                if get_order is not None:
                    payload = get_order(symbol, client_order_id)
                    if not isinstance(payload, dict):
                        # Malformed response shape → fail closed (§6/§16):
                        # never interpret an unreadable payload as a status.
                        self._log(Op.READ_ORDER_STATUS, normalized,
                                  client_order_id, Outcome.UNKNOWN.value,
                                  attempts, "malformed_payload")
                        return OrderStatus(
                            client_order_id=client_order_id,
                            symbol=normalized,
                            outcome=Outcome.UNKNOWN,
                            detail="order-status payload is not an object "
                                   "(fail closed, §6)",
                        )
                    status_field = payload.get("status")
                    if status_field in (
                        "NEW", "PARTIALLY_FILLED", "FILLED",
                        "CANCELED", "EXPIRED", "REJECTED",
                    ):
                        self._log(Op.READ_ORDER_STATUS, normalized, client_order_id,
                                  Outcome.CONFIRMED.value, attempts, None,
                                  {"status": status_field,
                                   "exchange_order_id": payload.get("orderId")})
                        return OrderStatus(
                            client_order_id=client_order_id,
                            symbol=normalized,
                            exchange_order_id=payload.get("orderId"),
                            status=status_field,
                            outcome=Outcome.CONFIRMED,
                            authoritative=True,
                            detail="single-order query confirmed status",
                        )
                    # Malformed/unknown status → UNKNOWN (§6 fail closed).
                    self._log(Op.READ_ORDER_STATUS, normalized, client_order_id,
                              Outcome.UNKNOWN.value, attempts, "unknown_status",
                              {"status": status_field})
                    return OrderStatus(
                        client_order_id=client_order_id,
                        symbol=normalized,
                        outcome=Outcome.UNKNOWN,
                        detail=f"unknown status {status_field!r} (fail closed, §6)",
                    )
                # No single-order query: fall back to the open-order
                # snapshot; absence is NOT authoritative (an order may have
                # just filled/archived/canceled).
                outcome, orders, _ = self.fetch_open_orders(symbol)
                if outcome is not Outcome.CONFIRMED:
                    return OrderStatus(
                        client_order_id=client_order_id,
                        symbol=normalized,
                        outcome=outcome,
                        detail="open-order snapshot unavailable",
                    )
                for o in orders:
                    if o.client_order_id == client_order_id:
                        self._log(Op.READ_ORDER_STATUS, normalized, client_order_id,
                                  Outcome.CONFIRMED.value, attempts, None,
                                  {"status": o.status,
                                   "exchange_order_id": o.order_id})
                        return OrderStatus(
                            client_order_id=client_order_id,
                            symbol=normalized,
                            exchange_order_id=o.order_id,
                            status=o.status,
                            outcome=Outcome.CONFIRMED,
                            authoritative=True,
                            detail="matched in open-order snapshot",
                        )
                if allow_confirmed_absence:
                    self._log(Op.READ_ORDER_STATUS, normalized, client_order_id,
                              Outcome.CONFIRMED.value, attempts,
                              "confirmed_absent",
                              {"via": "open_orders_absence"})
                    return OrderStatus(
                        client_order_id=client_order_id,
                        symbol=normalized,
                        status="CANCELED",
                        outcome=Outcome.CONFIRMED,
                        authoritative=True,
                        detail="absent from open orders after confirmed cancel",
                    )
                self._log(Op.READ_ORDER_STATUS, normalized, client_order_id,
                          Outcome.UNKNOWN.value, attempts, "missing_order",
                          {"via": "open_orders_absence"})
                return OrderStatus(
                    client_order_id=client_order_id,
                    symbol=normalized,
                    outcome=Outcome.UNKNOWN,
                    detail="order not in open-order snapshot (ambiguous, §3)",
                )
            except BinanceTestnetNetworkError as exc:
                last_err = type(exc).__name__
                if attempt + 1 >= budget:
                    break
                self._sleep(_retry_wait_s(Op.READ_ORDER_STATUS, attempt, exc))
                continue
            except BinanceTestnetError as exc:
                outcome = _classify_deterministic(exc)
                self._log(Op.READ_ORDER_STATUS, normalized, client_order_id,
                          outcome.value, attempts, type(exc).__name__)
                return OrderStatus(
                    client_order_id=client_order_id,
                    symbol=normalized,
                    outcome=outcome,
                    detail=type(exc).__name__,
                )
        self._log(Op.READ_ORDER_STATUS, normalized, client_order_id,
                  Outcome.UNKNOWN.value, attempts, last_err)
        return OrderStatus(
            client_order_id=client_order_id,
            symbol=normalized,
            outcome=Outcome.UNKNOWN,
            detail=f"status unresolved after {attempts} attempt(s): {last_err}",
        )

    # -- timeout-after-submit settlement (§4) ----------------------------------
    def settle_timeout_after_submit(
        self, symbol: str, client_order_id: str
    ) -> OrderStatus:
        """§4: after a submission timed out, resolve via clientOrderId.

        Deterministic behavior per spec §4:

        1. the local submission outcome is already UNKNOWN at the call site;
        2. this method does NOT resubmit anything — it queries exchange
           state by the deterministic client order id;
        3. FILLED / NEW (open) / PARTIALLY_FILLED / CANCELED / EXPIRED /
           REJECTED each settle to an authoritative status — recovery is
           only allowed from authoritative state (§4 step 5);
        4. if the order cannot be found, or the query itself fails, the
           result stays UNKNOWN and new economic orders stay blocked
           (§4 steps 4–5; §3 invariant: UNKNOWN is never "safe to retry").
        """
        return self.resolve_order(symbol, client_order_id,
                                  allow_confirmed_absence=False)

    # -- WRITE: cancel (§5) ---------------------------------------------------
    def cancel(
        self, symbol: str, client_order_id: str, *, verify: bool = True
    ) -> CancelRecord:
        """Cancel one order via the injected, seam-gated executor (§5).

        The dry-run default executor settles UNRECONCILED deterministically
        (no network call).  A real testnet executor performs the cancel and
        reports CONFIRMED_CANCELED; when ``verify`` is True this method then
        re-queries authoritative state, because a cancel ack or a network
        timeout is NEVER proof of cancellation (§5: "Do NOT treat
        HTTP/network timeout as proof of cancellation").  Only an
        explicitly confirmed CANCELED/absent state yields
        CONFIRMED_CANCELED; anything else settles UNRECONCILED and the kill
        state stays active (§5/§20).
        """
        normalized = str(symbol).upper()
        exec_verdict = self._cancel_executor(symbol, client_order_id)
        if exec_verdict is CancelVerdict.CONFIRMED_CANCELED and verify:
            # Re-establish authoritative post-cancel state (§5 rule).
            st = self.resolve_order(symbol, client_order_id,
                                   allow_confirmed_absence=True)
            if st.authoritative and st.status == "CANCELED":
                self._log(Op.WRITE_CANCEL, normalized, client_order_id,
                          Outcome.CONFIRMED.value, 2, None,
                          {"detail": st.detail})
                return CancelRecord(
                    client_order_id=client_order_id,
                    symbol=normalized,
                    verdict=CancelVerdict.CONFIRMED_CANCELED,
                    detail=f"verified: {st.detail}",
                    attempts=2,
                )
            # Verification inconclusive → UNRECONCILED (§5).
            self._log(Op.WRITE_CANCEL, normalized, client_order_id,
                      Outcome.UNKNOWN.value, 2, "verify_unconfirmed",
                      {"detail": st.detail, "status": st.status})
            return CancelRecord(
                client_order_id=client_order_id,
                symbol=normalized,
                verdict=CancelVerdict.UNRECONCILED,
                detail=f"cancel ack but verification inconclusive: {st.detail}",
                attempts=2,
            )
        if exec_verdict is CancelVerdict.CONFIRMED_CANCELED:
            self._log(Op.WRITE_CANCEL, normalized, client_order_id,
                      Outcome.CONFIRMED.value, 1, None)
            return CancelRecord(
                client_order_id=client_order_id,
                symbol=normalized,
                verdict=CancelVerdict.CONFIRMED_CANCELED,
                detail="executor-confirmed (verification skipped)",
                attempts=1,
            )
        # UNRECONCILED: kill remains active (§5/§20).
        self._log(Op.WRITE_CANCEL, normalized, client_order_id,
                  Outcome.UNKNOWN.value, 1, "unreconciled")
        return CancelRecord(
            client_order_id=client_order_id,
            symbol=normalized,
            verdict=CancelVerdict.UNRECONCILED,
            detail="executor did not confirm cancellation (fail closed, §5)",
            attempts=1,
        )

    # -- open-order reconciliation (§7) ----------------------------------------
    def reconcile_open_orders(
        self,
        symbol: str,
        local_open: Mapping[str, str],
        *,
        snapshot_fetched_at: Optional[datetime] = None,
        now: Optional[datetime] = None,
        max_stale_s: Optional[float] = None,
    ) -> ReconciliationResult:
        """Compare local open orders against the exchange snapshot (§7).

        ``local_open`` maps ``client_order_id`` → locally expected status.
        The comparison NEVER triggers destructive action (§7: any
        destructive action requires an unambiguous state interpretation).
        The result is authoritative only when the snapshot was established
        AND every local order matched exactly one remote order with a
        compatible status.  Divergence, duplicates, malformed entries, a
        stale snapshot, or an unavailable snapshot all settle UNKNOWN →
        blocks new orders:

        * A exact match → authoritative;
        * B local order missing remotely → UNKNOWN;
        * C remote order missing locally → UNKNOWN;
        * D remote status differs from local expectation → UNKNOWN;
        * E duplicate remote order id → UNKNOWN (ambiguous);
        * F malformed remote order → UNKNOWN (raised inside the fetch);
        * G exchange response unavailable → UNKNOWN;
        * H stale exchange response → UNKNOWN when
          ``snapshot_fetched_at``/``now``/``max_stale_s`` show the snapshot
          is older than the bound.
        """
        normalized = str(symbol).upper()
        outcome, orders, attempts = self.fetch_open_orders(normalized)
        if outcome is not Outcome.CONFIRMED:
            self._log(Op.READ_OPEN_ORDERS, normalized, "", outcome.value,
                      attempts, "reconcile_unavailable",
                      {"reconciliation_result": outcome.value})
            return ReconciliationResult(
                authoritative=False,
                outcome=outcome,
                matched=(),
                local_missing_remotely=tuple(local_open.keys()),
                remote_missing_locally=(),
                status_mismatches=(),
                duplicates=(),
                details=(f"exchange snapshot unavailable: {outcome.value} (§7 G)",),
            )
        remote_by_cid: dict[str, BinanceOpenOrderSnapshot] = {}
        duplicates: list[str] = []
        for o in orders:
            if o.client_order_id in remote_by_cid:
                duplicates.append(o.client_order_id)
            else:
                remote_by_cid[o.client_order_id] = o
        if duplicates:
            self._log(Op.READ_OPEN_ORDERS, normalized, "",
                      Outcome.UNKNOWN.value, attempts,
                      "duplicate_remote_order",
                      {"duplicates": duplicates})
            return ReconciliationResult(
                authoritative=False,
                outcome=Outcome.UNKNOWN,
                matched=(),
                local_missing_remotely=tuple(local_open.keys()),
                remote_missing_locally=tuple(remote_by_cid.keys()),
                status_mismatches=(),
                duplicates=tuple(duplicates),
                details=("duplicate remote order ids — ambiguous (§7 E)",),
            )
        matched: list[str] = []
        local_missing: list[str] = []
        mismatches: list[str] = []
        for cid, expected in local_open.items():
            remote = remote_by_cid.get(cid)
            if remote is None:
                local_missing.append(cid)
                continue
            exp = str(expected).upper()
            if exp != remote.status:
                mismatches.append(cid)
            else:
                matched.append(cid)
        remote_only = [c for c in remote_by_cid if c not in local_open]
        stale_detail = ""
        if (max_stale_s is not None
                and snapshot_fetched_at is not None
                and now is not None):
            if snapshot_fetched_at.tzinfo is None:
                snapshot_fetched_at = snapshot_fetched_at.replace(
                    tzinfo=timezone.utc)
            age_s = (now - snapshot_fetched_at).total_seconds()
            if age_s > max_stale_s:
                stale_detail = (f"snapshot age {age_s:.1f}s exceeds "
                                f"bound {max_stale_s:.1f}s (§7 H)")
        authoritative = (
            not local_missing
            and not remote_only
            and not mismatches
            and not stale_detail
        )
        result_outcome = Outcome.CONFIRMED if authoritative else Outcome.UNKNOWN
        details: list[str] = []
        if local_missing:
            details.append(f"local missing remotely: {local_missing} (§7 B)")
        if remote_only:
            details.append(f"remote missing locally: {remote_only} (§7 C)")
        if mismatches:
            details.append(f"status mismatch: {mismatches} (§7 D)")
        if stale_detail:
            details.append(stale_detail)
        if not details:
            details.append("exact match (§7 A)")
        self._log(Op.READ_OPEN_ORDERS, normalized, "", result_outcome.value,
                   attempts, None,
                   {"reconciliation_result": result_outcome.value,
                    "detail": "; ".join(details)})
        return ReconciliationResult(
            authoritative=authoritative,
            outcome=result_outcome,
            matched=tuple(matched),
            local_missing_remotely=tuple(local_missing),
            remote_missing_locally=tuple(remote_only),
            status_mismatches=tuple(mismatches),
            duplicates=(),
            details=tuple(details),
        )

    # -- clock skew (§10) -------------------------------------------------------
    def sync_clock_skew(self, samples: int = 3) -> ClockSkewReport:
        """Measure the bounded local-minus-server offset (§10).

        Never mutates the OS clock.  Each sample is trusted only when it is
        a positive integer within 24h of local time (absurd values are
        refused); a failing or malformed server-time response settles
        ``established=False`` so the caller fails closed.  The averaged
        offset must stay within ``max_skew_ms`` or the sync fails closed.
        """
        if samples < 1:
            return ClockSkewReport(established=False,
                                   detail="samples must be >= 1")
        now_ms = int(time.time() * 1000)
        lo_bound = now_ms - 86_400_000
        hi_bound = now_ms + 86_400_000
        offsets: list[int] = []
        for _ in range(samples):
            try:
                server_ms, _local = self._client.server_time()
            except BinanceTestnetError as exc:
                self._log(Op.READ_SERVER_TIME, "", "",
                          Outcome.UNKNOWN.value, len(offsets) + 1,
                          type(exc).__name__)
                return ClockSkewReport(
                    established=False, samples=len(offsets) + 1,
                    detail=f"server-time endpoint unavailable: {type(exc).__name__}")
            if not isinstance(server_ms, int) or isinstance(server_ms, bool) \
                    or server_ms <= 0:
                return ClockSkewReport(
                    established=False, samples=len(offsets) + 1,
                    detail="malformed server-time value")
            if server_ms < lo_bound or server_ms > hi_bound:
                return ClockSkewReport(
                    established=False, samples=len(offsets) + 1,
                    detail="absurd server-time value refused (§10)")
            offsets.append(server_ms - now_ms)
        offset = int(round(sum(offsets) / len(offsets)))
        if abs(offset) > self._max_skew_ms:
            self._log(Op.READ_SERVER_TIME, "", "", Outcome.UNKNOWN.value,
                      len(offsets), "skew_out_of_bounds")
            return ClockSkewReport(
                established=False, offset_ms=offset, samples=len(offsets),
                detail=f"|skew| {abs(offset)}ms exceeds bound "
                       f"{self._max_skew_ms}ms")
        self._log(Op.READ_SERVER_TIME, "", "", Outcome.CONFIRMED.value,
                   len(offsets), None, {"offset_ms": offset})
        return ClockSkewReport(established=True, offset_ms=offset,
                               samples=len(offsets))


def _dry_cancel_executor(symbol: str, client_order_id: str) -> CancelVerdict:
    """Default cancel executor: no network, deterministic, UNRECONCILED.

    Real testnet cancel execution is a separate, explicitly authorized
    capability (Round 6A §18/§19).  Until then the seam stays dry and
    fail-closed: it records intent, never assumes cancellation succeeded.
    """
    return CancelVerdict.UNRECONCILED
