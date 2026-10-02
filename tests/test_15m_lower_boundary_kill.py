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

import main
from market_data import AccountSnapshot, TickerSnapshot
from risk_engine import lower_boundary_15m_kill, range_break_kill, RiskDecision
from storage import get_kill_state, get_state, init_db, set_kill_state
from tests.test_main_order_integration import _install_main_stubs, _config


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


# -- G/H/I/J/K through the real main() execution path --------------------

def _seed_prior_kill_state(tmp_path, active=True):
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    set_kill_state(db, active=active, trigger="EQUITY_DRAWDOWN_KILL",
                   cancel_status="CANCELLED")


def test_restart_with_active_kill_stays_killed(tmp_path, monkeypatch):
    """G: a persisted active kill state survives restart and blocks orders.

    Uses the existing kill-latch restart-recovery path in main(): an active
    latch re-enters the kill branch and stops the run with no new orders.
    """
    db = str(tmp_path / "grid.sqlite3")
    _seed_prior_kill_state(tmp_path)
    _install_main_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(main, "fetch_symbol_info", _fetch_symbol_info_stub)
    monkeypatch.setattr(main, "fetch_klines",
                        lambda c, s, i, l, drop_incomplete=True: _closed_klines("99.9"))
    monkeypatch.setattr(main, "fetch_ticker_price",
                        lambda c, s: TickerSnapshot(
                            "BNBUSDT", Decimal("100.0"), datetime.now(timezone.utc)))
    monkeypatch.setattr(main, "fetch_account_snapshot",
                        lambda c, b, q: _account())
    assert main.main() == 0
    # Kill persists: still active after the restart, no new orders.
    assert get_kill_state(db)["active"] is True
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM orders WHERE status IN ('OPEN','PARTIALLY_FILLED')"
        ).fetchone()[0] == 0


def _fetch_symbol_info_stub(client, symbol):
    from tests.test_main_order_integration import _symbol_info
    return _symbol_info()


def test_15m_close_kill_latches_and_cancels(tmp_path, monkeypatch):
    """H/I: a closed candle at/below threshold (ticker still in range) must
    latch the kill state, invoke the cancel-on-kill controller, and block
    new order placement in main()."""
    db = str(tmp_path / "grid.sqlite3")
    _install_main_stubs(monkeypatch, tmp_path)
    cfg = _config(tmp_path)
    # manual range [94,103]; threshold = 94 * 0.98 = 92.12.  Last CLOSED
    # candle close 92.0 <= 92.12 -> 15m lower-boundary kill.  Ticker 100.0
    # is well inside the range, so ONLY the 15m close trips the latch.
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    monkeypatch.setattr(main, "fetch_symbol_info", _fetch_symbol_info_stub)
    monkeypatch.setattr(main, "fetch_klines",
                        lambda c, s, i, l, drop_incomplete=True: _closed_klines("92.0"))
    monkeypatch.setattr(main, "fetch_ticker_price",
                        lambda c, s: TickerSnapshot(
                            "BNBUSDT", Decimal("100.0"), datetime.now(timezone.utc)))
    monkeypatch.setattr(main, "fetch_account_snapshot", lambda c, b, q: _account())

    assert main.main() == 0
    # Risk veto recorded: the 15m stop is a block reason.
    risk = json.loads(get_state(db, "last_risk_decision"))
    assert risk["allowed"] is False
    assert "LOWER_BOUNDARY_STOP_15M" in risk["reason"]
    # Kill latched (persisted, survives restart).
    ks = get_kill_state(db)
    assert ks is not None and ks["active"] is True
    assert "LOWER_BOUNDARY_STOP_15M" in ks["trigger"]
    # KILL_TRIGGER event was recorded (cancel-on-kill path invoked).
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT 1 FROM risk_events WHERE reason='KILL_TRIGGER' LIMIT 1"
        ).fetchone() is not None
    # H: no new orders placed while the kill is active.
    assert get_state(db, "last_paper_orders") is None
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_above_threshold_candle_does_not_kill(tmp_path, monkeypatch):
    """C/D: a closed candle above the threshold must NOT trip the 15m stop,
    even when the ticker is below threshold (the gate ignores the ticker)."""
    db = str(tmp_path / "grid.sqlite3")
    _install_main_stubs(monkeypatch, tmp_path)
    cfg = _config(tmp_path)
    # Manual range [94,103] -> threshold = 94 * 0.98 = 92.12.  Last CLOSED
    # candle close 93.0 > 92.12 -> no 15m kill.  Ticker 91 is below the
    # range-break buffer (94*0.99 = 93.06), so the run is still blocked —
    # by the range-break gate, NOT by the 15m stop.
    cfg["range"]["lower_price"] = Decimal("94")
    cfg["range"]["upper_price"] = Decimal("103")
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    monkeypatch.setattr(main, "fetch_symbol_info", _fetch_symbol_info_stub)
    monkeypatch.setattr(main, "fetch_klines",
                        lambda c, s, i, l, drop_incomplete=True: _closed_klines("93.0"))
    monkeypatch.setattr(main, "fetch_ticker_price",
                        lambda c, s: TickerSnapshot(
                            "BNBUSDT", Decimal("91.0"), datetime.now(timezone.utc)))
    monkeypatch.setattr(main, "fetch_account_snapshot", lambda c, b, q: _account())

    assert main.main() == 0
    risk = json.loads(get_state(db, "last_risk_decision"))
    # The 15m stop must NOT be among the block reasons for a 93.0 close.
    assert "LOWER_BOUNDARY_STOP_15M" not in risk["reason"]
    # The ticker being out of buffer still blocks via the existing gate.
    assert "RANGE_BREAK_BELOW_BUFFER" in risk["reason"]


