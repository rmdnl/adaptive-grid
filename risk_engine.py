from dataclasses import dataclass

@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str

def profit_gate(net_pct, hard_min):
    return RiskDecision(net_pct >= hard_min,
                        "NET_PROFIT_PASS" if net_pct >= hard_min else "NET_PROFIT_BLOCK")

def market_gate(last_row, cfg):
    checks = {
        "ADX": float(last_row["adx"]) <= cfg["adx_max"],
        "ATR": float(last_row["atr_pct"]) <= cfg["atr_pct_max"],
        "BB": float(last_row["bb_width"]) <= cfg["bb_width_max"],
        "VOLUME": float(last_row["volume_ratio"]) <= cfg["volume_spike_max"],
    }
    ok = all(checks.values())
    reason = "MARKET_SIDEWAYS_PASS" if ok else "MARKET_FILTER_BLOCK:" + ",".join(k for k,v in checks.items() if not v)
    return RiskDecision(ok, reason)

def equity_dd_kill(drawdown_pct, max_dd_pct):
    return drawdown_pct >= max_dd_pct

def range_kill(lower, upper, price, buffer_pct):
    return price < lower*(1-buffer_pct) or price > upper*(1+buffer_pct)
