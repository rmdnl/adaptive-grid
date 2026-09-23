"""Phase 6A tests for the Binance Spot Testnet read-only adapter.

All tests use mocked SDK responses — no internet dependency.
"""
from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from binance_testnet import (
    BinanceTestnetConfig,
    BinanceTestnetClient,
    BinanceTestnetConfigError,
    BinanceTestnetEnvironmentError,
    BinanceTestnetNetworkError,
    BinanceTestnetAuthenticationError,
    BinanceTestnetResponseError,
    BinanceTestnetValidationError,
    BinanceAccountSnapshot,
    BinanceAccountBalance,
    BinanceOpenOrderSnapshot,
    BinanceSymbolSnapshot,
    BinanceTickerSnapshot,
    assert_testnet_read_only,
    load_testnet_config_from_env,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _valid_config(**overrides) -> BinanceTestnetConfig:
    base = dict(
        environment="testnet",
        base_url="https://testnet.binance.vision",
        api_key="test-key",
        api_secret="test-secret",
        dry_run=True,
        allow_live_execution=False,
    )
    base.update(overrides)
    return BinanceTestnetConfig(**base)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def data(self):
        return self._payload


def _make_client(config=None, rest_overrides=None):
    """Build a BinanceTestnetClient with mocked rest_api methods."""
    cfg = config or _valid_config()
    client = BinanceTestnetClient.__new__(BinanceTestnetClient)
    client._config = cfg
    rest = SimpleNamespace(**(rest_overrides or {}))
    client._spot = SimpleNamespace(rest_api=rest)
    return client


def _exchange_info_payload(symbol="BNBUSDT", status="TRADING"):
    return {
        "symbols": [
            {
                "symbol": symbol,
                "baseAsset": "BNB",
                "quoteAsset": "USDT",
                "status": status,
                "permissions": ["SPOT"],
                "filters": [
                    {"filterType": "PRICE_FILTER",
                     "minPrice": "0.01", "maxPrice": "100000", "tickSize": "0.01"},
                    {"filterType": "LOT_SIZE",
                     "minQty": "0.001", "maxQty": "10000", "stepSize": "0.001"},
                    {"filterType": "MARKET_LOT_SIZE",
                     "minQty": "0.001", "maxQty": "5000", "stepSize": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                    {"filterType": "NOTIONAL",
                     "minNotional": "10", "maxNotional": "100000"},
                    {"filterType": "PERCENT_PRICE",
                     "multiplierUp": "1.05", "multiplierDown": "0.95",
                     "avgPriceMins": 5},
                    {"filterType": "MAX_NUM_ORDERS", "maxNumOrders": 40},
                    {"filterType": "MAX_NUM_ALGO_ORDERS", "maxNumAlgoOrders": 10},
                    {"filterType": "UNKNOWN_CUSTOM_FILTER", "custom": "value"},
                ],
            }
        ]
    }


def _account_payload():
    return {
        "balances": [
            {"asset": "BNB", "free": "1.50000000", "locked": "0.00000000"},
            {"asset": "USDT", "free": "500.25000000", "locked": "10.50000000"},
            {"asset": "BTC", "free": "0.01000000", "locked": "0.00000000"},
        ]
    }


def _order_payload(order_id=1, client_id="grid-1", **overrides):
    order = {
        "symbol": "BNBUSDT",
        "orderId": order_id,
        "clientOrderId": client_id,
        "price": "350.50000000",
        "origQty": "1.50000000",
        "executedQty": "0.25000000",
        "status": "NEW",
        "timeInForce": "GTC",
        "type": "LIMIT",
        "side": "BUY",
        "isWorking": True,
    }
    order.update(overrides)
    return order


# ===========================================================================
# A. Valid testnet configuration
# ===========================================================================
def test_valid_testnet_configuration():
    cfg = _valid_config()
    assert cfg.environment == "testnet"
    assert cfg.base_url == "https://testnet.binance.vision"
    assert cfg.rest_base_path == "https://testnet.binance.vision/api"


# ===========================================================================
# B. Production URL rejected
# ===========================================================================
def test_production_url_rejected():
    with pytest.raises(BinanceTestnetEnvironmentError):
        _valid_config(base_url="https://api.binance.com")
    with pytest.raises(BinanceTestnetEnvironmentError):
        _valid_config(base_url="https://fapi.binance.com")
    with pytest.raises(BinanceTestnetEnvironmentError):
        _valid_config(base_url="https://testnet.binancefuture.com")
    with pytest.raises(BinanceTestnetEnvironmentError):
        _valid_config(base_url="https://api-gcp.binance.com")
    with pytest.raises(BinanceTestnetEnvironmentError):
        _valid_config(base_url="https://sapi.binance.com")


def test_futures_endpoint_string_rejected():
    with pytest.raises(BinanceTestnetEnvironmentError):
        _valid_config(base_url="https://testnet.binancefuture.com/api")


# ===========================================================================
# C. Missing environment rejected
# ===========================================================================
def test_missing_environment_rejected():
    with pytest.raises(BinanceTestnetConfigError):
        _valid_config(environment="")


# ===========================================================================
# D. DRY_RUN=false rejected
# ===========================================================================
def test_dry_run_false_rejected():
    with pytest.raises(BinanceTestnetConfigError):
        _valid_config(dry_run=False)


# ===========================================================================
# E. ALLOW_LIVE_EXECUTION=true rejected
# ===========================================================================
def test_allow_live_execution_true_rejected():
    with pytest.raises(BinanceTestnetConfigError):
        _valid_config(allow_live_execution=True)


def test_assert_testnet_read_only_passes_on_valid():
    assert_testnet_read_only(_valid_config())


def test_assert_testnet_read_only_fails_on_dry_run_false():
    with pytest.raises(BinanceTestnetConfigError):
        assert_testnet_read_only(_valid_config(dry_run=False))


def test_assert_testnet_read_only_fails_on_live_true():
    with pytest.raises(BinanceTestnetConfigError):
        assert_testnet_read_only(_valid_config(allow_live_execution=True))


# ===========================================================================
# F. Ping success
# ===========================================================================
def test_ping_success():
    client = _make_client(rest_overrides={
        "ping": lambda: None,
    })
    result = client.ping()
    assert result is True


# ===========================================================================
# G. Ping network failure
# ===========================================================================
def test_ping_network_failure():
    def _fail():
        raise OSError("connection refused")
    client = _make_client(rest_overrides={"ping": _fail})
    with pytest.raises(BinanceTestnetNetworkError):
        client.ping()


# ===========================================================================
# H. Server time success
# ===========================================================================
def test_server_time_success():
    expected_ms = 1756000000000
    client = _make_client(rest_overrides={
        "time": lambda: _FakeResponse({"serverTime": expected_ms}),
    })
    server_ms, local_ms = client.server_time()
    assert server_ms == expected_ms
    assert isinstance(local_ms, int)
    assert local_ms > 0


# ===========================================================================
# I. Server time malformed
# ===========================================================================
def test_server_time_malformed():
    for bad in ("not-an-int", -1, 0, 3.14, None):
        client = _make_client(rest_overrides={
            "time": lambda b=bad: _FakeResponse({"serverTime": b}),
        })
        with pytest.raises((BinanceTestnetResponseError, BinanceTestnetValidationError)):
            client.server_time()


# ===========================================================================
# J. exchangeInfo valid
# ===========================================================================
def test_exchange_info_valid():
    client = _make_client(rest_overrides={
        "exchange_info": lambda symbol=None: _FakeResponse(
            _exchange_info_payload(symbol or "BNBUSDT")
        ),
    })
    info = client.exchange_info("BNBUSDT")
    assert info["symbol"] == "BNBUSDT"
    assert info["baseAsset"] == "BNB"


# ===========================================================================
# K. exchangeInfo symbol missing
# ===========================================================================
def test_exchange_info_symbol_missing():
    client = _make_client(rest_overrides={
        "exchange_info": lambda symbol=None: _FakeResponse({"symbols": []}),
    })
    with pytest.raises(BinanceTestnetValidationError):
        client.exchange_info("BNBUSDT")


def test_exchange_info_mismatched_symbol_rejected():
    client = _make_client(rest_overrides={
        "exchange_info": lambda symbol=None: _FakeResponse(
            _exchange_info_payload(symbol="ETHUSDT")
        ),
    })
    with pytest.raises(BinanceTestnetValidationError):
        client.exchange_info("BNBUSDT")


# ===========================================================================
# L. exchangeInfo malformed filter (critical missing)
# ===========================================================================
def test_exchange_info_missing_price_filter_rejected():
    payload = _exchange_info_payload()
    payload["symbols"][0]["filters"] = [
        f for f in payload["symbols"][0]["filters"]
        if f["filterType"] != "PRICE_FILTER"
    ]
    client = _make_client(rest_overrides={
        "exchange_info": lambda symbol=None: _FakeResponse(payload),
    })
    with pytest.raises(BinanceTestnetValidationError, match="PRICE_FILTER"):
        client.symbol_snapshot("BNBUSDT")


def test_exchange_info_missing_lot_size_rejected():
    payload = _exchange_info_payload()
    payload["symbols"][0]["filters"] = [
        f for f in payload["symbols"][0]["filters"]
        if f["filterType"] != "LOT_SIZE"
    ]
    client = _make_client(rest_overrides={
        "exchange_info": lambda symbol=None: _FakeResponse(payload),
    })
    with pytest.raises(BinanceTestnetValidationError, match="LOT_SIZE"):
        client.symbol_snapshot("BNBUSDT")


# ===========================================================================
# M. Ticker valid
# ===========================================================================
def test_ticker_valid():
    client = _make_client(rest_overrides={
        "ticker_price": lambda symbol=None: _FakeResponse(
            {"symbol": "BNBUSDT", "price": "350.12345678"}
        ),
    })
    snap = client.ticker_price("BNBUSDT")
    assert snap.symbol == "BNBUSDT"
    assert snap.price == Decimal("350.12345678")
    assert isinstance(snap.fetched_at, datetime)


# ===========================================================================
# N/O/P. Ticker zero / negative / malformed rejected
# ===========================================================================
def test_ticker_zero_rejected():
    client = _make_client(rest_overrides={
        "ticker_price": lambda symbol=None: _FakeResponse(
            {"symbol": "BNBUSDT", "price": "0"}
        ),
    })
    with pytest.raises(BinanceTestnetValidationError):
        client.ticker_price("BNBUSDT")


def test_ticker_negative_rejected():
    client = _make_client(rest_overrides={
        "ticker_price": lambda symbol=None: _FakeResponse(
            {"symbol": "BNBUSDT", "price": "-1.0"}
        ),
    })
    with pytest.raises(BinanceTestnetValidationError):
        client.ticker_price("BNBUSDT")


def test_ticker_malformed_rejected():
    client = _make_client(rest_overrides={
        "ticker_price": lambda symbol=None: _FakeResponse(
            {"symbol": "BNBUSDT", "price": "not-a-number"}
        ),
    })
    with pytest.raises(BinanceTestnetValidationError):
        client.ticker_price("BNBUSDT")


def test_ticker_symbol_mismatch_rejected():
    client = _make_client(rest_overrides={
        "ticker_price": lambda symbol=None: _FakeResponse(
            {"symbol": "ETHUSDT", "price": "100"}
        ),
    })
    with pytest.raises(BinanceTestnetValidationError):
        client.ticker_price("BNBUSDT")


# ===========================================================================
# Q. Account valid
# ===========================================================================
def test_account_valid():
    client = _make_client(rest_overrides={
        "get_account": lambda omit_zero_balances=None: _FakeResponse(_account_payload()),
    })
    snap = client.account()
    assert isinstance(snap, BinanceAccountSnapshot)
    bnB = next(b for b in snap.balances if b.asset == "BNB")
    usdt = next(b for b in snap.balances if b.asset == "USDT")
    assert bnB.free == Decimal("1.50000000")
    assert usdt.locked == Decimal("10.50000000")
    assert isinstance(snap.fetched_at, datetime)


# ===========================================================================
# R. Account malformed
# ===========================================================================
def test_account_malformed():
    for bad_payload in (None, "not-dict", {"balances": "not-list"}):
        client = _make_client(rest_overrides={
            "get_account": lambda omit_zero_balances=None, p=bad_payload: _FakeResponse(p),
        })
        with pytest.raises((BinanceTestnetResponseError, BinanceTestnetValidationError)):
            client.account()


# ===========================================================================
# S. Account negative balance rejected
# ===========================================================================
def test_account_negative_balance_rejected():
    payload = {"balances": [
        {"asset": "BNB", "free": "-1.0", "locked": "0.0"},
        {"asset": "USDT", "free": "100.0", "locked": "0.0"},
    ]}
    client = _make_client(rest_overrides={
        "get_account": lambda omit_zero_balances=None: _FakeResponse(payload),
    })
    with pytest.raises(BinanceTestnetValidationError):
        client.account()


def test_account_negative_locked_rejected():
    payload = {"balances": [
        {"asset": "BNB", "free": "1.0", "locked": "-0.5"},
    ]}
    client = _make_client(rest_overrides={
        "get_account": lambda omit_zero_balances=None: _FakeResponse(payload),
    })
    with pytest.raises(BinanceTestnetValidationError):
        client.account()


# ===========================================================================
# T. Open orders empty (genuine [])
# ===========================================================================
def test_open_orders_empty_genuine():
    client = _make_client(rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse([]),
    })
    assert client.open_orders("BNBUSDT") == ()


# ===========================================================================
# U. Open orders valid
# ===========================================================================
def test_open_orders_valid():
    payload = [
        _order_payload(order_id=1, client_id="grid-1"),
        _order_payload(order_id=2, client_id="grid-2",
                        side="SELL", status="PARTIALLY_FILLED",
                        executedQty="1.00000000"),
    ]
    client = _make_client(rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse(payload),
    })
    orders = client.open_orders("BNBUSDT")
    assert len(orders) == 2
    assert orders[0].side == "BUY"
    assert orders[0].status == "NEW"
    assert orders[1].status == "PARTIALLY_FILLED"
    assert orders[0].price == Decimal("350.50000000")
    assert orders[0].orig_qty == Decimal("1.50000000")
    assert orders[0].executed_qty == Decimal("0.25000000")


