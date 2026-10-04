"""Public READ-ONLY web dashboard for the adaptive-grid bot.

A separate, independently-restartable observability process.  It reads the
existing SQLite state database and serves a small dark-themed HTML page
plus a JSON snapshot.  It is intentionally public and has NO authentication
(operator decision): security comes from a strictly read-only architecture
and process isolation, not passwords.

Hard boundaries (enforced by construction and pinned by tests):

* **GET-only.**  Exactly three routes exist: ``/`` (HTML), ``/api/status``
  (JSON), ``/healthz`` (health).  Everything else is 404; any non-GET
  method is 405.  There is no POST/PUT/PATCH/DELETE endpoint, no file
  serving, no query-parameter file access, no shell execution, no
  arbitrary SQLite queries, no debug/reload endpoint.
* **Read-only database.**  The SQLite database is opened per request via
  the ``file:...?mode=ro`` URI (SQLite refuses writes at the connection
  level).  No INSERT/UPDATE/DELETE exists in this module.  A database
  failure degrades the snapshot (``db_healthy: false``) instead of
  crashing the process or mutating anything.
* **No trading capability.**  This module imports no order client, no
  exchange adapter, and no trading control path.  It cannot place,
  cancel, replace, release the kill switch, or reset reference equity —
  those operations do not exist here.
* **No secrets.**  The dashboard requires no Binance credentials, never
  reads ``.env``, and never returns environment variables.  Only
  non-sensitive configuration flags (mode, dry_run, symbol, timeframe)
  are exposed, read from ``config.yaml``.

Independent of the trading runtime: if either process crashes, the other
keeps running (the dashboard shows a degraded/stale snapshot).

Configuration (environment variables):
    DASHBOARD_HOST=0.0.0.0
    DASHBOARD_PORT=8080
    GRID_DB_PATH=./data/grid_bot.sqlite3
"""
from __future__ import annotations

import html
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
DEFAULT_DB_PATH = "./data/grid_bot.sqlite3"

#: Tables the snapshot may read (existing schema only — see storage.init_db).
_RECENT_LIMIT = 10


def dashboard_settings() -> dict[str, Any]:
    """Load dashboard + bot display settings (read-only, no secrets)."""
    host = os.environ.get("DASHBOARD_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    raw_port = os.environ.get("DASHBOARD_PORT", str(DEFAULT_PORT)).strip()
    try:
        port = int(raw_port)
    except ValueError:
        raise SystemExit(f"DASHBOARD_PORT must be an integer, got {raw_port!r}")
    if not (1 <= port <= 65535):
        raise SystemExit(f"DASHBOARD_PORT out of range: {port}")
    db_path = os.environ.get("GRID_DB_PATH", DEFAULT_DB_PATH).strip() or DEFAULT_DB_PATH

    bot: dict[str, Any] = {}
    try:
        # Read-only use of the existing loader: validation guarantees the
        # flags shown in the UI are the real, validated safety values.
        import config_loader

        cfg = config_loader.load_config()
        config_loader.validate_config(cfg)
        env = cfg["environment"]
        symbols_raw = cfg.get("symbols", "")
        if symbols_raw:
            symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
        else:
            symbols = [str(cfg.get("symbol", "N/A"))]
        bot = {
            "mode": str(env.get("mode", "N/A")),
            "dry_run": bool(env.get("dry_run", False)),
            "allow_live_execution": bool(env.get("allow_live_execution", False)),
            "symbols": ", ".join(symbols),
            "timeframe": str(cfg.get("timeframe", "N/A")),
            "max_drawdown_pct": str(cfg["risk"]["max_equity_drawdown_pct"]),
            "grid_step_pct": str(cfg["grid"].get("step_pct", cfg["grid"].get("min_gross_profit_pct", "N/A"))),
            "hard_min_net_pct": str(cfg["grid"]["hard_min_net_pct"]),
            "config_error": None,
        }
    except Exception as exc:  # config unreadable: show N/A, never crash
        bot = {"config_error": type(exc).__name__}
    return {"host": host, "port": port, "db_path": db_path, "bot": bot}


# ---------------------------------------------------------------------------
# Read-only database snapshot
# ---------------------------------------------------------------------------
def _connect_readonly(db_path: str) -> sqlite3.Connection:
    """Open the state database strictly read-only (SQLite URI mode)."""
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=2.0)
    con.row_factory = sqlite3.Row
    return con


