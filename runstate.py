"""Roadmap G: durable run-state checkpoint + restart-recovery hardening.

Each completed bot run persists a small structured ``last_run_state`` marker
(bot_state KV) so the next process start can verify a clean hand-off and
detect interrupted work.  The marker is intentionally minimal and
deterministic (no wall-clock timestamps) so two identical runs produce
byte-identical state.

Safety:
* Read/write is confined to the ``bot_state`` key ``last_run_state`` plus a
  read-only kill-latch and cancel-record check.  No order, accounting, or
  risk state is mutated here.
* A run that dies mid-phase never writes a COMPLETED marker; the next
  restart therefore treats the previous run as INTERRUPTED and re-runs the
  full reconciliation (the orchestrator's cycle transaction is all-or-
  nothing, so an interrupted cycle is already rolled back on disk).
* The kill latch is the authoritative stop signal: restart verification
  re-enters the kill branch before any market data or order planning.
"""
from __future__ import annotations

import json
from typing import Optional

from storage import get_state, set_state

RUN_STATE_KEY = "last_run_state"
PHASE_COMPLETED = "COMPLETED"
PHASE_INTERRUPTED = "INTERRUPTED"


class RestartRecoveryError(RuntimeError):
    """Raised when restart verification cannot establish a safe state."""


def persist_run_state(
    db_path,
    *,
    run_id: str,
    completed: bool,
    risk_allowed: bool,
    kill_active: bool,
    pending_cancels: int,
    open_orders: int,
) -> None:
    """Write the structured run-complete / interrupted marker.

    ``completed=False`` is written explicitly when a shutdown signal is
    received after the read phase (so a restart knows the previous run did
    NOT finish) and is the natural state when the process was killed
    mid-run (no marker exists from that run; the PREVIOUS run's marker,
    if any, is read first by :func:`read_run_state`).
    """
    payload = {
        "run_id": str(run_id),
        "phase": PHASE_COMPLETED if completed else PHASE_INTERRUPTED,
        "risk_allowed": bool(risk_allowed),
        "kill_active": bool(kill_active),
        "pending_cancels": int(pending_cancels),
        "open_orders": int(open_orders),
    }
    set_state(db_path, RUN_STATE_KEY, json.dumps(payload, sort_keys=True, separators=(",", ":")))


def read_run_state(db_path) -> Optional[dict]:
    """Read the previous run's marker; ``None`` when it does not exist."""
    raw = get_state(db_path, RUN_STATE_KEY)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def has_paper_activity(db_path) -> bool:
    """True when the DB contains ANY persisted paper activity.

    A fresh/empty database has none of these; that is the normal first-run
    case and is safe to resume.  Corruption is only meaningful when there is
    prior activity that no longer reconciles.
    """
    from storage import connect

    con = connect(db_path)
    try:
        # ANY row in these tables means a prior run left state that must
        # reconcile.  Each table is checked for existence first so a DB that
        # has not been fully initialised does not error.
        tables = ("orders", "fills", "paper_reservations",
                  "paper_account_state", "paper_accounting_events")
        for table in tables:
            exists = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if exists is None:
                continue
            if con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
                return True
        return False
    finally:
        con.close()


def verify_restart_safety(db_path, recovery=None) -> dict:
    """Restart-time verification that the persisted state is safe to continue.

    Checks, in order (fail-closed):
    1. Paper-state reconciliation health (read-only ``recover_paper_state``
       when ``recovery`` is not supplied).  A genuinely empty database
       (no prior paper activity) is treated as a clean first run and is safe
       to resume; an unhealthy DB that HAS activity is refused.
    2. The persisted kill latch — if active, the run must stay in the kill
       branch; no new orders may be planned.
    3. The previous run marker — a completed marker with no pending
       reconciliations is the normal RESUME case; an INTERRUPTED marker, or a
       missing marker WITH prior activity, means the previous run was cut
       off and full reconciliation must re-run before planning new orders.

    Returns a structured verdict:
    ``{"previous_run", "recovery_healthy", "kill_active",
    "safe_to_continue", "restart_action"}`` where ``restart_action`` is one
    of ``RESUME``, ``RECONCILE_THEN_RESUME``, ``KILL_BRANCH``,
    ``REFUSE`` (corrupt state with activity).
    """
    from recovery import recover_paper_state
    from storage import get_kill_state

    if recovery is None:
        recovery = recover_paper_state(db_path)

    marker = read_run_state(db_path)
    prev_phase = (marker or {}).get("phase")
    kill = get_kill_state(db_path)
    kill_active = bool(kill and kill.get("active"))

    has_activity = has_paper_activity(db_path)

    # A fresh DB with no activity is healthy-by-construction even though
    # recover_paper_state reports unhealthy (no account row yet).
    if not recovery.healthy and not has_activity:
        effective_healthy = True
    else:
        effective_healthy = bool(recovery.healthy)

    if not effective_healthy:
        action = "REFUSE"
    elif kill_active:
        action = "KILL_BRANCH"
    elif prev_phase == PHASE_COMPLETED:
        action = "RESUME"
    elif not has_activity:
        # Fresh DB, no prior run marker, nothing to reconcile: clean start.
        action = "RESUME"
    else:
        # Missing or INTERRUPTED marker with prior activity: the previous run
        # was cut off.  The orchestrator's cycle transaction already rolled
        # back any partial work, but we must re-run reconciliation before
        # planning new orders.
        action = "RECONCILE_THEN_RESUME"

    return {
        "previous_run": {
            "present": marker is not None,
            "phase": prev_phase,
            "run_id": (marker or {}).get("run_id"),
        },
        "recovery_healthy": effective_healthy,
        "recovery_raw_healthy": bool(recovery.healthy),
        "recovery_errors": [str(e) for e in recovery.errors],
        "has_paper_activity": has_activity,
        "kill_active": kill_active,
        "safe_to_continue": action in {"RESUME", "KILL_BRANCH", "RECONCILE_THEN_RESUME"},
        "restart_action": action,
    }


def mark_run_interrupted(db_path, run_id: str) -> None:
    """Explicit operator/system call when a run is knowingly stopped early.

    Written by the graceful-shutdown path so the next restart sees a
    deterministic INTERRUPTED marker instead of a missing one.
    """
    set_state(
        db_path,
        RUN_STATE_KEY,
        json.dumps(
            {
                "run_id": str(run_id),
                "phase": PHASE_INTERRUPTED,
                "risk_allowed": False,
                "kill_active": False,
                "pending_cancels": 0,
                "open_orders": 0,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


__all__ = [
    "RUN_STATE_KEY",
    "PHASE_COMPLETED",
    "PHASE_INTERRUPTED",
    "RestartRecoveryError",
    "persist_run_state",
    "read_run_state",
    "verify_restart_safety",
    "mark_run_interrupted",
    "has_paper_activity",
]
