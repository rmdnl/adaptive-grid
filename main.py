import os
import yaml
from dotenv import load_dotenv
from market_data import make_client, fetch_klines
from indicators import enrich
from range_engine import auto_range
from grid_engine import build_geometric_grid, expected_net_pct
from risk_engine import profit_gate, market_gate

def load_config():
    with open("config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)

def main():
    load_dotenv()
    cfg = load_config()
    mode = cfg["environment"]["mode"]
    client = make_client(mode, os.getenv("BINANCE_API_KEY",""), os.getenv("BINANCE_API_SECRET",""))
    df = enrich(fetch_klines(client, cfg["symbol"], cfg["timeframe"], cfg["range"]["lookback"]))

    g, fees = cfg["grid"], cfg["fees"]
    net = expected_net_pct(g["step_pct"], fees["maker_fee_fallback"], fees["maker_fee_fallback"], fees["slippage_roundtrip_pct"])
    pg = profit_gate(net, g["hard_min_net_pct"])

    if cfg["range"]["mode"] == "manual":
        lower, upper = cfg["range"]["lower_price"], cfg["range"]["upper_price"]
        quality, reason = 100.0, "MANUAL_RANGE"
    else:
        r = auto_range(df, **cfg["range"]["auto"])
        lower, upper, quality, reason = r.lower, r.upper, r.quality, r.reason

    last = df.iloc[-1]
    mg = market_gate(last, cfg["market_filter"])

    print("=== Adaptive Risk-Controlled Grid Engine v3.1 ===")
    print(f"Symbol        : {cfg['symbol']}")
    print(f"Price         : {last['close']:.8f}")
    print(f"Range         : {lower:.8f} -> {upper:.8f}")
    print(f"Range Quality : {quality:.2f}/100 ({reason})")
    print(f"ADX           : {last['adx']:.2f}")
    print(f"ATR %         : {last['atr_pct']*100:.3f}%")
    print(f"BB Width      : {last['bb_width']*100:.3f}%")
    print(f"Volume Ratio  : {last['volume_ratio']:.2f}x")
    print(f"Grid Step     : {g['step_pct']*100:.3f}%")
    print(f"Expected Net  : {net*100:.3f}%")
    print(f"Profit Gate   : {'PASS' if pg.allowed else 'BLOCK'}")
    print(f"Market Gate   : {'PASS' if mg.allowed else 'BLOCK'}")

    if lower > 0 and upper > lower:
        levels, effective_upper = build_geometric_grid(lower, upper, g["step_pct"])
        print(f"Grid Levels   : {len(levels)}")
        print(f"Effective Top : {effective_upper:.8f}")

    if cfg["environment"]["dry_run"]:
        print("Execution     : DRY RUN")
    else:
        print("Execution     : ORDER ENGINE NOT ENABLED IN v3.1")

if __name__ == "__main__":
    main()
