"""Global account-level risk: shared equity, reference, and 2% drawdown kill.

Per-symbol strategy state is independent, but account risk is GLOBAL
(locked specification section 17):

    MAX_DRAWDOWN_PERCENT = 2.0%  (non-negotiable)

- Global equity = free/locked USDT + sum(base asset totals x price) across
  ALL configured symbols, computed once per multi-symbol pass.
- The reference equity is a high-water-mark persisted in the configured
  base database; it is NEVER reset automatically (only the operator
  reset script may clear it, with an audit row).
- When drawdown >= 2.0% the GLOBAL kill switch latches in the base
  database: no new grid deployment for ANY symbol, open orders canceled
  across ALL symbols, and the latch survives restart.  Automatic release
  and automatic reference-equity reset are forbidden — operator action
  only.
- Fail-closed: if global equity cannot be reliably determined while a
  reference exists, the drawdown is unknown and trading must stop
  (``available=False`` blocks every symbol for that pass).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from storage import (
    get_kill_state,
    get_state,
    record_reference_equity_audit,
    set_kill_state,
    set_state,
)

#: Locked, non-negotiable global drawdown limit (fraction).
GLOBAL_MAX_DRAWDOWN_PCT = Decimal("0.02")

_REFERENCE_KEY = "global_reference_equity"
_STATE_KEY = "global_risk_state"


def _decimal(value: Any) -> Optional[Decimal]:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def load_reference(db_path: str) -> Optional[Decimal]:
    """Persisted global reference equity; None when absent/corrupt."""
    raw = get_state(db_path, _REFERENCE_KEY)
    if raw is None:
        return None
    return _decimal(raw)


def update_reference(db_path: str, equity: Decimal) -> Decimal:
    """Raise the global high-water-mark; never lower it.  Returns current."""
    value = Decimal(str(equity))
    if not value.is_finite() or value <= 0:
        return load_reference(db_path) or Decimal("0")
    previous = load_reference(db_path)
    if previous is None or value > previous:
        previous_raw = get_state(db_path, _REFERENCE_KEY)
        set_state(db_path, _REFERENCE_KEY, str(value))
        record_reference_equity_audit(
            db_path,
            previous_value=previous_raw,
            new_value=str(value),
            reason="global reference high-water-mark update",
            actor="global_risk",
        )
        return value
    return previous


def is_killed(db_path: str) -> bool:
    """True when the GLOBAL kill switch is latched in the base database."""
    kill = get_kill_state(db_path)
    return bool(kill and kill.get("active"))


def evaluate(db_path: str, equity: Optional[Decimal],
             max_drawdown_pct: Decimal = GLOBAL_MAX_DRAWDOWN_PCT) -> dict[str, Any]:
    """Evaluate the global drawdown and latch the kill when breached.

    Boundary semantics (tested): drawdown < max -> allowed;
    drawdown == max -> GLOBAL KILL; drawdown > max -> GLOBAL KILL.

    Fail-closed: ``equity=None`` (or invalid) with an existing reference
    means UNKNOWN drawdown -> blocked, never allowed.  A previously latched
    kill stays latched: evaluation never releases it.
    """
    reference = load_reference(db_path)
    now = datetime.now(timezone.utc).isoformat()
    already_killed = is_killed(db_path)
    if equity is None or not _decimal(equity):
        decision = {
            "available": False,
            "allowed": False,
            "reason": ("GLOBAL_KILL_ACTIVE" if already_killed
                       else "GLOBAL_EQUITY_UNAVAILABLE"),
            "equity": None,
            "reference": str(reference) if reference is not None else None,
            "drawdown_pct": None,
            "kill_triggered": already_killed,
            "timestamp": now,
        }
        set_state(db_path, _STATE_KEY, decision)
        return decision

    value = Decimal(str(equity))
    if value < 0:
        decision = {
            "available": False, "allowed": False,
            "reason": "GLOBAL_EQUITY_INVALID", "equity": str(value),
            "reference": str(reference) if reference is not None else None,
            "drawdown_pct": None, "kill_triggered": False, "timestamp": now,
        }
        set_state(db_path, _STATE_KEY, decision)
        return decision

    if reference is None:
        # Bootstrap: first valid observation becomes the reference.
        reference = update_reference(db_path, value)
    if value > reference:
        reference = update_reference(db_path, value)

    drawdown = (reference - value) / reference if reference > 0 else Decimal("0")
    kill_triggered = drawdown >= Decimal(str(max_drawdown_pct))
    if kill_triggered and not is_killed(db_path):
        set_kill_state(
            db_path,
            active=True,
            trigger="GLOBAL_EQUITY_DRAWDOWN_KILL",
            cancel_status="PENDING_CANCELS",
            note=f"global drawdown {drawdown} >= {max_drawdown_pct} "
                 f"(equity={value}, reference={reference})",
        )
    kill_triggered = kill_triggered or already_killed
    decision = {
        "available": True,
        "allowed": not kill_triggered,
        "reason": ("PASS" if not kill_triggered
                   else "GLOBAL_EQUITY_DRAWDOWN_KILL"),
        "equity": str(value),
        "reference": str(reference),
        "drawdown_pct": str(drawdown),
        "kill_triggered": kill_triggered,
        "timestamp": now,
    }
    set_state(db_path, _STATE_KEY, decision)
    return decision


__all__ = [
    "GLOBAL_MAX_DRAWDOWN_PCT",
    "evaluate",
    "is_killed",
    "load_reference",
    "update_reference",
]
