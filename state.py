"""SQLite state persistence.

A single database holds global bot state, per-symbol state, cooldown,
grid plan values, orders, fills, fees, realized PnL, risk events and the
kill state. Kill state and cooldown survive process restart; nothing
resets them automatically. Schema is intentionally minimal — no
compatibility tables for any deleted architecture.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, fields as dc_fields
from typing import Dict, List, Optional, Tuple

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS symbols (
    symbol TEXT PRIMARY KEY,
    timeframe TEXT,
    last_price REAL,
    adx REAL, rsi REAL, percent_b REAL, volume_osc REAL, zscore REAL, atr REAL,
    strategy_state TEXT DEFAULT 'WAITING',
    entry_blocker TEXT,
    block_reason TEXT,
    exit_status INTEGER DEFAULT 0,
    exit_reason TEXT,
    cooldown_until REAL,
    grid_mode TEXT, grid_step REAL, grid_lower REAL,
    gross_pct REAL, net_pct REAL,
    -- Adaptive grid parameters (Phase 1): locked when grid becomes active
    adaptive_lower_price REAL, adaptive_upper_price REAL,
    adaptive_total_grids INTEGER, adaptive_quote_budget REAL,
    adaptive_grid_step REAL, adaptive_reference_price REAL,
    adaptive_timeframe TEXT,
    inventory_qty REAL DEFAULT 0, avg_cost REAL DEFAULT 0,
    risk_status TEXT DEFAULT 'ok',
    updated_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT UNIQUE,
    symbol TEXT, side TEXT, type TEXT,
    price REAL, qty REAL, filled_qty REAL DEFAULT 0,
    status TEXT DEFAULT 'NEW',
    mode TEXT,
    parent_order_id INTEGER,
    target_sell_price REAL,
    child_sell_qty REAL NOT NULL DEFAULT 0,
    created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    symbol TEXT, side TEXT,
    price REAL, qty REAL,
    fee REAL DEFAULT 0,
    realized_pnl REAL DEFAULT 0,
    trade_id TEXT UNIQUE,
    quote_qty REAL,
    commission_asset TEXT,
    client_order_id TEXT,
    exchange_order_id INTEGER,
    ts REAL
);
CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, scope TEXT, event TEXT, details TEXT
);
"""

_SYMBOL_COLUMNS = {
    "timeframe", "last_price", "adx", "rsi", "percent_b", "volume_osc",
    "zscore", "atr", "strategy_state", "entry_blocker", "block_reason",
    "exit_status", "exit_reason", "cooldown_until", "grid_mode", "grid_step",
    "grid_lower", "gross_pct", "net_pct",
    # Adaptive grid parameters (Phase 1): locked when grid becomes active
    "adaptive_lower_price", "adaptive_upper_price", "adaptive_total_grids",
    "adaptive_quote_budget", "adaptive_grid_step", "adaptive_reference_price",
    "adaptive_timeframe",
    "inventory_qty", "avg_cost", "risk_status",
}

OPEN_ORDER_STATUSES = ("NEW", "PARTIALLY_FILLED")

# orders.child_sell_qty tracks how much executed BUY quantity has already
# been converted into child SELL orders (prevents duplicate child sells).
# Schema v4 adds adaptive grid parameters for automatic grid range/count/budget.
SCHEMA_VERSION = 4

# Inventory dust below this absolute quantity is zeroed after a SELL.
_INVENTORY_DUST = 1e-12


@dataclass
class SymbolState:
    symbol: str
    timeframe: Optional[str] = None
    last_price: Optional[float] = None
    adx: Optional[float] = None
    rsi: Optional[float] = None
    percent_b: Optional[float] = None
    volume_osc: Optional[float] = None
    zscore: Optional[float] = None
    atr: Optional[float] = None
    strategy_state: str = "WAITING"
    entry_blocker: Optional[str] = None
    block_reason: Optional[str] = None
    exit_status: int = 0
    exit_reason: Optional[str] = None
    cooldown_until: Optional[float] = None
    grid_mode: Optional[str] = None
    grid_step: Optional[float] = None
    grid_lower: Optional[float] = None
    gross_pct: Optional[float] = None
    net_pct: Optional[float] = None
    # Adaptive grid parameters (Phase 1): locked when grid becomes active
    adaptive_lower_price: Optional[float] = None
    adaptive_upper_price: Optional[float] = None
    adaptive_total_grids: Optional[int] = None
    adaptive_quote_budget: Optional[float] = None
    adaptive_grid_step: Optional[float] = None
    adaptive_reference_price: Optional[float] = None
    adaptive_timeframe: Optional[str] = None
    inventory_qty: float = 0.0
    avg_cost: float = 0.0
    risk_status: str = "ok"
    updated_at: float = 0.0


