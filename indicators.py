"""Deterministic technical indicators over CLOSED candles only.

Regime + Recovery indicator set: ADX(14) with +DI/-DI, Stoch RSI
(RSI 14, stoch 14, smoothK 3, smoothD 3) and Wilder's ATR(14).

Every function returns None (or an empty/short series) when history is
insufficient or the value is degenerate (e.g. zero true range). Insufficient
history means NO TRADE — never a fabricated number. No lookahead: only the
data passed in is used, and callers must pass closed candles (see
`closed_candles`).
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Sequence, Tuple


def closed_candles(candles: List[Dict], now_ms: Optional[int] = None) -> List[Dict]:
    """Drop candles that have not closed yet (close_time in the future)."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return [c for c in candles if int(c["close_time"]) < now_ms]


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


def rsi_series(closes: Sequence[float], period: int) -> List[float]:
    """Wilder's RSI evaluated at every bar where it is defined.

    The first element corresponds to index `period` of the input closes
    (the first bar a full change window exists). Flat-loss history yields
    0.0, flat-gain history 100.0 (standard conventions), so the series
    never contains fabricated None holes once defined.
    """
    n = len(closes)
    if n < period + 1:
        return []
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
    out: List[float] = [_rsi_of(avg_gain, avg_loss)]
    for i in range(period + 1, n):
        change = closes[i] - closes[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out.append(_rsi_of(avg_gain, avg_loss))
    return out


def _rsi_of(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


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


def adx_dmi(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int
) -> Optional[Tuple[List[float], float, float]]:
    """Wilder's ADX together with the latest +DI and -DI.

    Returns (adx_series, plus_di, minus_di) where adx_series holds every
    smoothed-ADX value in order (the last element is the current ADX), or
    None when history is insufficient or degenerate (zero true range /
    zero directional movement). The +DI/-DI values are the most recent
    smoothed directional-index values (100 * smoothed DM / smoothed TR).
    """
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

    sm_plus = sum(plus_dms[:period])
    sm_minus = sum(minus_dms[:period])
    sm_tr = sum(trs[:period])
    if sm_tr <= 0:
        return None

    dxs: List[float] = []
    plus_di_last = 100.0 * sm_plus / sm_tr
    minus_di_last = 100.0 * sm_minus / sm_tr
    denom = plus_di_last + minus_di_last
    if denom > 0:
        dxs.append(100.0 * abs(plus_di_last - minus_di_last) / denom)
    for i in range(period, len(trs)):
        sm_plus = sm_plus - sm_plus / period + plus_dms[i]
        sm_minus = sm_minus - sm_minus / period + minus_dms[i]
        sm_tr = sm_tr - sm_tr / period + trs[i]
        if sm_tr <= 0:
            return None
        plus_di_last = 100.0 * sm_plus / sm_tr
        minus_di_last = 100.0 * sm_minus / sm_tr
        denom = plus_di_last + minus_di_last
        if denom > 0:
            dxs.append(100.0 * abs(plus_di_last - minus_di_last) / denom)
    if len(dxs) < period:
        return None
    adx_series: List[float] = []
    value = sum(dxs[:period]) / period
    adx_series.append(value)
    for dx in dxs[period:]:
        value = (value * (period - 1) + dx) / period
        adx_series.append(value)
    return adx_series, plus_di_last, minus_di_last


def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int) -> Optional[float]:
    """Current Wilder's ADX (scalar convenience over adx_dmi)."""
    result = adx_dmi(highs, lows, closes, period)
    if result is None:
        return None
    return result[0][-1]


def stoch_rsi(
    closes: Sequence[float],
    rsi_period: int,
    stoch_period: int,
    smooth_k: int,
    smooth_d: int,
) -> Optional[Tuple[List[float], List[float]]]:
    """Stochastic RSI on the 0..1 scale with smoothed %K and %D.

    raw = (rsi - min(rsi over stoch window)) / (max - min);
    %K = SMA(raw, smooth_k); %D = SMA(%K, smooth_d).

    Returns (k_series, d_series) — full smoothed series in bar order, the
    last elements being the current values — or None when history is
    insufficient (fail-closed: NO TRADE) or the stoch window is flat
    (max == min), which carries no stochastic information.
    """
    if rsi_period < 1 or stoch_period < 1 or smooth_k < 1 or smooth_d < 1:
        return None
    rsi_vals = rsi_series(closes, rsi_period)
    if len(rsi_vals) < stoch_period + smooth_k + smooth_d - 1:
        # %D needs smooth_d %K values, %K needs smooth_k raw values, raw
        # needs stoch_period RSI values, and at least two %K/%D pairs must
        # exist for crossover detection.
        return None

    raws: List[float] = []
    for i in range(stoch_period - 1, len(rsi_vals)):
        window = rsi_vals[i - stoch_period + 1 : i + 1]
        lo = min(window)
        hi = max(window)
        if hi == lo:
            return None  # flat RSI window: no stochastic information
        raws.append((rsi_vals[i] - lo) / (hi - lo))

    k_series: List[float] = []
    for i in range(smooth_k - 1, len(raws)):
        k_series.append(sum(raws[i - smooth_k + 1 : i + 1]) / smooth_k)
    if len(k_series) < smooth_d:
        return None
    d_series: List[float] = []
    for i in range(smooth_d - 1, len(k_series)):
        d_series.append(sum(k_series[i - smooth_d + 1 : i + 1]) / smooth_d)
    if len(d_series) < 2:
        return None  # crossover detection needs at least two K/D pairs
    return k_series, d_series
