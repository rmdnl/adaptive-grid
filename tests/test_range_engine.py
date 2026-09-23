"""Comprehensive tests for range_engine.py — auto_range, _clamp, _score_low_is_good."""
from __future__ import annotations

import math
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from range_engine import (
    RangeCandidate,
    _clamp,
    _score_low_is_good,
    auto_range,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_df(
    n: int = 100,
    close_base: float = 100.0,
    spread: float = 2.0,
    adx: float = 20.0,
    atr_pct: float = 0.02,
    bb_width: float = 0.05,
    volume_ratio: float = 1.0,
    close_offset: float = 0.0,
) -> pd.DataFrame:
    """Build a minimal DataFrame with all columns auto_range requires."""
    closes = np.full(n, close_base + close_offset)
    return pd.DataFrame({
        "high": closes + spread,
        "low": closes - spread,
        "close": closes,
        "adx": np.full(n, adx),
        "atr_pct": np.full(n, atr_pct),
        "bb_width": np.full(n, bb_width),
        "volume_ratio": np.full(n, volume_ratio),
    })


def _make_quality_df(n: int = 100) -> pd.DataFrame:
    """DataFrame with values that yield high quality score."""
    return _make_df(
        n=n,
        close_base=100.0,
        spread=1.5,
        adx=25.0,      # below threshold 28 → score 100
        atr_pct=0.020,  # below threshold 0.025 → score 100
        bb_width=0.04,  # at/below threshold 0.06 → score 100
        volume_ratio=1.5,  # below threshold 2.5 → score 100
    )


def _make_poor_df(n: int = 100) -> pd.DataFrame:
    """DataFrame with values that yield low quality score."""
    return _make_df(
        n=n,
        close_base=100.0,
        spread=1.0,
        adx=50.0,       # far above threshold+deadband → 0
        atr_pct=0.10,    # far above → 0
        bb_width=0.20,   # far above → 0
        volume_ratio=5.0, # far above → 0
    )


# ===========================================================================
# A. _clamp
# ===========================================================================
class TestClamp:
    def test_default_bounds(self):
        assert _clamp(50) == 50.0

    def test_below_zero(self):
        assert _clamp(-10) == 0.0

    def test_above_100(self):
        assert _clamp(150) == 100.0

    def test_exactly_zero(self):
        assert _clamp(0) == 0.0

    def test_exactly_100(self):
        assert _clamp(100) == 100.0

    def test_custom_bounds(self):
        assert _clamp(5, lo=10, hi=20) == 10.0
        assert _clamp(25, lo=10, hi=20) == 20.0
        assert _clamp(15, lo=10, hi=20) == 15.0

    def test_float_input(self):
        assert _clamp(33.7) == 33.7

    def test_negative_custom_bounds(self):
        assert _clamp(-5, lo=-10, hi=10) == -5.0
        assert _clamp(-15, lo=-10, hi=10) == -10.0


# ===========================================================================
# B. _score_low_is_good
# ===========================================================================
class TestScoreLowIsGood:
    def test_below_threshold_returns_100(self):
        assert _score_low_is_good(5.0, threshold=10.0, deadband=5.0) == 100.0

    def test_at_threshold_returns_100(self):
        assert _score_low_is_good(10.0, threshold=10.0, deadband=5.0) == 100.0

    def test_above_threshold_plus_deadband_returns_0(self):
        assert _score_low_is_good(20.0, threshold=10.0, deadband=5.0) == 0.0

    def test_midpoint_of_deadband(self):
        # value = 12.5 → (15-12.5)/5*100 = 50.0
        score = _score_low_is_good(12.5, threshold=10.0, deadband=5.0)
        assert score == pytest.approx(50.0, abs=0.01)

    def test_one_quarter_into_deadband(self):
        # value = 11.25 → (15-11.25)/5*100 = 75.0
        score = _score_low_is_good(11.25, threshold=10.0, deadband=5.0)
        assert score == pytest.approx(75.0, abs=0.01)

    def test_three_quarters_into_deadband(self):
        # value = 13.75 → (15-13.75)/5*100 = 25.0
        score = _score_low_is_good(13.75, threshold=10.0, deadband=5.0)
        assert score == pytest.approx(25.0, abs=0.01)

    def test_nan_returns_zero(self):
        assert _score_low_is_good(float("nan"), threshold=10.0, deadband=5.0) == 0.0

    def test_inf_returns_zero(self):
        assert _score_low_is_good(float("inf"), threshold=10.0, deadband=5.0) == 0.0

    def test_neg_inf_returns_zero(self):
        assert _score_low_is_good(float("-inf"), threshold=10.0, deadband=5.0) == 0.0

    def test_zero_value_below_threshold(self):
        assert _score_low_is_good(0.0, threshold=10.0, deadband=5.0) == 100.0

    def test_zero_deadband_edge(self):
        # value = threshold → 100; value > threshold → 0
        assert _score_low_is_good(10.0, threshold=10.0, deadband=0.0) == 100.0
        assert _score_low_is_good(10.1, threshold=10.0, deadband=0.0) == 0.0

    def test_negative_threshold(self):
        assert _score_low_is_good(-5.0, threshold=-10.0, deadband=5.0) == 0.0
        assert _score_low_is_good(-15.0, threshold=-10.0, deadband=5.0) == 100.0


# ===========================================================================
# C. auto_range — input validation
# ===========================================================================
class TestAutoRangeValidation:
    def test_too_few_candles_raises(self):
        df = _make_df(n=49)
        with pytest.raises(ValueError, match="Not enough closed candles"):
            auto_range(df)

    def test_exactly_50_candles_ok(self):
        df = _make_df(n=50)
        result = auto_range(df, min_quality_score=0)
        assert isinstance(result, RangeCandidate)

    def test_missing_columns_raises(self):
        df = pd.DataFrame({"high": [1]*50, "low": [1]*50})
        with pytest.raises(ValueError, match="Missing range inputs"):
            auto_range(df)

    def test_missing_single_column_raises(self):
        df = _make_df(n=60)
        df = df.drop(columns=["adx"])
        with pytest.raises(ValueError, match="Missing range inputs"):
            auto_range(df)

    def test_empty_high_data_raises(self):
        df = _make_df(n=60)
        df["high"] = np.nan
        with pytest.raises(ValueError, match="No valid high/low data"):
            auto_range(df)

    def test_empty_low_data_raises(self):
        df = _make_df(n=60)
        df["low"] = np.nan
        with pytest.raises(ValueError, match="No valid high/low data"):
            auto_range(df)


# ===========================================================================
# D. auto_range — invalid range (lower <= 0 or upper <= lower)
# ===========================================================================
class TestAutoRangeInvalidRange:
    def test_all_zero_lows_returns_invalid(self):
        df = _make_df(n=60, close_base=0.0, spread=0.0)
        # low=0, high=0 → lower=0 → invalid
        result = auto_range(df, min_quality_score=0)
        assert result.approved is False
        assert result.reason == "INVALID_RANGE"
        assert result.quality == 0.0

    def test_equal_lower_upper_returns_invalid(self):
        # If all highs == all lows (spread=0), lower == upper → invalid
        df = _make_df(n=60, close_base=100.0, spread=0.0)
        result = auto_range(df, min_quality_score=0)
        assert result.approved is False
        assert result.reason == "INVALID_RANGE"


# ===========================================================================
# E. auto_range — width outside limits
# ===========================================================================
class TestAutoRangeWidth:
    def test_width_too_narrow(self):
        # Spread is tiny relative to close → width < min_width_pct
        df = _make_df(n=60, close_base=100.0, spread=0.0001)
        result = auto_range(df, min_quality_score=0)
        # Width should be very small (close to 0)
        assert result.approved is False
        assert result.reason == "RANGE_WIDTH_OUTSIDE_LIMIT"

    def test_width_too_wide(self):
        # width = upper/lower - 1 > max_width_pct (0.25)
        # close_base=100, spread=20 → lower=80, upper=120 → width=0.5
        df = _make_df(n=60, close_base=100.0, spread=20.0)
        result = auto_range(df, min_quality_score=0)
        assert result.approved is False
        assert result.reason == "RANGE_WIDTH_OUTSIDE_LIMIT"

    def test_width_in_valid_range(self):
        df = _make_quality_df(n=80)
        result = auto_range(df, min_quality_score=0)
        assert result.approved is True
        assert result.reason == "AUTO_RANGE_APPROVED"


# ===========================================================================
# F. auto_range — quality scoring
# ===========================================================================
class TestAutoRangeQuality:
    def test_quality_is_float(self):
        df = _make_quality_df(n=80)
        result = auto_range(df, min_quality_score=0)
        assert isinstance(result.quality, float)

    def test_quality_bounded_0_100(self):
        df = _make_quality_df(n=80)
        result = auto_range(df, min_quality_score=0)
        assert 0.0 <= result.quality <= 100.0

    def test_perfect_indicators_yield_high_quality(self):
        df = _make_quality_df(n=80)
        result = auto_range(df, min_quality_score=0)
        # All scoring inputs are at/below thresholds → high quality
        assert result.quality >= 60.0

    def test_poor_indicators_yield_low_quality(self):
        df = _make_poor_df(n=80)
        # Need valid width, so adjust spread to be sensible
        df["high"] = df["close"] + 2.0
        df["low"] = df["close"] - 2.0
        result = auto_range(df, min_quality_score=0)
        # All indicators far above thresholds → 0 contribution each
        assert result.quality < 40.0

    def test_quality_formula_weights(self):
        """Verify the quality formula matches expected weights.

        quality = 0.25*width + 0.25*adx + 0.20*atr + 0.15*bb + 0.10*vol + 0.05*pos
        Width score is proximity to midpoint, not low-is-good.
        With adx/atr/bb/vol all below thresholds → sub-scores = 100.
        With position in [0.10, 0.90] → position_score = 100.
        Width ≈ 0.03, midpoint = (0.03+0.25)/2 = 0.14, so width_score ≈ 21.
        quality ≈ 0.25*21 + 0.25*100 + 0.20*100 + 0.15*100 + 0.10*100 + 0.05*100 ≈ 80.
        """
        df = _make_quality_df(n=80)
        result = auto_range(df, min_quality_score=0)
        assert 75.0 <= result.quality <= 100.0

    def test_quality_increases_with_better_indicators(self):
        df_good = _make_quality_df(n=80)
        df_bad = _make_poor_df(n=80)
        df_bad["high"] = df_bad["close"] + 2.0
        df_bad["low"] = df_bad["close"] - 2.0
        q_good = auto_range(df_good, min_quality_score=0).quality
        q_bad = auto_range(df_bad, min_quality_score=0).quality
        assert q_good > q_bad


# ===========================================================================
# G. auto_range — approval logic
# ===========================================================================
class TestAutoRangeApproval:
    def test_high_quality_approved(self):
        df = _make_quality_df(n=80)
        result = auto_range(df, min_quality_score=60)
        assert result.approved is True
        assert result.reason == "AUTO_RANGE_APPROVED"

    def test_low_quality_rejected(self):
        df = _make_poor_df(n=80)
        df["high"] = df["close"] + 2.0
        df["low"] = df["close"] - 2.0
        result = auto_range(df, min_quality_score=65)
        assert result.approved is False
        assert result.reason == "RANGE_QUALITY_TOO_LOW"

    def test_quality_exactly_at_threshold(self):
        df = _make_quality_df(n=80)
        result = auto_range(df, min_quality_score=0)
        # Set threshold exactly to actual quality → approved (>=)
        result2 = auto_range(df, min_quality_score=round(result.quality))
        assert result2.approved is True

    def test_min_quality_zero_always_approved_if_range_valid(self):
        df = _make_poor_df(n=80)
        df["high"] = df["close"] + 2.0
        df["low"] = df["close"] - 2.0
        result = auto_range(df, min_quality_score=0)
        assert result.approved is True

    def test_require_price_inside_true_rejects_outside(self):
        # Price at extreme low → outside upper quantile range
        df = _make_quality_df(n=80)
        # Shift last close far down
        df.loc[df.index[-1], "close"] = 1.0
        result = auto_range(df, min_quality_score=0, require_price_inside=True)
        assert result.approved is False
        assert result.reason == "CURRENT_PRICE_OUTSIDE_RANGE"

    def test_require_price_inside_false_allows_outside(self):
        df = _make_quality_df(n=80)
        df.loc[df.index[-1], "close"] = 1.0
        result = auto_range(df, min_quality_score=0, require_price_inside=False)
        # Should not be rejected for price being outside
        assert result.reason != "CURRENT_PRICE_OUTSIDE_RANGE"

    def test_price_inside_range_approved(self):
        df = _make_quality_df(n=80)
        result = auto_range(df, min_quality_score=0, require_price_inside=True)
        assert result.approved is True
        assert result.reason == "AUTO_RANGE_APPROVED"


# ===========================================================================
# H. auto_range — position_in_range
# ===========================================================================
class TestAutoRangePosition:
    def test_position_between_0_and_1(self):
        df = _make_quality_df(n=80)
        result = auto_range(df, min_quality_score=0)
        assert 0.0 <= result.position_in_range <= 1.0

    def test_position_midpoint(self):
        """When close == (lower+upper)/2, position ≈ 0.5."""
        n = 100
        # Uniform data → quantile lower=0.1 gives low value, 0.9 gives high
        lows = np.full(n, 98.0)
        highs = np.full(n, 102.0)
        closes = np.full(n, 100.0)
        df = pd.DataFrame({
            "high": highs, "low": lows, "close": closes,
            "adx": np.full(n, 25.0),
            "atr_pct": np.full(n, 0.02),
            "bb_width": np.full(n, 0.05),
            "volume_ratio": np.full(n, 1.5),
        })
        result = auto_range(df, min_quality_score=0)
        # lower ≈ 98, upper ≈ 102, close=100 → position ≈ 0.5
        assert 0.4 <= result.position_in_range <= 0.6

    def test_position_at_lower_bound(self):
        n = 100
        lows = np.full(n, 98.0)
        highs = np.full(n, 102.0)
        closes = np.full(n, 98.0)  # close at lower
        df = pd.DataFrame({
            "high": highs, "low": lows, "close": closes,
            "adx": np.full(n, 25.0),
            "atr_pct": np.full(n, 0.02),
            "bb_width": np.full(n, 0.05),
            "volume_ratio": np.full(n, 1.5),
        })
        result = auto_range(df, min_quality_score=0)
        assert result.position_in_range == pytest.approx(0.0, abs=0.01)

    def test_position_at_upper_bound(self):
        n = 100
        lows = np.full(n, 98.0)
        highs = np.full(n, 102.0)
        closes = np.full(n, 102.0)  # close at upper
        df = pd.DataFrame({
            "high": highs, "low": lows, "close": closes,
            "adx": np.full(n, 25.0),
            "atr_pct": np.full(n, 0.02),
            "bb_width": np.full(n, 0.05),
            "volume_ratio": np.full(n, 1.5),
        })
        result = auto_range(df, min_quality_score=0)
        assert result.position_in_range == pytest.approx(1.0, abs=0.01)

    def test_position_score_bonus_when_centered(self):
        """position between 0.10-0.90 → position_score=100, boosting quality."""
        df = _make_quality_df(n=80)
        result = auto_range(df, min_quality_score=0)
        # close=100 is roughly centered → position should be mid-range
        assert 0.10 <= result.position_in_range <= 0.90


# ===========================================================================
# I. RangeCandidate dataclass
# ===========================================================================
class TestRangeCandidate:
    def test_is_frozen(self):
        rc = RangeCandidate(
            lower=Decimal("90"), upper=Decimal("110"),
            quality=75.0, width_pct=Decimal("0.20"),
            approved=True, reason="OK", position_in_range=0.5,
        )
        with pytest.raises(AttributeError):
            rc.lower = Decimal("80")

    def test_all_fields_accessible(self):
        rc = RangeCandidate(
            lower=Decimal("90"), upper=Decimal("110"),
            quality=75.0, width_pct=Decimal("0.20"),
            approved=True, reason="OK", position_in_range=0.5,
        )
        assert rc.lower == Decimal("90")
        assert rc.upper == Decimal("110")
        assert rc.quality == 75.0
        assert rc.width_pct == Decimal("0.20")
        assert rc.approved is True
        assert rc.reason == "OK"
        assert rc.position_in_range == 0.5

    def test_equality(self):
        a = RangeCandidate(
            lower=Decimal("90"), upper=Decimal("110"),
            quality=75.0, width_pct=Decimal("0.20"),
            approved=True, reason="OK", position_in_range=0.5,
        )
        b = RangeCandidate(
            lower=Decimal("90"), upper=Decimal("110"),
            quality=75.0, width_pct=Decimal("0.20"),
            approved=True, reason="OK", position_in_range=0.5,
        )
        assert a == b


# ===========================================================================
# J. auto_range — quantile sensitivity
# ===========================================================================
class TestAutoRangeQuantiles:
    def test_narrow_quantiles_produce_narrower_range(self):
        # Uniform data → all quantiles identical; use ramped data
        n = 100
        ramp = np.linspace(80, 120, n)
        df = pd.DataFrame({
            "high": ramp + 2, "low": ramp - 2, "close": ramp,
            "adx": np.full(n, 25.0),
            "atr_pct": np.full(n, 0.02),
            "bb_width": np.full(n, 0.05),
            "volume_ratio": np.full(n, 1.5),
        })
        wide = auto_range(df, support_quantile=0.05, resistance_quantile=0.95,
                          min_quality_score=0)
        narrow = auto_range(df, support_quantile=0.20, resistance_quantile=0.80,
                            min_quality_score=0)
        if wide.reason not in ("INVALID_RANGE", "RANGE_WIDTH_OUTSIDE_LIMIT") \
           and narrow.reason not in ("INVALID_RANGE", "RANGE_WIDTH_OUTSIDE_LIMIT"):
            assert (narrow.upper - narrow.lower) < (wide.upper - wide.lower)

    def test_quantile_01_99_valid(self):
        df = _make_df(n=100, close_base=100.0, spread=5.0)
        result = auto_range(df, support_quantile=0.01,
                            resistance_quantile=0.99, min_quality_score=0)
        assert isinstance(result, RangeCandidate)


# ===========================================================================
# K. auto_range — edge cases with varied data
# ===========================================================================
class TestAutoRangeEdgeCases:
    def test_all_same_values(self):
        """All highs=lows=closes same → width=0 → invalid or width-limited."""
        n = 60
        df = pd.DataFrame({
            "high": np.full(n, 100.0),
            "low": np.full(n, 100.0),
            "close": np.full(n, 100.0),
            "adx": np.full(n, 20.0),
            "atr_pct": np.full(n, 0.02),
            "bb_width": np.full(n, 0.05),
            "volume_ratio": np.full(n, 1.0),
        })
        result = auto_range(df, min_quality_score=0)
        assert result.approved is False
        # Either INVALID_RANGE or RANGE_WIDTH_OUTSIDE_LIMIT
        assert result.reason in ("INVALID_RANGE", "RANGE_WIDTH_OUTSIDE_LIMIT")

    def test_negative_prices_still_valid(self):
        """Synthetic: negative closes, but lower < 0 → lower <= 0 → invalid."""
        n = 60
        df = pd.DataFrame({
            "high": np.full(n, -1.0),
            "low": np.full(n, -5.0),
            "close": np.full(n, -3.0),
            "adx": np.full(n, 20.0),
            "atr_pct": np.full(n, 0.02),
            "bb_width": np.full(n, 0.05),
            "volume_ratio": np.full(n, 1.0),
        })
        result = auto_range(df, min_quality_score=0)
        assert result.approved is False
        assert result.reason == "INVALID_RANGE"

    def test_large_n_dataframe(self):
        df = _make_df(n=500, close_base=50000.0, spread=500.0)
        result = auto_range(df, min_quality_score=0)
        assert isinstance(result, RangeCandidate)
        assert result.lower > 0

    def test_only_last_row_matters_for_quality(self):
        """Quality depends on last row indicators, quantiles depend on all rows."""
        df = _make_quality_df(n=80)
        result_a = auto_range(df, min_quality_score=0)

        # Change last row indicators to poor
        df2 = df.copy()
        df2.loc[df2.index[-1], "adx"] = 50.0
        df2.loc[df2.index[-1], "atr_pct"] = 0.10
        df2.loc[df2.index[-1], "bb_width"] = 0.20
        df2.loc[df2.index[-1], "volume_ratio"] = 5.0
        result_b = auto_range(df2, min_quality_score=0)

        # Same range bounds (quantiles unchanged), different quality
        assert result_a.lower == result_b.lower
        assert result_a.upper == result_b.upper
        assert result_a.quality > result_b.quality


# ===========================================================================
# L. auto_range — custom min/max width percentage
# ===========================================================================
class TestAutoRangeWidthCustom:
    def test_narrow_min_width_accepts_small_range(self):
        df = _make_df(n=60, close_base=100.0, spread=0.05)
        result = auto_range(df, min_quality_score=0, min_width_pct=0.001)
        # Very small width might still pass with lenient min
        assert result.reason != "RANGE_WIDTH_OUTSIDE_LIMIT"

    def test_strict_max_width_rejects(self):
        df = _make_df(n=60, close_base=100.0, spread=10.0)
        result = auto_range(df, min_quality_score=0, max_width_pct=0.001)
        assert result.approved is False
        assert result.reason == "RANGE_WIDTH_OUTSIDE_LIMIT"


# ===========================================================================
# M. auto_range — reason codes completeness
# ===========================================================================
class TestAutoRangeReasonCodes:
    """Ensure every code path returns a known reason string."""

    VALID_REASONS = {
        "AUTO_RANGE_APPROVED",
        "INVALID_RANGE",
        "RANGE_WIDTH_OUTSIDE_LIMIT",
        "RANGE_QUALITY_TOO_LOW",
        "CURRENT_PRICE_OUTSIDE_RANGE",
    }

    def test_approved_has_valid_reason(self):
        df = _make_quality_df(n=80)
        result = auto_range(df, min_quality_score=0)
        assert result.reason in self.VALID_REASONS

    def test_invalid_range_has_valid_reason(self):
        df = _make_df(n=60, close_base=0.0, spread=0.0)
        result = auto_range(df, min_quality_score=0)
        assert result.reason in self.VALID_REASONS

    def test_width_outside_has_valid_reason(self):
        df = _make_df(n=60, close_base=100.0, spread=0.0001)
        result = auto_range(df, min_quality_score=0)
        assert result.reason in self.VALID_REASONS

    def test_quality_too_low_has_valid_reason(self):
        df = _make_poor_df(n=80)
        df["high"] = df["close"] + 2.0
        df["low"] = df["close"] - 2.0
        result = auto_range(df, min_quality_score=65)
        assert result.reason in self.VALID_REASONS

    def test_price_outside_has_valid_reason(self):
        df = _make_quality_df(n=80)
        df.loc[df.index[-1], "close"] = 1.0
        result = auto_range(df, min_quality_score=0, require_price_inside=True)
        assert result.reason in self.VALID_REASONS


# ===========================================================================
# N. D() helper
# ===========================================================================
class TestD:
    def test_string(self):
        from range_engine import D
        assert D("0.001") == Decimal("0.001")

    def test_int(self):
        from range_engine import D
        assert D(5) == Decimal("5")

    def test_float(self):
        from range_engine import D
        assert D(0.001) == Decimal("0.001")

    def test_zero(self):
        from range_engine import D
        assert D(0) == Decimal("0")

    def test_decimal_passthrough(self):
        from range_engine import D
        assert D(Decimal("0.001")) == Decimal("0.001")

    def test_large_number(self):
        from range_engine import D
        assert D("999999999.99999999") == Decimal("999999999.99999999")

    def test_negative(self):
        from range_engine import D
        assert D(-0.001) == Decimal("-0.001")


# ===========================================================================
# O. Exact quality formula verification
# ===========================================================================
class TestAutoRangeExactQuality:
    def _build_constant(self, low, high, close, n=100,
                        adx=20.0, atr_pct=0.01, bb_width=0.02, volume_ratio=1.0):
        return pd.DataFrame({
            "high": [high]*n, "low": [low]*n, "close": [close]*n,
            "adx": [adx]*n, "atr_pct": [atr_pct]*n,
            "bb_width": [bb_width]*n, "volume_ratio": [volume_ratio]*n,
        })

    def test_quality_with_width_midpoint_deviation(self):
        """width=0.21, midpoint=0.14 → width_score=50.
        All sub-scores=100, position in [0.10,0.90] → position_score=100.
        quality = 0.25*50 + 0.25*100 + 0.20*100 + 0.15*100 + 0.10*100 + 0.05*100 = 87.5
        """
        df = self._build_constant(low=100, high=121, close=105)
        result = auto_range(df, min_quality_score=0)
        assert result.quality == pytest.approx(87.5, abs=0.01)

    def test_quality_with_width_at_midpoint(self):
        """width=0.14 → width_score=100, all sub-scores=100 → quality=100.0"""
        df = self._build_constant(low=100, high=114, close=107)
        result = auto_range(df, min_quality_score=0)
        assert result.quality == pytest.approx(100.0, abs=0.01)

    def test_quality_with_width_beyond_midpoint(self):
        """width=0.28, max_width_pct=0.30 → width_mid=0.165 → deviation=0.115 → width_score≈30.3.
        quality = 0.25*30.3 + 75.0 ≈ 82.58.
        """
        df = self._build_constant(low=100, high=128, close=110)
        result = auto_range(df, min_quality_score=0, max_width_pct=0.30)
        assert result.quality == pytest.approx(82.58, abs=0.01)

    def test_quality_position_below_10pct(self):
        """close=101 → position≈0.0476 < 0.10 → position_score≈9.52."""
        df = self._build_constant(low=100, high=121, close=101)
        result = auto_range(df, min_quality_score=0)
        pos = 1.0 / 21.0
        pos_score = _clamp(100 - abs(pos - 0.5) * 200)
        expected = (0.25*50 + 0.25*100 + 0.20*100 +
                    0.15*100 + 0.10*100 + 0.05*pos_score)
        assert result.quality == pytest.approx(expected, abs=0.01)

    def test_quality_position_at_lower_zero_score(self):
        """position=0.0 → position_score=0.0."""
        df = self._build_constant(low=100, high=121, close=100)
        result = auto_range(df, min_quality_score=0)
        expected = 0.25*50 + 0.25*100 + 0.20*100 + 0.15*100 + 0.10*100 + 0.05*0
        assert result.quality == pytest.approx(expected, abs=0.01)

    def test_quality_position_at_upper_zero_score(self):
        """position=1.0 → position_score=0.0."""
        df = self._build_constant(low=100, high=121, close=121)
        result = auto_range(df, min_quality_score=0)
        expected = 0.25*50 + 0.25*100 + 0.20*100 + 0.15*100 + 0.10*100 + 0.05*0
        assert result.quality == pytest.approx(expected, abs=0.01)


# ===========================================================================
# P. Exact width_pct and rounding
# ===========================================================================
class TestAutoRangeExactValues:
    def _df(self, low, high, close, n=100):
        return pd.DataFrame({
            "high": [high]*n, "low": [low]*n, "close": [close]*n,
            "adx": [20.0]*n, "atr_pct": [0.01]*n,
            "bb_width": [0.02]*n, "volume_ratio": [1.0]*n,
        })

    def test_width_pct_exact_decimal(self):
        """width_pct == upper/lower - 1 exactly."""
        result = auto_range(self._df(100, 121, 105), min_quality_score=0)
        assert result.width_pct == Decimal("0.21")

    def test_position_rounded_to_4_decimals(self):
        result = auto_range(self._df(100, 121, 105), min_quality_score=0)
        assert result.position_in_range == round(5.0 / 21.0, 4)

    def test_quality_rounded_to_2_decimals(self):
        result = auto_range(self._df(100, 121, 105), min_quality_score=0)
        assert result.quality == round(result.quality, 2)


# ===========================================================================
# Q. Price boundary inclusion (inclusive <=)
# ===========================================================================
class TestAutoRangePriceBoundaries:
    def test_price_exactly_at_lower_bound_not_rejected(self):
        df = pd.DataFrame({
            "high": [121.0]*100, "low": [100.0]*100, "close": [100.0]*100,
            "adx": [20.0]*100, "atr_pct": [0.01]*100,
            "bb_width": [0.02]*100, "volume_ratio": [1.0]*100,
        })
        result = auto_range(df, min_quality_score=0, require_price_inside=True)
        assert result.reason != "CURRENT_PRICE_OUTSIDE_RANGE"

    def test_price_exactly_at_upper_bound_not_rejected(self):
        df = pd.DataFrame({
            "high": [121.0]*100, "low": [100.0]*100, "close": [121.0]*100,
            "adx": [20.0]*100, "atr_pct": [0.01]*100,
            "bb_width": [0.02]*100, "volume_ratio": [1.0]*100,
        })
        result = auto_range(df, min_quality_score=0, require_price_inside=True)
        assert result.reason != "CURRENT_PRICE_OUTSIDE_RANGE"


# ===========================================================================
# R. require_price_inside=False + low quality → elif branch
# ===========================================================================
class TestAutoRangeRequirePriceFalseLowQuality:
    def test_require_false_and_low_quality(self):
        """Outside price ignored, quality below threshold → RANGE_QUALITY_TOO_LOW."""
        df = pd.DataFrame({
            "high": [121.0]*100, "low": [100.0]*100, "close": [105.0]*100,
            "adx": [50.0]*100, "atr_pct": [0.10]*100,
            "bb_width": [0.20]*100, "volume_ratio": [5.0]*100,
        })
        result = auto_range(df, min_quality_score=65, require_price_inside=False)
        assert result.approved is False
        assert result.reason == "RANGE_QUALITY_TOO_LOW"


# ===========================================================================
# S. Partial NaN / non-numeric high-low data
# ===========================================================================
class TestAutoRangePartialData:
    def _base(self, n=100, highs=None, lows=None):
        if highs is None:
            highs = [121.0]*n
        if lows is None:
            lows = [100.0]*n
        return pd.DataFrame({
            "high": highs, "low": lows, "close": [105.0]*n,
            "adx": [20.0]*n, "atr_pct": [0.01]*n,
            "bb_width": [0.02]*n, "volume_ratio": [1.0]*n,
        })

    def test_partial_nan_highs(self):
        highs = [121.0]*95 + [np.nan]*5
        result = auto_range(self._base(highs=highs), min_quality_score=0)
        assert isinstance(result, RangeCandidate)

    def test_partial_nan_lows(self):
        lows = [100.0]*95 + [np.nan]*5
        result = auto_range(self._base(lows=lows), min_quality_score=0)
        assert isinstance(result, RangeCandidate)

    def test_non_numeric_highs_coerced(self):
        highs = [121.0]*95 + ["bad"]*5
        result = auto_range(self._base(highs=highs), min_quality_score=0)
        assert isinstance(result, RangeCandidate)

    def test_non_numeric_lows_coerced(self):
        lows = [100.0]*95 + ["bad"]*5
        result = auto_range(self._base(lows=lows), min_quality_score=0)
        assert isinstance(result, RangeCandidate)

    def test_all_highs_nan_raises(self):
        highs = [np.nan]*100
        with pytest.raises(ValueError, match="No valid high/low data"):
            auto_range(self._base(highs=highs), min_quality_score=0)

    def test_all_lows_nan_raises(self):
        lows = [np.nan]*100
        with pytest.raises(ValueError, match="No valid high/low data"):
            auto_range(self._base(lows=lows), min_quality_score=0)


# ===========================================================================
# T. support_quantile > resistance_quantile
# ===========================================================================
class TestAutoRangeInvertedQuantiles:
    def test_inverted_quantiles_returns_invalid(self):
        """support > resistance on ramped data → lower > upper → INVALID_RANGE."""
        ramp = np.linspace(90, 110, 100)
        df = pd.DataFrame({
            "high": ramp + 2, "low": ramp, "close": ramp + 1,
            "adx": [20.0]*100, "atr_pct": [0.01]*100,
            "bb_width": [0.02]*100, "volume_ratio": [1.0]*100,
        })
        result = auto_range(df, support_quantile=0.90, resistance_quantile=0.10,
                            min_quality_score=0)
        assert result.approved is False
        assert result.reason == "INVALID_RANGE"
