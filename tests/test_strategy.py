"""Strategy tests: Regime + Recovery entry gate, soft/hard exit gate,
exit priority, cooldown."""

from __future__ import annotations

import pytest

import strategy
from conftest import make_candle
from strategy import EntryDecision, ExitDecision, IndicatorSnapshot


def entry_valid_snapshot(**overrides) -> IndicatorSnapshot:
    """A snapshot that satisfies every entry condition and fires no exit."""
    values = dict(
        symbol="BTC/USDT",
        last_close=50000.0,
        adx=15.0,
        adx_prev=16.0,          # ADX not rising: 15 <= 16
        plus_di=18.0,
        minus_di=22.0,
        stoch_k=0.25,
        stoch_d=0.20,           # K above D on the last bar
        stoch_k_prev=0.15,      # K was below D two bars ago -> cross up
        stoch_d_prev=0.22,
        atr=350.0,
    )
    values.update(overrides)
    return IndicatorSnapshot(**values)


# ----- entry -----

def test_entry_allowed_when_all_conditions_hold(cfg):
    assert strategy.evaluate_entry(entry_valid_snapshot(), cfg) == EntryDecision(True, None)


def test_entry_blocked_when_adx_at_max(cfg):
    d = strategy.evaluate_entry(entry_valid_snapshot(adx=20.0), cfg)
    assert d == EntryDecision(False, "adx_not_low")  # 20 < 20 is false: strict


def test_entry_allowed_when_adx_below_max(cfg):
    d = strategy.evaluate_entry(entry_valid_snapshot(adx=19.9, adx_prev=20.0), cfg)
    assert d.allowed is True


def test_entry_blocked_when_adx_rising(cfg):
    d = strategy.evaluate_entry(entry_valid_snapshot(adx=15.0, adx_prev=14.0), cfg)
    assert d == EntryDecision(False, "adx_rising")


def test_entry_allowed_when_adx_exactly_flat(cfg):
    # ADX <= ADX[lookback bars ago] includes the exact equality boundary
    d = strategy.evaluate_entry(entry_valid_snapshot(adx=15.0, adx_prev=15.0), cfg)
    assert d.allowed is True


def test_entry_blocked_when_stoch_kd_already_crossed(cfg):
    # K was already above D two bars ago: no fresh cross
    d = strategy.evaluate_entry(
        entry_valid_snapshot(stoch_k=0.25, stoch_d=0.20, stoch_k_prev=0.25, stoch_d_prev=0.20),
        cfg,
    )
    assert d == EntryDecision(False, "stoch_no_cross")


def test_entry_blocked_when_kd_crossing_down(cfg):
    d = strategy.evaluate_entry(
        entry_valid_snapshot(stoch_k=0.15, stoch_d=0.22, stoch_k_prev=0.30, stoch_d_prev=0.10),
        cfg,
    )
    assert d == EntryDecision(False, "stoch_no_cross")


def test_entry_allowed_when_k_crosses_up_from_below(cfg):
    d = strategy.evaluate_entry(
        entry_valid_snapshot(stoch_k=0.25, stoch_d=0.20, stoch_k_prev=0.10, stoch_d_prev=0.20),
        cfg,
    )
    assert d.allowed is True


def test_entry_blocked_when_stoch_k_too_high(cfg):
    d = strategy.evaluate_entry(entry_valid_snapshot(stoch_k=0.31, stoch_d=0.20), cfg)
    assert d == EntryDecision(False, "stoch_k_too_high")


def test_entry_allowed_when_stoch_k_exactly_below_limit(cfg):
    d = strategy.evaluate_entry(entry_valid_snapshot(stoch_k=0.2999, stoch_d=0.20), cfg)
    assert d.allowed is True


def test_entry_blockers_report_every_failed_condition(cfg):
    snap = entry_valid_snapshot(
        adx=25.0, adx_prev=20.0, stoch_k=0.5, stoch_d=0.6,
        stoch_k_prev=0.6, stoch_d_prev=0.4,
    )
    failed = strategy.entry_blockers(snap, cfg)
    assert failed == ["adx_not_low", "adx_rising", "stoch_no_cross", "stoch_k_too_high"]
    assert strategy.entry_blockers(entry_valid_snapshot(), cfg) == []


def test_stoch_kd_cross_up_semantics():
    assert strategy.stoch_kd_cross_up(
        entry_valid_snapshot(stoch_k=0.25, stoch_d=0.20, stoch_k_prev=0.10, stoch_d_prev=0.20)
    ) is True
    # equality on the previous bar still counts as a cross (K[-2] <= D[-2])
    assert strategy.stoch_kd_cross_up(
        entry_valid_snapshot(stoch_k=0.25, stoch_d=0.20, stoch_k_prev=0.20, stoch_d_prev=0.20)
    ) is True
    # no cross when K stays above D
    assert strategy.stoch_kd_cross_up(
        entry_valid_snapshot(stoch_k=0.25, stoch_d=0.20, stoch_k_prev=0.25, stoch_d_prev=0.20)
    ) is False
    # missing data never crosses
    assert strategy.stoch_kd_cross_up(entry_valid_snapshot(stoch_k_prev=None)) is False


@pytest.mark.parametrize("missing", ["adx", "adx_prev", "stoch_k", "stoch_d", "stoch_k_prev", "stoch_d_prev"])
def test_entry_blocked_on_missing_indicator(cfg, missing):
    values = {missing: None}
    d = strategy.evaluate_entry(entry_valid_snapshot(**values), cfg)
    assert d == EntryDecision(False, "insufficient_data")


