"""Risk engine: veto authority over every order.

- Global equity drawdown kill switch (hard limit 2%, `>=` triggers).
- 15m lower-boundary protection on the latest CLOSED 15m candle close
  (never an intrabar wick); fail-closed: missing/invalid candle data is
  reported as UNKNOWN and blocks new orders instead of guessing.
- Kill state and per-symbol stops are persisted and never auto-reset.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

OK = "ok"
BREACH = "breach"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: Optional[str] = None


class RiskEngine:
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store

    def order_veto(self, symbol: str) -> RiskDecision:
        """Veto any order placement while the global kill is active or the
        symbol is risk-stopped."""
        kill_active, kill_reason = self.store.global_kill()
        if kill_active:
            return RiskDecision(False, f"global_kill:{kill_reason}")
        sym = self.store.get_symbol(symbol)
        if sym is not None and sym.risk_status == "stopped":
            return RiskDecision(False, "symbol_stopped")
        return RiskDecision(True, None)

    def drawdown_breach(self, equity: float, reference: float) -> bool:
        """Global drawdown >= configured hard limit (2%) triggers the kill."""
        if reference is None or reference <= 0:
            return False
        drawdown = (reference - equity) / reference
        return drawdown >= self.cfg.max_drawdown

    def boundary_status(self, close_15m: Optional[float], lower_price: Optional[float]) -> str:
        """Evaluate the 15m lower-boundary gate against the latest CLOSED
        15m candle close. Returns OK, BREACH or UNKNOWN (fail-closed on
        missing/invalid data — never guesses)."""
        if close_15m is None or lower_price is None:
            return UNKNOWN
        if close_15m <= 0 or lower_price <= 0:
            return UNKNOWN
        if close_15m <= lower_price * (1.0 - self.cfg.stop_if_below_lower):
            return BREACH
        return OK

    def trigger_global_kill(self, reason: str) -> None:
        self.store.set_global_kill(reason)
        self.store.add_risk_event("global", "kill_switch", reason)

    def stop_symbol(self, symbol: str, reason: str) -> None:
        self.store.stop_symbol(symbol, reason)
        self.store.add_risk_event(symbol, "symbol_stop", reason)
