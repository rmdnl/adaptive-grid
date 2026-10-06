"""Strategy tests: strict entry gate, exit gate, exit priority, cooldown."""

from __future__ import annotations

import pytest

import indicators
import strategy
from conftest import make_candle
from strategy import EntryDecision, ExitDecision, IndicatorSnapshot


def entry_valid_snapshot(**overrides) -> IndicatorSnapshot:
    values = dict(
        symbol="BTC/USDT",
        last_close=50000.0,
        adx=15.0,
        rsi=30.0,
        percent_b=-0.1,
        volume_osc=0.2,
        zscore=0.5,
        atr=350.0,
    )
    values.update(overrides)
    return IndicatorSnapshot(**values)


# ----- entry -----

def test_entry_allowed_when_all_conditions_hold(cfg):
    assert strategy.evaluate_entry(entry_valid_snapshot(), cfg) == EntryDecision(True, None)


def test_entry_blocked_when_adx_not_below_max(cfg):
    d = strategy.evaluate_entry(entry_valid_snapshot(adx=20.0), cfg)
    assert d == EntryDecision(False, "adx_not_low")  # 20 < 20 is false: strict


def test_entry_allowed_when_rsi_below_max(cfg):
    # spec test 1: RSI below 40 passes when all other conditions pass
    d = strategy.evaluate_entry(entry_valid_snapshot(rsi=39.9), cfg)
    assert d == EntryDecision(True, None)


def test_entry_allowed_when_rsi_exactly_max(cfg):
    # spec test 2: RSI exactly 40 passes (inclusive boundary)
    d = strategy.evaluate_entry(entry_valid_snapshot(rsi=40.0), cfg)
    assert d == EntryDecision(True, None)


def test_entry_blocked_when_rsi_above_max(cfg):
    # spec test 3: RSI above 40 is blocked
    d = strategy.evaluate_entry(entry_valid_snapshot(rsi=40.1), cfg)
    assert d == EntryDecision(False, "rsi_not_low")


def test_entry_allowed_when_adx_below_max(cfg):
    # spec test 4: ADX below 20 passes
    d = strategy.evaluate_entry(entry_valid_snapshot(adx=19.9), cfg)
    assert d == EntryDecision(True, None)


def test_entry_allowed_when_volume_osc_exactly_zero(cfg):
    # spec test 6: VO exactly 0 passes (inclusive boundary)
    d = strategy.evaluate_entry(entry_valid_snapshot(volume_osc=0.0), cfg)
    assert d == EntryDecision(True, None)


def test_entry_blocked_when_volume_osc_negative(cfg):
    # spec test 7: VO below 0 is blocked
    d = strategy.evaluate_entry(entry_valid_snapshot(volume_osc=-0.001), cfg)
    assert d == EntryDecision(False, "volume_osc_not_positive")


def test_entry_allowed_when_percent_b_equals_zero(cfg):
    # spec test 8: BB %B exactly 0 passes
    d = strategy.evaluate_entry(entry_valid_snapshot(percent_b=0.0), cfg)
    assert d.allowed is True


def test_entry_allowed_when_percent_b_between_zero_and_max(cfg):
    # spec test 9: BB %B between 0 and 0.20 passes
    d = strategy.evaluate_entry(entry_valid_snapshot(percent_b=0.1), cfg)
    assert d.allowed is True


def test_entry_allowed_when_percent_b_exactly_max(cfg):
    # spec test 10: BB %B exactly 0.20 passes (inclusive boundary)
    d = strategy.evaluate_entry(entry_valid_snapshot(percent_b=0.20), cfg)
    assert d.allowed is True


def test_entry_blocked_when_percent_b_above_max(cfg):
    # spec test 11: BB %B above 0.20 is blocked
    d = strategy.evaluate_entry(entry_valid_snapshot(percent_b=0.21), cfg)
    assert d == EntryDecision(False, "percent_b_not_low")


def test_entry_blockers_report_every_failed_condition(cfg):
    """The telemetry helper reports ALL failed conditions, not just the first."""
    snap = entry_valid_snapshot(adx=25.0, rsi=50.0, volume_osc=-1.0, percent_b=0.5)
    failed = strategy.entry_blockers(snap, cfg)
    assert failed == [
        "adx_not_low",
        "rsi_not_low",
        "volume_osc_not_positive",
        "percent_b_not_low",
    ]
    # and reports none when all conditions hold
    assert strategy.entry_blockers(entry_valid_snapshot(), cfg) == []