# ===========================================================================
# V. Open orders wrong symbol rejected
# ===========================================================================
def test_open_orders_wrong_symbol_rejected():
    client = _make_client(rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse([
            _order_payload(symbol="ETHUSDT"),
        ]),
    })
    with pytest.raises(BinanceTestnetValidationError, match="symbol mismatch"):
        client.open_orders("BNBUSDT")


# ===========================================================================
# W. Duplicate orderId rejected
# ===========================================================================
def test_open_orders_duplicate_order_id_rejected():
    payload = [
        _order_payload(order_id=1, client_id="grid-1"),
        _order_payload(order_id=1, client_id="grid-2"),
    ]
    client = _make_client(rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse(payload),
    })
    with pytest.raises(BinanceTestnetValidationError, match="Duplicate orderId"):
        client.open_orders("BNBUSDT")


# ===========================================================================
# X. Duplicate clientOrderId rejected
# ===========================================================================
def test_open_orders_duplicate_client_order_id_rejected():
    payload = [
        _order_payload(order_id=1, client_id="grid-1"),
        _order_payload(order_id=2, client_id="grid-1"),
    ]
    client = _make_client(rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse(payload),
    })
    with pytest.raises(BinanceTestnetValidationError, match="Duplicate clientOrderId"):
        client.open_orders("BNBUSDT")


