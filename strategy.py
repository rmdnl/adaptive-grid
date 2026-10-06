"""Strategy evaluation: Regime + Recovery entry/exit gates.

Indicator set (CLOSED candles only, INDICATOR_TIMEFRAME): ADX(14) with
+DI/-DI, Stoch RSI (RSI 14, stoch 14, smoothK 3, smoothD 3, 0..1 scale),
ATR(14). The legacy RSI / BB %B / Volume-Oscillator / Z-score gates are
removed — they are no longer part of any decision.

Entry requires ALL conditions:
    ADX < ENTRY_ADX_MAX                 (ranging regime)
    ADX <= ADX[ADX_REGIME_LOOKBACK bars ago]   (regime not strengthening)
    Stoch RSI %K crosses UP through %D  (K[-2] <= D[-2] and K[-1] > D[-1])
    %K < ENTRY_STOCH_K_MAX              (recovery still early)
Grid economics and the global MIN_HOURS_BETWEEN_ENTRIES pacing gate are
enforced separately by the bot/plan layer.

Exit fires when ANY condition holds and carries a SEVERITY:
    SOFT  — ADX > EXIT_ADX_MIN and +DI > -DI, or Stoch RSI %K > EXIT_STOCH_K_MAX:
            cancel unfilled BUYs only, let SELLs fill, never market-sell.
    HARD  — ADX > EXIT_ADX_MIN and -DI > +DI:
            cancel everything and liquidate.
    (the 15m lower-boundary breach is a separate HARD path in the bot)

EXIT HAS PRIORITY OVER ENTRY. Insufficient indicator history blocks entry
(fail-closed: NO TRADE) and cannot trigger an exit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import indicators

# How many bars back the ADX regime must not have strengthened.
ADX_REGIME_LOOKBACK = 3

STOCH_SCALE = 1.0  # Stoch RSI is on the 0..1 scale


@dataclass(frozen=True)
class IndicatorSnapshot:
    symbol: str = ""
    last_close: Optional[float] = None
    last_candle_time: Optional[int] = None
    adx: Optional[float] = None
    adx_prev: Optional[float] = None       # ADX ADX_REGIME_LOOKBACK bars ago
    plus_di: Optional[float] = None
    minus_di: Optional[float] = None
    stoch_k: Optional[float] = None
    stoch_d: Optional[float] = None
    stoch_k_prev: Optional[float] = None   # %K two closed bars ago
    stoch_d_prev: Optional[float] = None   # %D two closed bars ago
    atr: Optional[float] = None


@dataclass(frozen=True)
class EntryDecision:
    allowed: bool
    blocker: Optional[str] = None


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: Optional[str] = None
    severity: Optional[str] = None  # "soft" | "hard" | None


def build_snapshot(
    closed_candles: List[Dict],
    *,
    symbol: str = "",
    adx_period: int = 14,
    rsi_period: int = 14,
    stoch_rsi_length: int = 14,
    stoch_smooth_k: int = 3,
    stoch_smooth_d: int = 3,
    atr_period: int = 14,
    adx_regime_lookback: int = ADX_REGIME_LOOKBACK,
) -> IndicatorSnapshot:
    """Build the indicator snapshot from CLOSED candles only."""
    if not closed_candles:
        return IndicatorSnapshot(symbol=symbol)
    closes = [float(c["close"]) for c in closed_candles]
    highs = [float(c["high"]) for c in closed_candles]
    lows = [float(c["low"]) for c in closed_candles]

    dmi = indicators.adx_dmi(highs, lows, closes, adx_period)
    adx_value = adx_prev = plus_di = minus_di = None
    if dmi is not None:
        adx_series, plus_di, minus_di = dmi
        adx_value = adx_series[-1]
        if len(adx_series) > adx_regime_lookback:
            adx_prev = adx_series[-1 - adx_regime_lookback]

    stoch = indicators.stoch_rsi(
        closes, rsi_period, stoch_rsi_length, stoch_smooth_k, stoch_smooth_d
    )
    stoch_k = stoch_d = stoch_k_prev = stoch_d_prev = None
    if stoch is not None:
        k_series, d_series = stoch
        stoch_k, stoch_d = k_series[-1], d_series[-1]
        stoch_k_prev, stoch_d_prev = k_series[-2], d_series[-2]

    return IndicatorSnapshot(
        symbol=symbol,
        last_close=closes[-1],
        last_candle_time=int(closed_candles[-1]["close_time"]),
        adx=adx_value,
        adx_prev=adx_prev,
        plus_di=plus_di,
        minus_di=minus_di,
        stoch_k=stoch_k,
        stoch_d=stoch_d,
        stoch_k_prev=stoch_k_prev,
        stoch_d_prev=stoch_d_prev,
        atr=indicators.atr(highs, lows, closes, atr_period),
    )


def stoch_kd_cross_up(snapshot: IndicatorSnapshot) -> bool:
    """True when %K crossed UP through %D on the last closed bar:
    K[-2] <= D[-2] and K[-1] > D[-1]. Missing values never cross."""
    if (
        snapshot.stoch_k is None
        or snapshot.stoch_d is None
        or snapshot.stoch_k_prev is None
        or snapshot.stoch_d_prev is None
    ):
        return False
    return snapshot.stoch_k_prev <= snapshot.stoch_d_prev and snapshot.stoch_k > snapshot.stoch_d


def entry_blockers(snapshot: IndicatorSnapshot, cfg) -> List[str]:
    """ALL currently-failed entry conditions, in gate order (empty list =
    every entry condition satisfied). Single source of truth for both the
    entry decision and the per-condition blocker telemetry."""
    if (
        snapshot.adx is None
        or snapshot.adx_prev is None
        or snapshot.stoch_k is None
        or snapshot.stoch_d is None
        or snapshot.stoch_k_prev is None
        or snapshot.stoch_d_prev is None
    ):
        return ["insufficient_data"]
    failed: List[str] = []
    if not (snapshot.adx < cfg.entry_adx_max):
        failed.append("adx_not_low")
    if not (snapshot.adx <= snapshot.adx_prev):
        failed.append("adx_rising")
    if not stoch_kd_cross_up(snapshot):
        failed.append("stoch_no_cross")
    if not (snapshot.stoch_k < cfg.entry_stoch_k_max):
        failed.append("stoch_k_too_high")
    return failed


def evaluate_entry(snapshot: IndicatorSnapshot, cfg) -> EntryDecision:
    failed = entry_blockers(snapshot, cfg)
    if not failed:
        return EntryDecision(True, None)
    return EntryDecision(False, failed[0])


def evaluate_exit(snapshot: IndicatorSnapshot, cfg) -> ExitDecision:
    # Missing indicators cannot fire their condition (fail-closed: an exit
    # is never triggered by fabricated data).
    if (
        snapshot.adx is not None
        and snapshot.plus_di is not None
        and snapshot.minus_di is not None
        and snapshot.adx > cfg.exit_adx_min
    ):
        if snapshot.plus_di > snapshot.minus_di:
            return ExitDecision(True, "adx_trending_up", "soft")
        if snapshot.minus_di > snapshot.plus_di:
            return ExitDecision(True, "adx_trending_down", "hard")
    if snapshot.stoch_k is not None and snapshot.stoch_k > cfg.exit_stoch_k_max:
        return ExitDecision(True, "stoch_k_overbought", "soft")
    return ExitDecision(False, None, None)


def evaluate_signal(
    snapshot: IndicatorSnapshot, cfg
) -> Tuple[ExitDecision, EntryDecision]:
    """Combined evaluation. Exit has priority over entry: when an exit
    condition fires, entry is refused even for a symbol without a grid."""
    exit_decision = evaluate_exit(snapshot, cfg)
    if exit_decision.should_exit:
        return exit_decision, EntryDecision(False, "exit_priority")
    return exit_decision, evaluate_entry(snapshot, cfg)


def cooldown_active(now_ts: float, cooldown_until: Optional[float]) -> bool:
    """True while a symbol is inside its post-exit cooldown window."""
    return cooldown_until is not None and now_ts < cooldown_until
