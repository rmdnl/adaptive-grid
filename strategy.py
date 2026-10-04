"""Auto-Entry / Auto-Exit Strategy Engine for Binance Spot Grid Bot.

THE authoritative strategy (locked specification):

Entry (ALL must be true — AND logic, the ONLY mandatory entry filters):
  1. ADX(14) < 25            (ADX_ENTRY_MAX, configurable)
  2. RSI(14) < 40            (RSI_ENTRY_MAX, configurable)
  3. Bollinger %B <= 0       (BB_ENTRY_MAX_PERCENT_B, configurable):
                             price at or below the Lower Bollinger Band.

  The Volume Oscillator is NOT an entry requirement — it is computed for
  diagnostics/metrics only.  The previous ADX<20 / RSI<35 / VO>0 logic and
  the "RSI OR %B" alternative trigger are REMOVED.

Exit (ANY triggers — OR logic, at-or-beyond the configured threshold):
  1. RSI(14) >= 70           (RSI_EXIT_MIN)
  2. ADX(14) >= 25           (ADX_EXIT_MIN)
  3. Bollinger %B >= 1       (BB_EXIT_MIN_PERCENT_B)
  4. |Z-Score(20)| >= 2.5    (ZSCORE_ABS_EXIT — either extreme triggers the
     same emergency exit procedure)

  Boundary behavior is pinned by tests per the specification's exit table:
  RSI 70.00 -> EXIT; ADX 25.00 -> EXIT; %B 1.00 -> EXIT; Z +2.5/-2.5 -> EXIT.
  Entry boundaries are strict: ADX 25.00 -> FAIL; RSI 40.00 -> FAIL;
  %B 0.01 -> FAIL; %B 0.00 -> PASS.

Priority: GLOBAL RISK > AUTO-EXIT > ENTRY.  Under these thresholds an entry
and an exit can never be simultaneously true (ADX<25 vs ADX>=25, RSI<40 vs
RSI>=70, %B<=0 vs %B>=1 are mutually exclusive), so exit priority is
structural; the caller additionally evaluates exit before entry.

All decisions use CLOSED candles only (the caller drops the forming candle).
Any required indicator that is missing/NaN yields INSUFFICIENT_DATA: no
signal is ever generated from incomplete indicator state.

Cooldown: 3-hour symbol-specific cooldown after auto-exit (caller-provided).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from market_features import MarketFeatures


class EntrySignal(str, Enum):
    """Auto-entry signal result."""
    ALLOWED = "ENTRY_ALLOWED"
    BLOCKED = "ENTRY_BLOCKED"
    COOLDOWN = "ENTRY_COOLDOWN"
    INSUFFICIENT_DATA = "ENTRY_INSUFFICIENT_DATA"


class ExitSignal(str, Enum):
    """Auto-exit signal result."""
    HOLD = "EXIT_HOLD"
    LIQUIDATE = "EXIT_LIQUIDATE"
    INSUFFICIENT_DATA = "EXIT_INSUFFICIENT_DATA"


@dataclass(frozen=True)
class EntryDecision:
    """Structured entry decision with all reasons."""
    signal: EntrySignal
    allowed: bool
    reasons: tuple[str, ...]
    adx: Decimal
    rsi: Decimal
    percent_b: Decimal
    volume_oscillator: Decimal
    adx_threshold: Decimal
    rsi_threshold: Decimal
    bb_threshold: Decimal
    volume_oscillator_threshold: Decimal


@dataclass(frozen=True)
class ExitDecision:
    """Structured exit decision with all triggered reasons."""
    signal: ExitSignal
    should_exit: bool
    triggered_reasons: tuple[str, ...]
    adx: Decimal
    rsi: Decimal
    percent_b: Decimal
    z_score: Decimal
    adx_threshold: Decimal
    rsi_threshold: Decimal
    bb_threshold: Decimal
    zscore_threshold: Decimal


def _to_decimal(value: Any) -> Decimal:
    """Safely convert to Decimal."""
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def calculate_percent_b(close: Decimal, bb_upper: Decimal, bb_lower: Decimal, bb_middle: Decimal) -> Decimal:
    """Calculate %B (percent B) Bollinger Band position indicator.

    %B = (close - lower) / (upper - lower)
    - %B > 1: price above upper band
    - %B = 1: price at upper band
    - %B = 0.5: price at middle band
    - %B = 0: price at lower band
    - %B < 0: price below lower band

    Zero-width bands (upper == lower) are handled safely: the value falls
    back to the middle (0.5) — never a division by zero.
    """
    if bb_upper <= bb_lower:
        return Decimal("0.5")  # zero-width fallback
    return (close - bb_lower) / (bb_upper - bb_lower)


def evaluate_entry_signal(
    features: MarketFeatures,
    config: dict[str, Any],
    in_cooldown: bool = False,
) -> EntryDecision:
    """Evaluate auto-entry conditions (ALL mandatory, AND logic).

    ENTRY = ADX(14) < 25 AND RSI(14) < 40 AND Bollinger %B <= 0
    (thresholds configurable via strategy.entry.*; the Volume Oscillator is
    diagnostics only and never gates entry).

    Any missing/NaN indicator yields INSUFFICIENT_DATA (blocked).
    """
    strategy = config.get("strategy", {})
    entry_cfg = strategy.get("entry", {})

    adx_max = _to_decimal(entry_cfg.get("adx_max", "25"))
    rsi_max = _to_decimal(entry_cfg.get("rsi_max", "40"))
    bb_pct_b_max = _to_decimal(entry_cfg.get("bb_percent_b_max", "0"))
    # VO is diagnostics-only; reported but never gating.
    vol_osc_min = _to_decimal(entry_cfg.get("volume_oscillator_min", "0"))

    adx = features.adx
    rsi = features.rsi
    volume_oscillator = features.volume_oscillator
    percent_b = calculate_percent_b(
        features.close_price, features.bb_upper, features.bb_lower, features.bb_middle
    )

    def _finite(value: Decimal) -> bool:
        return value.is_finite()

    if not all(_finite(v) for v in (adx, rsi, percent_b)):
        return EntryDecision(
            signal=EntrySignal.INSUFFICIENT_DATA,
            allowed=False,
            reasons=("INSUFFICIENT_DATA: required entry indicator missing/NaN",),
            adx=adx,
            rsi=rsi,
            percent_b=percent_b,
            volume_oscillator=volume_oscillator,
            adx_threshold=adx_max,
            rsi_threshold=rsi_max,
            bb_threshold=bb_pct_b_max,
            volume_oscillator_threshold=vol_osc_min,
        )

    reasons = []

    # Cooldown blocks entry evaluation entirely.
    if in_cooldown:
        return EntryDecision(
            signal=EntrySignal.COOLDOWN,
            allowed=False,
            reasons=("Auto-entry cooldown active (3 hours after auto-exit)",),
            adx=adx,
            rsi=rsi,
            percent_b=percent_b,
            volume_oscillator=volume_oscillator,
            adx_threshold=adx_max,
            rsi_threshold=rsi_max,
            bb_threshold=bb_pct_b_max,
            volume_oscillator_threshold=vol_osc_min,
        )

    # Mandatory condition 1: ADX < threshold
    adx_ok = adx < adx_max
    if not adx_ok:
        reasons.append(f"ADX {adx} >= {adx_max} (not sideways)")

    # Mandatory condition 2: RSI < threshold
    rsi_ok = rsi < rsi_max
    if not rsi_ok:
        reasons.append(f"RSI {rsi} >= {rsi_max} (not oversold)")

    # Mandatory condition 3: %B <= threshold (at/below lower band)
    bb_ok = percent_b <= bb_pct_b_max
    if not bb_ok:
        reasons.append(f"%B {percent_b} > {bb_pct_b_max} (price not at/below lower Bollinger Band)")

    # NOTE: the Volume Oscillator is deliberately NOT evaluated here.
    # It is diagnostics only (locked specification).

    entry_conditions_met = adx_ok and rsi_ok and bb_ok
    signal = EntrySignal.ALLOWED if entry_conditions_met else EntrySignal.BLOCKED

    return EntryDecision(
        signal=signal,
        allowed=entry_conditions_met,
        reasons=tuple(reasons),
        adx=adx,
        rsi=rsi,
        percent_b=percent_b,
        volume_oscillator=volume_oscillator,
        adx_threshold=adx_max,
        rsi_threshold=rsi_max,
        bb_threshold=bb_pct_b_max,
        volume_oscillator_threshold=vol_osc_min,
    )


def evaluate_exit_signal(
    features: MarketFeatures,
    config: dict[str, Any],
) -> ExitDecision:
    """Evaluate auto-exit conditions (ANY triggers — OR logic).

    Exit fires when ANY of (at-or-beyond the configured threshold):
      - RSI(14) >= 70
      - ADX(14) >= 25
      - Bollinger %B >= 1
      - |Z-Score(20)| >= 2.5 (either extreme, same emergency procedure)

    Missing/NaN indicators yield INSUFFICIENT_DATA (never a silent HOLD and
    never an exit) so the caller can act fail-closed.
    """
    strategy = config.get("strategy", {})
    exit_cfg = strategy.get("exit", {})

    rsi_min = _to_decimal(exit_cfg.get("rsi_min", "70"))
    adx_min = _to_decimal(exit_cfg.get("adx_min", "25"))
    bb_pct_b_min = _to_decimal(exit_cfg.get("bb_percent_b_min", "1"))
    zscore_threshold = _to_decimal(exit_cfg.get("zscore_threshold", "2.5"))

    adx = features.adx
    rsi = features.rsi
    z_score = features.z_score
    percent_b = calculate_percent_b(
        features.close_price, features.bb_upper, features.bb_lower, features.bb_middle
    )

    def _finite(value: Decimal) -> bool:
        return value.is_finite()

    if not all(_finite(v) for v in (adx, rsi, z_score, percent_b)):
        return ExitDecision(
            signal=ExitSignal.INSUFFICIENT_DATA,
            should_exit=False,
            triggered_reasons=("INSUFFICIENT_DATA: exit indicators missing/NaN",),
            adx=adx,
            rsi=rsi,
            percent_b=percent_b,
            z_score=z_score,
            adx_threshold=adx_min,
            rsi_threshold=rsi_min,
            bb_threshold=bb_pct_b_min,
            zscore_threshold=zscore_threshold,
        )

    triggered = []

    # Condition 1: RSI >= 70 (overbought)
    if rsi >= rsi_min:
        triggered.append(f"RSI {rsi} >= {rsi_min} (overbought)")

    # Condition 2: ADX >= 25 (trend breakout)
    if adx >= adx_min:
        triggered.append(f"ADX {adx} >= {adx_min} (trend breakout)")

    # Condition 3: %B >= 1 (at/above upper Bollinger Band)
    if percent_b >= bb_pct_b_min:
        triggered.append(f"%B {percent_b} >= {bb_pct_b_min} (price at/above upper Bollinger Band)")

    # Condition 4: |Z-Score| >= 2.5 (emergency exit, either extreme)
    if z_score >= zscore_threshold:
        triggered.append(f"Z-Score {z_score} >= {zscore_threshold} (extreme positive deviation)")
    elif z_score <= -zscore_threshold:
        triggered.append(f"Z-Score {z_score} <= -{zscore_threshold} (extreme negative deviation)")

    should_exit = len(triggered) > 0
    signal = ExitSignal.LIQUIDATE if should_exit else ExitSignal.HOLD

    return ExitDecision(
        signal=signal,
        should_exit=should_exit,
        triggered_reasons=tuple(triggered),
        adx=adx,
        rsi=rsi,
        percent_b=percent_b,
        z_score=z_score,
        adx_threshold=adx_min,
        rsi_threshold=rsi_min,
        bb_threshold=bb_pct_b_min,
        zscore_threshold=zscore_threshold,
    )


def evaluate_strategy(
    features: MarketFeatures,
    config: dict[str, Any],
    has_active_grid: bool = False,
    in_cooldown: bool = False,
) -> tuple[EntryDecision | None, ExitDecision | None]:
    """Evaluate the strategy signals based on the current state.

    Priority: GLOBAL RISK (caller) > AUTO-EXIT > ENTRY.  Exit is evaluated
    whenever a grid is active and always takes priority over entry; without
    an active grid only entry is meaningful (nothing to exit).
    """
    entry_decision = None
    exit_decision = None

    if not has_active_grid:
        entry_decision = evaluate_entry_signal(features, config, in_cooldown)

    if has_active_grid:
        exit_decision = evaluate_exit_signal(features, config)

    return entry_decision, exit_decision
