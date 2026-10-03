"""Auto-entry / auto-exit strategy engine: specification tests.

Pins the locked v4.0 strategy exactly as specified:

- Auto-entry (ALL must hold, AND logic): ADX(14) < 20; RSI(14) < 35
  OR %B <= 0 (alternative lower-Bollinger trigger); Volume
  Oscillator(5,10) > 0.
- Auto-exit (ANY triggers, OR logic): RSI(14) >= 70; ADX(14) > 25;
  %B > 1; |Z-Score(20)| > 2.5.
- Cooldown blocks entry evaluation but never exit evaluation.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from market_features import MarketFeatures
from market_regime import MarketRegime
from strategy import (
    EntrySignal,
    ExitSignal,
    calculate_percent_b,
    evaluate_entry_signal,
    evaluate_exit_signal,
    evaluate_strategy,
)


def _config() -> dict:
    return {
        "strategy": {
            "entry": {
                "adx_max": 20,
                "rsi_max": 35,
                "bb_percent_b_max": 0.0,
                "volume_oscillator_min": 0.0,
            },
            "exit": {
                "rsi_min": 70,
                "adx_min": 25,
                "bb_percent_b_min": 1.0,
                "zscore_threshold": 2.5,
            },
            "cooldown_hours": 3,
        },
    }


def _features(
    *,
    adx: str = "15",
    rsi: str = "30",
    close: str = "100",
    bb_lower: str = "100",
    bb_upper: str = "104",
    bb_middle: str = "102",
    volume_oscillator: str = "1.5",
    z_score: str = "0",
) -> MarketFeatures:
    return MarketFeatures(
        symbol="BTCUSDT",
        close_price=Decimal(close),
        atr=Decimal("1"),
        atr_pct=Decimal("0.01"),
        adx=Decimal(adx),
        plus_di=Decimal("20"),
        minus_di=Decimal("20"),
        bb_middle=Decimal(bb_middle),
        bb_upper=Decimal(bb_upper),
        bb_lower=Decimal(bb_lower),
        bb_width=Decimal("4"),
        bb_width_pct=Decimal("0.04"),
        current_volume=Decimal("100"),
        baseline_volume=Decimal("100"),
        volume_spike_ratio=Decimal("1.0"),
        directional_efficiency=Decimal("0.2"),
        atr_expansion_ratio=Decimal("1.0"),
        range_containment_pct=Decimal("0.95"),
        penetration_count=0,
        spread=None,
        spread_pct=None,
        rsi=Decimal(rsi),
        volume_oscillator=Decimal(volume_oscillator),
        z_score=Decimal(z_score),
    )


# ---------------------------------------------------------------------------
# %B helper
# ---------------------------------------------------------------------------

def test_percent_b_values():
    assert calculate_percent_b(Decimal("100"), Decimal("104"), Decimal("100"), Decimal("102")) == Decimal("0")
    assert calculate_percent_b(Decimal("104"), Decimal("104"), Decimal("100"), Decimal("102")) == Decimal("1")
    assert calculate_percent_b(Decimal("102"), Decimal("104"), Decimal("100"), Decimal("102")) == Decimal("0.5")
    # Below the lower band is negative; above the upper band is > 1.
    assert calculate_percent_b(Decimal("99"), Decimal("104"), Decimal("100"), Decimal("102")) < 0
    assert calculate_percent_b(Decimal("105"), Decimal("104"), Decimal("100"), Decimal("102")) > 1
    # Degenerate band falls back to mid.
    assert calculate_percent_b(Decimal("100"), Decimal("100"), Decimal("100"), Decimal("102")) == Decimal("0.5")


# ---------------------------------------------------------------------------
# Auto-entry: AND logic with the %B alternative trigger
# ---------------------------------------------------------------------------

def test_entry_allowed_when_all_conditions_met():
    decision = evaluate_entry_signal(_features(), _config())
    assert decision.signal is EntrySignal.ALLOWED
    assert decision.allowed is True
    assert decision.reasons == ()


def test_entry_blocked_when_adx_too_high():
    decision = evaluate_entry_signal(_features(adx="20"), _config())
    assert decision.allowed is False
    assert decision.signal is EntrySignal.BLOCKED
    assert any("ADX" in reason for reason in decision.reasons)


def test_entry_blocked_when_rsi_and_percent_b_both_fail():
    # RSI 40 >= 35 and %B 0.5 > 0 -> no oversold trigger.
    decision = evaluate_entry_signal(
        _features(rsi="40", close="102", bb_lower="100", bb_upper="104"),
        _config())
    assert decision.allowed is False
    assert any("RSI" in reason for reason in decision.reasons)


def test_entry_allowed_via_percent_b_alternative_trigger():
    # RSI 40 (>= 35) but price AT the lower band (%B == 0 <= 0) -> allowed.
    decision = evaluate_entry_signal(
        _features(rsi="40", close="100", bb_lower="100", bb_upper="104"),
        _config())
    assert decision.allowed is True


def test_entry_blocked_when_volume_oscillator_not_positive():
    decision = evaluate_entry_signal(_features(volume_oscillator="0"), _config())
    assert decision.allowed is False
    assert any("Volume Oscillator" in reason for reason in decision.reasons)


def test_entry_blocked_on_cooldown_regardless_of_indicators():
    decision = evaluate_entry_signal(_features(), _config(), in_cooldown=True)
    assert decision.allowed is False
    assert decision.signal is EntrySignal.COOLDOWN


# ---------------------------------------------------------------------------
# Auto-exit: OR logic
# ---------------------------------------------------------------------------

def test_exit_hold_when_no_condition_triggers():
    decision = evaluate_exit_signal(_features(), _config())
    assert decision.signal is ExitSignal.HOLD
    assert decision.should_exit is False
    assert decision.triggered_reasons == ()


def test_exit_on_rsi_overbought():
    decision = evaluate_exit_signal(_features(rsi="70"), _config())
    assert decision.should_exit is True
    assert any("RSI" in reason for reason in decision.triggered_reasons)


def test_exit_on_adx_breakout():
    decision = evaluate_exit_signal(_features(adx="25.1"), _config())
    assert decision.should_exit is True
    assert any("ADX" in reason for reason in decision.triggered_reasons)


def test_exit_on_price_above_upper_band():
    decision = evaluate_exit_signal(
        _features(close="105", bb_lower="100", bb_upper="104"), _config())
    assert decision.should_exit is True
    assert any("%B" in reason for reason in decision.triggered_reasons)


def test_exit_on_extreme_zscore_both_directions():
    for z in ("2.6", "-2.6"):
        decision = evaluate_exit_signal(_features(z_score=z), _config())
        assert decision.should_exit is True
        assert any("Z-Score" in reason for reason in decision.triggered_reasons)


def test_exit_triggers_collect_all_simultaneous_reasons():
    decision = evaluate_exit_signal(
        _features(rsi="75", adx="30", z_score="3.0"), _config())
    assert decision.should_exit is True
    assert len(decision.triggered_reasons) == 3


# ---------------------------------------------------------------------------
# Combined evaluation
# ---------------------------------------------------------------------------

def test_evaluate_strategy_without_grid_evaluates_entry_only():
    entry, exit_d = evaluate_strategy(_features(), _config(),
                                      has_active_grid=False)
    assert entry is not None and entry.allowed is True
    assert exit_d is None


def test_evaluate_strategy_with_grid_evaluates_exit_only():
    entry, exit_d = evaluate_strategy(_features(), _config(),
                                      has_active_grid=True)
    assert entry is None
    assert exit_d is not None and exit_d.should_exit is False
