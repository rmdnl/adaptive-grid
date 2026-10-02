from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

import market_data
from market_data import (
    AccountRequestError,
    AccountValidationError,
    MarketDataError,
    TickerSnapshot,
    build_account_risk_state,
    calculate_spot_equity,
    fetch_account_snapshot,
    fetch_klines,
    fetch_ticker_price,
    is_ticker_fresh,
)
from risk_engine import account_state_gate, open_orders_available_gate


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def data(self):
        return self.payload


class FakeRestApi:
    def __init__(self, ticker_payload=None, kline_payload=None, ticker_error=None):
        self.ticker_payload = ticker_payload
        self.kline_payload = kline_payload
        self.ticker_error = ticker_error
        self.ticker_calls = []
        self.kline_calls = []

    def ticker_price(self, **kwargs):
        self.ticker_calls.append(kwargs)
        if self.ticker_error:
            raise self.ticker_error
        return FakeResponse(self.ticker_payload)

    def klines(self, **kwargs):
        self.kline_calls.append(kwargs)
        return FakeResponse(self.kline_payload)


def client_with(**kwargs):
    return SimpleNamespace(rest_api=FakeRestApi(**kwargs))


def test_fetch_ticker_price_returns_decimal_snapshot_and_normalizes_symbol():
    client = client_with(ticker_payload={"symbol": "BTCUSDT", "price": "123.4500"})

    snapshot = fetch_ticker_price(client, "btcusdt")

    assert snapshot.symbol == "BTCUSDT"
    assert snapshot.price == Decimal("123.4500")
    assert snapshot.fetched_at.tzinfo is timezone.utc
    assert client.rest_api.ticker_calls == [{"symbol": "BTCUSDT"}]


@pytest.mark.parametrize("payload", [None, {}, [], {"symbol": "BTCUSDT"}, {"price": "abc"}])
def test_fetch_ticker_price_rejects_malformed_response(payload):
    client = client_with(ticker_payload=payload)

    with pytest.raises(MarketDataError):
        fetch_ticker_price(client, "BTCUSDT")


@pytest.mark.parametrize("price", ["0", "-0.01"])
def test_fetch_ticker_price_rejects_non_positive_prices(price):
    client = client_with(ticker_payload={"symbol": "BTCUSDT", "price": price})

    with pytest.raises(MarketDataError, match="Invalid Binance ticker price"):
        fetch_ticker_price(client, "BTCUSDT")


def test_is_ticker_fresh_within_threshold():
    snapshot = TickerSnapshot(
        "BTCUSDT", Decimal("100"), datetime.now(timezone.utc) - timedelta(seconds=2)
    )

    assert is_ticker_fresh(snapshot, max_age_seconds=10)


def test_is_ticker_fresh_rejects_stale_snapshot():
    snapshot = TickerSnapshot(
        "BTCUSDT", Decimal("100"), datetime.now(timezone.utc) - timedelta(seconds=11)
    )

    assert not is_ticker_fresh(snapshot, max_age_seconds=10)


@pytest.mark.parametrize("max_age", [-1, "not-a-number"])
def test_is_ticker_fresh_rejects_invalid_max_age(max_age):
    snapshot = TickerSnapshot("BTCUSDT", Decimal("100"), datetime.now(timezone.utc))

    with pytest.raises(ValueError, match="max_age_seconds"):
        is_ticker_fresh(snapshot, max_age_seconds=max_age)


def _kline_rows(include_incomplete=False):
    now = datetime.now(timezone.utc)
    rows = []
    for index in range(50):
        close_time = now - timedelta(minutes=(50 - index) * 15)
        open_time = close_time - timedelta(minutes=15)
        rows.append([
            int(open_time.timestamp() * 1000), "100", "101", "99", "100.5", "10",
            int(close_time.timestamp() * 1000), "1000", 1, "5", "500", "0",
        ])
    if include_incomplete:
        open_time = now - timedelta(minutes=1)
        rows.append([
            int(open_time.timestamp() * 1000), "200", "201", "199", "200.5", "10",
            int((now + timedelta(minutes=14)).timestamp() * 1000), "2000", 1, "5", "1000", "0",
        ])
    return rows


def test_fetch_klines_preserves_closed_candle_behavior():
    client = client_with(kline_payload=_kline_rows())

    candles = fetch_klines(client, "btcusdt", "15m", limit=50, drop_incomplete=True)

    assert len(candles) == 50
    assert candles["close"].iloc[-1] == 100.5
    assert client.rest_api.kline_calls[0]["symbol"] == "BTCUSDT"


def test_fetch_klines_excludes_incomplete_candle():
    client = client_with(kline_payload=_kline_rows(include_incomplete=True))

    candles = fetch_klines(client, "BTCUSDT", "15m", limit=51, drop_incomplete=True)

    assert len(candles) == 50
    assert 200.5 not in candles["close"].tolist()


def test_ticker_failure_never_falls_back_to_candle_close():
    client = client_with(ticker_error=OSError("network unavailable"), kline_payload=_kline_rows())

    with pytest.raises(MarketDataError, match="ticker price request failed"):
        fetch_ticker_price(client, "BTCUSDT")

    assert client.rest_api.kline_calls == []


