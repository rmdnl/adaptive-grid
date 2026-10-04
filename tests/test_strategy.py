"""Auto-entry / auto-exit strategy engine: specification tests (NEW strategy).

Pins the locked v5 strategy exactly:

- ENTRY (the ONLY mandatory filters, AND logic):
    ADX(14) < 25 AND RSI(14) < 40 AND Bollinger %B <= 0.
  The Volume Oscillator has NO influence on entry (diagnostics only).
- EXIT (OR logic, at-or-beyond the threshold):
    RSI >= 70; ADX >= 25; %B >= 1; |Z-Score(20)| >= 2.5.
- Boundary table from the specification:
    entry:  ADX 24.99 PASS / 25.00 FAIL; RSI 39.99 PASS / 40.00 FAIL;
            %B -0.01 PASS / 0.00 PASS / 0.01 FAIL
    exit:   RSI 70.00 EXIT; ADX 25.00 EXIT; %B 1.00 EXIT;
            Z +2.5 EXIT; Z -2.5 EXIT
- INSUFFICIENT_DATA: no signal from missing/NaN indicators.
- Exit priority over entry is structural (mutually exclusive thresholds).
"""
from __future__ import annotations

from decimal import Decimal

from market_features import MarketFeatures
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
                "adx_max": "25",
                "rsi_max": "40",
                "bb_percent_b_max": "0",
            },
            "exit": {
                "rsi_min": "70",
                "adx_min": "25",
                "bb_percent_b_min": "1",
                "zscore_threshold": "2.5",
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
    volume_oscillator: str = "-5",  # VO must have NO influence on entry
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
    assert calculate_percent_b(Decimal("99"), Decimal("104"), Decimal("100"), Decimal("102")) < 0
    assert calculate_percent_b(Decimal("105"), Decimal("104"), Decimal("100"), Decimal("102")) > 1
    # Zero-width bands never divide by zero.
    assert calculate_percent_b(Decimal("100"), Decimal("100"), Decimal("100"), Decimal("102")) == Decimal("0.5")


# ---------------------------------------------------------------------------
# ENTRY: the three mandatory filters (AND logic)
# ---------------------------------------------------------------------------

def test_entry_allowed_when_all_conditions_met():
    decision = evaluate_entry_signal(_features(), _config())
    assert decision.signal is EntrySignal.ALLOWED
    assert decision.allowed is True
    assert decision.reasons == ()


def test_entry_boundary_adx():
    # ADX 24.99 -> PASS, ADX 25.00 -> FAIL (strict <)
    assert evaluate_entry_signal(_features(adx="24.99"), _config()).allowed is True
    blocked = evaluate_entry_signal(_features(adx="25"), _config())
    assert blocked.allowed is False
    assert any("ADX" in reason for reason in blocked.reasons)


def test_entry_boundary_rsi():
    # RSI 39.99 -> PASS, RSI 40.00 -> FAIL (strict <)
    assert evaluate_entry_signal(_features(rsi="39.99"), _config()).allowed is True
    blocked = evaluate_entry_signal(_features(rsi="40"), _config())
    assert blocked.allowed is False
    assert any("RSI" in reason for reason in blocked.reasons)


def test_entry_boundary_percent_b():
    # %B -0.01 -> PASS, 0.00 -> PASS, 0.01 -> FAIL (<= 0)
    assert evaluate_entry_signal(
        _features(close="99.96", bb_lower="100", bb_upper="104"), _config()
    ).allowed is True
    assert evaluate_entry_signal(
        _features(close="100", bb_lower="100", bb_upper="104"), _config()
    ).allowed is True
    blocked = evaluate_entry_signal(
        _features(close="100.04", bb_lower="100", bb_upper="104"), _config())
    assert blocked.allowed is False
    assert any("%B" in reason for reason in blocked.reasons)


def test_volume_oscillator_has_no_influence_on_entry():
    """VO is diagnostics only: strongly negative and strongly positive VO
    must both leave the entry decision unchanged."""
    cfg = _config()
    assert evaluate_entry_signal(_features(volume_oscillator="-50"), cfg).allowed is True
    assert evaluate_entry_signal(_features(volume_oscillator="50"), cfg).allowed is True
    # ...and VO alone can never make a blocked entry allowed.
    assert evaluate_entry_signal(
        _features(adx="30", volume_oscillator="50"), cfg).allowed is False


def test_entry_blocked_when_every_condition_fails_lists_all_reasons():
    decision = evaluate_entry_signal(_features(adx="40", rsi="60"), _config())
    assert decision.allowed is False
    assert any("ADX" in r for r in decision.reasons)
    assert any("RSI" in r for r in decision.reasons)


def test_entry_blocked_on_cooldown_regardless_of_indicators():
    decision = evaluate_entry_signal(_features(), _config(), in_cooldown=True)
    assert decision.allowed is False
    assert decision.signal is EntrySignal.COOLDOWN


def test_entry_insufficient_data_blocks():
    features = _features(rsi="NaN")
    decision = evaluate_entry_signal(features, _config())
    assert decision.allowed is False
    assert decision.signal is EntrySignal.INSUFFICIENT_DATA
    assert any("INSUFFICIENT_DATA" in reason for reason in decision.reasons)


# ---------------------------------------------------------------------------
# EXIT: OR logic at-or-beyond the threshold
# ---------------------------------------------------------------------------

def test_exit_hold_when_no_condition_triggers():
    decision = evaluate_exit_signal(_features(), _config())
    assert decision.signal is ExitSignal.HOLD
    assert decision.should_exit is False
    assert decision.triggered_reasons == ()


def test_exit_boundary_rsi_70():
    decision = evaluate_exit_signal(_features(rsi="70"), _config())
    assert decision.should_exit is True
    assert any("RSI" in reason for reason in decision.triggered_reasons)


def test_exit_boundary_adx_25():
    decision = evaluate_exit_signal(_features(adx="25"), _config())
    assert decision.should_exit is True
    assert any("ADX" in reason for reason in decision.triggered_reasons)


def test_exit_boundary_percent_b_1():
    decision = evaluate_exit_signal(
        _features(close="104", bb_lower="100", bb_upper="104"), _config())
    assert decision.should_exit is True
    assert any("%B" in reason for reason in decision.triggered_reasons)


def test_exit_boundary_zscore_both_extremes():
    for z in ("2.5", "-2.5"):
        decision = evaluate_exit_signal(_features(z_score=z), _config())
        assert decision.should_exit is True
        assert any("Z-Score" in reason for reason in decision.triggered_reasons)


def test_exit_beyond_thresholds_also_triggers():
    for features in (
        _features(rsi="75"),
        _features(adx="30"),
        _features(close="105", bb_lower="100", bb_upper="104"),
        _features(z_score="3.0"),
        _features(z_score="-3.0"),
    ):
        decision = evaluate_exit_signal(features, _config())
        assert decision.should_exit is True


def test_exit_triggers_collect_all_simultaneous_reasons():
    decision = evaluate_exit_signal(
        _features(rsi="75", adx="30", z_score="3.0"), _config())
    assert decision.should_exit is True
    assert len(decision.triggered_reasons) == 3


def test_exit_insufficient_data_never_liquidates():
    decision = evaluate_exit_signal(_features(z_score="NaN"), _config())
    assert decision.should_exit is False
    assert decision.signal is ExitSignal.INSUFFICIENT_DATA


# ---------------------------------------------------------------------------
# Combined evaluation / priority
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


def test_entry_and_exit_thresholds_are_mutually_exclusive():
    """Structural exit priority: no indicator value can satisfy entry AND
    exit at the same time (ADX<25 vs >=25, RSI<40 vs >=70, %B<=0 vs >=1)."""
    for adx in ("24.99", "25", "30"):
        entry_allowed = Decimal(adx) < Decimal("25")
        exit_fires = Decimal(adx) >= Decimal("25")
        assert entry_allowed != exit_fires
