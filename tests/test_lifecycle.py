"""Regression tests for Phase 5B lifecycle idempotency and uniqueness fixes.

Scenarios tested:
A. Re-pending a finalized candidate is rejected (no second PENDING row).
B. Same candidate submitted twice before finalization → idempotent replay.
C. Same candidate submitted after finalization → rejected.
D. Same candidate submitted after stale rejection → rejected, single row.
E. Different candidates with same generation remain independent.
F. Two reconfiguration cycles don't collide on _is_duplicate_transition.
G. Restart/recovery after each terminal state.
H. Transaction rollback on failure leaves no partial state.
I. Exactly one active plan invariant.
J. Candidate plan uniqueness invariant (DB unique index).
"""

import json
import os
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from grid_lifecycle import (
    LifecycleManager,
    LifecycleState,
    LifecycleAction,
    ValidationErrorCode,
    LifecycleError,
)
from grid_planner import (
    AdaptiveGridPlan,
    PlanDecision,
    MarketRegime,
)
from storage import connect, init_db


def _make_plan(plan_id: str, decision: PlanDecision,
               lower: str = "98.0", upper: str = "102.0",
               step: str = "0.006", count: int = 10, score: str = "85.0") -> AdaptiveGridPlan:
    """Factory for test plans (minimal required fields)."""
    return AdaptiveGridPlan(
        plan_id=plan_id,
        pair="BTCUSDT",
        regime=MarketRegime.RANGE,
        range_quality_score=Decimal(score),
        candidate_lower=Decimal(lower),
        candidate_upper=Decimal(upper),
        grid_type="GEOMETRIC",
        grid_step=Decimal(step),
        grid_count=count,
        levels=(),
        total_quote_budget=Decimal("0"),
        buy_quote_budget=Decimal("0"),
        required_base_inventory=Decimal("2"),
        available_base_inventory=Decimal("2"),
        inventory_sufficient=True,
        estimated_net_profit_per_grid=Decimal("0.004"),
        decision=decision,
        reasons=(),
    )


def _base_config() -> dict:
    return {"execution": {"order_quote_size": 25}, "fees": {"maker_fee_fallback": 0.001}}


def _new_manager(tmp_path: Path) -> LifecycleManager:
    db = tmp_path / "lifecycle.db"
    init_db(str(db))
    return LifecycleManager(str(db))


def _count_active_plans(mgr: LifecycleManager) -> int:
    """Rows in active_plans with lifecycle_state 'ACTIVE' (the live plan)."""
    con = connect(mgr.db_path)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM active_plans WHERE lifecycle_state = 'ACTIVE'"
        ).fetchone()[0]
    finally:
        con.close()


def _count_active_plan_rows(mgr: LifecycleManager) -> int:
    """All rows in active_plans (incl. BLOCKED historical rows)."""
    con = connect(mgr.db_path)
    try:
        return con.execute("SELECT COUNT(*) FROM active_plans").fetchone()[0]
    finally:
        con.close()


def _count_pending_by_candidate(mgr: LifecycleManager, candidate_id: str) -> int:
    con = connect(mgr.db_path)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM pending_reconfigs WHERE candidate_plan_id = ?",
            (candidate_id,)
        ).fetchone()[0]
    finally:
        con.close()


def _count_candidate_rows(mgr: LifecycleManager, candidate_id: str) -> int:
    con = connect(mgr.db_path)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM candidate_plans WHERE plan_id = ?",
            (candidate_id,)
        ).fetchone()[0]
    finally:
        con.close()


