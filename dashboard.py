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
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

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
        bot = {
            "mode": str(env.get("mode", "N/A")),
            "dry_run": bool(env.get("dry_run", False)),
            "allow_live_execution": bool(env.get("allow_live_execution", False)),
            "symbol": str(cfg.get("symbol", "N/A")),
            "timeframe": str(cfg.get("timeframe", "N/A")),
            "max_drawdown_pct": str(cfg["risk"]["max_equity_drawdown_pct"]),
            "grid_step_pct": str(cfg["grid"]["step_pct"]),
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


def build_snapshot(db_path: str, bot: dict[str, Any]) -> dict[str, Any]:
    """Assemble the full read-only status snapshot.

    Every table read is independent: a missing/corrupt table degrades only
    its own section (``None``), never the whole response.  Nothing here
    can mutate the database (``mode=ro`` connection).
    """
    con: Optional[sqlite3.Connection] = None
    db_error: Optional[str] = None
    try:
        con = _connect_readonly(db_path)
        con.execute("SELECT 1")
    except (sqlite3.Error, OSError) as exc:
        db_error = type(exc).__name__

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

    snapshot: dict[str, Any] = {
        "dashboard": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "db_path_configured": db_path,
            "db_healthy": con is not None,
            "db_error": db_error,
            "read_only": True,
            "auth": "none (public by design; read-only by construction)",
        },
        "environment": {
            "mode": bot.get("mode", "N/A"),
            "dry_run": bot.get("dry_run", None),
            "allow_live_execution": bot.get("allow_live_execution", None),
            "live_execution_disabled": (bot.get("allow_live_execution") is False
                                        and bot.get("dry_run") is True),
            "symbol": bot.get("symbol", "N/A"),
            "timeframe": bot.get("timeframe", "N/A"),
            "config_error": bot.get("config_error"),
        },
        "runtime": _maybe(con, _run_state, None),
        "risk": {
            "kill_state": _maybe(con, _kill, None),
            "reference_equity": _maybe(con, _reference_equity, None),
            "equity_latest": _maybe(con, _equity_latest, None),
            "max_drawdown_pct": bot.get("max_drawdown_pct", "N/A"),
            "risk_events": _maybe(con, _risk_events, []),
        },
        "account": _maybe(con, _account, None),
        "grid": {
            "grid_step_pct": bot.get("grid_step_pct", "N/A"),
            "hard_min_net_pct": bot.get("hard_min_net_pct", "N/A"),
            "latest_cycles": _maybe(con, _last_cycles, []),
        },
        "orders": {
            "open": _maybe(con, _orders_open, []),
            "recent": _maybe(con, _orders_recent, []),
        },
        "fills": _maybe(con, _fills, []),
        "database": {"schema_user_version": _maybe(con, _schema_version, None)},
    }
    if con is not None:
        con.close()
    return snapshot


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------
def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else "N/A"))


def _badge(text: str, cls: str) -> str:
    return f'<span class="badge {cls}">{_esc(text)}</span>'


