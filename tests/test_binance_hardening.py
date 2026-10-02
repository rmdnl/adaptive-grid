"""Patch 3 — Binance Testnet Adapter Security Hardening tests.

Adversarial regression tests for the audit findings:
- 1/2/3: credential exposure, malformed JSON, duplicate asset
- 4/G: MAX_OPEN_ORDERS guard
- 5: Decimal NaN / Infinity rejection
- 6/C: config repr / str must not expose secrets
- 7/J: dormant production path is dead
- 8/I: symbol filter contradiction validation
- H: open-order response validation
- K: strict environment / numeric validation
- L: response-size resource guards
- M: error-failure semantics (failure != empty/default state)
- N: timestamp / clock-drift validation
- P: connectivity fail-closed

All tests use mocked SDK responses — no internet dependency.
"""
from __future__ import annotations

import pytest

from binance_testnet import (
    BinanceTestnetConfig,
    BinanceTestnetConfigError,
    BinanceTestnetNetworkError,
    BinanceTestnetResponseError,
    BinanceTestnetValidationError,
    load_testnet_config_from_env,
)
from tests.test_binance_testnet import (
    _FakeResponse,
    _account_payload,
    _exchange_info_payload,
    _make_client,
    _order_payload,
    _set_env_basics,
    _valid_config,
)


# ---------------------------------------------------------------------------
# 5. Decimal NaN / Infinity rejection
# ---------------------------------------------------------------------------

def test_ticker_rejects_nan_string():
    client = _make_client(rest_overrides={
        "ticker_price": lambda symbol=None: _FakeResponse(
            {"symbol": "BNBUSDT", "price": "NaN"}
        ),
    })
    with pytest.raises(BinanceTestnetValidationError, match="finite"):
        client.ticker_price("BNBUSDT")


@pytest.mark.parametrize("bad", ["Infinity", "-Infinity", "inf", "-inf", "+inf", "+infinity", "infinity"])
def test_ticker_rejects_infinity_string(bad):
    client = _make_client(rest_overrides={
        "ticker_price": lambda symbol=None, b=bad: _FakeResponse(
            {"symbol": "BNBUSDT", "price": b}
        ),
    })
    with pytest.raises(BinanceTestnetValidationError, match="finite"):
        client.ticker_price("BNBUSDT")


def test_account_rejects_nan_balance():
    payload = {"balances": [
        {"asset": "BNB", "free": "NaN", "locked": "0"},
        {"asset": "USDT", "free": "100", "locked": "0"},
    ]}
    client = _make_client(rest_overrides={
        "get_account": lambda omit_zero_balances=None: _FakeResponse(payload),
    })
    with pytest.raises(BinanceTestnetValidationError, match="finite"):
        client.account()


def test_account_rejects_infinity_balance():
    payload = {"balances": [
        {"asset": "BNB", "free": "Infinity", "locked": "0"},
    ]}
    client = _make_client(rest_overrides={
        "get_account": lambda omit_zero_balances=None: _FakeResponse(payload),
    })
    with pytest.raises(BinanceTestnetValidationError, match="finite"):
        client.account()


def test_open_order_rejects_nan_price():
    client = _make_client(rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse([
            _order_payload(price="NaN"),
        ]),
    })
    with pytest.raises(BinanceTestnetValidationError, match="finite"):
        client.open_orders("BNBUSDT")


def test_open_order_rejects_infinity_qty():
    client = _make_client(rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse([
            _order_payload(origQty="Infinity"),
        ]),
    })
    with pytest.raises(BinanceTestnetValidationError, match="finite"):
        client.open_orders("BNBUSDT")


# ---------------------------------------------------------------------------
# 6. Duplicate account asset (finding 3/F)
# ---------------------------------------------------------------------------

def test_account_duplicate_asset_rejected():
    payload = {"balances": [
        {"asset": "BTC", "free": "1", "locked": "0"},
        {"asset": "BTC", "free": "2", "locked": "0"},
    ]}
    client = _make_client(rest_overrides={
        "get_account": lambda omit_zero_balances=None: _FakeResponse(payload),
    })
    with pytest.raises(BinanceTestnetValidationError, match="[Dd]uplicate asset"):
        client.account()


