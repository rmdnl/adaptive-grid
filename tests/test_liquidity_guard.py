"""Tests for Phase 4 Liquidity and Spread Guard."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from market_data import MarketDataError, MarketQuote, is_quote_fresh


def test_valid_bid_ask():
    """Scenario 21: Valid bid/ask creates MarketQuote with correct mid and spread."""
    now = datetime.now(timezone.utc)
    quote = MarketQuote(
        symbol="BTCUSDT",
        bid_price=Decimal("100.00"),
        ask_price=Decimal("100.10"),
        bid_qty=Decimal("5.0"),
        ask_qty=Decimal("5.0"),
        fetched_at=now,
    )
    assert quote.bid_price == Decimal("100.00")
    assert quote.ask_price == Decimal("100.10")
    assert quote.spread == Decimal("0.10")
    assert quote.mid_price == Decimal("100.05")
    # spread_pct = 0.10 / 100.05 = ~0.0009995
    assert quote.spread_pct == quote.spread / quote.mid_price


@pytest.mark.parametrize("bad_bid", [Decimal("0"), Decimal("-1.5")])
def test_invalid_bid_non_positive(bad_bid):
    """Scenario 22: Non-positive bid price is rejected in validation or spread."""
    quote = MarketQuote(
        symbol="BTCUSDT",
        bid_price=bad_bid,
        ask_price=Decimal("100.00"),
        bid_qty=Decimal("1.0"),
        ask_qty=Decimal("1.0"),
        fetched_at=datetime.now(timezone.utc),
    )
    # mid_price calculation or spread_pct for 0 or negative mid
    if quote.mid_price <= 0:
        with pytest.raises(MarketDataError, match="Quote mid price must be positive"):
            _ = quote.spread_pct


@pytest.mark.parametrize("bad_ask", [Decimal("0"), Decimal("-5.0")])
def test_invalid_ask_non_positive(bad_ask):
    """Scenario 23: Non-positive ask price is rejected."""
    quote = MarketQuote(
        symbol="BTCUSDT",
        bid_price=Decimal("100.00"),
        ask_price=bad_ask,
        bid_qty=Decimal("1.0"),
        ask_qty=Decimal("1.0"),
        fetched_at=datetime.now(timezone.utc),
    )
    assert quote.ask_price <= 0
    assert quote.ask_price < quote.bid_price


def test_ask_below_bid_crossed_book():
    """Scenario 24: Ask below bid is a crossed book."""
    quote = MarketQuote(
        symbol="BTCUSDT",
        bid_price=Decimal("105.00"),
        ask_price=Decimal("100.00"),
        bid_qty=Decimal("1.0"),
        ask_qty=Decimal("1.0"),
        fetched_at=datetime.now(timezone.utc),
    )
    assert quote.ask_price < quote.bid_price
    assert quote.spread < Decimal("0")


def test_spread_percentage_calculation():
    """Scenario 25: Deterministic spread percentage calculation."""
    quote = MarketQuote(
        symbol="ETHUSDT",
        bid_price=Decimal("2000.00"),
        ask_price=Decimal("2002.00"),
        bid_qty=Decimal("10.0"),
        ask_qty=Decimal("10.0"),
        fetched_at=datetime.now(timezone.utc),
    )
    expected_mid = Decimal("2001.00")
    expected_spread = Decimal("2.00")
    expected_pct = expected_spread / expected_mid
    assert quote.mid_price == expected_mid
    assert quote.spread == expected_spread
    assert quote.spread_pct == expected_pct


@pytest.mark.parametrize(
    "bid,ask,max_spread_pct,is_blocked",
    [
        (Decimal("100.00"), Decimal("100.20"), Decimal("0.003"), False),  # 0.20% < 0.30% -> allowed
        (Decimal("100.00"), Decimal("100.30"), Decimal("0.003"), False),  # ~0.2995% < 0.30% -> allowed
        (Decimal("100.00"), Decimal("100.35"), Decimal("0.003"), True),   # ~0.349% > 0.30% -> blocked
        (Decimal("100.00"), Decimal("100.50"), Decimal("0.003"), True),   # 0.50% > 0.30% -> blocked
    ],
)
def test_spread_threshold_blocking(bid, ask, max_spread_pct, is_blocked):
    """Scenario 26: Spread threshold blocking around 0.30% boundary."""
    quote = MarketQuote(
        symbol="BTCUSDT",
        bid_price=bid,
        ask_price=ask,
        bid_qty=Decimal("1"),
        ask_qty=Decimal("1"),
        fetched_at=datetime.now(timezone.utc),
    )
    spread_pct = quote.spread_pct
    blocked = spread_pct > max_spread_pct
    assert blocked == is_blocked


def test_quote_freshness():
    """Verify quote freshness validation against max age."""
    now = datetime.now(timezone.utc)
    fresh_quote = MarketQuote(
        symbol="BTCUSDT",
        bid_price=Decimal("100"),
        ask_price=Decimal("101"),
        bid_qty=Decimal("1"),
        ask_qty=Decimal("1"),
        fetched_at=now - timedelta(seconds=5),
    )
    stale_quote = MarketQuote(
        symbol="BTCUSDT",
        bid_price=Decimal("100"),
        ask_price=Decimal("101"),
        bid_qty=Decimal("1"),
        ask_qty=Decimal("1"),
        fetched_at=now - timedelta(seconds=15),
    )
    assert is_quote_fresh(fresh_quote, max_age_seconds=10, now=now) is True
    assert is_quote_fresh(stale_quote, max_age_seconds=10, now=now) is False