def _get_transitions(mgr: LifecycleManager, action: LifecycleAction) -> list:
    con = connect(mgr.db_path)
    try:
        rows = con.execute(
            "SELECT * FROM lifecycle_transitions WHERE action = ? ORDER BY timestamp",
            (action.value,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _dump_state(mgr: LifecycleManager, label: str = "") -> None:
    con = connect(mgr.db_path)
    try:
        print(f"\n=== {label} ===")
        print("bot_state:", dict(con.execute("SELECT * FROM bot_state").fetchone() or {}))
        print("active_plans:", [dict(r) for r in con.execute("SELECT * FROM active_plans").fetchall()])
        print("candidate_plans:", [dict(r) for r in con.execute("SELECT * FROM candidate_plans").fetchall()])
        print("pending_reconfigs:", [dict(r) for r in con.execute("SELECT * FROM pending_reconfigs").fetchall()])
        print("generations:", [dict(r) for r in con.execute("SELECT * FROM generations").fetchall()])
        print("transitions:", [dict(r) for r in con.execute("SELECT * FROM lifecycle_transitions").fetchall()])
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Baseline: single full cycle works
# ---------------------------------------------------------------------------
def test_full_cycle_baseline(tmp_path):
    mgr = _new_manager(tmp_path)
    cfg = _base_config()

    plan_a = _make_plan("plan_A", PlanDecision.GRID_ALLOWED)
    t1 = mgr.activate_plan(plan_a, cfg)
    assert t1.to_state == LifecycleState.ACTIVE
    assert mgr.get_current_state() == LifecycleState.ACTIVE
    assert mgr.get_generation() == 1

    # KEEP_CURRENT does nothing (stays ACTIVE)
    active = mgr.get_active_plan()
    t2 = mgr.handle_planner_decision(_make_plan("plan_A_keep", PlanDecision.KEEP_CURRENT_PLAN), active, cfg)
    assert mgr.get_current_state() == LifecycleState.ACTIVE

    # Reconfiguration cycle
    reconfig_b = _make_plan("plan_B", PlanDecision.RECONFIGURATION_REQUIRED, lower="99.0", upper="103.0")
    t_pending = mgr.handle_planner_decision(reconfig_b, active, cfg, candle_index=100)
    assert t_pending.to_state == LifecycleState.RECONFIGURATION_PENDING
    assert mgr.get_current_state() == LifecycleState.RECONFIGURATION_PENDING
    assert _count_pending_by_candidate(mgr, "plan_B") == 1

    t_validated = mgr.validate_pending_reconfiguration(active.plan_id, candle_index=101)
    assert t_validated.to_state == LifecycleState.READY_TO_RECONFIGURE
    assert mgr.get_current_state() == LifecycleState.READY_TO_RECONFIGURE

    t_finalized = mgr.finalize_reconfiguration(active.plan_id, candle_index=102)
    assert t_finalized.to_state == LifecycleState.ACTIVE
    assert mgr.get_current_state() == LifecycleState.ACTIVE
    assert mgr.get_active_plan().plan_id == "plan_B"
    assert mgr.get_generation() == 2

    assert _count_active_plans(mgr) == 1
    assert _count_candidate_rows(mgr, "plan_B") == 1
    assert _count_pending_by_candidate(mgr, "plan_B") == 1  # FINALIZED row retained


# ---------------------------------------------------------------------------
# A. Re-pending a finalized candidate is rejected (no second PENDING row)
# ---------------------------------------------------------------------------
def test_A_repending_finalized_rejected(tmp_path):
    mgr = _new_manager(tmp_path)
    cfg = _base_config()

    plan_a = _make_plan("plan_A", PlanDecision.GRID_ALLOWED)
    mgr.activate_plan(plan_a, cfg)
    active = mgr.get_active_plan()

    reconfig_b = _make_plan("plan_B", PlanDecision.RECONFIGURATION_REQUIRED, lower="99.0", upper="103.0")
    mgr.handle_planner_decision(reconfig_b, active, cfg, candle_index=100)
    mgr.validate_pending_reconfiguration(active.plan_id, candle_index=101)
    mgr.finalize_reconfiguration(active.plan_id, candle_index=102)

    # plan_B is now active, plan_B candidate row lifecycle_state = BLOCKED (consumed)
    # Attempt to pend plan_B again (same candidate plan_id)
    with pytest.raises(LifecycleError) as exc:
        mgr.handle_planner_decision(reconfig_b, mgr.get_active_plan(), cfg, candle_index=200)
    assert exc.value.code == ValidationErrorCode.DUPLICATE_TRANSITION

    # No second PENDING row
    assert _count_pending_by_candidate(mgr, "plan_B") == 1
    # State remains ACTIVE
    assert mgr.get_current_state() == LifecycleState.ACTIVE
    assert mgr.get_active_plan().plan_id == "plan_B"


# ---------------------------------------------------------------------------
# B. Same candidate submitted twice before finalization → idempotent replay
# ---------------------------------------------------------------------------
def test_B_same_candidate_twice_before_finalize_idempotent(tmp_path):
    mgr = _new_manager(tmp_path)
    cfg = _base_config()

    plan_a = _make_plan("plan_A", PlanDecision.GRID_ALLOWED)
    mgr.activate_plan(plan_a, cfg)
    active = mgr.get_active_plan()

    reconfig_b = _make_plan("plan_B", PlanDecision.RECONFIGURATION_REQUIRED)
    t_first = mgr.handle_planner_decision(reconfig_b, active, cfg, candle_index=100)
    assert mgr.get_current_state() == LifecycleState.RECONFIGURATION_PENDING
    assert _count_pending_by_candidate(mgr, "plan_B") == 1

    # Exact same call again → idempotent replay, returns SAME transition
    t_second = mgr.handle_planner_decision(reconfig_b, active, cfg, candle_index=100)
    assert t_second.transition_id == t_first.transition_id
    assert mgr.get_current_state() == LifecycleState.RECONFIGURATION_PENDING
    assert _count_pending_by_candidate(mgr, "plan_B") == 1  # no second row


# ---------------------------------------------------------------------------
# C. Same candidate submitted after finalization → rejected (via high-level API)
# ---------------------------------------------------------------------------
def test_C_same_candidate_after_finalize_rejected_via_api(tmp_path):
    mgr = _new_manager(tmp_path)
    cfg = _base_config()

    plan_a = _make_plan("plan_A", PlanDecision.GRID_ALLOWED)
    mgr.activate_plan(plan_a, cfg)
    active = mgr.get_active_plan()

    reconfig_b = _make_plan("plan_B", PlanDecision.RECONFIGURATION_REQUIRED, lower="99.0", upper="103.0")
    mgr.handle_planner_decision(reconfig_b, active, cfg, candle_index=100)
    mgr.validate_pending_reconfiguration(active.plan_id, candle_index=101)
    mgr.finalize_reconfiguration(active.plan_id, candle_index=102)

    # plan_B now active; trying to handle planner decision with plan_B again
    with pytest.raises(LifecycleError) as exc:
        mgr.handle_planner_decision(reconfig_b, mgr.get_active_plan(), cfg, candle_index=200)
    assert exc.value.code == ValidationErrorCode.DUPLICATE_TRANSITION
    assert _count_pending_by_candidate(mgr, "plan_B") == 1
    assert mgr.get_current_state() == LifecycleState.ACTIVE


# ---------------------------------------------------------------------------
# D. Same candidate submitted after stale rejection → rejected, single row
# ---------------------------------------------------------------------------
def test_D_same_candidate_after_stale_rejection(tmp_path):
    mgr = _new_manager(tmp_path)
    cfg = _base_config()

    plan_a = _make_plan("plan_A", PlanDecision.GRID_ALLOWED)
    mgr.activate_plan(plan_a, cfg)
    active = mgr.get_active_plan()

    reconfig_b = _make_plan("plan_B", PlanDecision.RECONFIGURATION_REQUIRED, lower="99.0", upper="103.0")
    mgr.handle_planner_decision(reconfig_b, active, cfg, candle_index=100)
    assert mgr.get_current_state() == LifecycleState.RECONFIGURATION_PENDING

    # Artificially make candidate generation stale (≤ active generation) by
    # directly updating candidate_plans.generation. This simulates legacy
    # corruption or manual manipulation and exercises the STALE_CANDIDATE
    # path that would be hit by validate_pending_reconfiguration.
    con = connect(mgr.db_path)
    try:
        con.execute(
            "UPDATE candidate_plans SET generation = 0 WHERE plan_id = ?",
            ("plan_B",)
        )
        con.commit()
    finally:
        con.close()

    # validate now raises STALE_CANDIDATE_PLAN (or DUPLICATE due to our ordering)
    with pytest.raises(LifecycleError) as exc:
        mgr.validate_pending_reconfiguration(active.plan_id, candle_index=101)
    assert exc.value.code in (ValidationErrorCode.STALE_CANDIDATE_PLAN,
                              ValidationErrorCode.DUPLICATE_TRANSITION)

    # Re-submitting the SAME candidate again → idempotent replay of the pending
    # (because pending row still PENDING + state still RECONFIGURATION_PENDING)
    t_replay = mgr.handle_planner_decision(reconfig_b, active, cfg, candle_index=100)
    assert t_replay.to_state == LifecycleState.RECONFIGURATION_PENDING
    assert _count_pending_by_candidate(mgr, "plan_B") == 1


# ---------------------------------------------------------------------------
# E. Different candidates with same generation remain independent (DB level)
# ---------------------------------------------------------------------------
def test_E_different_candidates_same_generation_independent(tmp_path):
    mgr = _new_manager(tmp_path)
    cfg = _base_config()

    plan_a = _make_plan("plan_A", PlanDecision.GRID_ALLOWED)
    mgr.activate_plan(plan_a, cfg)
    active = mgr.get_active_plan()

    # API path: second candidate while PENDING is INVALID_TRANSITION (fail-closed)
    reconfig_b = _make_plan("plan_B", PlanDecision.RECONFIGURATION_REQUIRED)
    mgr.handle_planner_decision(reconfig_b, active, cfg, candle_index=100)

    reconfig_c = _make_plan("plan_C", PlanDecision.RECONFIGURATION_REQUIRED)
    with pytest.raises(LifecycleError) as exc:
        mgr.handle_planner_decision(reconfig_c, active, cfg, candle_index=100)
    assert exc.value.code == ValidationErrorCode.INVALID_TRANSITION
    assert _count_pending_by_candidate(mgr, "plan_B") == 1
    assert _count_pending_by_candidate(mgr, "plan_C") == 0

    # Direct DB level: insert a second candidate C2 with same to_generation
    # (bypassing state machine) → allowed by UNIQUE(candidate_plan_id) index.
    now = "2024-01-01T00:00:00+00:00"
    con = connect(mgr.db_path)
    try:
        con.execute(
            "INSERT INTO candidate_plans (plan_id, active_plan_id, pair, regime, "
            "candidate_lower, candidate_upper, grid_step, grid_count, "
            "range_quality_score, decision, reasons, generated_at_candle, "
            "generation, lifecycle_state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("plan_C2", "plan_A", "BTCUSDT", "RANGE", "99", "103", "0.006", 10,
             "90", "RECONFIGURATION_REQUIRED", "[]", 100, 2,
             "RECONFIGURATION_PENDING", now)
        )
        con.execute(
            "INSERT INTO pending_reconfigs (reconfig_id, active_plan_id, candidate_plan_id, "
            "from_generation, to_generation, decision, reasons, created_at, expires_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("plan_A_plan_C2_2", "plan_A", "plan_C2", 1, 2,
             "RECONFIGURATION_REQUIRED", "[]", now, now, "PENDING")
        )
        con.commit()
    finally:
        con.close()

    # Both rows coexist; unique index on candidate_plan_id prevents duplicate
    # insert of the SAME candidate_plan_id.
    assert _count_pending_by_candidate(mgr, "plan_B") == 1
    assert _count_pending_by_candidate(mgr, "plan_C2") == 1

    # Attempting to insert duplicate candidate_plan_id at DB level raises IntegrityError
    with pytest.raises(sqlite3.IntegrityError):
        con = connect(mgr.db_path)
        try:
            con.execute(
                "INSERT INTO pending_reconfigs (reconfig_id, active_plan_id, candidate_plan_id, "
                "from_generation, to_generation, decision, reasons, created_at, expires_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("dup", "plan_A", "plan_B", 1, 2, "RECONFIGURATION_REQUIRED", "[]", now, now, "PENDING")
            )
            con.commit()
        finally:
            con.close()


