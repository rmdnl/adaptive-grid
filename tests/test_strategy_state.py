"""Per-symbol strategy state machine (locked spec section 24): unit tests.

- All ten states exist.
- Invalid transitions are REJECTED (state unchanged) and reported.
- Valid transition chains for the full strategy lifecycle.
- State persists across "restarts" (fresh tracker on the same DB).
- Cooldown and restart-recovery edges.
"""
from __future__ import annotations

import pytest

from strategy_state import (
    StrategyState as S,
    StrategyStateTracker,
)


def _tracker(tmp_path):
    db = str(tmp_path / "grid_BTCUSDT.sqlite3")
    from storage import init_db
    init_db(db)
    return StrategyStateTracker(db), db


def test_all_states_exist():
    assert {s.value for s in S} == {
        "WAITING_FOR_ENTRY", "ENTRY_SIGNAL", "DEPLOYING_GRID", "GRID_ACTIVE",
        "EXIT_SIGNAL", "AUTO_EXIT", "LIQUIDATING", "COOLDOWN", "BLOCKED",
        "ERROR"}


def test_initial_state_is_waiting_for_entry(tmp_path):
    tracker, _ = _tracker(tmp_path)
    assert tracker.current() is S.WAITING_FOR_ENTRY


def test_full_lifecycle_chain(tmp_path):
    tracker, _ = _tracker(tmp_path)
    chain = [
        S.ENTRY_SIGNAL, S.DEPLOYING_GRID, S.GRID_ACTIVE, S.EXIT_SIGNAL,
        S.AUTO_EXIT, S.LIQUIDATING, S.COOLDOWN, S.WAITING_FOR_ENTRY,
    ]
    for target in chain:
        report = tracker.transition(target, "lifecycle")
        assert report["applied"] is True, (tracker.current(), target)
        assert tracker.current() is target


def test_invalid_transition_is_rejected_and_reported(tmp_path):
    tracker, _ = _tracker(tmp_path)
    # WAITING_FOR_ENTRY -> GRID_ACTIVE skips deployment: invalid.
    report = tracker.transition(S.GRID_ACTIVE, "skip deployment")
    assert report["applied"] is False
    assert report["rejected"] is True
    assert tracker.current() is S.WAITING_FOR_ENTRY
    detail = tracker.current_detail()
    assert detail["state"] == "WAITING_FOR_ENTRY"
    assert "rejected" in detail["reason"]


def test_error_from_any_state(tmp_path):
    from storage import init_db
    for start in (S.WAITING_FOR_ENTRY, S.DEPLOYING_GRID, S.GRID_ACTIVE,
                  S.EXIT_SIGNAL, S.COOLDOWN, S.BLOCKED):
        db = str(tmp_path / f"db-{start.value}.sqlite3")
        init_db(db)
        tracker = StrategyStateTracker(db)
        tracker.transition(start, "seed")
        report = tracker.transition(S.ERROR, "boom")
        assert report["applied"] is True
        assert tracker.current() is S.ERROR


def test_state_persists_across_restart(tmp_path):
    tracker, db = _tracker(tmp_path)
    tracker.transition(S.ENTRY_SIGNAL, "signal")
    tracker.transition(S.DEPLOYING_GRID, "deploy")
    tracker.transition(S.GRID_ACTIVE, "deployed")
    # Fresh tracker = restarted process on the same database.
    restarted = StrategyStateTracker(db)
    assert restarted.current() is S.GRID_ACTIVE


def test_cooldown_recovery_paths(tmp_path):
    tracker, _ = _tracker(tmp_path)
    tracker.transition(S.COOLDOWN, "after exit")
    assert tracker.current() is S.COOLDOWN
    # Cooldown -> cooldown (still cooling) is valid.
    assert tracker.transition(S.COOLDOWN, "still cooling")["applied"] is True
    # Cooldown -> blocked (risk veto during cooldown) is valid.
    assert tracker.transition(S.BLOCKED, "risk veto")["applied"] is True


def test_recovery_out_of_error_on_restart(tmp_path):
    tracker, _ = _tracker(tmp_path)
    tracker.transition(S.ERROR, "crash")
    # Restart re-derivation: a fresh cycle may recover to a defined state.
    assert tracker.transition(S.WAITING_FOR_ENTRY, "restart recovery")["applied"] is True
    assert tracker.current() is S.WAITING_FOR_ENTRY
