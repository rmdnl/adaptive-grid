from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

def D(value): return Decimal(str(value))

@dataclass(frozen=True)
class EffectiveFees:
    maker: Decimal
    taker: Decimal
    source: str

def _rate(node: Any, side: str) -> Decimal | None:
    if not isinstance(node, dict):
        return None
    value = node.get(side)
    if value is None:
        key = f"{side}Commission"
        value = node.get(key)
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    try:
        return D(value)
    except Exception:
        return None

def _sum_components(payload: dict, side: str) -> Decimal | None:
    parts = []
    for name in ("standardCommission", "specialCommission", "taxCommission"):
        value = _rate(payload.get(name), side)
        if value is None:
            return None
        parts.append(value)
    return sum(parts, Decimal("0"))

def effective_fees(payload: dict | None, fallback_maker, fallback_taker) -> EffectiveFees:
    fallback_m = D(fallback_maker)
    fallback_t = D(fallback_taker)
    if not payload:
        return EffectiveFees(fallback_m, fallback_t, "FALLBACK")

    maker = _sum_components(payload, "maker")
    taker = _sum_components(payload, "taker")
    if maker is None or taker is None:
        return EffectiveFees(fallback_m, fallback_t, "FALLBACK:INCOMPLETE_ACCOUNT_COMMISSION")

    # Deliberately do NOT apply the optional discount here.
    # Without verified eligible fee-asset funding, omitting a discount is conservative.
    return EffectiveFees(maker, taker, "ACCOUNT_COMMISSION_CONSERVATIVE")
