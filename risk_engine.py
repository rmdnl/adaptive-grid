from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    @property
    def reason(self):
        return "PASS" if self.allowed else " | ".join(self.reasons)

def D(value): return Decimal(str(value))

def profit_gate(net_pct, hard_min):
    ok = D(net_pct) >= D(hard_min)
    return RiskDecision(ok, () if ok else ("NET_PROFIT_BELOW_HARD_MIN",))

def market_gate(last_row: Any, cfg: dict):
    try:
        checks = {
            "ADX": float(last_row["adx"]) <= float(cfg["adx_max"]),
            "ATR": float(last_row["atr_pct"]) <= float(cfg["atr_pct_max"]),
            "BB": float(last_row["bb_width"]) <= float(cfg["bb_width_max"]),
            "VOLUME": float(last_row["volume_ratio"]) <= float(cfg["volume_spike_max"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        return RiskDecision(False, (f"MARKET_DATA_INVALID:{exc}",))
    reasons = tuple(f"MARKET_FILTER_BLOCK:{k}" for k, ok in checks.items() if not ok)
    return RiskDecision(not reasons, reasons)

def equity_dd_kill(drawdown_pct, max_dd_pct):
    return RiskDecision(False, ("EQUITY_DRAWDOWN_KILL",)) if D(drawdown_pct) >= D(max_dd_pct) else RiskDecision(True)

def strict_order_price_gate(lower, upper, price):
    lo, hi, px = map(D, (lower, upper, price))
    if px < lo:
        return RiskDecision(False, ("ORDER_PRICE_BELOW_RANGE",))
    if px > hi:
        return RiskDecision(False, ("ORDER_PRICE_ABOVE_RANGE",))
    return RiskDecision(True)

def range_break_kill(lower, upper, price, buffer_pct):
    lo, hi, px, buf = map(D, (lower, upper, price, buffer_pct))
    if px < lo*(D("1")-buf):
        return RiskDecision(False, ("RANGE_BREAK_BELOW_BUFFER",))
    if px > hi*(D("1")+buf):
        return RiskDecision(False, ("RANGE_BREAK_ABOVE_BUFFER",))
    return RiskDecision(True)

def range_gate(lower, upper, price, buffer_pct):
    # Backward-compatible alias for the old buffer gate.
    return range_break_kill(lower, upper, price, buffer_pct)

def inventory_gate(inventory_pct, max_inventory_pct):
    return RiskDecision(False, ("MAX_INVENTORY_EXCEEDED",)) if D(inventory_pct) > D(max_inventory_pct) else RiskDecision(True)

def open_orders_gate(open_orders, max_open_orders):
    return RiskDecision(False, ("MAX_OPEN_ORDERS_REACHED",)) if int(open_orders) >= int(max_open_orders) else RiskDecision(True)

def cooldown_gate(active):
    return RiskDecision(False, ("COOLDOWN_ACTIVE",)) if active else RiskDecision(True)

def daily_profit_lock(daily_pnl_pct, lock_pct):
    return RiskDecision(False, ("DAILY_PROFIT_LOCK",)) if D(daily_pnl_pct) >= D(lock_pct) else RiskDecision(True)

def combine(*decisions):
    reasons = []
    for decision in decisions:
        if not decision.allowed:
            reasons.extend(decision.reasons)
    return RiskDecision(not reasons, tuple(reasons))
