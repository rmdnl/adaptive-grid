"""Per-symbol strategy state machine (locked specification, section 24).

Explicit states:

    WAITING_FOR_ENTRY, ENTRY_SIGNAL, DEPLOYING_GRID, GRID_ACTIVE,
    EXIT_SIGNAL, AUTO_EXIT, LIQUIDATING, COOLDOWN, BLOCKED, ERROR

Rules:
- Invalid transitions are REJECTED (the state does not change and the
  rejection is reported) — the state machine can never be walked into an
  undefined situation.
- The state persists in the symbol's ``bot_state`` (survives process,
  systemd, and VPS restarts).
- Restart recovery is re-derivation: each cycle proposes a target state
  from the real persisted situation (active plan? cooldown? risk
  decision?); the transition table validates the jump — including
  documented recovery edges out of ERROR/GRID_ACTIVE.
- ERROR is recoverable only through re-derivation on a later cycle; it is
  always visible in the dashboard.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from storage import get_state, set_state


class StrategyState(str, Enum):
    WAITING_FOR_ENTRY = "WAITING_FOR_ENTRY"
    ENTRY_SIGNAL = "ENTRY_SIGNAL"
    DEPLOYING_GRID = "DEPLOYING_GRID"
    GRID_ACTIVE = "GRID_ACTIVE"
    EXIT_SIGNAL = "EXIT_SIGNAL"
    AUTO_EXIT = "AUTO_EXIT"
    LIQUIDATING = "LIQUIDATING"
    COOLDOWN = "COOLDOWN"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


_VALID_TRANSITIONS: dict[StrategyState, set[StrategyState]] = {
    StrategyState.WAITING_FOR_ENTRY: {
        StrategyState.WAITING_FOR_ENTRY, StrategyState.ENTRY_SIGNAL,
        StrategyState.COOLDOWN, StrategyState.BLOCKED, StrategyState.ERROR},
    StrategyState.ENTRY_SIGNAL: {
        StrategyState.DEPLOYING_GRID, StrategyState.WAITING_FOR_ENTRY,
        StrategyState.BLOCKED, StrategyState.ERROR},
    StrategyState.DEPLOYING_GRID: {
        StrategyState.GRID_ACTIVE, StrategyState.BLOCKED, StrategyState.ERROR},
    # Staying in GRID_ACTIVE is a valid self-transition (candle with no
    # exit signal).  WAITING_FOR_ENTRY covers plan closure without an exit
    # signal (e.g. operator action or liquidation completed elsewhere).
    StrategyState.GRID_ACTIVE: {
        StrategyState.GRID_ACTIVE, StrategyState.EXIT_SIGNAL,
        StrategyState.WAITING_FOR_ENTRY, StrategyState.BLOCKED,
        StrategyState.ERROR},
    StrategyState.EXIT_SIGNAL: {
        StrategyState.AUTO_EXIT, StrategyState.ERROR},
    StrategyState.AUTO_EXIT: {
        StrategyState.LIQUIDATING, StrategyState.ERROR},
    StrategyState.LIQUIDATING: {
        StrategyState.COOLDOWN, StrategyState.ERROR},
    StrategyState.COOLDOWN: {
        StrategyState.COOLDOWN, StrategyState.WAITING_FOR_ENTRY,
        StrategyState.ENTRY_SIGNAL, StrategyState.BLOCKED,
        StrategyState.ERROR},
    StrategyState.BLOCKED: {
        StrategyState.BLOCKED, StrategyState.WAITING_FOR_ENTRY,
        StrategyState.COOLDOWN, StrategyState.ENTRY_SIGNAL,
        StrategyState.GRID_ACTIVE, StrategyState.ERROR},
    # ERROR recovers only through re-derivation on a later cycle.
    StrategyState.ERROR: {
        StrategyState.ERROR, StrategyState.WAITING_FOR_ENTRY,
        StrategyState.GRID_ACTIVE, StrategyState.BLOCKED,
        StrategyState.COOLDOWN},
}

_STATE_KEY = "strategy_state"


class StrategyStateTracker:
    """Persisted, transition-validated per-symbol strategy state."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def current(self) -> StrategyState:
        raw = get_state(self.db_path, _STATE_KEY)
        if raw is None:
            return StrategyState.WAITING_FOR_ENTRY
        try:
            return StrategyState(str(raw))
        except ValueError:
            return StrategyState.ERROR

    def current_detail(self) -> Optional[dict[str, Any]]:
        raw = get_state(self.db_path, _STATE_KEY + ":detail")
        if raw is None:
            return None
        try:
            import json
            value = json.loads(raw)
            return value if isinstance(value, dict) else None
        except (ValueError, TypeError):
            return None

    def transition(self, target: StrategyState, reason: str = "",
                   detail: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Propose a transition; invalid proposals are rejected, not applied.

        Returns ``{"applied": bool, "from": ..., "to": ..., "rejected": ...}``.
        """
        if not isinstance(target, StrategyState):
            raise TypeError("target must be a StrategyState")
        current = self.current()
        now = datetime.now(timezone.utc).isoformat()
        if target not in _VALID_TRANSITIONS[current]:
            report = {
                "applied": False, "from": current.value, "to": target.value,
                "rejected": True, "reason": reason,
                "timestamp": now,
            }
            self._persist(current, reason + " [transition rejected: "
                          f"{current.value} -> {target.value}]", detail, now)
            return report
        self._persist(target, reason, detail, now)
        return {"applied": True, "from": current.value, "to": target.value,
                "rejected": False, "reason": reason, "timestamp": now}

    def _persist(self, state: StrategyState, reason: str,
                 detail: Optional[dict[str, Any]], now: str) -> None:
        payload: dict[str, Any] = {
            "state": state.value,
            "reason": reason,
            "updated_at": now,
        }
        if detail:
            payload["detail"] = detail
        set_state(self.db_path, _STATE_KEY, state.value)
        import json
        set_state(self.db_path, _STATE_KEY + ":detail",
                  json.dumps(payload, default=str))


__all__ = ["StrategyState", "StrategyStateTracker"]
