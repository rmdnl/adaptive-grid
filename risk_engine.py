from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
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

def equity_reference_gate(raw_reference, parsed_reference):
    """Fail-closed gate on the persisted reference/peak equity (PATCH 1 / F-H1).

    ``raw_reference`` is the raw stored value (None when the key is absent) and
    ``parsed_reference`` is the parsed Decimal (None when absent or unparseable).
    An ABSENT reference is not a block — the caller bootstraps it from the first
    observed equity.  A PRESENT-but-unparsable reference is corrupt state and
    blocks new submissions rather than silently being reset to the current
    equity, so the kill switch cannot be defeated by a bad value on disk.
    """
    if raw_reference is None:
        return RiskDecision(True)
    if parsed_reference is None:
        return RiskDecision(False, ("EQUITY_REFERENCE_INVALID",))
    return RiskDecision(True)

def account_state_gate(available):
    return RiskDecision(True) if available else RiskDecision(False, ("ACCOUNT_DATA_UNAVAILABLE",))

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

def _finite_positive_decimal(value) -> Decimal | None:
    """Parse ``value`` to a finite positive Decimal; ``None`` when invalid.

    Rejects None, non-numeric values, NaN, infinity, zero, and negatives.
    This is the fail-closed input check shared by the 15m lower-boundary
    kill gate.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = D(value)
    except (InvalidOperation, ValueError, TypeError, ArithmeticError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed

def _stop_pct_valid(value) -> Decimal | None:
    """Parse a stop-if-below-lower percentage to a finite Decimal in (0, 1).

    ``0`` disables the stop entirely (dangerous) and ``>= 1`` makes the
    threshold non-positive (nonsensical); both are rejected so the gate
    fails closed instead of weakening itself.
    """
    parsed = _finite_positive_decimal(value)
    if parsed is None or parsed >= 1:
        return None
    return parsed

def lower_boundary_15m_kill(closed_candle_close, lower_price, stop_if_below_lower_pct):
    """Dedicated 15-minute candle-close lower-boundary kill.

    Independent of the current-price range-break protection
    (:func:`range_break_kill`), this gate kills when the latest CLOSED 15m
    candle close trades at or below a stop band beneath the lower price:

        threshold = LOWER_PRICE * (1 - STOP_IF_BELOW_LOWER_PERCENT)
        kill        = closed_close <= threshold

    All arithmetic is Decimal.  The caller must supply the close of the
    latest CLOSED 15m candle (never the currently forming candle and never
    a ticker price).

    Fail-closed outcomes (each vetoes new orders):
      * ``LOWER_BOUNDARY_STOP_CONFIG_INVALID`` — the stop percentage or the
        lower price is missing, non-finite, non-numeric, or out of the
        valid (0, 1) / (0, +inf) range;
      * ``LOWER_BOUNDARY_STOP_DATA_UNAVAILABLE`` — the closed-candle close
        is missing, non-finite, non-numeric, or non-positive;
      * ``LOWER_BOUNDARY_STOP_15M`` — the closed close is at/below the
        threshold.

    A valid close strictly above the threshold returns ``PASS`` (allowed),
    including the exact boundary behavior ``close == threshold -> KILL``
    and ``close > threshold -> PASS``.
    """
    stop = _stop_pct_valid(stop_if_below_lower_pct)
    lower = _finite_positive_decimal(lower_price)
    if stop is None or lower is None:
        return RiskDecision(False, ("LOWER_BOUNDARY_STOP_CONFIG_INVALID",))
    close = _finite_positive_decimal(closed_candle_close)
    if close is None:
        return RiskDecision(False, ("LOWER_BOUNDARY_STOP_DATA_UNAVAILABLE",))
    threshold = lower * (D("1") - stop)
    if close <= threshold:
        return RiskDecision(False, ("LOWER_BOUNDARY_STOP_15M",))
    return RiskDecision(True)

def inventory_gate(inventory_pct, max_inventory_pct):
    return RiskDecision(False, ("MAX_INVENTORY_EXCEEDED",)) if D(inventory_pct) > D(max_inventory_pct) else RiskDecision(True)

def open_orders_gate(open_orders, max_open_orders):
    return RiskDecision(False, ("MAX_OPEN_ORDERS_REACHED",)) if int(open_orders) >= int(max_open_orders) else RiskDecision(True)

def open_orders_available_gate(available):
    return RiskDecision(True) if available else RiskDecision(False, ("OPEN_ORDERS_UNAVAILABLE",))

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