def _rows(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def _maybe(con: Optional[sqlite3.Connection], fn, default):
    if con is None:
        return default
    try:
        return fn(con)
    except sqlite3.Error:
        return default


def _symbol_db_path(base_path: str, symbol: str) -> str:
    """Generate per-symbol database path."""
    base = Path(base_path)
    return str(base.parent / f"{base.stem}_{symbol}{base.suffix}")


def build_snapshot(db_path: str, bot: dict[str, Any]) -> dict[str, Any]:
    """Assemble the full read-only status snapshot (multi-symbol aware).

    The multi-symbol runtime writes ALL per-symbol state into per-symbol
    databases (``grid_bot_{SYMBOL}.sqlite3``); the configured base database
    only carries legacy single-symbol state.  The snapshot therefore reads
    every symbol's database independently and aggregates:

    - per-symbol: price, range, risk decision, market-filter status, grid
      economics, kill state, run state, paper account, equity, open orders,
      recent fills/cycles;
    - global: sums (equity, PnL, fees), totals (open orders), any-kill,
      merged recent tables (each row tagged with its symbol).

    Every single query is guarded: a missing/corrupt table or database (for
    example a symbol database created by ``init_db`` that has not run a
    paper cycle yet and therefore has no ``paper_orch_cycles`` table)
    degrades only its own section — never the whole response.  When no
    per-symbol database holds a value, the legacy base-database value is
    used as a fallback so single-symbol deployments keep rendering.  Nothing
    here can mutate any database (``mode=ro`` connections only).
    """
    symbols_raw = str(bot.get("symbols", "") or "")
    if symbols_raw.strip():
        symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
    else:
        symbols = [str(bot.get("symbol", "N/A"))]

    # ---- per-symbol databases (authoritative in the multi-symbol runtime) --
    per_symbol_data = {sym: _read_symbol_db(db_path, sym) for sym in symbols}

    # ---- legacy base database (guarded; empty in multi-symbol deployments) --
    main_con: Optional[sqlite3.Connection] = None
    main_db_error: Optional[str] = None
    try:
        main_con = _connect_readonly(db_path)
        # Touch the schema (not just "SELECT 1"): a garbage file only fails
        # when its header is actually read.
        main_con.execute("SELECT name FROM sqlite_master LIMIT 1")
    except (sqlite3.Error, OSError) as exc:
        main_db_error = type(exc).__name__
        if main_con is not None:
            main_con.close()
            main_con = None

    def _kill(c):
        row = _rows(c, "SELECT active, trigger_reason, activated_at, "
                       "open_order_count, cancel_status, note "
                       "FROM kill_state WHERE key = 'kill_state'")
        return row[0] if row else None

    def _run_state(c):
        rows = _rows(c, "SELECT value FROM bot_state WHERE key = 'last_run_state'")
        if not rows:
            return None
        try:
            return json.loads(rows[0]["value"])
        except (ValueError, TypeError):
            return None

    def _state_json(key: str):
        def _read(c):
            rows = _rows(c, "SELECT value FROM bot_state WHERE key = ?", (key,))
            if not rows:
                return None
            try:
                return json.loads(rows[0]["value"])
            except (ValueError, TypeError):
                return None
        return _read

    def _state_raw(key: str):
        def _read(c):
            rows = _rows(c, "SELECT value FROM bot_state WHERE key = ?", (key,))
            return rows[0]["value"] if rows else None
        return _read

    def _reference_equity(c):
        rows = _rows(c, "SELECT value FROM bot_state "
                        "WHERE key = 'paper_reference_equity'")
        return rows[0]["value"] if rows else None

    def _account(c):
        rows = _rows(c, "SELECT base_asset, quote_asset, base_free, base_reserved,"
                        " quote_free, quote_reserved, average_cost, realized_pnl,"
                        " total_fees, updated_at FROM paper_account_state")
        return rows[0] if rows else None

    def _equity_latest(c):
        rows = _rows(c, "SELECT ts, equity_quote, drawdown_pct FROM equity_snapshots"
                        " ORDER BY ts DESC LIMIT 1")
        return rows[0] if rows else None

    def _risk_events(c):
        return _rows(c, "SELECT ts, allowed, reason FROM risk_events"
                        " ORDER BY id DESC LIMIT ?", (_RECENT_LIMIT,))

    def _orders_open(c):
        return _rows(c, "SELECT client_order_id, exchange_order_id, symbol, side,"
                        " grid_index, price, quantity, executed_qty, remaining_qty,"
                        " status, order_type, created_at, updated_at FROM orders"
                        " WHERE status IN ('NEW','PARTIALLY_FILLED')"
                        " ORDER BY created_at DESC LIMIT ?", (_RECENT_LIMIT,))

    def _orders_recent(c):
        return _rows(c, "SELECT client_order_id, symbol, side, grid_index, price,"
                        " quantity, executed_qty, remaining_qty, status,"
                        " created_at, updated_at FROM orders"
                        " ORDER BY created_at DESC LIMIT ?", (_RECENT_LIMIT,))

    def _fills(c):
        return _rows(c, "SELECT trade_id, order_id, symbol, side, price, quantity,"
                        " fee, fee_asset, event_time, resulting_state FROM fills"
                        " ORDER BY event_time DESC LIMIT ?", (_RECENT_LIMIT,))

    def _last_cycles(c):
        rows = _rows(c, "SELECT cycle_id, candle_index, symbol, plan_decision,"
                        " orders_submitted, fills_applied, success, is_idempotent,"
                        " recovery_healthy, blocked_reason, error, metadata,"
                        " created_at FROM paper_orch_cycles"
                        " ORDER BY created_at DESC LIMIT ?", (_RECENT_LIMIT,))
        for row in rows:
            if row.get("metadata"):
                try:
                    row["metadata"] = json.loads(row["metadata"])
                except (ValueError, TypeError):
                    row["metadata"] = None
        return rows

    def _schema_version(c):
        row = c.execute("PRAGMA user_version").fetchone()
        return row[0] if row else None

    # Legacy (single-symbol) values — used only where per-symbol data is absent.
    legacy = {
        "kill_state": _maybe(main_con, _kill, None),
        "run_state": _maybe(main_con, _run_state, None),
        "reference_equity": _maybe(main_con, _reference_equity, None),
        "account": _maybe(main_con, _account, None),
        "equity_latest": _maybe(main_con, _equity_latest, None),
        "risk_events": _maybe(main_con, _risk_events, []),
        "orders_open": _maybe(main_con, _orders_open, []),
        "orders_recent": _maybe(main_con, _orders_recent, []),
        "fills": _maybe(main_con, _fills, []),
        "cycles": _maybe(main_con, _last_cycles, []),
        "last_price": _maybe(main_con, _state_raw("last_price"), None),
        "last_range": _maybe(main_con, _state_json("last_range"), None),
        "last_symbol": _maybe(main_con, _state_raw("last_symbol"), None),
        "last_risk_decision": _maybe(main_con, _state_json("last_risk_decision"), None),
        "last_account_risk": _maybe(main_con, _state_json("last_account_risk"), None),
        "last_adaptive_plan": _maybe(main_con, _state_json("last_adaptive_plan"), None),
        "last_market_intelligence": _maybe(
            main_con, _state_json("last_market_intelligence"), None),
    }

    # ---- aggregates over the per-symbol state --------------------------------
    def _sym_rows(key: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for sym, data in per_symbol_data.items():
            for row in (data.get(key) or []):
                tagged = dict(row)
                tagged.setdefault("symbol", sym)
                rows.append(tagged)
        return rows

    def _dec_sum(values: list) -> Optional[str]:
        total = None
        for raw in values:
            if raw is None:
                continue
            try:
                value = Decimal(str(raw))
            except (InvalidOperation, ValueError, TypeError):
                continue
            total = value if total is None else total + value
        return str(total) if total is not None else None

    kill_states = {sym: data.get("kill_state")
                   for sym, data in per_symbol_data.items()}
    kill_states = {sym: ks for sym, ks in kill_states.items() if ks is not None}
    kill_any = any(bool((ks or {}).get("active")) for ks in kill_states.values())
    if kill_states:
        kill_view: dict[str, Any] = {
            "active": kill_any,
            "per_symbol": kill_states,
        }
    else:
        kill_view = legacy["kill_state"] or {"active": False}

    run_states = {sym: data.get("run_state")
                  for sym, data in per_symbol_data.items()}
    run_states = {sym: rs for sym, rs in run_states.items() if rs is not None}
    if run_states:
        phases = {str((rs or {}).get("phase")) for rs in run_states.values()}
        if any(p == "INTERRUPTED" for p in phases):
            global_phase = "INTERRUPTED"
        elif phases and phases == {"COMPLETED"}:
            global_phase = "COMPLETED"
        else:
            global_phase = "MIXED"
        runtime_view: dict[str, Any] = {
            "phase": global_phase,
            "per_symbol": run_states,
            "run_id": ", ".join(str((rs or {}).get("run_id") or "?")
                                for rs in run_states.values()),
            "risk_allowed": all(bool((rs or {}).get("risk_allowed"))
                                for rs in run_states.values()),
            "open_orders": sum(int((rs or {}).get("open_orders") or 0)
                               for rs in run_states.values()),
        }
    else:
        runtime_view = legacy["run_state"]

    reference_sum = _dec_sum(
        [data.get("paper_reference_equity") for data in per_symbol_data.values()])
    equity_rows = {sym: data.get("equity_latest")
                   for sym, data in per_symbol_data.items()}
    equity_rows = {sym: eq for sym, eq in equity_rows.items() if eq is not None}
    accounts = {sym: data.get("account")
                for sym, data in per_symbol_data.items()}
    accounts = {sym: acc for sym, acc in accounts.items() if acc is not None}

    equity_sum = _dec_sum([eq.get("equity_quote") for eq in equity_rows.values()])
    if equity_sum is None and accounts:
        equity_sum = _dec_sum(
            [acc.get("quote_free") for acc in accounts.values()])
    drawdown_values = [eq.get("drawdown_pct") for eq in equity_rows.values()
                       if eq.get("drawdown_pct") is not None]
    if equity_sum is not None and reference_sum is not None:
        try:
            ref_dec, eq_dec = Decimal(reference_sum), Decimal(equity_sum)
            if ref_dec > 0:
                aggregate_drawdown = str((ref_dec - eq_dec) / ref_dec)
            else:
                aggregate_drawdown = None
        except (InvalidOperation, ValueError, TypeError):
            aggregate_drawdown = None
    else:
        aggregate_drawdown = (legacy["equity_latest"] or {}).get("drawdown_pct")
    if equity_rows:
        equity_view: dict[str, Any] = {
            "equity_quote": equity_sum
                            or (legacy["equity_latest"] or {}).get("equity_quote"),
            "drawdown_pct": aggregate_drawdown,
            "per_symbol": equity_rows,
        }
    else:
        equity_view = legacy["equity_latest"]

    if accounts:
        account_view: dict[str, Any] = {
            "realized_pnl": _dec_sum([a.get("realized_pnl")
                                      for a in accounts.values()])
                             or (legacy["account"] or {}).get("realized_pnl"),
            "total_fees": _dec_sum([a.get("total_fees")
                                    for a in accounts.values()])
                          or (legacy["account"] or {}).get("total_fees"),
            "quote_free": _dec_sum([a.get("quote_free")
                                    for a in accounts.values()]),
            "base_free": _dec_sum([a.get("base_free")
                                   for a in accounts.values()]),
            "by_symbol": accounts,
        }
    else:
        account_view = legacy["account"]

    reference_view = reference_sum if reference_sum is not None \
        else legacy["reference_equity"]

    risk_decisions = {sym: data.get("last_risk_decision")
                      for sym, data in per_symbol_data.items()}
    risk_decisions = {sym: rd for sym, rd in risk_decisions.items()
                      if rd is not None}
    if risk_decisions:
        blocked = [str((rd or {}).get("reason") or "")
                   for rd in risk_decisions.values()
                   if not (rd or {}).get("allowed")]
        risk_decision_view: dict[str, Any] = {
            "allowed": all(bool((rd or {}).get("allowed"))
                           for rd in risk_decisions.values()),
            "reason": " | ".join(r for r in blocked if r) or "PASS",
            "per_symbol": risk_decisions,
        }
    else:
        risk_decision_view = legacy["last_risk_decision"]

    orders_open_merged = _sym_rows("open_orders") or legacy["orders_open"]
    orders_recent_merged = _sym_rows("recent_orders") or legacy["orders_recent"]
    fills_merged = _sym_rows("recent_fills") or legacy["fills"]
    cycles_merged = _sym_rows("latest_cycles") or legacy["cycles"]
    risk_events_merged = _sym_rows("risk_events") or legacy["risk_events"]

    last_prices = {sym: data.get("last_price")
                   for sym, data in per_symbol_data.items()
                   if data.get("last_price") is not None}
    last_ranges = {sym: data.get("last_range")
                   for sym, data in per_symbol_data.items()
                   if data.get("last_range") is not None}
    snapshot: dict[str, Any] = {
        "dashboard": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "db_path_configured": db_path,
            "db_healthy": main_con is not None,
            "db_error": main_db_error,
            "read_only": True,
            "auth": "none (public by design; read-only by construction)",
            "symbols": symbols,
        },
        "environment": {
            "mode": bot.get("mode", "N/A"),
            "dry_run": bot.get("dry_run", None),
            "allow_live_execution": bot.get("allow_live_execution", None),
            "live_execution_disabled": (bot.get("allow_live_execution") is False
                                        and bot.get("dry_run") is True),
            "symbols": bot.get("symbols", "N/A"),
            "timeframe": bot.get("timeframe", "N/A"),
            "config_error": bot.get("config_error"),
        },
        "runtime": runtime_view,
        "risk": {
            "kill_state": kill_view,
            "reference_equity": reference_view,
            "equity_latest": equity_view,
            "max_drawdown_pct": bot.get("max_drawdown_pct", "N/A"),
            "risk_events": risk_events_merged,
            "last_risk_decision": risk_decision_view,
            "last_account_risk": ({"per_symbol": {
                sym: data.get("last_account_risk")
                for sym, data in per_symbol_data.items()
                if data.get("last_account_risk") is not None}}
                if any(data.get("last_account_risk")
                       for data in per_symbol_data.values())
                else legacy["last_account_risk"]),
        },
        "account": account_view,
        "grid": {
            "grid_step_pct": bot.get("grid_step_pct", "N/A"),
            "hard_min_net_pct": bot.get("hard_min_net_pct", "N/A"),
            "latest_cycles": cycles_merged,
            "last_price": (last_prices if last_prices
                           else legacy["last_price"]),
            "last_range": (last_ranges if last_ranges
                           else legacy["last_range"]),
            "last_symbol": legacy["last_symbol"],
            "last_adaptive_plan": (legacy["last_adaptive_plan"]),
            "last_market_intelligence": legacy["last_market_intelligence"],
            "per_symbol": per_symbol_data,
        },
        "orders": {
            "open": orders_open_merged,
            "recent": orders_recent_merged,
        },
        "fills": fills_merged,
        "database": {
            "schema_user_version": _maybe(main_con, _schema_version, None),
            "per_symbol": {
                sym: {
                    "healthy": data.get("db_healthy", False),
                    "error": data.get("db_error"),
                    "missing_tables": data.get("missing_tables") or [],
                }
                for sym, data in per_symbol_data.items()
            },
        },
    }
    if main_con is not None:
        main_con.close()
    return snapshot


def _read_symbol_db(base_db_path: str, symbol: str) -> dict[str, Any]:
    """Read one symbol's database; every query degrades independently.

    A symbol database created by ``storage.init_db`` (which every runtime
    cycle does before any paper cycle runs) does NOT contain the
    orchestrator's ``paper_orch_cycles`` table — that schema is created by
    the paper orchestrator on its first cycle.  Every read below is guarded
    so a missing table, a missing database, or corrupt rows degrade only
    their own section and the dashboard never fails as a whole.
    """
    sym_db = _symbol_db_path(base_db_path, symbol)
    sym_con: Optional[sqlite3.Connection] = None
    sym_error: Optional[str] = None
    try:
        sym_con = _connect_readonly(sym_db)
        # Touch the schema (not just "SELECT 1"): a garbage file only fails
        # when its header is actually read.
        sym_con.execute("SELECT name FROM sqlite_master LIMIT 1")
    except (sqlite3.Error, OSError) as exc:
        sym_error = type(exc).__name__
        if sym_con is not None:
            sym_con.close()
            sym_con = None

    data: dict[str, Any] = {"db_healthy": sym_con is not None,
                            "db_error": sym_error,
                            "missing_tables": []}

    def _guarded(key: str, default, fn: Callable[[sqlite3.Connection], Any]):
        """Run one read; a missing/corrupt table degrades only that section."""
        if sym_con is None:
            data[key] = default
            return
        try:
            data[key] = fn(sym_con)
        except sqlite3.OperationalError as exc:
            data[key] = default
            message = str(exc)
            if "no such table" in message:
                table = message.split("no such table: ", 1)[-1].split()[0]
                if table not in data["missing_tables"]:
                    data["missing_tables"].append(table)
        except sqlite3.Error:
            data[key] = default

    def _rows(c, sql, args=()):
        return [dict(r) for r in c.execute(sql, args).fetchall()]

    def _state_json(c, key: str):
        rows = _rows(c, "SELECT value FROM bot_state WHERE key = ?", (key,))
        if not rows:
            return None
        try:
            return json.loads(rows[0]["value"])
        except (ValueError, TypeError):
            return None

    def _state_raw(c, key: str):
        rows = _rows(c, "SELECT value FROM bot_state WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def _kill(c):
        row = _rows(c, "SELECT active, trigger_reason, activated_at, "
                       "open_order_count, cancel_status, note "
                       "FROM kill_state WHERE key = 'kill_state'")
        return row[0] if row else None

    def _account(c):
        rows = _rows(c, "SELECT base_asset, quote_asset, base_free, base_reserved,"
                        " quote_free, quote_reserved, average_cost, realized_pnl,"
                        " total_fees, updated_at FROM paper_account_state")
        return rows[0] if rows else None

    def _equity_latest(c):
        rows = _rows(c, "SELECT ts, equity_quote, drawdown_pct FROM equity_snapshots"
                        " ORDER BY ts DESC LIMIT 1")
        return rows[0] if rows else None

    def _run_state(c):
        rows = _rows(c, "SELECT value FROM bot_state WHERE key = 'last_run_state'")
        if not rows:
            return None
        try:
            return json.loads(rows[0]["value"])
        except (ValueError, TypeError):
            return None

    # Per-symbol persisted runtime state (bot_state keys)
    _guarded("last_price", None, lambda c: _state_raw(c, "last_price"))
    _guarded("last_range", None, lambda c: _state_json(c, "last_range"))
    _guarded("last_risk_decision", None,
             lambda c: _state_json(c, "last_risk_decision"))
    _guarded("last_symbol", None, lambda c: _state_raw(c, "last_symbol"))
    _guarded("paper_reference_equity", None,
             lambda c: _state_raw(c, "paper_reference_equity"))
    _guarded("paper_cycle_index", None,
             lambda c: _state_raw(c, "paper_cycle_index"))
    _guarded("last_auto_exit_ts", None,
             lambda c: _state_raw(c, "last_auto_exit_ts"))
    _guarded("run_state", None, _run_state)
    # Strategy signal snapshots (NEW strategy: indicators, entry/exit
    # decisions, cooldown, explicit strategy state machine).
    _guarded("last_signal", None, lambda c: _state_json(c, "last_signal"))
    _guarded("last_exit_signal", None,
             lambda c: _state_json(c, "last_exit_signal"))
    _guarded("last_strategy_state", None,
             lambda c: _state_json(c, "last_strategy_state"))

    # Risk + account + orders + fills
    _guarded("kill_state", None, _kill)
    _guarded("account", None, _account)
    _guarded("equity_latest", None, _equity_latest)

    def _risk_events(c):
        rows = _rows(c, "SELECT ts, allowed, reason, context_json FROM risk_events"
                        " ORDER BY id DESC LIMIT ?", (_RECENT_LIMIT,))
        for row in rows:
            if row.get("context_json"):
                try:
                    row["payload"] = json.loads(row["context_json"])
                except (ValueError, TypeError):
                    row["payload"] = None
        return rows

    _guarded("risk_events", [], _risk_events)
    _guarded("open_orders", [], lambda c: _rows(
        c, "SELECT client_order_id, exchange_order_id, symbol, side,"
           " grid_index, price, quantity, executed_qty, remaining_qty,"
           " status, order_type, created_at, updated_at FROM orders"
           " WHERE status IN ('NEW','PARTIALLY_FILLED')"
           " ORDER BY created_at DESC LIMIT ?", (_RECENT_LIMIT,)))
    _guarded("recent_orders", [], lambda c: _rows(
        c, "SELECT client_order_id, symbol, side, grid_index, price,"
           " quantity, executed_qty, remaining_qty, status,"
           " created_at, updated_at FROM orders"
           " ORDER BY created_at DESC LIMIT ?", (_RECENT_LIMIT,)))
    _guarded("recent_fills", [], lambda c: _rows(
        c, "SELECT trade_id, order_id, symbol, side, price, quantity,"
           " fee, fee_asset, event_time, resulting_state FROM fills"
           " ORDER BY event_time DESC LIMIT ?", (_RECENT_LIMIT,)))

    # Orchestrator cycles: table exists only after the first paper cycle ran.
    def _cycles(c):
        rows = _rows(c, "SELECT cycle_id, candle_index, symbol, plan_decision,"
                        " orders_submitted, fills_applied, success, is_idempotent,"
                        " recovery_healthy, blocked_reason, error, metadata,"
                        " created_at FROM paper_orch_cycles"
                        " ORDER BY created_at DESC LIMIT ?", (_RECENT_LIMIT,))
        for row in rows:
            if row.get("metadata"):
                try:
                    row["metadata"] = json.loads(row["metadata"])
                except (ValueError, TypeError):
                    row["metadata"] = None
        return rows

    _guarded("latest_cycles", [], _cycles)

    # Derived per-symbol views (real persisted fields only — no invention).
    decision = data.get("last_risk_decision") or {}
    if bool((data.get("kill_state") or {}).get("active")):
        data["status"] = "KILL_ACTIVE"
    elif decision.get("allowed") is True:
        data["status"] = "ACTIVE"
    elif decision.get("allowed") is False:
        data["status"] = "BLOCKED"
    else:
        data["status"] = "NO_DATA" if sym_con is None or not data.get("last_price") \
            else "NO_DECISION"

    economics: dict[str, Any] = {}
    for event in (data.get("risk_events") or []):
        payload = event.get("payload") or {}
        for key in ("min_net_pct", "dynamic_step_pct", "grid_cells",
                    "atr_pct", "grid_mode", "price", "range"):
            if key in payload and payload[key] is not None:
                economics[key] = payload[key]
        if economics:
            break
    data["grid_economics"] = economics or None

    if sym_con is not None:
        sym_con.close()
    return data


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------
def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else "N/A"))


