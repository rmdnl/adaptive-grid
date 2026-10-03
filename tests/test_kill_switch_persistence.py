"""PATCH 1 (F-H1): persisted equity-drawdown kill switch.

Regression tests for the production-path kill switch:

1. The reference/peak equity persists in SQLite (bot_state KV store) and
   survives a process restart — it is never reset to the current equity on
   each invocation.
2. A >= 2% drawdown against the persisted peak blocks new submissions
   through the actual production execution path (``main.main``), not only
   the risk-engine unit function.  The triggered kill switch stops new
   submissions; open orders are preserved because no cancel path exists
   yet (F-H2 territory, out of scope for PATCH 1).
3. A healthy equity increase does not incorrectly trigger the kill switch.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import main
from market_data import AccountSnapshot, TickerSnapshot
from storage import get_state, init_db
from tests.test_main_order_integration import _install_main_stubs, _symbol_info

DB = "grid.sqlite3"


def _wide_percent_price_symbol_info():
    """Exchange stub with a wider PERCENT_PRICE band (±10%).

    The integration suite's stub uses ±5%, which at current price ~101 makes
    the executable band [95.95, 106.05] — narrower than this file's manual
    range [94, 103].  Grid cells near the range edge would then be rejected
    by PERCENT_PRICE quantization, so the equity kill switch would no longer
    be the ONLY reason new submissions are blocked at price 94 (the docstring
    intent above).  Widening the exchange band to ±10% keeps the whole range
    executable and isolates the kill-switch behavior under test.
    """
    info = _symbol_info()
    for f in info["filters"]:
        if f.get("filterType") == "PERCENT_PRICE":
            f["multiplierUp"] = "1.1"
            f["multiplierDown"] = "0.9"
    return info

# These tests monkeypatch ``fetch_account_snapshot`` to a stub exchange
# account of 5 BNB + 1000 USDT, so equity = 1000 + 5 * price.  The
# persisted peak (1500 at price 100) is tripped at 2% when equity <= 1470,
# i.e. price <= 94.  The manual range [94, 103] keeps 94 inside the
# range-break buffer (94 >= 93.06), so the ONLY reason new submissions are
# blocked there is the kill switch itself.


def _account_snapshot_5bnb():
    return AccountSnapshot(
        base_asset="BNB",
        base_free=Decimal("5"),
        base_locked=Decimal("0"),
        quote_asset="USDT",
        quote_free=Decimal("1000"),
        quote_locked=Decimal("0"),
        fetched_at=datetime.now(timezone.utc),
    )


def _config(tmp_path):
    """Stub config for the main-loop kill-switch tests (BNBUSDT, 15m).

    Mirrors tests/test_main_order_integration._config with the manual range
    narrowed so the 2% drawdown threshold lands inside the executable band.
    """
    return {
        "environment": {"mode": "testnet", "dry_run": True, "allow_live_execution": False},
        "symbol": "BNBUSDT",
        "timeframe": "15m",
        "grid": {
            "step_pct": Decimal("0.006"),
            "hard_min_net_pct": Decimal("0.003"),
            "preferred_net_max_pct": Decimal("0.004"),
            "min_cells": 6,
            "max_levels": 40,
        },
        "range": {
            "mode": "manual",
            "lower_price": Decimal("94"),
            "upper_price": Decimal("103"),
            "lookback": 200,
            "buffer_pct": Decimal("0.01"),
            "auto": {},
        },
        "market_filter": {
            "adx_max": Decimal("28"),
            "atr_pct_max": Decimal("0.025"),
            "bb_width_max": Decimal("0.06"),
            "volume_spike_max": Decimal("2.5"),
        },
        "execution": {
            "prefer_limit_maker": True,
            "stale_order_minutes": 30,
            "max_open_orders": 40,
            "order_quote_size": Decimal("25"),
            "total_quote_budget": Decimal("0"),
            "max_inventory_pct": Decimal("0.70"),
        },
        "fees": {
            "maker_fee_fallback": Decimal("0.001"),
            "taker_fee_fallback": Decimal("0.001"),
            "slippage_roundtrip_pct": Decimal("0.0005"),
        },
        "paper": {
            "initial_base_balance": Decimal("2"),
            "initial_quote_balance": Decimal("1000"),
            "maker_fee": Decimal("0.001"),
            "taker_fee": Decimal("0.001"),
            "fee_asset": "USDT",
        },
        "risk": {
            "max_equity_drawdown_pct": Decimal("0.02"),
            "range_break_buffer_pct": Decimal("0.01"),
            "daily_profit_lock_pct": Decimal("0.01"),
            "cooldown_minutes": 30,
            "stop_if_below_lower_pct": Decimal("0.02"),
        },
        "logging": {
            "sqlite_path": str(tmp_path / DB),
            "log_path": str(tmp_path / "grid.log"),
            "csv_path": str(tmp_path / "trades.csv"),
        },
    }


def _seed_peak(db_path, equity: str) -> None:
    """Persist the reference equity exactly as an earlier process did."""
    init_db(db_path)
    main.record_peak_equity(db_path, Decimal(equity))


def _risk_reasons(db_path):
    with sqlite3.connect(db_path) as con:
        row = con.execute(
            "SELECT reason FROM risk_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return row[0] if row else ""


def _paper_orders_state(db_path):
    raw = get_state(db_path, "last_paper_orders")
    return json.loads(raw) if raw is not None else None


# ---------------------------------------------------------------------------
# 1. Persistence across a restart
# ---------------------------------------------------------------------------

def test_peak_equity_persists_across_restart(tmp_path):
    db = str(tmp_path / DB)
    _seed_peak(db, "1000")
    # Simulate a fresh process: load from SQLite only, not from memory.
    assert main.load_peak_equity(db) == Decimal("1000")


def test_drawdown_not_reset_to_current_equity(tmp_path):
    """A 4% drop versus the persisted reference must remain visible.

    The old process-local behavior reset the reference to the current equity
    on every invocation, making drawdown 0 by construction.
    """
    db = str(tmp_path / DB)
    _seed_peak(db, "1000")
    loaded = main.load_peak_equity(db)
    current = Decimal("960")
    drawdown = (loaded - current) / loaded
    assert drawdown == Decimal("0.04")
    # Recording a LOWER current equity must not lower the persisted peak.
    main.record_peak_equity(db, current)
    assert main.load_peak_equity(db) == Decimal("1000")


def test_peak_equity_rises_with_healthy_growth(tmp_path):
    db = str(tmp_path / DB)
    _seed_peak(db, "1000")
    main.record_peak_equity(db, Decimal("1200"))
    assert main.load_peak_equity(db) == Decimal("1200")
    main.record_peak_equity(db, Decimal("1100"))  # lower -> peak unchanged
    assert main.load_peak_equity(db) == Decimal("1200")


def test_load_peak_equity_fails_closed_on_corrupt_state(tmp_path):
    db = str(tmp_path / DB)
    init_db(db)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO bot_state(key,value) VALUES "
            "('paper_reference_equity','garbage')"
        )
        con.commit()
    assert main.load_peak_equity(db) is None


def test_load_peak_equity_absent_returns_none(tmp_path):
    db = str(tmp_path / DB)
    init_db(db)
    assert main.load_peak_equity(db) is None


# ---------------------------------------------------------------------------
# 2. Production execution path: >= 2% drawdown blocks new submissions
# ---------------------------------------------------------------------------

def test_drawdown_blocks_new_submissions_after_restart(tmp_path, monkeypatch):
    """A process restart sees a >= 2% drawdown against the persisted peak
    (5 BNB + 1000 USDT stub: peak 1500 at price 100; price 94 => equity
    1470 => 2% drawdown) and the production risk path blocks new
    submissions.  No paper cycle runs; no new orders are created."""
    db = str(tmp_path / DB)
    _seed_peak(db, "1500")  # earlier process observed equity 1500 (price 100)

    _install_main_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(main, "fetch_symbol_info", lambda client, symbol: _wide_percent_price_symbol_info())
    monkeypatch.setattr(main, "fetch_account_snapshot",
                        lambda client, base, quote: _account_snapshot_5bnb())
    monkeypatch.setattr(
        main,
        "fetch_ticker_price",
        lambda client, symbol: TickerSnapshot(
            symbol="BNBUSDT", price=Decimal("94.00"),
            fetched_at=datetime.now(timezone.utc),
        ),
    )
    assert main.main() == 0

    # Kill switch tripped through the production gate, not just the unit.
    assert "EQUITY_DRAWDOWN_KILL" in _risk_reasons(db)
    decision = json.loads(get_state(db, "last_risk_decision"))
    assert decision["allowed"] is False
    # No new paper cycle ran: no paper orders were recorded this run.
    assert _paper_orders_state(db) is None
    # The persisted reference survived: it is still the seeded peak.
    assert main.load_peak_equity(db) == Decimal("1500")


def test_corrupt_reference_equity_blocks_fail_closed(tmp_path, monkeypatch):
    """Persisted state present but unparsable/invalid -> fail closed: the
    run blocks (no silent substitution of the current equity)."""
    db = str(tmp_path / DB)
    init_db(db)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO bot_state(key,value) VALUES "
            "('paper_reference_equity','garbage')"
        )
        con.commit()

    _install_main_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(main, "fetch_account_snapshot",
                        lambda client, base, quote: _account_snapshot_5bnb())
    monkeypatch.setattr(
        main,
        "fetch_ticker_price",
        lambda client, symbol: TickerSnapshot(
            symbol="BNBUSDT", price=Decimal("98.00"),
            fetched_at=datetime.now(timezone.utc),
        ),
    )
    assert main.main() == 0

    reason = _risk_reasons(db)
    assert "EQUITY_REFERENCE" in reason, f"corrupt reference must block: {reason!r}"
    decision = json.loads(get_state(db, "last_risk_decision"))
    assert decision["allowed"] is False


def test_kill_switch_blocks_submission_with_open_book_preserved(tmp_path):
    """Engine-level proof through the real paper state: an open,
    reservation-backed order stays intact while a new submission is vetoed
    by the kill-switch risk decision (no invented cancel behavior)."""
    from decimal import Decimal as D

    from order_engine import OrderIntent, PaperOrderEngine, make_client_order_id
    from paper_accounting import PaperAccountingEngine
    from risk_engine import RiskDecision

    db = str(tmp_path / DB)
    accounting = PaperAccountingEngine(
        "BNB", "USDT", D("2"), D("1000"), D("0.001"), D("0.001"), "USDT"
    )
    engine = PaperOrderEngine(
        db, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        accounting=accounting,
    )
    cid = make_client_order_id("AG", "BNBUSDT", 1, 0, "BUY")
    intent = OrderIntent(
        client_order_id=cid,
        symbol="BNBUSDT",
        side="BUY",
        order_type="LIMIT_MAKER",
        price=D("98.0"),
        quantity=D("0.01"),
        time_in_force="GTC",
        grid_index=0,
        generation=1,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    engine.submit(intent, RiskDecision(True), D("94"), D("103"))
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM orders WHERE status IN ('OPEN','PARTIALLY_FILLED')"
        ).fetchone()[0] == 1

    # Persisted reference far above current paper equity => >= 2% drawdown.
    main.record_peak_equity(db, D("1100"))

    # A second engine (fresh process) loads the same persisted state; the
    # kill-switch risk decision must reject a new submission.
    engine2 = PaperOrderEngine(
        db, clock=lambda: datetime(2026, 1, 2, tzinfo=timezone.utc),
        accounting=accounting,
    )
    cid2 = make_client_order_id("AG", "BNBUSDT", 1, 1, "BUY")
    intent2 = OrderIntent(
        client_order_id=cid2,
        symbol="BNBUSDT",
        side="BUY",
        order_type="LIMIT_MAKER",
        price=D("98.2"),
        quantity=D("0.01"),
        time_in_force="GTC",
        grid_index=1,
        generation=1,
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    risk = RiskDecision(False, ("EQUITY_DRAWDOWN_KILL",))
    try:
        engine2.submit(intent2, risk, D("94"), D("103"))
        raised = False
    except Exception:
        raised = True
    assert raised, "submit must be rejected under a triggered kill switch"
    # Open book preserved: the existing reservation-backed order is intact.
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM orders WHERE status IN ('OPEN','PARTIALLY_FILLED')"
        ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# 3. Healthy equity growth does not trigger the kill switch
# ---------------------------------------------------------------------------

def test_equity_growth_does_not_trigger_kill_switch(tmp_path, monkeypatch):
    """Persisted peak 1500; restart at a HIGHER price (101): drawdown is 0
    and the kill switch must NOT block — new submissions proceed."""
    db = str(tmp_path / DB)
    _seed_peak(db, "1500")

    _install_main_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(main, "fetch_symbol_info", lambda client, symbol: _wide_percent_price_symbol_info())
    monkeypatch.setattr(main, "fetch_account_snapshot",
                        lambda client, base, quote: _account_snapshot_5bnb())
    monkeypatch.setattr(
        main,
        "fetch_ticker_price",
        lambda client, symbol: TickerSnapshot(
            symbol="BNBUSDT", price=Decimal("101.00"),
            fetched_at=datetime.now(timezone.utc),
        ),
    )
    assert main.main() == 0

    reason = _risk_reasons(db)
    assert "EQUITY_DRAWDOWN_KILL" not in reason, \
        f"healthy equity growth must not trip the kill switch: {reason!r}"
    assert "EQUITY_REFERENCE" not in reason
    decision = json.loads(get_state(db, "last_risk_decision"))
    assert decision["allowed"] is True
    # The run recorded the higher equity (1000 + 5*101 = 1505) as the new
    # persisted peak.
    assert main.load_peak_equity(db) == Decimal("1505")


# ---------------------------------------------------------------------------
# Equity snapshot persistence (dashboard observability)
# ---------------------------------------------------------------------------

def test_production_cycle_persists_equity_snapshot(tmp_path, monkeypatch):
    """Every production cycle persists the equity/drawdown it already
    computed (storage.record_equity, previously an unwired helper) so the
    read-only dashboard can display it.  Pure observability: the risk
    decision and trading behavior are unchanged."""
    db = str(tmp_path / DB)
    _seed_peak(db, "1500")  # peak: 5 BNB + 1000 USDT @ price 100

    _install_main_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(main, "fetch_symbol_info",
                        lambda client, symbol: _wide_percent_price_symbol_info())
    monkeypatch.setattr(main, "fetch_account_snapshot",
                        lambda client, base, quote: _account_snapshot_5bnb())
    monkeypatch.setattr(
        main, "fetch_ticker_price",
        lambda client, symbol: TickerSnapshot(
            symbol="BNBUSDT", price=Decimal("94.00"),
            fetched_at=datetime.now(timezone.utc),
        ),
    )
    assert main.main() == 0

    with sqlite3.connect(db) as con:
        rows = con.execute(
            "SELECT equity_quote, drawdown_pct FROM equity_snapshots"
            " ORDER BY ts DESC LIMIT 1"
        ).fetchall()
    assert rows, "cycle must persist an equity snapshot"
    equity, drawdown = rows[0]
    # 5 BNB @ 94 + 1000 USDT = 1470; drawdown vs the 1500 peak = 2%
    assert Decimal(equity) == Decimal("1470")
    assert Decimal(drawdown) == Decimal("0.02")
    # observability only: the run above still blocked via the kill switch
    assert "EQUITY_DRAWDOWN_KILL" in _risk_reasons(db)