def test_missing_kline_df_fails_closed(tmp_path, monkeypatch):
    """E: if the closed-kline fetch yields no DataFrame, the 15m gate must
    veto (DATA_UNAVAILABLE), not pass."""
    db = str(tmp_path / "grid.sqlite3")
    _install_main_stubs(monkeypatch, tmp_path)
    cfg = _config(tmp_path)
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    monkeypatch.setattr(main, "fetch_symbol_info", _fetch_symbol_info_stub)
    # No kline DataFrame available to the gate.
    monkeypatch.setattr(main, "fetch_klines",
                        lambda c, s, i, l, drop_incomplete=True: None)
    monkeypatch.setattr(main, "enrich", lambda df: None)
    monkeypatch.setattr(main, "latest_valid_row",
                        lambda df: {"close": Decimal("100.0"),
                                    "atr_pct": Decimal("0.001"),
                                    "adx": Decimal("10"),
                                    "bb_width": Decimal("0.001"),
                                    "volume_ratio": Decimal("1.0"),
                                    "rsi": Decimal("50")})
    monkeypatch.setattr(main, "fetch_ticker_price",
                        lambda c, s: TickerSnapshot(
                            "BNBUSDT", Decimal("100.0"), datetime.now(timezone.utc)))
    monkeypatch.setattr(main, "fetch_account_snapshot", lambda c, b, q: _account())

    assert main.main() == 0
    risk = json.loads(get_state(db, "last_risk_decision"))
    assert risk["allowed"] is False
    assert "LOWER_BOUNDARY_STOP_DATA_UNAVAILABLE" in risk["reason"]