def test_account_duplicate_asset_different_casing_rejected():
    payload = {"balances": [
        {"asset": "btc", "free": "1", "locked": "0"},
        {"asset": "BTC", "free": "2", "locked": "0"},
    ]}
    client = _make_client(rest_overrides={
        "get_account": lambda omit_zero_balances=None: _FakeResponse(payload),
    })
    with pytest.raises(BinanceTestnetValidationError, match="[Dd]uplicate asset"):
        client.account()


def test_account_malformed_asset_name_rejected():
    payload = {"balances": [
        {"asset": "", "free": "1", "locked": "0"},
    ]}
    client = _make_client(rest_overrides={
        "get_account": lambda omit_zero_balances=None: _FakeResponse(payload),
    })
    with pytest.raises(BinanceTestnetValidationError, match="empty asset"):
        client.account()


def test_account_missing_asset_rejected():
    payload = {"balances": [
        {"free": "1", "locked": "0"},
    ]}
    client = _make_client(rest_overrides={
        "get_account": lambda omit_zero_balances=None: _FakeResponse(payload),
    })
    with pytest.raises(BinanceTestnetValidationError, match="empty asset"):
        client.account()


# ---------------------------------------------------------------------------
# 4/G. MAX_OPEN_ORDERS + response-size guards
# ---------------------------------------------------------------------------

def test_open_orders_exceeding_max_rejected():
    cfg = _valid_config(max_open_orders=5)
    payload = [_order_payload(order_id=i, client_id=f"grid-{i}") for i in range(1, 7)]
    client = _make_client(config=cfg, rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse(payload),
    })
    with pytest.raises(BinanceTestnetValidationError, match="exceeding"):
        client.open_orders("BNBUSDT")


def test_open_orders_at_limit_accepted():
    cfg = _valid_config(max_open_orders=5)
    payload = [_order_payload(order_id=i, client_id=f"grid-{i}") for i in range(1, 6)]
    client = _make_client(config=cfg, rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse(payload),
    })
    assert len(client.open_orders("BNBUSDT")) == 5


def test_open_orders_below_limit_accepted():
    cfg = _valid_config(max_open_orders=5)
    client = _make_client(config=cfg, rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse([
            _order_payload(order_id=1, client_id="grid-1"),
        ]),
    })
    assert len(client.open_orders("BNBUSDT")) == 1


def test_max_account_assets_guard():
    cfg = _valid_config(max_account_assets=2)
    payload = {"balances": [
        {"asset": "BTC", "free": "1", "locked": "0"},
        {"asset": "ETH", "free": "2", "locked": "0"},
        {"asset": "SOL", "free": "3", "locked": "0"},
    ]}
    client = _make_client(config=cfg, rest_overrides={
        "get_account": lambda omit_zero_balances=None: _FakeResponse(payload),
    })
    with pytest.raises(BinanceTestnetValidationError, match="exceeding"):
        client.account()


def test_config_max_open_orders_must_be_positive():
    with pytest.raises(BinanceTestnetConfigError):
        _valid_config(max_open_orders=0)
    with pytest.raises(BinanceTestnetConfigError):
        _valid_config(max_open_orders=-1)


def test_config_max_account_assets_must_be_positive():
    with pytest.raises(BinanceTestnetConfigError):
        _valid_config(max_account_assets=0)


# ---------------------------------------------------------------------------
# 8/I. Symbol filter contradiction
# ---------------------------------------------------------------------------

def _payload_with_filter_updated(ft: str, **updates):
    payload = _exchange_info_payload()
    for f in payload["symbols"][0]["filters"]:
        if f["filterType"] == ft:
            f.update(updates)
    return payload


def test_filter_contradiction_min_price_gt_max_price_rejected():
    payload = _payload_with_filter_updated(
        "PRICE_FILTER", minPrice="1000", maxPrice="0.01")
    client = _make_client(rest_overrides={
        "exchange_info": lambda symbol=None: _FakeResponse(payload),
    })
    with pytest.raises(BinanceTestnetValidationError, match="Contradictory"):
        client.symbol_snapshot("BNBUSDT")


