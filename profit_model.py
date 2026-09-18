def net_pct(step_pct, maker_buy, maker_sell, slippage_roundtrip):
    return (1 + step_pct) * (1-maker_buy) * (1-maker_sell) * (1-slippage_roundtrip) - 1

def passes(net_value, hard_min):
    return net_value >= hard_min
