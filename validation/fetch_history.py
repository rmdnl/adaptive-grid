"""Phase 7 validation — historical data acquisition (read-only market data).

Fetches closed 15m klines for BNBUSDT from the PUBLIC Binance market-data
endpoint (no authentication, no order endpoints) and caches them to
``data/validation_klines_BNBUSDT_15m.csv`` so all validation runs are
deterministic and offline afterward.

Safety: this module ONLY ever performs GET requests for klines.  It has no
write endpoints, no futures/margin/leverage/withdrawal calls, and no
credentials.
"""

from __future__ import annotations

import csv
import json
import pathlib
import urllib.parse
import urllib.request

BASE = "https://api.binance.com/api/v3/klines"
CACHE = pathlib.Path(__file__).resolve().parent.parent / "data" / "validation_klines_BNBUSDT_15m.csv"
SYMBOL = "BNBUSDT"
INTERVAL = "15m"


def fetch_history(total_candles: int = 4000) -> list[dict]:
    """Fetch the most recent ``total_candles`` CLOSED 15m klines.

    Pages backwards in time (newest-first) using the public endpoint, then
    drops the currently-forming candle so validation only ever replays
    closed candles.
    """
    collected: list[list] = []
    end_time: int | None = None
    while len(collected) < total_candles:
        params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": "1000"}
        if end_time is not None:
            params["endTime"] = str(end_time)
        url = f"{BASE}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=30) as response:
            page = json.load(response)
        if not page:
            break
        # API returns oldest-first within the page; we fetch newest windows
        # first, so prepend.
        collected = list(page) + collected
        end_time = int(page[0][0]) - 1
        if len(page) < 1000:
            break
    if len(collected) > total_candles:
        collected = collected[-total_candles:]
    # Drop the forming (last) candle if it is still open.
    import time
    rows = []
    for row in collected:
        if row_last_is_forming(row, time.time() * 1000):
            continue
        rows.append({
            "open_time": int(row[0]),
            "close_time": int(row[6]),
            "open": row[1],
            "high": row[2],
            "low": row[3],
            "close": row[4],
            "volume": row[5],
            "quote_volume": row[7],
            "trades": int(row[8]),
        })
    return rows


def row_last_is_forming(row: list, now_ms: float) -> bool:
    """A candle is closed only when its close_time <= now."""
    return int(row[6]) >= now_ms


def save_csv(rows: list[dict]) -> pathlib.Path:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return CACHE


def load_csv() -> list[dict]:
    with open(CACHE, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        for key in row:
            if key != "symbol":
                row[key] = float(row[key]) if key in ("open", "high", "low", "close", "volume", "quote_volume") else int(row[key])
    return rows


if __name__ == "__main__":
    import datetime

    existing = pathlib.Path(__file__).resolve().parent.parent / "data" / "validation_klines_BNBUSDT_15m.csv"
    if existing.exists():
        rows = load_csv()
        print(f"CACHE HIT: {len(rows)} rows")
    else:
        rows = fetch_history(4000)
        save_csv(rows)
        print(f"FETCH: {len(rows)} rows")
    if rows:
        t0 = datetime.datetime.fromtimestamp(rows[0]["open_time"] / 1000, datetime.timezone.utc)
        t1 = datetime.datetime.fromtimestamp(rows[-1]["close_time"] / 1000, datetime.timezone.utc)
        print(f"range: {t0} .. {t1} (UTC)")
        closes = [r["close"] for r in rows]
        print(f"close min={min(closes)} max={max(closes)} last={closes[-1]}")