# ===========================================================================
# Y. Invalid quantity rejected
# ===========================================================================
def test_open_orders_invalid_quantity_rejected():
    bad_cases = [
        {"origQty": "0"},
        {"origQty": "-1"},
        {"executedQty": "2"},  # executed > orig
        {"price": "0"},        # limit-style with zero price
    ]
    for overrides in bad_cases:
        client = _make_client(rest_overrides={
            "get_open_orders": lambda symbol=None, o=overrides: _FakeResponse([
                _order_payload(**o),
            ]),
        })
        with pytest.raises(BinanceTestnetValidationError):
            client.open_orders("BNBUSDT")


def test_open_orders_invalid_status_rejected():
    client = _make_client(rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse([
            _order_payload(status="FILLED"),
        ]),
    })
    with pytest.raises(BinanceTestnetValidationError):
        client.open_orders("BNBUSDT")


def test_open_orders_invalid_side_rejected():
    client = _make_client(rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse([
            _order_payload(side="HOLD"),
        ]),
    })
    with pytest.raises(BinanceTestnetValidationError):
        client.open_orders("BNBUSDT")


# ===========================================================================
# Z. Open orders API failure does NOT become empty list
# ===========================================================================
def test_open_orders_api_failure_not_empty_list():
    def _fail(symbol=None):
        raise OSError("network down")
    client = _make_client(rest_overrides={"get_open_orders": _fail})
    with pytest.raises(BinanceTestnetNetworkError):
        client.open_orders("BNBUSDT")