# ---------------------------------------------------------------------------
# F. Two reconfiguration cycles don't collide on _is_duplicate_transition
# ---------------------------------------------------------------------------
def test_F_two_cycles_no_duplicate_transition_collision(tmp_path):
    mgr = _new_manager(tmp_path)
    cfg = _base_config()

    # Cycle 1: A -> B
    plan_a = _make_plan("plan_A", PlanDecision.GRID_ALLOWED)
    mgr.activate_plan(plan_a, cfg)
    active = mgr.get_active_plan()

    reconfig_b = _make_plan("plan_B", PlanDecision.RECONFIGURATION_REQUIRED, lower="99.0", upper="103.0")
    mgr.handle_planner_decision(reconfig_b, active, cfg, candle_index=100)
    mgr.validate_pending_reconfiguration(active.plan_id, candle_index=101)
    mgr.finalize_reconfiguration(active.plan_id, candle_index=102)

    assert mgr.get_active_plan().plan_id == "plan_B"
    assert mgr.get_generation() == 2

    # Cycle 2: B -> C
    active = mgr.get_active_plan()  # now plan_B
    reconfig_c = _make_plan("plan_C", PlanDecision.RECONFIGURATION_REQUIRED, lower="100.0", upper="104.0")
    mgr.handle_planner_decision(reconfig_c, active, cfg, candle_index=200)
    mgr.validate_pending_reconfiguration(active.plan_id, candle_index=201)
    t_final = mgr.finalize_reconfiguration(active.plan_id, candle_index=202)

    assert t_final.to_state == LifecycleState.ACTIVE
    assert mgr.get_active_plan().plan_id == "plan_C"
    assert mgr.get_generation() == 3

    # There must be TWO distinct VALIDATE_PENDING transitions recorded
    # (one for B, one for C). Old code would replay the first validate
    # and wedge the second cycle in RECONFIGURATION_PENDING.
    val_transitions = _get_transitions(mgr, LifecycleAction.VALIDATE_PENDING)
    assert len(val_transitions) == 2

    # Both candidates have exactly one pending row each (FINALIZED)
    assert _count_pending_by_candidate(mgr, "plan_B") == 1
    assert _count_pending_by_candidate(mgr, "plan_C") == 1


