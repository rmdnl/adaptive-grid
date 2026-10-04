"""Read-only dashboard over the state database.

The dashboard implements NO strategy logic, makes NO trading decisions,
places/cancels NO orders, exposes NO credentials and modifies NO
configuration. It only serves GET routes: `/` (HTML) and `/api/state`
(JSON). Any other method is refused with 405.

Symbol states are displayed verbatim from the database — one state is
never inferred from another:
WAITING / ENTRY_BLOCKED / GRID_BLOCKED / ACTIVE / COOLDOWN /
EXITING / STOPPED / KILL_ACTIVE / ERROR.
"""

from __future__ import annotations

import argparse
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional

from state import StateStore

log = logging.getLogger("dashboard")

KNOWN_STATES = (
    "WAITING", "ENTRY_BLOCKED", "GRID_BLOCKED", "ACTIVE", "COOLDOWN",
    "EXITING", "STOPPED", "KILL_ACTIVE", "ERROR",
)


def _global_payload(store: StateStore) -> Dict:
    equity = store.get_meta_float("equity")
    reference = store.get_meta_float("reference_equity")
    kill_active, kill_reason = store.global_kill()
    runtime_status, last_cycle_ts = store.last_runtime()
    drawdown = 0.0
    if reference is not None and reference > 0 and equity is not None:
        drawdown = (reference - equity) / reference
    return {
        "equity": equity,
        "reference_equity": reference,
        "drawdown": drawdown,
        "max_drawdown_percent": None,  # filled by caller when config known
        "kill_active": kill_active,
        "kill_reason": kill_reason,
        "open_orders": store.count_open_orders(),
        "realized_pnl": store.sum_realized_pnl(),
        "fees": store.sum_fees(),
        "runtime_status": runtime_status,
        "last_cycle_ts": last_cycle_ts,
        "database": store.database_status(),
    }


def _symbol_payload(store: StateStore, st) -> Dict:
    cooldown_active = st.cooldown_until is not None
    return {
        "symbol": st.symbol,
        "timeframe": st.timeframe,
        "last_price": st.last_price,
        "adx": st.adx,
        "rsi": st.rsi,
        "percent_b": st.percent_b,
        "volume_osc": st.volume_osc,
        "zscore": st.zscore,
        "atr": st.atr,
        "strategy_state": st.strategy_state,
        "entry_status": "blocked" if st.entry_blocker else "allowed",
        "entry_blocker": st.entry_blocker,
        "block_reason": st.block_reason,
        "exit_status": "triggered" if st.exit_status else "none",
        "exit_reason": st.exit_reason,
        "cooldown": cooldown_active,
        "cooldown_until": st.cooldown_until,
        "grid_mode": st.grid_mode,
        "grid_step": st.grid_step,
        "grid_count": store.count_completed_grids(st.symbol),
        "gross_pct": st.gross_pct,
        "net_pct": st.net_pct,
        "inventory_qty": st.inventory_qty,
        "avg_cost": st.avg_cost,
        "open_orders": store.count_open_orders(st.symbol),
        "realized_pnl": store.sum_realized_pnl(st.symbol),
        "fees": store.sum_fees(st.symbol),
        "risk_status": st.risk_status,
        "updated_at": st.updated_at,
    }


def build_payload(db_path: str, max_drawdown_percent: Optional[float] = None) -> Dict:
    """Read-only snapshot of global and per-symbol state. Missing data is
    reported as None — never inferred."""
    symbols: List[Dict] = []
    store: Optional[StateStore] = None
    try:
        store = StateStore(db_path, read_only=True)
        symbols = [_symbol_payload(store, st) for st in store.all_symbols()]
        glob = _global_payload(store)
    except Exception as exc:
        glob = {
            "equity": None,
            "reference_equity": None,
            "drawdown": None,
            "max_drawdown_percent": max_drawdown_percent,
            "kill_active": False,
            "kill_reason": None,
            "open_orders": 0,
            "realized_pnl": None,
            "fees": None,
            "runtime_status": None,
            "last_cycle_ts": None,
            "database": {"ok": False, "path": db_path, "error": str(exc)},
        }
        return {"global": glob, "symbols": symbols}
    glob["max_drawdown_percent"] = max_drawdown_percent
    return {"global": glob, "symbols": symbols}