def test_open_orders_non_list_response_rejected():
    client = _make_client(rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse({"orders": []}),
    })
    with pytest.raises(BinanceTestnetResponseError):
        client.open_orders("BNBUSDT")


# ===========================================================================
# AA. Symbol snapshot deterministic
# ===========================================================================
def test_symbol_snapshot_deterministic():
    client = _make_client(rest_overrides={
        "exchange_info": lambda symbol=None: _FakeResponse(
            _exchange_info_payload(symbol or "BNBUSDT")
        ),
    })
    snap1 = client.symbol_snapshot("BNBUSDT")
    snap2 = client.symbol_snapshot("BNBUSDT")
    assert snap1.symbol == snap2.symbol == "BNBUSDT"
    assert snap1.base_asset == "BNB"
    assert snap1.quote_asset == "USDT"
    assert snap1.status == "TRADING"
    assert "PRICE_FILTER" in snap1.filters
    assert "LOT_SIZE" in snap1.filters
    assert "MIN_NOTIONAL" in snap1.filters
    assert "NOTIONAL" in snap1.filters
    assert "PERCENT_PRICE" in snap1.filters
    assert "MAX_NUM_ORDERS" in snap1.filters
    assert "MAX_NUM_ALGO_ORDERS" in snap1.filters
    assert snap1.filters == snap2.filters


def test_symbol_snapshot_preserves_unknown_filter():
    client = _make_client(rest_overrides={
        "exchange_info": lambda symbol=None: _FakeResponse(
            _exchange_info_payload(symbol or "BNBUSDT")
        ),
    })
    snap = client.symbol_snapshot("BNBUSDT")
    assert "UNKNOWN_CUSTOM_FILTER" in snap.filters


# ===========================================================================
# AB. Connectivity check success
# ===========================================================================
def _connectivity_pass_client(symbol="BNBUSDT"):
    return _make_client(rest_overrides={
        "ping": lambda: None,
        "time": lambda: _FakeResponse(
            {"serverTime": 1756000000000}
        ),
        "exchange_info": lambda symbol=None: _FakeResponse(
            _exchange_info_payload(symbol=symbol or "BNBUSDT")
        ),
        "ticker_price": lambda symbol=None: _FakeResponse(
            {"symbol": "BNBUSDT", "price": "350.50"}
        ),
        "get_account": lambda omit_zero_balances=None: _FakeResponse(_account_payload()),
        "get_open_orders": lambda symbol=None: _FakeResponse([]),
    })


