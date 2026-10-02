"""PATCH 5A + 5B — defense-in-depth hardening tests.

5A: an UNHEALTHY in-cycle recovery MUST roll back the whole cycle
    transaction (no order / reservation / accounting / lifecycle / fill /
    cycle-record mutation survives).  Recovery failures are never swallowed.

5B: an UNRESOLVABLE open-order capacity limit MUST fail closed
    (OPEN_ORDER_CAPACITY_UNRESOLVED) — never interpreted as unlimited.

Reuses the deterministic fixtures / helpers from test_paper_orchestrator.
No timing race: the 5A injection shadows the engine's reconcile() with a
deterministic unhealthy RecoveryResult; the 5B cases drive the gate through
explicit config/rules.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from paper_accounting import PaperAccountingEngine
from paper_orchestrator import PaperCycleInput, PaperSession
from recovery import RecoveryError, RecoveryErrorCode, RecoveryResult

from tests.test_paper_orchestrator import (
    FIXED_RULES,
    _seed_open_order,
    _state_after_rollback_is_clean,
    happy_cfg,
    make_input,
    make_session,
)

D = Decimal


# ---------------------------------------------------------------------------
# 5A helpers
# ---------------------------------------------------------------------------

def _unhealthy_result() -> RecoveryResult:
    """A deterministic unhealthy recovery result (no timing race)."""
    return RecoveryResult(
        healthy=False,
        errors=(RecoveryError(
            code=RecoveryErrorCode.RESERVATION_EXCEEDS_ORDER,
            entity="injected",
            detail="PATCH 5A deterministic in-cycle recovery failure",
        ),),
        warnings=(),
        recovered_orders=1,
        recovered_reservations=1,
        recovered_fills=0,
        account_state_valid=False,
    )


class _ReconcileGuard:
    """Deterministically shadow engine.reconcile() and restore on exit.

    Only explicit reconcile() calls are shadowed; _ensure_healthy reads the
    cached result and is unaffected, so submissions still proceed.  On exit
    the original bound method is restored and the cached recovery result is
    refreshed to the healthy post-rollback state so a subsequent retry is not
    gated by a stale unhealthy cache.
    """

    def __init__(self, session, active: bool = True):
        self._session = session
        self._active = active
        self._orig = None
        self._install()

    def _install(self) -> None:
        engine = self._session.order_engine
        self._orig = engine.reconcile
        if self._active:
            engine.reconcile = lambda con=None: _unhealthy_result()

    def __enter__(self) -> "_ReconcileGuard":
        return self

    def __exit__(self, *exc) -> bool:
        engine = self._session.order_engine
        engine.reconcile = self._orig
        # Refresh the cached recovery result to the healthy post-rollback
        # committed state (there are no committed orders after rollback).
        engine.reconcile()
        return False


# ---------------------------------------------------------------------------
# 5A — recovery unhealthy must roll back the cycle transaction
# ---------------------------------------------------------------------------

def test_5a_recovery_unhealthy_after_single_submission_rolls_back(
    tmp_path,
):
    """1. Unhealthy recovery after order submission → entire cycle rolls back."""
    session = make_session(tmp_path)
    with _ReconcileGuard(session, active=True):
        result = session.run_cycle(make_input(1, Decimal("110")))

    # The cycle deterministically failed and was rolled back.
    assert result.success is False
    assert result.orders_submitted == 0
    assert result.error is not None
    assert result.error.startswith("CYCLE_ROLLED_BACK")
    # No partial mutation survived in either database.
    _state_after_rollback_is_clean(session)
    # Post-rollback recovery reports a healthy pre-cycle state.
    assert session.is_healthy() is True


def test_5a_recovery_unhealthy_after_multiple_orders_zero_remain(tmp_path):
    """2. Unhealthy recovery after multiple orders → zero orders remain."""
    session = make_session(tmp_path)
    with _ReconcileGuard(session, active=True):
        result = session.run_cycle(make_input(1, Decimal("110")))

    assert result.success is False
    from storage import connect
    con = connect(session.order_engine.db_path)
    try:
        remaining = con.execute("SELECT count(*) FROM orders").fetchone()[0]
        reservations = con.execute(
            "SELECT count(*) FROM paper_reservations"
        ).fetchone()[0]
    finally:
        con.close()
    assert remaining == 0
    assert reservations == 0


def test_5a_recovery_unhealthy_rolls_back_accounting(tmp_path):
    """3. Unhealthy recovery after accounting mutation → accounting rolls back."""
    session = make_session(tmp_path)
    with _ReconcileGuard(session, active=True):
        result = session.run_cycle(make_input(1, Decimal("110")))

    assert result.success is False
    # Accounting-event log (cycle-owned mutation) must be empty after rollback.
    from storage import connect
    con = connect(session.order_engine.db_path)
    try:
        events = con.execute(
            "SELECT count(*) FROM paper_accounting_events"
        ).fetchone()[0]
    finally:
        con.close()
    assert events == 0


def test_5a_recovery_unhealthy_rolls_back_lifecycle(tmp_path):
    """4. Unhealthy recovery after lifecycle mutation → lifecycle rolls back."""
    session = make_session(tmp_path)
    with _ReconcileGuard(session, active=True):
        result = session.run_cycle(make_input(1, Decimal("110")))

    assert result.success is False
    # The in-cycle plan activation is rolled back: no lifecycle mutation rows.
    _state_after_rollback_is_clean(session)


def test_5a_result_indicates_failure_deterministically(tmp_path):
    """5. The rolled-back cycle result is deterministic (failure + rollback)."""
    session = make_session(tmp_path)
    with _ReconcileGuard(session, active=True):
        result = session.run_cycle(make_input(1, Decimal("110")))

    assert result.success is False
    assert result.orders_submitted == 0
    assert result.error is not None
    assert result.error.startswith("CYCLE_ROLLED_BACK")
    # The recovery diagnostic is surfaced inside the deterministic error text.
    assert "reconciliation failed" in result.error


def test_5a_retry_after_rollback_executes_cleanly(tmp_path):
    """6. Retry after rollback: no stale partial state, recovery healthy,
    cycle can execute normally."""
    session = make_session(tmp_path)

    # First attempt: force unhealthy recovery → roll back.
    with _ReconcileGuard(session, active=True):
        r1 = session.run_cycle(make_input(1, Decimal("110")))
    assert r1.success is False
    _state_after_rollback_is_clean(session)

    # Retry the same logical cycle with the real (healthy) reconcile restored.
    r2 = session.run_cycle(make_input(1, Decimal("110")))

    # No stale partial state, recovery healthy, and the cycle now commits.
    assert r2.success is True
    assert r2.orders_submitted > 0
    assert session.is_healthy() is True


def test_5a_recovery_failure_remains_fail_closed(tmp_path):
    """7. Existing fail-closed behavior preserved: an unhealthy engine never
    commits an order (independent of the in-cycle recovery raise)."""
    session = make_session(tmp_path)
    with _ReconcileGuard(session, active=True):
        result = session.run_cycle(make_input(1, Decimal("110")))

    # No order was ever committed despite the failed cycle.
    assert result.orders_submitted == 0
    assert result.success is False
    from storage import connect
    con = connect(session.order_engine.db_path)
    try:
        rows = con.execute("SELECT count(*) FROM orders").fetchone()[0]
    finally:
        con.close()
    assert rows == 0


# ---------------------------------------------------------------------------
# 5B — open-order capacity must fail closed when unresolvable
# ---------------------------------------------------------------------------

def _zero_cap_input() -> PaperCycleInput:
    """An input whose capacity limit is UNRESOLVABLE: rules.max_num_orders=0
    and no configured max_open_orders."""
    zero_rules = replace(FIXED_RULES, max_num_orders=0)
    return replace(make_input(1, Decimal("110")), rules=zero_rules)


def test_5b_rules_none_missing_max_blocked(tmp_path):
    """1. rules=None + missing max_open_orders → allocation blocks first,
    and any surviving intent path would still hit the capacity fail-closed."""
    session = make_session(tmp_path)
    cycle_input = replace(make_input(1, Decimal("110")), rules=None)
    result = session.run_cycle(cycle_input)

    assert result.success is False
    assert result.orders_submitted == 0
    assert result.blocked_reason is not None


def test_5b_rules_none_max_zero_blocked(tmp_path):
    """2. rules=None + max_open_orders=0 → zero submission (fail-closed).

    With rules=None the allocation stage fails closed (NO_RULES) before the
    capacity gate; max_open_orders=0 additionally drives the planner to a
    zero-cell grid.  Either way the invariant is: ZERO executable orders.
    (``success`` only tracks allocation/capacity/lifecycle blocks, so a
    grid-level block can report success=True — we assert the submission
    invariant, not the success flag.)
    """
    session = make_session(tmp_path)
    cfg = happy_cfg()
    cfg["execution"]["max_open_orders"] = 0
    cycle_input = replace(make_input(1, Decimal("110")), rules=None, cfg=cfg)
    result = session.run_cycle(cycle_input)

    assert result.orders_submitted == 0
    assert result.blocked_reason is not None
    from storage import connect
    con = connect(session.order_engine.db_path)
    try:
        rows = con.execute("SELECT count(*) FROM orders").fetchone()[0]
    finally:
        con.close()
    assert rows == 0


def test_5b_unresolvable_limit_fails_closed(tmp_path):
    """5B core: a cycle that generates intents but has an unresolvable limit
    (rules.max_num_orders=0, no cfg max_open_orders) is BLOCKED, not unlimited."""
    session = make_session(tmp_path)
    cfg = happy_cfg()  # no max_open_orders key
    assert "max_open_orders" not in cfg.get("execution", {})
    result = session.run_cycle(_zero_cap_input())

    # The gate fired: intents were generated but capacity was unresolvable.
    assert result.success is False
    assert result.orders_submitted == 0
    assert result.blocked_reason == "OPEN_ORDER_CAPACITY_UNRESOLVED"
    cap_events = [e for e in result.events
                  if e.event_type == "OPEN_ORDER_CAPACITY_BLOCKED"]
    assert len(cap_events) == 1
    assert cap_events[0].payload["status"] == "OPEN_ORDER_CAPACITY_UNRESOLVED"
    assert cap_events[0].payload["max_open_orders"] is None


def test_5b_rules_present_valid_max_unchanged(tmp_path):
    """3. rules present + valid max_open_orders → normal behavior unchanged."""
    session = make_session(tmp_path)
    cfg = happy_cfg()
    cfg["execution"]["max_open_orders"] = 400  # generous → not the limit
    cycle_input = make_input(1, Decimal("110"), cfg=cfg)
    result = session.run_cycle(cycle_input)

    assert result.success is True
    assert result.orders_submitted > 0
    assert result.blocked_reason is None
    # limit resolves to min(400, 199) = 199.
    assert session.orchestrator._compute_open_order_capacity_limit(cycle_input) == 199


def test_5b_min_of_config_and_exchange_limit(tmp_path):
    """4. rules present + exchange max_num_orders → min(config, exchange)."""
    session = make_session(tmp_path)

    cfg = happy_cfg()
    cfg["execution"]["max_open_orders"] = 50
    cycle_input = make_input(1, Decimal("110"), cfg=cfg)
    # exchange (rules) = 199, config = 50 → limit is 50.
    assert session.orchestrator._compute_open_order_capacity_limit(cycle_input) == 50

    # Now tighten the exchange limit below config.
    strict_rules = replace(FIXED_RULES, max_num_orders=30)
    cycle_input2 = replace(cycle_input, rules=strict_rules)
    assert session.orchestrator._compute_open_order_capacity_limit(cycle_input2) == 30


def test_5b_existing_at_capacity_blocked(tmp_path):
    """5. existing orders exactly at capacity → blocked."""
    order_db = str(tmp_path / "orders.db")
    lifecycle_db = str(tmp_path / "lifecycle.db")
    accounting = PaperAccountingEngine(
        "BTC", "USDT", D("2.0"), D("10000.0"),
        D("0.001"), D("0.001"), "USDT",
    )
    session = PaperSession(order_db, lifecycle_db, accounting, "AG")
    cfg = happy_cfg()
    cfg["execution"]["max_open_orders"] = 1
    _seed_open_order(order_db, "SEED_AT_CAP", "BTCUSDT", "BUY", 99, "105", "0.001")
    cycle_input = make_input(1, Decimal("110"), cfg=cfg)

    result = session.run_cycle(cycle_input)

    # existing(1) + proposed(N>=1) > max(1) → blocked.
    assert result.success is False
    assert result.orders_submitted == 0
    assert result.blocked_reason is not None
    assert result.blocked_reason.startswith("OPEN_ORDER_CAPACITY_BLOCKED:")


def test_5b_existing_plus_proposed_exceeds_blocked(tmp_path):
    """6. existing + proposed exceeds capacity → blocked; existing untouched."""
    order_db = str(tmp_path / "orders.db")
    lifecycle_db = str(tmp_path / "lifecycle.db")
    accounting = PaperAccountingEngine(
        "BTC", "USDT", D("2.0"), D("10000.0"),
        D("0.001"), D("0.001"), "USDT",
    )
    session = PaperSession(order_db, lifecycle_db, accounting, "AG")
    cfg = happy_cfg()
    cfg["execution"]["max_open_orders"] = 2
    # Seed two NON-FILLING open orders: at current price 110 a BUY@105 never
    # fills, so the fill path cannot act on them (a SELL@105 would fill and
    # hit the seeded order's missing reservation → roll back the cycle).
    _seed_open_order(order_db, "SEED_A", "BTCUSDT", "BUY", 99, "105", "0.001")
    _seed_open_order(order_db, "SEED_B", "BTCUSDT", "BUY", 98, "105", "0.001")
    cycle_input = make_input(1, Decimal("110"), cfg=cfg)

    result = session.run_cycle(cycle_input)

    # 2 existing + any proposed > 2 → blocked.
    assert result.success is False
    assert result.orders_submitted == 0
    assert result.blocked_reason.startswith("OPEN_ORDER_CAPACITY_BLOCKED:")
    # Existing seeded orders are untouched.
    from storage import connect
    con = connect(order_db)
    try:
        rows = con.execute(
            "SELECT client_order_id FROM orders "
            "WHERE client_order_id IN ('SEED_A','SEED_B')"
        ).fetchall()
    finally:
        con.close()
    assert {r[0] for r in rows} == {"SEED_A", "SEED_B"}


def test_5b_capacity_failure_zero_submission(tmp_path):
    """7. Any capacity failure (blocked or unresolvable) → zero submission."""
    session = make_session(tmp_path)

    # Unresolvable-limit variant.
    r1 = session.run_cycle(_zero_cap_input())
    assert r1.orders_submitted == 0 and r1.success is False

    # Exceeded-limit variant (explicit small configured cap).
    cfg = happy_cfg()
    cfg["execution"]["max_open_orders"] = 1
    r2 = session.run_cycle(make_input(1, Decimal("110"), cfg=cfg))
    assert r2.orders_submitted == 0 and r2.success is False


def test_5b_retry_after_resolvable_capacity_executes(tmp_path):
    """8. unresolvable capacity blocks; resolvable capacity executes normally.

    A capacity-BLOCKED cycle still persists its lifecycle mutation
    (capacity block is a clean commit, not a rollback), so the two scenarios
    are proven on INDEPENDENT sessions (fresh lifecycle state each) to keep
    the lifecycle deterministic.  Invariant: unresolvable → blocked with zero
    submissions; resolvable → cycle commits with orders.
    """
    # Unresolvable-limit scenario → blocked, zero submissions.
    (tmp_path / "blocked").mkdir(parents=True, exist_ok=True)
    (tmp_path / "resolvable").mkdir(parents=True, exist_ok=True)
    blocked_session = make_session(tmp_path / "blocked")
    blocked = blocked_session.run_cycle(_zero_cap_input())
    assert blocked.success is False
    assert blocked.blocked_reason == "OPEN_ORDER_CAPACITY_UNRESOLVED"
    assert blocked.orders_submitted == 0

    # Resolvable-capacity scenario → executes normally.
    ok_session = make_session(tmp_path / "resolvable")
    cfg = happy_cfg()
    cfg["execution"]["max_open_orders"] = 199
    ok = ok_session.run_cycle(make_input(1, Decimal("110"), cfg=cfg))
    assert ok.success is True
    assert ok.orders_submitted > 0
    assert ok.blocked_reason is None
