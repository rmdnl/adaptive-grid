from __future__ import annotations

import contextlib
import csv
import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = "3.2.1"

#: Schema alias used when a cycle transaction must span two SQLite files.  The
#: order database is opened as ``main``; the lifecycle database is attached
#: under this name so one transaction can cover both.
LIFECYCLE_SCHEMA = "lifecycle"

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def connect(path):
    p=Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(p); con.row_factory=sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def same_file(a, b) -> bool:
    """True when two SQLite paths resolve to the same file on disk."""
    if a is None or b is None:
        return False
    if str(a) == str(b):
        return True
    try:
        return os.path.samefile(str(a), str(b))
    except (FileNotFoundError, OSError):
        return False


@contextlib.contextmanager
def cycle_transaction(order_db_path, lifecycle_db_path=None):
    """Own the single transaction that spans one logical paper cycle.

    The order database is opened as ``main``.  When the lifecycle database is a
    *different* file it is attached as ``LIFECYCLE_SCHEMA`` so that one
    ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK`` covers both.  Every
    cycle-owned mutation is issued on the yielded connection and no lower-level
    component commits on its own, which is what makes the cycle atomic.

    Yields:
        ``(con, lifecycle_prefix)`` -- ``con`` is the shared connection and
        ``lifecycle_prefix`` is ``"lifecycle."`` when the lifecycle database was
        attached, or ``""`` when both logical databases share one file.
    """
    con = connect(order_db_path)
    attached = False
    try:
        if lifecycle_db_path is not None and not same_file(order_db_path, lifecycle_db_path):
            con.execute(
                "ATTACH DATABASE ? AS %s" % LIFECYCLE_SCHEMA,
                (str(Path(lifecycle_db_path)),),
            )
            attached = True
        prefix = ("%s." % LIFECYCLE_SCHEMA) if attached else ""
        # ATTACH must happen outside a transaction; BEGIN comes after.
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con, prefix
            con.commit()
        except BaseException:
            con.rollback()
            raise
    finally:
        if attached:
            # DETACH cannot run inside a transaction; rollback above has already
            # ended it, so this is safe on both the success and failure paths.
            try:
                con.execute("DETACH DATABASE %s" % LIFECYCLE_SCHEMA)
            except sqlite3.Error:
                pass
        con.close()


@contextlib.contextmanager
def _transaction(path, con: Optional[sqlite3.Connection] = None):
    """Join the caller's transaction, or own a standalone atomic one.

    When ``con`` is supplied the caller already owns an open transaction, so
    the body runs directly on it and no commit happens here -- the outer cycle
    commits exactly once.  When ``con`` is ``None`` a private connection is
    opened and wrapped in ``BEGIN IMMEDIATE``, preserving the standalone
    atomicity that the order/fill/accounting operations already had.
    """
    if con is not None:
        yield con
        return
    own = connect(path)
    try:
        own.execute("BEGIN IMMEDIATE")
        try:
            yield own
        except BaseException:
            own.rollback()
            raise
        own.commit()
    finally:
        own.close()

