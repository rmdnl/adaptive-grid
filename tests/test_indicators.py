"""Indicator tests: deterministic math, insufficient history, closed candles.

Regime + Recovery indicator set: ADX with +DI/-DI, Stoch RSI, ATR. Every
non-trivial value below is checked against a HAND-COMPUTED reference.
"""

from __future__ import annotations

import pytest

import indicators
from conftest import make_candle


# ----- ATR (Wilder) -----

def test_atr_constant_range():
    highs = [11, 12, 13, 14]
    lows = [9, 10, 11, 12]
    closes = [10, 11, 12, 13]
    assert indicators.atr(highs, lows, closes, 3) == pytest.approx(2.0)


def test_atr_insufficient_history_is_none():
    assert indicators.atr([11, 12, 13], [9, 10, 11], [10, 11, 12], 3) is None


# ----- RSI series (inner Stoch RSI engine, Wilder) -----

def test_rsi_series_all_gains_is_100():
    closes = [float(i) for i in range(1, 6)]
    assert indicators.rsi_series(closes, 3) == [100.0, 100.0]


def test_rsi_series_all_losses_is_0():
    closes = [float(i) for i in range(5, 0, -1)]
    assert indicators.rsi_series(closes, 3) == [0.0, 0.0]


def test_rsi_series_matches_wilder_reference():
    closes = [10, 11, 10.5, 11.5, 10, 11, 12, 11, 12.5, 11.5, 12, 13, 12.5, 13.5, 14]
    # first (and only) value: avg_gain = 8.5/14, avg_loss = 4.5/14
    # RSI = 100 - 100/(1 + 8.5/4.5) = 65.384615
    series = indicators.rsi_series(closes, 14)
    assert len(series) == len(closes) - 14
    assert series[0] == pytest.approx(65.384615, abs=1e-6)


def test_rsi_series_insufficient_history_is_empty():
    assert indicators.rsi_series([1.0] * 3, 14) == []


# ----- ADX with +DI/-DI (Wilder) -----

def test_adx_dmi_pure_uptrend_hand_computed():
    """Seven monotonic up candles, period 3: every +DM=1, -DM=0, TR=2.
    +DI = 100*3/6 = 50, -DI = 0, every DX = 100 -> ADX = 100."""
    highs = [12, 13, 14, 15, 16, 17, 18]
    lows = [10, 11, 12, 13, 14, 15, 16]
    closes = [11, 12, 13, 14, 15, 16, 17]
    result = indicators.adx_dmi(highs, lows, closes, 3)
    assert result is not None
    adx_series, plus_di, minus_di = result
    assert adx_series[-1] == pytest.approx(100.0, abs=1e-9)
    assert plus_di == pytest.approx(50.0, abs=1e-9)
    assert minus_di == pytest.approx(0.0, abs=1e-9)


def test_adx_dmi_zigzag_hand_computed():
    """Alternating up/down candles, period 3 — hand-computed Wilder DMI:
    plus_dms=[1,0,1,0,1,0], minus_dms=[0,1,0,1,0,1], TR=2 everywhere.
      window(candles 1-3): sm+=2, sm-=1, smTR=6 -> +DI=33.333, -DI=16.667,
                           DX = 16.667/50*100 = 33.333
      candle 4 (down):     sm+=1.3333, sm-=1.6667 -> +DI=22.222, -DI=27.778,
                           DX = 5.5556/50*100 = 11.111
      candle 5 (up):       sm+=1.8889, sm-=1.1111 -> +DI=31.481, -DI=18.519,
                           DX = 12.963/50*100 = 25.926
      candle 6 (down):     sm+=1.2593, sm-=1.7407 -> +DI=20.988, -DI=29.012,
                           DX = 8.0247/50*100 = 16.049
      ADX: mean(first 3 DX)=23.4568, then (23.4568*2+16.0494)/3=20.9877"""
    highs = [10, 11, 10, 11, 10, 11, 10]
    lows = [8, 9, 8, 9, 8, 9, 8]
    closes = [9, 10, 9, 10, 9, 10, 9]
    result = indicators.adx_dmi(highs, lows, closes, 3)
    assert result is not None
    adx_series, plus_di, minus_di = result
    assert len(adx_series) == 2
    assert adx_series[0] == pytest.approx(23.456790, abs=1e-5)
    assert adx_series[1] == pytest.approx(20.987654, abs=1e-5)
    assert plus_di == pytest.approx(20.987654, abs=1e-5)
    assert minus_di == pytest.approx(29.012346, abs=1e-5)


