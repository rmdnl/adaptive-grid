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
from decimal import Decimal
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
            "last_risk_decision": _maybe(
                con, _state_json("last_risk_decision"), None),
            "last_account_risk": _maybe(
                con, _state_json("last_account_risk"), None),
        },
        "account": _maybe(con, _account, None),
        "grid": {
            "grid_step_pct": bot.get("grid_step_pct", "N/A"),
            "hard_min_net_pct": bot.get("hard_min_net_pct", "N/A"),
            "latest_cycles": _maybe(con, _last_cycles, []),
            "last_price": _maybe(con, _state_raw("last_price"), None),
            "last_range": _maybe(con, _state_json("last_range"), None),
            "last_symbol": _maybe(con, _state_raw("last_symbol"), None),
            "last_adaptive_plan": _maybe(
                con, _state_json("last_adaptive_plan"), None),
            "last_active_plan": _maybe(
                con, _state_json("last_active_plan"), None),
            "last_market_intelligence": _maybe(
                con, _state_json("last_market_intelligence"), None),
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
# Dashboard chrome (dark, mobile-friendly). Plain strings so they can be
# interpolated into the f-string body without brace-escaping.
# ---------------------------------------------------------------------------
_DASH_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; background:#0d1117; color:#e6edf3; font-family:
  -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; }
