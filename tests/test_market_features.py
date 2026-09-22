"""Tests for Phase 4 Market Features Calculation and Candle Validation."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import numpy as np
import pandas as pd
import pytest

from market_data import MarketQuote
from market_features import (
    CandleValidationError,
    InsufficientDataError,
    calculate_market_features,
    validate_candles,
)


def make_deterministic_candles(
    n: int = 80,
    base_price: float = 100.0,
    step_minutes: int = 15,
    start_time: datetime | None = None,
    volatility: float = 0.5,
) -> pd.DataFrame:
    """Generate deterministic closed 15m candles without randomness."""
    if start_time is None:
        start_time = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)

    rows = []
    curr = base_price
    for i in range(n):
        open_time = start_time + timedelta(minutes=i * step_minutes)
        close_time = open_time + timedelta(minutes=step_minutes)
        # Deterministic wave oscillation
        offset = np.sin(i * 0.2) * volatility
        close_val = base_price + offset
        open_val = curr
        high_val = max(open_val, close_val) + 0.25
        low_val = min(open_val, close_val) - 0.25
        volume_val = 100.0 + (i % 5) * 10.0
        rows.append({
            "open_time": open_time,
            "close_time": close_time,
            "open": round(open_val, 4),
            "high": round(high_val, 4),
            "low": round(low_val, 4),
            "close": round(close_val, 4),
            "volume": round(volume_val, 4),
        })
        curr = close_val
    return pd.DataFrame(rows)


def default_config() -> dict:
    return {
        "market_intelligence": {
            "timeframe": "15m",
            "min_candles": 60,
            "max_candle_age_seconds": 3600,
            "atr_period": 14,
            "adx_period": 14,
            "bb_length": 20,
            "bb_std_mult": 2.0,
            "volume_baseline_period": 20,
            "range_stability_period": 20,
            "regime": {
                "adx_trend_min": 25,
                "atr_expansion_ratio": 1.5,
                "price_range_inclusion_min": 0.90,
                "directional_efficiency_max": 0.60,
            },
            "liquidity": {
                "max_spread_pct": 0.003,
                "max_quote_ticker_age_seconds": 10,
            },
            "quality": {
                "min_range_quality_score": 60,
                "weight_trend_stability": 0.25,
                "weight_volatility_suitability": 0.20,
                "weight_bb_width_suitability": 0.15,
                "weight_volume_stability": 0.10,
                "weight_spread_suitability": 0.10,
                "weight_range_containment": 0.20,
            },
        }
    }


def test_valid_closed_candle_dataset():
    """Scenario 1: Valid closed candle dataset passes validation cleanly."""
    df = make_deterministic_candles(70)
    validated = validate_candles(df, min_candles=60)
    assert len(validated) == 70


def test_forming_candle_excluded():
    """Scenario 2: Forming candle (close_time > now) fails closed."""
    df = make_deterministic_candles(65)
    # Set now to be before the last candle closes
    now = df["close_time"].iloc[-1] - timedelta(minutes=5)
    with pytest.raises(CandleValidationError, match="Incomplete or currently forming candle"):
        validate_candles(df, min_candles=60, now=now)


def test_insufficient_candles():
    """Scenario 3: Fewer candles than min_candles raises InsufficientDataError."""
    df = make_deterministic_candles(40)
    with pytest.raises(InsufficientDataError, match="Insufficient candles"):
        validate_candles(df, min_candles=60)


@pytest.mark.parametrize(
    "corrupt_field,corrupt_val,expected_err",
    [
        ("high", 80.0, "high < low"),     # high below low
        ("high", 99.0, "high <"),         # high below open/close/low
        ("low", 120.0, "high < low"),     # low above high
        ("close", -10.0, "non-positive"), # negative price
        ("volume", -5.0, "negative"),     # negative volume
    ],
)
def test_malformed_ohlc(corrupt_field, corrupt_val, expected_err):
    """Scenario 4: Malformed OHLC relationships fail closed."""
    df = make_deterministic_candles(65)
    df.loc[10, corrupt_field] = corrupt_val
    with pytest.raises(CandleValidationError, match=expected_err):
        validate_candles(df, min_candles=60)


def test_duplicate_candle_timestamps():
    """Scenario 5: Duplicate candle timestamps fail closed."""
    df = make_deterministic_candles(65)
    df.loc[10, "open_time"] = df.loc[9, "open_time"]
    with pytest.raises(CandleValidationError):
        validate_candles(df, min_candles=60)


def test_non_monotonic_timestamps():
    """Scenario 6: Timestamps jumping backward fail closed."""
    df = make_deterministic_candles(65)
    # Move row 10 to be earlier than row 9
    df.loc[10, "open_time"] = df.loc[9, "open_time"] - timedelta(minutes=15)
    df.loc[10, "close_time"] = df.loc[10, "open_time"] + timedelta(minutes=15)
    with pytest.raises(CandleValidationError, match="monotonic"):
        validate_candles(df, min_candles=60)


def test_invalid_volume():
    """Scenario 7: Non-numeric or NaN volume fails closed."""
    df = make_deterministic_candles(65)
    df.loc[15, "volume"] = np.nan
    with pytest.raises(CandleValidationError, match="NaN or null"):
        validate_candles(df, min_candles=60)


def _make_valid_quote() -> MarketQuote:
    return MarketQuote(
        symbol="BTCUSDT",
        bid_price=Decimal("99.98"),
        ask_price=Decimal("100.02"),
        bid_qty=Decimal("10"),
        ask_qty=Decimal("10"),
        fetched_at=datetime.now(timezone.utc),
    )


def test_atr_and_atr_percentage():
    """Scenarios 8 & 9: ATR and ATR percentage calculation."""
    df = make_deterministic_candles(80)
    cfg = default_config()
    quote = _make_valid_quote()
    features = calculate_market_features(
        df=df,
        quote=quote,
        lower_price=Decimal("90.0"),
        upper_price=Decimal("110.0"),
        symbol="BTCUSDT",
        config=cfg,
    )
    assert features.atr > 0
    assert features.atr_pct == features.atr / features.close_price
    assert isinstance(features.atr, Decimal)
    assert isinstance(features.atr_pct, Decimal)


def test_adx_calculation():
    """Scenario 10: ADX, +DI, -DI exposed and valid."""
    df = make_deterministic_candles(80)
    cfg = default_config()
    quote = _make_valid_quote()
    features = calculate_market_features(
        df=df,
        quote=quote,
        lower_price=Decimal("90.0"),
        upper_price=Decimal("110.0"),
        symbol="BTCUSDT",
        config=cfg,
    )
    assert features.adx >= 0
    assert features.plus_di >= 0
    assert features.minus_di >= 0


def test_bollinger_width():
    """Scenario 11: Bollinger middle, upper, lower, width, width_pct."""
    df = make_deterministic_candles(80)
    cfg = default_config()
    quote = _make_valid_quote()
    features = calculate_market_features(
        df=df,
        quote=quote,
        lower_price=Decimal("90.0"),
        upper_price=Decimal("110.0"),
        symbol="BTCUSDT",
        config=cfg,
    )
    assert features.bb_upper > features.bb_middle > features.bb_lower
    assert features.bb_width == features.bb_upper - features.bb_lower
    assert features.bb_width_pct == features.bb_width / features.bb_middle


def test_volume_baseline_and_spike_ratio():
    """Scenarios 12 & 13: Volume baseline and spike ratio."""
    df = make_deterministic_candles(80)
    cfg = default_config()
    quote = _make_valid_quote()
    features = calculate_market_features(
        df=df,
        quote=quote,
        lower_price=Decimal("90.0"),
        upper_price=Decimal("110.0"),
        symbol="BTCUSDT",
        config=cfg,
    )
    assert features.baseline_volume > 0
    assert features.volume_spike_ratio == features.current_volume / features.baseline_volume


def test_range_stability_metrics():
    """Scenario 14: Directional efficiency and range containment."""
    df = make_deterministic_candles(80)
    cfg = default_config()
    quote = _make_valid_quote()
    features = calculate_market_features(
        df=df,
        quote=quote,
        lower_price=Decimal("95.0"),
        upper_price=Decimal("105.0"),
        symbol="BTCUSDT",
        config=cfg,
    )
    assert Decimal("0") <= features.directional_efficiency <= Decimal("1.0")
    assert Decimal("0") <= features.range_containment_pct <= Decimal("1.0")
    assert features.penetration_count == 0  # all candles are well within [95, 105]


def test_deterministic_repeated_calculation():
    """Scenario 27: Repeated calculation with identical input yields identical output."""
    df = make_deterministic_candles(80)
    cfg = default_config()
    quote = _make_valid_quote()
    f1 = calculate_market_features(
        df=df,
        quote=quote,
        lower_price=Decimal("90.0"),
        upper_price=Decimal("110.0"),
        symbol="BTCUSDT",
        config=cfg,
    )
    f2 = calculate_market_features(
        df=df,
        quote=quote,
        lower_price=Decimal("90.0"),
        upper_price=Decimal("110.0"),
        symbol="BTCUSDT",
        config=cfg,
    )
    assert f1 == f2
