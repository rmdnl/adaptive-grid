"""Phase 5B – Adaptive Grid Lifecycle & Reconfiguration Manager.

Implements a deterministic, immutable state machine for adaptive grid plans
without executing any orders.

Core guarantees:
- Deterministic transitions (no randomness, no wall-clock time as logical input)
- Atomic persistence using SQLite transactions
- Immutable active and candidate plans
- Generation/version to prevent stale-plan replacement
- Fail-closed behavior on any ambiguity
- No order placement/cancellation execution (pure lifecycle management)
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from storage import connect, get_state, init_db, set_state
from grid_planner import (
    ActivePlan,
    AdaptiveGridPlan,
    PlanBlockReason,
    PlanDecision,
)

logger = logging.getLogger("grid_lifecycle")

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class LifecycleState(str, Enum):
    """Immutable lifecycle states."""
    NO_ACTIVE_GRID = "NO_ACTIVE_GRID"
    ACTIVE = "ACTIVE"
    RECONFIGURATION_PENDING = "RECONFIGURATION_PENDING"
    READY_TO_RECONFIGURE = "READY_TO_RECONFIGURE"
    BLOCKED = "BLOCKED"

    def __str__(self) -> str:
        return self.value


class LifecycleAction(str, Enum):
    """Lifecycle transition actions."""
    ACTIVATE = "ACTIVATE"
    BLOCK = "BLOCK"
    KEEP_CURRENT = "KEEP_CURRENT"
    ENTER_PENDING = "ENTER_PENDING"
    VALIDATE_PENDING = "VALIDATE_PENDING"
    READY_FOR_RECONFIG = "READY_FOR_RECONFIG"
    # Strategy auto-exit / operator close: the active plan is closed and the
    # lifecycle returns to NO_ACTIVE_GRID (audited, idempotent).
    CLOSE = "CLOSE"


class ValidationErrorCode(str, Enum):
    """Structured validation error codes."""
    DUPLICATE_GENERATION = "DUPLICATE_GENERATION"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    STALE_CANDIDATE_PLAN = "STALE_CANDIDATE_PLAN"
    TRANSITION_REJECTED = "TRANSITION_REJECTED"
    DUPLICATE_TRANSITION = "DUPLICATE_TRANSITION"


# ---------------------------------------------------------------------------
# Valid state transitions (fail-closed: anything not listed is rejected)
# ---------------------------------------------------------------------------

_VALID_TRANSITIONS: dict[LifecycleState, dict[LifecycleAction, LifecycleState]] = {
    LifecycleState.NO_ACTIVE_GRID: {
        LifecycleAction.ACTIVATE: LifecycleState.ACTIVE,
        LifecycleAction.BLOCK: LifecycleState.BLOCKED,
    },
    LifecycleState.ACTIVE: {
        LifecycleAction.KEEP_CURRENT: LifecycleState.ACTIVE,
        LifecycleAction.ENTER_PENDING: LifecycleState.RECONFIGURATION_PENDING,
        LifecycleAction.BLOCK: LifecycleState.BLOCKED,
        LifecycleAction.CLOSE: LifecycleState.NO_ACTIVE_GRID,
    },
    LifecycleState.RECONFIGURATION_PENDING: {
        LifecycleAction.VALIDATE_PENDING: LifecycleState.READY_TO_RECONFIGURE,
        LifecycleAction.BLOCK: LifecycleState.BLOCKED,
        LifecycleAction.CLOSE: LifecycleState.NO_ACTIVE_GRID,
    },
    LifecycleState.READY_TO_RECONFIGURE: {
        LifecycleAction.ACTIVATE: LifecycleState.ACTIVE,
        LifecycleAction.BLOCK: LifecycleState.BLOCKED,
        LifecycleAction.CLOSE: LifecycleState.NO_ACTIVE_GRID,
    },
    LifecycleState.BLOCKED: {
        LifecycleAction.ACTIVATE: LifecycleState.ACTIVE,
        LifecycleAction.BLOCK: LifecycleState.BLOCKED,
    },
}

# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActivePlanState:
    """Immutable representation of the currently active grid plan."""
    plan_id: str
    pair: str
    candidate_lower: Decimal
    candidate_upper: Decimal
    grid_step: Decimal
    grid_count: int
    regime: str
    range_quality_score: Decimal
    candle_index: int
    generation: int
    lifecycle_state: LifecycleState

    @classmethod
    def from_active_plan(cls, active_plan: ActivePlan, lifecycle_state: LifecycleState) -> ActivePlanState:
        """Create ActivePlanState from ActivePlan."""
        return cls(
            plan_id=active_plan.plan_id,
            pair=active_plan.pair,
            candidate_lower=active_plan.candidate_lower,
            candidate_upper=active_plan.candidate_upper,
            grid_step=active_plan.grid_step,
            grid_count=active_plan.grid_count,
            regime=active_plan.regime.value,
            range_quality_score=active_plan.range_quality_score,
            candle_index=active_plan.candle_index,
            generation=0,
            lifecycle_state=lifecycle_state,
        )


@dataclass(frozen=True)
class CandidatePlanState:
    """Immutable representation of a pending candidate plan."""
    plan_id: str
    active_plan_id: str
    decision: PlanDecision
    reasons: Tuple[PlanBlockReason, ...]
    pair: str
    regime: str
    candidate_lower: Decimal
    candidate_upper: Decimal
    grid_step: Decimal
    grid_count: int
    range_quality_score: Decimal
    generated_at_candle: int
    generation: int
    lifecycle_state: LifecycleState

    def is_stale(self, current_generation: int) -> bool:
        """Check if candidate is stale relative to current generation."""
        return self.generation < current_generation


@dataclass(frozen=True)
class LifecycleTransition:
    """Represents a lifecycle state transition."""
    transition_id: str
    from_state: LifecycleState
    to_state: LifecycleState
    action: LifecycleAction
    timestamp: str
    details: Dict[str, Any]

    @classmethod
    def create(cls, from_state: LifecycleState, to_state: LifecycleState,
               action: LifecycleAction, details: Optional[Dict] = None) -> LifecycleTransition:
        """Create a new transition with deterministic ID."""
        ts = datetime.now(timezone.utc).isoformat()
        # Deterministic ID: hash of logical inputs
        payload = json.dumps({
            "from": from_state.value,
            "to": to_state.value,
            "action": action.value,
            "ts": ts,
        }, sort_keys=True)
        tid = hashlib.sha256(payload.encode()).hexdigest()[:20]
        return cls(
            transition_id=tid,
            from_state=from_state,
            to_state=to_state,
            action=action,
            timestamp=ts,
            details=details or {},
        )


class LifecycleManager:
    """Deterministic lifecycle state machine for adaptive grid plans."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        init_db(db_path)  # Ensure base tables (bot_state etc.) exist
        self._ensure_tables_exist()

    def _ensure_tables_exist(self) -> None:
        """Ensure required database tables exist."""
        con = connect(self.db_path)
        try:
            # Active plans table
            con.execute("""
                CREATE TABLE IF NOT EXISTS active_plans (
                    plan_id TEXT PRIMARY KEY,
                    pair TEXT NOT NULL,
                    candidate_lower TEXT NOT NULL,
                    candidate_upper TEXT NOT NULL,
                    grid_step TEXT NOT NULL,
                    grid_count INTEGER NOT NULL,
                    regime TEXT NOT NULL,
                    range_quality_score TEXT NOT NULL,
                    candle_index INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    lifecycle_state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            # Candidate plans table (for pending reconfigurations)
            con.execute("""
                CREATE TABLE IF NOT EXISTS candidate_plans (
                    plan_id TEXT PRIMARY KEY,
                    active_plan_id TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    candidate_lower TEXT NOT NULL,
                    candidate_upper TEXT NOT NULL,
                    grid_step TEXT NOT NULL,
                    grid_count INTEGER NOT NULL,
                    range_quality_score TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reasons TEXT NOT NULL,
                    generated_at_candle INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    lifecycle_state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (active_plan_id) REFERENCES active_plans(plan_id)
                )
            """)

            # Pending reconfigurations table
            # candidate_plan_id is globally single-use: a candidate can enter
            # PENDING at most once, ever. The UNIQUE constraint is the
            # database-level integrity guard (application checks are a second,
            # friendlier layer that converts violations into LifecycleError).
            con.execute("""
                CREATE TABLE IF NOT EXISTS pending_reconfigs (
                    reconfig_id TEXT PRIMARY KEY,
                    active_plan_id TEXT NOT NULL,
                    candidate_plan_id TEXT NOT NULL UNIQUE,
                    from_generation INTEGER NOT NULL,
                    to_generation INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    reasons TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    FOREIGN KEY (active_plan_id) REFERENCES active_plans(plan_id),
                    FOREIGN KEY (candidate_plan_id) REFERENCES candidate_plans(plan_id)
                )
            """)

            # Lifecycle transitions table
            con.execute("""
                CREATE TABLE IF NOT EXISTS lifecycle_transitions (
                    transition_id TEXT PRIMARY KEY,
                    from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL,
                    action TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    details TEXT NOT NULL,
                    event_id TEXT
                )
            """)

            # Generations table
            con.execute("""
                CREATE TABLE IF NOT EXISTS generations (
                    generation INTEGER PRIMARY KEY,
                    active_plan_id TEXT,
                    previous_plan_id TEXT,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    FOREIGN KEY (active_plan_id) REFERENCES active_plans(plan_id)
                )
            """)

            self._ensure_single_use_migration(con)
            con.commit()
        finally:
            con.close()

    def _ensure_single_use_migration(self, con) -> None:
        """Backfill the candidate single-use guard on pre-existing databases.

        Tables created before the UNIQUE constraint may contain duplicate
        pending_reconfigs rows for one candidate. Keep the oldest row per
        candidate (the original submission), remove later duplicates, then
        install the unique index.
        """
        try:
            con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_reconfigs_candidate "
                "ON pending_reconfigs(candidate_plan_id)"
            )
        except sqlite3.OperationalError:
            con.execute(
                "DELETE FROM pending_reconfigs WHERE rowid NOT IN ("
                "SELECT MIN(rowid) FROM pending_reconfigs GROUP BY candidate_plan_id)"
            )
            con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_reconfigs_candidate "
                "ON pending_reconfigs(candidate_plan_id)"
            )

    def get_current_state(self, con=None, prefix: str = "") -> LifecycleState:
        """Get the current global lifecycle state.

        When ``con`` is supplied (cycle transaction join) the state is read
        from the caller's connection/prefix so it sees uncommitted cycle
        mutations.
        """
        if con is not None:
            state_entry = self._get_state_on(con, prefix, "lifecycle:state")
        else:
            state_entry = get_state(self.db_path, "lifecycle:state")
        if state_entry is None:
            return LifecycleState.NO_ACTIVE_GRID

        try:
            state_data = json.loads(state_entry)
            return LifecycleState(state_data["state"])
        except (json.JSONDecodeError, KeyError, ValueError):
            return LifecycleState.NO_ACTIVE_GRID

    def _set_state(self, state: LifecycleState) -> None:
        """Set the global lifecycle state."""
        state_data = {
            "state": state.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        set_state(self.db_path, "lifecycle:state", state_data)

    def _get_state_on(self, con, prefix: str, key: str):
        """Read one bot_state value on the given connection.

        ``prefix`` is the schema alias for the (possibly attached) lifecycle
        database.  Returns the raw value string or ``None``.
        """
        row = con.execute(
            f"SELECT value FROM {self._table('bot_state', prefix)} WHERE key=?",
            (key,),
        ).fetchone()
        return None if row is None else str(row["value"])

    def _set_state_on(self, con, state: LifecycleState, prefix: str = "") -> None:
        """Set global lifecycle state using the given (open) connection."""
        payload = json.dumps({
            "state": state.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, separators=(",", ":"))
        con.execute(
            f"INSERT INTO {self._table('bot_state', prefix)}(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("lifecycle:state", payload),
        )

    def get_generation(self, con=None, prefix: str = "") -> int:
        """Get the current generation counter (highest recorded generation)."""
        def block(c):
            return self._get_max_generation(c, prefix)
        return self._run_on(con, prefix, block)

    def _get_max_generation(self, con, prefix: str = "") -> int:
        """Read the highest recorded generation from an open connection."""
        row = con.execute(
            f"SELECT MAX(generation) as gen FROM {self._table('generations', prefix)}"
        ).fetchone()
        return row["gen"] if row is not None and row["gen"] is not None else 0

    def get_active_plan(self, con=None, prefix: str = "") -> Optional[ActivePlanState]:
        """Get the currently active grid plan, if any.

        The active plan remains visible while a reconfiguration is pending
        (trading still governed by the active plan during reconfig evaluation).

        When ``con`` is supplied (cycle transaction join) the row is read from
        the caller's connection so it sees uncommitted cycle mutations.
        """
        state = self.get_current_state(con=con, prefix=prefix)
        if state in (LifecycleState.NO_ACTIVE_GRID, LifecycleState.BLOCKED):
            return None

        own = con is None
        if own:
            con = connect(self.db_path)
        try:
            row = con.execute(
                f"""
                SELECT plan_id, pair, candidate_lower, candidate_upper, grid_step, grid_count,
                       regime, range_quality_score, candle_index, generation,
                       lifecycle_state
                FROM {self._table('active_plans', prefix)}
                WHERE lifecycle_state IN (?, ?)
                ORDER BY generation DESC, created_at DESC
                LIMIT 1
                """,
                (LifecycleState.ACTIVE.value, LifecycleState.RECONFIGURATION_PENDING.value)
            ).fetchone()

            if row is None:
                return None

            return ActivePlanState(
                plan_id=row["plan_id"],
                pair=row["pair"],
                candidate_lower=Decimal(row["candidate_lower"]),
                candidate_upper=Decimal(row["candidate_upper"]),
                grid_step=Decimal(row["grid_step"]),
                grid_count=row["grid_count"],
                regime=row["regime"],
                range_quality_score=Decimal(row["range_quality_score"]),
                candle_index=row["candle_index"],
                generation=row["generation"],
                lifecycle_state=LifecycleState(row["lifecycle_state"]),
            )
        finally:
            if own:
                con.close()

    def get_candidate_plan(self, candidate_plan_id: str,
                           con=None, prefix: str = "") -> Optional[CandidatePlanState]:
        """Get a candidate plan by ID.

        When ``con`` is supplied (cycle transaction join) the row is read from
        the caller's connection so it sees uncommitted cycle mutations.
        """
        own = con is None
        if own:
            con = connect(self.db_path)
        try:
            row = con.execute(
                f"""
                SELECT plan_id, active_plan_id, pair, regime, candidate_lower,
                       candidate_upper, grid_step, grid_count, range_quality_score,
                       decision, reasons, generated_at_candle, generation,
                       lifecycle_state
                FROM {self._table('candidate_plans', prefix)}
                WHERE plan_id = ?
                """,
                (candidate_plan_id,)
            ).fetchone()

            if row is None:
                return None

            return CandidatePlanState(
                plan_id=row["plan_id"],
                active_plan_id=row["active_plan_id"],
                decision=PlanDecision(row["decision"]),
                reasons=tuple(PlanBlockReason(r) for r in json.loads(row["reasons"])),
                pair=row["pair"],
                regime=row["regime"],
                candidate_lower=Decimal(row["candidate_lower"]),
                candidate_upper=Decimal(row["candidate_upper"]),
                grid_step=Decimal(row["grid_step"]),
                grid_count=row["grid_count"],
                range_quality_score=Decimal(row["range_quality_score"]),
                generated_at_candle=row["generated_at_candle"],
                generation=row["generation"],
                lifecycle_state=LifecycleState(row["lifecycle_state"]),
            )
        finally:
            if own:
                con.close()

    def get_pending_reconfiguration(self, active_plan_id: str,
                                    con=None, prefix: str = "") -> Optional[CandidatePlanState]:
        """Get pending reconfiguration for the given active plan, if any.

        When ``con`` is supplied (cycle transaction join) the rows are read
        from the caller's connection so they see uncommitted cycle mutations.
        """
        own = con is None
        if own:
            con = connect(self.db_path)
        try:
            row = con.execute(
                f"""
                SELECT candidate_plan_id
                FROM {self._table('pending_reconfigs', prefix)}
                WHERE active_plan_id = ? AND status IN ('PENDING', 'VALIDATED')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (active_plan_id,)
            ).fetchone()

            if row is None:
                return None

            return self.get_candidate_plan(row["candidate_plan_id"],
                                           con=con, prefix=prefix)
        finally:
            if own:
                con.close()

    def get_transition_history(self, limit: int = 50) -> List[LifecycleTransition]:
        """Get recent lifecycle transitions for audit trail."""
        con = connect(self.db_path)
        try:
            rows = con.execute(
                """
                SELECT transition_id, from_state, to_state, action, timestamp, details
                FROM lifecycle_transitions
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,)
            ).fetchall()

            result = []
            for row in rows:
                result.append(LifecycleTransition(
                    transition_id=row["transition_id"],
                    from_state=LifecycleState(row["from_state"]),
                    to_state=LifecycleState(row["to_state"]),
                    action=LifecycleAction(row["action"]),
                    timestamp=row["timestamp"],
                    details=json.loads(row["details"]),
                ))
            return result
        finally:
            con.close()

    def _transition_row_on(self, con, action: str, details_like: str,
                           prefix: str) -> Optional[dict]:
        """Latest lifecycle_transitions row matching an action + details fragment."""
        return con.execute(
            f"""SELECT transition_id, from_state, to_state, action, timestamp, details
            FROM {self._table('lifecycle_transitions', prefix)} WHERE action = ? AND details LIKE ?
            ORDER BY timestamp DESC LIMIT 1""",
            (action, details_like)
        ).fetchone()

    def _transition_from_row(self, row: dict) -> LifecycleTransition:
        return LifecycleTransition(
            transition_id=row["transition_id"],
            from_state=LifecycleState(row["from_state"]),
            to_state=LifecycleState(row["to_state"]),
            action=LifecycleAction(row["action"]),
            timestamp=row["timestamp"],
            details=json.loads(row["details"]),
        )

    def activate_plan(self, plan: AdaptiveGridPlan, cfg: Dict[str, Any],
                      con=None, prefix: str = "") -> LifecycleTransition:
        """Activate a new grid plan (initial or recovery from BLOCKED).

        Idempotent: if plan_id already exists, returns the existing transition.

        When ``con`` is supplied the mutation block runs on the caller's
        connection (cycle-transaction join) and does not commit; when it is
        None the method behaves exactly as before (private transaction).
        """
        current_state = self.get_current_state(con=con, prefix=prefix)

        # Idempotency: if this exact plan is already active, return existing transition early
        joined = con is not None
        if not joined:
            con_check = connect(self.db_path)
            try:
                if self._is_duplicate_plan(con_check, plan.plan_id, prefix=prefix):
                    row = self._transition_row_on(
                        con_check, LifecycleAction.ACTIVATE.value,
                        f'%"{plan.plan_id}"%', prefix,
                    )
                    if row is not None:
                        return self._transition_from_row(row)
            finally:
                con_check.close()

        if current_state not in (LifecycleState.NO_ACTIVE_GRID, LifecycleState.BLOCKED):
            raise LifecycleError(
                ValidationErrorCode.INVALID_TRANSITION,
                f"Cannot activate plan in current state: {current_state}"
            )

        # Validate plan: must be actionable and have an id
        if not plan.is_actionable:
            raise LifecycleError(
                ValidationErrorCode.TRANSITION_REJECTED,
                f"Cannot activate non-actionable plan: decision={plan.decision}",
                {"decision": plan.decision.value}
            )
        if not plan.plan_id:
            raise LifecycleError(
                ValidationErrorCode.TRANSITION_REJECTED,
                "Plan has no plan_id; compute_plan_id must be called before activation"
            )

        target_state = self._validate_transition(current_state, LifecycleAction.ACTIVATE)
        now = datetime.now(timezone.utc).isoformat()

        tx_con = con if joined else None
        with self._transaction(tx_con) as c:
            owns_commit = not joined
            # Idempotency fallback inside transaction
            if self._is_duplicate_plan(c, plan.plan_id, prefix=prefix):
                row = self._transition_row_on(
                    c, LifecycleAction.ACTIVATE.value,
                    f'%"{plan.plan_id}"%', prefix,
                )
                if row is not None:
                    if owns_commit:
                        c.commit()
                    return self._transition_from_row(row)

            # Determine generation
            new_generation = self._get_max_generation(c, prefix) + 1

            # Deactivate any previous active plan
            c.execute(
                f"UPDATE {self._table('active_plans', prefix)} "
                "SET lifecycle_state = ?, updated_at = ? WHERE lifecycle_state = ?",
                (LifecycleState.BLOCKED.value, now, LifecycleState.ACTIVE.value)
            )

            # Insert active plan
            c.execute(
                f"""
                INSERT OR IGNORE INTO {self._table('active_plans', prefix)} (
                    plan_id, pair, candidate_lower, candidate_upper, grid_step, grid_count,
                    regime, range_quality_score, candle_index, generation,
                    lifecycle_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id, plan.pair, str(plan.candidate_lower),
                    str(plan.candidate_upper), str(plan.grid_step), plan.grid_count,
                    plan.regime.value, str(plan.range_quality_score), 0, new_generation,
                    LifecycleState.ACTIVE.value, now, now,
                )
            )

            # Record generation
            c.execute(
                f"INSERT OR IGNORE INTO {self._table('generations', prefix)} "
                "(generation, active_plan_id, previous_plan_id, created_at, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (new_generation, plan.plan_id, None, now, "ACTIVE")
            )

            # Set global state
            self._set_state_on(c, target_state, prefix)

            # Create transition
            transition = LifecycleTransition.create(
                current_state, target_state, LifecycleAction.ACTIVATE,
                {"plan_id": plan.plan_id, "generation": new_generation, "candle_index": 0}
            )
            self._record_transition(c, transition, prefix)
            if owns_commit:
                c.commit()

        return transition

    def handle_planner_decision(self, adaptive_plan: AdaptiveGridPlan,
                               active_plan_state: Optional[ActivePlanState],
                               cfg: Dict[str, Any],
                               candle_index: int = 0,
                               con=None, prefix: str = "") -> LifecycleTransition:
        """Process a planner decision and trigger appropriate lifecycle transitions.

        When ``con`` is supplied the mutation path joins the caller's
        transaction (cycle ownership); when ``con`` is None the method
        behaves exactly as before (private transactions).
        """
        current_state = self.get_current_state(con=con, prefix=prefix)

        if active_plan_state is None:
            # No active plan, handle according to decision
            if adaptive_plan.decision == PlanDecision.GRID_ALLOWED:
                return self.activate_plan(adaptive_plan, cfg, con=con, prefix=prefix)
            elif adaptive_plan.decision == PlanDecision.GRID_BLOCKED:
                return self._transition_to_blocked(con=con, prefix=prefix)
            else:
                raise LifecycleError(
                    ValidationErrorCode.INVALID_TRANSITION,
                    f"Unhandled decision {adaptive_plan.decision} with no active plan"
                )

        # We have an active plan
        if adaptive_plan.decision == PlanDecision.KEEP_CURRENT_PLAN:
            return self._transition_to_keep_current(
                active_plan_state, adaptive_plan, candle_index,
                con=con, prefix=prefix,
            )
        elif adaptive_plan.decision == PlanDecision.RECONFIGURATION_REQUIRED:
            return self._transition_to_pending_reconfig(
                active_plan_state, adaptive_plan, candle_index,
                con=con, prefix=prefix,
            )
        elif adaptive_plan.decision == PlanDecision.GRID_BLOCKED:
            return self._transition_to_blocked(
                active_plan_state, con=con, prefix=prefix,
            )
        else:
            raise LifecycleError(
                ValidationErrorCode.INVALID_TRANSITION,
                f"Unhandled decision {adaptive_plan.decision} with active plan"
            )

    def validate_pending_reconfiguration(self, active_plan_id: str,
                                          candle_index: int = 0,
                                          con=None, prefix: str = "") -> LifecycleTransition:
        """Validate the pending reconfiguration and move to READY_TO_RECONFIGURE.

        Idempotent: if already validated, returns the existing transition.

        When ``con`` is supplied the mutation block joins the caller's
        transaction (cycle ownership); when ``con`` is None it behaves as
        before (private transaction).
        """
        joined = con is not None
        # Idempotency: if already READY_TO_RECONFIGURE, return existing VALIDATE_PENDING transition
        pre_con = con if joined else connect(self.db_path)
        try:
            if self.get_current_state(con=con, prefix=prefix) == LifecycleState.READY_TO_RECONFIGURE:
                row = self._transition_row_on(
                    pre_con, LifecycleAction.VALIDATE_PENDING.value,
                    f'%"{active_plan_id}"%', prefix,
                )
                if row is not None:
                    return self._transition_from_row(row)
        finally:
            if not joined:
                pre_con.close()

        current_state = self.get_current_state(con=con, prefix=prefix)
        if current_state != LifecycleState.RECONFIGURATION_PENDING:
            raise LifecycleError(
                ValidationErrorCode.INVALID_TRANSITION,
                f"Cannot validate pending reconfiguration from state {current_state}"
            )

        pending = self.get_pending_reconfiguration(active_plan_id, con=con, prefix=prefix)
        if pending is None:
            raise LifecycleError(
                ValidationErrorCode.TRANSITION_REJECTED,
                f"No pending reconfiguration found for plan {active_plan_id}"
            )

        # Stale generation check
        active_plan = self.get_active_plan(con=con, prefix=prefix)
        if active_plan is not None and pending.generation <= active_plan.generation:
            raise LifecycleError(
                ValidationErrorCode.STALE_CANDIDATE_PLAN,
                f"Candidate plan {pending.plan_id} is stale (gen {pending.generation} <= {active_plan.generation})"
            )

        target_state = self._validate_transition(current_state, LifecycleAction.VALIDATE_PENDING)
        now = datetime.now(timezone.utc).isoformat()

        tx_con = con if joined else None
        with self._transaction(tx_con) as c:
            owns_commit = not joined
            # Idempotency: check for existing VALIDATE_PENDING transition for
            # this exact (active plan, candidate, generation) identity, so
            # distinct reconfiguration cycles never collide.
            if self._is_duplicate_transition(
                c, current_state, target_state, LifecycleAction.VALIDATE_PENDING,
                plan_id=active_plan_id, candidate_plan_id=pending.plan_id,
                generation=pending.generation, prefix=prefix,
            ):
                existing = self._find_transition(
                    c, LifecycleAction.VALIDATE_PENDING,
                    plan_id=active_plan_id, candidate_plan_id=pending.plan_id,
                    prefix=prefix,
                )
                if existing is not None:
                    if owns_commit:
                        c.commit()
                    return existing

            # Update candidate plan lifecycle state
            c.execute(
                f"UPDATE {self._table('candidate_plans', prefix)} "
                "SET lifecycle_state = ? WHERE plan_id = ?",
                (LifecycleState.READY_TO_RECONFIGURE.value, pending.plan_id)
            )

            # Update pending_reconfigs status
            c.execute(
                f"UPDATE {self._table('pending_reconfigs', prefix)} "
                "SET status = ? WHERE active_plan_id = ? AND status = 'PENDING'",
                ("VALIDATED", active_plan_id)
            )

            # Set global state
            self._set_state_on(c, target_state, prefix)

            # Create transition
            transition = LifecycleTransition.create(
                current_state,
                target_state,
                LifecycleAction.VALIDATE_PENDING,
                {
                    "plan_id": active_plan_id,
                    "candidate_plan_id": pending.plan_id,
                    "candle_index": candle_index,
                    "generation": pending.generation,
                }
            )
            self._record_transition(c, transition, prefix)
            if owns_commit:
                c.commit()

        return transition

    def finalize_reconfiguration(self, active_plan_id: str,
                                 candle_index: int = 0,
                                 con=None, prefix: str = "") -> LifecycleTransition:
        """Finalize reconfiguration: swap candidate plan into active.

        Idempotent: if already finalized, returns the existing transition.

        When ``con`` is supplied the mutation block joins the caller's
        transaction (cycle ownership); when ``con`` is None it behaves as
        before (private transaction).
        """
        joined = con is not None
        # Idempotency: if already back to ACTIVE, return the existing READY
        # -> ACTIVE reconfiguration-swap transition for this candidate.
        # The lookup is scoped to the reconfiguration-swap rows only; the
        # legacy initial-activation ACTIVATE row (NO_ACTIVE_GRID -> ACTIVE,
        # same plan_id when candidate and active ids coincide) must NOT
        # alias this early return, or the swap is silently skipped and the
        # state wedges at READY_TO_RECONFIGURE (Phase 7A anomaly A1).
        pre_con = con if joined else connect(self.db_path)
        try:
            if self.get_current_state(con=con, prefix=prefix) == LifecycleState.ACTIVE:
                row2 = con.execute(
                    f"""SELECT transition_id, from_state, to_state, action, timestamp, details
                    FROM {self._table('lifecycle_transitions', prefix)}
                    WHERE action = ? AND from_state = ?
                    AND details LIKE ?
                    ORDER BY timestamp DESC LIMIT 1""",
                        (LifecycleAction.ACTIVATE.value,
                         LifecycleState.READY_TO_RECONFIGURE.value,
                         f'"previous_plan_id": "{active_plan_id}"'),
                    ).fetchone()
                if row2 is not None:
                    return self._transition_from_row(row2)
        finally:
            if not joined:
                pre_con.close()

        current_state = self.get_current_state(con=con, prefix=prefix)
        if current_state != LifecycleState.READY_TO_RECONFIGURE:
            raise LifecycleError(
                ValidationErrorCode.INVALID_TRANSITION,
                f"Cannot finalize reconfiguration from state {current_state}"
            )

        pending = self.get_pending_reconfiguration(active_plan_id, con=con, prefix=prefix)
        candidate = None
        if pending is not None:
            candidate = self.get_candidate_plan(pending.plan_id, con=con, prefix=prefix)
        if candidate is None:
            raise LifecycleError(
                ValidationErrorCode.TRANSITION_REJECTED,
                f"No validated candidate plan found for plan {active_plan_id}"
            )

        target_state = self._validate_transition(current_state, LifecycleAction.ACTIVATE)
        now = datetime.now(timezone.utc).isoformat()

        tx_con = con if joined else None
        with self._transaction(tx_con) as c:
            owns_commit = not joined
            # Idempotency: return the existing reconfiguration-swap
            # transition (READY_TO_RECONFIGURE -> ACTIVE) for this
            # candidate if it was already recorded.  Scoped to the swap
            # rows; the legacy initial-activation ACTIVATE row
            # (NO_ACTIVE_GRID -> ACTIVE, same plan_id when candidate and
            # active ids coincide) must NOT alias this check, or the
            # swap is silently skipped and the state wedges at READY
            # (Phase 7A anomaly A1).
            row = c.execute(
                f"""SELECT transition_id, from_state, to_state, action, timestamp, details
                FROM {self._table('lifecycle_transitions', prefix)}
                WHERE action = ? AND from_state = ?
                AND (details LIKE ? OR details LIKE ?)
                ORDER BY timestamp DESC LIMIT 1""",
                (LifecycleAction.ACTIVATE.value,
                 LifecycleState.READY_TO_RECONFIGURE.value,
                 f'"plan_id": "{candidate.plan_id}"',
                 f'"candidate_plan_id": "{candidate.plan_id}"'),
            ).fetchone()
            if row is not None:
                if owns_commit:
                    c.commit()
                return self._transition_from_row(row)

            # Deactivate current active plan / mark previous generation
            c.execute(
                f"UPDATE {self._table('active_plans', prefix)} "
                "SET lifecycle_state = ?, updated_at = ? WHERE plan_id = ?",
                (LifecycleState.BLOCKED.value, now, active_plan_id)
            )

            new_generation = self._get_max_generation(c, prefix) + 1

            # Swap candidate into active_plans
            c.execute(
                f"""
                INSERT OR REPLACE INTO {self._table('active_plans', prefix)} (
                    plan_id, pair, candidate_lower, candidate_upper, grid_step, grid_count,
                    regime, range_quality_score, candle_index, generation,
                    lifecycle_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.plan_id, candidate.pair, str(candidate.candidate_lower),
                    str(candidate.candidate_upper), str(candidate.grid_step),
                    candidate.grid_count, candidate.regime, str(candidate.range_quality_score),
                    candle_index, new_generation, LifecycleState.ACTIVE.value, now, now,
                )
            )

            # Record new generation
            c.execute(
                f"INSERT OR IGNORE INTO {self._table('generations', prefix)} "
                "(generation, active_plan_id, created_at, status) VALUES (?, ?, ?, ?)",
                (new_generation, candidate.plan_id, now, "ACTIVE")
            )

            # Mark candidate as consumed
            c.execute(
                f"UPDATE {self._table('candidate_plans', prefix)} "
                "SET lifecycle_state = ? WHERE plan_id = ?",
                (LifecycleState.BLOCKED.value, candidate.plan_id)
            )
            c.execute(
                f"UPDATE {self._table('pending_reconfigs', prefix)} "
                "SET status = ? WHERE active_plan_id = ? AND status IN ('PENDING','VALIDATED')",
                ("FINALIZED", active_plan_id)
            )

            # Set global state
            self._set_state_on(c, target_state, prefix)

            # Create transition
            transition = LifecycleTransition.create(
                current_state,
                target_state,
                LifecycleAction.ACTIVATE,
                {
                    "plan_id": candidate.plan_id,
                    "previous_plan_id": active_plan_id,
                    "candle_index": candle_index,
                    "generation": new_generation,
                }
            )
            self._record_transition(c, transition, prefix)
            if owns_commit:
                c.commit()

        return transition

    def _transition_to_blocked(self, active_plan_state: Optional[ActivePlanState] = None,
                               con=None, prefix: str = "") -> LifecycleTransition:
        """Transition to BLOCKED state.

        When ``con`` is supplied the mutation block joins the caller's
        transaction (cycle ownership); when ``con`` is None it behaves as
        before (private transaction).
        """
        current_state = self.get_current_state(con=con, prefix=prefix)

        if current_state == LifecycleState.BLOCKED:
            return LifecycleTransition.create(
                LifecycleState.BLOCKED,
                LifecycleState.BLOCKED,
                LifecycleAction.BLOCK,
                {"reason": "Already in BLOCKED state"}
            )

        # Validate transition
        target_state = self._validate_transition(current_state, LifecycleAction.BLOCK)
        now = datetime.now(timezone.utc).isoformat()

        joined = con is not None
        tx_con = con if joined else None
        with self._transaction(tx_con) as c:
            owns_commit = not joined
            # Idempotency: check for a duplicate BLOCK transition for the same
            # active plan. Scoping by plan_id keeps separate cycles independent.
            plan_id = active_plan_state.plan_id if active_plan_state is not None else None
            if self._is_duplicate_transition(
                c, current_state, LifecycleState.BLOCKED, LifecycleAction.BLOCK,
                plan_id=plan_id, prefix=prefix,
            ):
                existing = self._find_transition(
                    c, LifecycleAction.BLOCK, plan_id=plan_id, prefix=prefix,
                )
                if existing is not None:
                    if owns_commit:
                        c.commit()
                    return existing

            # Update active plan if present
            if active_plan_state is not None:
                c.execute(
                    f"UPDATE {self._table('active_plans', prefix)} "
                    "SET lifecycle_state = ?, updated_at = ? WHERE plan_id = ?",
                    (LifecycleState.BLOCKED.value, now, active_plan_state.plan_id)
                )

            # Set global state to BLOCKED
            self._set_state_on(c, target_state, prefix)

            # Create transition
            transition = LifecycleTransition.create(
                current_state,
                target_state,
                LifecycleAction.BLOCK,
                {"active_plan_id": active_plan_state.plan_id if active_plan_state else None}
            )
            self._record_transition(c, transition, prefix)
            if owns_commit:
                c.commit()

        return transition

    def _transition_to_keep_current(self, active_plan_state: ActivePlanState,
                                    adaptive_plan: AdaptiveGridPlan,
                                    candle_index: int = 0,
                                    con=None, prefix: str = "") -> LifecycleTransition:
        """Transition to KEEP_CURRENT_PLAN (no state change).

        When ``con`` is supplied the transition record joins the caller's
        transaction (cycle ownership); when ``con`` is None it behaves as
        before (private transaction).
        """
        current_state = self.get_current_state(con=con, prefix=prefix)

        # Validate transition
        target_state = self._validate_transition(current_state, LifecycleAction.KEEP_CURRENT)

        # Log the decision but don't change state
        logger.info(
            f"Keeping current plan {active_plan_state.plan_id} - decision: {adaptive_plan.decision}"
        )

        # Record transition for audit trail
        transition = LifecycleTransition.create(
            LifecycleState.ACTIVE,
            LifecycleState.ACTIVE,
            LifecycleAction.KEEP_CURRENT,
            {
                "plan_id": active_plan_state.plan_id,
                "candidate_plan_id": adaptive_plan.plan_id,
                "candle_index": candle_index,
            }
        )

        joined = con is not None
        tx_con = con if joined else None
        with self._transaction(tx_con) as c:
            owns_commit = not joined
            self._record_transition(c, transition, prefix)
            if owns_commit:
                c.commit()

        return transition

    def _pending_reconfig_check(self, c, active_plan_state, candidate_plan_id,
                               current_state, prefix: str):
        """Run the pre-transaction guards for ENTER_PENDING.

        Raises ``LifecycleError`` on any violation. Uses ``prefix`` to address
        lifecycle tables on the (possibly attached) schema.
        """
        # Idempotent replay: the exact same pair is still pending and the
        # machine has not progressed past it.
        if current_state in (
            LifecycleState.RECONFIGURATION_PENDING,
            LifecycleState.READY_TO_RECONFIGURE,
        ):
            row = c.execute(
                f"SELECT status FROM {self._table('pending_reconfigs', prefix)} "
                "WHERE active_plan_id = ? AND candidate_plan_id = ? LIMIT 1",
                (active_plan_state.plan_id, candidate_plan_id)
            ).fetchone()
            if row is not None and row["status"] in ("PENDING", "VALIDATED"):
                existing = self._find_transition(
                    c, LifecycleAction.ENTER_PENDING,
                    plan_id=active_plan_state.plan_id,
                    candidate_plan_id=candidate_plan_id,
                    prefix=prefix,
                )
                if existing is not None:
                    return existing

        # Single-use guard: any prior use of this candidate (including a
        # terminal FINALIZED row) forbids re-entering PENDING.
        used = c.execute(
            f"SELECT 1 FROM {self._table('pending_reconfigs', prefix)} "
            "WHERE candidate_plan_id = ? LIMIT 1",
            (candidate_plan_id,)
        ).fetchone()
        if used is not None:
            raise LifecycleError(
                ValidationErrorCode.DUPLICATE_TRANSITION,
                f"Candidate plan {candidate_plan_id} has already been used in a "
                f"previous reconfiguration cycle; candidate plans are single-use"
            )

        # Stale / terminal candidate row: reject it.
        cand = c.execute(
            f"SELECT lifecycle_state, generation FROM "
            f"{self._table('candidate_plans', prefix)} WHERE plan_id = ?",
            (candidate_plan_id,)
        ).fetchone()
        if cand is not None:
            max_gen = self._get_max_generation(c, prefix)
            if (cand["lifecycle_state"] in (
                LifecycleState.ACTIVE.value,
                LifecycleState.BLOCKED.value,
                LifecycleState.READY_TO_RECONFIGURE.value,
            ) or cand["generation"] <= max_gen):
                raise LifecycleError(
                    ValidationErrorCode.DUPLICATE_TRANSITION,
                    f"Candidate plan {candidate_plan_id} is terminal or stale "
                    f"(lifecycle_state={cand['lifecycle_state']}, gen={cand['generation']}) "
                    f"and cannot re-enter PENDING"
                )
            raise LifecycleError(
                ValidationErrorCode.DUPLICATE_TRANSITION,
                f"Candidate plan {candidate_plan_id} already exists in the lifecycle"
            )
        return None

    def _transition_to_pending_reconfig(self, active_plan_state: ActivePlanState,
                                        adaptive_plan: AdaptiveGridPlan,
                                        candle_index: int = 0,
                                        con=None, prefix: str = "") -> LifecycleTransition:
        """Transition to RECONFIGURATION_PENDING.

        Candidate plans are globally single-use:
        - Re-pending the exact same (active_plan_id, candidate_plan_id) pair
          while it is still pending replays the existing transition (idempotent).
        - A candidate that was finalized, consumed, stale-rejected, or otherwise
          terminally processed is rejected and can never re-enter PENDING.

        When ``con`` is supplied the mutation block joins the caller's
        transaction (cycle ownership); when ``con`` is None it behaves as
        before (private transaction).
        """
        candidate_plan_id = adaptive_plan.plan_id
        current_state = self.get_current_state(con=con, prefix=prefix)

        # Idempotent / guard pre-checks. When joining the cycle transaction we
        # run them on the caller's connection; otherwise on a private one.
        joined = con is not None
        pre_con = con if joined else connect(self.db_path)
        try:
            replay = self._pending_reconfig_check(
                pre_con, active_plan_state, candidate_plan_id,
                current_state, prefix,
            )
            if replay is not None:
                return replay
        finally:
            if not joined:
                pre_con.close()

        # Validate transition (fail-closed: e.g. ENTER_PENDING while PENDING is rejected)
        target_state = self._validate_transition(current_state, LifecycleAction.ENTER_PENDING)

        now = datetime.now(timezone.utc).isoformat()
        new_generation = self.get_generation(con=con, prefix=prefix) + 1

        try:
            tx_con = con if joined else None
            with self._transaction(tx_con) as c:
                owns_commit = not joined

                # Re-check inside the lock: the same pair may have been
                # recorded between the early check and BEGIN IMMEDIATE.
                row = c.execute(
                    f"SELECT status FROM {self._table('pending_reconfigs', prefix)} "
                    "WHERE active_plan_id = ? AND candidate_plan_id = ? LIMIT 1",
                    (active_plan_state.plan_id, candidate_plan_id)
                ).fetchone()
                if row is not None and row["status"] in ("PENDING", "VALIDATED"):
                    existing = self._find_transition(
                        c, LifecycleAction.ENTER_PENDING,
                        plan_id=active_plan_state.plan_id,
                        candidate_plan_id=candidate_plan_id,
                        prefix=prefix,
                    )
                    if owns_commit:
                        c.commit()
                    if existing is not None:
                        return existing

                # Single-use guard re-checked under the lock.
                used = c.execute(
                    f"SELECT 1 FROM {self._table('pending_reconfigs', prefix)} "
                    "WHERE candidate_plan_id = ? LIMIT 1",
                    (candidate_plan_id,)
                ).fetchone()
                if used is not None:
                    raise LifecycleError(
                        ValidationErrorCode.DUPLICATE_TRANSITION,
                        f"Candidate plan {candidate_plan_id} is single-use; rejected"
                    )

                # Insert candidate plan into candidate_plans table.
                # Plain INSERT (not OR IGNORE): a pre-existing row must have been
                # caught above; the PK violation here is fail-closed.
                c.execute(
                    f"""
                    INSERT INTO {self._table('candidate_plans', prefix)} (
                        plan_id, active_plan_id, pair, regime, candidate_lower,
                        candidate_upper, grid_step, grid_count, range_quality_score,
                        decision, reasons, generated_at_candle, generation,
                        lifecycle_state, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_plan_id,
                        active_plan_state.plan_id,
                        adaptive_plan.pair,
                        adaptive_plan.regime.value,
                        str(adaptive_plan.candidate_lower),
                        str(adaptive_plan.candidate_upper),
                        str(adaptive_plan.grid_step),
                        adaptive_plan.grid_count,
                        str(adaptive_plan.range_quality_score),
                        adaptive_plan.decision.value,
                        json.dumps([str(r) for r in adaptive_plan.reasons]),
                        candle_index,
                        new_generation,
                        LifecycleState.RECONFIGURATION_PENDING.value,
                        now,
                    )
                )

                # Insert pending reconfiguration. Plain INSERT: the UNIQUE
                # (candidate_plan_id) database guard is the last line of defense.
                reconfig_id = f"{active_plan_state.plan_id}_{candidate_plan_id}_{now}"
                c.execute(
                    f"""
                    INSERT INTO {self._table('pending_reconfigs', prefix)} (
                        reconfig_id, active_plan_id, candidate_plan_id,
                        from_generation, to_generation, decision, reasons,
                        created_at, expires_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reconfig_id,
                        active_plan_state.plan_id,
                        candidate_plan_id,
                        active_plan_state.generation,
                        new_generation,
                        adaptive_plan.decision.value,
                        json.dumps([str(r) for r in adaptive_plan.reasons]),
                        now,
                        now,  # expires_at - could add TTL logic later
                        "PENDING",
                    )
                )

                # Update active plan lifecycle state to RECONFIGURATION_PENDING
                c.execute(
                    f"UPDATE {self._table('active_plans', prefix)} "
                    "SET lifecycle_state = ?, updated_at = ? WHERE plan_id = ?",
                    (LifecycleState.RECONFIGURATION_PENDING.value, now, active_plan_state.plan_id)
                )

                # Set global state
                self._set_state_on(c, target_state, prefix)

                # Create transition
                transition = LifecycleTransition.create(
                    LifecycleState.ACTIVE,
                    LifecycleState.RECONFIGURATION_PENDING,
                    LifecycleAction.ENTER_PENDING,
                    {
                        "plan_id": active_plan_state.plan_id,
                        "candidate_plan_id": candidate_plan_id,
                        "candle_index": candle_index,
                        "generation": new_generation,
                    }
                )
                self._record_transition(c, transition, prefix)
                if owns_commit:
                    c.commit()
        except sqlite3.IntegrityError as exc:
            # Database-level single-use guard fired (concurrent race or legacy
            # conflict that slipped past the application checks).
            raise LifecycleError(
                ValidationErrorCode.DUPLICATE_TRANSITION,
                f"Candidate plan {candidate_plan_id} conflicts with an existing "
                f"lifecycle record; single-use violated",
                {"reason": str(exc)},
            ) from exc

        return transition

    def close_active_plan(self, reason: str,
                          details: Optional[Dict[str, Any]] = None,
                          con=None, prefix: str = "") -> bool:
        """Close the active plan and return the lifecycle to NO_ACTIVE_GRID.

        Used by the strategy auto-exit path (and available to operators): the
        active plan is marked CLOSED, the global state transitions to
        NO_ACTIVE_GRID via the audited ``CLOSE`` action, and a transition row
        is recorded.  The transition is validated against
        ``_VALID_TRANSITIONS`` (fail-closed) and the whole mutation runs in a
        single transaction.

        Idempotent: when the lifecycle is already NO_ACTIVE_GRID or BLOCKED
        there is nothing to close and this returns ``False`` without writing.

        Returns ``True`` when a plan was closed, ``False`` when no plan was
        active.
        """
        if not reason or not str(reason).strip():
            raise ValueError("close_active_plan requires a non-empty reason")

        def block(c):
            current_state = self.get_current_state(con=c, prefix=prefix)
            if current_state in (LifecycleState.NO_ACTIVE_GRID,
                                 LifecycleState.BLOCKED):
                return False
            active = self.get_active_plan(con=c, prefix=prefix)
            plan_id = active.plan_id if active is not None else None
            target_state = self._validate_transition(
                current_state, LifecycleAction.CLOSE)
            now = datetime.now(timezone.utc).isoformat()
            if plan_id is not None:
                c.execute(
                    f"UPDATE {self._table('active_plans', prefix)} "
                    "SET lifecycle_state = ?, updated_at = ? WHERE plan_id = ?",
                    ("CLOSED", now, plan_id),
                )
            payload: Dict[str, Any] = {
                "reason": str(reason).strip(),
                "plan_id": plan_id,
            }
            if details:
                payload.update(details)
            transition = LifecycleTransition.create(
                from_state=current_state,
                to_state=target_state,
                action=LifecycleAction.CLOSE,
                details=payload,
            )
            self._record_transition(c, transition, prefix)
            self._set_state_on(c, target_state, prefix)
            return True

        return self._run_on(con, prefix, block)

    def _validate_transition(self, from_state: LifecycleState,
                             action: LifecycleAction) -> LifecycleState:
        """Validate a transition. Fail-closed: unknown transitions raise."""
        allowed = _VALID_TRANSITIONS.get(from_state)
        if allowed is None or action not in allowed:
            raise LifecycleError(
                ValidationErrorCode.INVALID_TRANSITION,
                f"Invalid transition: {from_state} --[{action}]--> ?"
            )
        return allowed[action]

    def _record_transition(self, con, transition: LifecycleTransition,
                           prefix: str = "") -> None:
        """Insert a transition record (idempotent via transition_id PK)."""
        con.execute(
            f"INSERT OR IGNORE INTO {self._table('lifecycle_transitions', prefix)} "
            "(transition_id, from_state, to_state, action, timestamp, details, event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                transition.transition_id,
                transition.from_state.value,
                transition.to_state.value,
                transition.action.value,
                transition.timestamp,
                json.dumps(transition.details),
                transition.transition_id,
            )
        )

    def _is_duplicate_transition(self, con, from_state: LifecycleState,
                                 to_state: LifecycleState,
                                 action: LifecycleAction,
                                 plan_id: Optional[str] = None,
                                 candidate_plan_id: Optional[str] = None,
                                 generation: Optional[int] = None,
                                 prefix: str = "") -> bool:
        """Check whether an identical transition for the same lifecycle identity
        has already been recorded.

        Scope is ``(from_state, to_state, action)`` plus optional identity
        fields (plan_id / candidate_plan_id / generation). Without the identity
        fields transitions from different plans or reconfiguration cycles would
        incorrectly look like duplicates of one another.
        """
        sql = (f"SELECT 1 FROM {self._table('lifecycle_transitions', prefix)} "
               "WHERE from_state = ? AND to_state = ? AND action = ?")
        params: List[Any] = [from_state.value, to_state.value, action.value]
        if plan_id is not None:
            # Transitions record the plan under either "plan_id" (ACTIVATE /
            # ENTER_PENDING / VALIDATE_PENDING) or "active_plan_id" (BLOCK).
            sql += " AND (details LIKE ? OR details LIKE ?)"
            params.append(f'%"plan_id": "{plan_id}"%')
            params.append(f'%"active_plan_id": "{plan_id}"%')
        if candidate_plan_id is not None:
            sql += " AND details LIKE ?"
            params.append(f'%"candidate_plan_id": "{candidate_plan_id}"%')
        if generation is not None:
            sql += " AND (details LIKE ? OR details LIKE ?)"
            params.append(f'"generation": {generation}}}')
            params.append(f'"generation": {generation},')
        row = con.execute(sql, params).fetchone()
        return row is not None

    def _find_transition(self, con, action: LifecycleAction,
                         plan_id: Optional[str] = None,
                         candidate_plan_id: Optional[str] = None,
                         prefix: str = "") -> Optional[LifecycleTransition]:
        """Return the most recent recorded transition matching action + identity."""
        sql = (f"SELECT transition_id, from_state, to_state, action, timestamp, details "
               f"FROM {self._table('lifecycle_transitions', prefix)} WHERE action = ?")
        params: List[Any] = [action.value]
        if plan_id is not None:
            # Transitions record the plan under either "plan_id" (ACTIVATE /
            # ENTER_PENDING / VALIDATE_PENDING) or "active_plan_id" (BLOCK).
            sql += " AND (details LIKE ? OR details LIKE ?)"
            params.append(f'%"plan_id": "{plan_id}"%')
            params.append(f'%"active_plan_id": "{plan_id}"%')
        if candidate_plan_id is not None:
            sql += " AND details LIKE ?"
            params.append(f'%"candidate_plan_id": "{candidate_plan_id}"%')
        sql += " ORDER BY timestamp DESC LIMIT 1"
        row = con.execute(sql, params).fetchone()
        if row is None:
            return None
        return LifecycleTransition(
            transition_id=row["transition_id"],
            from_state=LifecycleState(row["from_state"]),
            to_state=LifecycleState(row["to_state"]),
            action=LifecycleAction(row["action"]),
            timestamp=row["timestamp"],
            details=json.loads(row["details"]),
        )

    def _is_duplicate_plan(self, con, plan_id: str, prefix: str = "") -> bool:
        """Check whether the plan already exists in active_plans."""
        row = con.execute(
            f"SELECT 1 FROM {self._table('active_plans', prefix)} "
            "WHERE plan_id = ? LIMIT 1",
            (plan_id,)
        ).fetchone()
        return row is not None

    def _pending_row(self, con, active_plan_id: str,
                     candidate_plan_id: Optional[str] = None):
        """Return a pending_reconfigs row for the given active plan / candidate.

        With only ``active_plan_id`` this matches any non-terminal row for that
        plan. With ``candidate_plan_id`` it matches any row (including terminal
        FINALIZED ones) because a candidate is globally single-use.
        """
        if candidate_plan_id is not None:
            return con.execute(
                "SELECT reconfig_id, active_plan_id, candidate_plan_id, status "
                "FROM pending_reconfigs WHERE candidate_plan_id = ? LIMIT 1",
                (candidate_plan_id,)
            ).fetchone()
        return con.execute(
            "SELECT reconfig_id, active_plan_id, candidate_plan_id, status "
            "FROM pending_reconfigs WHERE active_plan_id = ? "
            "AND status IN ('PENDING','VALIDATED') LIMIT 1",
            (active_plan_id,)
        ).fetchone()

    @contextmanager
    def _transaction(self, con=None, owns_commit=True):
        """Context manager for database transactions.

        Standalone (``con is None``): opens a private connection with
        ``BEGIN IMMEDIATE``; the caller commits explicitly. When ``con`` is
        supplied the caller already owns an open transaction (e.g. the cycle
        transaction), so the body runs directly on it and no commit or rollback
        is issued here -- the outer owner commits exactly once.
        """
        if con is not None:
            yield con
            return
        own = connect(self.db_path)
        try:
            own.execute("BEGIN IMMEDIATE")
            yield own
        except Exception:
            own.rollback()
            raise
        finally:
            own.close()

    def _table(self, name: str, prefix: str) -> str:
        """Return a schema-prefixed table name for lifecycle SQL.

        ``prefix`` is ``"lifecycle."`` when the lifecycle database was attached
        to the order-DB connection, or ``""`` when lifecycle tables live in the
        connection's own ``main`` schema.
        """
        return f"{prefix}{name}"

    # ------------------------------------------------------------------
    # Transaction-join support (Patch 2D)
    #
    # Every read/mutation method accepts an optional ``con`` (the caller's
    # open connection) and ``prefix`` ("lifecycle." when the lifecycle DB is
    # attached to the order-DB connection). When ``con`` is supplied the body
    # runs on it and no commit/close happens here -- the outer cycle owner
    # commits exactly once. When ``con`` is None the method behaves exactly
    # as before (private connection, own transaction).
    # ------------------------------------------------------------------

    def _run_on(self, con, prefix: str, block):
        """Run ``block(con)`` on ``con`` when supplied, else standalone.

        ``block`` receives the connection to issue SQL on. In the joined case
        the connection is the caller's and ownership (commit/close) is left
        with the caller. In the standalone case a private connection with
        BEGIN IMMEDIATE is opened and closed here.
        """
        if con is not None:
            return block(con)
        own = connect(self.db_path)
        try:
            own.execute("BEGIN IMMEDIATE")
            result = block(own)
            own.commit()
        except Exception:
            own.rollback()
            raise
        finally:
            own.close()
        return result


class LifecycleError(Exception):
    """Lifecycle management exception."""
    def __init__(self, code: ValidationErrorCode, message: str, details: Optional[Dict] = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(f"[{code.value}] {message}")


if __name__ == "__main__":
    # Simple test
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(suffix='.sqlite3', delete=False) as tmp:
        db_path = tmp.name

    try:
        manager = LifecycleManager(db_path)

        # Test activation
        from grid_planner import AdaptiveGridPlan, PlanDecision
        from market_regime import MarketRegime

        test_plan = AdaptiveGridPlan(
            plan_id="test_plan_123",
            pair="BTCUSDT",
            regime=MarketRegime.RANGE,
            range_quality_score=Decimal("85.0"),
            candidate_lower=Decimal("98"),
            candidate_upper=Decimal("103"),
            grid_type="GEOMETRIC",
            grid_step=Decimal("0.006"),
            grid_count=10,
            levels=(),
            total_quote_budget=Decimal("0"),
            buy_quote_budget=Decimal("0"),
            required_base_inventory=Decimal("2"),
            available_base_inventory=Decimal("2"),
            inventory_sufficient=True,
            estimated_net_profit_per_grid=Decimal("0.004"),
            decision=PlanDecision.GRID_ALLOWED,
            reasons=(),
        )

        config = {
            "grid": {"step_pct": Decimal("0.006"), "hard_min_net_pct": Decimal("0.002")},
            "execution": {"total_quote_budget": Decimal("0")},
        }

        print("Testing plan activation...")
        transition = manager.activate_plan(test_plan, config)
        print(f"Activated plan with transition: {transition.transition_id}")
        print(f"Current state: {manager.get_current_state()}")

        print("\nTesting planner decision handling...")
        decision_plan = AdaptiveGridPlan(
            plan_id="decision_plan_456",
            pair="BTCUSDT",
            regime=MarketRegime.RANGE,
            range_quality_score=Decimal("85.0"),
            candidate_lower=Decimal("98.5"),
            candidate_upper=Decimal("102.5"),
            grid_type="GEOMETRIC",
            grid_step=Decimal("0.006"),
            grid_count=10,
            levels=(),
            total_quote_budget=Decimal("0"),
            buy_quote_budget=Decimal("0"),
            required_base_inventory=Decimal("2"),
            available_base_inventory=Decimal("2"),
            inventory_sufficient=True,
            estimated_net_profit_per_grid=Decimal("0.004"),
            decision=PlanDecision.KEEP_CURRENT_PLAN,
            reasons=(),
        )

        active = manager.get_active_plan()
        transition2 = manager.handle_planner_decision(decision_plan, active, config)
        print(f"Handled decision with transition: {transition2.transition_id}")

        print("\nTesting reconfiguration flow (pending -> validate -> finalize)...")
        reconfig_plan = AdaptiveGridPlan(
            plan_id="reconfig_plan_789",
            pair="BTCUSDT",
            regime=MarketRegime.RANGE,
            range_quality_score=Decimal("90.0"),
            candidate_lower=Decimal("99"),
            candidate_upper=Decimal("102"),
            grid_type="GEOMETRIC",
            grid_step=Decimal("0.005"),
            grid_count=12,
            levels=(),
            total_quote_budget=Decimal("0"),
            buy_quote_budget=Decimal("0"),
            required_base_inventory=Decimal("2"),
            available_base_inventory=Decimal("2"),
            inventory_sufficient=True,
            estimated_net_profit_per_grid=Decimal("0.005"),
            decision=PlanDecision.RECONFIGURATION_REQUIRED,
            reasons=(),
        )

        active = manager.get_active_plan()
        t_pending = manager.handle_planner_decision(reconfig_plan, active, config, candle_index=100)
        print(f"Entered pending: {t_pending.to_state} gen={manager.get_generation()}")
        assert manager.get_current_state() == LifecycleState.RECONFIGURATION_PENDING

        t_validated = manager.validate_pending_reconfiguration(active.plan_id, candle_index=101)
        print(f"Validated: {t_validated.to_state}")
        assert manager.get_current_state() == LifecycleState.READY_TO_RECONFIGURE

        t_finalized = manager.finalize_reconfiguration(active.plan_id, candle_index=102)
        print(f"Finalized: {t_finalized.to_state}")
        assert manager.get_current_state() == LifecycleState.ACTIVE
        assert manager.get_active_plan().plan_id == "reconfig_plan_789"
        assert manager.get_active_plan().generation == 2
        print(f"New active plan: {manager.get_active_plan().plan_id} gen={manager.get_active_plan().generation}")

        print("\nAll lifecycle tests passed!")

    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)