def test_connectivity_check_success():
    client = _connectivity_pass_client()
    snap = client.connectivity_check("BNBUSDT")
    assert snap.overall == "PASS"
    assert snap.environment == "testnet"
    assert snap.base_url == "https://testnet.binance.vision"
    assert snap.symbol == "BNBUSDT"
    assert snap.symbol_rules_valid is True
    assert snap.account_available is True
    assert snap.open_order_count == 0
    assert snap.ticker_price == "350.50"
    assert snap.server_time_ms == 1756000000000
    assert snap.skew_ms == snap.server_time_ms - snap.local_time_ms


# ===========================================================================
# AC. Connectivity check fails closed
# ===========================================================================
def test_connectivity_check_fails_closed_on_ping_failure():
    def _fail():
        raise OSError("down")
    client = _make_client(rest_overrides={"ping": _fail})
    snap = client.connectivity_check("BNBUSDT")
    assert snap.overall == "FAIL"
    assert "PING_FAILED" in snap.reason


def test_connectivity_check_fails_closed_on_ticker_failure():
    client = _make_client(rest_overrides={
        "ping": lambda: None,
        "time": lambda: _FakeResponse({"serverTime": 1756000000000}),
        "exchange_info": lambda symbol=None: _FakeResponse(
            _exchange_info_payload(symbol=symbol or "BNBUSDT")
        ),
        "ticker_price": lambda symbol=None: (_ for _ in ()).throw(
            BinanceTestnetNetworkError("ticker endpoint unreachable")
        ),
        "get_account": lambda omit_zero_balances=None: _FakeResponse(_account_payload()),
        "get_open_orders": lambda symbol=None: _FakeResponse([]),
    })
    snap = client.connectivity_check("BNBUSDT")
    assert snap.overall == "FAIL"
    assert "TICKER_FAILED" in snap.reason


# ===========================================================================
# AD. Credentials never appear in exception/log output
# ===========================================================================
def test_credentials_never_in_exception_output():
    secret = "very-secret-key-12345"
    client = _make_client(config=_valid_config(api_key="my-key-abc",
                                                api_secret=secret),
                          rest_overrides={
        "ping": lambda: (_ for _ in ()).throw(
            BinanceTestnetNetworkError("network failure")
        ),
    })
    with pytest.raises(BinanceTestnetNetworkError) as excinfo:
        client.ping()
    text = str(excinfo.value)
    assert secret not in text
    assert "my-key-abc" not in text


def test_credentials_never_in_config_validation_output():
    secret = "super-secret-999"
    try:
        _valid_config(api_secret=secret, dry_run=False)
    except BinanceTestnetConfigError as exc:
        assert secret not in str(exc)


# ===========================================================================
# AE. Adapter exposes no trading methods
# ===========================================================================
def test_adapter_exposes_no_trading_methods():
    forbidden = [
        "create_order", "cancel_order", "amend_order",
        "new_order", "batch_orders", "batchOrders",
        "place_order", "submit_order", "execute_order",
        "cancelAll", "cancel_all",
    ]
    for method in forbidden:
        assert not hasattr(BinanceTestnetClient, method), (
            f"BinanceTestnetClient must not expose {method}"
        )


def test_adapter_instance_has_no_trading_methods():
    client = _connectivity_pass_client()
    forbidden = [
        "create_order", "cancel_order", "amend_order",
        "new_order", "batch_orders", "batchOrders",
    ]
    for method in forbidden:
        assert not hasattr(client, method)


# ===========================================================================
# AF. Production endpoint string rejected (already covered in B)
# ===========================================================================
def test_production_endpoint_string_rejected():
    with pytest.raises(BinanceTestnetEnvironmentError):
        _valid_config(base_url="https://api.binance.com")


# ===========================================================================
# AG. Futures endpoint rejected
# ===========================================================================
def test_futures_endpoint_rejected():
    for url in ("https://fapi.binance.com", "https://testnet.binancefuture.com"):
        with pytest.raises(BinanceTestnetEnvironmentError):
            _valid_config(base_url=url)


# ===========================================================================
# AH. Margin endpoint rejected
# ===========================================================================
def test_margin_endpoint_rejected():
    for url in ("https://sapi.binance.com", "https://api.binance.com/margin"):
        with pytest.raises(BinanceTestnetEnvironmentError):
            _valid_config(base_url=url)


# ===========================================================================
# AI. Withdrawal endpoint rejected (sub-path of approved base is also rejected
#     because base_url must be EXACTLY the approved testnet root)
# ===========================================================================
def test_withdrawal_endpoint_rejected():
    # sub-paths of the approved testnet base are rejected (strict base_url match)
    for url in (
        "https://testnet.binance.vision/sapi/v1/capital/withdraw",
        "https://api.binance.com/sapi/v1/capital/withdraw",
        "https://sapi.binance.com/sapi/v1/capital/withdraw",
    ):
        with pytest.raises(BinanceTestnetEnvironmentError):
            _valid_config(base_url=url)