def _row_to_symbol(row: sqlite3.Row) -> SymbolState:
    known = {f.name for f in dc_fields(SymbolState)}
    data = {k: row[k] for k in row.keys() if k in known}
    return SymbolState(**data)


class StateStore:
    def __init__(self, path: str = "state.db", read_only: bool = False):
        self.path = path
        self.read_only = read_only
        if not read_only:
            self._connect().close()
            conn = self._connect()
            conn.executescript(_SCHEMA)
            conn.commit()
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()
            conn.close()
            self._migrate()

    def _migrate(self) -> None:
        """Minimal deterministic schema migration. Version 1 = the clean
        rebuild schema; version 2 adds orders.child_sell_qty for
        duplicate-free child-sell conversion; version 3 adds fill
        provenance columns (quote_qty, commission_asset, client_order_id,
        exchange_order_id); version 4 adds adaptive grid parameters for
        automatic grid range/count/budget. Idempotent."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            version = int(row["value"]) if row else 1
            if version < 2:
                cols = {
                    r["name"]
                    for r in conn.execute("PRAGMA table_info(orders)").fetchall()
                }
                if "child_sell_qty" not in cols:
                    conn.execute(
                        "ALTER TABLE orders ADD COLUMN child_sell_qty REAL NOT NULL DEFAULT 0"
                    )
            if version < 3:
                cols = {
                    r["name"]
                    for r in conn.execute("PRAGMA table_info(fills)").fetchall()
                }
                for column, decl in (
                    ("quote_qty", "REAL"),
                    ("commission_asset", "TEXT"),
                    ("client_order_id", "TEXT"),
                    ("exchange_order_id", "INTEGER"),
                ):
                    if column not in cols:
                        conn.execute(f"ALTER TABLE fills ADD COLUMN {column} {decl}")
            if version < 4:
                cols = {
                    r["name"]
                    for r in conn.execute("PRAGMA table_info(symbols)").fetchall()
                }
                for column, decl in (
                    ("adaptive_lower_price", "REAL"),
                    ("adaptive_upper_price", "REAL"),
                    ("adaptive_total_grids", "INTEGER"),
                    ("adaptive_quote_budget", "REAL"),
                    ("adaptive_grid_step", "REAL"),
                    ("adaptive_reference_price", "REAL"),
                    ("adaptive_timeframe", "TEXT"),
                ):
                    if column not in cols:
                        conn.execute(f"ALTER TABLE symbols ADD COLUMN {column} {decl}")
            if version < SCHEMA_VERSION:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(SCHEMA_VERSION),),
                )
                conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            # Dashboard access: refuse to create or modify anything.
            if not os.path.exists(self.path):
                raise FileNotFoundError(f"state database not found: {self.path}")
            conn = sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=1")
            return conn
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ----- meta / global state -----

    def set_meta(self, key: str, value: str) -> None:
        conn = self._connect()
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()
        conn.close()

    def get_meta(self, key: str) -> Optional[str]:
        conn = self._connect()
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        conn.close()
        return row["value"] if row else None

    def set_meta_float(self, key: str, value: float) -> None:
        self.set_meta(key, repr(float(value)))

    def get_meta_float(self, key: str) -> Optional[float]:
        raw = self.get_meta(key)
        if raw is None:
            return None
        return float(raw)

    def configured_symbols(self) -> Optional[List[str]]:
        """The symbol list of the active runtime session, persisted by the
        bot at startup as a JSON array in meta (PAIR_LIST order). None when
        the key is absent or the value is corrupt — the dashboard must fail
        closed for display instead of showing all historical symbols."""
        raw = self.get_meta("configured_symbols")
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(parsed, list) or not all(isinstance(s, str) for s in parsed):
            return None
        return [s for s in parsed if s]

    def set_global_kill(self, reason: str) -> None:
        self.set_meta("kill_active", "1")
        self.set_meta("kill_reason", reason)

    def global_kill(self) -> Tuple[bool, Optional[str]]:
        active = self.get_meta("kill_active") == "1"
        return active, self.get_meta("kill_reason") if active else None

    def set_runtime(self, status: str, ts: float) -> None:
        self.set_meta("runtime_status", status)
        self.set_meta("last_cycle_ts", repr(float(ts)))

    def last_runtime(self) -> Tuple[Optional[str], Optional[float]]:
        status = self.get_meta("runtime_status")
        raw_ts = self.get_meta("last_cycle_ts")
        return status, (float(raw_ts) if raw_ts is not None else None)

    # ----- symbols -----

    def ensure_symbols(self, symbols: List[str]) -> None:
        conn = self._connect()
        for symbol in symbols:
            conn.execute(
                "INSERT INTO symbols(symbol, updated_at) VALUES(?, ?) "
                "ON CONFLICT(symbol) DO NOTHING",
                (symbol, time.time()),
            )
        conn.commit()
        conn.close()

    def get_symbol(self, symbol: str) -> Optional[SymbolState]:
        conn = self._connect()
        row = conn.execute("SELECT * FROM symbols WHERE symbol=?", (symbol,)).fetchone()
        conn.close()
        return _row_to_symbol(row) if row else None

    def all_symbols(self) -> List[SymbolState]:
        conn = self._connect()
        rows = conn.execute("SELECT * FROM symbols ORDER BY symbol").fetchall()
        conn.close()
        return [_row_to_symbol(r) for r in rows]

    def update_symbol(self, symbol: str, **values) -> None:
        cols = [k for k in values if k in _SYMBOL_COLUMNS]
        if not cols:
            return
        assignments = ", ".join(f"{c}=?" for c in cols)
        params = [values[c] for c in cols] + [time.time(), symbol]
        conn = self._connect()
        conn.execute(
            f"UPDATE symbols SET {assignments}, updated_at=? WHERE symbol=?", params
        )
        conn.commit()
        conn.close()

    def set_symbol_state(self, symbol: str, state: str, **values) -> None:
        values["strategy_state"] = state
        self.update_symbol(symbol, **values)

    def set_cooldown(self, symbol: str, until_ts: float) -> None:
        self.update_symbol(symbol, cooldown_until=until_ts)

    def stop_symbol(self, symbol: str, reason: str) -> None:
        self.update_symbol(
            symbol,
            risk_status="stopped",
            strategy_state="STOPPED",
            exit_reason=reason,
        )

    # ----- orders -----

    def create_order(
        self,
        client_order_id: str,
        symbol: str,
        side: str,
        order_type: str,
        price: float,
        qty: float,
        mode: str,
        parent_order_id: Optional[int] = None,
        target_sell_price: Optional[float] = None,
    ) -> int:
        conn = self._connect()
        cur = conn.execute(
            "INSERT INTO orders(client_order_id, symbol, side, type, price, qty, "
            "status, mode, parent_order_id, target_sell_price, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                client_order_id, symbol, side, order_type, price, qty,
                "NEW", mode, parent_order_id, target_sell_price,
                time.time(), time.time(),
            ),
        )
        conn.commit()
        order_id = int(cur.lastrowid)
        conn.close()
        return order_id

    def create_child_sell_order(
        self,
        client_order_id: str,
        symbol: str,
        price: float,
        qty: float,
        parent_order_id: int,
        mode: str,
    ) -> int:
        """Create a child SELL order and mark the parent BUY's quantity as
        converted — in ONE transaction, so a crash can never produce a
        duplicate child sell or lose one (duplicate-prevention state)."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO orders(client_order_id, symbol, side, type, price, qty, "
                "status, mode, parent_order_id, target_sell_price, created_at, updated_at) "
                "VALUES(?, ?, 'SELL', 'LIMIT_MAKER', ?, ?, 'NEW', ?, ?, NULL, ?, ?)",
                (
                    client_order_id, symbol, price, qty, mode, parent_order_id,
                    time.time(), time.time(),
                ),
            )
            child_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE orders SET child_sell_qty = child_sell_qty + ?, updated_at=? "
                "WHERE id=?",
                (qty, time.time(), parent_order_id),
            )
            conn.commit()
            return child_id
        finally:
            conn.close()

    def update_order_status(
        self, order_id: int, status: str, filled_qty: Optional[float] = None
    ) -> None:
        conn = self._connect()
        if filled_qty is None:
            conn.execute(
                "UPDATE orders SET status=?, updated_at=? WHERE id=?",
                (status, time.time(), order_id),
            )
        else:
            conn.execute(
                "UPDATE orders SET status=?, filled_qty=?, updated_at=? WHERE id=?",
                (status, filled_qty, time.time(), order_id),
            )
        conn.commit()
        conn.close()

    def get_order(self, order_id: int) -> Optional[Dict]:
        conn = self._connect()
        row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_order_by_client_id(self, client_order_id: str) -> Optional[Dict]:
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def symbol_orders(self, symbol: str) -> List[Dict]:
        """All orders for a symbol (any status) — used for accounting
        invariants like pending child-sell conversion."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM orders WHERE symbol=? ORDER BY id", (symbol,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        conn = self._connect()
        if symbol is None:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status IN (?, ?) ORDER BY id",
                OPEN_ORDER_STATUSES,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM orders WHERE symbol=? AND status IN (?, ?) ORDER BY id",
                (symbol, *OPEN_ORDER_STATUSES),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def count_open_orders(self, symbol: Optional[str] = None) -> int:
        return len(self.open_orders(symbol))

    def fill_quantities(self, symbol: str) -> Tuple[float, float]:
        """Total BUY and SELL fill quantities for a symbol (from the fills
        ledger). Used by the ledger-consistency check: held inventory must
        equal BUY qty - SELL qty; a mismatch signals corrupted accounting."""
        conn = self._connect()
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN qty ELSE 0 END), 0) AS b, "
            "COALESCE(SUM(CASE WHEN side='SELL' THEN qty ELSE 0 END), 0) AS s "
            "FROM fills WHERE symbol=?",
            (symbol,),
        ).fetchone()
        conn.close()
        return float(row["b"]), float(row["s"])

    # ----- fills / pnl / fees -----

    def fill_exists(self, trade_id: str) -> bool:
        """True when this exchange trade has already been accounted."""
        conn = self._connect()
        row = conn.execute(
            "SELECT 1 FROM fills WHERE trade_id=?", (trade_id,)
        ).fetchone()
        conn.close()
        return row is not None

    def record_fill(
        self,
        order_id: int,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        fee: float,
        trade_id: Optional[str] = None,
        quote_qty: Optional[float] = None,
        commission_asset: Optional[str] = None,
        client_order_id: Optional[str] = None,
        exchange_order_id: Optional[int] = None,
    ) -> bool:
        """THE authoritative accounting event for one execution trade.

        Exactly one call per exchange trade updates inventory, average cost,
        realized PnL (SELL) and the fills ledger — atomically, in a single
        transaction. `trade_id` is the idempotency key: a trade recorded
        before (including after restart) returns False and mutates nothing.

        Provenance (quote_qty, commission_asset, client_order_id,
        exchange_order_id) is persisted with the fill when provided.

        Accounting model: BUY grows inventory at weighted average cost; SELL
        realizes qty x (price - avg_cost) against held inventory; fees are
        tracked separately; equity = start + realized - fees + unrealized.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT inventory_qty, avg_cost FROM symbols WHERE symbol=?",
                (symbol,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO symbols(symbol, inventory_qty, avg_cost, updated_at) "
                    "VALUES(?, 0, 0, ?)",
                    (symbol, time.time()),
                )
                inventory = 0.0
                avg_cost = 0.0
            else:
                inventory = float(row["inventory_qty"] or 0.0)
                avg_cost = float(row["avg_cost"] or 0.0)

            realized = 0.0
            if side == "BUY":
                new_inventory = inventory + qty
                avg_cost = ((inventory * avg_cost) + qty * price) / new_inventory
                inventory = new_inventory
            else:  # SELL realizes PnL only against actually held inventory
                sell_qty = min(qty, inventory) if inventory > 0.0 else 0.0
                if sell_qty > 0.0:
                    realized = sell_qty * (price - avg_cost)
                    inventory -= sell_qty
                if inventory <= _INVENTORY_DUST:
                    inventory = 0.0
                    avg_cost = 0.0

            cur = conn.execute(
                "INSERT OR IGNORE INTO fills(order_id, symbol, side, price, qty, fee, "
                "realized_pnl, trade_id, quote_qty, commission_asset, client_order_id, "
                "exchange_order_id, ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (order_id, symbol, side, price, qty, fee, realized, trade_id,
                 quote_qty, commission_asset, client_order_id, exchange_order_id,
                 time.time()),
            )
            if cur.rowcount == 0:
                # Duplicate trade (e.g. replayed reconciliation): the ledger
                # keeps exactly one row; nothing else may change.
                conn.rollback()
                return False
            conn.execute(
                "UPDATE symbols SET inventory_qty=?, avg_cost=?, updated_at=? "
                "WHERE symbol=?",
                (inventory, avg_cost, time.time(), symbol),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def sum_realized_pnl(self, symbol: Optional[str] = None) -> float:
        conn = self._connect()
        if symbol is None:
            row = conn.execute("SELECT COALESCE(SUM(realized_pnl), 0) AS s FROM fills").fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) AS s FROM fills WHERE symbol=?",
                (symbol,),
            ).fetchone()
        conn.close()
        return float(row["s"])

    def sum_fees(self, symbol: Optional[str] = None) -> float:
        conn = self._connect()
        if symbol is None:
            row = conn.execute("SELECT COALESCE(SUM(fee), 0) AS s FROM fills").fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(fee), 0) AS s FROM fills WHERE symbol=?",
                (symbol,),
            ).fetchone()
        conn.close()
        return float(row["s"])

    def count_completed_grids(self, symbol: str) -> int:
        """A completed grid = one executed sell closing a bought unit."""
        conn = self._connect()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM fills WHERE symbol=? AND side='SELL'",
            (symbol,),
        ).fetchone()
        conn.close()
        return int(row["n"])

    def net_pnl_history(self) -> List[Dict]:
        """Reconstructable telemetry: cumulative net PnL (realized - fees)
        over time from the fills ledger. Read-only; no invented data."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT ts, realized_pnl, fee FROM fills ORDER BY id"
        ).fetchall()
        conn.close()
        points: List[Dict] = []
        net = 0.0
        for r in rows:
            net += float(r["realized_pnl"] or 0.0) - float(r["fee"] or 0.0)
            points.append({"ts": float(r["ts"]), "net": net})
        return points

    # ----- risk events -----

    def add_risk_event(self, scope: str, event: str, details: str = "") -> None:
        conn = self._connect()
        conn.execute(
            "INSERT INTO risk_events(ts, scope, event, details) VALUES(?,?,?,?)",
            (time.time(), scope, event, details),
        )
        conn.commit()
        conn.close()

    def recent_risk_events(self, limit: int = 50) -> List[Dict]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM risk_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ----- session reset (explicit operator action) -----

    SESSION_META_KEYS = (
        "session_id", "session_mode", "session_env", "session_started_ts",
        "session_start_equity", "session_initial_cash",
        "equity", "reference_equity", "wallet_usdt",
        "kill_active", "kill_reason", "runtime_status", "last_cycle_ts",
    )

    def reset_session(self) -> None:
        """Wipe all session-scoped trading state (fills, orders, symbol
        rows, equity/kill meta). Explicit operator action only — the
        caller must verify there are no open orders. Risk events are kept
        as permanent audit trail."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM fills")
            conn.execute("DELETE FROM orders")
            conn.execute("DELETE FROM symbols")
            placeholders = ", ".join("?" for _ in self.SESSION_META_KEYS)
            conn.execute(f"DELETE FROM meta WHERE key IN ({placeholders})", self.SESSION_META_KEYS)
            conn.commit()
        finally:
            conn.close()

    # ----- health -----

    def database_status(self) -> Dict:
        try:
            size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
            conn = self._connect()
            conn.execute("SELECT 1").fetchone()
            conn.close()
            return {"ok": True, "path": self.path, "size_bytes": size}
        except Exception as exc:  # dashboard must never crash on DB issues
            return {"ok": False, "path": self.path, "error": str(exc)}