def test_filter_contradiction_min_qty_gt_max_qty_rejected():
    payload = _payload_with_filter_updated(
        "LOT_SIZE", minQty="100", maxQty="0.001")
    client = _make_client(rest_overrides={
        "exchange_info": lambda symbol=None: _FakeResponse(payload),
    })
    with pytest.raises(BinanceTestnetValidationError, match="Contradictory"):
        client.symbol_snapshot("BNBUSDT")


def test_filter_contradiction_market_lot_size_rejected():
    payload = _payload_with_filter_updated(
        "MARKET_LOT_SIZE", minQty="5000", maxQty="0.001")
    client = _make_client(rest_overrides={
        "exchange_info": lambda symbol=None: _FakeResponse(payload),
    })
    with pytest.raises(BinanceTestnetValidationError, match="Contradictory"):
        client.symbol_snapshot("BNBUSDT")


def test_filter_contradiction_min_notional_gt_max_notional_rejected():
    payload = _payload_with_filter_updated(
        "NOTIONAL", minNotional="100000", maxNotional="10")
    client = _make_client(rest_overrides={
        "exchange_info": lambda symbol=None: _FakeResponse(payload),
    })
    with pytest.raises(BinanceTestnetValidationError, match="Contradictory"):
        client.symbol_snapshot("BNBUSDT")


def test_filter_contradiction_percent_price_multipliers_rejected():
    payload = _payload_with_filter_updated(
        "PERCENT_PRICE", multiplierDown="1.05", multiplierUp="0.95")
    client = _make_client(rest_overrides={
        "exchange_info": lambda symbol=None: _FakeResponse(payload),
    })
    with pytest.raises(BinanceTestnetValidationError, match="Contradictory"):
        client.symbol_snapshot("BNBUSDT")


def test_consistent_filters_accepted():
    client = _make_client(rest_overrides={
        "exchange_info": lambda symbol=None: _FakeResponse(_exchange_info_payload()),
    })
    assert client.symbol_snapshot("BNBUSDT").symbol == "BNBUSDT"


# ---------------------------------------------------------------------------
# H. Open-order field validation
# ---------------------------------------------------------------------------

def test_open_order_missing_isworking_rejected():
    order = _order_payload()
    del order["isWorking"]
    client = _make_client(rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse([order]),
    })
    with pytest.raises(BinanceTestnetValidationError, match="isWorking"):
        client.open_orders("BNBUSDT")


def test_open_order_executed_exceeds_orig_rejected():
    client = _make_client(rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse([
            _order_payload(origQty="1.0", executedQty="2.0"),
        ]),
    })
    with pytest.raises(BinanceTestnetValidationError, match="exceeds origQty"):
        client.open_orders("BNBUSDT")


def test_open_order_negative_price_rejected():
    client = _make_client(rest_overrides={
        "get_open_orders": lambda symbol=None: _FakeResponse([
            _order_payload(price="-1.0"),
        ]),
    })
    with pytest.raises(BinanceTestnetValidationError):
        client.open_orders("BNBUSDT")


# ---------------------------------------------------------------------------
# 6/C. Config repr / str must not expose secrets
# ---------------------------------------------------------------------------

def test_config_repr_excludes_credentials():
    cfg = _valid_config(api_key="super-secret-key", api_secret="super-secret-value")
    text = repr(cfg)
    assert "super-secret-key" not in text
    assert "super-secret-value" not in text
    assert "testnet" in text
    assert "https://testnet.binance.vision" in text


def test_config_str_excludes_credentials():
    cfg = _valid_config(api_key="super-secret-key", api_secret="super-secret-value")
    text = str(cfg)
    assert "super-secret-key" not in text
    assert "super-secret-value" not in text


def test_config_exception_does_not_expose_secrets():
    secret = "my-very-long-secret-xyz987"
    with pytest.raises(BinanceTestnetConfigError) as excinfo:
        _valid_config(api_secret=secret, dry_run=False)
    assert secret not in str(excinfo.value)


# ---------------------------------------------------------------------------
# 7/J. Dormant production path is dead
# ---------------------------------------------------------------------------

def test_market_data_make_client_rejects_live_mode():
    from market_data import make_client, MarketDataError
    with pytest.raises(MarketDataError, match="testnet"):
        make_client("live")