def _table(columns: list[str], rows: list[dict]) -> str:
    if not rows:
        return '<p class="muted">no data</p>'
    head = "".join(f"<th>{_esc(c)}</th>" for c in columns)
    body = ""
    for row in rows:
        body += "<tr>" + "".join(
            f"<td>{_esc(row.get(c))}</td>" for c in columns) + "</tr>"
    return (f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>")


def render_html(snap: dict[str, Any]) -> str:
    """Dark, mobile-friendly, server-rendered page (auto-refresh 12s)."""
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
    mode_cls = "green" if mode == "TESTNET" else "yellow"
    env_badge = _badge(f"{mode} / {'PAPER' if env.get('dry_run') else 'LIVE!'}",
                       mode_cls)
    dry_badge = (_badge("DRY RUN", "green") if env.get("dry_run")
                 else _badge("DRY RUN OFF", "red"))
    live_badge = (_badge("LIVE DISABLED", "green") if env_ok
                  else _badge("LIVE FLAG ON", "red"))
    kill_active = bool(kill.get("active"))
    kill_badge = (_badge("KILL SWITCH ACTIVE", "red") if kill_active
                  else _badge("KILL SWITCH OFF", "green"))
    db_badge = (_badge("DB OK", "green") if dash.get("db_healthy")
                else _badge("DB UNAVAILABLE", "red"))
    run_phase = str(run_state.get("phase") or "N/A")
    run_badge = (_badge(f"RUNTIME {run_phase}", "green")
                 if run_phase == "COMPLETED" else
                 _badge(f"RUNTIME {run_phase}", "yellow"))
    plan_decision = (last_cycle or {}).get("plan_decision") or "N/A"
    grid_badge = (_badge(f"GRID {plan_decision}", "green")
                  if "ALLOW" in str(plan_decision).upper()
                  else _badge(f"GRID {plan_decision}", "yellow"))

    def card(label: str, value: Any, sub: str = "") -> str:
        sub_html = f'<div class="cardsub">{_esc(sub)}</div>' if sub else ""
        return (f'<div class="card"><div class="cardlabel">{_esc(label)}</div>'
                f'<div class="cardvalue">{_esc(value)}</div>{sub_html}</div>')

    open_orders = snap["orders"].get("open") or []
    quote_free = account.get("quote_free")
    equity_val = equity.get("equity_quote") or quote_free
    drawdown = equity.get("drawdown_pct")

    cycle_rows = [
        {"cycle_id": c.get("cycle_id"), "candle_index": c.get("candle_index"),
         "plan": c.get("plan_decision"), "orders": c.get("orders_submitted"),
         "fills": c.get("fills_applied"), "success": c.get("success"),
         "blocked_reason": c.get("blocked_reason"), "at": c.get("created_at")}
        for c in cycles]

    metadata = (last_cycle or {}).get("metadata") or {}
    grid_detail = {
        "lower_price": metadata.get("lower_price"),
        "upper_price": metadata.get("upper_price"),
        "effective_upper": metadata.get("effective_upper"),
        "grid_cells": metadata.get("grid_cells"),
        "net_pct": metadata.get("net_pct"),
        "current_price": metadata.get("current_price"),
        "range_quality": metadata.get("range_quality"),
        "adx": metadata.get("adx"),
        "atr_pct": metadata.get("atr_pct"),
        "bb_width": metadata.get("bb_width"),
        "volume_ratio": metadata.get("volume_ratio"),
        "market_regime": metadata.get("market_regime"),
    }

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="12">
<title>adaptive-grid dashboard</title>
<style>
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:#0d1117; color:#e6edf3; font-family:
  -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; }}
main {{ max-width:1080px; margin:0 auto; padding:16px; }}
h1 {{ font-size:1.5rem; margin:8px 0 2px; }}
h2 {{ font-size:1.05rem; margin:28px 0 8px; color:#8b949e;
  text-transform:uppercase; letter-spacing:.08em; }}
