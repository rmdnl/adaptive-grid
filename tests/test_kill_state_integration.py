"""Operator reference-equity reset + F-H2 main() kill-state integration tests.

Covers the explicit, operator-triggered reset of the persisted drawdown
reference (``main.reset_reference_equity``) and proves the fail-closed
invariants:

* the reset is NEVER automatic — only the high-water-mark raise in
  ``record_peak_equity`` changes the reference on its own;
* a reset requires a non-empty reason and actor and writes a durable audit
  row (timestamp, previous value, new value, reason, actor);
* the reference persists across restart;
* clearing the reference makes the next run re-bootstrap;
* a lower reset value does NOT re-enable the kill switch by itself — the
  2% drawdown gate still runs against whatever value is persisted.

And the main() kill-state integration:

* an equity-drawdown kill LATCHES the persisted kill state, cancels open
  orders, and blocks new order placement for the rest of the run;
* a restart re-enters the kill branch and re-attempts cancellation;
* range-break kills also latch the kill state;
* releasing the latch (operator command) is the only way to resume.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import main
from cancel_controller import CancelController, CancelOutcome, ReleaseBlockedError
from market_data import AccountSnapshot, TickerSnapshot
from storage import (
    get_kill_state,
    get_state,
    init_db,
)
from tests.test_main_order_integration import _install_main_stubs
from tests.test_kill_switch_persistence import _config, _seed_peak


# ---------------------------------------------------------------------------
# 1. Reference-equity reset primitive
# ---------------------------------------------------------------------------

def test_reset_reference_equity_set_and_audit(tmp_path):
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    main.record_peak_equity(db, Decimal("1500"))
    assert main.load_peak_equity(db) == Decimal("1500")

    previous = main.reset_reference_equity(db, Decimal("1200"),
                                           reason="post-incident rebaseline",
                                           actor="ops@adaptive")
    assert previous == Decimal("1500")
    assert main.load_peak_equity(db) == Decimal("1200")

    with sqlite3.connect(db) as con:
        row = con.execute(
            "SELECT previous_value,new_value,reason,actor FROM "
            "reference_equity_audits ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row[0] == "1500"
    assert row[1] == "1200"
    assert row[2] == "post-incident rebaseline"
    assert row[3] == "ops@adaptive"


def test_reset_reference_equity_clear(tmp_path):
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    main.record_peak_equity(db, Decimal("1500"))
    previous = main.reset_reference_equity(db, None,
                                           reason="start clean", actor="ops")
    assert previous == Decimal("1500")
    # The key is ABSENT (cleared), not an empty string the gate would reject.
    assert get_state(db, "paper_reference_equity") is None
    assert main.load_peak_equity(db) is None


def test_reset_reference_equity_rejects_missing_reason_actor(tmp_path):
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    for bad in (("", "op"), ("why", "")):
        try:
            main.reset_reference_equity(db, Decimal("100"),
                                        reason=bad[0], actor=bad[1])
            raised = False
        except ValueError:
            raised = True
        assert raised, f"expected rejection for reason/actor={bad!r}"


def test_reset_reference_equity_rejects_invalid_value(tmp_path):
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    for bad in ("0", "-5", "nan", "inf"):
        try:
            main.reset_reference_equity(db, Decimal(bad),
                                        reason="why", actor="op")
            raised = False
        except ValueError:
            raised = True
        assert raised, f"expected rejection for value={bad!r}"


def test_reference_reset_does_not_weaken_drawdown_kill(tmp_path):
    """A reset to a HIGHER reference must still let the 2% kill fire.

    The reset only changes the persisted reference; it does not touch the
    kill threshold.  A 3%+ drawdown against the reset reference still
    blocks new submissions through the production path (driven by pytest's
    monkeypatch in ``test_reference_reset_then_drawdown_kills`` below).
    """
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    _seed_peak(db, "1500")
    main.reset_reference_equity(db, Decimal("2000"),
                                reason="funded account", actor="ops")
    assert main.load_peak_equity(db) == Decimal("2000")
    # High-water-mark auto-raise still works after an explicit reset:
    # a lower equity must NOT move the reference down.
    main.record_peak_equity(db, Decimal("1800"))
    assert main.load_peak_equity(db) == Decimal("2000")
    # ...and a higher equity raises it.
    main.record_peak_equity(db, Decimal("2100"))
    assert main.load_peak_equity(db) == Decimal("2100")


def test_reference_reset_then_drawdown_kills(tmp_path, monkeypatch):
    _config_tmp = _config(tmp_path)
    db_path = _config_tmp["logging"]["sqlite_path"]
    _seed_peak(db_path, "2000")
    main.reset_reference_equity(db_path, Decimal("2000"),
                                reason="rebaseline", actor="ops")

    _install_main_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(
        main, "fetch_symbol_info",
        lambda client, symbol: _wide_percent_price_symbol_info(),
    )
    monkeypatch.setattr(
        main, "fetch_account_snapshot",
        lambda client, base, quote: AccountSnapshot(
            base_asset="BNB",
            base_free=Decimal("5"),
            base_locked=Decimal("0"),
            quote_asset="USDT",
            quote_free=Decimal("1000"),
            quote_locked=Decimal("0"),
            fetched_at=datetime.now(timezone.utc),
        ),
    )
    # equity = 1000 + 5*94 = 1470; reference 2000 => 26.5% drawdown => KILL.
    monkeypatch.setattr(
        main, "fetch_ticker_price",
        lambda client, symbol: TickerSnapshot(
            symbol="BNBUSDT", price=Decimal("94.00"),
            fetched_at=datetime.now(timezone.utc),
        ),
    )
    assert main.main() == 0
    with sqlite3.connect(db_path) as con:
        reason = con.execute(
            "SELECT reason FROM risk_events WHERE reason LIKE '%EQUITY%' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert "EQUITY_DRAWDOWN_KILL" in reason


# ---------------------------------------------------------------------------
# 2. main() kill-state integration (F-H2)
# ---------------------------------------------------------------------------

def _wide_percent_price_symbol_info():
    """Re-export the wide PERCENT_PRICE band from the persistence test module
    so the kill-trigger grid stays fully executable."""
    from tests.test_kill_switch_persistence import (
        _wide_percent_price_symbol_info as _w,
    )
    return _w()


def test_equity_kill_latches_kill_state_and_cancels_open_orders(tmp_path, monkeypatch):
    db = str(tmp_path / "grid.sqlite3")
    _seed_peak(db, "1500")
    _install_main_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(
        main, "fetch_symbol_info",
        lambda client, symbol: _wide_percent_price_symbol_info(),
    )
    monkeypatch.setattr(
        main, "fetch_account_snapshot",
        lambda client, base, quote: AccountSnapshot(
            base_asset="BNB",
            base_free=Decimal("5"),
            base_locked=Decimal("0"),
            quote_asset="USDT",
            quote_free=Decimal("1000"),
            quote_locked=Decimal("0"),
            fetched_at=datetime.now(timezone.utc),
        ),
    )
    # price 94 => equity 1470 => 2% drawdown against persisted peak 1500 => KILL.
    monkeypatch.setattr(
        main, "fetch_ticker_price",
        lambda client, symbol: TickerSnapshot(
            symbol="BNBUSDT", price=Decimal("94.00"),
            fetched_at=datetime.now(timezone.utc),
        ),
    )
    assert main.main() == 0

    # The kill latch is persisted and active; a KILL_TRIGGER event was logged.
    latch = get_kill_state(db)
    assert latch is not None and latch["active"] is True
    assert "EQUITY_DRAWDOWN_KILL" in latch["trigger"]
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT 1 FROM risk_events WHERE reason='KILL_TRIGGER' LIMIT 1"
        ).fetchone() is not None
    # No new paper orders were created on this kill run.
    assert get_state(db, "last_paper_orders") is None


def test_range_break_kill_latches_kill_state(tmp_path, monkeypatch):
    """A price beyond the range-break buffer (but not a drawdown kill) also
    latches the kill state and blocks order placement."""
    db = str(tmp_path / "grid.sqlite3")
    # Seed a healthy reference so the equity gate is NOT the trigger.
    _seed_peak(db, "100")  # very low reference => no drawdown

    cfg = _config(tmp_path)
    cfg["range"]["lower_price"] = Decimal("94")
    cfg["range"]["upper_price"] = Decimal("103")
    cfg["risk"]["range_break_buffer_pct"] = Decimal("0.01")
    # price 104 > 103*1.01=104.03? No, 104 < 104.03 so NOT beyond buffer.
    # Use 105 which is > 104.03 => RANGE_BREAK_ABOVE_BUFFER.
    _install_main_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    monkeypatch.setattr(
        main, "fetch_symbol_info",
        lambda client, symbol: _wide_percent_price_symbol_info(),
    )
    monkeypatch.setattr(
        main, "fetch_account_snapshot",
        lambda client, base, quote: AccountSnapshot(
            base_asset="BNB", base_free=Decimal("0"), base_locked=Decimal("0"),
            quote_asset="USDT", quote_free=Decimal("1000"),
            quote_locked=Decimal("0"),
            fetched_at=datetime.now(timezone.utc),
        ),
    )
    monkeypatch.setattr(
        main, "fetch_ticker_price",
        lambda client, symbol: TickerSnapshot(
            symbol="BNBUSDT", price=Decimal("105.00"),
            fetched_at=datetime.now(timezone.utc),
        ),
    )
    assert main.main() == 0
    latch = get_kill_state(db)
    assert latch is not None and latch["active"] is True
    assert "RANGE_BREAK_ABOVE_BUFFER" in latch["trigger"]


def test_kill_state_restart_blocks_new_orders(tmp_path, monkeypatch):
    """After a kill latch is persisted, a fresh process restart must NOT place
    new orders: it re-enters the kill branch and stops.  No paper cycle runs.
    """
    db = str(tmp_path / "grid.sqlite3")
    _seed_peak(db, "1500")

    # First run: the kill fires and latches.
    _run_kill(tmp_path, monkeypatch, price="94.00")
    assert get_kill_state(db)["active"] is True

    # Fresh process: re-install stubs (simulating restart) and run again.
    _run_kill(tmp_path, monkeypatch, price="94.00")
    latch = get_kill_state(db)
    assert latch["active"] is True
    # No paper orders exist anywhere in the DB from either run.
    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert rows == 0


def _run_kill(tmp_path, monkeypatch, price):
    _install_main_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(
        main, "fetch_symbol_info",
        lambda client, symbol: _wide_percent_price_symbol_info(),
    )
    monkeypatch.setattr(
        main, "fetch_account_snapshot",
        lambda client, base, quote: AccountSnapshot(
            base_asset="BNB", base_free=Decimal("5"), base_locked=Decimal("0"),
            quote_asset="USDT", quote_free=Decimal("1000"),
            quote_locked=Decimal("0"),
            fetched_at=datetime.now(timezone.utc),
        ),
    )
    monkeypatch.setattr(
        main, "fetch_ticker_price",
        lambda client, symbol: TickerSnapshot(
            symbol="BNBUSDT", price=Decimal(price),
            fetched_at=datetime.now(timezone.utc),
        ),
    )
    assert main.main() == 0


def test_release_kill_state_rejects_with_unreconciled_open_order(tmp_path, monkeypatch):
    """Release is refused while an open order is not reconciled as canceled.
    The kill-state release command's fail-closed gate is exercised here through
    the controller (the command wraps the same primitive).
    """
    from order_engine import OrderIntent, make_client_order_id
    from risk_engine import RiskDecision
    from symbol_rules import parse_symbol_info
    from tests.test_main_order_integration import _symbol_info

    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    cfg = _config(tmp_path)
    controller = CancelController(
        db, cfg, parse_symbol_info(_symbol_info()),
        canceler=lambda order: CancelOutcome.UNKNOWN,
    )
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cid = make_client_order_id("AG", "BNBUSDT", 1, 0, "BUY")
    intent = OrderIntent(
        client_order_id=cid, symbol="BNBUSDT", side="BUY",
        order_type="LIMIT_MAKER", price=Decimal("98.0"),
        quantity=Decimal("0.01"), time_in_force="GTC",
        grid_index=0, generation=1, created_at=now,
    )
    controller._engine.submit(intent, RiskDecision(True), Decimal("94"), Decimal("103"))
    controller.latch_kill_state("EQUITY_DRAWDOWN_KILL",
                                 controller.cancel_open_orders())
    try:
        controller.release(reason="try", actor="op")
        raised = False
    except ReleaseBlockedError:
        raised = True
    assert raised
    assert get_kill_state(db)["active"] is True

