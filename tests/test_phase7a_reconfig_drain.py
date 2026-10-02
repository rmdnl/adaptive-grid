"""PHASE 7A — Anomaly A1: pending-reconfiguration drain regression tests.

Root cause (A1): when the planner emits RECONFIGURATION_REQUIRED with an
active plan, the lifecycle enters RECONFIGURATION_PENDING and stores a
pending candidate.  No production caller ever invoked
validate_pending_reconfiguration()/finalize_reconfiguration(), so the
pending reconfiguration was never completed and every subsequent cycle
hit ``LIFECYCLE_BLOCKED:INVALID_TRANSITION`` (a permanent liveness
wedge).

The PHASE 7A fix wires a production drain at the top of Step 5 of
``_run_cycle_txn``: while the lifecycle state is PENDING/READY the
orchestrator completes the existing lifecycle chain
PENDING -> READY -> ACTIVE atomically inside the cycle transaction,
then re-derives the planner decision against the freshly-active plan.

These tests reuse the deterministic fixtures from
test_paper_orchestrator.  No timing races: reconfiguration state is
driven through the authoritative LifecycleManager API; spies on the
drain-completion API are installed to prove the exact production call
path.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List

from grid_lifecycle import LifecycleState as GridLifecycleState
from grid_planner import ActivePlan
from market_regime import MarketRegime

from tests.test_paper_orchestrator import (
    _make_reconfig_plan,
    happy_cfg,
    make_input,
    make_session,
)

D = Decimal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reconfig_cfg() -> Dict[str, Any]:
    """The config that produced the pending candidate (step 0.05 beyond
    the hysteresis threshold, cooldown disabled).  A post-drain re-derive
    against this cfg keeps the just-finalized plan stable (KEEP)."""
    cfg = happy_cfg()
    cfg["grid_step_pct"] = 0.05
    cfg.setdefault("adaptive_planner", {})["cooldown_candles"] = 0
    return cfg


def _drive_to_pending(session) -> str:
    """Establish a valid active plan, then drive the lifecycle to
    RECONFIGURATION_PENDING with a valid candidate.  Returns the active
    plan id."""
    session.run_cycle(make_input(1, D("110")))
    active_plan_id = session.orchestrator.lifecycle_manager.get_active_plan().plan_id
    active_state = session.orchestrator.lifecycle_manager.get_active_plan()
    plan = _make_reconfig_plan(session)
    session.orchestrator.lifecycle_manager.handle_planner_decision(
        plan, active_state, happy_cfg(), candle_index=2,
    )
    return active_plan_id


def _lifecycle_active_plan(session) -> ActivePlan:
    """Mirror main.py's FIX 4C reader: build the planner's ActivePlan
    from the authoritative lifecycle manager (authoritative generation)."""
    lm = session.orchestrator.lifecycle_manager
    active = lm.get_active_plan()
    if active is None:
        return None  # type: ignore[return-value]
    return ActivePlan(
        plan_id=active.plan_id,
        candidate_lower=active.candidate_lower,
        candidate_upper=active.candidate_upper,
        grid_step=active.grid_step,
        grid_count=active.grid_count,
        regime=MarketRegime(active.regime),
        range_quality_score=active.range_quality_score,
        candle_index=active.candle_index,
        generation=lm.get_generation(),
    )


class _DrainSpy:
    """Records drain-completion API calls on the session's
    LifecycleManager (proves the exact production call path)."""

    def __init__(self, session) -> None:
        self.lm = session.orchestrator.lifecycle_manager
        self.validate_calls: List[Any] = []
        self.finalize_calls: List[Any] = []
        self._orig_validate = self.lm.validate_pending_reconfiguration
        self._orig_finalize = self.lm.finalize_reconfiguration

        def _validate(*args, **kwargs):
            self.validate_calls.append(kwargs.get("con") is not None)
            return self._orig_validate(*args, **kwargs)

        def _finalize(*args, **kwargs):
            self.finalize_calls.append(kwargs.get("con") is not None)
            return self._orig_finalize(*args, **kwargs)

        self.lm.validate_pending_reconfiguration = _validate
        self.lm.finalize_reconfiguration = _finalize

    def restore(self) -> None:
        self.lm.validate_pending_reconfiguration = self._orig_validate
        self.lm.finalize_reconfiguration = self._orig_finalize


def _open_order_count(db_path: str) -> int:
    from storage import connect
    con = connect(db_path)
    try:
        return con.execute("SELECT count(*) FROM orders").fetchone()[0]
    finally:
        con.close()


def _corrupt_candidate_to_stale(session, active_plan_id: str) -> None:
    """Mirror test_2c_E: make the pending candidate STALE (its generation
    no longer strictly greater than the active plan's)."""
    lm = session.orchestrator.lifecycle_manager
    active_gen = lm.get_active_plan().generation
    from storage import connect
    con = connect(lm.db_path)
    try:
        con.execute(
            "UPDATE candidate_plans SET generation = ? "
            "WHERE active_plan_id = ?",
            (active_gen, active_plan_id),
        )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 1-5, 7. RECONFIGURATION_REQUIRED -> PENDING -> READY -> ACTIVE,
#         validate + finalize actually executed (joined to the cycle txn)
# ---------------------------------------------------------------------------

def test_7a_drain_completes_pending_chain(tmp_path):
    """1+2+3+4+5+7. A cycle running in PENDING state drains PENDING ->
    READY -> ACTIVE; validate and finalize are executed and both join
    the cycle transaction (``con`` supplied); generation advances only
    through LifecycleManager."""
    session = make_session(tmp_path)
    _drive_to_pending(session)
    lm = session.orchestrator.lifecycle_manager
    assert lm.get_current_state() == GridLifecycleState.RECONFIGURATION_PENDING

    old_gen = lm.get_active_plan().generation
    spy = _DrainSpy(session)
    try:
        result = session.run_cycle(
            make_input(3, D("110"), cfg=_reconfig_cfg()),
        )
    finally:
        spy.restore()

    # 5. Lifecycle returned to ACTIVE.
    assert lm.get_current_state() == GridLifecycleState.ACTIVE
    # 3. validate was executed, joined to the cycle transaction.
    assert spy.validate_calls == [True]
    # 4. finalize was executed, joined to the cycle transaction.
    assert spy.finalize_calls == [True]
    # 7. Generation changed only through LifecycleManager: the finalized
    #    active plan sits at a strictly greater generation, and that
    #    generation matches the manager generation exactly.  (The plan id
    #    itself may coincide with the pre-drain active id when the planner
    #    hash of unchanged geometry is identical -- the liveness invariant
    #    is the generation/active-state swap, not a new string id.)
    new_active = lm.get_active_plan()
    assert new_active is not None
    assert new_active.generation > old_gen
    assert new_active.generation == lm.get_generation()
    # The cycle itself is a clean commit (no error, no hard veto).
    assert result.error is None
    assert not any(e.event_type == "LIFECYCLE_ERROR" for e in result.events)
    # The finalized plan was announced on the cycle record.
    assert any(e.event_type == "RECONFIGURATION_FINALIZED" for e in result.events)


# ---------------------------------------------------------------------------
# 6. Subsequent KEEP cycle does NOT produce INVALID_TRANSITION
# ---------------------------------------------------------------------------

def test_7a_subsequent_keep_cycle_no_invalid_transition(tmp_path):
    """6. After the drain completes the reconfiguration, the next KEEP
    cycle transitions cleanly (KEEP_CURRENT from ACTIVE) with no
    INVALID_TRANSITION lifecycle error and no LIFECYCLE_BLOCKED gate."""
    session = make_session(tmp_path)
    _drive_to_pending(session)
    lm = session.orchestrator.lifecycle_manager

    drained = session.run_cycle(
        make_input(3, D("110"), cfg=_reconfig_cfg()),
    )
    assert drained.error is None
    assert lm.get_current_state() == GridLifecycleState.ACTIVE

    # The KEEP cycle sources its active plan from the lifecycle DB (as
    # main.py does in production), so the planner returns KEEP and the
    # lifecycle executes a valid KEEP_CURRENT transition.
    keep = session.run_cycle(
        make_input(
            4, D("110"),
            active_plan=_lifecycle_active_plan(session),
            cfg=_reconfig_cfg(),
        ),
    )
    assert keep.error is None
    assert not any(e.event_type == "LIFECYCLE_ERROR" for e in keep.events)
    assert keep.blocked_reason is None
    assert lm.get_current_state() == GridLifecycleState.ACTIVE

    # Determinism: replaying the drained cycle is idempotent (Patch 2A).
    replay = session.run_cycle(
        make_input(3, D("110"), cfg=_reconfig_cfg()),
    )
    assert replay.error is None
    assert not any(e.event_type == "LIFECYCLE_ERROR" for e in replay.events)


# ---------------------------------------------------------------------------
# 8, 9. Invalid candidate remains blocked; cannot become active
# ---------------------------------------------------------------------------

def test_7a_invalid_pending_candidate_cannot_complete(tmp_path, monkeypatch):
    """8+9. A STALE pending candidate (generation <= active generation)
    cannot be completed by the drain: the cycle fails closed with a
    LIFECYCLE_ERROR hard veto, submits zero orders, the candidate never
    becomes active, and the lifecycle generation does not advance."""
    session = make_session(tmp_path)
    active_plan_id = _drive_to_pending(session)
    _corrupt_candidate_to_stale(session, active_plan_id)
    lm = session.orchestrator.lifecycle_manager

    gen_before = lm.get_generation()
    open_before = _open_order_count(session.order_engine.db_path)
    submitted: List[Any] = []
    original_submit = session.order_engine.submit

    def spy_submit(intent, *args, **kwargs):
        submitted.append(intent)
        return original_submit(intent, *args, **kwargs)

    monkeypatch.setattr(session.order_engine, "submit", spy_submit)

    result = session.run_cycle(make_input(3, D("110"), cfg=_reconfig_cfg()))

    # Fail closed: no submissions, no success, no partial reconfiguration.
    assert result.orders_submitted == 0
    assert not result.success
    assert submitted == []
    assert _open_order_count(session.order_engine.db_path) == open_before
    # 7. Generation did NOT advance through the invalid candidate.
    assert lm.get_generation() == gen_before
    # 9. The stale candidate did not become active.
    active_now = lm.get_active_plan()
    assert active_now is not None
    assert active_now.plan_id == active_plan_id
    # A deterministic lifecycle error was recorded for the drain stage.
    assert any(
        e.event_type == "LIFECYCLE_ERROR"
        and e.payload.get("stage") == "reconfig_drain"
        for e in result.events
    )


# ---------------------------------------------------------------------------
# 10. No orders are submitted while reconfiguration is unresolved
# ---------------------------------------------------------------------------

def test_7a_no_submissions_while_reconfig_unresolved(tmp_path, monkeypatch):
    """10. While the reconfiguration is UNRESOLVED (drain rejects a stale
    candidate), zero orders are submitted.  Conversely, when the pending
    candidate is valid the drain finalizes it and NO order is submitted
    against the unvalidated/pre-drain plan: any submission belongs to the
    post-finalize generation only."""
    session = make_session(tmp_path)
    active_plan_id = _drive_to_pending(session)
    _corrupt_candidate_to_stale(session, active_plan_id)

    submitted: List[Any] = []
    original_submit = session.order_engine.submit

    def spy_submit(intent, *args, **kwargs):
        submitted.append(intent)
        return original_submit(intent, *args, **kwargs)

    monkeypatch.setattr(session.order_engine, "submit", spy_submit)

    result = session.run_cycle(make_input(3, D("110"), cfg=_reconfig_cfg()))
    assert result.orders_submitted == 0
    assert submitted == []

    # Resolvable scenario (fresh session): the drain finalizes the
    # candidate; the just-finalized plan is stable under the reconfig cfg,
    # so the post-drain decision is KEEP and nothing new is submitted.
    (tmp_path / "resolvable").mkdir(parents=True, exist_ok=True)
    session2 = make_session(tmp_path / "resolvable")
    _drive_to_pending(session2)
    lm2 = session2.orchestrator.lifecycle_manager
    gen_pending_active = lm2.get_active_plan().generation
    original_submit2 = session2.order_engine.submit
    submitted2: List[Any] = []

    def spy_submit2(intent, *args, **kwargs):
        submitted2.append(intent)
        return original_submit2(intent, *args, **kwargs)

    monkeypatch.setattr(session2.order_engine, "submit", spy_submit2)
    result2 = session2.run_cycle(make_input(3, D("110"), cfg=_reconfig_cfg()))
    assert lm2.get_current_state() == GridLifecycleState.ACTIVE
    # No order may be submitted using the unvalidated/pre-drain plan:
    # every submitted intent belongs to the post-finalize generation.
    for intent in submitted2:
        assert intent.generation > gen_pending_active
    assert result2.orders_submitted == 0
    assert submitted2 == []


# ---------------------------------------------------------------------------
# Cross-check: drain must not fire when the state is not PENDING/READY
# ---------------------------------------------------------------------------

def test_7a_drain_is_noop_when_active(tmp_path):
    """The drain is a no-op in steady ACTIVE state: validate/finalize are
    never called and the active plan + generation are untouched."""
    session = make_session(tmp_path)
    session.run_cycle(make_input(1, D("110")))
    lm = session.orchestrator.lifecycle_manager
    assert lm.get_current_state() == GridLifecycleState.ACTIVE

    plan_id = lm.get_active_plan().plan_id
    gen = lm.get_generation()
    spy = _DrainSpy(session)
    try:
        result = session.run_cycle(
            make_input(
                2, D("110"),
                active_plan=_lifecycle_active_plan(session),
                cfg=_reconfig_cfg(),
            ),
        )
    finally:
        spy.restore()

    assert result.error is None
    assert spy.validate_calls == []
    assert spy.finalize_calls == []
    assert lm.get_active_plan().plan_id == plan_id
    assert lm.get_generation() == gen
