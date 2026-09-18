# Backtest scaffold.
# Feed OHLCV data into a conservative candle simulator.
# Do not assume both high and low are touched in a favorable order within
# the same candle. Ambiguous candles should be processed conservatively.
from grid_engine import build_geometric_grid, expected_net_pct

if __name__ == "__main__":
    levels, eff = build_geometric_grid(100, 120, 0.006)
    print("levels:", len(levels), "effective_upper:", eff)
    print("expected net:", expected_net_pct(0.006, 0.001, 0.001, 0.0005))