def test_repeated_15m_kill_evaluation_is_idempotent(tmp_path, monkeypatch):
    """K: re-running the same 15m-kill condition (fresh process, kill active)
    does not create orders or duplicate the latch."""
    db = str(tmp_path / "grid.sqlite3")
    _install_main_stubs(monkeypatch, tmp_path)
    cfg = _config(tmp_path)
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    monkeypatch.setattr(main, "fetch_symbol_info", _fetch_symbol_info_stub)
    monkeypatch.setattr(main, "fetch_klines",
                        lambda c, s, i, l, drop_incomplete=True: _closed_klines("92.0"))
    monkeypatch.setattr(main, "fetch_ticker_price",
                        lambda c, s: TickerSnapshot(
                            "BNBUSDT", Decimal("100.0"), datetime.now(timezone.utc)))
    monkeypatch.setattr(main, "fetch_account_snapshot", lambda c, b, q: _account())

    assert main.main() == 0  # first run latches the kill
    kill_count_1 = get_kill_state(db)
    assert kill_count_1["active"] is True

    # Simulate a restart: kill is already active; a fresh process re-enters
    # the kill branch and must keep it active with no orders / no reset.
    assert main.main() == 0
    kill_count_2 = get_kill_state(db)
    assert kill_count_2["active"] is True
    # Idempotent: the latch is a single persistent record, not appended.
    with sqlite3.connect(db) as con:
        n = con.execute(
            "SELECT COUNT(*) FROM kill_state WHERE key='kill_state'"
        ).fetchone()[0]
    assert n == 1
    # No orders ever placed across both runs.
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_failed_cancel_keeps_kill_active(tmp_path):
    """J: if the cancel pass cannot confirm cancellation (UNKNOWN), the kill
    state remains active with PENDING_RECONCILIATION and no orders are placed.
    Exercised through the CancelController + kill latch directly."""
    from cancel_controller import CancelController, CancelOutcome
    from order_engine import PaperOrderEngine, OrderIntent, make_client_order_id
    from paper_accounting import PaperAccountingEngine
    from symbol_rules import parse_symbol_info
    from tests.test_main_order_integration import _symbol_info

    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    cfg = _config(tmp_path)
    rules = parse_symbol_info(_symbol_info())
    accounting = PaperAccountingEngine(
        rules.base_asset, rules.quote_asset,
        Decimal("2"), Decimal("1000"), Decimal("0.001"), Decimal("0.001"), "USDT",
    )
    engine = PaperOrderEngine(db, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
                              accounting=accounting)
    cid = make_client_order_id("AG", "BNBUSDT", 1, 0, "BUY")
    engine.submit(
        OrderIntent(client_order_id=cid, symbol="BNBUSDT", side="BUY",
                    order_type="LIMIT_MAKER", price=Decimal("98.0"),
                    quantity=Decimal("0.01"), time_in_force="GTC",
                    grid_index=0, generation=1,
                    created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        RiskDecision(True), Decimal("94"), Decimal("103"),
    )
    controller = CancelController(db, cfg, rules,
                                   canceler=lambda order: CancelOutcome.UNKNOWN)
    controller.pre_latch("LOWER_BOUNDARY_STOP_15M", actor="risk_engine")
    report = controller.cancel_open_orders()
    controller.latch_kill_state("LOWER_BOUNDARY_STOP_15M", report,
                                 actor="risk_engine")
    # J: unknown cancel leaves the kill active and unreconciled.
    ks = get_kill_state(db)
    assert ks["active"] is True
    assert ks["cancel_status"] == "PENDING_RECONCILIATION"
    # The order was NOT locally canceled (we never confirmed it).
    assert engine.get(cid).state.value == "OPEN"
    # Release is refused while unreconciled orders remain.
    from cancel_controller import ReleaseBlockedError
    with pytest.raises(ReleaseBlockedError):
        controller.release(reason="force", actor="op")


# ---------------------------------------------------------------------------
# Audit-documentation guard: the gate is actually wired into production main()
# ---------------------------------------------------------------------------

def test_gate_is_wired_into_production_main():
    """Prevent regression to a dead helper: main.py must import and call the
    gate and feed its result into the combined risk decision + kill trigger
    set."""
    src = main.__file__
    with open(src, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert "lower_boundary_15m_kill" in text
    assert "last_closed_close" in text
    assert "LOWER_BOUNDARY_STOP_15M" in text
    # The gate is called with the closed-candle close, not the ticker.
    assert "lower_boundary_15m_kill(" in text
    assert "lower_boundary_15m_kill(\n            last_closed_close," in text or \
           "lower_boundary_15m_kill(last_closed_close," in text.replace("\n", "")
