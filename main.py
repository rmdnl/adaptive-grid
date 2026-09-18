import os
import yaml
from dotenv import load_dotenv
from grid_engine import build_geometric_grid, expected_net_pct
from range_engine import auto_range
from risk_engine import profit_gate

def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def main():
    load_dotenv()
    cfg = load_config()

    g = cfg["grid"]
    fees = cfg["fees"]
    net = expected_net_pct(
        g["step_pct"],
        fees["maker_fee_fallback"],
        fees["maker_fee_fallback"],
        fees["slippage_roundtrip_pct"],
    )

    gate = profit_gate(net, g["hard_min_net_pct"])

    print("=== Adaptive Risk-Controlled Grid Engine v3 ===")
    print(f"Symbol: {cfg['symbol']}")
    print(f"Mode: {cfg['environment']['mode']} | dry_run={cfg['environment']['dry_run']}")
    print(f"Grid step: {g['step_pct']*100:.3f}%")
    print(f"Expected net: {net*100:.3f}%")
    print(f"Profit gate: {'PASS' if gate.allowed else 'BLOCK'}")

    if cfg["range"]["mode"] == "manual":
        lower = cfg["range"]["lower_price"]
        upper = cfg["range"]["upper_price"]
    else:
        # Placeholder sample only. Live implementation must feed real klines.
        prices = [100 + i*0.02 for i in range(cfg["range"]["lookback"])]
        candidate = auto_range(prices,
                               cfg["range"]["auto"]["min_width_pct"],
                               cfg["range"]["auto"]["max_width_pct"])
        lower, upper = candidate.lower, candidate.upper

    levels, effective_upper = build_geometric_grid(lower, upper, g["step_pct"])
    print(f"Range: {lower:.8f} -> {upper:.8f}")
    print(f"Effective geometric range: {levels[0].price:.8f} -> {effective_upper:.8f}")
    print(f"Grid levels: {len(levels)}")

if __name__ == "__main__":
    main()