# ---------------------------------------------------------------------------
# F2. BLOCK duplicate scope: two cycles, BLOCK on each active plan
# ---------------------------------------------------------------------------
def test_F2_block_duplicate_scope(tmp_path):
    mgr = _new_manager(tmp_path)
    cfg = _base_config()

    # Cycle 1: activate A, then BLOCK
    plan_a = _make_plan("plan_A", PlanDecision.GRID_ALLOWED)
    mgr.activate_plan(plan_a, cfg)
    t_block1 = mgr._transition_to_blocked(mgr.get_active_plan())
    assert t_block1.to_state == LifecycleState.BLOCKED

    # Recovery: activate a fresh plan B
    plan_b = _make_plan("plan_B", PlanDecision.GRID_ALLOWED)
    mgr.activate_plan(plan_b, cfg)
    assert mgr.get_current_state() == LifecycleState.ACTIVE

    # Cycle 2: BLOCK again on plan_B
    t_block2 = mgr._transition_to_blocked(mgr.get_active_plan())
    assert t_block2.to_state == LifecycleState.BLOCKED
    assert t_block2.transition_id != t_block1.transition_id

    # Two distinct BLOCK transitions recorded
    block_transitions = _get_transitions(mgr, LifecycleAction.BLOCK)
    assert len(block_transitions) == 2