# ===========================================================================
# AJ. Unknown filter preserved (also in AA, repeated here for spec coverage)
# ===========================================================================
def test_unknown_filter_preserved():
    payload = _exchange_info_payload()
    payload["symbols"][0]["filters"].append(
        {"filterType": "SOME_NEW_FILTER", "someKey": "someVal"}
    )
    client = _make_client(rest_overrides={
        "exchange_info": lambda symbol=None: _FakeResponse(payload),
    })
    snap = client.symbol_snapshot("BNBUSDT")
    assert "SOME_NEW_FILTER" in snap.filters
    assert snap.filters["SOME_NEW_FILTER"]["someKey"] == "someVal"


# ===========================================================================
# Integration: existing model compatibility
# ===========================================================================
def test_binance_ticker_can_feed_fresh_ticker_validation():
    from market_data import is_ticker_fresh, TickerSnapshot
    client = _make_client(rest_overrides={
        "ticker_price": lambda symbol=None: _FakeResponse(
            {"symbol": "BNBUSDT", "price": "350.50"}
        ),
    })
    bnb_snap = client.ticker_price("BNBUSDT")
    # Map to existing TickerSnapshot
    existing = TickerSnapshot(
        symbol=bnb_snap.symbol,
        price=bnb_snap.price,
        fetched_at=bnb_snap.fetched_at,
    )
    assert is_ticker_fresh(existing, max_age_seconds=10) is True


def test_binance_account_snapshot_maps_to_existing_account_snapshot():
    from market_data import AccountSnapshot
    client = _make_client(rest_overrides={
        "get_account": lambda omit_zero_balances=None: _FakeResponse(_account_payload()),
    })
    bnb_snap = client.account()
    bnB = next(b for b in bnb_snap.balances if b.asset == "BNB")
    usdt = next(b for b in bnb_snap.balances if b.asset == "USDT")
    existing = AccountSnapshot(
        base_asset="BNB",
        base_free=bnB.free,
        base_locked=bnB.locked,
        quote_asset="USDT",
        quote_free=usdt.free,
        quote_locked=usdt.locked,
        fetched_at=bnb_snap.fetched_at,
    )
    assert existing.base_total == bnB.free + bnB.locked
    assert existing.quote_total == usdt.free + usdt.locked


def test_binance_open_orders_feed_existing_reconciliation():
    from market_data import OpenOrder
    payload = [_order_payload()]
    client = _make_client(rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse(payload),
    })
    orders = client.open_orders("BNBUSDT")
    existing = OpenOrder(
        order_id=orders[0].order_id,
        client_order_id=orders[0].client_order_id,
        symbol=orders[0].symbol,
        side=orders[0].side,
        order_type=orders[0].order_type,
        status=orders[0].status,
        price=orders[0].price,
        orig_qty=orders[0].orig_qty,
        executed_qty=orders[0].executed_qty,
        time_in_force=orders[0].time_in_force,
        is_working=orders[0].is_working,
    )
    assert existing.status == "NEW"
    assert existing.price == Decimal("350.50000000")


def test_binance_symbol_filters_feed_existing_order_plan_validation():
    from symbol_rules import parse_symbol_info, validate_quantized_order_plan
    from grid_engine import GridLevel
    client = _make_client(rest_overrides={
        "exchange_info": lambda symbol=None: _FakeResponse(
            _exchange_info_payload(symbol or "BNBUSDT")
        ),
    })
    snap = client.symbol_snapshot("BNBUSDT")
    # parse_symbol_info consumes raw exchangeInfo dict (authoritative Phase 1 validator)
    rules = parse_symbol_info(snap.raw_exchange_info)
    assert rules.tick_size == Decimal("0.01")
    assert rules.min_notional == Decimal("10")
    levels = [GridLevel(0, Decimal("350.00")), GridLevel(1, Decimal("352.10"))]
    plan = validate_quantized_order_plan(
        levels, rules, "50", "350", "0.001", "0.001", "0.0005", "0.003", 40
    )
    assert plan is not None


# ===========================================================================
# load_testnet_config_from_env
# ===========================================================================
def test_load_config_from_env_valid(monkeypatch):
    monkeypatch.setenv("BINANCE_ENV", "testnet")
    monkeypatch.setenv("BINANCE_BASE_URL", "https://testnet.binance.vision")
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("ALLOW_LIVE_EXECUTION", "false")
    cfg = load_testnet_config_from_env()
    assert cfg.environment == "testnet"
    assert cfg.dry_run is True
    assert cfg.allow_live_execution is False


def test_load_config_from_env_rejects_production_url(monkeypatch):
    monkeypatch.setenv("BINANCE_ENV", "testnet")
    monkeypatch.setenv("BINANCE_BASE_URL", "https://api.binance.com")
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("ALLOW_LIVE_EXECUTION", "false")
    with pytest.raises(BinanceTestnetEnvironmentError):
        load_testnet_config_from_env()