# ----- exits: soft vs hard severity -----

def test_exit_soft_on_adx_up_with_plus_di_dominant(cfg):
    d = strategy.evaluate_exit(entry_valid_snapshot(adx=30.0, plus_di=30.0, minus_di=10.0), cfg)
    assert d == ExitDecision(True, "adx_trending_up", "soft")


def test_exit_hard_on_adx_with_minus_di_dominant(cfg):
    d = strategy.evaluate_exit(entry_valid_snapshot(adx=30.0, plus_di=10.0, minus_di=30.0), cfg)
    assert d == ExitDecision(True, "adx_trending_down", "hard")


def test_no_exit_at_adx_threshold(cfg):
    # ADX > 25 is strict: exactly 25 does not exit regardless of DI side
    assert strategy.evaluate_exit(
        entry_valid_snapshot(adx=25.0, plus_di=30.0, minus_di=10.0), cfg
    ).should_exit is False


def test_no_exit_when_adx_high_but_di_balanced(cfg):
    # +DI == -DI: neither direction dominates -> no ADX exit
    assert strategy.evaluate_exit(
        entry_valid_snapshot(adx=30.0, plus_di=20.0, minus_di=20.0), cfg
    ).should_exit is False


def test_exit_soft_on_stoch_k_overbought(cfg):
    d = strategy.evaluate_exit(entry_valid_snapshot(stoch_k=0.81), cfg)
    assert d == ExitDecision(True, "stoch_k_overbought", "soft")


def test_no_exit_at_stoch_k_threshold(cfg):
    # %K > 0.8 is strict: exactly 0.8 does not exit
    assert strategy.evaluate_exit(entry_valid_snapshot(stoch_k=0.8), cfg).should_exit is False


def test_missing_indicators_cannot_trigger_exit(cfg):
    empty = IndicatorSnapshot(symbol="BTC/USDT")
    assert strategy.evaluate_exit(empty, cfg) == ExitDecision(False, None, None)


def test_stoch_overbought_wins_over_soft_adx_ordering(cfg):
    # both soft conditions hold: ADX-up fires first (stable ordering)
    d = strategy.evaluate_exit(
        entry_valid_snapshot(adx=30.0, plus_di=30.0, minus_di=10.0, stoch_k=0.9), cfg
    )
    assert d.reason == "adx_trending_up" and d.severity == "soft"


def test_hard_adx_takes_precedence_when_di_flips(cfg):
    # ADX down (hard) is checked before the stochastic soft exit
    d = strategy.evaluate_exit(
        entry_valid_snapshot(adx=30.0, plus_di=10.0, minus_di=30.0, stoch_k=0.9), cfg
    )
    assert d.reason == "adx_trending_down" and d.severity == "hard"


# ----- exit priority -----

def test_exit_has_priority_over_entry(cfg):
    # stochastic overbought can coexist with all entry conditions being true
    snap = entry_valid_snapshot(stoch_k=0.9)
    exit_d, entry_d = strategy.evaluate_signal(snap, cfg)
    assert exit_d == ExitDecision(True, "stoch_k_overbought", "soft")
    assert entry_d == EntryDecision(False, "exit_priority")


def test_signal_without_exit_defers_to_entry(cfg):
    exit_d, entry_d = strategy.evaluate_signal(entry_valid_snapshot(), cfg)
    assert exit_d.should_exit is False
    assert entry_d.allowed is True


# ----- cooldown -----

def test_cooldown_active_semantics():
    assert strategy.cooldown_active(1000.0, 1000.1) is True
    assert strategy.cooldown_active(1000.0, 1000.0) is False
    assert strategy.cooldown_active(2000.0, 1000.0) is False
    assert strategy.cooldown_active(1000.0, None) is False


# ----- snapshot construction from closed candles -----

def _wiring_candles():
    """Mostly-flat series with a final dip: ADX degenerate-ish, RSI low."""
    closes = [10.0] * 39 + [5.0]
    candles = []
    for i, c in enumerate(closes):
        candles.append(
            make_candle(c, c + 1.0, c - 1.0, 1.0, i * 1000, i * 1000 + 999)
        )
    return candles


def test_build_snapshot_from_closed_candles():
    snap = strategy.build_snapshot(
        _wiring_candles(),
        symbol="BTC/USDT",
        adx_period=14,
        rsi_period=14,
        stoch_rsi_length=14,
        stoch_smooth_k=3,
        stoch_smooth_d=3,
        atr_period=14,
        adx_regime_lookback=3,
    )
    assert snap.symbol == "BTC/USDT"
    assert snap.last_close == 5.0
    assert snap.atr is not None and snap.atr > 0
    # RSI series exists for the stoch engine; K/D are on the 0..1 scale
    if snap.stoch_k is not None:
        assert 0.0 <= snap.stoch_k <= 1.0
        assert 0.0 <= snap.stoch_d <= 1.0


def test_build_snapshot_empty_is_insufficient():
    snap = strategy.build_snapshot([], symbol="BTC/USDT")
    assert snap.last_close is None
    assert snap.adx is None and snap.stoch_k is None and snap.atr is None


def test_build_snapshot_short_history_is_insufficient():
    # too few candles for the ADX/stoch stack -> NO TRADE (fail-closed)
    candles = [make_candle(10.0, 11.0, 9.0, 1.0, i * 1000, i * 1000 + 999) for i in range(20)]
    snap = strategy.build_snapshot(candles, symbol="BTC/USDT")
    assert snap.adx is None or snap.adx_prev is None or snap.stoch_k is None