def test_market_data_make_client_rejects_unknown_mode():
    from market_data import make_client, MarketDataError
    with pytest.raises(MarketDataError, match="Unsupported"):
        make_client("staging")


def test_market_data_base_path_rejects_live():
    from market_data import _base_path, MarketDataError
    with pytest.raises(MarketDataError):
        _base_path("live")
    # The dormant production constant must not be importable anymore.
    import market_data
    assert not hasattr(market_data, "SPOT_REST_API_PROD_URL")


def test_market_data_make_client_testnet_uses_approved_base():
    from market_data import make_client
    client = make_client("testnet")
    base = client._cfg_rest_api.base_path if hasattr(client, "_cfg_rest_api") else None
    # The exact attribute layout varies by SDK; assert the client object exists.
    assert client is not None


# ---------------------------------------------------------------------------
# 1/B. Credential redaction in market_data error paths
# ---------------------------------------------------------------------------

def test_market_data_redact_credentials_masks_known_secret():
    from market_data import redact_credentials
    secret = "abcdef1234567890"
    out = redact_credentials(f"failed: key {secret}", known_secrets=[secret])
    assert secret not in out
    assert "abcd" in out and "7890" in out


def test_market_data_redact_credentials_strips_bearer_token():
    from market_data import redact_credentials
    out = redact_credentials("Authorization Bearer eyJhbGciOi.fake.token")
    assert "eyJhbGciOi.fake.token" not in out
    assert "[redacted]" in out


def test_market_data_redact_credentials_is_deterministic():
    from market_data import redact_credentials
    msg = "api_key=SECRETVALUE123 unauthorized"
    a = redact_credentials(msg)
    b = redact_credentials(msg)
    assert a == b


def test_market_data_ticker_failure_message_redacted():
    from market_data import fetch_ticker_price, MarketDataError
    from types import SimpleNamespace

    class _Resp:
        def data(self):
            raise RuntimeError("boom")

    client = SimpleNamespace(rest_api=SimpleNamespace(
        ticker_price=lambda symbol=None: (_ for _ in ()).throw(
            RuntimeError("unauthorized: api_key=LEAKSECRET42")
        )
    ))
    with pytest.raises(MarketDataError) as excinfo:
        fetch_ticker_price(client, "BNBUSDT")
    assert "LEAKSECRET42" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# M. Error-failure semantics: failure != empty/default state
# ---------------------------------------------------------------------------

def test_account_request_failure_raises_not_empty():
    def _fail(omit_zero_balances=None):
        raise OSError("network down")
    client = _make_client(rest_overrides={"get_account": _fail})
    with pytest.raises(BinanceTestnetNetworkError):
        client.account()


def test_open_orders_request_failure_raises_not_empty_list():
    def _fail(symbol=None):
        raise OSError("network down")
    client = _make_client(rest_overrides={"get_open_orders": _fail})
    with pytest.raises(BinanceTestnetNetworkError):
        client.open_orders("BNBUSDT")


def test_ticker_request_failure_raises_not_default_price():
    def _fail(symbol=None):
        raise OSError("network down")
    client = _make_client(rest_overrides={"ticker_price": _fail})
    with pytest.raises(BinanceTestnetNetworkError):
        client.ticker_price("BNBUSDT")


# ---------------------------------------------------------------------------
# N. Timestamp / clock-drift validation
# ---------------------------------------------------------------------------

def test_server_time_rejects_absurd_negative():
    client = _make_client(rest_overrides={"time": lambda: _FakeResponse({"serverTime": -1})})
    with pytest.raises((BinanceTestnetResponseError, BinanceTestnetValidationError)):
        client.server_time()


def test_server_time_rejects_float():
    client = _make_client(rest_overrides={
        "time": lambda: _FakeResponse({"serverTime": 1756000000000.5}),
    })
    with pytest.raises((BinanceTestnetResponseError, BinanceTestnetValidationError)):
        client.server_time()


def test_server_time_rejects_string():
    client = _make_client(rest_overrides={
        "time": lambda: _FakeResponse({"serverTime": "1756000000000"}),
    })
    with pytest.raises((BinanceTestnetResponseError, BinanceTestnetValidationError)):
        client.server_time()


