"""Round 7 tests for the gated testnet order-path client.

All tests use mocked SDK responses — no internet dependency.  The suite
pins the fail-closed semantics of every write outcome (CONFIRMED ack,
deterministic rejection, UNKNOWN transport failure), the explicit
orders_enabled gate, the minimal method surface, and the seam cancel
executor contract (never raises, never confirms an unproven cancel).
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

import testnet_orders as to
from binance_testnet import (
    BinanceTestnetConfigError,
    BinanceTestnetEnvironmentError,
    BinanceTestnetError,
    BinanceTestnetNetworkError,
    BinanceTestnetRateLimitError,
    BinanceTestnetResponseError,
    BinanceTestnetValidationError,
)
from rest_reconciler import CancelVerdict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _valid_config(**overrides):
    base = dict(
        environment="testnet",
        base_url="https://testnet.binance.vision",
        api_key="test-key",
        api_secret="test-secret",
        dry_run=True,
        allow_live_execution=False,
    )
    base.update(overrides)
    return to.BinanceTestnetConfig(**base)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def data(self):
        return self._payload


def _make_client(orders_enabled=True, rest_overrides=None):
    """Build an order client with mocked rest_api (skips SDK construction)."""
    client = to.BinanceTestnetOrderClient.__new__(to.BinanceTestnetOrderClient)
    client._config = _valid_config()
    rest = SimpleNamespace(**(rest_overrides or {}))
    client._spot = SimpleNamespace(rest_api=rest)
    return client


def _new_order_payload(order_id=99, client_id="AGTV-1", status="NEW",
                       side="BUY", **overrides):
    payload = {
        "symbol": "BNBUSDT",
        "orderId": order_id,
        "orderListId": -1,
        "clientOrderId": client_id,
        "transactTime": 1759480000000,
        "price": "600.50000000",
        "origQty": "0.02000000",
        "executedQty": "0.00000000",
        "cummulativeQuoteQty": "0.00000000",
        "status": status,
        "timeInForce": "GTC",
        "type": "LIMIT_MAKER",
        "side": side,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# A. Explicit gate
# ---------------------------------------------------------------------------
def test_gate_env_default_is_disabled():
    import os
    saved = os.environ.pop("TESTNET_ORDERS_ENABLED", None)
    try:
        assert to.load_testnet_orders_enabled_from_env() is False
    finally:
        if saved is not None:
            os.environ["TESTNET_ORDERS_ENABLED"] = saved


def test_gate_env_strict_parsing(monkeypatch):
    monkeypatch.setenv("TESTNET_ORDERS_ENABLED", "true")
    assert to.load_testnet_orders_enabled_from_env() is True
    monkeypatch.setenv("TESTNET_ORDERS_ENABLED", "false")
    assert to.load_testnet_orders_enabled_from_env() is False
    for bad in ("1", "yes", "enable", "on"):
        monkeypatch.setenv("TESTNET_ORDERS_ENABLED", bad)
        with pytest.raises(BinanceTestnetConfigError):
            to.load_testnet_orders_enabled_from_env()


def test_constructor_requires_explicit_enabled_flag():
    with pytest.raises(BinanceTestnetConfigError, match="DISABLED"):
        to.BinanceTestnetOrderClient(_valid_config(), orders_enabled=False)
    # Non-bool "truthy" values must not smuggle the gate open.
    with pytest.raises(BinanceTestnetConfigError, match="DISABLED"):
        to.BinanceTestnetOrderClient(_valid_config(), orders_enabled=1)


def test_constructor_refuses_production_base_url():
    with pytest.raises(BinanceTestnetEnvironmentError):
        to.BinanceTestnetOrderClient(
            _valid_config(base_url="https://api.binance.com"),
            orders_enabled=True,
        )


def test_constructor_refuses_futures_base_url():
    with pytest.raises(BinanceTestnetEnvironmentError):
        to.BinanceTestnetOrderClient(
            _valid_config(base_url="https://testnet.binancefuture.com"),
            orders_enabled=True,
        )


def test_constructor_refuses_live_execution_enabled():
    with pytest.raises(BinanceTestnetConfigError):
        to.BinanceTestnetOrderClient(
            _valid_config(allow_live_execution=True), orders_enabled=True
        )


def test_constructor_refuses_dry_run_false():
    with pytest.raises(BinanceTestnetConfigError):
        to.BinanceTestnetOrderClient(
            _valid_config(dry_run=False), orders_enabled=True
        )


def test_constructor_refuses_non_testnet_environment():
    with pytest.raises(BinanceTestnetConfigError):
        to.BinanceTestnetOrderClient(
            _valid_config(environment="live"), orders_enabled=True
        )


# ---------------------------------------------------------------------------
# B. Minimal surface (read-path invariant preserved)
# ---------------------------------------------------------------------------
def test_order_client_exposes_no_extra_trading_methods():
    forbidden = [
        "new_order", "create_order", "place_order", "submit_order",
        "batch_orders", "batchOrders", "cancel_open_orders", "cancelAll",
        "order_oco", "order_list_oco", "sor_order", "order_cancel_replace",
        "withdraw", "margin", "futures", "get_my_trades", "my_trades",
        "account", "open_orders", "exchange_info",
    ]
    for method in forbidden:
        assert not hasattr(to.BinanceTestnetOrderClient, method), (
            f"BinanceTestnetOrderClient must not expose {method}"
        )


# ---------------------------------------------------------------------------
# C. Placement validation and outcomes
# ---------------------------------------------------------------------------
def test_place_happy_path_returns_validated_ack():
    calls = []

    def fake_new_order(**kwargs):
        calls.append(kwargs)
        return _FakeResponse(_new_order_payload(client_id="AGTV-1"))

    client = _make_client(rest_overrides={"new_order": fake_new_order})
    ack = client.place_limit_maker_order(
        "bnbusdt", "buy", Decimal("0.020"), Decimal("600.50"), "AGTV-1"
    )
    assert ack.symbol == "BNBUSDT"
    assert ack.client_order_id == "AGTV-1"
    assert ack.side == "BUY"
    assert ack.order_type == "LIMIT_MAKER"
    assert ack.status == "NEW"
    assert ack.price == Decimal("600.50000000")
    assert ack.orig_qty == Decimal("0.02000000")
    assert ack.executed_qty == Decimal("0.00000000")
    assert ack.order_id == 99
    assert ack.transact_time == 1759480000000
    # Exact decimal strings on the wire — no float conversion.
    assert calls[0]["quantity"] == "0.02"
    assert calls[0]["price"] == "600.5"
    assert calls[0]["type"] == "LIMIT_MAKER"
    assert calls[0]["new_order_resp_type"] == "RESULT"


def test_place_input_validation_makes_no_http_call():
    def fail_new_order(**kwargs):
        raise AssertionError("HTTP call must not happen for invalid input")

    client = _make_client(rest_overrides={"new_order": fail_new_order})
    with pytest.raises(BinanceTestnetValidationError):
        client.place_limit_maker_order(
            "BNBUSDT", "LONG", Decimal("1"), Decimal("100"), "AGTV-1"
        )
    with pytest.raises(BinanceTestnetValidationError):
        client.place_limit_maker_order(
            "BNBUSDT", "BUY", Decimal("0"), Decimal("100"), "AGTV-1"
        )
    with pytest.raises(BinanceTestnetValidationError):
        client.place_limit_maker_order(
            "BNBUSDT", "BUY", Decimal("1"), Decimal("-1"), "AGTV-1"
        )
    with pytest.raises(BinanceTestnetValidationError):
        client.place_limit_maker_order(
            "BNBUSDT", "BUY", Decimal("NaN"), Decimal("100"), "AGTV-1"
        )
    with pytest.raises(BinanceTestnetValidationError):
        client.place_limit_maker_order(
            "BNBUSDT", "BUY", Decimal("1"), Decimal("100"), "bad cid!"
        )
    with pytest.raises(BinanceTestnetValidationError):
        client.place_limit_maker_order(
            "BNBUSDT", "BUY", Decimal("1"), Decimal("100"), "x" * 37
        )


def test_place_symbol_mismatch_fails_closed():
    client = _make_client(rest_overrides={
        "new_order": lambda **kw: _FakeResponse(
            _new_order_payload(symbol="ETHUSDT"))
    })
    with pytest.raises(BinanceTestnetResponseError):
        client.place_limit_maker_order(
            "BNBUSDT", "BUY", Decimal("1"), Decimal("100"), "AGTV-1"
        )


def test_place_client_id_mismatch_fails_closed():
    client = _make_client(rest_overrides={
        "new_order": lambda **kw: _FakeResponse(
            _new_order_payload(client_id="DIFFERENT"))
    })
    with pytest.raises(BinanceTestnetResponseError):
        client.place_limit_maker_order(
            "BNBUSDT", "BUY", Decimal("1"), Decimal("100"), "AGTV-1"
        )


def test_place_unknown_status_fails_closed():
    client = _make_client(rest_overrides={
        "new_order": lambda **kw: _FakeResponse(
            _new_order_payload(status="PENDING_CANCEL"))
    })
    with pytest.raises(BinanceTestnetResponseError):
        client.place_limit_maker_order(
            "BNBUSDT", "BUY", Decimal("1"), Decimal("100"), "AGTV-1"
        )


def test_place_executed_exceeds_orig_fails_closed():
    client = _make_client(rest_overrides={
        "new_order": lambda **kw: _FakeResponse(
            _new_order_payload(executedQty="0.03000000"))
    })
    with pytest.raises(BinanceTestnetValidationError):
        client.place_limit_maker_order(
            "BNBUSDT", "BUY", Decimal("1"), Decimal("100"), "AGTV-1"
        )


def test_place_non_object_response_fails_closed():
    client = _make_client(rest_overrides={
        "new_order": lambda **kw: _FakeResponse(["not", "an", "object"])
    })
    with pytest.raises(BinanceTestnetResponseError):
        client.place_limit_maker_order(
            "BNBUSDT", "BUY", Decimal("1"), Decimal("100"), "AGTV-1"
        )


def test_place_exchange_rejection_is_deterministic():
    """A 400-family rejection with an exchange code is FAILED, not UNKNOWN."""
    from binance_sdk_spot import BadRequestError

    def reject(**kwargs):
        raise BadRequestError(
            error_message="Order would immediately match and take.",
            status_code=-2010,
        )

    client = _make_client(rest_overrides={"new_order": reject})
    with pytest.raises(to.BinanceTestnetOrderRejectedError) as excinfo:
        client.place_limit_maker_order(
            "BNBUSDT", "BUY", Decimal("1"), Decimal("100"), "AGTV-1"
        )
    assert excinfo.value.code == -2010
    assert excinfo.value.ambiguous is False
    # The raw exchange message is not echoed (conservative redaction);
    # no credential material either way.
    assert "test-key" not in str(excinfo.value)
    assert "test-secret" not in str(excinfo.value)


def test_place_transport_failure_is_unknown_network_error():
    class FakeTimeout(Exception):
        pass

    def timeout(**kwargs):
        raise FakeTimeout("Connection reset by peer")

    client = _make_client(rest_overrides={"new_order": timeout})
    with pytest.raises(BinanceTestnetNetworkError):
        client.place_limit_maker_order(
            "BNBUSDT", "BUY", Decimal("1"), Decimal("100"), "AGTV-1"
        )


def test_place_rate_limit_keeps_retry_after():
    from binance_sdk_spot import TooManyRequestsError

    def limited(**kwargs):
        raise TooManyRequestsError(
            error_message="Too many requests", status_code=429, retry_after=7
        )

    client = _make_client(rest_overrides={"new_order": limited})
    with pytest.raises(BinanceTestnetRateLimitError) as excinfo:
        client.place_limit_maker_order(
            "BNBUSDT", "BUY", Decimal("1"), Decimal("100"), "AGTV-1"
        )
    assert excinfo.value.retry_after_s == 7


def test_place_timestamp_skew_classified():
    from binance_sdk_spot import BadRequestError

    def skew(**kwargs):
        raise BadRequestError(
            error_message=(
                "Timestamp for this request was 1000ms earlier than our time."
            ),
            status_code=-1021,
        )

    client = _make_client(rest_overrides={"new_order": skew})
    with pytest.raises(BinanceTestnetNetworkError) as excinfo:
        client.place_limit_maker_order(
            "BNBUSDT", "BUY", Decimal("1"), Decimal("100"), "AGTV-1"
        )
    assert type(excinfo.value).__name__ == "BinanceTestnetTimestampError"


# ---------------------------------------------------------------------------
# D. Cancellation outcomes
# ---------------------------------------------------------------------------
def test_cancel_happy_path_confirms_canceled():
    def fake_delete(**kwargs):
        assert kwargs["symbol"] == "BNBUSDT"
        assert kwargs["orig_client_order_id"] == "AGTV-1"
        return _FakeResponse(_new_order_payload(status="CANCELED"))

    client = _make_client(rest_overrides={"delete_order": fake_delete})
    ack = client.cancel_order_by_client_id("BNBUSDT", "AGTV-1")
    assert ack.status == "CANCELED"


def test_cancel_unknown_order_is_ambiguous_not_confirmed():
    from binance_sdk_spot import BadRequestError

    def reject(**kwargs):
        raise BadRequestError(
            error_message="Unknown order sent.", status_code=-2011
        )

    client = _make_client(rest_overrides={"delete_order": reject})
    with pytest.raises(to.BinanceTestnetOrderRejectedError) as excinfo:
        client.cancel_order_by_client_id("BNBUSDT", "AGTV-1")
    assert excinfo.value.ambiguous is True


def test_cancel_non_canceled_status_fails_closed():
    client = _make_client(rest_overrides={
        "delete_order": lambda **kw: _FakeResponse(
            _new_order_payload(status="NEW"))
    })
    with pytest.raises(BinanceTestnetResponseError):
        client.cancel_order_by_client_id("BNBUSDT", "AGTV-1")


def test_cancel_transport_failure_is_unknown():
    def timeout(**kwargs):
        raise Exception("read timed out")

    client = _make_client(rest_overrides={"delete_order": timeout})
    with pytest.raises(BinanceTestnetNetworkError):
        client.cancel_order_by_client_id("BNBUSDT", "AGTV-1")


# ---------------------------------------------------------------------------
# E. Seam cancel executor contract
# ---------------------------------------------------------------------------
def test_executor_confirms_only_validated_ack():
    client = _make_client(rest_overrides={
        "delete_order": lambda **kw: _FakeResponse(
            _new_order_payload(status="CANCELED"))
    })
    executor = client.make_cancel_executor()
    assert executor("BNBUSDT", "AGTV-1") is CancelVerdict.CONFIRMED_CANCELED


def test_executor_ambiguous_rejection_settles_unreconciled():
    from binance_sdk_spot import BadRequestError

    def reject(**kwargs):
        raise BadRequestError(
            error_message="Unknown order sent.", status_code=-2011
        )

    client = _make_client(rest_overrides={"delete_order": reject})
    executor = client.make_cancel_executor()
    assert executor("BNBUSDT", "AGTV-1") is CancelVerdict.UNRECONCILED


def test_executor_ambiguous_plus_authoritative_canceled_confirms():
    """§5: a lost cancel ack is settled by an authoritative re-query."""
    from binance_sdk_spot import BadRequestError

    def reject(**kwargs):
        raise BadRequestError(
            error_message="Unknown order sent.", status_code=-2011
        )

    client = _make_client(rest_overrides={"delete_order": reject})
    executor = client.make_cancel_executor(
        resolver=lambda symbol, cid: "CANCELED"
    )
    assert executor("BNBUSDT", "AGTV-1") is CancelVerdict.CONFIRMED_CANCELED


def test_executor_ambiguous_plus_filled_stays_unreconciled():
    """A fill racing the cancel is NOT a confirmed cancel (inventory!)."""
    from binance_sdk_spot import BadRequestError

    def reject(**kwargs):
        raise BadRequestError(
            error_message="Unknown order sent.", status_code=-2011
        )

    client = _make_client(rest_overrides={"delete_order": reject})
    for status in ("FILLED", "PARTIALLY_FILLED", "EXPIRED", "REJECTED", None):
        executor = client.make_cancel_executor(
            resolver=lambda symbol, cid, _s=status: _s
        )
        assert executor("BNBUSDT", "AGTV-1") is CancelVerdict.UNRECONCILED, (
            f"status={status!r} must not confirm a cancel"
        )


def test_executor_ambiguous_plus_failing_resolver_stays_unreconciled():
    from binance_sdk_spot import BadRequestError

    def reject(**kwargs):
        raise BadRequestError(
            error_message="Unknown order sent.", status_code=-2011
        )

    def bad_resolver(symbol, cid):
        raise BinanceTestnetNetworkError("query failed")

    client = _make_client(rest_overrides={"delete_order": reject})
    executor = client.make_cancel_executor(resolver=bad_resolver)
    assert executor("BNBUSDT", "AGTV-1") is CancelVerdict.UNRECONCILED


def test_executor_deterministic_rejection_settles_unreconciled():
    from binance_sdk_spot import BadRequestError

    def reject(**kwargs):
        raise BadRequestError(
            error_message="Unknown order sent.", status_code=-2011
        )

    client = _make_client(rest_overrides={"delete_order": reject})
    assert client.make_cancel_executor()("BNBUSDT", "AGTV-1") is \
        CancelVerdict.UNRECONCILED


def test_executor_network_failure_settles_unreconciled():
    def timeout(**kwargs):
        raise Exception("connection reset")

    client = _make_client(rest_overrides={"delete_order": timeout})
    assert client.make_cancel_executor()("BNBUSDT", "AGTV-1") is \
        CancelVerdict.UNRECONCILED


def test_executor_never_raises_on_surprise_exception():
    class Boom(Exception):
        pass

    def explode(**kwargs):
        raise Boom("unexpected internal error")

    client = _make_client(rest_overrides={"delete_order": explode})
    assert client.make_cancel_executor()("BNBUSDT", "AGTV-1") is \
        CancelVerdict.UNRECONCILED


def test_executor_integrates_with_rest_reconciler_verification():
    """Executor CONFIRMED + authoritative re-query ⇒ CONFIRMED_CANCELED."""
    from rest_reconciler import RestReconciler

    client = _make_client(rest_overrides={
        "delete_order": lambda **kw: _FakeResponse(
            _new_order_payload(status="CANCELED")),
        "get_order": lambda symbol, orig_client_order_id: (
            _new_order_payload(status="CANCELED")
        ),
    })
    executor = client.make_cancel_executor()

    reconciler = RestReconciler(
        SimpleNamespace(_config=client._config),
        cancel_executor=executor,
        sleep=lambda _s: None,
    )
    reconciler._client = SimpleNamespace(
        _config=client._config,
        get_order=lambda symbol, cid: _new_order_payload(status="CANCELED"),
        open_orders=lambda symbol: (),
    )
    record = reconciler.cancel("BNBUSDT", "AGTV-1", verify=True)
    assert record.verdict is CancelVerdict.CONFIRMED_CANCELED
    assert record.reconciled is True


def test_executor_plus_unverifiable_requery_settles_unreconciled():
    """Cancel ack but re-query fails ⇒ UNRECONCILED (kill stays, §5)."""
    from rest_reconciler import RestReconciler

    client = _make_client(rest_overrides={
        "delete_order": lambda **kw: _FakeResponse(
            _new_order_payload(status="CANCELED")),
    })
    executor = client.make_cancel_executor()

    class FailClient:
        _config = client._config

        def get_order(self, symbol, cid):
            raise BinanceTestnetNetworkError("query failed")

        def open_orders(self, symbol):
            raise BinanceTestnetNetworkError("snapshot failed")

    reconciler = RestReconciler(
        FailClient(), cancel_executor=executor, sleep=lambda _s: None
    )
    record = reconciler.cancel("BNBUSDT", "AGTV-1", verify=True)
    assert record.verdict is CancelVerdict.UNRECONCILED


# ---------------------------------------------------------------------------
# F. Credential redaction on write errors
# ---------------------------------------------------------------------------
def test_auth_failure_message_is_redacted():
    def reject(**kwargs):
        raise Exception(
            "403 Forbidden: invalid signature for api_key=test-key "
            "secret=test-secret"
        )

    client = _make_client(rest_overrides={"new_order": reject})
    with pytest.raises(BinanceTestnetError) as excinfo:
        client.place_limit_maker_order(
            "BNBUSDT", "BUY", Decimal("1"), Decimal("100"), "AGTV-1"
        )
    assert "test-key" not in str(excinfo.value)
    assert "test-secret" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# G. Decimal string serialization
# ---------------------------------------------------------------------------
def test_dec_str_never_uses_scientific_notation():
    assert to._dec_str(Decimal("0.00000123")) == "0.00000123"
    assert to._dec_str(Decimal("1E+2")) == "100"
    assert to._dec_str(Decimal("600.50000000")) == "600.5"
    assert to._dec_str(Decimal("0.020")) == "0.02"
