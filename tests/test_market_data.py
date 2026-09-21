from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from market_data import (
    MarketDataError,
    TickerSnapshot,
    fetch_klines,
    fetch_ticker_price,
    is_ticker_fresh,
)


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