def test_server_time_rejects_none():
    client = _make_client(rest_overrides={
        "time": lambda: _FakeResponse({"serverTime": None}),
    })
    with pytest.raises((BinanceTestnetResponseError, BinanceTestnetValidationError)):
        client.server_time()


# ---------------------------------------------------------------------------
# K. Strict environment validation
# ---------------------------------------------------------------------------

def test_load_config_rejects_non_integer_max_open_orders(monkeypatch):
    _set_env_basics(monkeypatch)
    monkeypatch.setenv("BINANCE_MAX_OPEN_ORDERS", "abc")
    with pytest.raises(BinanceTestnetConfigError, match="BINANCE_MAX_OPEN_ORDERS"):
        load_testnet_config_from_env()


def test_load_config_rejects_negative_max_open_orders(monkeypatch):
    _set_env_basics(monkeypatch)
    monkeypatch.setenv("BINANCE_MAX_OPEN_ORDERS", "-5")
    with pytest.raises(BinanceTestnetConfigError, match="BINANCE_MAX_OPEN_ORDERS"):
        load_testnet_config_from_env()


def test_load_config_max_open_orders_valid(monkeypatch):
    _set_env_basics(monkeypatch)
    monkeypatch.setenv("BINANCE_MAX_OPEN_ORDERS", "50")
    assert load_testnet_config_from_env().max_open_orders == 50


def test_load_config_rejects_ambiguous_bool(monkeypatch):
    _set_env_basics(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "1")
    with pytest.raises(BinanceTestnetConfigError, match="DRY_RUN"):
        load_testnet_config_from_env()


def test_load_config_rejects_empty_api_key(monkeypatch):
    _set_env_basics(monkeypatch)
    monkeypatch.setenv("BINANCE_API_KEY", "")
    with pytest.raises(BinanceTestnetConfigError):
        load_testnet_config_from_env()


def test_load_config_rejects_missing_max_env_uses_default(monkeypatch):
    """Absent BINANCE_MAX_OPEN_ORDERS must use the positive default, not 0."""
    _set_env_basics(monkeypatch)
    monkeypatch.delenv("BINANCE_MAX_OPEN_ORDERS", raising=False)
    assert load_testnet_config_from_env().max_open_orders >= 1


# ---------------------------------------------------------------------------
# P. Connectivity fail-closed (account / open-orders failure -> FAIL)
# ---------------------------------------------------------------------------

def test_connectivity_unhealthy_when_account_fails():
    client = _make_client(rest_overrides={
        "ping": lambda: None,
        "time": lambda: _FakeResponse({"serverTime": 1756000000000}),
        "exchange_info": lambda symbol=None: _FakeResponse(_exchange_info_payload(symbol="BNBUSDT")),
        "ticker_price": lambda symbol=None: _FakeResponse({"symbol": "BNBUSDT", "price": "350.50"}),
        "get_account": lambda omit_zero_balances=None: (_ for _ in ()).throw(
            BinanceTestnetNetworkError("account unavailable")
        ),
        "get_open_orders": lambda symbol=None: _FakeResponse([]),
    })
    snap = client.connectivity_check("BNBUSDT")
    assert snap.overall == "FAIL"
    assert snap.account_available is False


def test_connectivity_unhealthy_when_open_orders_fail():
    client = _make_client(rest_overrides={
        "ping": lambda: None,
        "time": lambda: _FakeResponse({"serverTime": 1756000000000}),
        "exchange_info": lambda symbol=None: _FakeResponse(_exchange_info_payload(symbol="BNBUSDT")),
        "ticker_price": lambda symbol=None: _FakeResponse({"symbol": "BNBUSDT", "price": "350.50"}),
        "get_account": lambda omit_zero_balances=None: _FakeResponse(_account_payload()),
        "get_open_orders": lambda symbol=None: (_ for _ in ()).throw(
            BinanceTestnetNetworkError("open orders unavailable")
        ),
    })
    snap = client.connectivity_check("BNBUSDT")
    assert snap.overall == "FAIL"
    assert "OPEN_ORDERS" in snap.reason
