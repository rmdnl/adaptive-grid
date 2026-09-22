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
    },
    LifecycleState.RECONFIGURATION_PENDING: {
        LifecycleAction.VALIDATE_PENDING: LifecycleState.READY_TO_RECONFIGURE,
        LifecycleAction.BLOCK: LifecycleState.BLOCKED,
    },
    LifecycleState.READY_TO_RECONFIGURE: {
        LifecycleAction.ACTIVATE: LifecycleState.ACTIVE,
        LifecycleAction.BLOCK: LifecycleState.BLOCKED,
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

    def get_current_state(self) -> LifecycleState:
        """Get the current global lifecycle state."""
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

    def _set_state_on(self, con, state: LifecycleState) -> None:
        """Set global lifecycle state using the given (open) connection."""
        payload = json.dumps({
            "state": state.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, separators=(",", ":"))
        con.execute(
            "INSERT INTO bot_state(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("lifecycle:state", payload),
        )

    def get_generation(self) -> int:
        """Get the current generation counter (highest recorded generation)."""
        con = connect(self.db_path)
        try:
            return self._get_max_generation(con)
        finally:
            con.close()

    def _get_max_generation(self, con) -> int:
        """Read the highest recorded generation from an open connection."""
        row = con.execute("SELECT MAX(generation) as gen FROM generations").fetchone()
        return row["gen"] if row is not None and row["gen"] is not None else 0

    def get_active_plan(self) -> Optional[ActivePlanState]:
        """Get the currently active grid plan, if any.

        The active plan remains visible while a reconfiguration is pending
        (trading still governed by the active plan during reconfig evaluation).
        """
        state = self.get_current_state()
        if state in (LifecycleState.NO_ACTIVE_GRID, LifecycleState.BLOCKED):
            return None

        con = connect(self.db_path)
        try:
            row = con.execute(
                """
                SELECT plan_id, pair, candidate_lower, candidate_upper, grid_step, grid_count,
                       regime, range_quality_score, candle_index, generation,
                       lifecycle_state
                FROM active_plans
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
            con.close()

    def get_candidate_plan(self, candidate_plan_id: str) -> Optional[CandidatePlanState]:
        """Get a candidate plan by ID."""
        con = connect(self.db_path)
        try:
            row = con.execute(
                """
                SELECT plan_id, active_plan_id, pair, regime, candidate_lower,
                       candidate_upper, grid_step, grid_count, range_quality_score,
                       decision, reasons, generated_at_candle, generation,
                       lifecycle_state
                FROM candidate_plans
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
            con.close()

    def get_pending_reconfiguration(self, active_plan_id: str) -> Optional[CandidatePlanState]:
        """Get pending reconfiguration for the given active plan, if any."""
        con = connect(self.db_path)
        try:
            row = con.execute(
                """
                SELECT candidate_plan_id
                FROM pending_reconfigs
                WHERE active_plan_id = ? AND status IN ('PENDING', 'VALIDATED')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (active_plan_id,)
            ).fetchone()

            if row is None:
                return None

            return self.get_candidate_plan(row["candidate_plan_id"])
        finally:
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

    def activate_plan(self, plan: AdaptiveGridPlan, cfg: Dict[str,
 Any]) -> LifecycleTransition:
        """Activate a new grid plan (initial or recovery from BLOCKED).

        Idempotent: if plan_id already exists, returns the existing transition.
        """
        current_state = self.get_current_state()

        # Idempotency: if this exact plan is already active, return existing transition early
        con_check = connect(self.db_path)
        try:
            if self._is_duplicate_plan(con_check, plan.plan_id):
                row = con_check.execute(
                    """SELECT transition_id, from_state, to_state, action, timestamp, details
                    FROM lifecycle_transitions WHERE action = ? AND details LIKE ?
                    ORDER BY timestamp DESC LIMIT 1""",
                    (LifecycleAction.ACTIVATE.value, f'%"{plan.plan_id}"%')
                ).fetchone()
                if row is not None:
                    return LifecycleTransition(
                        transition_id=row["transition_id"],
                        from_state=LifecycleState(row["from_state"]),
                        to_state=LifecycleState(row["to_state"]),
                        action=LifecycleAction(row["action"]),
                        timestamp=row["timestamp"],
                        details=json.loads(row["details"]),
                    )
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

        with self._transaction() as con:
            # Idempotency fallback inside transaction
            if self._is_duplicate_plan(con, plan.plan_id):
                row = con.execute(
                    """SELECT transition_id, from_state, to_state, action, timestamp, details
                    FROM lifecycle_transitions WHERE action = ? AND details LIKE ?
                    ORDER BY timestamp DESC LIMIT 1""",
                    (LifecycleAction.ACTIVATE.value, f'%"{plan.plan_id}"%')
                ).fetchone()
                if row is not None:
                    con.commit()
                    return LifecycleTransition(
                        transition_id=row["transition_id"],
                        from_state=LifecycleState(row["from_state"]),
                        to_state=LifecycleState(row["to_state"]),
                        action=LifecycleAction(row["action"]),
                        timestamp=row["timestamp"],
                        details=json.loads(row["details"]),
                    )

            # Determine generation
            new_generation = self._get_max_generation(con) + 1

            # Deactivate any previous active plan
            con.execute(
                "UPDATE active_plans SET lifecycle_state = ?, updated_at = ? WHERE lifecycle_state = ?",
                (LifecycleState.BLOCKED.value, now, LifecycleState.ACTIVE.value)
            )

            # Insert active plan
            con.execute(
                """
                INSERT OR IGNORE INTO active_plans (
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
            con.execute(
                "INSERT OR IGNORE INTO generations (generation, active_plan_id, previous_plan_id, created_at, status) VALUES (?, ?, ?, ?, ?)",
                (new_generation, plan.plan_id, None, now, "ACTIVE")
            )

            # Set global state
            self._set_state_on(con, target_state)

            # Create transition
            transition = LifecycleTransition.create(
                current_state, target_state, LifecycleAction.ACTIVATE,
                {"plan_id": plan.plan_id, "generation": new_generation, "candle_index": 0}
            )
            self._record_transition(con, transition)
            con.commit()

        return transition

    def handle_planner_decision(self, adaptive_plan: AdaptiveGridPlan,
                               active_plan_state: Optional[ActivePlanState],
                               cfg: Dict[str, Any],
                               candle_index: int = 0) -> LifecycleTransition:
        """Process a planner decision and trigger appropriate lifecycle transitions."""
        current_state = self.get_current_state()

        if active_plan_state is None:
            # No active plan, handle according to decision
            if adaptive_plan.decision == PlanDecision.GRID_ALLOWED:
                return self.activate_plan(adaptive_plan, cfg)
            elif adaptive_plan.decision == PlanDecision.GRID_BLOCKED:
                return self._transition_to_blocked()
            else:
                raise LifecycleError(
                    ValidationErrorCode.INVALID_TRANSITION,
                    f"Unhandled decision {adaptive_plan.decision} with no active plan"
                )

        # We have an active plan
        if adaptive_plan.decision == PlanDecision.KEEP_CURRENT_PLAN:
            return self._transition_to_keep_current(active_plan_state, adaptive_plan, candle_index)
        elif adaptive_plan.decision == PlanDecision.RECONFIGURATION_REQUIRED:
            return self._transition_to_pending_reconfig(active_plan_state, adaptive_plan, candle_index)
        elif adaptive_plan.decision == PlanDecision.GRID_BLOCKED:
            return self._transition_to_blocked(active_plan_state)
        else:
            raise LifecycleError(
                ValidationErrorCode.INVALID_TRANSITION,
                f"Unhandled decision {adaptive_plan.decision} with active plan"
            )

    def validate_pending_reconfiguration(self, active_plan_id: str,
                                          candle_index: int = 0) -> LifecycleTransition:
        """Validate the pending reconfiguration and move to READY_TO_RECONFIGURE.

        Idempotent: if already validated, returns the existing transition.
        """
        # Idempotency: if already READY_TO_RECONFIGURE, return existing VALIDATE_PENDING transition
        con_check = connect(self.db_path)
        try:
            if self.get_current_state() == LifecycleState.READY_TO_RECONFIGURE:
                row = con_check.execute(
                    """SELECT transition_id, from_state, to_state, action, timestamp, details
                    FROM lifecycle_transitions WHERE action = ? AND details LIKE ?
                    ORDER BY timestamp DESC LIMIT 1""",
                    (LifecycleAction.VALIDATE_PENDING.value, f'%"{active_plan_id}"%')
                ).fetchone()
                if row is not None:
                    return LifecycleTransition(
                        transition_id=row["transition_id"],
                        from_state=LifecycleState(row["from_state"]),
                        to_state=LifecycleState(row["to_state"]),
                        action=LifecycleAction(row["action"]),
                        timestamp=row["timestamp"],
                        details=json.loads(row["details"]),
                    )
        finally:
            con_check.close()

        current_state = self.get_current_state()
        if current_state != LifecycleState.RECONFIGURATION_PENDING:
            raise LifecycleError(
                ValidationErrorCode.INVALID_TRANSITION,
                f"Cannot validate pending reconfiguration from state {current_state}"
            )

        pending = self.get_pending_reconfiguration(active_plan_id)
        if pending is None:
            raise LifecycleError(
                ValidationErrorCode.TRANSITION_REJECTED,
                f"No pending reconfiguration found for plan {active_plan_id}"
            )

        # Stale generation check
        active_plan = self.get_active_plan()
        if active_plan is not None and pending.generation <= active_plan.generation:
            raise LifecycleError(
                ValidationErrorCode.STALE_CANDIDATE_PLAN,
                f"Candidate plan {pending.plan_id} is stale (gen {pending.generation} <= {active_plan.generation})"
            )

        target_state = self._validate_transition(current_state, LifecycleAction.VALIDATE_PENDING)
        now = datetime.now(timezone.utc).isoformat()

        with self._transaction() as con:
            # Idempotency: check for existing VALIDATE_PENDING transition for
            # this exact (active plan, candidate, generation) identity, so
            # distinct reconfiguration cycles never collide.
            if self._is_duplicate_transition(
                con, current_state, target_state, LifecycleAction.VALIDATE_PENDING,
                plan_id=active_plan_id, candidate_plan_id=pending.plan_id,
                generation=pending.generation,
            ):
                existing = self._find_transition(
                    con, LifecycleAction.VALIDATE_PENDING,
                    plan_id=active_plan_id, candidate_plan_id=pending.plan_id,
                )
                if existing is not None:
                    con.commit()
                    return existing

            # Update candidate plan lifecycle state
            con.execute(
                "UPDATE candidate_plans SET lifecycle_state = ? WHERE plan_id = ?",
                (LifecycleState.READY_TO_RECONFIGURE.value, pending.plan_id)
            )

            # Update pending_reconfigs status
            con.execute(
                "UPDATE pending_reconfigs SET status = ? WHERE active_plan_id = ? AND status = 'PENDING'",
                ("VALIDATED", active_plan_id)
            )

            # Set global state
            self._set_state_on(con, target_state)

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
            self._record_transition(con, transition)
            con.commit()

        return transition

    def finalize_reconfiguration(self, active_plan_id: str,
                                 candle_index: int = 0) -> LifecycleTransition:
        """Finalize reconfiguration: swap candidate plan into active.

        Idempotent: if already finalized, returns the existing transition.
        """
        # Idempotency: if already back to ACTIVE, return existing ACTIVATE transition for this candidate
        con_check = connect(self.db_path)
        try:
            row = con_check.execute(
                """SELECT c.plan_id FROM pending_reconfigs p
                JOIN candidate_plans c ON c.plan_id = p.candidate_plan_id
                WHERE p.active_plan_id = ?
                ORDER BY p.created_at DESC LIMIT 1""",
                (active_plan_id,)
            ).fetchone()
            if row is not None and self.get_current_state() == LifecycleState.ACTIVE:
                row2 = con_check.execute(
                    """SELECT transition_id, from_state, to_state, action, timestamp, details
                    FROM lifecycle_transitions WHERE action = ? AND details LIKE ?
                    ORDER BY timestamp DESC LIMIT 1""",
                    (LifecycleAction.ACTIVATE.value, f'%"{row["plan_id"]}"%')
                ).fetchone()
                if row2 is not None:
                    return LifecycleTransition(
                        transition_id=row2["transition_id"],
                        from_state=LifecycleState(row2["from_state"]),
                        to_state=LifecycleState(row2["to_state"]),
                        action=LifecycleAction(row2["action"]),
                        timestamp=row2["timestamp"],
                        details=json.loads(row2["details"]),
                    )
        finally:
            con_check.close()

        current_state = self.get_current_state()
        if current_state != LifecycleState.READY_TO_RECONFIGURE:
            raise LifecycleError(
                ValidationErrorCode.INVALID_TRANSITION,
                f"Cannot finalize reconfiguration from state {current_state}"
            )

        pending = self.get_pending_reconfiguration(active_plan_id)
        candidate = None
        if pending is not None:
            candidate = self.get_candidate_plan(pending.plan_id)
        if candidate is None:
            raise LifecycleError(
                ValidationErrorCode.TRANSITION_REJECTED,
                f"No validated candidate plan found for plan {active_plan_id}"
            )

        target_state = self._validate_transition(current_state, LifecycleAction.ACTIVATE)
        now = datetime.now(timezone.utc).isoformat()

        with self._transaction() as con:
            # Idempotency: check for existing ACTIVATE transition for this candidate
            row = con.execute(
                """SELECT transition_id, from_state, to_state, action, timestamp, details
                FROM lifecycle_transitions WHERE action = ? AND details LIKE ?
                ORDER BY timestamp DESC LIMIT 1""",
                (LifecycleAction.ACTIVATE.value, f'%"{candidate.plan_id}"%')
            ).fetchone()
            if row is not None:
                con.commit()
                return LifecycleTransition(
                    transition_id=row["transition_id"],
                    from_state=LifecycleState(row["from_state"]),
                    to_state=LifecycleState(row["to_state"]),
                    action=LifecycleAction(row["action"]),
                    timestamp=row["timestamp"],
                    details=json.loads(row["details"]),
                )

            # Deactivate current active plan / mark previous generation
            con.execute(
                "UPDATE active_plans SET lifecycle_state = ?, updated_at = ? WHERE plan_id = ?",
                (LifecycleState.BLOCKED.value, now, active_plan_id)
            )

            new_generation = self._get_max_generation(con) + 1

            # Swap candidate into active_plans
            con.execute(
                """
                INSERT OR REPLACE INTO active_plans (
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
            con.execute(
                "INSERT OR IGNORE INTO generations (generation, active_plan_id, created_at, status) VALUES (?, ?, ?, ?)",
                (new_generation, candidate.plan_id, now, "ACTIVE")
            )

            # Mark candidate as consumed
            con.execute(
                "UPDATE candidate_plans SET lifecycle_state = ? WHERE plan_id = ?",
                (LifecycleState.BLOCKED.value, candidate.plan_id)
            )
            con.execute(
                "UPDATE pending_reconfigs SET status = ? WHERE active_plan_id = ? AND status IN ('PENDING','VALIDATED')",
                ("FINALIZED", active_plan_id)
            )

            # Set global state
            self._set_state_on(con, target_state)

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
            self._record_transition(con, transition)
            con.commit()

        return transition

    def _transition_to_blocked(self, active_plan_state: Optional[ActivePlanState] = None) -> LifecycleTransition:
        """Transition to BLOCKED state."""
        current_state = self.get_current_state()

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

        with self._transaction() as con:
            # Idempotency: check for a duplicate BLOCK transition for the same
            # active plan. Scoping by plan_id keeps separate cycles independent.
            plan_id = active_plan_state.plan_id if active_plan_state is not None else None
            if self._is_duplicate_transition(
                con, current_state, LifecycleState.BLOCKED, LifecycleAction.BLOCK,
                plan_id=plan_id,
            ):
                existing = self._find_transition(
                    con, LifecycleAction.BLOCK, plan_id=plan_id,
                )
                if existing is not None:
                    con.commit()
                    return existing

            # Update active plan if present
            if active_plan_state is not None:
                con.execute(
                    "UPDATE active_plans SET lifecycle_state = ?, updated_at = ? WHERE plan_id = ?",
                    (LifecycleState.BLOCKED.value, now, active_plan_state.plan_id)
                )

            # Set global state to BLOCKED
            self._set_state_on(con, target_state)

            # Create transition
            transition = LifecycleTransition.create(
                current_state,
                target_state,
                LifecycleAction.BLOCK,
                {"active_plan_id": active_plan_state.plan_id if active_plan_state else None}
            )
            self._record_transition(con, transition)
            con.commit()

        return transition

    def _transition_to_keep_current(self, active_plan_state: ActivePlanState,
                                    adaptive_plan: AdaptiveGridPlan,
                                    candle_index: int = 0) -> LifecycleTransition:
        """Transition to KEEP_CURRENT_PLAN (no state change)."""
        current_state = self.get_current_state()

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

        with self._transaction() as con:
            self._record_transition(con, transition)
            con.commit()

        return transition

    def _transition_to_pending_reconfig(self, active_plan_state: ActivePlanState,
                                        adaptive_plan: AdaptiveGridPlan,
                                        candle_index: int = 0) -> LifecycleTransition:
        """Transition to RECONFIGURATION_PENDING.

        Candidate plans are globally single-use:
        - Re-pending the exact same (active_plan_id, candidate_plan_id) pair
          while it is still pending replays the existing transition (idempotent).
        - A candidate that was finalized, consumed, stale-rejected, or otherwise
          terminally processed is rejected and can never re-enter PENDING.
        """
        candidate_plan_id = adaptive_plan.plan_id
        current_state = self.get_current_state()

        con_check = connect(self.db_path)
        try:
            # Idempotent replay: the exact same pair is still pending and the
            # machine has not progressed past it.
            if current_state in (
                LifecycleState.RECONFIGURATION_PENDING,
                LifecycleState.READY_TO_RECONFIGURE,
            ):
                row = con_check.execute(
                    "SELECT status FROM pending_reconfigs "
                    "WHERE active_plan_id = ? AND candidate_plan_id = ? LIMIT 1",
                    (active_plan_state.plan_id, candidate_plan_id)
                ).fetchone()
                if row is not None and row["status"] in ("PENDING", "VALIDATED"):
                    existing = self._find_transition(
                        con_check, LifecycleAction.ENTER_PENDING,
                        plan_id=active_plan_state.plan_id,
                        candidate_plan_id=candidate_plan_id,
                    )
                    if existing is not None:
                        return existing

            # Single-use guard: any prior use of this candidate (including a
            # terminal FINALIZED row) forbids re-entering PENDING.
            used = con_check.execute(
                "SELECT 1 FROM pending_reconfigs WHERE candidate_plan_id = ? LIMIT 1",
                (candidate_plan_id,)
            ).fetchone()
            if used is not None:
                raise LifecycleError(
                    ValidationErrorCode.DUPLICATE_TRANSITION,
                    f"Candidate plan {candidate_plan_id} has already been used in a "
                    f"previous reconfiguration cycle; candidate plans are single-use"
                )

            # Stale / terminal candidate row: reject it.
            cand = con_check.execute(
                "SELECT lifecycle_state, generation FROM candidate_plans WHERE plan_id = ?",
                (candidate_plan_id,)
            ).fetchone()
            if cand is not None:
                max_gen = self._get_max_generation(con_check)
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
        finally:
            con_check.close()

        # Validate transition (fail-closed: e.g. ENTER_PENDING while PENDING is rejected)
        target_state = self._validate_transition(current_state, LifecycleAction.ENTER_PENDING)

        now = datetime.now(timezone.utc).isoformat()
        new_generation = self.get_generation() + 1

        try:
            with self._transaction() as con:
                # Re-check inside the lock: the same pair may have been recorded
                # between the early check and BEGIN IMMEDIATE.
                row = con.execute(
                    "SELECT status FROM pending_reconfigs "
                    "WHERE active_plan_id = ? AND candidate_plan_id = ? LIMIT 1",
                    (active_plan_state.plan_id, candidate_plan_id)
                ).fetchone()
                if row is not None and row["status"] in ("PENDING", "VALIDATED"):
                    existing = self._find_transition(
                        con, LifecycleAction.ENTER_PENDING,
                        plan_id=active_plan_state.plan_id,
                        candidate_plan_id=candidate_plan_id,
                    )
                    con.commit()
                    if existing is not None:
                        return existing

                # Single-use guard re-checked under the lock.
                used = con.execute(
                    "SELECT 1 FROM pending_reconfigs WHERE candidate_plan_id = ? LIMIT 1",
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
                con.execute(
                    """
                    INSERT INTO candidate_plans (
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
                con.execute(
                    """
                    INSERT INTO pending_reconfigs (
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
                con.execute(
                    "UPDATE active_plans SET lifecycle_state = ?, updated_at = ? WHERE plan_id = ?",
                    (LifecycleState.RECONFIGURATION_PENDING.value, now, active_plan_state.plan_id)
                )

                # Set global state
                self._set_state_on(con, target_state)

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
                self._record_transition(con, transition)
                con.commit()
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

    def _record_transition(self, con, transition: LifecycleTransition) -> None:
        """Insert a transition record (idempotent via transition_id PK)."""
        con.execute(
            "INSERT OR IGNORE INTO lifecycle_transitions "
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
                                 generation: Optional[int] = None) -> bool:
        """Check whether an identical transition for the same lifecycle identity
        has already been recorded.

        Scope is ``(from_state, to_state, action)`` plus optional identity
        fields (plan_id / candidate_plan_id / generation). Without the identity
        fields transitions from different plans or reconfiguration cycles would
        incorrectly look like duplicates of one another.
        """
        sql = ("SELECT 1 FROM lifecycle_transitions "
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
                         candidate_plan_id: Optional[str] = None
                         ) -> Optional[LifecycleTransition]:
        """Return the most recent recorded transition matching action + identity."""
        sql = ("SELECT transition_id, from_state, to_state, action, timestamp, details "
               "FROM lifecycle_transitions WHERE action = ?")
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

    def _is_duplicate_plan(self, con, plan_id: str) -> bool:
        """Check whether the plan already exists in active_plans."""
        row = con.execute(
            "SELECT 1 FROM active_plans WHERE plan_id = ? LIMIT 1",
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
    def _transaction(self):
        """Context manager for database transactions.

        Provides a connection with BEGIN IMMEDIATE. Caller must call
        con.commit() explicitly. On exception, rollback is automatic.
        """
        con = connect(self.db_path)
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            # Do NOT auto-commit; caller must call con.commit() explicitly
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()


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
            "grid": {"step_pct": Decimal("0.006"), "hard_min_net_pct": Decimal("0.003")},
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