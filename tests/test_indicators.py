"""Indicator tests: deterministic math, insufficient history, closed candles."""

from __future__ import annotations

import pytest

import indicators
from conftest import make_candle


# ----- RSI (Wilder) -----

def test_rsi_all_gains_is_100():
    closes = [float(i) for i in range(1, 16)]
    assert indicators.rsi(closes, 14) == 100.0


def test_rsi_all_losses_is_0():
    closes = [float(i) for i in range(15, 0, -1)]
    assert indicators.rsi(closes, 14) == 0.0


def test_rsi_mixed_matches_wilder_reference():
    closes = [10, 11, 10.5, 11.5, 10, 11, 12, 11, 12.5, 11.5, 12, 13, 12.5, 13.5, 14]
    # avg_gain = 8.5/14, avg_loss = 4.5/14 -> RSI = 100 - 100/(1 + 8.5/4.5)
    expected = 100.0 - 100.0 / (1.0 + 8.5 / 4.5)
    assert indicators.rsi(closes, 14) == pytest.approx(expected, abs=1e-9)
    assert indicators.rsi(closes, 14) == pytest.approx(65.384615, abs=1e-4)


def test_rsi_insufficient_history_is_none():
    assert indicators.rsi([1.0] * 14, 14) is None


def test_rsi_flat_history_is_none():
    assert indicators.rsi([10.0] * 30, 14) is None


# ----- ATR (Wilder) -----

def test_atr_constant_range():
    highs = [11, 12, 13, 14]
    lows = [9, 10, 11, 12]
    closes = [10, 11, 12, 13]
    assert indicators.atr(highs, lows, closes, 3) == pytest.approx(2.0)


def test_atr_insufficient_history_is_none():
    assert indicators.atr([11, 12, 13], [9, 10, 11], [10, 11, 12], 3) is None


# ----- ADX (Wilder) -----

def test_adx_strong_uptrend_is_100():
    n = 40
    closes = [100.0 + i for i in range(n)]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    assert indicators.adx(highs, lows, closes, 14) == pytest.approx(100.0)


def test_adx_insufficient_history_is_none():
    closes = [100.0 + i for i in range(28)]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    # needs 2*period+1 = 29 candles
    assert indicators.adx(highs, lows, closes, 14) is None


def test_adx_flat_degenerate_is_none():
    closes = [100.0] * 40
    highs = [100.0] * 40
    lows = [100.0] * 40
    assert indicators.adx(highs, lows, closes, 14) is None


# ----- Bollinger %B -----

def test_percent_b_within_bands():
    closes = [1.0, 2.0, 3.0, 4.0, 5.0]
    # mean 3, population sd sqrt(2); %B = (5 - (3-2*sqrt(2))) / (4*sqrt(2))
    sd = 2 ** 0.5
    expected = (5.0 - (3.0 - 2 * sd)) / (4 * sd)
    assert indicators.bollinger_percent_b(closes, 5, 2.0) == pytest.approx(expected, abs=1e-9)
    assert indicators.bollinger_percent_b(closes, 5, 2.0) == pytest.approx(0.8535534, abs=1e-6)


def test_percent_b_below_lower_band_is_negative():
    closes = [10.0] * 19 + [5.0]
    # %B simplifies exactly to 0.5 - sd where sd = sqrt(1.1875)
    assert indicators.bollinger_percent_b(closes, 20, 2.0) == pytest.approx(-0.5897247, abs=1e-6)


def test_percent_b_insufficient_history_is_none():
    assert indicators.bollinger_percent_b([1.0] * 19, 20, 2.0) is None


# ----- Volume Oscillator -----

def test_volume_oscillator_basic():
    volumes = [float(i) for i in range(1, 11)]
    # fast avg = 8, slow avg = 5.5
    assert indicators.volume_oscillator(volumes, 5, 10) == pytest.approx(8 / 5.5 - 1.0)


def test_volume_oscillator_insufficient_history_is_none():
    assert indicators.volume_oscillator([1.0] * 9, 5, 10) is None


def test_volume_oscillator_fast_ge_slow_is_none():
    assert indicators.volume_oscillator([1.0] * 20, 10, 10) is None


# ----- Z-Score -----

def test_zscore_dip_below_mean():
    closes = [10.0] * 19 + [5.0]
    sd = 1.1875 ** 0.5
    assert indicators.zscore(closes, 20) == pytest.approx((5.0 - 9.75) / sd, abs=1e-9)
    assert indicators.zscore(closes, 20) == pytest.approx(-4.3588989, abs=1e-6)


def test_zscore_flat_is_none():
    assert indicators.zscore([10.0] * 20, 20) is None


# ----- closed candle handling -----

def test_closed_candles_drops_unclosed():
    candles = [
        make_candle(1.0, 2.0, 0.5, 1.0, 0, 999),      # closed
        make_candle(1.0, 2.0, 0.5, 1.0, 1000, 1999),  # closed
        make_candle(1.0, 2.0, 0.5, 1.0, 2000, 2999),  # unclosed at now=2500
    ]
    closed = indicators.closed_candles(candles, now_ms=2500)
    assert len(closed) == 2
    assert closed[-1]["close_time"] == 1999


def test_closed_candles_strict_boundary():
    # close_time == now is still the current candle -> dropped
    candles = [make_candle(1.0, 2.0, 0.5, 1.0, 0, 1000)]
    assert indicators.closed_candles(candles, now_ms=1000) == []
    assert len(indicators.closed_candles(candles, now_ms=1001)) == 1
