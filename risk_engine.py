from dataclasses import dataclass

@dataclass
class RiskDecision:
    allowed: bool
    reason: str

def profit_gate(net_pct: float, hard_min: float):
    return RiskDecision(net_pct >= hard_min, "NET_PROFIT_PASS" if net_pct >= hard_min else "NET_PROFIT_BLOCK")

def equity_dd_kill(drawdown_pct: float, max_dd_pct: float):
    return drawdown_pct >= max_dd_pct

def range_kill(lower: float, upper: float, price: float, buffer_pct: float):
    return price < lower * (1-buffer_pct) or price > upper * (1+buffer_pct)