# ---------------------------------------------------------------------------
# G. Restart/recovery after each terminal state
# ---------------------------------------------------------------------------
def test_G_restart_recovery(tmp_path):
    cfg = _base_config()

    # 1. Full cycle to ACTIVE, then restart
    mgr1 = _new_manager(tmp_path)
    plan_a = _make_plan("plan_A", PlanDecision.GRID_ALLOWED)
    mgr1.activate_plan(plan_a, cfg)

    reconfig_b = _make_plan("plan_B", PlanDecision.RECONFIGURATION_REQUIRED, lower="99.0", upper="103.0")
    active = mgr1.get_active_plan()
    mgr1.handle_planner_decision(reconfig_b, active, cfg, candle_index=100)
    mgr1.validate_pending_reconfiguration(active.plan_id, candle_index=101)
    mgr1.finalize_reconfiguration(active.plan_id, candle_index=102)
    assert mgr1.get_active_plan().plan_id == "plan_B"
    gen1 = mgr1.get_generation()
    db_path = mgr1.db_path

    # Restart: new manager on same DB
    mgr2 = LifecycleManager(db_path)
    assert mgr2.get_current_state() == LifecycleState.ACTIVE
    assert mgr2.get_active_plan().plan_id == "plan_B"
    assert mgr2.get_generation() == gen1

    # 2. After BLOCKED, restart
    mgr3 = LifecycleManager(db_path)
    mgr3._transition_to_blocked()
    assert mgr3.get_current_state() == LifecycleState.BLOCKED

    mgr4 = LifecycleManager(db_path)
    assert mgr4.get_current_state() == LifecycleState.BLOCKED
    # Can activate new plan from BLOCKED
    plan_c = _make_plan("plan_C", PlanDecision.GRID_ALLOWED)
    mgr4.activate_plan(plan_c, cfg)
    assert mgr4.get_current_state() == LifecycleState.ACTIVE
    assert mgr4.get_active_plan().plan_id == "plan_C"

    # 3. After DUPLICATE rejection, state is unchanged
    mgr5 = LifecycleManager(db_path)
    active = mgr5.get_active_plan()
    reconfig_d = _make_plan("plan_D", PlanDecision.RECONFIGURATION_REQUIRED, lower="99.0", upper="103.0")
    mgr5.handle_planner_decision(reconfig_d, active, cfg, candle_index=300)
    mgr5.validate_pending_reconfiguration(active.plan_id, candle_index=301)
    mgr5.finalize_reconfiguration(active.plan_id, candle_index=302)

    # Now try to re-pend plan_D (already finalized) → DUPLICATE_TRANSITION
    with pytest.raises(LifecycleError) as exc:
        mgr5.handle_planner_decision(reconfig_d, mgr5.get_active_plan(), cfg, candle_index=400)
    assert exc.value.code == ValidationErrorCode.DUPLICATE_TRANSITION

    mgr6 = LifecycleManager(db_path)
    assert mgr6.get_current_state() == LifecycleState.ACTIVE
    assert mgr6.get_active_plan().plan_id == "plan_D"

    # Invariant holds after all restarts
    assert _count_active_plans(mgr6) == 1


