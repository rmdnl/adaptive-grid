from decimal import Decimal
from types import SimpleNamespace

import pytest

from market_data import (
    OpenOrdersRequestError,
    OpenOrdersValidationError,
    fetch_open_orders,
)
from risk_engine import open_orders_available_gate, open_orders_gate
from storage import get_state, init_db, set_state


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def data(self):
        return self.payload


def _client(payload=None, error=None):
    calls = []

    def get_open_orders(**kwargs):
        calls.append(kwargs)
        if error:
            raise error
        return FakeResponse(payload)

    client = SimpleNamespace(rest_api=SimpleNamespace(get_open_orders=get_open_orders))
    client.calls = calls
    return client


def _order(**overrides):
    order = {
        "symbol": "BTCUSDT", "orderId": 123, "clientOrderId": "grid-123",
        "price": "100.125", "origQty": "1.500", "executedQty": "0.250",
        "status": "NEW", "timeInForce": "GTC", "type": "LIMIT",
        "side": "BUY", "isWorking": True,
    }
    order.update(overrides)
    return order


def test_valid_empty_open_order_response_is_verified_and_not_assumed():
    client = _client([])

    assert fetch_open_orders(client, "btcusdt") == ()
    assert client.calls == [{"symbol": "BTCUSDT"}]


def test_valid_one_order_parses_decimal_fields_and_new_status():
    orders = fetch_open_orders(_client([_order()]), "BTCUSDT")

    assert len(orders) == 1
    assert orders[0].status == "NEW"
    assert orders[0].price == Decimal("100.125")
    assert orders[0].orig_qty == Decimal("1.500")
    assert orders[0].executed_qty == Decimal("0.250")


def test_valid_multiple_orders_and_partially_filled_status_are_accepted():
    orders = fetch_open_orders(_client([
        _order(), _order(orderId=124, clientOrderId="grid-124", side="SELL",
                          status="PARTIALLY_FILLED", executedQty="1.0"),
    ]), "BTCUSDT")

    assert len(orders) == 2
    assert orders[1].status == "PARTIALLY_FILLED"


@pytest.mark.parametrize(("change", "message"), [
    ({"origQty": "-1"}, "origQty"),
    ({"executedQty": "2"}, "exceeds"),
    ({"price": "0"}, "invalid price"),
    ({"orderId": None}, "Missing orderId"),
    ({"clientOrderId": ""}, "Missing clientOrderId"),
    ({"side": "HOLD"}, "Invalid side"),
    ({"status": "FILLED"}, "Unexpected"),
])
def test_malformed_open_order_fails_closed(change, message):
    with pytest.raises(OpenOrdersValidationError, match=message):
        fetch_open_orders(_client([_order(**change)]), "BTCUSDT")


def test_duplicate_order_id_fails_closed():
    payload = [_order(), _order(clientOrderId="grid-124")]
    with pytest.raises(OpenOrdersValidationError, match="Duplicate orderId"):
        fetch_open_orders(_client(payload), "BTCUSDT")


def test_duplicate_client_order_id_fails_closed():
    payload = [_order(), _order(orderId=124)]
    with pytest.raises(OpenOrdersValidationError, match="Duplicate clientOrderId"):
        fetch_open_orders(_client(payload), "BTCUSDT")


def test_wrong_symbol_response_fails_closed():
    with pytest.raises(OpenOrdersValidationError, match="symbol mismatch"):
        fetch_open_orders(_client([_order(symbol="ETHUSDT")]), "BTCUSDT")


def test_api_failure_is_not_converted_to_empty_list():
    with pytest.raises(OpenOrdersRequestError, match="request failed"):
        fetch_open_orders(_client(error=OSError("offline")), "BTCUSDT")


@pytest.mark.parametrize("payload", [None, {}, "not a list", [_order(), "bad"]])
def test_malformed_top_level_response_fails_closed(payload):
    with pytest.raises(OpenOrdersValidationError):
        fetch_open_orders(_client(payload), "BTCUSDT")


def test_successful_count_is_used_by_open_order_risk_gate():
    orders = fetch_open_orders(_client([_order(), _order(orderId=124, clientOrderId="grid-124")]), "BTCUSDT")

    assert open_orders_available_gate(True).allowed
    assert open_orders_gate(len(orders), 3).allowed
    assert open_orders_gate(len(orders), 2).reason == "MAX_OPEN_ORDERS_REACHED"


def test_reconciliation_failure_fails_closed_even_when_previous_state_exists(tmp_path):
    db = tmp_path / "state.sqlite3"
    init_db(str(db))
    set_state(str(db), "last_open_order_reconciliation", {"count": 0, "status": "VERIFIED"})

    with pytest.raises(OpenOrdersRequestError):
        fetch_open_orders(_client(error=OSError("offline")), "BTCUSDT")

    # The reconciliation function does not read storage; stale verified state
    # therefore cannot replace the current failed Binance response.
    assert get_state(str(db), "last_open_order_reconciliation") is not None
    assert open_orders_available_gate(False).reason == "OPEN_ORDERS_UNAVAILABLE"
