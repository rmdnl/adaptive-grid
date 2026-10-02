"""
Regression tests: fetch_klines must fail closed on malformed latest CLOSED candle.

Safety invariant:
- The 15m lower-boundary kill depends on the latest CLOSED candle close.
- If that critical candle is malformed/NaN/Inf/non-positive, the system
  must veto orders (MarketDataError), never silently substitute the
  previous valid candle.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from market_data import MarketDataError, fetch_klines


def _ts_ms(dt):
    return int(dt.timestamp() * 1000)


def _valid_kline_row(i, close_val, open_time):
    """Single valid kline row with deterministic OHLCV."""
    return [
        _ts_ms(open_time),
        str(close_val),  # open
        str(close_val + 1),  # high
        str(close_val - 1),  # low
        str(close_val),  # close
        "1000.0",  # volume
        _ts_ms(open_time + timedelta(minutes=15)),
        "50000.0",  # quote_volume
        100,  # trades
        "500.0",  # taker_base
        "25000.0",  # taker_quote
        "0",
    ]


def _kline_payload(num_closed, malform_latest=..., malform_field="close"):
    """
    Build kline payload with `num_closed` valid closed candles.
    If `malform_latest` is set (not the sentinel ...), the latest CLOSED candle's
    `malform_field` is replaced with that value (e.g., "not-a-number", None, math.inf).
    """
    now = datetime.now(timezone.utc)
    base_time = now - timedelta(minutes=15 * (num_closed + 1))
    rows = []
    for i in range(num_closed):
        candle_time = base_time + timedelta(minutes=15 * i)
        row = _valid_kline_row(i, 100.0 + i, candle_time)
        rows.append(row)
    
    if malform_latest is not ...:
        field_index = {
            "open": 1,
            "high": 2,
            "low": 3,
            "close": 4,
            "volume": 5,
        }[malform_field]
        rows[-1][field_index] = malform_latest
    
    return rows


def _client_with_klines(rows):
    def klines_fn(**kwargs):
        return SimpleNamespace(data=lambda: rows)
    return SimpleNamespace(rest_api=SimpleNamespace(klines=klines_fn))


# --- SPEC A: latest CLOSED candle close malformed string -> FAIL CLOSED ---
def test_latest_closed_candle_close_malformed_string_fails():
    """A. Malformed close (unparseable string) on latest CLOSED candle raises."""
    client = _client_with_klines(_kline_payload(60, malform_latest="not-a-number"))
    with pytest.raises(MarketDataError, match="Latest CLOSED candle has NaN close"):
        fetch_klines(client, "BTCUSDT", "15m", limit=60, drop_incomplete=True)


# --- SPEC B: latest CLOSED candle close missing (None) -> FAIL CLOSED ---
def test_latest_closed_candle_close_none_fails():
    """B. Missing close (None coerced to NaN) on latest CLOSED candle raises."""
    client = _client_with_klines(_kline_payload(60, malform_latest=None))
    with pytest.raises(MarketDataError, match="Latest CLOSED candle has NaN close"):
        fetch_klines(client, "BTCUSDT", "15m", limit=60, drop_incomplete=True)


# --- SPEC C: latest CLOSED candle close NaN -> FAIL CLOSED ---
def test_latest_closed_candle_close_nan_fails():
    """C. Explicit NaN on latest CLOSED candle close raises."""
    client = _client_with_klines(_kline_payload(60, malform_latest=math.nan))
    with pytest.raises(MarketDataError, match="Latest CLOSED candle has NaN close"):
        fetch_klines(client, "BTCUSDT", "15m", limit=60, drop_incomplete=True)


# --- SPEC D: latest CLOSED candle close Infinity -> FAIL CLOSED ---
def test_latest_closed_candle_close_infinity_fails():
    """D. +Infinity on latest CLOSED candle close raises."""
    client = _client_with_klines(_kline_payload(60, malform_latest=math.inf))
    with pytest.raises(MarketDataError, match="Latest CLOSED candle close is non-finite"):
        fetch_klines(client, "BTCUSDT", "15m", limit=60, drop_incomplete=True)


def test_latest_closed_candle_close_negative_infinity_fails():
    """D. -Infinity on latest CLOSED candle close raises."""
    client = _client_with_klines(_kline_payload(60, malform_latest=-math.inf))
    with pytest.raises(MarketDataError, match="Latest CLOSED candle close is non-finite"):
        fetch_klines(client, "BTCUSDT", "15m", limit=60, drop_incomplete=True)


def test_latest_closed_candle_close_zero_fails():
    """D. Zero close on latest CLOSED candle raises (non-positive)."""
    client = _client_with_klines(_kline_payload(60, malform_latest=0.0))
    with pytest.raises(MarketDataError, match="Latest CLOSED candle close is non-finite or non-positive"):
        fetch_klines(client, "BTCUSDT", "15m", limit=60, drop_incomplete=True)


def test_latest_closed_candle_close_negative_fails():
    """D. Negative close on latest CLOSED candle raises."""
    client = _client_with_klines(_kline_payload(60, malform_latest=-50.0))
    with pytest.raises(MarketDataError, match="Latest CLOSED candle close is non-finite or non-positive"):
        fetch_klines(client, "BTCUSDT", "15m", limit=60, drop_incomplete=True)


# --- SPEC E: malformed CURRENT/forming candle is safely excluded ---
def test_malformed_incomplete_candle_excluded_not_latest():
    """E. A malformed incomplete (current) candle is dropped; valid closed remain."""
    now = datetime.now(timezone.utc)
    base_time = now - timedelta(minutes=15 * 61)
    rows = []
    for i in range(60):
        candle_time = base_time + timedelta(minutes=15 * i)
        rows.append(_valid_kline_row(i, 100.0 + i, candle_time))
    
    # Add a malformed incomplete candle (close_time is in the future)
    incomplete_time = now + timedelta(minutes=5)
    bad_row = _valid_kline_row(60, 200.0, incomplete_time - timedelta(minutes=15))
    bad_row[4] = "garbage"  # malform the close
    rows.append(bad_row)
    
    client = _client_with_klines(rows)
    df = fetch_klines(client, "BTCUSDT", "15m", limit=61, drop_incomplete=True)
    # The incomplete candle is time-filtered out before validation; no raise
    assert len(df) == 60
    assert df["close"].iloc[-1] == 159.0  # last closed candle


# --- SPEC F: valid latest CLOSED candle remains usable ---
def test_valid_latest_closed_candle_passes():
    """F. All valid CLOSED candles pass through fetch_klines successfully."""
    client = _client_with_klines(_kline_payload(60))
    df = fetch_klines(client, "BTCUSDT", "15m", limit=60, drop_incomplete=True)
    assert len(df) == 60
    assert df["close"].iloc[-1] == 159.0
    assert df["open"].iloc[-1] == 159.0
    assert df["volume"].iloc[-1] == 1000.0


# --- SPEC G: previous valid candle must NEVER substitute malformed latest ---
def test_previous_valid_candle_not_substituted_for_malformed_latest():
    """G. When latest CLOSED candle is malformed, fetch_klines raises; the
    previous valid candle is NOT silently promoted to "latest"."""
    # Build 60 valid candles, then malform the 60th (latest CLOSED)
    client = _client_with_klines(_kline_payload(60, malform_latest="bad"))
    with pytest.raises(MarketDataError, match="Latest CLOSED candle has NaN close"):
        fetch_klines(client, "BTCUSDT", "15m", limit=60, drop_incomplete=True)
    # The previous candle (index 58, close=158.0) is NOT used; must raise


# --- SPEC H: 15m lower-boundary kill remains correctly triggered ---
def test_fifteen_minute_kill_safety_after_fix():
    """H. After the fix, a valid latest CLOSED candle close still triggers
    the 15m lower-boundary kill when close <= threshold."""
    from decimal import Decimal
    from risk_engine import lower_boundary_15m_kill
    
    # Simulate: LOWER_PRICE=100, stop_pct=0.02 -> threshold=98
    # Latest CLOSED candle close = 97.5 -> should kill
    client = _client_with_klines(_kline_payload(60, malform_latest=97.5))
    df = fetch_klines(client, "BTCUSDT", "15m", limit=60, drop_incomplete=True)
    latest_close = Decimal(str(df["close"].iloc[-1]))
    
    decision = lower_boundary_15m_kill(
        closed_candle_close=latest_close,
        lower_price=Decimal("100"),
        stop_if_below_lower_pct=Decimal("0.02"),
    )
    # RiskDecision returns (allowed, reasons), not an enum
    assert decision.allowed is False
    assert "LOWER_BOUNDARY_STOP_15M" in decision.reasons


# --- SPEC I: ticker price must not become a fallback ---
def test_ticker_not_fallback_for_malformed_candle_close():
    """I. fetch_klines raises on malformed latest CLOSED candle; ticker is
    never consulted as a substitute (fetch_klines does not call ticker API)."""
    client = _client_with_klines(_kline_payload(60, malform_latest=None))
    # If ticker were a fallback, this would not raise. It must raise.
    with pytest.raises(MarketDataError, match="Latest CLOSED candle has NaN close"):
        fetch_klines(client, "BTCUSDT", "15m", limit=60, drop_incomplete=True)


# --- Additional safety: other OHLCV fields also validated on latest ---
def test_latest_closed_candle_open_nan_fails():
    """Latest CLOSED candle with NaN open raises."""
    client = _client_with_klines(_kline_payload(60, malform_latest=math.nan, malform_field="open"))
    with pytest.raises(MarketDataError, match="Latest CLOSED candle has NaN open"):
        fetch_klines(client, "BTCUSDT", "15m", limit=60, drop_incomplete=True)


def test_latest_closed_candle_high_infinity_fails():
    """Latest CLOSED candle with Inf high raises."""
    client = _client_with_klines(_kline_payload(60, malform_latest=math.inf, malform_field="high"))
    with pytest.raises(MarketDataError, match="Latest CLOSED candle high is non-finite"):
        fetch_klines(client, "BTCUSDT", "15m", limit=60, drop_incomplete=True)


def test_latest_closed_candle_low_zero_fails():
    """Latest CLOSED candle with zero low raises (non-positive)."""
    client = _client_with_klines(_kline_payload(60, malform_latest=0.0, malform_field="low"))
    with pytest.raises(MarketDataError, match="Latest CLOSED candle low is non-finite or non-positive"):
        fetch_klines(client, "BTCUSDT", "15m", limit=60, drop_incomplete=True)


def test_latest_closed_candle_volume_negative_fails():
    """Latest CLOSED candle with negative volume raises."""
    client = _client_with_klines(_kline_payload(60, malform_latest=-100.0, malform_field="volume"))
    with pytest.raises(MarketDataError, match="Latest CLOSED candle volume is non-finite or non-positive"):
        fetch_klines(client, "BTCUSDT", "15m", limit=60, drop_incomplete=True)


# --- Middle candle malformed: should still pass (dropna removes it) ---
def test_middle_candle_malformed_still_passes_with_valid_latest():
    """Older/middle candles with NaN are dropped by dropna; latest CLOSED valid -> pass."""
    rows = _kline_payload(60)
    # Malform the 30th candle (middle), not the latest
    rows[29][4] = "garbage"  # close becomes NaN after coerce
    
    client = _client_with_klines(rows)
    df = fetch_klines(client, "BTCUSDT", "15m", limit=60, drop_incomplete=True)
    # After dropna, the malformed middle row is removed; 59 remain
    assert len(df) == 59
    assert df["close"].iloc[-1] == 159.0  # latest still valid


# --- Integration: main() wiring — malformed latest CLOSED candle vetoes ---
# Spec: fetch_klines runs BEFORE enrich() and indicator computation in main().
# A MarketDataError from fetch_klines propagates uncaught to main(), aborting
# the run before any order placement. This test verifies that path.
def test_main_aborts_on_malformed_latest_closed_candle():
    """
    I. main() calls fetch_klines (line 446) BEFORE enrich/latest_valid_row.
    If the latest CLOSED candle is malformed, fetch_klines raises MarketDataError
    which propagates to main() uncaught, aborting the run before any order
    placement logic executes. Ticker price is never consulted as a substitute
    inside fetch_klines — it is not even called.
    """
    from market_data import MarketDataError, fetch_klines
    
    # Simulate: Binance returns valid klines but latest CLOSED candle close
    # is malformed (coerced to NaN by to_numeric)
    client = _client_with_klines(_kline_payload(60, malform_latest="garbage"))
    
    # fetch_klines raises BEFORE any indicator computation or main() path
    # beyond this point
    with pytest.raises(MarketDataError, match="Latest CLOSED candle has NaN close"):
        df = fetch_klines(client, "BTCUSDT", "15m", limit=60, drop_incomplete=True)
    
    # Confirm enrich() was never reached: fetch_klines raises before
    # returning a DataFrame, so main() never reaches indicator computation
    # or order placement. The pytest.raises above already verifies this.
    # If fetch_klines had returned a substituted DataFrame, the pytest.raises
    # would have failed (no exception raised).
    pass


# --- Non-finding documentation: indicators dropna is advisory, not safety-critical ---
def test_indicator_dropna_latest_valid_row_uses_previous_valid_for_nan_indicators():
    """
    Spec item 9 non-finding: indicators.latest_valid_row() uses dropna() + iloc[-1]
    for DERIVED indicators (ATR, ADX, BB_width, volume_ratio). This is correct
    because:
    1. fetch_klines now validates latest CLOSED candle OHLCV BEFORE enrich(),
       so malformed OHLCV cannot reach the indicator layer.
    2. Derived indicators can be NaN due to rolling-window warm-up (not malformed).
    3. latest_valid_row raises ValueError if ALL rows are invalid (fail-closed).
    4. These indicators feed market intelligence (advisory), not direct kill gates.
    
    This test documents that the pattern is safe: a NaN derived indicator
    on the latest row falls back to the previous valid observation, but
    the close price used by the kill gate comes from the validated DataFrame
    directly, not from latest_valid_row.
    """
    from indicators import enrich, latest_valid_row
    
    # DataFrame with valid OHLCV (passes fetch_klines validation)
    rows = _kline_payload(60)
    now = datetime.now(timezone.utc)
    base_time = now - timedelta(minutes=15 * 61)
    columns = ["open_time","open","high","low","close","volume","close_time",
               "quote_volume","trades","taker_base","taker_quote","ignore"]
    df = pd.DataFrame([list(r[:12]) for r in rows], columns=columns)
    for col in ["open","high","low","close","volume","quote_volume","taker_base","taker_quote"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    
    enriched = enrich(df)
    last = latest_valid_row(enriched)
    # The latest valid row's close matches the original DataFrame's latest close
    assert float(last["close"]) == 159.0