_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>adaptive-grid</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 24px; background: #111; color: #ddd; }}
 h1 {{ font-size: 20px; }} h2 {{ font-size: 16px; margin-top: 24px; }}
 table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
 th, td {{ border: 1px solid #333; padding: 4px 8px; text-align: left; white-space: nowrap; }}
 th {{ background: #1c1c1c; }}
 .ACTIVE {{ color: #6f6; }} .COOLDOWN {{ color: #fa0; }} .ERROR, .KILL_ACTIVE, .STOPPED {{ color: #f55; }}
 .EXITING {{ color: #f80; }} .WAITING, .ENTRY_BLOCKED, .GRID_BLOCKED {{ color: #aaa; }}
 .kill {{ color: #f55; font-weight: bold; }}
</style></head><body>
<h1>adaptive-grid — read-only dashboard</h1>
<div>runtime: {runtime_status} | kill: <span class="{kill_class}">{kill}</span> | equity: {equity} | drawdown: {drawdown}</div>
<h2>symbols</h2>
<table>
<tr><th>symbol</th><th>state</th><th>last</th><th>ADX</th><th>RSI</th><th>%B</th><th>VO</th><th>Z</th><th>ATR</th>
<th>entry</th><th>blocker</th><th>exit</th><th>cooldown</th><th>mode</th><th>step</th><th>grids</th>
<th>gross</th><th>net</th><th>inv</th><th>open</th><th>pnl</th><th>fees</th><th>risk</th></tr>
{rows}
</table>
</body></html>"""


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def render_html(payload: Dict) -> str:
    glob = payload["global"]
    rows = []
    for s in payload["symbols"]:
        rows.append(
            "<tr><td>{sym}</td><td class='{state}'>{state}</td><td>{last}</td><td>{adx}</td>"
            "<td>{rsi}</td><td>{pb}</td><td>{vo}</td><td>{z}</td><td>{atr}</td>"
            "<td>{estatus}</td><td>{blocker}</td><td>{xstatus}</td><td>{cooldown}</td>"
            "<td>{mode}</td><td>{step}</td><td>{grids}</td><td>{gross}</td><td>{net}</td>"
            "<td>{inv}</td><td>{open}</td><td>{pnl}</td><td>{fees}</td><td>{risk}</td></tr>".format(
                sym=_fmt(s["symbol"]), state=_fmt(s["strategy_state"]), last=_fmt(s["last_price"]),
                adx=_fmt(s["adx"]), rsi=_fmt(s["rsi"]), pb=_fmt(s["percent_b"]),
                vo=_fmt(s["volume_osc"]), z=_fmt(s["zscore"]), atr=_fmt(s["atr"]),
                estatus=_fmt(s["entry_status"]), blocker=_fmt(s["entry_blocker"]),
                xstatus=_fmt(s["exit_status"]), cooldown=_fmt(s["cooldown"]),
                mode=_fmt(s["grid_mode"]), step=_fmt(s["grid_step"]),
                grids=_fmt(s["grid_count"]), gross=_fmt(s["gross_pct"]), net=_fmt(s["net_pct"]),
                inv=_fmt(s["inventory_qty"]), open=_fmt(s["open_orders"]),
                pnl=_fmt(s["realized_pnl"]), fees=_fmt(s["fees"]), risk=_fmt(s["risk_status"]),
            )
        )
    kill = "ACTIVE" if glob["kill_active"] else "inactive"
    return _HTML.format(
        runtime_status=_fmt(glob["runtime_status"]),
        kill=kill,
        kill_class="kill" if glob["kill_active"] else "",
        equity=_fmt(glob["equity"]),
        drawdown=_fmt(glob["drawdown"]),
        rows="\n".join(rows),
    )


class DashboardHandler(BaseHTTPRequestHandler):
    db_path = "state.db"
    max_drawdown_percent: Optional[float] = None

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/api/state":
            payload = build_payload(self.db_path, self.max_drawdown_percent)
            body = json.dumps(payload, indent=2).encode()
            self._send(200, body, "application/json")
            return
        if self.path in ("/", "/index.html"):
            payload = build_payload(self.db_path, self.max_drawdown_percent)
            self._send(200, render_html(payload).encode(), "text/html; charset=utf-8")
            return
        self._send(404, b"not found\n", "text/plain")

    def _refuse(self) -> None:
        self._send(405, b"dashboard is read-only\n", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        self._refuse()

    def do_PUT(self) -> None:  # noqa: N802
        self._refuse()

    def do_DELETE(self) -> None:  # noqa: N802
        self._refuse()

    def log_message(self, fmt: str, *args) -> None:  # quiet default logging
        log.debug(fmt, *args)


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    handler = type("BoundDashboardHandler", (DashboardHandler,), {
        "db_path": db_path,
        "max_drawdown_percent": None,
    })
    server = ThreadingHTTPServer((host, port), handler)
    log.info("dashboard listening on http://%s:%d (read-only)", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="adaptive-grid read-only dashboard")
    parser.add_argument("--db", default="state.db", help="path to the SQLite state database")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    serve(args.db, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