def test_load_config_from_env_missing_env(monkeypatch):
    for var in ("BINANCE_ENV", "BINANCE_BASE_URL", "BINANCE_API_KEY",
                "BINANCE_API_SECRET", "DRY_RUN", "ALLOW_LIVE_EXECUTION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BINANCE_ENV", "")
    with pytest.raises(BinanceTestnetConfigError):
        load_testnet_config_from_env()


# ===========================================================================
# 🔒 Phase 6A hardening regression tests
# ===========================================================================

# --- Issue 1: connectivity_check fails closed on account/open_orders ---

def test_connectivity_fails_closed_on_account_auth_error():
    """If account() raises AuthenticationError, connectivity_check must FAIL."""
    client = _make_client(rest_overrides={
        "ping": lambda: None,
        "time": lambda: _FakeResponse({"serverTime": 1756000000000}),
        "exchange_info": lambda symbol=None: _FakeResponse(
            _exchange_info_payload(symbol="BNBUSDT")
        ),
        "ticker_price": lambda symbol=None: _FakeResponse(
            {"symbol": "BNBUSDT", "price": "350.50"}
        ),
        "get_account": lambda omit_zero_balances=None: (_ for _ in ()).throw(
            BinanceTestnetAuthenticationError("invalid signature")
        ),
        "get_open_orders": lambda symbol=None: _FakeResponse([]),
    })
    snap = client.connectivity_check("BNBUSDT")
    assert snap.overall == "FAIL"
    assert "ACCOUNT_AUTH_FAILED" in snap.reason
    assert snap.account_available is False


def test_connectivity_fails_closed_on_account_network_error():
    """If account() raises any non-auth exception, connectivity_check must FAIL."""
    client = _make_client(rest_overrides={
        "ping": lambda: None,
        "time": lambda: _FakeResponse({"serverTime": 1756000000000}),
        "exchange_info": lambda symbol=None: _FakeResponse(
            _exchange_info_payload(symbol="BNBUSDT")
        ),
        "ticker_price": lambda symbol=None: _FakeResponse(
            {"symbol": "BNBUSDT", "price": "350.50"}
        ),
        "get_account": lambda omit_zero_balances=None: (_ for _ in ()).throw(
            BinanceTestnetResponseError("unexpected JSON")
        ),
        "get_open_orders": lambda symbol=None: _FakeResponse([]),
    })
    snap = client.connectivity_check("BNBUSDT")
    assert snap.overall == "FAIL"
    assert "ACCOUNT_FAILED" in snap.reason


def test_connectivity_fails_closed_on_open_orders_auth_error():
    """If open_orders() raises AuthenticationError, connectivity_check must FAIL."""
    client = _make_client(rest_overrides={
        "ping": lambda: None,
        "time": lambda: _FakeResponse({"serverTime": 1756000000000}),
        "exchange_info": lambda symbol=None: _FakeResponse(
            _exchange_info_payload(symbol="BNBUSDT")
        ),
        "ticker_price": lambda symbol=None: _FakeResponse(
            {"symbol": "BNBUSDT", "price": "350.50"}
        ),
        "get_account": lambda omit_zero_balances=None: _FakeResponse(_account_payload()),
        "get_open_orders": lambda symbol=None: (_ for _ in ()).throw(
            BinanceTestnetAuthenticationError("signature verification failed: invalid api_key")
        ),
    })
    snap = client.connectivity_check("BNBUSDT")
    assert snap.overall == "FAIL"
    assert "OPEN_ORDERS_AUTH_FAILED" in snap.reason


def test_connectivity_fails_closed_on_open_orders_network_error():
    """If open_orders() raises non-auth exception, connectivity_check must FAIL."""
    client = _make_client(rest_overrides={
        "ping": lambda: None,
        "time": lambda: _FakeResponse({"serverTime": 1756000000000}),
        "exchange_info": lambda symbol=None: _FakeResponse(
            _exchange_info_payload(symbol="BNBUSDT")
        ),
        "ticker_price": lambda symbol=None: _FakeResponse(
            {"symbol": "BNBUSDT", "price": "350.50"}
        ),
        "get_account": lambda omit_zero_balances=None: _FakeResponse(_account_payload()),
        "get_open_orders": lambda symbol=None: (_ for _ in ()).throw(
            BinanceTestnetResponseError("bad payload")
        ),
    })
    snap = client.connectivity_check("BNBUSDT")
    assert snap.overall == "FAIL"
    assert "OPEN_ORDERS_FAILED" in snap.reason


# --- Issue 2: _parse_open_order hardens quantity/price checks ---

def test_parse_open_order_rejects_negative_executed_qty():
    client = _make_client(rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse([
            _order_payload(executedQty="-0.5"),
        ]),
    })
    with pytest.raises(BinanceTestnetValidationError, match="non-finite|Negative|invalid"):
        client.open_orders("BNBUSDT")


def test_parse_open_order_rejects_negative_price():
    client = _make_client(rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse([
            _order_payload(price="-1.0"),
        ]),
    })
    with pytest.raises(BinanceTestnetValidationError):
        client.open_orders("BNBUSDT")