def _fmt(value: Any) -> str:
    """Human-friendly number: trims Decimal noise, keeps sensible precision.

    ``405275.8339338000000000`` → ``405275.83``; ``765.88000000`` →
    ``765.88``; ``0.00341234`` → ``0.003412``.  Non-numeric values are
    returned unchanged (rendered via :func:`_esc` by the caller).
    """
    if value is None or isinstance(value, bool):
        return str(value) if value is not None else "N/A"
    text = str(value).strip()
    try:
        number = Decimal(text)
    except Exception:
        return text
    if not number.is_finite():
        return text
    magnitude = abs(number)
    if magnitude >= 1000:
        places = 2
    elif magnitude >= 1:
        places = 4
    else:
        places = 6
    quantized = number.quantize(Decimal(1).scaleb(-places))
    text = format(quantized, "f").rstrip("0").rstrip(".")
    return text or "0"


def _pct(value: Any) -> str:
    """Fraction → percentage string (``0.006`` → ``0.60%``)."""
    if value is None or isinstance(value, bool):
        return "N/A"
    text = str(value).strip()
    if text.endswith("%"):
        return text
    try:
        number = Decimal(text)
    except Exception:
        return _esc(value)
    if not number.is_finite():
        return _esc(value)
    return f"{_fmt(number * Decimal(100))}%"


_WIB = timezone(timedelta(hours=7))
_MONTHS_ID = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun",
              "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]


def _wib(value: Any) -> str:
    """UTC timestamp → Indonesian time (WIB).  Unparseable → raw value."""
    if value is None:
        return "N/A"
    text = str(value).strip()
    try:
        # ISO-8601 with timezone
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            # ISO-8601 naive (assume UTC) or plain date
            moment = datetime.fromisoformat(text)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
        except ValueError:
            return _esc(text)
    local = moment.astimezone(_WIB)
    return (f"{local.day:02d} {_MONTHS_ID[local.month - 1]} "
            f"{local.year} {local.hour:02d}:{local.minute:02d}:{local.second:02d} WIB")


def _badge(text: str, cls: str) -> str:
    return f'<span class="badge {cls}">{_esc(text)}</span>'


#: kolom yang berisi timestamp (dirender sebagai WIB)
_TIME_COLUMNS = {"ts", "at", "activated_at", "created_at", "updated_at",
                 "event_time"}
#: kolom yang berisi fraksi (dirender sebagai persen)
_PCT_COLUMNS = {"drawdown_pct", "max_allowed_drawdown", "net_pct_grid",
                "min_net", "grid_step", "grid_step_pct", "hard_min_net",
                "atr_pct", "bb_width"}


def _short_id(value: Any, keep: int = 14) -> str:
    """Short display form of a long identifier (full value in a tooltip)."""
    text = str(value) if value is not None else ""
    if len(text) <= keep:
        return text
    return "…" + text[-keep:]


def _yn(value: Any) -> str:
    """Human label for boolean-ish DB values (0/1/None)."""
    if value is None:
        return "N/A"
    if isinstance(value, str) and value.lower() in ("0", "1", "true", "false"):
        value = int(value) if value.isdigit() else value.lower() == "true"
    if isinstance(value, bool):
        return "YES" if value else "NO"
    return str(value)


