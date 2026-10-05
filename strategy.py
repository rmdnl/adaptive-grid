"""Strategy evaluation: indicator snapshot, entry gate, exit gate.

Entry requires ALL conditions (strict thresholds, never loosened):
    ADX < ENTRY_ADX_MAX AND RSI < ENTRY_RSI_MAX
    AND VolumeOscillator > ENTRY_VOLUME_OSC_MIN AND %B <= ENTRY_BB_PERCENT_B_MAX

Exit fires when ANY condition holds:
    RSI >= EXIT_RSI_MIN OR ADX > EXIT_ADX_MIN
    OR %B > EXIT_BB_PERCENT_B_MIN OR |Z| > EXIT_ZSCORE_ABS_MAX

EXIT HAS PRIORITY OVER ENTRY. Insufficient indicator history blocks entry
(fail-closed: NO TRADE) and cannot trigger an exit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import indicators


@dataclass(frozen=True)
class IndicatorSnapshot:
    symbol: str = ""
    last_close: Optional[float] = None
    last_candle_time: Optional[int] = None
    adx: Optional[float] = None
    rsi: Optional[float] = None
    percent_b: Optional[float] = None
    volume_osc: Optional[float] = None
    zscore: Optional[float] = None
    atr: Optional[float] = None


@dataclass(frozen=True)
class EntryDecision:
    allowed: bool
    blocker: Optional[str] = None


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: Optional[str] = None


def build_snapshot(
    closed_candles: List[Dict],
    *,
    symbol: str = "",
    adx_period: int = 14,
    rsi_period: int = 14,
    bb_period: int = 20,
    bb_std: float = 2.0,
    vo_fast: int = 5,
    vo_slow: int = 10,
    zscore_period: int = 20,
    atr_period: int = 14,
) -> IndicatorSnapshot:
    """Build the indicator snapshot from CLOSED candles only."""
    if not closed_candles:
        return IndicatorSnapshot(symbol=symbol)
    closes = [float(c["close"]) for c in closed_candles]
    highs = [float(c["high"]) for c in closed_candles]
    lows = [float(c["low"]) for c in closed_candles]
    volumes = [float(c["volume"]) for c in closed_candles]
    return IndicatorSnapshot(
        symbol=symbol,
        last_close=closes[-1],
        last_candle_time=int(closed_candles[-1]["close_time"]),
        adx=indicators.adx(highs, lows, closes, adx_period),
        rsi=indicators.rsi(closes, rsi_period),
        percent_b=indicators.bollinger_percent_b(closes, bb_period, bb_std),
        volume_osc=indicators.volume_oscillator(volumes, vo_fast, vo_slow),
        zscore=indicators.zscore(closes, zscore_period),
        atr=indicators.atr(highs, lows, closes, atr_period),
    )


def evaluate_entry(snapshot: IndicatorSnapshot, cfg) -> EntryDecision:
    if (
        snapshot.adx is None
        or snapshot.rsi is None
        or snapshot.percent_b is None
        or snapshot.volume_osc is None
    ):
        return EntryDecision(False, "insufficient_data")
    if not (snapshot.adx < cfg.entry_adx_max):
        return EntryDecision(False, "adx_not_low")
    if not (snapshot.rsi < cfg.entry_rsi_max):
        return EntryDecision(False, "rsi_not_low")
    if not (snapshot.volume_osc > cfg.entry_volume_osc_min):
        return EntryDecision(False, "volume_osc_not_positive")
    if not (snapshot.percent_b <= cfg.entry_bb_percent_b_max):
        return EntryDecision(False, "percent_b_not_low")
    return EntryDecision(True, None)


def evaluate_exit(snapshot: IndicatorSnapshot, cfg) -> ExitDecision:
    # Missing indicators cannot fire their condition (fail-closed: an exit
    # is never triggered by fabricated data).
    if snapshot.rsi is not None and snapshot.rsi >= cfg.exit_rsi_min:
        return ExitDecision(True, "rsi_overbought")
    if snapshot.adx is not None and snapshot.adx > cfg.exit_adx_min:
        return ExitDecision(True, "adx_trending")
    if snapshot.percent_b is not None and snapshot.percent_b > cfg.exit_bb_percent_b_min:
        return ExitDecision(True, "bb_upper_break")
    if snapshot.zscore is not None and abs(snapshot.zscore) > cfg.exit_zscore_abs_max:
        return ExitDecision(True, "zscore_extreme")
    return ExitDecision(False, None)


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
