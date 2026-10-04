"""Deterministic technical indicators over CLOSED candles only.

Every function returns the value as of the last element of the input
series and None when history is insufficient or the value is degenerate
(e.g. zero deviation). Insufficient history means NO TRADE — never a
fabricated number. No lookahead: only the data passed in is used, and
callers must pass closed candles (see `closed_candles`).
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Sequence


def closed_candles(candles: List[Dict], now_ms: Optional[int] = None) -> List[Dict]:
    """Drop candles that have not closed yet (close_time in the future)."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return [c for c in candles if int(c["close_time"]) < now_ms]


def _stddev(values: Sequence[float]) -> float:
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return var ** 0.5


def rsi(closes: Sequence[float], period: int) -> Optional[float]:
    """Wilder's RSI. None if fewer than period+1 closes, or flat history."""
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        if change > 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else None
    if avg_gain == 0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int) -> Optional[float]:
    """Wilder's ATR. None if fewer than period+1 candles."""
    if len(closes) < period + 1:
        return None
    trs = _true_ranges(highs, lows, closes)
    if len(trs) < period:
        return None
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def _true_ranges(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> List[float]:
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return trs


def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int) -> Optional[float]:
    """Wilder's ADX. None if history is insufficient or degenerate (zero
    true range / zero directional movement)."""
    n = len(closes)
    if n < 2 * period + 1:
        return None
    plus_dms: List[float] = []
    minus_dms: List[float] = []
    trs: List[float] = []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dms.append(up if (up > 0 and up > down) else 0.0)
        minus_dms.append(down if (down > 0 and down > up) else 0.0)
        trs.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )

    def dx_of(sm_plus: float, sm_minus: float, sm_tr: float) -> Optional[float]:
        if sm_tr <= 0:
            return None
        plus_di = 100.0 * sm_plus / sm_tr
        minus_di = 100.0 * sm_minus / sm_tr
        denom = plus_di + minus_di
        if denom <= 0:
            return None
        return 100.0 * abs(plus_di - minus_di) / denom

    sm_plus = sum(plus_dms[:period])
    sm_minus = sum(minus_dms[:period])
    sm_tr = sum(trs[:period])
    dxs: List[float] = []
    first = dx_of(sm_plus, sm_minus, sm_tr)
    if first is not None:
        dxs.append(first)
    for i in range(period, len(trs)):
        sm_plus = sm_plus - sm_plus / period + plus_dms[i]
        sm_minus = sm_minus - sm_minus / period + minus_dms[i]
        sm_tr = sm_tr - sm_tr / period + trs[i]
        dx = dx_of(sm_plus, sm_minus, sm_tr)
        if dx is not None:
            dxs.append(dx)
    if len(dxs) < period:
        return None
    value = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        value = (value * (period - 1) + dx) / period
    return value


def bollinger_percent_b(closes: Sequence[float], period: int, num_std: float) -> Optional[float]:
    """Bollinger %B = (close - lower) / (upper - lower). None if the band
    is degenerate (upper <= lower)."""
    if len(closes) < period:
        return None
    window = list(closes[-period:])
    mean = sum(window) / period
    sd = _stddev(window)
    upper = mean + num_std * sd
    lower = mean - num_std * sd
    if upper <= lower:
        return None
    return (closes[-1] - lower) / (upper - lower)


def volume_oscillator(volumes: Sequence[float], fast: int, slow: int) -> Optional[float]:
    """VO = SMA(volume, fast) / SMA(volume, slow) - 1."""
    if len(volumes) < slow or fast >= slow:
        return None
    fast_avg = sum(volumes[-fast:]) / fast
    slow_avg = sum(volumes[-slow:]) / slow
    if slow_avg == 0:
        return None
    return fast_avg / slow_avg - 1.0


def zscore(closes: Sequence[float], period: int) -> Optional[float]:
    """Z = (close - SMA(period)) / population-std(period). None if the
    deviation is zero."""
    if len(closes) < period:
        return None
    window = list(closes[-period:])
    mean = sum(window) / period
    sd = _stddev(window)
    if sd == 0:
        return None
    return (closes[-1] - mean) / sd