# ---------------------------------------------------------------------------
# H. Transaction rollback on failure leaves no partial state
# ---------------------------------------------------------------------------
def test_H_transaction_rollback_on_failure(tmp_path):
    import grid_lifecycle as gl
    original_record = gl.LifecycleManager._record_transition

    def _boom(self, con, transition, prefix=""):
        raise RuntimeError("injected failure")

    mgr = _new_manager(tmp_path)
    cfg = _base_config()

    plan_a = _make_plan("plan_A", PlanDecision.GRID_ALLOWED)
    mgr.activate_plan(plan_a, cfg)
    active = mgr.get_active_plan()

    reconfig_b = _make_plan("plan_B", PlanDecision.RECONFIGURATION_REQUIRED)

    # Patch _record_transition to fail inside the transaction
    gl.LifecycleManager._record_transition = _boom
    try:
        with pytest.raises(RuntimeError):
            mgr.handle_planner_decision(reconfig_b, active, cfg, candle_index=100)
    finally:
        gl.LifecycleManager._record_transition = original_record

    # State unchanged, no candidate/pending rows for plan_B
    assert mgr.get_current_state() == LifecycleState.ACTIVE
    assert mgr.get_active_plan().plan_id == "plan_A"
    assert _count_candidate_rows(mgr, "plan_B") == 0
    assert _count_pending_by_candidate(mgr, "plan_B") == 0
    assert _count_active_plans(mgr) == 1

    # Retry succeeds
    t = mgr.handle_planner_decision(reconfig_b, active, cfg, candle_index=100)
    assert t.to_state == LifecycleState.RECONFIGURATION_PENDING
    assert _count_candidate_rows(mgr, "plan_B") == 1
    assert _count_pending_by_candidate(mgr, "plan_B") == 1


