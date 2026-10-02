"""Roadmap G: structured logging / health-status / graceful shutdown / restart
safety regression tests.

All targets are read-only observability and safe-boundary control.  No test
here places an order or weakens a risk gate; they assert:

* ``health.classify_health`` precedence (unsafe config > kill > reconciliation
  > degraded > healthy) and the deterministic JSON payload;
* ``runstate.verify_restart_safety`` verdicts (RESUME / KILL_BRANCH /
  RECONCILE_THEN_RESUME / REFUSE) and that a fresh DB is safe to resume;
* ``ShutdownCoordinator`` idempotency, forced pacing, terminal completion,
  and the run-loop boundary check;
* ``main()`` integration: a shutdown request skips the paper cycle but the
  run still completes with a COMPLETED=false marker and a health JSONL line;
  a corrupt-with-activity DB makes ``main()`` refuse to plan;
* ``collect_health_report`` reflects kill / reference / pending-cancel state.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import main
from health import HealthStatus, classify_health, collect_health_report
from market_data import AccountSnapshot, TickerSnapshot
from runstate import (
    PHASE_COMPLETED,
    PHASE_INTERRUPTED,
    persist_run_state,
    read_run_state,
    verify_restart_safety,
)
from shutdown import ShutdownCoordinator, ShutdownPhase
from storage import init_db, set_kill_state, set_state
from tests.test_main_order_integration import _install_main_stubs, _symbol_info


def _cfg(tmp_path, dry_run=True):
    return {
        "environment": {"mode": "testnet", "dry_run": dry_run, "allow_live_execution": False},
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
            "sqlite_path": str(tmp_path / "grid.sqlite3"),
            "log_path": str(tmp_path / "grid.log"),
            "csv_path": str(tmp_path / "trades.csv"),
        },
    }


# ---------------------------------------------------------------------------
# health.classify_health precedence
# ---------------------------------------------------------------------------

def test_classify_health_precedence():
    env_safe = {"dry_run": True, "allow_live_execution": False}
    ref_valid = {"present": True, "valid": True, "value": "1000"}
    assert classify_health(env_safe, False, True, ref_valid, []) is HealthStatus.HEALTHY
    # kill dominates degraded
    assert classify_health(env_safe, True, True, ref_valid, ["o"]) is HealthStatus.KILLED
    # reconciliation failure dominates degraded (but not kill)
    assert classify_health(env_safe, False, False, ref_valid, []) is HealthStatus.UNHEALTHY
    # corrupt reference or pending cancel => degraded
    assert classify_health(
        env_safe, False, True, {"present": True, "valid": False, "value": None}, []
    ) is HealthStatus.DEGRADED
    assert classify_health(
        env_safe, False, True, ref_valid, ["o1"]
    ) is HealthStatus.DEGRADED
    # unsafe config dominates everything (even a clean kill)
    assert classify_health(
        {"dry_run": False, "allow_live_execution": False}, True, True, ref_valid, []
    ) is HealthStatus.UNSAFE_CONFIG
    assert classify_health(
        {"dry_run": True, "allow_live_execution": True}, False, True, ref_valid, []
    ) is HealthStatus.UNSAFE_CONFIG


def test_classify_exit_codes():
    assert HealthStatus.HEALTHY.exit_code() == 0
    assert HealthStatus.DEGRADED.exit_code() == 1
    assert HealthStatus.KILLED.exit_code() == 1
    assert HealthStatus.UNHEALTHY.exit_code() == 1
    assert HealthStatus.UNSAFE_CONFIG.exit_code() == 2


def test_health_report_json_is_deterministic(tmp_path):
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    cfg = _cfg(tmp_path)
    r1 = collect_health_report(db, cfg)
    r2 = collect_health_report(db, cfg)
    assert r1.as_json() == r2.as_json()
    parsed = json.loads(r1.as_json())
    # no wall-clock field leaks into the payload
    assert "wall_time" not in parsed
    assert parsed["status"] in [s.value for s in HealthStatus]


# ---------------------------------------------------------------------------
# runstate.verify_restart_safety
# ---------------------------------------------------------------------------

def test_verify_restart_safety_fresh_db_is_resume(tmp_path):
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    verdict = verify_restart_safety(db)
    assert verdict["restart_action"] == "RESUME"
    assert verdict["safe_to_continue"] is True
    assert verdict["kill_active"] is False


def test_verify_restart_safety_completed_marker_is_resume(tmp_path):
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    persist_run_state(db, run_id="r1", completed=True, risk_allowed=True,
                      kill_active=False, pending_cancels=0, open_orders=0)
    verdict = verify_restart_safety(db)
    assert verdict["restart_action"] == "RESUME"
    assert verdict["previous_run"]["phase"] == PHASE_COMPLETED


def test_verify_restart_safety_interrupted_marker_needs_reconcile(tmp_path):
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    # Plant paper activity so there is something to reconcile on restart;
    # without it an interrupted marker on an empty DB is a clean first run.
    from order_engine import OrderIntent, PaperOrderEngine, make_client_order_id
    from paper_accounting import PaperAccountingEngine
    from risk_engine import RiskDecision

    accounting = PaperAccountingEngine("BNB", "USDT",
                                       Decimal("2"), Decimal("1000"),
                                       Decimal("0.001"), Decimal("0.001"), "USDT")
    engine = PaperOrderEngine(
        db, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        accounting=accounting,
    )
    cid = make_client_order_id("AG", "BNBUSDT", 1, 0, "BUY")
    engine.submit(
        OrderIntent(
            client_order_id=cid, symbol="BNBUSDT", side="BUY",
            order_type="LIMIT_MAKER", price=Decimal("98.0"),
            quantity=Decimal("0.01"), time_in_force="GTC",
            grid_index=0, generation=1,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        RiskDecision(True), Decimal("94"), Decimal("103"),
    )
    # The previous run was cut off: an INTERRUPTED marker is persisted.
    persist_run_state(db, run_id="r1", completed=False, risk_allowed=False,
                      kill_active=False, pending_cancels=0, open_orders=1)
    verdict = verify_restart_safety(db)
    assert verdict["restart_action"] == "RECONCILE_THEN_RESUME"
    assert verdict["previous_run"]["phase"] == PHASE_INTERRUPTED
    assert verdict["safe_to_continue"] is True  # still operable after reconcile


def test_verify_restart_safety_kill_active_goes_to_kill_branch(tmp_path):
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    set_kill_state(db, active=True, trigger="EQUITY_DRAWDOWN_KILL",
                   cancel_status="PENDING_RECONCILIATION")
    persist_run_state(db, run_id="r1", completed=True, risk_allowed=False,
                      kill_active=True, pending_cancels=1, open_orders=1)
    verdict = verify_restart_safety(db)
    assert verdict["restart_action"] == "KILL_BRANCH"
    assert verdict["kill_active"] is True
    assert verdict["safe_to_continue"] is True  # the kill branch is a safe state


def test_verify_restart_safety_corrupt_activity_refuses(tmp_path):
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    # Create activity, then corrupt a critical invariant so reconciliation
    # fails for a NON-empty DB.
    from order_engine import OrderIntent, PaperOrderEngine, make_client_order_id
    from paper_accounting import PaperAccountingEngine
    from risk_engine import RiskDecision

    accounting = PaperAccountingEngine("BNB", "USDT",
                                       Decimal("2"), Decimal("1000"),
                                       Decimal("0.001"), Decimal("0.001"), "USDT")
    engine = PaperOrderEngine(
        db, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        accounting=accounting,
    )
    cid = make_client_order_id("AG", "BNBUSDT", 1, 0, "BUY")
    engine.submit(
        OrderIntent(
            client_order_id=cid, symbol="BNBUSDT", side="BUY",
            order_type="LIMIT_MAKER", price=Decimal("98.0"),
            quantity=Decimal("0.01"), time_in_force="GTC",
            grid_index=0, generation=1,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        RiskDecision(True), Decimal("94"), Decimal("103"),
    )
    # Corrupt: an OPEN order with a non-zero executed_qty is impossible.
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE orders SET executed_qty='0.5' WHERE client_order_id=?",
            (cid,),
        )
        con.commit()
    verdict = verify_restart_safety(db)
    assert verdict["restart_action"] == "REFUSE"
    assert verdict["safe_to_continue"] is False
    assert verdict["recovery_raw_healthy"] is False


# ---------------------------------------------------------------------------
# ShutdownCoordinator
# ---------------------------------------------------------------------------

def test_shutdown_coordinator_idempotent_and_forced():
    c = ShutdownCoordinator()
    assert c.phase is ShutdownPhase.IDLE
    assert c.is_requested is False
    # 1st request: IDLE -> REQUESTED.
    assert c.request(reason="stop") is True
    assert c.phase is ShutdownPhase.REQUESTED
    assert c.is_requested is True
    assert c.is_forced is False
    # 2nd request while pending: newly sets forced (a state change).
    assert c.request(reason="harder") is True
    assert c.is_forced is True
    # 3rd request is absorbed (already forced).
    assert c.request(reason="nope") is False
    # Completion is terminal; later requests are ignored.
    c.complete()
    assert c.phase is ShutdownPhase.COMPLETED
    assert c.request(reason="late") is False


def test_shutdown_coordinator_complete_is_idempotent():
    c = ShutdownCoordinator()
    c.complete()  # no pending request: transitions IDLE -> COMPLETED (no-op safe)
    c.complete()
    assert c.phase is ShutdownPhase.COMPLETED


def test_shutdown_run_loop_boundary_check():
    c = ShutdownCoordinator()
    from shutdown import run_loop_boundary_check
    assert run_loop_boundary_check(c) is False
    c.request()
    assert run_loop_boundary_check(c) is True


def test_shutdown_describe_is_safe():
    c = ShutdownCoordinator()
    assert c.describe() == "shutdown:IDLE"
    c.request(signal_name="SIGTERM")
    assert c.describe() == "shutdown:REQUESTED:SIGTERM"
    c.request()
    assert c.describe() == "shutdown:FORCED:SIGTERM"


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------

def test_main_persists_run_state_and_health_jsonl(tmp_path, monkeypatch):
    db = str(tmp_path / "grid.sqlite3")
    _install_main_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(main, "fetch_symbol_info",
                        lambda client, symbol: _symbol_info())
    monkeypatch.setattr(main, "fetch_account_snapshot",
                        lambda c, b, q: AccountSnapshot(
                            "BNB", Decimal("1"), Decimal("0"),
                            "USDT", Decimal("1000"), Decimal("0"),
                            datetime.now(timezone.utc)))
    monkeypatch.setattr(main, "fetch_ticker_price",
                        lambda c, s: TickerSnapshot(
                            "BNBUSDT", Decimal("100.01"),
                            datetime.now(timezone.utc)))
    assert main.main() == 0

    # The run persisted a COMPLETED marker.
    marker = read_run_state(db)
    assert marker is not None
    assert marker["phase"] == PHASE_COMPLETED

    # A machine-readable health JSONL line was written beside the log.
    log_path = tmp_path / "grid.log"
    jsonl = log_path.with_suffix(".jsonl")
    assert jsonl.exists()
    lines = [json.loads(line) for line in jsonl.read_text().splitlines() if line]
    assert lines
    assert lines[-1]["status"] in [s.value for s in HealthStatus]


def test_main_skips_cycle_when_shutdown_requested(tmp_path, monkeypatch):
    db = str(tmp_path / "grid.sqlite3")
    _install_main_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(main, "fetch_symbol_info",
                        lambda client, symbol: _symbol_info())
    monkeypatch.setattr(main, "fetch_account_snapshot",
                        lambda c, b, q: AccountSnapshot(
                            "BNB", Decimal("1"), Decimal("0"),
                            "USDT", Decimal("1000"), Decimal("0"),
                            datetime.now(timezone.utc)))
    monkeypatch.setattr(main, "fetch_ticker_price",
                        lambda c, s: TickerSnapshot(
                            "BNBUSDT", Decimal("100.01"),
                            datetime.now(timezone.utc)))
    # Simulate an operator asking to stop just before the paper cycle.  The
    # seam the production run-loop consults is the same coordinator type the
    # real code constructs; we drive it by forcing the module-level behavior
    # through a pre-injected coordinator.  Since main() builds its own
    # coordinator, we instead verify the *effect*: no paper orders exist when
    # the run is marked interrupted, and the marker is COMPLETED=False.
    #
    # To exercise the real branch deterministically, monkeypatch the
    # ShutdownCoordinator class used by main so its instance reports a
    # pending request only when the kill/reconciliation path has passed.
    from shutdown import ShutdownCoordinator as _Real

    class _RequestedCoordinator(_Real):
        def __init__(self):
            super().__init__()
            # The first boundary check after setup sees a pending request.
            self.request(reason="test")

    monkeypatch.setattr(main, "ShutdownCoordinator", _RequestedCoordinator)

    assert main.main() == 0
    # No orders were placed because the cycle was skipped.
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    # The marker records an interrupted run.
    marker = read_run_state(db)
    assert marker is not None
    assert marker["phase"] == PHASE_INTERRUPTED


def test_main_refuses_when_restart_reconciliation_fails(tmp_path, monkeypatch):
    """A corrupt-with-activity DB must make main() refuse to plan."""
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    # Plant activity + corruption, exactly like the runstate test above.
    from order_engine import OrderIntent, PaperOrderEngine, make_client_order_id
    from paper_accounting import PaperAccountingEngine
    from risk_engine import RiskDecision

    accounting = PaperAccountingEngine("BNB", "USDT",
                                       Decimal("2"), Decimal("1000"),
                                       Decimal("0.001"), Decimal("0.001"), "USDT")
    engine = PaperOrderEngine(
        db, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        accounting=accounting,
    )
    cid = make_client_order_id("AG", "BNBUSDT", 1, 0, "BUY")
    engine.submit(
        OrderIntent(
            client_order_id=cid, symbol="BNBUSDT", side="BUY",
            order_type="LIMIT_MAKER", price=Decimal("98.0"),
            quantity=Decimal("0.01"), time_in_force="GTC",
            grid_index=0, generation=1,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        RiskDecision(True), Decimal("94"), Decimal("103"),
    )
    with sqlite3.connect(db) as con:
        con.execute("UPDATE orders SET executed_qty='0.5' WHERE "
                    "client_order_id=?", (cid,))
        con.commit()

    _install_main_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "load_config", lambda: _cfg(tmp_path))
    # main() must stop at the restart gate and return 1, never plan.
    assert main.main() == 1
    # A risk event records the refusal.
    with sqlite3.connect(db) as con:
        row = con.execute(
            "SELECT 1 FROM risk_events WHERE reason='RESTART_RECONCILIATION_REFUSED'"
        ).fetchone()
    assert row is not None
    # No new paper cycle was run.
    assert read_run_state(db) is None


def test_kill_state_blocks_new_orders_even_when_risk_allows(tmp_path, monkeypatch):
    """While the kill latch is active, main() must not plan even on a pass
    risk gate (the absolute veto)."""
    db = str(tmp_path / "grid.sqlite3")
    _install_main_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "load_config", lambda: _cfg(tmp_path))
    # Latch the kill state so the run takes the restart-recovery branch.
    # init_db first so the kill_state table exists on the fresh file.
    init_db(db)
    set_kill_state(db, active=True, trigger="EQUITY_DRAWDOWN_KILL",
                   cancel_status="CANCELLED")
    monkeypatch.setattr(main, "fetch_symbol_info",
                        lambda client, symbol: _symbol_info())
    assert main.main() == 0
    # No orders were placed; the kill state was re-latched (still active).
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    from storage import get_kill_state
    ks = get_kill_state(db)
    assert ks is not None and ks["active"] is True


# ---------------------------------------------------------------------------
# collect_health_report reflects persisted state
# ---------------------------------------------------------------------------

def test_collect_health_report_reflects_kill_and_reference(tmp_path):
    db = str(tmp_path / "grid.sqlite3")
    init_db(db)
    cfg = _cfg(tmp_path)
    # healthy fresh state
    rep = collect_health_report(db, cfg)
    assert rep.status is HealthStatus.HEALTHY
    # a latched kill flips the report to KILLED
    set_kill_state(db, active=True, trigger="EQUITY_DRAWDOWN_KILL",
                   cancel_status="CANCELLED")
    rep2 = collect_health_report(db, cfg)
    assert rep2.status is HealthStatus.KILLED
    assert rep2.kill_state["active"] is True
    # a corrupt reference (while kill released) flips it to DEGRADED
    set_kill_state(db, active=False)
    set_state(db, "paper_reference_equity", "garbage")
    rep3 = collect_health_report(db, cfg)
    assert rep3.status is HealthStatus.DEGRADED
    assert rep3.reference_equity["valid"] is False
    # unsafe config always reports UNSAFE_CONFIG
    unsafe_cfg = dict(cfg)
    unsafe_cfg["environment"] = {"mode": "testnet", "dry_run": False,
                                 "allow_live_execution": False}
    assert collect_health_report(db, unsafe_cfg).status is HealthStatus.UNSAFE_CONFIG
