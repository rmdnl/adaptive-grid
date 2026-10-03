"""Round 7 — gated Binance Spot TESTNET order-path capability.

Extends the read-only Phase 6A adapter with the two write capabilities the
testnet phase requires — LIMIT_MAKER order placement and order cancellation —
while keeping every isolation barrier intact:

* **Testnet only.**  Construction re-asserts the adapter barrier
  (``environment=testnet``, ``dry_run=true``, ``allow_live_execution=false``,
  base URL exactly ``https://testnet.binance.vision``).  Production and
  futures endpoints are rejected at construction time.
* **Explicit gate.**  The write path is DISABLED unless an explicit
  ``orders_enabled=True`` is passed (env ``TESTNET_ORDERS_ENABLED=true``,
  strict true/false, default **false**).  ``DRY_RUN``/``ALLOW_LIVE_EXECUTION``
  semantics are unchanged: live execution remains structurally impossible and
  the ``main()`` dry-run cycle still never places a real order.
* **Minimal surface.**  Exactly two economic capabilities exist:
  ``place_limit_maker_order`` (POST-only, maker-only — an order that would
  cross the book is rejected by the exchange, never repriced) and
  ``cancel_order_by_client_id``.  No market orders, no OCO/algo/SOR orders,
  no batch endpoints, no withdrawal — those methods do not exist here.
* **Deterministic outcomes (Round 6A §3 semantics).**  Every exchange
  interaction settles to CONFIRMED (validated authoritative ack),
  UNKNOWN (network/timeout/rate-limit — never assumed safe, never blindly
  retried: the SDK itself never retries POST), or a deterministic exchange
  rejection (validated 400-family error with the Binance error code — the
  order was NOT accepted).  A cancel that reports "unknown order" is
  AMBIGUOUS on Binance Spot (the order may have filled first) and is never
  reported as a confirmed cancel.
* **Precision.**  Quantities and prices are ``Decimal`` end to end and are
  sent as exact decimal strings (no float conversion).
* **Secrets.**  Credentials never appear in exceptions or logs — the
  adapter's redaction layer is applied to every error message.

The real cancel executor for the Round 6A ``RestReconciler`` seam is
provided by ``make_seam_cancel_executor``: it confirms a cancellation ONLY
on a validated CANCELED ack and settles every other outcome to
``UNRECONCILED`` without exception (fail closed, §5).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

import binance_testnet as _bt
from binance_testnet import (
    BinanceTestnetConfig,
    BinanceTestnetConfigError,
    BinanceTestnetError,
    BinanceTestnetResponseError,
    BinanceTestnetValidationError,
    _model_to_plain,
    _parse_order_status,
    _raise_network_or_auth,
    assert_testnet_read_only,
)
from rest_reconciler import CancelVerdict

try:
    from binance_sdk_spot.spot import Spot, ConfigurationRestAPI
    from binance_sdk_spot import BadRequestError as _SDK_BadRequestError
except ImportError:  # pragma: no cover - SDK is a hard dependency when used
    Spot = ConfigurationRestAPI = None  # type: ignore[assignment,misc]
    _SDK_BadRequestError = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Explicit order-path gate
# ---------------------------------------------------------------------------
#: The testnet order path is disabled unless explicitly enabled.
DEFAULT_TESTNET_ORDERS_ENABLED = False

#: Environment variable that must be exactly "true" to enable the write path.
ORDERS_ENABLED_ENV = "TESTNET_ORDERS_ENABLED"


def load_testnet_orders_enabled_from_env() -> bool:
    """Strict boolean load of the order-path gate.  Unset ⇒ disabled."""
    raw = os.environ.get(ORDERS_ENABLED_ENV, "").strip().lower()
    if not raw:
        return DEFAULT_TESTNET_ORDERS_ENABLED
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise BinanceTestnetConfigError(
        f"{ORDERS_ENABLED_ENV} must be 'true' or 'false' (got {raw!r})"
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class BinanceTestnetOrderError(BinanceTestnetError):
    """Base class for testnet order-path errors."""


class BinanceTestnetOrderRejectedError(BinanceTestnetOrderError):
    """Deterministic exchange rejection with the Binance error code.

    A 400-family response carrying an exchange error code is authoritative:
    a **placement** was NOT accepted (the order does not exist); a
    **cancel** was not accepted.  Rejections are never retried.

    ``ambiguous`` marks the cancel-side "unknown order / order does not
    exist" family (codes -2011 / -2013): on Binance Spot this means the
    order is not open — it may have FILLED first — so it is NOT proof that
    the order was canceled (Round 6A §3 ambiguity rule).  Callers must
    re-query authoritative state.
    """

    def __init__(self, message: str, *, code: Optional[int] = None,
                 ambiguous: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.ambiguous = ambiguous


# ---------------------------------------------------------------------------
# Ack snapshot
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BinanceOrderAck:
    """Validated, authoritative exchange response for one order action."""

    symbol: str
    order_id: int
    client_order_id: str
    side: str
    order_type: str
    status: str
    price: Decimal
    orig_qty: Decimal
    executed_qty: Decimal
    transact_time: Optional[int] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
#: Binance clientOrderId rule: ``^[\.A-Z\:/a-z0-9_-]{1,36}$``.
_CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,36}$")

#: Exchange error codes that mean "order not open anymore" on a cancel.
_AMBIGUOUS_CANCEL_CODES = frozenset({-2011, -2013})


def _dec_str(value: Decimal) -> str:
    """Exact, exponent-free decimal string for an exchange parameter."""
    d = Decimal(value)
    if not d.is_finite():
        raise BinanceTestnetValidationError(
            "Order parameter must be a finite number"
        )
    return format(d.normalize(), "f")


def _to_decimal(value: object, field: str) -> Decimal:
    """Parse a positive finite Decimal, failing closed."""
    if value is None or isinstance(value, bool):
        raise BinanceTestnetValidationError(f"{field} must be numeric")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BinanceTestnetValidationError(
            f"{field} is not a valid Decimal: {value!r}"
        ) from exc
    if not d.is_finite():
        raise BinanceTestnetValidationError(f"{field} must be finite")
    return d


def _validate_client_order_id(client_order_id: object) -> str:
    if not isinstance(client_order_id, str):
        raise BinanceTestnetValidationError("client_order_id must be a string")
    if not _CLIENT_ORDER_ID_RE.match(client_order_id):
        raise BinanceTestnetValidationError(
            "client_order_id must match ^[A-Za-z0-9._:/-]{1,36}$ "
            f"(got {len(client_order_id)} chars)"
        )
    return client_order_id


def _exchange_code(exc: Exception) -> Optional[int]:
    """Extract the Binance error code from a typed SDK 4xx error."""
    code = getattr(exc, "status_code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    raw = str(exc)
    match = re.search(r'"?code"?\s*[:=]\s*(-?\d+)', raw)
    if match:
        try:
            return int(match.group(1))
        except ValueError:  # pragma: no cover - regex guarantees digits
            return None
    return None


def _classify_write_error(exc: Exception, *, ambiguous_codes: frozenset) -> None:
    """Raise the deterministic adapter error for a failed WRITE call.

    Classification order (fail closed):

    1. already-typed adapter errors are re-raised untouched;
    2. a typed 400-family SDK error carrying an exchange error code is a
       deterministic rejection (``BinanceTestnetOrderRejectedError``);
       cancel-side "order not open" codes are additionally marked
       ``ambiguous`` (the order may have filled first);
    3. timestamp-skew and rate-limit rejections keep their dedicated typed
       errors via the shared adapter mapper;
    4. everything else is a transport failure (UNKNOWN semantics).
    """
    if isinstance(exc, BinanceTestnetError):
        raise exc
    raw_msg = str(exc).lower()
    is_bad_request = _is_bad_request(exc)
    if is_bad_request and any(
        k in raw_msg for k in _bt._TIMESTAMP_INDICATORS
    ):
        # Timestamp skew is retryable ONLY after a clock re-sync (§10) —
        # classify it before the deterministic-rejection branch.
        _raise_network_or_auth(exc)
    code = _exchange_code(exc)
    if code is not None and is_bad_request:
        # Deterministic 400-family rejection: the request was parsed and
        # refused by the exchange.  Never retried, never UNKNOWN.
        raise BinanceTestnetOrderRejectedError(
            f"exchange rejected request: code={code} (message redacted or "
            "non-credential)",
            code=code,
            ambiguous=code in ambiguous_codes,
        ) from exc
    _raise_network_or_auth(exc)


def _is_bad_request(exc: Exception) -> bool:
    return _SDK_BadRequestError is not None and isinstance(exc, _SDK_BadRequestError)


def _build_ack(payload: dict, symbol: str, client_order_id: str) -> BinanceOrderAck:
    """Validate a write-response payload and freeze it into an ack."""
    validated = _parse_order_status(payload, symbol, client_order_id)
    transact_time = payload.get("transactTime")
    if transact_time is not None:
        if isinstance(transact_time, bool) or not isinstance(transact_time, int) \
                or transact_time <= 0:
            raise BinanceTestnetValidationError(
                f"Invalid transactTime for {client_order_id!r}: {transact_time!r}"
            )
    side = payload.get("side")
    if not isinstance(side, str) or side.upper() not in ("BUY", "SELL"):
        raise BinanceTestnetValidationError(
            f"Invalid side in write response for {client_order_id!r}: {side!r}"
        )
    order_type = payload.get("type")
    if not isinstance(order_type, str) or not order_type.strip():
        raise BinanceTestnetValidationError(
            f"Missing type in write response for {client_order_id!r}"
        )
    return BinanceOrderAck(
        symbol=validated["symbol"],
        order_id=validated["orderId"],
        client_order_id=validated["clientOrderId"],
        side=side.upper(),
        order_type=order_type.upper(),
        status=validated["status"],
        price=Decimal(validated["price"]),
        orig_qty=Decimal(validated["origQty"]),
        executed_qty=Decimal(validated["executedQty"]),
        transact_time=transact_time,
    )


# ---------------------------------------------------------------------------
# The write-capable client
# ---------------------------------------------------------------------------
class BinanceTestnetOrderClient:
    """Gated write-capable Binance Spot TESTNET client (LIMIT_MAKER + cancel).

    The read-only ``BinanceTestnetClient`` stays untouched; this class is a
    separate surface so the "adapter exposes no trading methods" invariant
    keeps holding for the read path.
    """

    def __init__(self, config: BinanceTestnetConfig, *, orders_enabled: bool) -> None:
        # Re-assert the full adapter barrier: testnet URL, dry_run, no-live.
        assert_testnet_read_only(config)
        if orders_enabled is not True:
            raise BinanceTestnetConfigError(
                "testnet order path is DISABLED: construct with "
                "orders_enabled=True only after an explicit operator "
                f"decision ({ORDERS_ENABLED_ENV}=true)"
            )
        if Spot is None or ConfigurationRestAPI is None:
            raise BinanceTestnetConfigError("binance-sdk-spot is not installed")
        self._config = config
        cfg = ConfigurationRestAPI(
            api_key=config.api_key,
            api_secret=config.api_secret,
            base_path=config.rest_base_path,
            timeout=config.timeout_ms,
            retries=config.retries,
            backoff=config.backoff_ms,
        )
        self._spot = Spot(config_rest_api=cfg)

    # -- placement (LIMIT_MAKER only) ----------------------------------------
    def place_limit_maker_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        client_order_id: str,
    ) -> BinanceOrderAck:
        """Place one LIMIT_MAKER (post-only) order.  Single POST, no retry.

        The exchange rejects (never reprices) an order that would cross the
        book, so a confirmed ack is a resting maker order — or one that
        matched as a maker immediately (status NEW / PARTIALLY_FILLED /
        FILLED / EXPIRED are all known, validated states).

        A network failure settles UNKNOWN: the caller must resolve the
        deterministic ``client_order_id`` against the exchange before ANY
        resubmission decision (Round 6A §4; the SDK never retries POST).
        A 400-family rejection is deterministic (the order does not exist).
        """
        normalized = str(symbol).upper()
        side_u = str(side).upper()
        if side_u not in ("BUY", "SELL"):
            raise BinanceTestnetValidationError(f"side must be BUY or SELL: {side!r}")
        qty = _to_decimal(quantity, "quantity")
        px = _to_decimal(price, "price")
        if qty <= 0:
            raise BinanceTestnetValidationError("quantity must be > 0")
        if px <= 0:
            raise BinanceTestnetValidationError("price must be > 0")
        cid = _validate_client_order_id(client_order_id)
        try:
            resp = self._spot.rest_api.new_order(
                symbol=normalized,
                side=side_u,
                type="LIMIT_MAKER",
                quantity=_dec_str(qty),
                price=_dec_str(px),
                new_client_order_id=cid,
                new_order_resp_type="RESULT",
            )
        except Exception as exc:
            _classify_write_error(exc, ambiguous_codes=frozenset())
        payload = _model_to_plain(resp.data())
        if not isinstance(payload, dict):
            raise BinanceTestnetResponseError(
                "new_order response is not an object"
            )
        return _build_ack(payload, normalized, cid)

    # -- cancellation ---------------------------------------------------------
    def cancel_order_by_client_id(
        self, symbol: str, client_order_id: str
    ) -> BinanceOrderAck:
        """Cancel one order by deterministic clientOrderId.

        Returns a validated ack with status CANCELED.  The cancel-side
        "unknown order" family (-2011/-2013) is AMBIGUOUS (the order may
        have filled before the cancel arrived) and raises
        ``BinanceTestnetOrderRejectedError`` with ``ambiguous=True`` — never
        a confirmed cancellation.
        """
        normalized = str(symbol).upper()
        cid = _validate_client_order_id(client_order_id)
        try:
            resp = self._spot.rest_api.delete_order(
                symbol=normalized, orig_client_order_id=cid
            )
        except Exception as exc:
            _classify_write_error(exc, ambiguous_codes=_AMBIGUOUS_CANCEL_CODES)
        payload = _model_to_plain(resp.data())
        if not isinstance(payload, dict):
            raise BinanceTestnetResponseError(
                "cancel_order response is not an object"
            )
        ack = _build_ack(payload, normalized, cid)
        if ack.status != "CANCELED":
            # A cancel response must prove the canceled state; anything else
            # is an unreadable outcome (fail closed — never "assume gone").
            raise BinanceTestnetResponseError(
                f"cancel response status is not CANCELED for {cid!r}: "
                f"{ack.status!r}"
            )
        return ack

    # -- seam executor for RestReconciler.cancel ------------------------------
    def make_cancel_executor(
        self, resolver: Optional[Callable[[str, str], Optional[str]]] = None
    ) -> Callable[[str, str], CancelVerdict]:
        """Build the Round 6A §5 seam executor from this client.

        The executor NEVER raises and NEVER proves a cancel that did not
        happen: only a validated CANCELED ack — or the ambiguous
        "unknown order" family settled by an AUTHORITATIVE re-query
        returning CANCELED (§5: a lost cancel response is settled only by
        an authoritative re-query) — yields ``CONFIRMED_CANCELED``.  Every
        other outcome (fill racing the cancel, query failure, timeout,
        surprise exception) settles ``UNRECONCILED`` so the kill state
        stays active and the caller re-queries (§5/§20).

        ``resolver``: optional ``f(symbol, client_order_id) -> status|None``
        used ONLY to settle the ambiguous cancel family.  A ``None``
        resolver keeps the strictly-unreconciled behavior for ambiguity.
        """
        def _executor(symbol: str, client_order_id: str) -> CancelVerdict:
            try:
                self.cancel_order_by_client_id(symbol, client_order_id)
            except BinanceTestnetOrderRejectedError as exc:
                if exc.ambiguous and resolver is not None:
                    try:
                        status = resolver(symbol, client_order_id)
                    except Exception:
                        # §16: a failing re-query must not confirm anything.
                        return CancelVerdict.UNRECONCILED
                    if status == "CANCELED":
                        return CancelVerdict.CONFIRMED_CANCELED
                return CancelVerdict.UNRECONCILED
            except BinanceTestnetError:
                return CancelVerdict.UNRECONCILED
            except Exception:
                # §16 surprise-exception guard: never assume anything.
                return CancelVerdict.UNRECONCILED
            return CancelVerdict.CONFIRMED_CANCELED
        return _executor


# ---------------------------------------------------------------------------
# Convenience: enabled-from-env constructor
# ---------------------------------------------------------------------------
def load_order_client_from_env() -> BinanceTestnetOrderClient:
    """Build a write-capable client iff ``TESTNET_ORDERS_ENABLED=true``.

    Fail-closed: the env gate defaults to disabled and any value other than
    strict true/false is a configuration error.
    """
    return BinanceTestnetOrderClient(
        load_config_required(),
        orders_enabled=load_testnet_orders_enabled_from_env(),
    )


def load_config_required() -> BinanceTestnetConfig:
    """Re-exported env config load (keeps script imports one-level deep)."""
    return _bt.load_testnet_config_from_env()