main { max-width:1080px; margin:0 auto; padding:14px 14px 32px; }
h1 { font-size:1.4rem; margin:6px 0 2px; }
.sub { color:#8b949e; margin:0 0 14px; font-size:.9rem; }
.badges { display:flex; flex-wrap:wrap; gap:8px; margin:10px 0; }
.badge { padding:4px 10px; border-radius:999px; font-size:.78rem;
  font-weight:600; border:1px solid transparent; white-space:nowrap; }
.green { background:#0f2d1d; color:#3fb950; border-color:#1d4527; }
.yellow { background:#332a0d; color:#d29922; border-color:#574413; }
.red { background:#3d1114; color:#f85149; border-color:#67282c; }
.cards { display:grid; gap:10px; grid-template-columns:
  repeat(auto-fit, minmax(150px, 1fr)); }
.card { background:#161b22; border:1px solid #30363d; border-radius:10px;
  padding:12px 14px; }
.cardlabel { color:#8b949e; font-size:.72rem; text-transform:uppercase;
  letter-spacing:.06em; }
.cardvalue { font-size:1.3rem; font-weight:600; margin-top:4px;
  word-break:break-word; }
.cardsub { color:#8b949e; font-size:.72rem; margin-top:2px;
  word-break:break-word; }
.toolbar { display:flex; flex-wrap:wrap; align-items:center; gap:10px;
  margin:16px 0 6px; }
.tabs { display:flex; flex-wrap:wrap; gap:6px; }
.tab { background:#161b22; color:#c9d1d9; border:1px solid #30363d;
  border-radius:8px; padding:6px 12px; font-size:.85rem; cursor:pointer;
  font-family:inherit; }
.tab:hover { border-color:#8b949e; }
.tab[aria-selected="true"] { background:#1f6feb22; color:#58a6ff;
  border-color:#1f6feb; }
.penting { margin-left:auto; display:flex; align-items:center; gap:6px;
  font-size:.8rem; color:#8b949e; cursor:pointer; user-select:none; }
.penting input { accent-color:#1f6feb; }
.tabpane { display:none; }
.tabpane.active { display:block; }
section.block { margin:18px 0; }
h2 { font-size:1rem; margin:18px 0 8px; color:#8b949e;
  text-transform:uppercase; letter-spacing:.06em; }
h3 { font-size:.9rem; margin:14px 0 6px; color:#c9d1d9; }
.tablewrap { overflow-x:auto; background:#161b22; border:1px solid #30363d;
  border-radius:10px; margin:8px 0; }
table { border-collapse:collapse; width:100%; font-size:.85rem; }
th, td { padding:7px 10px; text-align:left; border-bottom:1px solid #21262d; }
th { color:#8b949e; font-weight:600; background:#161b22; white-space:nowrap; }
td { white-space:nowrap; }
td.wrap { white-space:normal; word-break:break-word; max-width:38ch; }
tr:last-child td { border-bottom:none; }
table.numalign td.num, table.numalign th.num { text-align:right;
  font-variant-numeric:tabular-nums; }
.muted { color:#8b949e; padding:0 4px; }
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:.85em; color:#c9d1d9; }
.foot { color:#8b949e; font-size:.75rem; margin-top:24px; line-height:1.5; }
/* key/value lists (replaces the confusing "field | value" tables) */
.kvlist { background:#161b22; border:1px solid #30363d; border-radius:10px;
  overflow-x:auto; }
.kvrow { display:grid; grid-template-columns:150px 1fr; gap:0 14px;
  padding:9px 14px; border-bottom:1px solid #21262d; align-items:baseline; }
.kvrow:last-child { border-bottom:none; }
.kvlabel { color:#8b949e; font-size:.82rem; }
.kvvalue { color:#e6edf3; font-size:.9rem; font-weight:500;
  word-break:break-word; min-width:0; }
.kvline { color:#e6edf3; }
.kvcode { color:#8b949e; font-size:.74rem; margin-top:3px;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  word-break:break-word; }
/* "penting saja" mode hides detail rows/blocks + the raw-code sub-lines */
body.penting-only tr.detail { display:none; }
body.penting-only .detail-block { display:none; }
body.penting-only .kvcode { display:none; }
/* mobile: stack cards 2-up, smaller cards, scrollable tables */
@media (max-width: 640px) {
  main { padding:10px 10px 28px; }
  .cards { grid-template-columns:repeat(2, 1fr); gap:8px; }
  .cardvalue { font-size:1.05rem; }
  .card { padding:9px 11px; }
  .tab { padding:5px 9px; font-size:.8rem; }
  table { font-size:.8rem; }
  th, td { padding:6px 8px; }
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
    grid_badge = (_badge("GRID OK", "green") if plan_allowed
                  else _badge("GRID BLOCKED", "yellow"))
    risk_allowed = bool(risk_decision.get("allowed"))
    risk_badge = (_badge("RISK PASS", "green") if risk_allowed
                  else _badge("RISK BLOCKED", "yellow"))
    intel_status = str(market_intel.get("status") or "N/A")
    intel_badge = (_badge("MARKET OK", "green")
                   if market_intel.get("allowed") is True
                   else _badge("MARKET FILTERED", "yellow"))
    last_range = grid_state.get("last_range") or {}
    last_price = grid_state.get("last_price")

    def card(label: str, value: Any, sub: str = "") -> str:
        sub_html = f'<div class="cardsub">{_esc(sub)}</div>' if sub else ""
        return (f'<div class="card"><div class="cardlabel">{_esc(label)}</div>'
                f'<div class="cardvalue">{_esc(_fmt(value))}</div>{sub_html}</div>')

    def card_pct(label: str, value: Any, sub: str = "") -> str:
        sub_html = f'<div class="cardsub">{_esc(sub)}</div>' if sub else ""
        return (f'<div class="card"><div class="cardlabel">{_esc(label)}</div>'
                f'<div class="cardvalue">{_pct(value)}</div>{sub_html}</div>')

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

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="12">
<title>adaptive-grid dashboard</title>
<style>{_DASH_CSS}</style></head>
<body><main>
<h1>ADAPTIVE GRID</h1>
<p class="sub">Binance Spot Grid Monitor &middot; {_esc(env.get("symbol"))} &middot;
{_esc(env.get("timeframe"))} &middot; read-only &middot; waktu Indonesia (WIB)</p>
<div class="badges">{env_badge} {dry_badge} {live_badge} {run_badge}
{grid_badge} {risk_badge} {intel_badge} {kill_badge} {db_badge}</div>

<div class="cards">
{card("Equity", equity_val, f"ref {_fmt(risk.get('reference_equity'))}")}
{card_pct("Drawdown", drawdown, f"limit {_pct(risk.get('max_drawdown_pct'))}")}
{card("Realized PnL", account.get("realized_pnl"), "after fees")}
{card("Total fees", account.get("total_fees"))}
{card("Open orders", len(open_orders))}
{card("Harga terakhir", grid_detail.get("current_price"))}
    {card("Range", f"{_fmt(grid_detail.get('lower_price'))} &ndash; {_fmt(grid_detail.get('upper_price'))}")}
</div>

<div class="toolbar">
<nav class="tabs" role="tablist">
<button class="tab" role="tab" data-tab="tab-general" aria-selected="true">General</button>
<button class="tab" role="tab" data-tab="tab-risk" aria-selected="false">Risk</button>
<button class="tab" role="tab" data-tab="tab-grid" aria-selected="false">Grid</button>
<button class="tab" role="tab" data-tab="tab-market" aria-selected="false">Market</button>
<button class="tab" role="tab" data-tab="tab-orders" aria-selected="false">Orders &amp; Fills</button>
<button class="tab" role="tab" data-tab="tab-system" aria-selected="false">System</button>
</nav>
<label class="penting"><input type="checkbox" id="penting-toggle">penting saja</label>
</div>

<section class="tabpane active" id="tab-general" role="tabpanel">
<h2>Ringkasan</h2>
{_kv([
    ("Risk decision", "PASS" if risk_allowed else "BLOCKED"),
    ("Plan decision", _human(plan_decision)),
    ("Plan reasons", _human(plan_reason)),
    ("Market filter", _human(intel_status)),
])}
<h2 class="detail-block">Recent cycles</h2>
{_table(["cycle_id", "candle_index", "plan", "orders", "fills", "success",
         "blocked_reason", "at"], cycle_rows)}
</section>

<section class="tabpane" id="tab-risk" role="tabpanel">
<h2>Risk</h2>
{_kv([
    ("Kill switch", _yn(kill.get("active", False))),
    ("Kill trigger", _human(kill.get("trigger_reason") or "")),
    ("Activated at", _wib(kill.get("activated_at"))),
    ("Cancel status", _yn(kill.get("cancel_status")) if kill.get("cancel_status") is not None else "N/A"),
    ("Reference equity", _fmt(risk.get("reference_equity"))),
    ("Current drawdown", _pct(drawdown)),
    ("Drawdown limit", _pct(risk.get("max_drawdown_pct"))),
])}
<h3 class="detail-block">Recent risk events</h3>
{_table(["ts", "allowed", "reason"], risk.get("risk_events") or [])}
</section>

<section class="tabpane" id="tab-grid" role="tabpanel">
<h2>Grid</h2>
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
</section>

<section class="tabpane" id="tab-market" role="tabpanel">
<h2>Market</h2>
{_kv([
    ("Market regime", _human(grid_detail.get("market_regime")) if grid_detail.get("market_regime") else "N/A"),
    ("Range quality", _fmt(grid_detail.get("range_quality"))),
    ("Filter status", _human(intel_status) if intel_status else "N/A"),
    ("Filter allowed", _yn(market_intel.get("allowed"))),
    ("Filter reasons", _human(", ".join(market_intel.get("reasons") or [])) if market_intel.get("reasons") else None),
] + [
    (_diag_label(k), _fmt(v))
    for k, v in sorted(diag.items()) if not isinstance(v, (dict, list))
])}
</section>

<section class="tabpane" id="tab-orders" role="tabpanel">
<h2>Open orders ({len(open_orders)})</h2>
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
</section>

<section class="tabpane" id="tab-system" role="tabpanel">
<h2>System</h2>
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
</section>
<p class="foot">Public read-only monitor &middot; no authentication by design
&middot; this dashboard cannot place, cancel, or modify orders, cannot change
risk or configuration, and cannot release the kill switch &middot; page
auto-refreshes every 12s &middot; API: <code>/api/status</code>,
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
