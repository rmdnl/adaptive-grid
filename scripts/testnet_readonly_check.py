"""Phase 6A testnet read-only connectivity smoke script.

Loads environment, verifies testnet configuration, constructs the adapter,
runs connectivity_check(), prints structured output, and exits 0/1.

No orders are submitted or cancelled.  Never prints secret values.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from binance_testnet import (
    BinanceTestnetClient,
    BinanceTestnetConfigError,
    load_testnet_config_from_env,
)


def main() -> int:
    load_dotenv()
    # 1. Load + validate environment (fail-closed).
    try:
        config = load_testnet_config_from_env()
    except BinanceTestnetConfigError as exc:
        print("CONFIG: FAIL")
        print(f"reason: {type(exc).__name__}")
        # Never print credential values.
        return 1

    print(f"environment: {config.environment}")
    print(f"base_url:    {config.base_url}")

    # 2. Construct adapter (validates again internally).
    try:
        client = BinanceTestnetClient(config)
    except BinanceTestnetConfigError as exc:
        print("ADAPTER: FAIL")
        print(f"reason: {type(exc).__name__}")
        return 1

    # 3. Run connectivity check.
    symbol = "BNBUSDT"
    try:
        snap = client.connectivity_check(symbol)
    except Exception as exc:
        print("CONNECTIVITY: FAIL")
        print(f"reason: {type(exc).__name__}")
        return 1

    # 4. Print structured result.
    print(f"symbol:         {snap.symbol}")
    print(f"ticker:         {snap.ticker_price}")
    print(f"server_time_ms: {snap.server_time_ms}")
    print(f"local_time_ms:  {snap.local_time_ms}")
    print(f"skew_ms:        {snap.skew_ms}")
    print(f"account:        {'AVAILABLE' if snap.account_available else 'UNAVAILABLE (auth required?)'}")
    print(f"open_orders:    {snap.open_order_count}")
    print(f"symbol_rules:   {'VALID' if snap.symbol_rules_valid else 'INVALID'}")
    print(f"overall:        {snap.overall}")
    if snap.reason:
        print(f"reason:         {snap.reason}")

    # 5. Exit status.
    if snap.overall == "PASS":
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
