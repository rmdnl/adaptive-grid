"""Auto-Entry / Auto-Exit Strategy Engine for Binance Spot Grid Bot.

This module implements the deterministic entry and exit logic for the multi-symbol
adaptive grid bot. The strategy is purely rule-based with no ML or prediction.

Entry (ALL must be true - AND logic):
  1. ADX(14) < 20                 -> confirms sideways/ranging market
  2. RSI(14) < 35                 -> confirms oversold/cheap entry zone
  3. %B <= 0                      -> price at or below lower Bollinger Band (alternative trigger)
  4. Volume Oscillator(5,10) > 0  -> volume expanding (trend confirmation)

Exit (ANY triggers - OR logic):
  1. RSI(14) >= 70                -> overbought, take profit
  2. ADX(14) > 25                 -> strong trend breakout, avoid floating loss
  3. %B > 1                       -> price above upper Bollinger Band
  4. |Z-Score(20)| > 2.5          -> extreme deviation, emergency liquidation

Cooldown Timer:
  - 3-hour cooldown after auto-exit before new auto-entry allowed

All indicators use 14-period lookback on the configured timeframe (1h or 4h).
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


class ExitSignal(str, Enum):
    """Auto-exit signal result."""
    HOLD = "EXIT_HOLD"
    LIQUIDATE = "EXIT_LIQUIDATE"


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
    """
    if bb_upper <= bb_lower:
        return Decimal("0.5")  # fallback to middle
    return (close - bb_lower) / (bb_upper - bb_lower)


def evaluate_entry_signal(
    features: MarketFeatures,
    config: dict[str, Any],
    in_cooldown: bool = False,
) -> EntryDecision:
    """Evaluate auto-entry conditions (ALL must pass - AND logic).
    
    Entry triggers when:
      - ADX(14) < entry.adx_max (default 20)  -> sideways market
      - RSI(14) < entry.rsi_max (default 35)  -> oversold
      - %B <= entry.bb_percent_b_max (default 0) -> at/below lower BB
      - Volume Oscillator(5,10) > entry.volume_oscillator_min (default 0) -> volume expanding
    
    The third condition (%B) is an ALTERNATIVE trigger: if price touches
    lower Bollinger Band, entry is allowed even if RSI is not < 35.
    
    Returns EntryDecision with signal and detailed reasons.
    """
    strategy = config.get("strategy", {})
    entry_cfg = strategy.get("entry", {})
    
    adx_max = _to_decimal(entry_cfg.get("adx_max", "20"))
    rsi_max = _to_decimal(entry_cfg.get("rsi_max", "35"))
    bb_pct_b_max = _to_decimal(entry_cfg.get("bb_percent_b_max", "0"))
    vol_osc_min = _to_decimal(entry_cfg.get("volume_oscillator_min", "0"))
    
    adx = features.adx
    rsi = features.rsi if hasattr(features, 'rsi') else Decimal("50")
    percent_b = calculate_percent_b(
        features.close_price, features.bb_upper, features.bb_lower, features.bb_middle
    )
    volume_oscillator = features.volume_oscillator if hasattr(features, 'volume_oscillator') else Decimal("0")
    
    reasons = []
    
    # Check cooldown first
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
    
    # Condition 1: ADX < threshold (sideways market)
    adx_ok = adx < adx_max
    if not adx_ok:
        reasons.append(f"ADX {adx} >= {adx_max} (not sideways)")
    
    # Condition 2: RSI < threshold (oversold) OR %B <= 0 (at lower BB)
    rsi_ok = rsi < rsi_max
    bb_ok = percent_b <= bb_pct_b_max
    
    # Alternative trigger: price at/below lower Bollinger Band
    rsi_or_bb_ok = rsi_ok or bb_ok
    
    if not rsi_ok and not bb_ok:
        reasons.append(f"RSI {rsi} >= {rsi_max} AND %B {percent_b:.4f} > {bb_pct_b_max} (not oversold nor at lower BB)")
    elif not rsi_ok:
        reasons.append(f"RSI {rsi} >= {rsi_max} (but %B {percent_b:.4f} <= {bb_pct_b_max} triggers entry)")
    
    # Condition 3: Volume Oscillator > 0 (volume expanding)
    vol_osc_ok = volume_oscillator > vol_osc_min
    if not vol_osc_ok:
        reasons.append(f"Volume Oscillator {volume_oscillator:.4f} <= {vol_osc_min} (volume not expanding)")
    
    entry_conditions_met = adx_ok and rsi_or_bb_ok and vol_osc_ok
    
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
    """Evaluate auto-exit conditions (ANY triggers - OR logic).
    
    Exit triggers when ANY of:
      - RSI(14) >= exit.rsi_min (default 70)  -> overbought
      - ADX(14) > exit.adx_min (default 25)   -> trend breakout
      - %B > exit.bb_percent_b_min (default 1) -> above upper BB
      - |Z-Score(20)| > exit.zscore_threshold (default 2.5) -> extreme deviation
    
    Returns ExitDecision with signal and all triggered reasons.
    """
    strategy = config.get("strategy", {})
    exit_cfg = strategy.get("exit", {})
    
    rsi_min = _to_decimal(exit_cfg.get("rsi_min", "70"))
    adx_min = _to_decimal(exit_cfg.get("adx_min", "25"))
    bb_pct_b_min = _to_decimal(exit_cfg.get("bb_percent_b_min", "1"))
    zscore_threshold = _to_decimal(exit_cfg.get("zscore_threshold", "2.5"))
    
    adx = features.adx
    rsi = features.rsi if hasattr(features, 'rsi') else Decimal("50")
    percent_b = calculate_percent_b(
        features.close_price, features.bb_upper, features.bb_lower, features.bb_middle
    )
    z_score = features.z_score if hasattr(features, 'z_score') else Decimal("0")
    
    triggered = []
    
    # Condition 1: RSI >= 70 (overbought)
    if rsi >= rsi_min:
        triggered.append(f"RSI {rsi} >= {rsi_min} (overbought)")
    
    # Condition 2: ADX > 25 (strong trend breakout)
    if adx > adx_min:
        triggered.append(f"ADX {adx} > {adx_min} (trend breakout)")
    
    # Condition 3: %B > 1 (above upper Bollinger Band)
    if percent_b > bb_pct_b_min:
        triggered.append(f"%B {percent_b:.4f} > {bb_pct_b_min} (above upper BB)")
    
    # Condition 4: |Z-Score| > 2.5 (extreme deviation - emergency exit)
    if z_score > zscore_threshold:
        triggered.append(f"Z-Score {z_score:.4f} > {zscore_threshold} (extreme positive deviation)")
    elif z_score < -zscore_threshold:
        triggered.append(f"Z-Score {z_score:.4f} < -{zscore_threshold} (extreme negative deviation)")
    
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
    """Evaluate both entry and exit signals based on current state.
    
    Args:
        features: Current market features
        config: Full bot configuration
        has_active_grid: Whether we currently have an active grid deployed
        in_cooldown: Whether we're in the post-exit cooldown period
    
    Returns:
        Tuple of (entry_decision, exit_decision)
        - entry_decision is None if grid already active
        - exit_decision is None if no active grid
    """
    entry_decision = None
    exit_decision = None
    
    if not has_active_grid:
        entry_decision = evaluate_entry_signal(features, config, in_cooldown)
    
    if has_active_grid:
        exit_decision = evaluate_exit_signal(features, config)
    
    return entry_decision, exit_decision