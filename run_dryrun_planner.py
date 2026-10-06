#!/usr/bin/env python3
"""Dry-run adaptive grid planner for specified symbols using testnet data.

READ-ONLY: Fetches market data, computes grid parameters, prints results.
Does NOT place orders, modify state.db, or alter .env.
"""

from __future__ import annotations

import os
import sys
import time

# Load .env
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, ".")

from config import load_config, Config
from exchange import BinanceSpot
from adaptive_grid import AdaptiveGridPlanner
from grid import ExchangeFilters


def compute_atr(klines: list, period: int = 14) -> float:
    """Compute ATR from klines using Wilder's smoothing."""
    if len(klines) < period + 1:
        raise ValueError(f"Need at least {period + 1} klines for ATR({period})")
    
    trs = []
    for i in range(1, len(klines)):
        high = klines[i]["high"]
        low = klines[i]["low"]
        prev_close = klines[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    
    # Wilder's smoothing: first ATR = simple average of first `period` TRs
    atr = sum(trs[:period]) / period
    # Subsequent: atr = (prev_atr * (period - 1) + current_tr) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def get_current_price(klines: list) -> float:
    """Get last CLOSED candle close price (not the current forming candle)."""
    # Use the second-to-last candle (index -2) as the last CLOSED candle
    # The last candle (index -1) is still forming
    if len(klines) < 2:
        raise ValueError("Need at least 2 klines")
    return klines[-2]["close"]


def get_latest_closed_15m_close(klines_15m: list) -> float:
    """Get the latest CLOSED 15m candle close for boundary check."""
    if len(klines_15m) < 2:
        raise ValueError("Need at least 2 15m klines")
    return klines_15m[-2]["close"]


def main():
    symbols = ["NEAR/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"]
    
    print("=" * 80)
    print("ADAPTIVE GRID DRY-RUN PLANNER")
    print("=" * 80)
    print(f"Symbols: {symbols}")
    print()
    
    # Load config
    cfg = load_config()
    
    # Create spot client
    spot = BinanceSpot(cfg)
    
    # Get USDT balance once (same for all symbols)
    print("Fetching USDT balance...")
    try:
        usdt_balance = spot.get_balance("USDT")
        print(f"Available USDT: {usdt_balance:.2f}")
    except Exception as e:
        print(f"ERROR fetching USDT balance: {e}")
        return 1
    
    print()
    
    for symbol in symbols:
        print("-" * 80)
        print(f"SYMBOL: {symbol}")
        print("-" * 80)
        
        try:
            # Fetch exchange filters
            print("Fetching exchange filters...")
            filters = spot.get_filters(symbol)
            print(f"  tick_size: {filters.tick_size}")
            print(f"  step_size: {filters.step_size}")
            print(f"  min_notional: {filters.min_notional}")
            print(f"  min_qty: {filters.min_qty}")
            print(f"  max_price: {filters.max_price}")
            print(f"  max_qty: {filters.max_qty}")
            print(f"  max_notional: {filters.max_notional}")
            print(f"  PERCENT_PRICE_BY_SIDE: bid_up={filters.bid_multiplier_up}, bid_down={filters.bid_multiplier_down}, ask_up={filters.ask_multiplier_up}, ask_down={filters.ask_multiplier_down}")
            
            # Fetch 4h klines for ATR and current price
            print(f"\nFetching {cfg.indicator_timeframe} klines...")
            klines = spot.fetch_klines(symbol, cfg.indicator_timeframe, limit=100)
            print(f"  Fetched {len(klines)} klines")
            
            current_price = get_current_price(klines)
            print(f"  Current price (last closed {cfg.indicator_timeframe} candle): {current_price:.8f}")
            
            atr = compute_atr(klines, cfg.atr_period)
            print(f"  ATR({cfg.atr_period}): {atr:.8f}")
            
            # Fetch 15m klines for boundary check
            print("\nFetching 15m klines for boundary check...")
            klines_15m = spot.fetch_klines(symbol, "15m", limit=50)
            print(f"  Fetched {len(klines_15m)} 15m klines")
            
            close_15m = get_latest_closed_15m_close(klines_15m)
            print(f"  Latest CLOSED 15m close: {close_15m:.8f}")
            
            # Fetch reference price (weighted average price)
            print("\nFetching reference price (avgPrice)...")
            avg_price_resp = spot.get_avg_price(symbol)
            reference_price = float(avg_price_resp.get("price", 0))
            print(f"  Reference price: {reference_price:.8f}")
            
            # Run adaptive planner
            print("\nRunning AdaptiveGridPlanner.plan()...")
            plan = AdaptiveGridPlanner.plan(
                symbol=symbol,
                current_price=current_price,
                atr=atr,
                cfg=cfg,
                filters=filters,
                reference_price=reference_price,
                available_usdt=usdt_balance,
            )
            
            print(f"\n>>> ADAPTIVE GRID PLAN for {symbol} <<<")
            print(f"  lower_price:      {plan.lower_price:.8f}")
            print(f"  upper_price:      {plan.upper_price:.8f}")
            print(f"  total_grids:      {plan.total_grids}")
            print(f"  quote_budget:     {plan.quote_budget:.2f} USDT")
            print(f"  grid_step:        {plan.step:.8f}")
            print(f"  reference_price:  {plan.reference_price:.8f}")
            print(f"  gross_pct:        {plan.gross_pct:.4%}")
            print(f"  net_pct:          {plan.net_pct:.4%}")
            print(f"  mode:             {plan.mode}")
            print(f"  levels:           {len(plan.levels)}")
            
            # Show grid levels
            print(f"\n  Grid Levels:")
            for lvl in plan.levels:
                print(f"    [{lvl.index:2d}] BUY: {lvl.buy_price:.8f}  SELL: {lvl.sell_price:.8f}  QTY: {lvl.qty:.8f}  Gross: {lvl.gross_pct:.4%}  Net: {lvl.net_pct:.4%}")
            
            # 15m boundary check
            from config import Config
            stop_if_below_lower = cfg.stop_if_below_lower
            boundary = close_15m <= plan.lower_price * (1.0 - stop_if_below_lower)
            print(f"\n  15m Boundary Check:")
            print(f"    close_15m:           {close_15m:.8f}")
            print(f"    lower_price:         {plan.lower_price:.8f}")
            print(f"    stop_if_below_lower: {stop_if_below_lower:.2%}")
            print(f"    threshold:           {plan.lower_price * (1.0 - stop_if_below_lower):.8f}")
            print(f"    BREACH:              {'YES' if boundary else 'NO'}")
            
        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()
        
        print()
        time.sleep(0.5)  # Rate limiting
    
    print("=" * 80)
    print("DRY-RUN COMPLETE")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())