def init_db(path):
    con=connect(path)
    try:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
          client_order_id TEXT PRIMARY KEY, exchange_order_id TEXT, symbol TEXT NOT NULL,
          side TEXT NOT NULL, grid_index INTEGER NOT NULL, price TEXT NOT NULL, quantity TEXT NOT NULL,
          status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          executed_qty TEXT NOT NULL DEFAULT '0', remaining_qty TEXT NOT NULL DEFAULT '0'
        );
        CREATE TABLE IF NOT EXISTS fills (
          trade_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
          price TEXT NOT NULL, quantity TEXT NOT NULL, fee TEXT NOT NULL, fee_asset TEXT NOT NULL,
          event_time TEXT NOT NULL, resulting_state TEXT NOT NULL DEFAULT 'OPEN',
          executed_qty TEXT NOT NULL DEFAULT '0', remaining_qty TEXT NOT NULL DEFAULT '0'
        );
        CREATE TABLE IF NOT EXISTS paper_account_state (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          base_asset TEXT NOT NULL,
          quote_asset TEXT NOT NULL,
          base_free TEXT NOT NULL,
          base_reserved TEXT NOT NULL,
          quote_free TEXT NOT NULL,
          quote_reserved TEXT NOT NULL,
          average_cost TEXT NOT NULL,
          realized_pnl TEXT NOT NULL,
          total_fees TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paper_reservations (
          client_order_id TEXT PRIMARY KEY,
          side TEXT NOT NULL,
          asset TEXT NOT NULL,
          original_amount TEXT NOT NULL,
          remaining_amount TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paper_accounting_events (
          event_id TEXT PRIMARY KEY,
          event_type TEXT NOT NULL,
          client_order_id TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS equity_snapshots (
          ts TEXT PRIMARY KEY, equity_quote TEXT NOT NULL, drawdown_pct TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bot_state (
          key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS risk_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, allowed INTEGER NOT NULL,
          reason TEXT NOT NULL, context_json TEXT NOT NULL
        );
        PRAGMA user_version = 321;
        """)
        # Phase 3A migration: older Phase 1/2 databases retain their existing
        # rows and gain the immutable paper-order type with a safe default.
        columns = {row["name"] for row in con.execute("PRAGMA table_info(orders)")}
        if "order_type" not in columns:
            con.execute(
                "ALTER TABLE orders ADD COLUMN order_type TEXT NOT NULL DEFAULT 'LIMIT'"
            )
        if "time_in_force" not in columns:
            con.execute(
                "ALTER TABLE orders ADD COLUMN time_in_force TEXT NOT NULL DEFAULT 'GTC'"
            )
        if "executed_qty" not in columns:
            con.execute(
                "ALTER TABLE orders ADD COLUMN executed_qty TEXT NOT NULL DEFAULT '0'"
            )
        if "remaining_qty" not in columns:
            con.execute(
                "ALTER TABLE orders ADD COLUMN remaining_qty TEXT NOT NULL DEFAULT '0'"
            )
            con.execute("UPDATE orders SET remaining_qty = quantity")
        fill_columns = {row["name"] for row in con.execute("PRAGMA table_info(fills)")}
        if "resulting_state" not in fill_columns:
            con.execute(
                "ALTER TABLE fills ADD COLUMN resulting_state TEXT NOT NULL DEFAULT 'OPEN'"
            )
        if "executed_qty" not in fill_columns:
            con.execute(
                "ALTER TABLE fills ADD COLUMN executed_qty TEXT NOT NULL DEFAULT '0'"
            )
        if "remaining_qty" not in fill_columns:
            con.execute(
                "ALTER TABLE fills ADD COLUMN remaining_qty TEXT NOT NULL DEFAULT '0'"
            )
        con.execute(
            "INSERT OR REPLACE INTO bot_state(key,value) VALUES (?,?)",
            ("schema_version", SCHEMA_VERSION)
        )
        con.commit()
    finally:
        con.close()

def set_state(path,key,value):
    con=connect(path)
    try:
        payload=value if isinstance(value,str) else json.dumps(value,separators=(",",":"))
        con.execute(
            "INSERT INTO bot_state(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key,payload)
        ); con.commit()
    finally: con.close()

def get_state(path,key):
    con=connect(path)
    try:
        row=con.execute("SELECT value FROM bot_state WHERE key=?",(key,)).fetchone()
        return None if row is None else str(row["value"])
    finally: con.close()

def record_risk_event(path,allowed,reason,context):
    con=connect(path)
    try:
        con.execute(
            "INSERT INTO risk_events(ts,allowed,reason,context_json) VALUES (?,?,?,?)",
            (utc_now(),int(allowed),reason,json.dumps(context,default=str))
        ); con.commit()
    finally: con.close()

class OrderPersistenceError(ValueError):
    """Raised when a persisted order's immutable identity would be changed."""

class PaperAccountingStaleState(RuntimeError):
    """Raised when an accounting update was prepared from obsolete state."""

class PaperAccountingMigrationError(RuntimeError):
    """Raised when legacy paper activity cannot be safely migrated."""

class OrderSubmissionError(RuntimeError):
    """Raised when an atomic paper-order submission cannot be completed."""


def _persist_order(con, order):
    existing = con.execute(
        "SELECT symbol,side,order_type,grid_index,price,quantity,time_in_force,created_at "
        "FROM orders WHERE client_order_id=?",
        (order.intent.client_order_id,),
    ).fetchone()
    if existing is not None:
        expected = {
            "symbol": order.intent.symbol,
            "side": order.intent.side,
            "order_type": order.intent.order_type,
            "grid_index": order.intent.grid_index,
            "price": str(order.intent.price),
            "quantity": str(order.intent.quantity),
            "time_in_force": order.intent.time_in_force,
            "created_at": order.intent.created_at.isoformat(),
        }
        for field, value in expected.items():
            stored = existing[field]
            if field == "grid_index":
                matches = int(stored) == value
            elif field in {"price", "quantity"}:
                matches = str(stored) == value
            else:
                matches = stored == value
            if not matches:
                raise OrderPersistenceError(
                    f"Immutable order field mismatch for {order.intent.client_order_id}: "
                    f"{field} differs"
                )
    con.execute(
        "INSERT INTO orders("
        "client_order_id,exchange_order_id,symbol,side,order_type,grid_index,"
        "price,quantity,time_in_force,status,created_at,updated_at,executed_qty,remaining_qty"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(client_order_id) DO UPDATE SET "
        "status=excluded.status,updated_at=excluded.updated_at,"
        "executed_qty=excluded.executed_qty,remaining_qty=excluded.remaining_qty",
        (
            order.intent.client_order_id, None, order.intent.symbol, order.intent.side,
            order.intent.order_type, order.intent.grid_index, str(order.intent.price),
            str(order.intent.quantity), order.intent.time_in_force, order.state.value, order.intent.created_at.isoformat(),
            order.updated_at.isoformat(), str(order.executed_qty),
            str(order.remaining_qty),
        ),
    )


def save_order(path, order, accounting_update=None, expected_order=None, con=None):
    """Insert or update one locally persisted paper order state.

    When ``con`` is supplied, the caller owns the transaction and this function
    must not commit on its own -- it only performs its mutations on ``con`` so
    that they are committed or rolled back together with the rest of the cycle.
    """
    owns = con is None
    if owns:
        con=connect(path)
        con.execute("BEGIN IMMEDIATE")
    try:
        if expected_order is not None:
            current=con.execute(
                "SELECT status,executed_qty,remaining_qty FROM orders "
                "WHERE client_order_id=?",
                (expected_order.intent.client_order_id,),
            ).fetchone()
            if (
                current is None
                or current["status"] != expected_order.state.value
                or Decimal(current["executed_qty"]) != expected_order.executed_qty
                or Decimal(current["remaining_qty"]) != expected_order.remaining_qty
            ):
                raise OrderPersistenceError(
                    f"Paper order {expected_order.intent.client_order_id} changed before update"
                )
        _persist_order(con, order)
        if accounting_update is not None:
            _apply_accounting_update(con, accounting_update)
        if owns:
            con.commit()
    except:
        if owns:
            con.rollback()
        raise
    finally:
        if owns:
            con.close()


def save_order_submission(path, planned, submitted, opened, accounting_update=None, con=None):
    """Persist one complete paper-order submission in a single transaction.

    When ``con`` is supplied, the caller owns the transaction and this function
    must not commit on its own -- it only performs its mutations on ``con`` so
    that they are committed or rolled back together with the rest of the cycle.
    """
    if (
        planned.state.value != "PLANNED"
        or submitted.state.value != "SUBMITTED"
        or opened.state.value != "OPEN"
        or planned.intent.client_order_id != submitted.intent.client_order_id
        or submitted.intent.client_order_id != opened.intent.client_order_id
    ):
        raise OrderSubmissionError("Invalid paper-order submission sequence")

    owns = con is None
    if owns:
        con=connect(path)
        con.execute("BEGIN IMMEDIATE")
    try:
        existing = con.execute(
            "SELECT 1 FROM orders WHERE client_order_id=?",
            (planned.intent.client_order_id,),
        ).fetchone()
        if existing is not None:
            raise OrderSubmissionError(
                f"Duplicate client_order_id {planned.intent.client_order_id} in local state"
            )
        _persist_order(con, planned)
        _persist_order(con, submitted)
        _persist_order(con, opened)
        if accounting_update is not None:
            _apply_accounting_update(con, accounting_update)
        if owns:
            con.commit()
    except Exception as exc:
        if owns:
            con.rollback()
        if isinstance(exc, OrderSubmissionError):
            raise
        raise OrderSubmissionError(
            f"Paper-order submission failed for {planned.intent.client_order_id}"
        ) from exc
    finally:
        if owns:
            con.close()


class FillIdentityMismatch(RuntimeError):
    """Raised when a fill ID is reused with different fill semantics."""


def save_paper_fill(path, old_order, new_order, fill, accounting_update=None, con=None):
    """Atomically update one paper order and insert its fill event.

    When ``con`` is supplied, the caller owns the transaction and this function
    must not commit on its own -- it only performs its mutations on ``con`` so
    that they are committed or rolled back together with the rest of the cycle.
    """
    owns = con is None
    if owns:
        con=connect(path)
        con.execute("BEGIN IMMEDIATE")
    try:
        existing = con.execute(
            "SELECT trade_id,order_id,symbol,side,price,quantity "
            "FROM fills WHERE trade_id=?",
            (fill.fill_id,),
        ).fetchone()
        if existing is not None:
            same_event = (
                existing["trade_id"] == fill.fill_id
                and existing["order_id"] == fill.client_order_id
                and existing["symbol"] == fill.symbol
                and existing["side"] == fill.side
                and Decimal(existing["price"]) == fill.price
                and Decimal(existing["quantity"]) == fill.quantity
            )
            if not same_event:
                raise FillIdentityMismatch(
                    f"Fill identity {fill.fill_id!r} was already used with different semantics"
                )
            # Idempotent replay: nothing was mutated, so the caller keeps
            # ownership of the transaction.  A standalone call still commits
            # its (no-op) transaction so the call contract is unchanged.
            if owns:
                con.commit()
            return False

        current = con.execute(
            "SELECT status,executed_qty,remaining_qty FROM orders "
            "WHERE client_order_id=?",
            (old_order.intent.client_order_id,),
        ).fetchone()
        if current is None:
            raise OrderPersistenceError(
                f"Paper order {old_order.intent.client_order_id} does not exist"
            )
        if (
            current["status"] != old_order.state.value
            or Decimal(current["executed_qty"]) != old_order.executed_qty
            or Decimal(current["remaining_qty"]) != old_order.remaining_qty
        ):
            raise OrderPersistenceError(
                f"Paper order {old_order.intent.client_order_id} changed before fill"
            )

        con.execute(
            "UPDATE orders SET status=?,executed_qty=?,remaining_qty=?,updated_at=? "
            "WHERE client_order_id=?",
            (
                new_order.state.value, str(new_order.executed_qty),
                str(new_order.remaining_qty), new_order.updated_at.isoformat(),
                new_order.intent.client_order_id,
            ),
        )
        con.execute(
            "INSERT INTO fills("
            "trade_id,order_id,symbol,side,price,quantity,fee,fee_asset,event_time,"
            "resulting_state,executed_qty,remaining_qty"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fill.fill_id, fill.client_order_id, fill.symbol, fill.side,
                str(fill.price), str(fill.quantity), "0", "NONE",
                fill.filled_at.isoformat(), fill.state.value,
                str(fill.executed_qty), str(fill.remaining_qty),
            ),
        )
        if accounting_update is not None:
            _apply_accounting_update(con, accounting_update)
        if owns:
            con.commit()
        return True
    except:
        if owns:
            con.rollback()
        raise
    finally:
        if owns:
            con.close()


def get_fill(path, fill_id, con=None):
    """Return one persisted paper-fill event, if present.

    When ``con`` is supplied the caller owns the transaction and this function
    reads from that connection so it sees uncommitted cycle state.
    """
    owns = con is None
    if owns:
        con=connect(path)
    try:
        row=con.execute(
            "SELECT trade_id,order_id,symbol,side,price,quantity,fee,fee_asset,event_time,"
            "resulting_state,executed_qty,remaining_qty "
            "FROM fills WHERE trade_id=?",
            (fill_id,),
        ).fetchone()
        return None if row is None else dict(row)
    finally:
        if owns:
            con.close()


class PaperAccountingEventMismatch(RuntimeError):
    """Raised when a paper-accounting event ID is reused with different semantics."""


def ensure_paper_account_state(path, state):
    """Insert the explicit initial paper account state if it does not exist."""
    con=connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        existing=con.execute(
            "SELECT 1 FROM paper_account_state WHERE id=1"
        ).fetchone()
        if existing is None:
            legacy_activity=con.execute(
                "SELECT 1 FROM orders LIMIT 1"
            ).fetchone()
            if legacy_activity is not None:
                raise PaperAccountingMigrationError(
                    "Existing paper activity has no accounting state; "
                    "refusing to initialize balances without a deterministic migration"
                )
            con.execute(
                "INSERT INTO paper_account_state("
                "id,base_asset,quote_asset,base_free,base_reserved,quote_free,"
                "quote_reserved,average_cost,realized_pnl,total_fees,updated_at"
                ") VALUES (1,?,?,?,?,?,?,?,?,?,?)",
                (
                    state.base_asset,state.quote_asset,str(state.base_free),
                    str(state.base_reserved),str(state.quote_free),
                    str(state.quote_reserved),str(state.average_cost),
                    str(state.realized_pnl),str(state.total_fees),
                    state.updated_at.isoformat(),
                ),
            )
        con.commit()
    except:
        con.rollback()
        raise
    finally:
        con.close()


def get_paper_account_state(path, con=None):
    """Return the current persisted paper account state.

    When ``con`` is supplied the caller owns the transaction and this function
    reads from that connection so it sees uncommitted cycle state.
    """
    owns = con is None
    if owns:
        con = connect(path)
    try:
        row = con.execute(
            "SELECT base_asset,quote_asset,base_free,base_reserved,quote_free,"
            "quote_reserved,average_cost,realized_pnl,total_fees,updated_at "
            "FROM paper_account_state WHERE id=1"
        ).fetchone()
        if row is None:
            return None
        return {
            "base_asset": row["base_asset"],
            "quote_asset": row["quote_asset"],
            "base_free": Decimal(row["base_free"]),
            "base_reserved": Decimal(row["base_reserved"]),
            "quote_free": Decimal(row["quote_free"]),
            "quote_reserved": Decimal(row["quote_reserved"]),
            "average_cost": Decimal(row["average_cost"]),
            "realized_pnl": Decimal(row["realized_pnl"]),
            "total_fees": Decimal(row["total_fees"]),
            "updated_at": datetime.fromisoformat(row["updated_at"]),
        }
    finally:
        if owns:
            con.close()


def get_paper_reservation(path, client_order_id, con=None):
    """Return one persisted paper reservation, if present.

    When ``con`` is supplied the caller owns the transaction and this function
    reads from that connection so it sees uncommitted cycle state.
    """
    owns = con is None
    if owns:
        con = connect(path)
    try:
        row = con.execute(
            "SELECT client_order_id,side,asset,original_amount,remaining_amount,"
            "created_at,updated_at FROM paper_reservations WHERE client_order_id=?",
            (client_order_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "client_order_id": row["client_order_id"],
            "side": row["side"],
            "asset": row["asset"],
            "original_amount": Decimal(row["original_amount"]),
            "remaining_amount": Decimal(row["remaining_amount"]),
            "created_at": datetime.fromisoformat(row["created_at"]),
            "updated_at": datetime.fromisoformat(row["updated_at"]),
        }
    finally:
        if owns:
            con.close()


def _apply_accounting_update(con, update):
    event_payload=json.dumps(update.payload, sort_keys=True, separators=(",", ":"), default=str)
    existing=con.execute(
        "SELECT payload_json FROM paper_accounting_events WHERE event_id=?",
        (update.event_id,),
    ).fetchone()
    if existing is not None:
        if existing["payload_json"] != event_payload:
            raise PaperAccountingEventMismatch(
                f"Accounting event {update.event_id!r} was already applied with different semantics"
            )
        return False

    current_state=con.execute(
        "SELECT base_asset,quote_asset,base_free,base_reserved,quote_free,"
        "quote_reserved,average_cost,realized_pnl,total_fees,updated_at "
        "FROM paper_account_state WHERE id=1"
    ).fetchone()
    if current_state is None:
        raise PaperAccountingStaleState("Paper accounting state is missing")
    old_state=update.old_state
    if (
        current_state["base_asset"] != old_state.base_asset
        or current_state["quote_asset"] != old_state.quote_asset
        or Decimal(current_state["base_free"]) != old_state.base_free
        or Decimal(current_state["base_reserved"]) != old_state.base_reserved
        or Decimal(current_state["quote_free"]) != old_state.quote_free
        or Decimal(current_state["quote_reserved"]) != old_state.quote_reserved
        or Decimal(current_state["average_cost"]) != old_state.average_cost
        or Decimal(current_state["realized_pnl"]) != old_state.realized_pnl
        or Decimal(current_state["total_fees"]) != old_state.total_fees
        or current_state["updated_at"] != old_state.updated_at.isoformat()
    ):
        raise PaperAccountingStaleState(
            "Paper accounting state changed before the update was applied"
        )

    current_reservation=con.execute(
        "SELECT client_order_id,side,asset,original_amount,remaining_amount,"
        "created_at,updated_at FROM paper_reservations WHERE client_order_id=?",
        (update.client_order_id,),
    ).fetchone()
    if update.old_reservation is None:
        if current_reservation is not None:
            raise PaperAccountingStaleState(
                f"Reservation {update.client_order_id!r} already exists"
            )
    elif current_reservation is None:
        raise PaperAccountingStaleState(
            f"Reservation {update.client_order_id!r} is missing"
        )
    else:
        old_reservation=update.old_reservation
        if (
            current_reservation["client_order_id"] != old_reservation.client_order_id
            or current_reservation["side"] != old_reservation.side
            or current_reservation["asset"] != old_reservation.asset
            or Decimal(current_reservation["original_amount"]) != old_reservation.original_amount
            or Decimal(current_reservation["remaining_amount"]) != old_reservation.remaining_amount
            or current_reservation["created_at"] != old_reservation.created_at.isoformat()
            or current_reservation["updated_at"] != old_reservation.updated_at.isoformat()
        ):
            raise PaperAccountingStaleState(
                f"Reservation {update.client_order_id!r} changed before the update was applied"
            )

    con.execute(
        "UPDATE paper_account_state SET base_free=?,base_reserved=?,quote_free=?,"
        "quote_reserved=?,average_cost=?,realized_pnl=?,total_fees=?,updated_at=? "
        "WHERE id=1",
        (
            str(update.new_state.base_free),str(update.new_state.base_reserved),
            str(update.new_state.quote_free),str(update.new_state.quote_reserved),
            str(update.new_state.average_cost),str(update.new_state.realized_pnl),
            str(update.new_state.total_fees),update.new_state.updated_at.isoformat(),
        ),
    )
    if update.reservation is not None:
        con.execute(
            "INSERT INTO paper_reservations("
            "client_order_id,side,asset,original_amount,remaining_amount,created_at,updated_at"
            ") VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(client_order_id) DO UPDATE SET "
            "remaining_amount=excluded.remaining_amount,updated_at=excluded.updated_at",
            (
                update.reservation.client_order_id,update.reservation.side,
                update.reservation.asset,str(update.reservation.original_amount),
                str(update.reservation.remaining_amount),
                update.reservation.created_at.isoformat(),
                update.reservation.updated_at.isoformat(),
            ),
        )
    con.execute(
        "INSERT INTO paper_accounting_events("
        "event_id,event_type,client_order_id,payload_json,created_at"
        ") VALUES (?,?,?,?,?)",
        (
            update.event_id,update.event_type,update.client_order_id,
            event_payload,update.new_state.updated_at.isoformat(),
        ),
    )
    return True

def get_order(path, client_order_id, con=None):
    """Return a persisted order row, if present; it is local state, not exchange truth.

    When ``con`` is supplied the caller owns the transaction and this function
    reads from that connection so it sees uncommitted cycle state.
    """
    owns = con is None
    if owns:
        con=connect(path)
    try:
        row=con.execute(
            "SELECT client_order_id,symbol,side,order_type,grid_index,price,quantity,"
            "time_in_force,status,created_at,updated_at,executed_qty,remaining_qty "
            "FROM orders WHERE client_order_id=?",
            (client_order_id,),
        ).fetchone()
        return None if row is None else dict(row)
    finally:
        if owns: con.close()

def record_equity(path,equity_quote,drawdown_pct):
    con=connect(path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO equity_snapshots(ts,equity_quote,drawdown_pct) VALUES (?,?,?)",
            (utc_now(),str(equity_quote),str(drawdown_pct))
        ); con.commit()
    finally: con.close()

def append_csv_trade(path,row):
    p=Path(path); p.parent.mkdir(parents=True,exist_ok=True)
    fields=list(row.keys()); new_file=not p.exists()
    with p.open("a",newline="",encoding="utf-8") as fh:
        writer=csv.DictWriter(fh,fieldnames=fields)
        if new_file: writer.writeheader()
        writer.writerow(row)
