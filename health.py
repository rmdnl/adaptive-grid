"""Roadmap G: structured health / status reporting.

Read-only observability layer over the bot's persisted state.  It never feeds
back into order placement: the report is built from storage reads (bot_state,
kill_state, recovery) plus the validated config flags, and is consumed by
operators (``scripts/status_report.py``) and by ``main.py``'s structured
run-complete log line.

Safety:
* No trading logic and no order placement.
* No secrets: only the config safety flags (mode / dry_run /
  allow_live_execution), kill state, recovery diagnostics, local order
  counts, and the last persisted risk / plan / cycle decisions (written by
  the risk engine and the orchestrator, never by this module).
* Deterministic: ``as_json()`` uses ``sort_keys=True`` and omits wall-clock
  time, so two identical states produce byte-identical payloads.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Optional

from storage import get_state


class HealthStatus(str, Enum):
    """Operational status of a single bot run / persisted state snapshot."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"        # unreconciled cancel records / corrupt reference
    KILLED = "KILLED"            # kill latch active
    UNHEALTHY = "UNHEALTHY"      # paper-state reconciliation failed
    UNSAFE_CONFIG = "UNSAFE_CONFIG"  # not dry-run, or live execution enabled

    def exit_code(self) -> int:
        # 0 = healthy; 1 = degraded/killed/unhealthy (still reportable, safe);
        # 2 = unsafe configuration (refuse to operate).
        return {"HEALTHY": 0}.get(self.value, 2 if self is HealthStatus.UNSAFE_CONFIG else 1)


def _parse_reference(db_path: str) -> dict:
    """Reference-equity validity, mirroring main.load_peak_equity's parse.

    Returns ``{"present", "valid", "value"}`` where ``value`` is the string
    form when valid, else None.
    """
    raw = get_state(db_path, "paper_reference_equity")
    if raw is None:
        return {"present": False, "valid": True, "value": None}
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError, TypeError):
        return {"present": True, "valid": False, "value": None}
    if not value.is_finite() or value <= 0:
        return {"present": True, "valid": False, "value": None}
    return {"present": True, "valid": True, "value": str(value)}


def _open_orders(db_path: str) -> dict:
    """Local paper order counts by status (open states are the risk-relevant
    ones; terminal states are reported for completeness)."""
    from storage import connect

    con = connect(db_path)
    try:
        rows = con.execute(
            "SELECT status, COUNT(*) AS n FROM orders GROUP BY status"
        ).fetchall()
    finally:
        con.close()
    counts = {r["status"]: r["n"] for r in rows}
    open_states = ("OPEN", "PARTIALLY_FILLED")
    open_total = sum(counts.get(s, 0) for s in open_states)
    return {"open": open_total, "by_status": counts}


def _pending_cancels(db_path: str) -> list[str]:
    """Client-order-ids with an unreconciled cancel record that still have a
    live local order.  These keep the kill state PENDING_RECONCILIATION."""
    from storage import connect

    con = connect(db_path)
    try:
        rows = con.execute(
            "SELECT cr.client_order_id FROM cancel_records cr "
            "JOIN orders o ON o.client_order_id = cr.client_order_id "
            "WHERE cr.reconciled = 0 AND o.status IN ('OPEN','PARTIALLY_FILLED')"
        ).fetchall()
    finally:
        con.close()
    return sorted(r["client_order_id"] for r in rows)


def _load_json_state(db_path: str, key: str) -> Optional[dict]:
    raw = get_state(db_path, key)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def classify_health(env: dict, kill_active: bool, recovery_healthy: bool,
                   reference: dict, pending_cancels: list[str]) -> HealthStatus:
    """Fail-closed status classification.  Order of precedence matters:
    an unsafe config dominates everything else, then the kill latch, then
    reconciliation health, then degraded diagnostics."""
    dry_run = env.get("dry_run")
    allow_live = env.get("allow_live_execution", False)
    if not dry_run or allow_live:
        return HealthStatus.UNSAFE_CONFIG
    if kill_active:
        return HealthStatus.KILLED
    if not recovery_healthy:
        return HealthStatus.UNHEALTHY
    if not reference.get("valid") or pending_cancels:
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY


@dataclass(frozen=True)
class HealthReport:
    status: HealthStatus
    environment: dict
    kill_state: Optional[dict]
    recovery: dict
    reference_equity: dict
    open_orders: dict
    pending_cancels: tuple[str, ...]
    last_risk_decision: Optional[dict]
    last_paper_cycle: Optional[dict]
    run_state: Optional[dict]

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "environment": self.environment,
            "kill_state": self.kill_state,
            "recovery": self.recovery,
            "reference_equity": self.reference_equity,
            "open_orders": self.open_orders,
            "pending_cancels": list(self.pending_cancels),
            "last_risk_decision": self.last_risk_decision,
            "last_paper_cycle": self.last_paper_cycle,
            "run_state": self.run_state,
        }

    def as_json(self) -> str:
        """Deterministic single-line JSON (sorted keys, no wall-clock time)."""
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"), default=str)


def collect_health_report(
    db_path: str,
    cfg: dict,
    recovery=None,
) -> HealthReport:
    """Build a read-only health snapshot.

    ``recovery`` is an optional precomputed ``RecoveryResult``; when omitted
    the module runs a read-only ``recover_paper_state`` (safe: it never
    mutates the database).  Passing it explicitly keeps tests deterministic.
    """
    from recovery import recover_paper_state
    from runstate import has_paper_activity
    from storage import get_kill_state

    if recovery is None:
        recovery = recover_paper_state(db_path)

    env = cfg.get("environment", {})
    reference = _parse_reference(db_path)
    pending = _pending_cancels(db_path)
    kill = get_kill_state(db_path)
    kill_active = bool(kill and kill.get("active"))

    # A fresh DB with no paper activity is healthy-by-construction even when
    # recover_paper_state reports unhealthy (no account row yet).  This mirrors
    # runstate.verify_restart_safety so the two views never disagree.
    if not recovery.healthy and not has_paper_activity(db_path):
        effective_healthy = True
    else:
        effective_healthy = bool(recovery.healthy)

    status = classify_health(
        env=env,
        kill_active=kill_active,
        recovery_healthy=effective_healthy,
        reference=reference,
        pending_cancels=pending,
    )
    return HealthReport(
        status=status,
        environment={
            "mode": str(env.get("mode", "")),
            "dry_run": bool(env.get("dry_run")),
            "allow_live_execution": bool(env.get("allow_live_execution", False)),
        },
        kill_state=kill,
        recovery={
            "healthy": effective_healthy,
            "raw_healthy": bool(recovery.healthy),
            "errors": [str(e) for e in recovery.errors],
            "warnings": [str(w) for w in recovery.warnings],
        },
        reference_equity=reference,
        open_orders=_open_orders(db_path),
        pending_cancels=tuple(pending),
        last_risk_decision=_load_json_state(db_path, "last_risk_decision"),
        last_paper_cycle=_load_json_state(db_path, "last_paper_orders"),
        run_state=_load_json_state(db_path, "last_run_state"),
    )


def write_health_jsonl(log_path, payload: dict) -> Path:
    """Append one structured JSON line to the log sidecar (``<log>.jsonl``).

    Sidecar keeps the human-readable ``<log>`` file free of JSON noise; the
    jsonl stream is machine-parseable for monitoring.  Never raises on a
    missing parent directory (created on demand).
    """
    target = Path(log_path)
    jsonl_path = target.with_suffix(".jsonl")
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str) + "\n")
    return jsonl_path


__all__ = [
    "HealthStatus",
    "HealthReport",
    "classify_health",
    "collect_health_report",
    "write_health_jsonl",
]
