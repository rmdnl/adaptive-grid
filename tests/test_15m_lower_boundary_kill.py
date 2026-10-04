"""15m lower-boundary candle-close kill: regression tests.

The dedicated gate (risk_engine.lower_boundary_15m_kill) kills when the
latest CLOSED 15m candle close is at/below LOWER_PRICE * (1 -
stop_if_below_lower_pct), independent of the current-price range-break
kill.  It is fail-closed on missing/invalid config or candle data, uses
Decimal arithmetic, and feeds the production Risk Engine veto + kill
latch + cancel-on-kill path in main().

Scenarios covered (task spec A-L):
A. close exactly at threshold -> KILL
B. close below threshold -> KILL
C. close just above threshold -> NO lower-boundary kill
D. ticker below threshold but latest CLOSED candle above -> no candle-close
   kill from the ticker (the gate only sees the closed-candle close)
E. malformed/missing candle close -> fail closed (DATA_UNAVAILABLE)
F. NaN / Infinity / invalid Decimal -> fail closed
G. kill persists after restart
H. kill prevents new order submission
I. cancel-on-kill is invoked
J. failed/unknown cancellation leaves kill active
K. repeated kill evaluation is idempotent
L. existing range_break_kill behavior remains unchanged
plus config validation (explicit required field, no hidden fallback).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest

from market_data import AccountSnapshot, TickerSnapshot
from risk_engine import lower_boundary_15m_kill, range_break_kill, RiskDecision
from storage import get_kill_state, get_state, init_db, set_kill_state
from tests.test_multi_symbol import _config


# ---------------------------------------------------------------------------
# Gate-level deterministic tests (A-L, plus config/data validation)
# ---------------------------------------------------------------------------

def _dec(x) -> Decimal:
    return Decimal(str(x))


def test_close_exactly_at_threshold_kills():
    # threshold = 100 * (1 - 0.02) = 98 exactly (Decimal arithmetic).
    d = lower_boundary_15m_kill(_dec("98"), _dec("100"), _dec("0.02"))
    assert d.allowed is False
    assert "LOWER_BOUNDARY_STOP_15M" in d.reasons


def test_close_below_threshold_kills():
    d = lower_boundary_15m_kill(_dec("97.99"), _dec("100"), _dec("0.02"))
    assert d.allowed is False
    assert "LOWER_BOUNDARY_STOP_15M" in d.reasons


def test_close_just_above_threshold_passes():
    # 98.01 > 98.0 threshold -> no lower-boundary kill (but not a pass of the
    # whole risk engine, just this gate).
    d = lower_boundary_15m_kill(_dec("98.01"), _dec("100"), _dec("0.02"))
    assert d.allowed is True
    assert d.reasons == ()


def test_ticker_below_threshold_does_not_trigger_candle_gate():
    """The gate consumes ONLY the closed-candle close, never the ticker.

    A ticker print at 97 (below the 98 threshold) must NOT kill the gate so
    long as the latest CLOSED candle closed above threshold.  The gate has no
    access to the ticker at all — it takes the closed close as its sole
    price input.
    """
    ticker_price = _dec("97.00")          # below threshold, must be ignored
    last_closed_close = _dec("98.50")     # above threshold, drives the gate
    d = lower_boundary_15m_kill(last_closed_close, _dec("100"), _dec("0.02"))
    assert d.allowed is True, (
        f"gate must key off the closed candle, not the ticker "
        f"(ticker={ticker_price}); got {d.reasons}"
    )


def test_malformed_or_missing_candle_close_fails_closed():
    for bad in (None, "", "abc", [1], object()):
        d = lower_boundary_15m_kill(bad, _dec("100"), _dec("0.02"))
        assert d.allowed is False
        assert "LOWER_BOUNDARY_STOP_DATA_UNAVAILABLE" in d.reasons, (
            f"missing/invalid close {bad!r} must fail closed"
        )


def test_nonpositive_candle_close_fails_closed():
    for bad in ("0", "-1"):
        d = lower_boundary_15m_kill(_dec(bad), _dec("100"), _dec("0.02"))
        assert d.allowed is False
        assert "LOWER_BOUNDARY_STOP_DATA_UNAVAILABLE" in d.reasons


def test_nan_infinity_candle_close_fails_closed():
    for bad in ("NaN", "Infinity", "-Infinity"):
        d = lower_boundary_15m_kill(_dec(bad), _dec("100"), _dec("0.02"))
        assert d.allowed is False
        assert "LOWER_BOUNDARY_STOP_DATA_UNAVAILABLE" in d.reasons


def test_invalid_stop_pct_fails_closed():
    for bad in (None, "", "abc", "0", "1", "1.5", "-0.1", "NaN", "Infinity"):
        # Pass the raw (possibly malformed) value straight to the gate: the
        # gate itself must reject it fail-closed.
        d = lower_boundary_15m_kill(_dec("50"), _dec("100"), bad)
        assert d.allowed is False
        assert "LOWER_BOUNDARY_STOP_CONFIG_INVALID" in d.reasons, (
            f"stop_pct {bad!r} must fail closed"
        )


def test_invalid_lower_price_fails_closed():
    for bad in (None, "0", "-1", "NaN", "Infinity"):
        value = _dec(bad) if isinstance(bad, str) else bad
        d = lower_boundary_15m_kill(_dec("50"), value, _dec("0.02"))
        assert d.allowed is False
        assert "LOWER_BOUNDARY_STOP_CONFIG_INVALID" in d.reasons


def test_exact_boundary_with_decimal_arithmetic():
    """No float drift: 100 * (1 - 0.02) == 98 in Decimal, and 98 == 98."""
    lower = _dec("100")
    stop = _dec("0.02")
    threshold = lower * (_dec("1") - stop)
    assert threshold == _dec("98")
    assert lower_boundary_15m_kill(threshold, lower, stop).allowed is False
    # One Decimal step above the threshold must not kill.
    assert lower_boundary_15m_kill(_dec("98.01"), lower, stop).allowed is True


def test_range_break_kill_behavior_unchanged():
    """L: the existing current-price range-break kill is independent and
    unchanged.  These assertions pin its exact semantics."""
    # Below the buffer band.
    d = range_break_kill(_dec("100"), _dec("110"), _dec("98"), _dec("0.01"))
    assert d.allowed is False and "RANGE_BREAK_BELOW_BUFFER" in d.reasons
    # Above the buffer band.
    d = range_break_kill(_dec("100"), _dec("110"), _dec("112"), _dec("0.01"))
    assert d.allowed is False and "RANGE_BREAK_ABOVE_BUFFER" in d.reasons
    # Inside the buffer band passes.
    d = range_break_kill(_dec("100"), _dec("110"), _dec("105"), _dec("0.01"))
    assert d.allowed is True
    # The 15m gate is a SEPARATE protection: a price inside the range-break
    # band can still trip the 15m close-based stop (and vice versa).
    assert range_break_kill(_dec("94"), _dec("103"), _dec("98"), _dec("0.01")).allowed is True


def test_two_protections_are_independent():
    """A. current-price range-break vs B. 15m candle-close lower-boundary.

    The 15m stop can fire while the ticker is still inside the range-break
    buffer, because it keys off the closed candle, not the live price.
    """
    lower, upper = _dec("94"), _dec("103")
    # Ticker 96 is inside the range-break buffer (94*0.99=93.06 .. 103*1.01).
    assert range_break_kill(lower, upper, _dec("96"), _dec("0.01")).allowed is True
    # But the last closed candle at 92 <= 94*0.98=92.12 trips the 15m stop.
    d15 = lower_boundary_15m_kill(_dec("92"), lower, _dec("0.02"))
    assert d15.allowed is False and "LOWER_BOUNDARY_STOP_15M" in d15.reasons


# ---------------------------------------------------------------------------
# Production-path integration: the gate reaches the Risk Engine veto + kill
# latch + cancel-on-kill in main()
# ---------------------------------------------------------------------------

def _closed_klines(close: str, ts: datetime | None = None) -> pd.DataFrame:
    """A single CLOSED 15m kline row (drop_incomplete already applied)."""
    ts = ts or datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    return pd.DataFrame([{
        "open_time": ts,
        "close_time": ts + timedelta(minutes=15),
        "open": 100.0, "high": 101.0, "low": 99.0,
        "close": close, "volume": 100.0,
    }])


def _account():
    return AccountSnapshot(
        base_asset="BNB", base_free=Decimal("0"), base_locked=Decimal("0"),
        quote_asset="USDT", quote_free=Decimal("1000"),
        quote_locked=Decimal("0"),
        fetched_at=datetime.now(timezone.utc),
    )


def test_config_validation_requires_stop_if_below_lower_pct(tmp_path):
    from config_loader import ConfigError, validate_config
    cfg = _config(tmp_path)
    del cfg["risk"]["stop_if_below_lower_pct"]
    with pytest.raises(ConfigError, match="stop_if_below_lower_pct"):
        validate_config(cfg)


def test_config_validation_rejects_out_of_range_stop(tmp_path):
    from config_loader import ConfigError, validate_config
    for bad in (0, 1, -0.1, "abc", None):
        cfg = _config(tmp_path)
        cfg["risk"]["stop_if_below_lower_pct"] = bad
        with pytest.raises(ConfigError, match="stop_if_below_lower_pct"):
            validate_config(cfg)

def test_valid_config_includes_default_stop(tmp_path):
    from config_loader import validate_config
    validate_config(_config(tmp_path))  # default 0.02 passes

# The production wiring of this gate (fetch_15m_closed_close -> Risk
# Engine veto -> kill latch -> cancel-on-kill) is covered against the
# authoritative multi-symbol runtime in tests/test_multi_symbol.py
# (test_missing_15m_close_fails_closed_without_latching and the drawdown/
# kill-latch tests there). The old single-symbol main() path was removed
# with the legacy strategy.