@pytest.mark.parametrize("missing", ["adx", "rsi", "percent_b", "volume_osc"])
def test_entry_blocked_on_missing_indicator(cfg, missing):
    values = {missing: None}
    d = strategy.evaluate_entry(entry_valid_snapshot(**values), cfg)
    assert d == EntryDecision(False, "insufficient_data")


# ----- exit -----

def test_exit_on_rsi_overbought(cfg):
    assert strategy.evaluate_exit(entry_valid_snapshot(rsi=70.0), cfg) == ExitDecision(True, "rsi_overbought")


def test_no_exit_just_below_rsi_threshold(cfg):
    assert strategy.evaluate_exit(entry_valid_snapshot(rsi=69.9), cfg).should_exit is False


def test_exit_on_adx_trending(cfg):
    assert strategy.evaluate_exit(entry_valid_snapshot(adx=25.1), cfg) == ExitDecision(True, "adx_trending")


def test_no_exit_at_adx_threshold(cfg):
    # ADX > 25 is strict: exactly 25 does not exit
    assert strategy.evaluate_exit(entry_valid_snapshot(adx=25.0), cfg).should_exit is False


def test_exit_on_bb_upper_break(cfg):
    assert strategy.evaluate_exit(entry_valid_snapshot(percent_b=1.001), cfg) == ExitDecision(True, "bb_upper_break")


def test_no_exit_at_percent_b_one(cfg):
    assert strategy.evaluate_exit(entry_valid_snapshot(percent_b=1.0), cfg).should_exit is False


def test_exit_on_extreme_zscore_negative(cfg):
    assert strategy.evaluate_exit(entry_valid_snapshot(zscore=-2.6), cfg) == ExitDecision(True, "zscore_extreme")


def test_exit_on_extreme_zscore_positive(cfg):
    assert strategy.evaluate_exit(entry_valid_snapshot(zscore=2.6), cfg) == ExitDecision(True, "zscore_extreme")


def test_no_exit_at_zscore_threshold(cfg):
    assert strategy.evaluate_exit(entry_valid_snapshot(zscore=2.5), cfg).should_exit is False
    assert strategy.evaluate_exit(entry_valid_snapshot(zscore=-2.5), cfg).should_exit is False


def test_missing_indicators_cannot_trigger_exit(cfg):
    # fail-closed: an exit is never triggered by missing data
    empty = IndicatorSnapshot(symbol="BTC/USDT")
    assert strategy.evaluate_exit(empty, cfg) == ExitDecision(False, None)


# ----- exit priority -----

def test_exit_has_priority_over_entry(cfg):
    # z-score exit can coexist with all entry conditions being true
    snap = entry_valid_snapshot(zscore=3.0)
    exit_d, entry_d = strategy.evaluate_signal(snap, cfg)
    assert exit_d == ExitDecision(True, "zscore_extreme")
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
    closes = [10.0] * 19 + [5.0]
    volumes = [10.0] * 15 + [30.0, 40.0, 50.0, 60.0, 70.0]
    candles = []
    for i, c in enumerate(closes):
        candles.append(
            make_candle(c, c + 1.0, c - 1.0, volumes[i], i * 1000, i * 1000 + 999)
        )
    return candles


def test_build_snapshot_from_closed_candles():
    snap = strategy.build_snapshot(
        _wiring_candles(),
        symbol="BTC/USDT",
        adx_period=14,
        rsi_period=14,
        bb_period=20,
        bb_std=2.0,
        vo_fast=5,
        vo_slow=10,
        zscore_period=20,
        atr_period=14,
    )
    assert snap.symbol == "BTC/USDT"
    assert snap.last_close == 5.0
    assert snap.rsi == 0.0                      # only losses recently
    assert snap.percent_b == pytest.approx(-0.5897247, abs=1e-6)
    assert snap.volume_osc == pytest.approx(50.0 / 30.0 - 1.0)
    assert snap.zscore == pytest.approx(-4.3588989, abs=1e-6)
    assert snap.atr is not None and snap.atr > 0
    # degenerate mostly-flat series -> ADX undefined -> NO TRADE (fail-closed)
    assert snap.adx is None


def test_build_snapshot_empty_is_insufficient():
    snap = strategy.build_snapshot([], symbol="BTC/USDT")
    assert snap.last_close is None
    assert snap.adx is None and snap.rsi is None and snap.atr is None
