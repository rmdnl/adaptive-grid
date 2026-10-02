"""Phase 7 shared helpers — deterministic paper-validation data seams.

Every seam here is READ-ONLY market/account data derived from either the
cached historical BNBUSDT klines or the paper SQLite database itself.
Nothing in this package touches an order endpoint, a live URL, or credentials.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import sys
from pathlib import Path as _Path

# Ensure the project root is importable when this runs as a script.
_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd

from market_data import (
    AccountSnapshot,
    MarketQuote,
    TickerSnapshot,
)
from symbol_rules import SymbolRules
from storage import connect, get_paper_account_state

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
KLINES_CSV = DATA_DIR / "validation_klines_BNBUSDT_15m.csv"

SYMBOL = "BNBUSDT"

# Deterministic exchange filters for BNB (Spot) used for offline rule parsing.
# Mirrors the testnet symbol-info fixture used throughout the test suite.
SYMBOL_INFO = {
    "symbol": SYMBOL,
    "baseAsset": "BNB",
    "quoteAsset": "USDT",
    "status": "TRADING",
    "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "0.01000000", "maxPrice": "1000000.00000000", "tickSize": "0.01000000"},
        {"filterType": "LOT_SIZE", "minQty": "0.00000001", "maxQty": "1000000.00000000", "stepSize": "0.00000001"},
        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.00000001", "maxQty": "500000.00000000", "stepSize": "0.00000001"},
        {"filterType": "MIN_NOTIONAL", "minNotional": "5.00000000"},
        {"filterType": "NOTIONAL", "minNotional": "10.00000000", "maxNotional": "1000000.00000000"},
        {"filterType": "PERCENT_PRICE", "multiplierUp": "1.05000000", "multiplierDown": "0.95000000", "avgPriceMins": 5},
        {"filterType": "MAX_NUM_ORDERS", "maxNumOrders": 40},
    ],
}


def parse_rules() -> SymbolRules:
    from symbol_rules import parse_symbol_info
    return parse_symbol_info(SYMBOL_INFO)


# ---------------------------------------------------------------------------
# Historical candle loading
# ---------------------------------------------------------------------------

def load_klines() -> pd.DataFrame:
    """Load the cached closed 15m BNBUSDT candles (deterministic replay)."""
    if not KLINES_CSV.exists():
        raise RuntimeError(
            f"validation klines cache missing: {KLINES_CSV} — "
            "run `python validation/fetch_history.py` first (offline after fetch)"
        )
    df = pd.read_csv(KLINES_CSV)
    required = {"open_time", "close_time", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"klines cache missing columns: {missing}")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    for col in ("open", "high", "low", "close", "volume", "quote_volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col])
    return df.reset_index(drop=True)


def candle_index_to_ts(candle_index: int, df: pd.DataFrame) -> datetime:
    """Map 1-based validation candle index to the close timestamp of that candle."""
    row = df.iloc[candle_index - 1]
    return row["close_time"].to_pydatetime()


def window_df(df: pd.DataFrame, candle_index: int, window: int = 200) -> pd.DataFrame:
    """Closed-candle window ending at ``candle_index`` (inclusive)."""
    start = max(1, candle_index - window + 1)
    slice_ = df.iloc[start - 1 : candle_index].copy()
    slice_.reset_index(drop=True, inplace=True)
    return slice_


def price_at(df: pd.DataFrame, candle_index: int) -> Decimal:
    return Decimal(str(float(df.iloc[candle_index - 1]["close"])))


def close_ts(df: pd.DataFrame, candle_index: int) -> datetime:
    return df.iloc[candle_index - 1]["close_time"].to_pydatetime()


# ---------------------------------------------------------------------------
# Data-seam factories (all read-only)
# ---------------------------------------------------------------------------

def ticker_from_candle(df: pd.DataFrame, candle_index: int) -> TickerSnapshot:
    row = df.iloc[candle_index - 1]
    ts = row["close_time"].to_pydatetime()
    return TickerSnapshot(symbol=SYMBOL, price=Decimal(str(float(row["close"]))), fetched_at=ts)


def quote_from_candle(df: pd.DataFrame, candle_index: int) -> MarketQuote:
    """Deterministic synthetic book-ticker: mid = last close, fixed 0.1% spread.

    The real book ticker has no historical source; a deterministic bid/ask
    derived from the candle close keeps liquidity gates (spread/age)
    deterministic without inventing prices — mid stays on real close.
    """
    row = df.iloc[candle_index - 1]
    ts = row["close_time"].to_pydatetime()
    mid = Decimal(str(float(row["close"])))
    spread = (mid * Decimal("0.001")).quantize(Decimal("0.0001"))
    half = spread / Decimal("2")
    return MarketQuote(
        symbol=SYMBOL,
        bid_price=mid - half,
        bid_qty=Decimal("100"),
        ask_price=mid + half,
        ask_qty=Decimal("100"),
        fetched_at=ts,
    )


def paper_open_orders(db_path: str) -> list:
    """Open paper orders from the paper orders table (execution truth)."""
    con = connect(db_path)
    try:
        rows = con.execute(
            "SELECT client_order_id, symbol, side, price, quantity, state "
            "FROM orders WHERE state='OPEN' ORDER BY client_order_id"
        ).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        out.append({
            "client_order_id": r[0],
            "symbol": r[1],
            "side": r[2],
            "price": r[3],
            "quantity": r[4],
            "state": r[5],
        })
    return out


def account_snapshot_from_paper_state(db_path: str, ts: datetime) -> AccountSnapshot:
    """Reconstruct the paper account snapshot from the persisted accounting state.

    The paper engine's persisted `paper_account_state` IS the account truth in
    paper mode; mapping it to an AccountSnapshot keeps main.py's account risk
    gates exercising real data instead of failing closed every cycle.
    """
    from storage import ensure_paper_account_state
    from paper_accounting import PaperAccountingEngine

    state = get_paper_account_state(db_path)
    if state is None:
        # Seed exactly once with the configured initial balances so the next
        # orchestrator cycle (or a direct main-only run) can bootstrap.
        from config_loader import load_config
        cfg = load_config()
        initial = PaperAccountingEngine(
            "BNB", "USDT",
            Decimal(str(cfg["paper"]["initial_base_balance"])),
            Decimal(str(cfg["paper"]["initial_quote_balance"])),
            Decimal(str(cfg["paper"]["maker_fee"])),
            Decimal(str(cfg["paper"]["taker_fee"])),
            str(cfg["paper"]["fee_asset"]),
        ).initial_state()
        ensure_paper_account_state(db_path, initial)
        state = get_paper_account_state(db_path)
        assert state is not None
    return AccountSnapshot(
        base_asset=state["base_asset"],
        base_free=state["base_free"],
        base_locked=state["base_reserved"],
        quote_asset=state["quote_asset"],
        quote_free=state["quote_free"],
        quote_locked=state["quote_reserved"],
        fetched_at=ts,
    )


# ---------------------------------------------------------------------------
# Scenario record
# ---------------------------------------------------------------------------

@dataclass
class CycleRecord:
    """One observed execution cycle (JSON-serializable)."""
    timestamp: str
    cycle_id: str
    symbol: str
    plan_id: str | None
    generation: int | None
    regime: str | None
    grid_lower: str | None
    grid_upper: str | None
    grid_count: int | None
    grid_spacing: str | None
    risk_allowed: bool
    blocked_reason: str | None
    orders_submitted: int
    fills: int
    inventory: dict | None
    equity: str | None
    drawdown: str | None
    fees: str | None
    realized_pnl: str | None
    unrealized_pnl: str | None
    recovery_status: str
    success: bool

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def paper_state_snapshot(db_path: str, mark_price: Decimal | None = None) -> dict:
    state = get_paper_account_state(db_path)
    out: dict[str, Any] = {}
    if state is not None:
        out = dict(state)
        if mark_price is not None:
            out["equity"] = str(
                state["quote_free"] + state["quote_reserved"]
                + (state["base_free"] + state["base_reserved"]) * mark_price
            )
    return out


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)
