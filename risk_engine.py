from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reason(self) -> str:
        return "PASS" if self.allowed else " | ".join(self.reasons)


def D(value) -> Decimal:
    return Decimal(str(value))


def profit_gate(net_pct, hard_min) -> RiskDecision:
    ok = D(net_pct) >= D(hard_min)
    return RiskDecision(ok, () if ok else ("NET_PROFIT_BELOW_HARD_MIN",))


def market_gate(last_row: Any, cfg: dict) -> RiskDecision:
    reasons = []
    try:
        checks = {
            "ADX": float(last_row["adx"]) <= float(cfg["adx_max"]),
            "ATR": float(last_row["atr_pct"]) <= float(cfg["atr_pct_max"]),
            "BB": float(last_row["bb_width"]) <= float(cfg["bb_width_max"]),
            "VOLUME": float(last_row["volume_ratio"]) <= float(cfg["volume_spike_max"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        return RiskDecision(False, (f"MARKET_DATA_INVALID:{exc}",))

    reasons.extend(f"MARKET_FILTER_BLOCK:{k}" for k, ok in checks.items() if not ok)
    return RiskDecision(not reasons, tuple(reasons))


def equity_dd_kill(drawdown_pct, max_dd_pct) -> RiskDecision:
    dd = D(drawdown_pct)
    limit = D(max_dd_pct)
    if dd >= limit:
        return RiskDecision(False, ("EQUITY_DRAWDOWN_KILL",))
    return RiskDecision(True)


def range_gate(lower, upper, price, buffer_pct) -> RiskDecision:
    lo, hi, px, buf = map(D, (lower, upper, price, buffer_pct))
    if px < lo * (D("1") - buf):
        return RiskDecision(False, ("PRICE_BELOW_RANGE_BUFFER",))
    if px > hi * (D("1") + buf):
        return RiskDecision(False, ("PRICE_ABOVE_RANGE_BUFFER",))
    return RiskDecision(True)


def inventory_gate(inventory_pct, max_inventory_pct) -> RiskDecision:
    if D(inventory_pct) > D(max_inventory_pct):
        return RiskDecision(False, ("MAX_INVENTORY_EXCEEDED",))
    return RiskDecision(True)


def open_orders_gate(open_orders: int, max_open_orders: int) -> RiskDecision:
    if int(open_orders) >= int(max_open_orders):
        return RiskDecision(False, ("MAX_OPEN_ORDERS_REACHED",))
    return RiskDecision(True)


def cooldown_gate(active: bool) -> RiskDecision:
    return RiskDecision(False, ("COOLDOWN_ACTIVE",)) if active else RiskDecision(True)


def combine(*decisions: RiskDecision) -> RiskDecision:
    reasons: list[str] = []
    for decision in decisions:
        if not decision.allowed:
            reasons.extend(decision.reasons)
    return RiskDecision(not reasons, tuple(reasons))


def daily_profit_lock(daily_pnl_pct, lock_pct) -> RiskDecision:
    if D(daily_pnl_pct) >= D(lock_pct):
        return RiskDecision(False, ("DAILY_PROFIT_LOCK",))
    return RiskDecision(True)