def test_parse_open_order_rejects_limit_with_zero_price():
    """Limit-style orders require price > 0 (existing behavior, confirmed)."""
    client = _make_client(rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse([
            _order_payload(price="0", type="LIMIT"),
        ]),
    })
    with pytest.raises(BinanceTestnetValidationError, match="invalid price"):
        client.open_orders("BNBUSDT")


def test_parse_open_order_rejects_executed_exceeds_orig():
    client = _make_client(rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse([
            _order_payload(origQty="1.0", executedQty="2.0"),
        ]),
    })
    with pytest.raises(BinanceTestnetValidationError, match="exceeds origQty"):
        client.open_orders("BNBUSDT")


# --- Issue 3: load_testnet_config_from_env strict validation ---

def test_load_config_dry_run_case_insensitive(monkeypatch):
    for val in ("TRUE", "True", "true"):
        monkeypatch.setenv("DRY_RUN", val)
        monkeypatch.setenv("ALLOW_LIVE_EXECUTION", "false")
        _set_env_basics(monkeypatch)
        cfg = load_testnet_config_from_env()
        assert cfg.dry_run is True


def test_load_config_dry_run_invalid_rejected(monkeypatch):
    _set_env_basics(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "yes")
    monkeypatch.setenv("ALLOW_LIVE_EXECUTION", "false")
    with pytest.raises(BinanceTestnetConfigError, match="DRY_RUN"):
        load_testnet_config_from_env()


def test_load_config_allow_live_invalid_rejected(monkeypatch):
    _set_env_basics(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("ALLOW_LIVE_EXECUTION", "nope")
    with pytest.raises(BinanceTestnetConfigError, match="ALLOW_LIVE_EXECUTION"):
        load_testnet_config_from_env()


def test_load_config_timeout_not_integer(monkeypatch):
    _set_env_basics(monkeypatch)
    monkeypatch.setenv("BINANCE_TIMEOUT_MS", "abc")
    with pytest.raises(BinanceTestnetConfigError, match="BINANCE_TIMEOUT_MS"):
        load_testnet_config_from_env()


def test_load_config_timeout_out_of_range(monkeypatch):
    _set_env_basics(monkeypatch)
    monkeypatch.setenv("BINANCE_TIMEOUT_MS", "0")
    with pytest.raises(BinanceTestnetConfigError, match="BINANCE_TIMEOUT_MS"):
        load_testnet_config_from_env()


def test_load_config_timeout_exceeds_max(monkeypatch):
    _set_env_basics(monkeypatch)
    monkeypatch.setenv("BINANCE_TIMEOUT_MS", "30001")
    with pytest.raises(BinanceTestnetConfigError, match="30000"):
        load_testnet_config_from_env()


def test_load_config_retries_negative(monkeypatch):
    _set_env_basics(monkeypatch)
    monkeypatch.setenv("BINANCE_RETRIES", "-1")
    with pytest.raises(BinanceTestnetConfigError, match="BINANCE_RETRIES"):
        load_testnet_config_from_env()


def test_load_config_backoff_negative(monkeypatch):
    _set_env_basics(monkeypatch)
    monkeypatch.setenv("BINANCE_BACKOFF_MS", "-100")
    with pytest.raises(BinanceTestnetConfigError, match="BINANCE_BACKOFF_MS"):
        load_testnet_config_from_env()


def _set_env_basics(monkeypatch):
    """Set all required env vars to valid values."""
    monkeypatch.setenv("BINANCE_ENV", "testnet")
    monkeypatch.setenv("BINANCE_BASE_URL", "https://testnet.binance.vision")
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("ALLOW_LIVE_EXECUTION", "false")


# --- Issue 4: credential sanitization in exception messages ---

def test_network_error_message_sanitized_on_api_key():
    """SDK exception containing 'api_key' must be redacted."""
    client = _make_client(rest_overrides={
        "ping": lambda: (_ for _ in ()).throw(Exception(
            "Request failed: api_key=ABC123XYZ unauthorized"
        )),
    })
    with pytest.raises(BinanceTestnetAuthenticationError) as excinfo:
        client.ping()
    assert "ABC123XYZ" not in str(excinfo.value)


def test_network_error_message_sanitized_on_api_secret():
    """SDK exception containing 'api_secret' must be redacted."""
    client = _make_client(rest_overrides={
        "ping": lambda: (_ for _ in ()).throw(Exception(
            "Invalid signature: api_secret=my-secret-here"
        )),
    })
    with pytest.raises(BinanceTestnetAuthenticationError) as excinfo:
        client.ping()
    assert "my-secret-here" not in str(excinfo.value)


def test_network_error_message_preserves_non_credential_errors():
    """SDK exception without credential keywords must pass through unchanged."""
    client = _make_client(rest_overrides={
        "ping": lambda: (_ for _ in ()).throw(Exception("connection refused")),
    })
    with pytest.raises(BinanceTestnetNetworkError) as excinfo:
        client.ping()
    assert "connection refused" in str(excinfo.value)