def _account_client(payload=None, error=None):
    def get_account(**kwargs):
        if error:
            raise error
        return FakeResponse(payload)
    return SimpleNamespace(rest_api=SimpleNamespace(get_account=get_account))


def _account_payload(base="BTC", quote="USDT", base_free="1.25", base_locked="0.25",
                     quote_free="100", quote_locked="20"):
    return {"balances": [
        {"asset": base, "free": base_free, "locked": base_locked},
        {"asset": quote, "free": quote_free, "locked": quote_locked},
    ]}


@pytest.mark.parametrize(("base", "quote"), [("BTC", "USDT"), ("BNB", "USDT")])
def test_fetch_account_snapshot_parses_configured_pair_assets(base, quote):
    snapshot = fetch_account_snapshot(_account_client(_account_payload(base, quote)), base, quote)

    assert snapshot.base_asset == base
    assert snapshot.quote_asset == quote
    assert snapshot.base_free == Decimal("1.25")
    assert snapshot.quote_locked == Decimal("20")


def test_account_snapshot_totals_include_free_and_locked_balances():
    snapshot = fetch_account_snapshot(_account_client(_account_payload()), "BTC", "USDT")

    assert snapshot.base_total == Decimal("1.50")
    assert snapshot.quote_total == Decimal("120")


@pytest.mark.parametrize(("balances", "asset"), [
    ([{"asset": "USDT", "free": "1", "locked": "0"}], "BTC"),
    ([{"asset": "BTC", "free": "1", "locked": "0"}], "USDT"),
])
def test_fetch_account_snapshot_rejects_missing_required_asset(balances, asset):
    with pytest.raises(AccountValidationError, match=asset):
        fetch_account_snapshot(_account_client({"balances": balances}), "BTC", "USDT")


@pytest.mark.parametrize("base_free", ["not-a-number", "-0.01"])
def test_fetch_account_snapshot_rejects_malformed_or_negative_balance(base_free):
    with pytest.raises(AccountValidationError, match="balance"):
        fetch_account_snapshot(
            _account_client(_account_payload(base_free=base_free)), "BTC", "USDT"
        )


def test_account_api_failure_is_distinct_and_fails_closed():
    with pytest.raises(AccountRequestError, match="account information request failed"):
        fetch_account_snapshot(_account_client(error=OSError("offline")), "BTC", "USDT")

    assert not account_state_gate(False).allowed


def test_spot_equity_marks_base_inventory_with_ticker_price():
    snapshot = fetch_account_snapshot(_account_client(_account_payload()), "BTC", "USDT")

    assert calculate_spot_equity(snapshot, Decimal("200")) == Decimal("420")


@pytest.mark.parametrize("reference", [Decimal("0"), Decimal("-1")])
def test_account_risk_rejects_zero_or_negative_reference_equity(reference):
    snapshot = fetch_account_snapshot(_account_client(_account_payload()), "BTC", "USDT")

    with pytest.raises(AccountValidationError, match="Reference equity must be positive"):
        build_account_risk_state(snapshot, Decimal("200"), reference)


def test_account_risk_uses_first_observed_equity_not_fake_history_and_calculates_drawdown():
    snapshot = fetch_account_snapshot(_account_client(_account_payload()), "BTC", "USDT")

    first = build_account_risk_state(snapshot, Decimal("200"))
    later = build_account_risk_state(snapshot, Decimal("180"), first.reference_equity)

    assert first.current_equity == first.reference_equity == Decimal("420")
    assert first.drawdown_pct == Decimal("0")
    assert later.current_equity == Decimal("390")
    assert later.drawdown_pct == Decimal("30") / Decimal("420")
    assert later.base_inventory == Decimal("1.50")
    assert later.inventory_pct == Decimal("270") / Decimal("390")


def test_unavailable_open_orders_fails_closed():
    assert open_orders_available_gate(False).reason == "OPEN_ORDERS_UNAVAILABLE"


# --- Adversarial safety: invalid/NaN/Infinity equity fails closed ---
def test_account_risk_rejects_nan_equity():
    """A NaN current equity must fail closed, not silently pass through to
    NaN drawdown/inventory percentages that could bypass kill gates.

    The NaN < 0 check returns False in Decimal arithmetic, so without the
    explicit is_finite() check, NaN equity would slip through.
    """
    snapshot = fetch_account_snapshot(_account_client(_account_payload()), "BTC", "USDT")
    original = market_data.calculate_spot_equity
    market_data.calculate_spot_equity = lambda s, p: Decimal("NaN")
    try:
        with pytest.raises(AccountValidationError, match="not finite|must be positive"):
            build_account_risk_state(snapshot, Decimal("200"), Decimal("200"))
    finally:
        market_data.calculate_spot_equity = original


def test_account_risk_rejects_infinite_equity():
    """An Infinity current equity must fail closed (not finite)."""
    snapshot = fetch_account_snapshot(_account_client(_account_payload()), "BTC", "USDT")
    original = market_data.calculate_spot_equity
    market_data.calculate_spot_equity = lambda s, p: Decimal("Infinity")
    try:
        with pytest.raises(AccountValidationError, match="not finite|must be positive"):
            build_account_risk_state(snapshot, Decimal("200"), Decimal("200"))
    finally:
        market_data.calculate_spot_equity = original