def test_adx_dmi_downtrend_has_minus_di_dominant():
    highs = [18, 17, 16, 15, 14, 13, 12]
    lows = [16, 15, 14, 13, 12, 11, 10]
    closes = [17, 16, 15, 14, 13, 12, 11]
    _, plus_di, minus_di = indicators.adx_dmi(highs, lows, closes, 3)
    assert plus_di == pytest.approx(0.0, abs=1e-9)
    assert minus_di == pytest.approx(50.0, abs=1e-9)


def test_adx_dmi_insufficient_history_is_none():
    closes = [100.0 + i for i in range(6)]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    # needs 2*period+1 = 7 candles
    assert indicators.adx_dmi(highs, lows, closes, 3) is None


def test_adx_dmi_flat_degenerate_is_none():
    assert indicators.adx_dmi([100.0] * 7, [100.0] * 7, [100.0] * 7, 3) is None


def test_adx_scalar_matches_series_tail():
    highs = [10, 11, 10, 11, 10, 11, 10]
    lows = [8, 9, 8, 9, 8, 9, 8]
    closes = [9, 10, 9, 10, 9, 10, 9]
    series = indicators.adx_dmi(highs, lows, closes, 3)[0]
    assert indicators.adx(highs, lows, closes, 3) == pytest.approx(series[-1], abs=1e-12)


# ----- Stochastic RSI -----

def test_stoch_rsi_hand_computed_kd_series():
    """rsi=2, stoch=2, smoothK=1, smoothD=2 over alternating closes:
    RSI series = [50, 75, 37.5, 62.5, 37.5, 62.5]
    raw (stoch window 2) = [1, 0, 1, 0, 1]
    %K = raw (smooth 1) = [1, 0, 1, 0, 1]
    %D = SMA(%K, 2)     = [0.5, 0.5, 0.5, 0.5]"""
    closes = [1, 2, 1, 2, 1, 2, 1, 2]
    result = indicators.stoch_rsi(closes, rsi_period=2, stoch_period=2, smooth_k=1, smooth_d=2)
    assert result is not None
    k_series, d_series = result
    assert k_series == pytest.approx([1.0, 0.0, 1.0, 0.0, 1.0])
    assert d_series == pytest.approx([0.5, 0.5, 0.5, 0.5])


def test_stoch_rsi_scale_is_unit():
    """Values stay on the 0..1 scale for an extreme trend."""
    closes = [float(i) for i in range(1, 20)]  # monotonic up: RSI pinned at 100
    result = indicators.stoch_rsi(closes, rsi_period=5, stoch_period=5, smooth_k=3, smooth_d=3)
    # RSI window flat at 100 -> hi == lo -> degenerate -> None (fail-closed)
    assert result is None


def test_stoch_rsi_insufficient_history_is_none():
    assert indicators.stoch_rsi([1, 2, 1], rsi_period=2, stoch_period=2, smooth_k=1, smooth_d=2) is None


def test_stoch_rsi_last_two_pairs_support_crossover_detection():
    closes = [1, 2, 1, 2, 1, 2, 1, 2]
    k_series, d_series = indicators.stoch_rsi(
        closes, rsi_period=2, stoch_period=2, smooth_k=1, smooth_d=2
    )
    # K[-2] <= D[-2] and K[-1] > D[-1] -> a genuine cross-up on the last bar
    assert k_series[-2] <= d_series[-2]
    assert k_series[-1] > d_series[-1]


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