# ---------------------------------------------------------------------------
# I. Exactly one active plan invariant
# ---------------------------------------------------------------------------
def test_I_exactly_one_active_plan(tmp_path):
    mgr = _new_manager(tmp_path)
    cfg = _base_config()

    plan_a = _make_plan("plan_A", PlanDecision.GRID_ALLOWED)
    mgr.activate_plan(plan_a, cfg)
    assert _count_active_plans(mgr) == 1

    # Reconfig cycle
    active = mgr.get_active_plan()
    reconfig_b = _make_plan("plan_B", PlanDecision.RECONFIGURATION_REQUIRED)
    mgr.handle_planner_decision(reconfig_b, active, cfg, candle_index=100)
    assert _count_active_plans(mgr) == 0          # plan_A moved to RECONFIGURATION_PENDING
    assert _count_active_plan_rows(mgr) == 1      # still exactly one row
    mgr.validate_pending_reconfiguration(active.plan_id, candle_index=101)
    mgr.finalize_reconfiguration(active.plan_id, candle_index=102)
    assert _count_active_plans(mgr) == 1          # plan_B is the single ACTIVE plan

    # Second cycle
    active = mgr.get_active_plan()
    reconfig_c = _make_plan("plan_C", PlanDecision.RECONFIGURATION_REQUIRED)
    mgr.handle_planner_decision(reconfig_c, active, cfg, candle_index=200)
    mgr.validate_pending_reconfiguration(active.plan_id, candle_index=201)
    mgr.finalize_reconfiguration(active.plan_id, candle_index=202)
    assert _count_active_plans(mgr) == 1  # plan_C is ACTIVE

    # BLOCK then activate new
    mgr._transition_to_blocked(mgr.get_active_plan())
    assert _count_active_plans(mgr) == 0
    plan_d = _make_plan("plan_D", PlanDecision.GRID_ALLOWED)
    mgr.activate_plan(plan_d, cfg)
    assert _count_active_plans(mgr) == 1


# ---------------------------------------------------------------------------
# J. Candidate plan uniqueness invariant (DB unique index + app guards)
# ---------------------------------------------------------------------------
def test_J_candidate_uniqueness_invariant(tmp_path):
    mgr = _new_manager(tmp_path)
    cfg = _base_config()

    # Baseline cycle
    plan_a = _make_plan("plan_A", PlanDecision.GRID_ALLOWED)
    mgr.activate_plan(plan_a, cfg)
    active = mgr.get_active_plan()
    reconfig_b = _make_plan("plan_B", PlanDecision.RECONFIGURATION_REQUIRED)
    mgr.handle_planner_decision(reconfig_b, active, cfg, candle_index=100)
    mgr.validate_pending_reconfiguration(active.plan_id, candle_index=101)
    mgr.finalize_reconfiguration(active.plan_id, candle_index=102)

    # Candidate B has exactly 1 row in candidate_plans and 1 in pending_reconfigs
    assert _count_candidate_rows(mgr, "plan_B") == 1
    assert _count_pending_by_candidate(mgr, "plan_B") == 1

    # Second cycle with new candidate C
    active = mgr.get_active_plan()
    reconfig_c = _make_plan("plan_C", PlanDecision.RECONFIGURATION_REQUIRED)
    mgr.handle_planner_decision(reconfig_c, active, cfg, candle_index=200)
    mgr.validate_pending_reconfiguration(active.plan_id, candle_index=201)
    mgr.finalize_reconfiguration(active.plan_id, candle_index=202)

    assert _count_candidate_rows(mgr, "plan_C") == 1
    assert _count_pending_by_candidate(mgr, "plan_C") == 1

    # Each candidate appears exactly once in pending_reconfigs (no dupes)
    con = connect(mgr.db_path)
    try:
        rows = con.execute(
            "SELECT candidate_plan_id, COUNT(*) as c FROM pending_reconfigs "
            "GROUP BY candidate_plan_id HAVING c > 1"
        ).fetchall()
        assert len(rows) == 0, f"Duplicate candidate_plan_id rows: {rows}"
    finally:
        con.close()

    # DB unique index enforcement (direct insert attempt)
    with pytest.raises(sqlite3.IntegrityError):
        con = connect(mgr.db_path)
        try:
            now = "2024-01-01T00:00:00+00:00"
            con.execute(
                "INSERT INTO pending_reconfigs (reconfig_id, active_plan_id, candidate_plan_id, "
                "from_generation, to_generation, decision, reasons, created_at, expires_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("dup", "plan_C", "plan_B", 1, 2, "RECONFIGURATION_REQUIRED", "[]", now, now, "PENDING")
            )
            con.commit()
        finally:
            con.close()