# ---------------------------------------------------------------------------
# Machine-code → human language.  The dashboard stores raw machine codes
# (e.g. "NET_PROFIT_BELOW_HARD_MIN | MARKET_FILTER_BLOCK:ADX | ...").  These
# are rendered as plain Indonesian/English labels, with the raw code kept in
# a small muted line below for power users.  Translation only — no data is
# invented or altered.
# ---------------------------------------------------------------------------
#: human-readable label for each machine reason code (the raw code is still
#: shown in a muted line below the human text, for power users).
_REASONS = {
    "NET_PROFIT_BELOW_HARD_MIN": "Net profit per grid below the 0.30% minimum",
    "GRID_COUNT_INVALID": "Grid has fewer than 6 cells",
    "VOLATILITY_TOO_LOW": "Volatility too low to grid",
    "RANGE_QUALITY_TOO_LOW": "Range quality too low",
    "RANGE_WIDTH_OUTSIDE_LIMIT": "Range width outside 3%–25% limits",
    "ADX": "ADX too high (strong trend)",
    "ATR": "ATR too high",
    "BB": "Bollinger width too wide",
    "VOLUME": "Volume spike too high",
    "SPREAD": "Spread too wide",
    "LIQUIDITY": "Liquidity unavailable",
    "TREND_TOO_STRONG": "Trend too strong for a grid",
    "VOLATILITY_TOO_HIGH": "Volatility too high",
    "RANGE_TOO_UNSTABLE": "Range too unstable",
    "EQUITY_DRAWDOWN_KILL": "Equity drawdown kill switch",
    "RANGE_BREAK_BELOW_BUFFER": "Price broke below range buffer",
    "RANGE_BREAK_ABOVE_BUFFER": "Price broke above range buffer",
    "LOWER_BOUNDARY_STOP_15M": "15m lower-boundary stop",
}
#: module prefix captions kept for documentation; the reason renderer uses
#: the leaf code (last ':' segment) so no prefix table is required at runtime.
#: market-intelligence diagnostic keys → plain labels
_DIAGNOSTIC_LABELS = {
    "adx": "ADX (trend strength)",
    "atr_pct": "ATR % (volatility)",
    "bb_width": "Bollinger width",
    "volume_ratio": "Volume ratio",
    "spread_pct": "Spread %",
    "range_quality_score": "Range quality score",
    "min_quality_score": "Minimum quality required",
    "atr_pct_too_low": "ATR too low flag",
    "regime_reason": "Regime reason",
    "error": "Error",
    "volume_oscillator": "Volume Oscillator (5,10)",
    "z_score": "Z-Score (20)",
}


def _diag_label(key: str) -> str:
    return _DIAGNOSTIC_LABELS.get(key, key.replace("_", " ").title())


#: machine regime values → plain words
_REGIME = {
    "RANGE": "Range-bound", "TREND_UP": "Trend up", "TREND_DOWN": "Trend down",
    "VOLATILE": "Volatile", "INSUFFICIENT_DATA": "Not enough data",
    "INVALID_DATA": "Invalid data",
}
#: machine status / decision values → plain words
_STATUS = {
    "GRID_ALLOWED": "Allowed", "GRID_BLOCKED": "Blocked",
    "COMPLETED": "Completed", "INTERRUPTED": "Interrupted",
}


def _kvline(human: str) -> str:
    """A human line (no raw code)."""
    return f"<div class='kvline'>{_esc(human)}</div>"


def _human_code(code: str) -> str:
    """Translate a single machine code to a human phrase (never invent)."""
    code = code.strip()
    if code in _REASONS:
        return _REASONS[code]
    if code in _REGIME:
        return _REGIME[code]
    # a mixed-case sentence with spaces is already human text — keep as-is
    if code and not code.isupper() and " " in code:
        return code
    if code:
        return code.replace("_", " ").title()
    return "N/A"


def _human_reasons(raw: Any) -> str:
    """Render a machine reason string into clean, human-readable lines.

    Input shapes (all produced by the existing engine — not modified here):
      * "CODE | PREFIX:CODE | PREFIX:CODE"
      * "CODE1, CODE2"                        (comma-joined machine codes)
      * "PREFIX:CODE:Range produces only 2 grid cells; minimum is 6"
      * "PREFIX:GRID_BLOCKED:CODE1|CODE2"

    Each '|' / ',' separated reason becomes one human line (the leaf code,
    the last ':' segment, translated).  No raw code is shown.
    """
    if raw is None:
        return "N/A"
    text = str(raw).strip()
    if not text:
        return "N/A"
    lines = []
    for part in re.split(r"\s*[|,]\s*", text):
        part = part.strip()
        if not part:
            continue
        leaf = part.split(":")[-1].strip()
        lines.append(f"<div class='kvline'>{_esc(_human_code(leaf))}</div>")
    if not lines:
        return _esc(_fmt(text))
    return "".join(lines)


def _is_machineish(text: str) -> bool:
    """True when a value looks like machine codes rather than plain text."""
    if "|" in text:
        return True
    tokens = [t for t in re.split(r"\s*[|,]\s*", text) if t]
    return any(t.upper() == t and "_" in t for t in tokens)


def _human(value: Any) -> str:
    """Best-effort human rendering of a value for the key/value tables.

    Reason-looking values (contain '|' or a ':' module prefix) go through
    :func:`_human_reasons`; regime names are translated; everything else
    keeps the existing number/trim formatting.  Translation only — no data
    is invented or altered.
    """
    if value is None:
        return "N/A"
    text = str(value).strip()
    if not text:
        return "N/A"
    # Known machine values: show the human word, with the raw code kept in a
    # muted sub-line (consistent with reason lists; hidden in "penting saja").
    if text in _REGIME:
        return _kvline(_REGIME[text])
    if text in _STATUS:
        return _kvline(_STATUS[text])
    if text in _REASONS:
        return _kvline(_REASONS[text])
    # Reason-looking values: module prefix (e.g. "RANGE:...", "GRID:...") or
    # any machine-code list (contains '|' / comma-joined UPPER_SNAKE codes).
    prefix = text.split(":", 1)[0].upper() if ":" in text else ""
    known_prefix = prefix in {
        "NET_PROFIT", "MARKET_FILTER_BLOCK", "RANGE", "GRID",
        "MARKET_INTELLIGENCE", "ADAPTIVE_PLANNER", "OPEN_ORDERS", "ACCOUNT"}
    if _is_machineish(text) or known_prefix:
        return _human_reasons(text)
    return _esc(_fmt(value))


def _kv(pairs: list[tuple[str, Any]]) -> str:
    """Render (label, value) pairs as a clean two-column list.

    ``value`` is a ready-to-display STRING (number, text, or human-rendered
    HTML).  The label sits on the left, the value on the right; the value is
    HTML-escaped except when it is known reason-render HTML (from
    :func:`_human`), which is inserted verbatim.  This replaces the confusing
    "field | value" header row.
    """
    if not pairs:
        return '<p class="muted">no data</p>'
    rows = []
    for label, value in pairs:
        text = "N/A" if value in (None, "") else str(value)
        # reason-render HTML produced by _human() is safe by construction;
        # everything else is escaped.  A value containing '<div' is the
        # rendered-reason signature.
        value_html = text if "<div" in text else _esc(text)
        rows.append(
            f'<div class="kvrow"><div class="kvlabel">{_esc(label)}</div>'
            f'<div class="kvvalue">{value_html}</div></div>')
    return f'<div class="kvlist">{"".join(rows)}</div>'