.sub {{ color:#8b949e; margin:0 0 16px; }}
.badges {{ display:flex; flex-wrap:wrap; gap:8px; margin:12px 0; }}
.badge {{ padding:4px 10px; border-radius:999px; font-size:.8rem;
  font-weight:600; border:1px solid transparent; }}
.green {{ background:#0f2d1d; color:#3fb950; border-color:#1d4527; }}
.yellow {{ background:#332a0d; color:#d29922; border-color:#574413; }}
.red {{ background:#3d1114; color:#f85149; border-color:#67282c; }}
.cards {{ display:grid; gap:12px; grid-template-columns:
  repeat(auto-fit, minmax(150px, 1fr)); }}
.card {{ background:#161b22; border:1px solid #30363d; border-radius:10px;
  padding:12px 14px; }}
.cardlabel {{ color:#8b949e; font-size:.75rem; text-transform:uppercase;
  letter-spacing:.06em; }}
.cardvalue {{ font-size:1.25rem; font-weight:600; margin-top:4px;
  word-break:break-all; }}
.cardsub {{ color:#8b949e; font-size:.75rem; margin-top:2px; }}
.tablewrap {{ overflow-x:auto; background:#161b22; border:1px solid #30363d;
  border-radius:10px; }}
table {{ border-collapse:collapse; width:100%; font-size:.85rem; }}
th, td {{ padding:7px 10px; text-align:left; border-bottom:1px solid #21262d;
  white-space:nowrap; }}
th {{ color:#8b949e; font-weight:600; background:#161b22; }}
tr:last-child td {{ border-bottom:none; }}
.muted {{ color:#8b949e; padding:0 4px; }}
.foot {{ color:#8b949e; font-size:.75rem; margin-top:24px; }}
</style></head>
<body><main>
<h1>ADAPTIVE GRID</h1>
<p class="sub">Binance Spot Grid Monitor &middot; {_esc(env.get("symbol"))} &middot;
{_esc(env.get("timeframe"))} &middot; read-only</p>
<div class="badges">{env_badge} {dry_badge} {live_badge} {run_badge}
{grid_badge} {kill_badge} {db_badge}</div>

<div class="cards">
{card("Equity (quote)", equity_val, "latest equity snapshot")}
{card("Drawdown", drawdown is not None and f"{drawdown}" or "N/A",
      f"limit {risk.get('max_drawdown_pct', 'N/A')}")}
{card("Realized PnL", account.get("realized_pnl"), "after fees")}
{card("Total fees", account.get("total_fees"))}
{card("Open orders", len(open_orders))}
{card("Grid step", snap['grid'].get('grid_step_pct'),
      f"min net {snap['grid'].get('hard_min_net_pct')}")}
</div>

<h2>Risk</h2>
{_table(["field", "value"], [
    {"field": "kill switch active", "value": kill.get("active", False)},
    {"field": "kill trigger", "value": kill.get("trigger_reason")},
    {"field": "activated at", "value": kill.get("activated_at")},
    {"field": "cancel status", "value": kill.get("cancel_status")},
    {"field": "reference equity", "value": risk.get("reference_equity")},
    {"field": "latest drawdown", "value": drawdown},
    {"field": "max allowed drawdown", "value": risk.get("max_drawdown_pct")},
])}
<h3>Recent risk events</h3>
{_table(["ts", "allowed", "reason"], risk.get("risk_events") or [])}

<h2>Grid</h2>
{_table(["field", "value"], [
    {"field": "grid step", "value": snap["grid"].get("grid_step_pct")},
    {"field": "min net profit per grid",
     "value": snap["grid"].get("hard_min_net_pct")},
    {"field": "lower price", "value": grid_detail.get("lower_price")},
    {"field": "upper price", "value": grid_detail.get("upper_price")},
    {"field": "effective upper", "value": grid_detail.get("effective_upper")},
    {"field": "grid cells", "value": grid_detail.get("grid_cells")},
    {"field": "net profit / grid", "value": grid_detail.get("net_pct")},
    {"field": "current price", "value": grid_detail.get("current_price")},
    {"field": "range quality", "value": grid_detail.get("range_quality")},
    {"field": "market regime", "value": grid_detail.get("market_regime")},
    {"field": "ADX", "value": grid_detail.get("adx")},
    {"field": "ATR %", "value": grid_detail.get("atr_pct")},
    {"field": "BB width", "value": grid_detail.get("bb_width")},
    {"field": "volume ratio", "value": grid_detail.get("volume_ratio")},
])}

<h2>Recent cycles</h2>
{_table(["cycle_id", "candle_index", "plan", "orders", "fills", "success",
         "blocked_reason", "at"], cycle_rows)}

<h2>Open orders</h2>
{_table(["client_order_id", "side", "grid_index", "price", "quantity",
         "executed_qty", "remaining_qty", "status", "type", "created_at"],
        open_orders)}

<h2>Recent orders</h2>
{_table(["client_order_id", "side", "grid_index", "price", "quantity",
         "executed_qty", "remaining_qty", "status", "created_at"],
        snap["orders"].get("recent") or [])}

<h2>Recent fills</h2>
{_table(["event_time", "side", "price", "quantity", "fee", "fee_asset",
         "resulting_state", "symbol"],
        snap["fills"] or [])}

<h2>System</h2>
{_table(["field", "value"], [
    {"field": "dashboard time (UTC)", "value": dash.get("generated_at_utc")},
    {"field": "database healthy", "value": dash.get("db_healthy")},
    {"field": "database error", "value": dash.get("db_error")},
    {"field": "schema user_version", "value":
        (snap.get("database") or {}).get("schema_user_version")},
    {"field": "last run id", "value": run_state.get("run_id")},
    {"field": "last run phase", "value": run_state.get("phase")},
    {"field": "last run risk allowed", "value": run_state.get("risk_allowed")},
    {"field": "open orders at last run", "value": run_state.get("open_orders")},
    {"field": "read-only", "value": dash.get("read_only")},
])}
<p class="foot">Public read-only monitor &middot; no authentication by design
&middot; this dashboard cannot place, cancel, or modify orders, cannot change
risk or configuration, and cannot release the kill switch &middot; page
auto-refreshes every 12s &middot; API: <code>/api/status</code>,
<code>/healthz</code></p>
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

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
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