def _table(columns: list[str], rows: list[dict]) -> str:
    if not rows:
        return '<p class="muted">no data</p>'

    def _cell(column: str, row: dict) -> str:
        value = row.get(column)
        if column in _TIME_COLUMNS:
            return _wib(value)
        if column in _PCT_COLUMNS:
            return _pct(value)
        if column in ("client_order_id", "order_id", "trade_id"):
            text = _fmt(value)
            return f'<span class="mono" title="{_esc(value)}">{_esc(_short_id(text))}</span>'
        if value is True or value is False:
            return _yn(value)
        return _esc(_fmt(value))

    head = "".join(
        f'<th class="{("num " if c in _NUMERIC_COLUMNS else "")}'
        f'{"wrap" if c in _WRAP_COLUMNS else ""}">{_esc(c)}</th>'
        for c in columns)
    body = ""
    for row in rows:
        cls = ' class="detail"' if row.get("detail") else ""
        tds = []
        for c in columns:
            classes = []
            if c in _NUMERIC_COLUMNS:
                classes.append("num")
            if c in _WRAP_COLUMNS:
                classes.append("wrap")
            tds.append(f'<td class="{" ".join(classes)}">{_cell(c, row)}</td>')
        body += f"<tr{cls}>" + "".join(tds) + "</tr>"
    has_num = any(c in _NUMERIC_COLUMNS for c in columns)
    tbl = "table numalign" if has_num else "table"
    return (f'<div class="tablewrap"><table class="{tbl}"><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>")


_NUMERIC_COLUMNS = {"price", "quantity", "executed_qty", "remaining_qty",
                   "fee", "grid_index", "orders", "fills", "success",
                   "candle_index", "drawdown_pct", "equity_quote"}
#: long free-text columns that should wrap instead of forcing horizontal scroll
_WRAP_COLUMNS = {"reason", "blocked_reason", "filter reasons", "plan reasons",
                 "regime_reason", "value"}


# ---------------------------------------------------------------------------
# Dashboard chrome (retrofuturism theme, mobile-friendly).
# ---------------------------------------------------------------------------
_DASH_CSS = """
:root {
  color-scheme: dark;
  --bg-deep: #05080c;
  --bg-panel: #0a0f1a;
  --bg-panel-hover: #0d1424;
  --border-dim: #1a2338;
  --border-bright: #2a3f6e;
  --fg-primary: #e8f4fd;
  --fg-muted: #6b8aaa;
  --fg-dim: #3d5a8a;
  --accent-cyan: #00ffff;
  --accent-cyan-dim: #00cccc;
  --accent-magenta: #ff00ff;
  --accent-magenta-dim: #cc00cc;
  --accent-amber: #ffbf00;
  --accent-amber-dim: #cc9900;
  --accent-green: #00ff88;
  --accent-green-dim: #00cc6e;
  --accent-red: #ff3366;
  --accent-red-dim: #cc2952;
  --glow-cyan: rgba(0, 255, 255, 0.4);
  --glow-magenta: rgba(255, 0, 255, 0.3);
  --glow-amber: rgba(255, 191, 0, 0.3);
  --font-mono: 'JetBrains Mono', 'Fira Code', 'SF Mono', 'Monaco', 'Consolas', monospace;
  --font-ui: 'Space Grotesk', 'Orbitron', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}

* { box-sizing: border-box; }

@keyframes scanline {
  0% { transform: translateY(-100%); opacity: 0.03; }
  100% { transform: translateY(100vh); opacity: 0.03; }
}

@keyframes pulse-glow {
  0%, 100% { opacity: 0.6; }
  50% { opacity: 1; }
}

@keyframes blink-caret {
  0%, 50% { border-color: var(--accent-cyan); }
  51%, 100% { border-color: transparent; }
}

@keyframes grid-shift {
  0% { background-position: 0 0; }
  100% { background-position: 60px 60px; }
}

body {
  margin: 0;
  background: var(--bg-deep);
  color: var(--fg-primary);
  font-family: var(--font-ui);
  line-height: 1.5;
  min-height: 100vh;
  overflow-x: hidden;
}

/* Retro grid background */
body::before {
  content: "";
  position: fixed;
  inset: 0;
  background-image:
    linear-gradient(var(--border-dim) 1px, transparent 1px),
    linear-gradient(90deg, var(--border-dim) 1px, transparent 1px);
  background-size: 60px 60px;
  animation: grid-shift 20s linear infinite;
  pointer-events: none;
  z-index: 0;
  opacity: 0.4;
}

/* Scanline overlay */
body::after {
  content: "";
  position: fixed;
  inset: 0;
  background: repeating-linear-gradient(
    0deg,
    transparent,
    transparent 2px,
    rgba(0, 255, 255, 0.02) 2px,
    rgba(0, 255, 255, 0.02) 4px
  );
  pointer-events: none;
  z-index: 1;
  opacity: 0.5;
}

main {
  max-width: 1200px;
  margin: 0 auto;
  padding: 20px 16px 40px;
  position: relative;
  z-index: 2;
}

/* Header */
header.dash-header {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 24px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--border-dim);
  position: relative;
}

header.dash-header::after {
  content: "";
  position: absolute;
  bottom: -1px;
  left: 0;
  width: 100%;
  height: 2px;
  background: linear-gradient(90deg,
    transparent,
    var(--accent-cyan) 20%,
    var(--accent-magenta) 50%,
    var(--accent-amber) 80%,
    transparent
  );
  animation: pulse-glow 3s ease-in-out infinite;
}

.logo-block {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.logo {
  font-family: var(--font-mono);
  font-size: 1.6rem;
  font-weight: 700;
  letter-spacing: 0.15em;
  color: var(--accent-cyan);
  text-shadow: 0 0 20px var(--glow-cyan), 0 0 40px var(--glow-cyan);
  position: relative;
}

.logo::before {
  content: "[ ";
  color: var(--accent-magenta);
  text-shadow: 0 0 15px var(--glow-magenta);
}
.logo::after {
  content: " ]";
  color: var(--accent-magenta);
  text-shadow: 0 0 15px var(--glow-magenta);
}

.subtitle {
  font-family: var(--font-mono);
  font-size: 0.7rem;
  color: var(--fg-muted);
  letter-spacing: 0.2em;
  text-transform: uppercase;
}

.status-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

/* Badges - retro pill style */
.badge {
  font-family: var(--font-mono);
  font-size: 0.65rem;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  padding: 4px 10px;
  border-radius: 4px;
  border: 1px solid;
  white-space: nowrap;
  position: relative;
  overflow: hidden;
}

.badge::before {
  content: "";
  position: absolute;
  inset: 0;
  background: inherit;
  opacity: 0.15;
  filter: blur(8px);
  pointer-events: none;
}

.badge-green {
  background: rgba(0, 255, 136, 0.12);
  color: var(--accent-green);
  border-color: var(--accent-green-dim);
  box-shadow: 0 0 12px rgba(0, 255, 136, 0.2), inset 0 0 12px rgba(0, 255, 136, 0.1);
}

.badge-yellow {
  background: rgba(255, 191, 0, 0.12);
  color: var(--accent-amber);
  border-color: var(--accent-amber-dim);
  box-shadow: 0 0 12px rgba(255, 191, 0, 0.2), inset 0 0 12px rgba(255, 191, 0, 0.1);
}

.badge-red {
  background: rgba(255, 51, 102, 0.12);
  color: var(--accent-red);
  border-color: var(--accent-red-dim);
  box-shadow: 0 0 12px rgba(255, 51, 102, 0.2), inset 0 0 12px rgba(255, 51, 102, 0.1);
}

.badge-cyan {
  background: rgba(0, 255, 255, 0.12);
  color: var(--accent-cyan);
  border-color: var(--accent-cyan-dim);
  box-shadow: 0 0 12px rgba(0, 255, 255, 0.2), inset 0 0 12px rgba(0, 255, 255, 0.1);
}

.badge-magenta {
  background: rgba(255, 0, 255, 0.12);
  color: var(--accent-magenta);
  border-color: var(--accent-magenta-dim);
  box-shadow: 0 0 12px rgba(255, 0, 255, 0.2), inset 0 0 12px rgba(255, 0, 255, 0.1);
}

.badge-dim {
  background: rgba(61, 90, 138, 0.3);
  color: var(--fg-dim);
  border-color: var(--border-dim);
}

/* Cards Grid */
.cards-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 14px;
  margin-bottom: 20px;
}

.card {
  background: linear-gradient(145deg, var(--bg-panel), var(--bg-panel-hover));
  border: 1px solid var(--border-dim);
  border-radius: 8px;
  padding: 16px 18px;
  position: relative;
  transition: border-color 0.3s, box-shadow 0.3s, transform 0.2s;
}

.card::before {
  content: "";
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 2px;
  background: linear-gradient(90deg, var(--accent-cyan), var(--accent-magenta), var(--accent-amber));
  opacity: 0;
  transition: opacity 0.3s;
  border-radius: 8px 8px 0 0;
}

.card:hover {
  border-color: var(--border-bright);
  transform: translateY(-2px);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4), 0 0 20px var(--glow-cyan);
}

.card:hover::before {
  opacity: 1;
}

.card-label {
  font-family: var(--font-mono);
  font-size: 0.62rem;
  font-weight: 600;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--fg-dim);
  margin-bottom: 8px;
}

.card-value {
  font-family: var(--font-mono);
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--fg-primary);
  word-break: break-word;
  line-height: 1.3;
}

.card-sub {
  font-family: var(--font-mono);
  font-size: 0.62rem;
  color: var(--fg-muted);
  margin-top: 4px;
  line-height: 1.4;
  word-break: break-word;
}

.card-sub.accent { color: var(--accent-cyan); }
.card-sub.warn { color: var(--accent-amber); }
.card-sub.danger { color: var(--accent-red); }
.card-sub.success { color: var(--accent-green); }

/* Symbol Cards */
.symbols-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 14px;
  margin: 20px 0;
}

.symbol-card {
  border-left: 3px solid var(--accent-cyan);
  background: linear-gradient(145deg, rgba(0, 255, 255, 0.03), var(--bg-panel));
}

.symbol-card:nth-child(2n) { border-left-color: var(--accent-magenta); }
.symbol-card:nth-child(3n) { border-left-color: var(--accent-amber); }
.symbol-card:nth-child(4n) { border-left-color: var(--accent-green); }

.symbol-card .card-value { font-size: 1.1rem; }
.symbol-card .card-sub { font-size: 0.58rem; margin-top: 3px; }

/* Toolbar & Tabs */
.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin: 24px 0 12px;
  padding: 12px 16px;
  background: var(--bg-panel);
  border: 1px solid var(--border-dim);
  border-radius: 8px;
  position: relative;
}

.toolbar::before {
  content: "";
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--accent-cyan), transparent);
}

.tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.tab {
  font-family: var(--font-mono);
  font-size: 0.7rem;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  background: transparent;
  color: var(--fg-muted);
  border: 1px solid var(--border-dim);
  border-radius: 4px;
  padding: 8px 16px;
  cursor: pointer;
  transition: all 0.2s;
  position: relative;
  overflow: hidden;
}

.tab::before {
  content: "";
  position: absolute;
  inset: 0;
  background: linear-gradient(90deg, var(--accent-cyan), var(--accent-magenta));
  opacity: 0;
  transition: opacity 0.2s;
}

.tab:hover {
  color: var(--fg-primary);
  border-color: var(--accent-cyan);
  box-shadow: 0 0 16px var(--glow-cyan);
}

.tab[aria-selected="true"] {
  color: var(--bg-deep);
  border-color: var(--accent-cyan);
  box-shadow: 0 0 20px var(--glow-cyan), inset 0 0 20px rgba(0, 255, 255, 0.2);
}

.tab[aria-selected="true"]::before {
  opacity: 1;
}

.tab[aria-selected="true"] span { position: relative; z-index: 1; }

.penting-toggle {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
  font-family: var(--font-mono);
  font-size: 0.65rem;
  color: var(--fg-muted);
  cursor: pointer;
  user-select: none;
}

.penting-toggle input {
  accent-color: var(--accent-cyan);
  width: 14px;
  height: 14px;
}

.penting-toggle:hover { color: var(--accent-cyan); }

/* Tab Panes */
.tabpane { display: none; }
.tabpane.active { display: block; animation: fade-in 0.3s ease; }

@keyframes fade-in {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}

section.panel {
  background: var(--bg-panel);
  border: 1px solid var(--border-dim);
  border-radius: 8px;
  padding: 20px;
  margin-bottom: 16px;
  position: relative;
}

section.panel::before {
  content: "";
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--accent-cyan), transparent);
}

.panel-title {
  font-family: var(--font-mono);
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  color: var(--accent-cyan);
  margin: 0 0 16px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--border-dim);
  display: flex;
  align-items: center;
  gap: 10px;
}

.panel-title::before {
  content: ">";
  color: var(--accent-magenta);
  animation: blink-caret 1s infinite;
}

.panel-title .count {
  font-family: var(--font-mono);
  font-size: 0.65rem;
  color: var(--fg-dim);
  background: rgba(0, 255, 255, 0.1);
  padding: 2px 8px;
  border-radius: 3px;
  border: 1px solid var(--border-dim);
}

/* Key/Value Lists */
.kvlist {
  display: grid;
  gap: 0;
}

.kvrow {
  display: grid;
  grid-template-columns: 180px 1fr;
  gap: 0 20px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-dim);
  align-items: baseline;
  transition: background 0.2s;
}

.kvrow:last-child { border-bottom: none; }

.kvrow:hover { background: rgba(0, 255, 255, 0.03); }

.kvlabel {
  font-family: var(--font-mono);
  font-size: 0.7rem;
  color: var(--fg-muted);
  letter-spacing: 0.04em;
}

.kvvalue {
  font-family: var(--font-mono);
  font-size: 0.85rem;
  font-weight: 500;
  color: var(--fg-primary);
  word-break: break-word;
  min-width: 0;
}

/* Reason lines */
.kvline {
  font-family: var(--font-mono);
  font-size: 0.75rem;
  color: var(--fg-primary);
  line-height: 1.6;
  padding: 4px 0;
}

.kvline .reason-code {
  color: var(--fg-dim);
  font-size: 0.6rem;
  margin-left: 8px;
  opacity: 0;
  transition: opacity 0.2s;
}

.kvline:hover .reason-code { opacity: 1; }

/* Tables */
.tablewrap {
  overflow-x: auto;
  background: var(--bg-panel);
  border: 1px solid var(--border-dim);
  border-radius: 8px;
  margin: 12px 0;
}

table { border-collapse: collapse; width: 100%; font-size: 0.78rem; }

th, td {
  padding: 10px 12px;
  text-align: left;
  border-bottom: 1px solid var(--border-dim);
  font-family: var(--font-mono);
}

th {
  color: var(--accent-cyan);
  font-weight: 600;
  font-size: 0.65rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  background: rgba(0, 255, 255, 0.05);
  white-space: nowrap;
}

td {
  color: var(--fg-primary);
  white-space: nowrap;
}

td.wrap { white-space: normal; word-break: break-word; max-width: 40ch; }

tr:last-child td { border-bottom: none; }

tr:hover td { background: rgba(0, 255, 255, 0.04); }

tr.detail { display: table-row; }

table.numalign td.num, table.numalign th.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

/* Footer */
.foot {
  font-family: var(--font-mono);
  font-size: 0.62rem;
  color: var(--fg-dim);
  margin-top: 32px;
  padding-top: 16px;
  border-top: 1px solid var(--border-dim);
  text-align: center;
  line-height: 1.8;
}

.foot a { color: var(--accent-cyan); text-decoration: none; border-bottom: 1px dotted var(--accent-cyan); }
.foot a:hover { color: var(--accent-magenta); border-bottom-color: var(--accent-magenta); text-shadow: 0 0 8px var(--glow-magenta); }

/* Mobile */
@media (max-width: 640px) {
  main { padding: 14px 10px 32px; }
  .logo { font-size: 1.2rem; }
  .cards-grid { grid-template-columns: repeat(2, 1fr); gap: 10px; }
  .card { padding: 12px 12px; }
  .card-value { font-size: 1rem; }
  .card-label { font-size: 0.55rem; }
  .symbols-grid { grid-template-columns: 1fr; }
  .symbol-card .card-value { font-size: 1rem; }
  .tab { padding: 6px 10px; font-size: 0.62rem; }
  .toolbar { padding: 10px 12px; }
  .kvrow { grid-template-columns: 140px 1fr; padding: 10px 12px; font-size: 0.8rem; }
  .kvlabel { font-size: 0.62rem; }
  th, td { padding: 8px 8px; font-size: 0.7rem; }
  section.panel { padding: 14px; }
  .panel-title { font-size: 0.62rem; }
}

/* Reduced motion */
@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
  body::before { animation: none; }
  body::after { animation: none; }
}

/* High contrast mode tweaks */
@media (prefers-contrast: high) {
  :root {
    --border-dim: #3a4f78;
    --fg-muted: #9bc4e8;
  }
}
"""

_DASH_JS = """
(function () {
  var tabs = document.querySelectorAll('.tab');
  var panes = document.querySelectorAll('.tabpane');
  tabs.forEach(function (tab) {
    tab.addEventListener('click', function () {
      tabs.forEach(function (t) { t.setAttribute('aria-selected', 'false'); });
      panes.forEach(function (p) { p.classList.remove('active'); });
      tab.setAttribute('aria-selected', 'true');
      var pane = document.getElementById(tab.getAttribute('data-tab'));
      if (pane) { pane.classList.add('active'); }
    });
  });
  var toggle = document.getElementById('penting-toggle');
  var saved = null;
  try { saved = localStorage.getItem('dashboard-penting'); } catch (e) { saved = null; }
  if (toggle) {
    if (saved === '1') { document.body.classList.add('penting-only'); toggle.checked = true; }
    toggle.addEventListener('change', function () {
      document.body.classList.toggle('penting-only', toggle.checked);
      try { localStorage.setItem('dashboard-penting', toggle.checked ? '1' : '0'); }
      catch (e) {}
    });
  }
  // Add hover effect for kvrow reason codes
  document.querySelectorAll('.kvline').forEach(function(line) {
    line.addEventListener('mouseenter', function() {
      this.querySelectorAll('.reason-code').forEach(function(c) { c.style.opacity = '1'; });
    });
    line.addEventListener('mouseleave', function() {
      this.querySelectorAll('.reason-code').forEach(function(c) { c.style.opacity = '0'; });
    });
  });
})();
"""


def render_html(snap: dict[str, Any]) -> str:
    """Dark, mobile-friendly, tabbed, server-rendered page (auto-refresh 12s).

    Layout: a persistent "key data" strip (status badges + metric cards) on
    top, then tabs (General / Risk / Grid / Market / Orders / System).  The
    "penting saja" toggle hides detail rows and detail blocks, showing only
    the essentials.  Everything is read-only; no client logic trades or
    mutates state.
    """
    env = snap["environment"]
    risk = snap["risk"]
    kill = risk.get("kill_state") or {}
    account = snap.get("account") or {}
    equity = risk.get("equity_latest") or {}
    run_state = snap.get("runtime") or {}
    dash = snap["dashboard"]
    cycles = snap["grid"].get("latest_cycles") or []
    last_cycle = cycles[0] if cycles else None

    env_ok = env.get("live_execution_disabled") is True
    mode = str(env.get("mode") or "N/A").upper()
    mode_cls = "badge-green" if mode == "TESTNET" else "badge-yellow"
    env_badge = _badge(f"{mode} / {'PAPER' if env.get('dry_run') else 'LIVE!'}", mode_cls)
    dry_badge = (_badge("DRY RUN", "badge-green") if env.get("dry_run")
                 else _badge("DRY RUN OFF", "badge-red"))
    live_badge = (_badge("LIVE DISABLED", "badge-green") if env_ok
                  else _badge("LIVE FLAG ON", "badge-red"))
    kill_active = bool(kill.get("active"))
    kill_badge = (_badge("KILL ACTIVE", "badge-red") if kill_active
                  else _badge("KILL OFF", "badge-green"))
    db_badge = (_badge("DB OK", "badge-green") if dash.get("db_healthy")
                else _badge("DB UNAVAILABLE", "badge-red"))
    run_phase = str(run_state.get("phase") or "N/A")
    run_badge = (_badge(f"RUNTIME {run_phase}", "badge-green")
                 if run_phase == "COMPLETED" else
                 _badge(f"RUNTIME {run_phase}", "badge-yellow"))

    grid_state = snap["grid"]
    adaptive_plan = grid_state.get("last_adaptive_plan") or {}
    market_intel = grid_state.get("last_market_intelligence") or {}
    risk_decision = risk.get("last_risk_decision") or {}
    plan_decision = str(adaptive_plan.get("decision")
                        or ("GRID_ALLOWED" if risk_decision.get("allowed")
                            else "GRID_BLOCKED"))
    plan_reason = str(adaptive_plan.get("reason")
                      or risk_decision.get("reason") or "")
    plan_allowed = "ALLOW" in plan_decision.upper()
    grid_badge = (_badge("GRID ALLOWED", "badge-green") if plan_allowed
                  else _badge("GRID BLOCKED", "badge-yellow"))
    risk_allowed = bool(risk_decision.get("allowed"))
    risk_badge = (_badge("RISK PASS", "badge-green") if risk_allowed
                  else _badge("RISK BLOCKED", "badge-yellow"))
    intel_status = str(market_intel.get("status") or "N/A")
    if market_intel.get("allowed") is True:
        intel_badge = _badge("MARKET OK", "badge-green")
    elif market_intel.get("allowed") is False:
        intel_badge = _badge("MARKET FILTERED", "badge-yellow")
    else:
        intel_badge = _badge("MARKET N/A", "badge-yellow")
    last_range = grid_state.get("last_range") or {}
    last_price = grid_state.get("last_price")

    def card(label: str, value: Any, sub: str = "") -> str:
        sub_html = f'<div class="card-sub">{_esc(sub)}</div>' if sub else ""
        return (f'<div class="card"><div class="card-label">{_esc(label)}</div>'
                f'<div class="card-value">{_esc(_fmt(value))}</div>{sub_html}</div>')

    def card_pct(label: str, value: Any, sub: str = "") -> str:
        sub_html = f'<div class="card-sub">{_esc(sub)}</div>' if sub else ""
        return (f'<div class="card"><div class="card-label">{_esc(label)}</div>'
                f'<div class="card-value">{_pct(value)}</div>{sub_html}</div>')

    open_orders = snap["orders"].get("open") or []
    quote_free = account.get("quote_free")
    account_risk = risk.get("last_account_risk") or {}
    equity_val = (equity.get("equity_quote") or quote_free
                  or account_risk.get("current_equity"))
    drawdown = equity.get("drawdown_pct") or account_risk.get("drawdown_pct")

    cycle_rows = [
        {"cycle_id": c.get("cycle_id"), "candle_index": c.get("candle_index"),
         "plan": c.get("plan_decision"), "orders": c.get("orders_submitted"),
         "fills": c.get("fills_applied"), "success": c.get("success"),
         "blocked_reason": c.get("blocked_reason"), "at": c.get("created_at")}
        for c in cycles]

    metadata = (last_cycle or {}).get("metadata") or {}
    diag = market_intel.get("diagnostics") or {}

    def _first(*keys_vals):
        """First non-None value among (key, source-dict) pairs."""
        for key, src in keys_vals:
            if isinstance(src, dict) and src.get(key) is not None:
                return src.get(key)
        return None

    grid_detail = {
        # latest cycle state (bot_state keys) wins over historical cycle rows
        "lower_price": _first(("lower", last_range),
                              ("candidate_lower", adaptive_plan),
                              ("lower_price", metadata)),
        "upper_price": _first(("upper", last_range),
                              ("candidate_upper", adaptive_plan),
                              ("upper_price", metadata)),
        "effective_upper": last_range.get("upper")
                           or metadata.get("effective_upper"),
        "grid_cells": _first(("grid_count", adaptive_plan),
                             ("grid_cells", metadata)),
        "net_pct": _first(("estimated_net_profit_per_grid", adaptive_plan),
                          ("net_pct", metadata)),
        "current_price": metadata.get("current_price") or last_price,
        "grid_step": _first(("grid_step", adaptive_plan),
                            ("grid_step", metadata)),
        "range_quality": _first(("range_quality_score", adaptive_plan),
                                ("range_quality_score", market_intel),
                                ("range_quality", metadata)),
        "market_regime": _first(("regime", adaptive_plan),
                                ("regime", market_intel),
                                ("market_regime", metadata)),
    }

    # Build multi-symbol cards
    # Build multi-symbol cards from each symbol's real persisted state
    # (legacy single-symbol grid_detail is the fallback when a symbol has no
    # per-symbol database yet).
    symbols_raw = env.get("symbols", "")
    if symbols_raw:
        symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
    else:
        symbols = [env.get("symbol", "N/A")]

    # Get per-symbol data from grid_state
    per_symbol_data = grid_state.get("per_symbol") or {}

    # Build symbol cards
    symbol_cards = []
    for sym in symbols:
        sym_data = per_symbol_data.get(sym) or {}
        first_symbol = sym == symbols[0]
        sym_price = (sym_data.get("last_price")
                     or (grid_detail.get("current_price") if first_symbol else None))
        sym_range = sym_data.get("last_range") or (
            last_range if first_symbol else None)
        sym_lower = (sym_range or {}).get("lower") or (
            grid_detail.get("lower_price") if first_symbol else None)
        sym_upper = (sym_range or {}).get("upper") or (
            grid_detail.get("upper_price") if first_symbol else None)
        econ = sym_data.get("grid_economics") or {}
        sym_cells = econ.get("grid_cells") or (
            grid_detail.get("grid_cells") if first_symbol else None)
        sym_step = econ.get("dynamic_step_pct") or (
            grid_detail.get("grid_step") if first_symbol else None)
        sym_net = econ.get("min_net_pct") or (
            grid_detail.get("net_pct") if first_symbol else None)
        sym_regime = econ.get("grid_mode") or (
            grid_detail.get("market_regime") if first_symbol else None)
        # Per-symbol status derived from its real risk decision / kill state.
        sym_status = str(sym_data.get("status") or "N/A")
        if sym_status == "KILL_ACTIVE":
            status_badge = _badge("KILL", "badge-red")
        elif sym_status == "ACTIVE":
            status_badge = _badge("ACTIVE", "badge-green")
        elif sym_status == "BLOCKED":
            status_badge = _badge("BLOCKED", "badge-yellow")
        else:
            status_badge = _badge("N/A", "badge-yellow")
        sym_reason = str((sym_data.get("last_risk_decision") or {}).get("reason")
                         or "")
        sym_open = len(sym_data.get("open_orders") or [])
        sym_fills = len(sym_data.get("recent_fills") or [])
        sym_ref = sym_data.get("paper_reference_equity")
        # Per-symbol indicator snapshot + strategy state (NEW strategy).
        signal = sym_data.get("last_signal") or {}
        ind = signal.get("indicators") or {}
        sym_adx = ind.get("adx")
        sym_rsi = ind.get("rsi")
        sym_pb = ind.get("percent_b")
        sym_z = ind.get("z_score")
        sym_atr = ind.get("atr_pct")
        strat_state = str((sym_data.get("last_strategy_state") or {})
                          .get("state") or sym_data.get("status") or "N/A")
        entry_sig = str((signal.get("entry_signal") or {}).get("signal")
                        or "N/A")
        exit_sig = str((sym_data.get("last_exit_signal") or {})
                       .get("triggered_reasons") or "N/A")
        cooldown_until = "N/A"
        last_exit_ts = sym_data.get("last_auto_exit_ts")
        cooldown_hours = 3
        if last_exit_ts:
            try:
                from datetime import timedelta
                last_dt = datetime.fromisoformat(
                    str(last_exit_ts).replace("Z", "+00:00"))
                cooldown_until = _wib(
                    (last_dt + timedelta(hours=cooldown_hours)).isoformat())
            except (ValueError, TypeError):
                cooldown_until = "N/A"
        db_ok = bool(sym_data.get("db_healthy"))
        db_note = "" if db_ok else " · DB unavailable"
        missing = sym_data.get("missing_tables") or []
        if missing:
            db_note += f" · no {missing[0]}"

        symbol_cards.append(f"""
        <div class="card symbol-card">
            <div class="card-label">{_esc(sym)} {status_badge}</div>
            <div class="card-value">{_esc(_fmt(sym_price))}</div>
            <div class="card-sub">Range: {_fmt(sym_lower)} \u2013 {_fmt(sym_upper)}</div>
            <div class="card-sub">Grid: {_fmt(sym_cells)} cells @ {_pct(sym_step)} | Net/grid: {_pct(sym_net)}</div>
            <div class="card-sub">Mode: {_human(sym_regime)} | Ref equity: {_fmt(sym_ref)}</div>
            <div class="card-sub">State: {_esc(strat_state)} | Entry: {_esc(entry_sig)} | Cooldown end: {cooldown_until}</div>
            <div class="card-sub">ADX: {_fmt(sym_adx)} | RSI: {_fmt(sym_rsi)} | %B: {_fmt(sym_pb)} | Z: {_fmt(sym_z)} | ATR%: {_fmt(sym_atr)}</div>
            <div class="card-sub">Open orders: {sym_open} | Recent fills: {sym_fills}</div>
            <div class="card-sub">Last exit: {_esc(exit_sig if isinstance(exit_sig, str) else ", ".join(exit_sig))}</div>
            <div class="card-sub">Risk: {_esc(sym_reason or "N/A")}{_esc(db_note)}</div>
        </div>""")
    
    symbol_cards_html = "".join(symbol_cards)

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="12">
<title>adaptive-grid dashboard</title>
<style>{_DASH_CSS}</style></head>
<body><main>
<header class="dash-header">
  <div class="logo-block">
    <div class="logo">ADAPTIVE GRID</div>
    <div class="subtitle">Binance Spot Grid Monitor</div>
  </div>
  <div class="status-badges">{env_badge} {dry_badge} {live_badge} {run_badge}
  {grid_badge} {risk_badge} {intel_badge} {kill_badge} {db_badge}</div>
</header>

<div class="cards-grid">
{card("Equity", equity_val, f"ref {_fmt(risk.get('reference_equity'))}")}
{card_pct("Drawdown", drawdown, f"limit {_pct(risk.get('max_drawdown_pct'))}")}
{card("Realized PnL", account.get("realized_pnl"), "after fees")}
{card("Total fees", account.get("total_fees"))}
{card("Open orders", len(open_orders))}
</div>

<div class="symbols-grid">
{symbol_cards_html}
</div>

<div class="toolbar">
<nav class="tabs" role="tablist">
<button class="tab" role="tab" data-tab="tab-general" aria-selected="true"><span>General</span></button>
<button class="tab" role="tab" data-tab="tab-risk" aria-selected="false"><span>Risk</span></button>
<button class="tab" role="tab" data-tab="tab-grid" aria-selected="false"><span>Grid</span></button>
<button class="tab" role="tab" data-tab="tab-market" aria-selected="false"><span>Market</span></button>
<button class="tab" role="tab" data-tab="tab-orders" aria-selected="false"><span>Orders & Fills</span></button>
<button class="tab" role="tab" data-tab="tab-system" aria-selected="false"><span>System</span></button>
</nav>
<label class="penting-toggle"><input type="checkbox" id="penting-toggle"><span>penting saja</span></label>
</div>

<section class="tabpane active" id="tab-general" role="tabpanel">
<div class="panel">
  <h2 class="panel-title">Ringkasan</h2>
  {_kv([
      ("Risk decision", "PASS" if risk_allowed else "BLOCKED"),
      ("Plan decision", _human(plan_decision)),
      ("Plan reasons", _human(plan_reason)),
      ("Market filter", _human(intel_status)),
  ])}
</div>
<div class="panel">
  <h2 class="panel-title">Per-symbol status <span class="count">{len(symbols)}</span></h2>
  {_table(["symbol", "status", "last_price", "open orders", "fills",
           "risk reason", "db"],
          [{"symbol": sym,
            "status": (per_symbol_data.get(sym) or {}).get("status") or "N/A",
            "last_price": (per_symbol_data.get(sym) or {}).get("last_price") or "N/A",
            "open orders": len((per_symbol_data.get(sym) or {}).get("open_orders") or []),
            "fills": len((per_symbol_data.get(sym) or {}).get("recent_fills") or []),
            "risk reason": ((per_symbol_data.get(sym) or {})
                            .get("last_risk_decision") or {}).get("reason") or "N/A",
            "db": "OK" if (per_symbol_data.get(sym) or {}).get("db_healthy")
                  else "unavailable"}
           for sym in symbols])}
</div>
<div class="panel">
  <h2 class="panel-title">Recent cycles <span class="count">{len(cycle_rows)}</span></h2>
  {_table(["symbol", "cycle_id", "candle_index", "plan", "orders", "fills", "success",
           "blocked_reason", "at"], [dict(r, **{"class": "detail"}) for r in cycle_rows])}
</div>
</section>

<section class="tabpane" id="tab-risk" role="tabpanel">
<div class="panel">
  <h2 class="panel-title">Risk</h2>
  {_kv([
      ("Kill switch", _yn(kill.get("active", False))),
      ("Kill trigger", _human(kill.get("trigger_reason") or "")),
      ("Activated at", _wib(kill.get("activated_at"))),
      ("Cancel status", _yn(kill.get("cancel_status")) if kill.get("cancel_status") is not None else "N/A"),
      ("Reference equity", _fmt(risk.get("reference_equity"))),
      ("Current drawdown", _pct(drawdown)),
      ("Drawdown limit", _pct(risk.get("max_drawdown_pct"))),
  ])}
</div>
<div class="panel">
  <h2 class="panel-title">Kill state per symbol <span class="count">{len(kill.get("per_symbol") or {})}</span></h2>
  {_table(["symbol", "active", "trigger", "activated_at", "cancel_status"],
          [{"symbol": sym, "active": _yn(bool((ks or {}).get("active"))),
            "trigger": (ks or {}).get("trigger_reason") or "",
            "activated_at": (ks or {}).get("activated_at") or "",
            "cancel_status": (ks or {}).get("cancel_status") or ""}
           for sym, ks in sorted((kill.get("per_symbol") or {}).items())])}
</div>
<div class="panel">
  <h2 class="panel-title">Recent risk events <span class="count">{(len(risk.get("risk_events") or []))}</span></h2>
  {_table(["symbol", "ts", "allowed", "reason"], [dict(r, **{"class": "detail"}) for r in (risk.get("risk_events") or [])])}
</div>
</section>

<section class="tabpane" id="tab-grid" role="tabpanel">
<div class="panel">
  <h2 class="panel-title">Grid</h2>
  {_kv([
      ("Grid step", _pct(grid_detail.get("grid_step") or snap["grid"].get("grid_step_pct"))),
      ("Min net profit / grid", _pct(snap["grid"].get("hard_min_net_pct"))),
      ("Lower price", _fmt(grid_detail.get("lower_price"))),
      ("Upper price", _fmt(grid_detail.get("upper_price"))),
      ("Effective upper", _fmt(grid_detail.get("effective_upper"))),
      ("Grid cells", _fmt(grid_detail.get("grid_cells"))),
      ("Net profit / grid (est.)", _pct(grid_detail.get("net_pct"))),
      ("Current price", _fmt(grid_detail.get("current_price"))),
      ("Plan decision", _human(plan_decision)),
      ("Plan reasons", _human(plan_reason)),
  ])}
</div>
</section>

<section class="tabpane" id="tab-market" role="tabpanel">
<div class="panel">
  <h2 class="panel-title">Market</h2>
  {_kv([
      ("Market regime", _human(grid_detail.get("market_regime")) if grid_detail.get("market_regime") else "N/A"),
      ("Range quality", _fmt(grid_detail.get("range_quality"))),
      ("Filter status", _human(intel_status) if intel_status else "N/A"),
      ("Filter allowed", _yn(market_intel.get("allowed"))),
      ("Filter reasons", _human(", ".join(market_intel.get("reasons") or [])) if market_intel.get("reasons") else None),
      ("Volume Oscillator (5,10)", _fmt(diag.get("volume_oscillator")) if diag.get("volume_oscillator") is not None else "N/A"),
      ("Z-Score (20)", _fmt(diag.get("z_score")) if diag.get("z_score") is not None else "N/A"),
  ] + [
      (_diag_label(k), _fmt(v))
      for k, v in sorted(diag.items()) if not isinstance(v, (dict, list))
  ])}
</div>
</section>

<section class="tabpane" id="tab-orders" role="tabpanel">
<div class="panel">
  <h2 class="panel-title">Open orders <span class="count">{len(open_orders)}</span></h2>
  {_table(["client_order_id", "side", "grid_index", "price", "quantity",
           "executed_qty", "remaining_qty", "status", "type", "created_at"],
          open_orders)}
</div>
<div class="panel">
  <h2 class="panel-title">Recent orders</h2>
  {_table(["client_order_id", "side", "grid_index", "price", "quantity",
           "executed_qty", "remaining_qty", "status", "created_at"],
          snap["orders"].get("recent") or [])}
</div>
<div class="panel">
  <h2 class="panel-title">Recent fills</h2>
  {_table(["event_time", "side", "price", "quantity", "fee", "fee_asset",
           "resulting_state", "symbol"],
          snap["fills"] or [])}
</div>
</section>

<section class="tabpane" id="tab-system" role="tabpanel">
<div class="panel">
  <h2 class="panel-title">System</h2>
  {_kv([
      ("Dashboard time", _wib(dash.get("generated_at_utc"))),
      ("Database healthy", _yn(dash.get("db_healthy"))),
      ("Database error", _human(dash.get("db_error") or "")),
      ("Schema user_version", _fmt((snap.get("database") or {}).get("schema_user_version"))),
      ("Last run id", _fmt(run_state.get("run_id"))),
      ("Last run phase", _human(run_state.get("phase") or "")),
      ("Last run risk allowed", _yn(run_state.get("risk_allowed"))),
      ("Open orders at last run", _fmt(run_state.get("open_orders"))),
      ("Read-only", _yn(dash.get("read_only"))),
  ])}
</div>
<div class="panel">
  <h2 class="panel-title">Runtime state per symbol <span class="count">{len((run_state or {}).get("per_symbol") or {})}</span></h2>
  {_table(["symbol", "phase", "run_id", "risk_allowed", "open_orders", "pending_cancels"],
          [{"symbol": sym,
            "phase": (rs or {}).get("phase") or "N/A",
            "run_id": (rs or {}).get("run_id") or "N/A",
            "risk_allowed": _yn(bool((rs or {}).get("risk_allowed"))),
            "open_orders": (rs or {}).get("open_orders") or 0,
            "pending_cancels": (rs or {}).get("pending_cancels") or 0}
           for sym, rs in sorted(((run_state or {}).get("per_symbol") or {}).items())])}
</div>
<div class="panel">
  <h2 class="panel-title">Per-symbol databases <span class="count">{len(symbols)}</span></h2>
  {_table(["symbol", "healthy", "error", "missing tables"],
          [{"symbol": sym,
            "healthy": _yn(bool((per_symbol_data.get(sym) or {}).get("db_healthy"))),
            "error": (per_symbol_data.get(sym) or {}).get("db_error") or "",
            "missing tables": ", ".join(
                (per_symbol_data.get(sym) or {}).get("missing_tables") or []) or "none"}
           for sym in symbols])}
</div>
</section>
<p class="foot">Public read-only monitor \u00b7 no authentication by design
\u00b7 this dashboard cannot place, cancel, or modify orders, cannot change
risk or configuration, and cannot release the kill switch \u00b7 page
auto-refreshes every 12s \u00b7 API: <code>/api/status</code>,
<code>/healthz</code></p>
<script>{_DASH_JS}</script>
</main></body></html>"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
def make_handler(settings: dict[str, Any]):
    """Build the request handler bound to the given (immutable) settings."""

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "adaptive-grid-dashboard/1.0"
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # The dashboard is strictly observational: no caching surprises
            # and no need for anything beyond same-origin framing rules.
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        _KNOWN_ROUTES = ("/", "/api/status", "/healthz")

        def _not_found(self) -> None:
            self._send(404, b'{"error": "not found"}', "application/json")

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/healthz":
                body = json.dumps({"status": "ok", "read_only": True,
                                   "auth": "none"}).encode()
                self._send(200, body, "application/json")
                return
            if path == "/api/status":
                try:
                    snap = build_snapshot(settings["db_path"], settings["bot"])
                    body = json.dumps(snap, default=str).encode()
                    self._send(200, body, "application/json")
                except Exception as exc:
                    # Controlled degradation — never a crash, never a write.
                    body = json.dumps({"error": "snapshot unavailable",
                                       "detail": type(exc).__name__}).encode()
                    self._send(503, body, "application/json")
                return
            if path == "/":
                try:
                    snap = build_snapshot(settings["db_path"], settings["bot"])
                    self._send(200, render_html(snap).encode(), "text/html; charset=utf-8")
                except Exception as exc:
                    body = (f"<html><body><h1>dashboard error</h1>"
                            f"<p>snapshot unavailable ({_esc(type(exc).__name__)})"
                            f"</p></body></html>").encode()
                    self._send(503, body, "text/html; charset=utf-8")
                return
            self._not_found()

        def do_POST(self) -> None:
            self._method_not_allowed()

        do_PUT = do_PATCH = do_DELETE = do_POST

        def _method_not_allowed(self) -> None:
            # Unknown paths 404 regardless of method (no hidden admin
            # routes); known routes are GET-only.
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path not in self._KNOWN_ROUTES:
                self._not_found()
                return
            self.send_response(405)
            self.send_header("Allow", "GET")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, fmt: str, *args) -> None:
            # Default access logging to stderr (journald); never logs
            # credentials (none exist) or response bodies.
            import sys
            sys.stderr.write("dashboard: " + fmt % args + "\n")

    return DashboardHandler


def create_server(host: Optional[str] = None, port: Optional[int] = None,
                  db_path: Optional[str] = None) -> ThreadingHTTPServer:
    """Build the dashboard server (bind address configurable for tests)."""
    settings = dashboard_settings()
    host = settings["host"] if host is None else host
    port = settings["port"] if port is None else port
    if db_path is not None:
        settings["db_path"] = db_path
    return ThreadingHTTPServer((host, port), make_handler(settings))


def main() -> int:
    settings = dashboard_settings()
    logging_enabled = True
    if logging_enabled:
        print("adaptive-grid dashboard: PUBLIC READ-ONLY monitor "
              "(no authentication by design; ensure the network layer "
              "matches your exposure intent)")
    print(f"listening on http://{settings['host']}:{settings['port']}/ "
          f"(db: {settings['db_path']}, mode: read-only)")
    server = create_server()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
