"""Read-only retrofuturistic operator dashboard over the state database.

"Serious crypto trading control system from an alternate 1987."

The dashboard implements NO strategy logic, makes NO trading decisions,
places/cancels NO orders, exposes NO credentials and modifies NO
configuration. It never reads .env or any environment variable: the
operating mode it displays (TESTNET/LIVE, DRY-RUN/LIVE) is the runtime's
own persisted record (meta keys written by bot.py).

Routes (GET only):
    /            static operator-console shell (HTML/CSS/JS, no assets)
    /api/state   full state snapshot (JSON)
    /api/history cumulative net PnL telemetry from the fills ledger

Every other path is 404; every write method (POST/PUT/PATCH/DELETE) is
405 Method Not Allowed. Dynamic values reach the page exclusively via
DOM APIs (textContent) — never via HTML string interpolation.

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
from typing import Dict, List, Optional, Tuple

from state import StateStore

log = logging.getLogger("dashboard")

KNOWN_STATES = (
    "WAITING", "ENTRY_BLOCKED", "GRID_BLOCKED", "ACTIVE", "COOLDOWN",
    "EXITING", "STOPPED", "KILL_ACTIVE", "ERROR",
)

# Human-readable labels for technical state/blocker/reason codes
STATE_LABELS = {
    "WAITING": "WAITING FOR ENTRY",
    "ENTRY_BLOCKED": "ENTRY BLOCKED",
    "GRID_BLOCKED": "GRID BLOCKED",
    "ACTIVE": "ACTIVE — GRID RUNNING",
    "COOLDOWN": "COOLDOWN AFTER EXIT",
    "EXITING": "EXITING — LIQUIDATING",
    "STOPPED": "STOPPED — MANUAL/BOUNDARY",
    "KILL_ACTIVE": "KILL SWITCH ACTIVE",
    "ERROR": "ERROR — INVESTIGATE",
}

ENTRY_BLOCKER_LABELS = {
    "exit_priority": "Exit signal takes priority",
    "adx_rising": "ADX rising — regime strengthening",
    "stoch_no_cross": "Stoch RSI %K has not crossed up through %D",
    "stoch_k_too_high": "Stoch RSI %K above entry limit",
    "min_interval_not_elapsed": "Minimum hours between entries not elapsed",
    "rsi_not_low": "RSI above entry threshold",
    "adx_trending": "ADX indicates trending market",
    "bb_not_low": "Price not at lower Bollinger Band",
    "volume_osc_insufficient": "Volume oscillator too low",
    "zscore_too_high": "Z-score exceeds entry limit",
    "boundary_breach": "15m candle breached lower boundary",
    "boundary_unknown": "15m boundary data unavailable",
    "risk_veto": "Risk engine vetoed entry",
    "grid_placement_failed": "Grid generation failed",
    "insufficient_balance": "Insufficient USDT balance",
    "no_active_symbol_config": "No active symbol configuration",
    "stale_data": "Market data too old",
}

EXIT_REASON_LABELS = {
    "adx_trending": "ADX trending — trend strength confirmed",
    "rsi_overbought": "RSI overbought — mean reversion likely",
    "bb_percent_b_max": "Price at upper Bollinger Band",
    "zscore_abs_max": "Z-score extreme — statistical edge exhausted",
    "exit_adx_min": "ADX minimum threshold met",
    "exit_rsi_min": "RSI minimum threshold met",
    "exit_bb_percent_b_min": "BB %B minimum threshold met",
    "exit_zscore_abs_max": "Z-score absolute maximum exceeded",
    "adx_trending_up": "ADX trending up with +DI dominant — SOFT exit (buys cancelled, sells left to fill)",
    "stoch_k_overbought": "Stoch RSI %K overbought — SOFT exit (buys cancelled, sells left to fill)",
    "adx_trending_down": "ADX trending with -DI dominant — HARD exit (liquidated)",
    "time_stop": "Time stop — grid older than HOLD_MAX_HOURS (SOFT exit)",
    "time_stop_escalation": "Time stop — inventory remained after the SOFT window (HARD exit)",
    "lower_boundary_breach": "15m close breached lower stop boundary",
    "global_drawdown": "Global equity drawdown limit hit",
    "risk_engine_veto": "Risk engine vetoed continuation",
}

BLOCK_REASON_LABELS = {
    "exit_priority": "Exit signal active — new entry blocked",
    "rsi_not_low": "RSI not in oversold zone",
    "adx_trending": "ADX shows trending — no ranging entry",
    "boundary_unknown": "15m boundary data missing — fail-closed",
    "boundary_breach": "15m close below lower boundary — STOPPED",
    "risk_veto": "Risk engine veto",
    "stale_data": "Market data stale — cannot verify safety",
    "insufficient_balance": "USDT balance below grid minimum",
    "grid_placement_failed": "Grid economics failed validation",
    "unknown": "Unknown blocker — check logs",
}

RISK_STATUS_LABELS = {
    "ok": "OK — All risk gates clear",
    "drawdown_breach": "DRAWDOWN BREACH — Global limit exceeded",
    "lower_boundary": "15M BOUNDARY BREACH — Price below stop",
    "stale_data": "STALE DATA — Candle freshness check failed",
    "spread_wide": "SPREAD WIDE — Liquidity risk detected",
    "inventory_high": "INVENTORY HIGH — Position limit approached",
    "unknown": "UNKNOWN RISK STATE — Investigate",
}

EXECUTION_MODE_LABELS = {
    "paper": "PAPER TRADING — SIMULATED ONLY",
    "testnet": "BINANCE TESTNET — REAL ORDERS, TEST FUNDS",
    "live": "LIVE MAINNET — REAL CAPITAL AT RISK",
}

BINANCE_ENV_LABELS = {
    "testnet": "TESTNET ENVIRONMENT",
    "live": "LIVE MAINNET ENVIRONMENT",
}

def human_state(state: str) -> str:
    return STATE_LABELS.get(state, state)

def human_entry_blocker(blocker: Optional[str]) -> str:
    if not blocker:
        return "No blocker"
    return ENTRY_BLOCKER_LABELS.get(blocker, blocker.replace("_", " ").title())

def human_exit_reason(reason: Optional[str]) -> str:
    if not reason:
        return "No exit reason"
    return EXIT_REASON_LABELS.get(reason, reason.replace("_", " ").title())

def human_block_reason(reason: Optional[str]) -> str:
    if not reason:
        return "No block reason"
    return BLOCK_REASON_LABELS.get(reason, reason.replace("_", " ").title())

def human_risk_status(status: str) -> str:
    return RISK_STATUS_LABELS.get(status, status.replace("_", " ").title())

def human_execution_mode(mode: Optional[str]) -> str:
    if not mode:
        return "UNKNOWN"
    return EXECUTION_MODE_LABELS.get(mode.lower(), mode.upper())

def human_binance_env(env: Optional[str]) -> str:
    if not env:
        return "UNKNOWN"
    return BINANCE_ENV_LABELS.get(env.lower(), env.upper())


def _global_payload(store: StateStore) -> Dict:
    equity = store.get_meta_float("equity")
    reference = store.get_meta_float("reference_equity")
    kill_active, kill_reason = store.global_kill()
    runtime_status, last_cycle_ts = store.last_runtime()
    drawdown = 0.0
    if reference is not None and reference > 0 and equity is not None:
        drawdown = (reference - equity) / reference
    binance_env = store.get_meta("mode_binance_env")
    execution_mode = store.get_meta("mode_execution")
    session = {
        "id": store.get_meta("session_id"),
        "mode": store.get_meta("session_mode"),
        "env": store.get_meta("session_env"),
        "started_ts": store.get_meta_float("session_started_ts"),
        "start_equity": store.get_meta_float("session_start_equity"),
        "initial_cash": store.get_meta_float("session_initial_cash"),
    }
    return {
        "execution_mode": execution_mode,
        "execution_mode_human": human_execution_mode(execution_mode),
        "wallet_usdt": store.get_meta_float("wallet_usdt"),
        "session": session,
        "equity": equity,
        "reference_equity": reference,
        "drawdown": drawdown,
        "max_drawdown_percent": None,  # filled by caller when known
        "kill_active": kill_active,
        "kill_reason": kill_reason,
        "open_orders": store.count_open_orders(),
        "realized_pnl": store.sum_realized_pnl(),
        "fees": store.sum_fees(),
        "runtime_status": runtime_status,
        "last_cycle_ts": last_cycle_ts,
        "database": store.database_status(),
        # operating mode, as persisted by the trading runtime itself
        "binance_env": binance_env,
        "binance_env_human": human_binance_env(binance_env),
    }


def _symbol_payload(store: StateStore, st) -> Dict:
    payload = {
        "symbol": st.symbol,
        "timeframe": st.timeframe,
        "last_price": st.last_price,
        "adx": st.adx,
        "plus_di": st.plus_di,
        "minus_di": st.minus_di,
        "stoch_k": st.stoch_k,
        "stoch_d": st.stoch_d,
        "atr": st.atr,
        "strategy_state": st.strategy_state,
        "strategy_state_human": human_state(st.strategy_state),
        "entry_status": "blocked" if st.entry_blocker else "allowed",
        "entry_blocker": st.entry_blocker,
        "entry_blocker_human": human_entry_blocker(st.entry_blocker),
        "block_reason": st.block_reason,
        "block_reason_human": human_block_reason(st.block_reason),
        "exit_status": "triggered" if st.exit_status else "none",
        "exit_reason": st.exit_reason,
        "exit_reason_human": human_exit_reason(st.exit_reason),
        "cooldown": st.cooldown_until is not None,
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
        "risk_status_human": human_risk_status(st.risk_status),
        "updated_at": st.updated_at,
    }
    # Entry blocker telemetry (read-only tuning statistics; the bot is the
    # sole writer, the dashboard only displays).
    payload["entry_telemetry"] = {
        "entry_evaluations": st.entry_evaluations,
        "blocked_adx": st.blocked_adx,
        "blocked_rsi": st.blocked_rsi,
        "blocked_vo": st.blocked_vo,
        "blocked_bb": st.blocked_bb,
        "blocked_grid": st.blocked_grid,
        "blocked_budget": st.blocked_budget,
        "blocked_risk": st.blocked_risk,
        "blocked_cooldown": st.blocked_cooldown,
        "blocked_exit_priority": st.blocked_exit_priority,
        "entries_total": st.entries_total,
        "last_entry_ts": st.last_entry_ts,
        "last_entry_blocker": st.last_entry_blocker,
        "last_grid_reject_reason": st.last_grid_reject_reason,
    }
    # Adaptive grid parameters (Phase 1) - only include when grid is/was active
    if st.adaptive_lower_price is not None:
        payload["adaptive_lower_price"] = st.adaptive_lower_price
    if st.adaptive_upper_price is not None:
        payload["adaptive_upper_price"] = st.adaptive_upper_price
    if st.adaptive_total_grids is not None:
        payload["adaptive_total_grids"] = st.adaptive_total_grids
    if st.adaptive_quote_budget is not None:
        payload["adaptive_quote_budget"] = st.adaptive_quote_budget
    if st.adaptive_grid_step is not None:
        payload["adaptive_grid_step"] = st.adaptive_grid_step
    if st.adaptive_reference_price is not None:
        payload["adaptive_reference_price"] = st.adaptive_reference_price
    if st.adaptive_timeframe is not None:
        payload["adaptive_timeframe"] = st.adaptive_timeframe
    return payload


def _active_symbol_payloads(store: StateStore) -> Tuple[List[Dict], Optional[List[str]]]:
    """Per-symbol payloads for the ACTIVE runtime session only.

    The bot persists its configured symbol list (PAIR_LIST, PAIR_LIST
    order) in the read-only state DB at startup. Dashboard rendering is
    filtered against that list so historical symbols (e.g. a pair that
    left PAIR_LIST) never reappear. When the key is absent the display
    fails closed: an empty symbol list plus the explicit NO ACTIVE SYMBOL
    CONFIGURATION marker — historical symbols are not shown silently.
    """
    configured = store.configured_symbols()
    if configured is None:
        return [], None
    by_symbol = {st.symbol: st for st in store.all_symbols()}
    payloads = [
        _symbol_payload(store, by_symbol[symbol])
        for symbol in configured
        if symbol in by_symbol
    ]
    return payloads, configured


def build_payload(db_path: str, max_drawdown_percent: Optional[float] = None) -> Dict:
    """Read-only snapshot of global and per-symbol state. Missing data is
    reported as None — never inferred, never fabricated. Symbol rows are
    restricted to the active session's configured symbols."""
    symbols: List[Dict] = []
    store: Optional[StateStore] = None
    configured: Optional[List[str]] = None
    try:
        store = StateStore(db_path, read_only=True)
        symbols, configured = _active_symbol_payloads(store)
        glob = _global_payload(store)
        glob["configured_symbols"] = configured
        glob["active_symbol_config"] = configured is not None
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
            "execution_mode": None,
            "wallet_usdt": None,
            "session": None,
            "binance_env": None,
            "configured_symbols": None,
            "active_symbol_config": False,
        }
        return {"global": glob, "symbols": symbols}
    glob["max_drawdown_percent"] = max_drawdown_percent
    return {"global": glob, "symbols": symbols}


def build_history(db_path: str) -> List[Dict]:
    """Cumulative net PnL telemetry from the fills ledger. If the database
    is unavailable the telemetry is empty — nothing is invented."""
    try:
        store = StateStore(db_path, read_only=True)
        return store.net_pnl_history()
    except Exception as exc:  # dashboard must never crash on DB issues
        log.debug("history unavailable: %s", exc)
        return []


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>ADAPTIVE-GRID // SPOT TRADING CONTROL SYSTEM</title>
<style>
:root{
  --bg:#05080f; --panel:#0a101c; --panel2:#0d1524; --line:#182c47; --line2:#0f2036;
  --text:#c9d8e8; --dim:#5f7688; --faint:#3b4f61;
  --cyan:#4fd8ff; --green:#3dff9e; --amber:#ffb545; --orange:#ff8a3d;
  --red:#ff4d5e; --magenta:#ff5fd2;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--bg); color:var(--text); min-height:100vh;
  font-family:"IBM Plex Mono","JetBrains Mono","Roboto Mono",ui-monospace,Menlo,Consolas,monospace;
  font-size:13px; line-height:1.45;
}
/* subtle technical grid */
body::before{content:""; position:fixed; inset:0; pointer-events:none; z-index:0;
  background:
    repeating-linear-gradient(0deg, rgba(79,216,255,.030) 0 1px, transparent 1px 48px),
    repeating-linear-gradient(90deg, rgba(79,216,255,.030) 0 1px, transparent 1px 48px);
}
/* subtle CRT scanlines */
body::after{content:""; position:fixed; inset:0; pointer-events:none; z-index:0;
  background:repeating-linear-gradient(0deg, rgba(0,0,0,.16) 0 1px, transparent 1px 3px);
}
.wrap{position:relative; z-index:1; max-width:1340px; margin:0 auto; padding:18px 22px 40px}
a{color:var(--cyan)}
/* ---------- header ---------- */
header.mast{
  display:flex; flex-wrap:wrap; gap:14px 26px; align-items:flex-end;
  border:1px solid var(--line); background:linear-gradient(180deg,var(--panel2),var(--panel));
  padding:14px 18px; margin-bottom:14px; position:relative;
}
.mast::before,.mast::after{content:""; position:absolute; width:10px; height:10px; pointer-events:none}
.mast::before{top:-1px; left:-1px; border-top:2px solid var(--cyan); border-left:2px solid var(--cyan)}
.mast::after{bottom:-1px; right:-1px; border-bottom:2px solid var(--cyan); border-right:2px solid var(--cyan)}
.brand h1{margin:0; font-size:19px; letter-spacing:.42em; color:var(--cyan);
  text-shadow:0 0 10px rgba(79,216,255,.35); font-weight:600}
.brand .sub{margin-top:3px; font-size:10px; letter-spacing:.34em; color:var(--dim)}
.idrow{display:flex; flex-wrap:wrap; gap:8px; margin-left:auto; align-items:center}
.ind{display:flex; align-items:center; gap:7px; border:1px solid var(--line2);
  background:rgba(5,10,20,.75); padding:4px 10px; font-size:11px; letter-spacing:.14em; white-space:nowrap}
.dot{width:8px; height:8px; border-radius:50%; background:var(--faint); flex:0 0 auto;
  box-shadow:0 0 6px rgba(0,0,0,0)}
.ind.online .dot{background:var(--green); box-shadow:0 0 7px rgba(61,255,158,.7)}
.ind.offline .dot{background:var(--red); box-shadow:0 0 7px rgba(255,77,94,.7)}
.ind.live{border-color:var(--red); color:var(--red)}
.ind.paper{border-color:rgba(255,181,69,.55); color:var(--amber)}
.ind.testnet{border-color:rgba(79,216,255,.55); color:var(--cyan)}
.ind .lbl{color:var(--dim)}
/* ---------- panels ---------- */
.panel{border:1px solid var(--line); background:linear-gradient(180deg,var(--panel2),var(--panel));
  padding:12px 14px; position:relative}
.panel>.cap{font-size:10px; letter-spacing:.30em; color:var(--dim); margin-bottom:9px;
  border-bottom:1px solid var(--line2); padding-bottom:5px}
.kpis{display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; margin-bottom:14px}
.kpi .val{font-size:19px; margin-top:2px; color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.kpi .unit{font-size:10px; color:var(--dim); letter-spacing:.18em; margin-top:4px}
.kpi.risk .val{color:var(--amber)}
.kpi.risk.hot{border-color:var(--red)}
.kpi.risk.hot .val{color:var(--red); text-shadow:0 0 9px rgba(255,77,94,.45)}
.kpi.kill.hot .val{color:var(--red)}
.kpi.kill .val{color:var(--green)}
.mid{display:grid; grid-template-columns:minmax(280px,340px) 1fr; gap:10px; margin-bottom:14px}
/* ---------- system telemetry ---------- */
.sysrow{display:flex; gap:9px 18px; flex-wrap:wrap; font-size:12px}
.sysrow .item{display:flex; gap:8px; min-width:190px}
.sysrow .k{color:var(--dim); letter-spacing:.12em}
.sysrow .v{letter-spacing:.08em}
.sysrow .v.ok{color:var(--green)} .sysrow .v.warn{color:var(--amber)}
.sysrow .v.danger{color:var(--red)} .sysrow .v.cyan{color:var(--cyan)}
.sysrow .v.magenta{color:var(--magenta)}
.killreason{margin-top:9px; border-top:1px dashed var(--line2); padding-top:7px;
  color:var(--red); font-size:11.5px; letter-spacing:.06em; display:none}
.killreason.show{display:block}
/* ---------- chart ---------- */
#chart{width:100%; height:190px; display:block}
.chartmeta{display:flex; justify-content:space-between; color:var(--faint); font-size:10px;
  letter-spacing:.16em; margin-top:6px}
/* ---------- symbols ---------- */
.symbols{display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:10px}
.sym .symhead{display:flex; justify-content:space-between; align-items:center;
  border-bottom:1px solid var(--line2); padding-bottom:7px; margin-bottom:8px}
.symname{font-size:15px; letter-spacing:.22em; color:var(--cyan)}
.badge{border:1px solid var(--faint); padding:2px 8px; font-size:10.5px; letter-spacing:.14em; white-space:nowrap}
.badge.ok{color:var(--green); border-color:rgba(61,255,158,.55); text-shadow:0 0 6px rgba(61,255,158,.35)}
.badge.neutral{color:#9fb4c6; border-color:#33475c}
.badge.warn{color:var(--amber); border-color:rgba(255,181,69,.55)}
.badge.warn2{color:var(--orange); border-color:rgba(255,138,61,.55)}
.badge.danger{color:var(--red); border-color:rgba(255,77,94,.6); text-shadow:0 0 6px rgba(255,77,94,.35)}
.badge.magenta{color:var(--magenta); border-color:rgba(255,95,210,.6)}
.sec{font-size:9.5px; letter-spacing:.30em; color:var(--faint); margin:8px 0 4px}
.kv{display:flex; justify-content:space-between; gap:12px; padding:1.5px 0; font-size:12px}
.kv .k{color:var(--dim); letter-spacing:.12em}
.kv .v{font-variant-numeric:tabular-nums; text-align:right; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.kv .v.ok{color:var(--green)} .kv .v.warn{color:var(--amber)}
.kv .v.danger{color:var(--red)} .kv .v.cyan{color:var(--cyan)}
.hr{border-top:1px dashed var(--line2); margin:9px 0}
/* ---------- footer ---------- */
footer{margin-top:16px; border:1px solid var(--line2); background:rgba(5,10,20,.6);
  padding:10px 14px; display:flex; flex-wrap:wrap; gap:8px 22px; font-size:10.5px; color:var(--dim);
  letter-spacing:.14em; align-items:center}
.legend .sw{display:inline-block; width:8px; height:8px; margin:0 5px 0 14px; vertical-align:middle}
.sw.g{background:var(--green)} .sw.c{background:#9fb4c6} .sw.a{background:var(--amber)}
.sw.o{background:var(--orange)} .sw.r{background:var(--red)}
footer .ro{margin-left:auto; color:var(--green); letter-spacing:.22em}
@media (max-width:760px){
  .mid{grid-template-columns:1fr}
  .idrow{margin-left:0}
  .brand h1{font-size:15px; letter-spacing:.3em}
}
</style></head>
<body>
<div class="wrap">

<header class="mast">
  <div class="brand">
    <h1>ADAPTIVE-GRID</h1>
    <div class="sub">SPOT TRADING CONTROL SYSTEM // MK-IV CONSOLE</div>
  </div>
  <div class="idrow">
    <span class="ind" id="ind-system"><span class="dot"></span><span class="lbl">SYSTEM</span><span id="system">BOOT</span></span>
    <span class="ind" id="ind-binance"><span class="lbl">BINANCE</span><span id="binance">&#8212;</span></span>
    <span class="ind" id="ind-exec"><span class="lbl">EXECUTION</span><span id="execution">&#8212;</span></span>
    <span class="ind" id="ind-data"><span class="lbl">FEED</span><span id="dataflag">CONNECTING</span></span>
    <span class="ind"><span class="lbl">LAST CYCLE</span><span id="lastcycle">&#8212;</span></span>
    <span class="ind"><span class="lbl">UPDATED</span><span id="updated">&#8212;</span></span>
    <span class="ind"><span class="lbl">TIME</span><span id="clock">&#8212;</span></span>
  </div>
</header>

<div class="kpis">
  <div class="panel kpi"><div class="cap" id="k-equity-label">EQUITY</div><div class="val" id="k-equity">&#8212;</div><div class="unit">USDT &middot; PNL-BASED</div></div>
  <div class="panel kpi"><div class="cap">TESTNET WALLET</div><div class="val" id="k-wallet">&#8212;</div><div class="unit">USDT &middot; LIVE WALLET READ</div></div>
  <div class="panel kpi"><div class="cap">REFERENCE EQUITY</div><div class="val" id="k-ref">&#8212;</div><div class="unit">HIGH-WATER MARK</div></div>
  <div class="panel kpi risk" id="k-dd-box"><div class="cap">DRAWDOWN</div><div class="val" id="k-dd">&#8212;</div><div class="unit">FROM REFERENCE</div></div>
  <div class="panel kpi"><div class="cap">MAX DRAWDOWN</div><div class="val" id="k-maxdd">&#8212;</div><div class="unit">HARD LIMIT</div></div>
  <div class="panel kpi"><div class="cap">OPEN ORDERS</div><div class="val" id="k-open">&#8212;</div><div class="unit">ACROSS ALL SYMBOLS</div></div>
  <div class="panel kpi"><div class="cap">REALIZED PNL</div><div class="val" id="k-pnl">&#8212;</div><div class="unit">USDT &middot; NET LEDGER</div></div>
  <div class="panel kpi"><div class="cap">FEES</div><div class="val" id="k-fees">&#8212;</div><div class="unit">USDT &middot; CUMULATIVE</div></div>
  <div class="panel kpi kill" id="k-kill-box"><div class="cap">KILL SWITCH</div><div class="val" id="k-kill">&#8212;</div><div class="unit" id="k-killsub">GLOBAL DRAWDOWN GUARD</div></div>
</div>

<div class="mid">
  <div class="panel">
    <div class="cap">SYSTEM SAFETY TELEMETRY</div>
    <div class="sysrow">
      <span class="item"><span class="k">RUNTIME</span><span class="v" id="s-runtime">&#8212;</span></span>
      <span class="item"><span class="k">DATABASE</span><span class="v" id="s-db">&#8212;</span></span>
      <span class="item"><span class="k">BINANCE</span><span class="v" id="s-binance">&#8212;</span></span>
      <span class="item"><span class="k">EXECUTION</span><span class="v" id="s-exec">&#8212;</span></span>
      <span class="item"><span class="k">KILL SWITCH</span><span class="v" id="s-kill">&#8212;</span></span>
      <span class="item"><span class="k">RISK STATUS</span><span class="v" id="s-risk">&#8212;</span></span>
      <span class="item"><span class="k">LAST CYCLE</span><span class="v" id="s-cycle">&#8212;</span></span>
      <span class="item"><span class="k">SESSION</span><span class="v" id="s-session">&#8212;</span></span>
      <span class="item"><span class="k">START EQUITY</span><span class="v" id="s-start">&#8212;</span></span>
    </div>
    <div class="killreason" id="killreason"></div>
  </div>
  <div class="panel">
    <div class="cap">NET PNL TELEMETRY &middot; OSCILLOSCOPE</div>
    <canvas id="chart"></canvas>
    <div class="chartmeta"><span id="chart-lo">MIN &#8212;</span><span>CUMULATIVE REALIZED &#8722; FEES &middot; FROM FILLS LEDGER</span><span id="chart-hi">MAX &#8212;</span></div>
  </div>
</div>

<div class="symbols" id="symbols"></div>

<footer>
  <span class="legend">STATES:
    <span class="sw g"></span>ACTIVE / OK
    <span class="sw c"></span>WAITING / ENTRY BLOCKED
    <span class="sw a"></span>GRID BLOCKED / COOLDOWN
    <span class="sw o"></span>EXITING
    <span class="sw r"></span>ERROR / STOPPED / KILL
  </span>
  <span class="ro">&#9679; READ-ONLY OBSERVER &middot; NO TRADING CONTROLS</span>
</footer>

</div>
<script>
(function () {
  "use strict";
  var REFRESH_MS = 5000;
  var lastGood = null, lastGoodAt = null, dataLive = false, lastHistory = null;

  function $(id) { return document.getElementById(id); }
  var DASH = "\\u2014";

  function fmtNum(v, digits) {
    if (v === null || v === undefined || isNaN(Number(v))) return DASH;
    return Number(v).toLocaleString("en-US", {
      minimumFractionDigits: digits, maximumFractionDigits: digits
    });
  }
  function fmtPrice(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return DASH;
    var n = Number(v);
    if (Math.abs(n) >= 1000) return fmtNum(n, 2);
    if (Math.abs(n) >= 1) return fmtNum(n, 4);
    return fmtNum(n, 8);
  }
  function fmtSigned(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return DASH;
    var n = Number(v);
    var s = Math.abs(n).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
    return (n >= 0 ? "+" : "\\u2212") + s;
  }
  function fmtPctFrac(v) { // fraction -> signed percent
    if (v === null || v === undefined || isNaN(Number(v))) return DASH;
    var n = Number(v) * 100;
    var s = Math.abs(n).toFixed(2) + "%";
    return (n > 0 ? "+" : n < 0 ? "\\u2212" : "") + s;
  }
  function fmtTs(ts) {
    if (ts === null || ts === undefined || isNaN(Number(ts))) return DASH;
    var d = new Date(Number(ts) * 1000);
    return isNaN(d.getTime()) ? DASH : d.toLocaleTimeString("en-GB");
  }
  function set(id, text) { var n = $(id); if (n) n.textContent = text; }
  function setCls(id, cls) { var n = $(id); if (n) n.className = cls; }

  function stateClass(state) {
    switch (state) {
      case "ACTIVE": case "OK": case "ok": case "RUNNING": return "ok";
      case "GRID_BLOCKED": case "COOLDOWN": case "GRID BLOCKED": return "warn";
      case "EXITING": return "warn2";
      case "ERROR": case "STOPPED": case "KILL_ACTIVE": case "error": case "stopped": return "danger";
      default: return "neutral";
    }
  }

  function kv(label, value, cls) {
    var row = document.createElement("div"); row.className = "kv";
    var k = document.createElement("span"); k.className = "k"; k.textContent = label;
    var v = document.createElement("span"); v.className = "v" + (cls ? " " + cls : "");
    v.textContent = value;
    row.appendChild(k); row.appendChild(v);
    return row;
  }
  function sec(title) {
    var s = document.createElement("div"); s.className = "sec"; s.textContent = title;
    return s;
  }
  function hr() { var h = document.createElement("div"); h.className = "hr"; return h; }

  // ----- entry telemetry (read-only display of symbols[].entry_telemetry) -----

  var BLOCKER_LABELS = {
    exit_priority: "EXIT SIGNAL TAKES PRIORITY",
    adx_rising: "ADX RISING — REGIME STRENGTHENING",
    stoch_no_cross: "STOCH RSI %K HAS NOT CROSSED UP THROUGH %D",
    stoch_k_too_high: "STOCH RSI %K ABOVE ENTRY LIMIT",
    min_interval_not_elapsed: "MINIMUM HOURS BETWEEN ENTRIES NOT ELAPSED",
    rsi_not_low: "RSI ABOVE ENTRY THRESHOLD",
    adx_not_low: "ADX ABOVE ENTRY LIMIT",
    adx_trending: "ADX INDICATES TRENDING MARKET",
    volume_osc_not_positive: "VOLUME OSCILLATOR BELOW MINIMUM",
    volume_osc_insufficient: "VOLUME OSCILLATOR TOO LOW",
    percent_b_not_low: "PRICE NOT AT LOWER BOLLINGER BAND",
    bb_not_low: "PRICE NOT AT LOWER BOLLINGER BAND",
    zscore_too_high: "Z-SCORE EXCEEDS ENTRY LIMIT",
    boundary_breach: "15M CANDLE BREACHED LOWER BOUNDARY",
    boundary_unknown: "15M BOUNDARY DATA UNAVAILABLE",
    risk_veto: "RISK ENGINE VETOED ENTRY",
    symbol_stopped: "SYMBOL RISK-STOPPED",
    symbol_error: "SYMBOL IN ERROR STATE",
    grid_placement_failed: "GRID GENERATION FAILED",
    insufficient_balance: "INSUFFICIENT USDT BALANCE",
    balance_unavailable: "USDT BALANCE UNAVAILABLE",
    no_closed_candle: "NO CLOSED CANDLE",
    stale_market_data: "MARKET DATA STALE",
    stale_data: "MARKET DATA STALE",
    insufficient_data: "INSUFFICIENT INDICATOR DATA",
    cooldown: "COOLDOWN AFTER EXIT",
    no_active_symbol_config: "NO ACTIVE SYMBOL CONFIGURATION"
  };
  function humanBlocker(code) {
    // Known codes map to their existing labels; anything else falls back to
    // a readable form of the raw code — never a fabricated interpretation.
    if (code === null || code === undefined || code === "") return DASH;
    var c = String(code);
    if (c.indexOf("global_kill:") === 0) {
      return "GLOBAL KILL: " + c.slice("global_kill:".length).toUpperCase();
    }
    if (BLOCKER_LABELS[c]) return BLOCKER_LABELS[c];
    return c.replace(/_/g, " ").toUpperCase();
  }

  var GRID_REJECT_LABELS = {
    invalid_mode: "INVALID GRID MODE",
    insufficient_data: "INSUFFICIENT DATA (NO CLOSED CANDLES / NO ATR)",
    invalid_filters: "INVALID EXCHANGE FILTERS",
    reference_price_unavailable: "REFERENCE PRICE UNAVAILABLE",
    percent_price_band: "OUTSIDE PERCENT PRICE BAND",
    no_valid_levels: "INSUFFICIENT VALID LEVELS",
    gross_below_minimum: "GROSS PROFIT BELOW MINIMUM",
    net_below_minimum: "NET PROFIT BELOW MINIMUM",
    quote_budget_exceeded: "TOTAL QUOTE BUDGET EXCEEDED",
    current_price_below_lower_bound: "CURRENT PRICE BELOW LOWER BOUND",
    current_price_above_upper_bound: "CURRENT PRICE ABOVE UPPER BOUND"
  };
  function humanGridReject(reason) {
    if (reason === null || reason === undefined || reason === "") return DASH;
    var r = String(reason);
    if (GRID_REJECT_LABELS[r]) return GRID_REJECT_LABELS[r];
    if (r.indexOf("adaptive_grid_failed:") === 0) {
      var detail = r.slice("adaptive_grid_failed:".length).trim();
      return "ADAPTIVE PLANNER: " + (GRID_REJECT_LABELS[detail] || detail.toUpperCase());
    }
    return r.replace(/_/g, " ").toUpperCase();
  }

  function telemetryCounter(t, field) {
    // Explicit zero must display "0"; null/undefined/missing displays the
    // unavailable dash. Never fabricate a zero from missing data.
    if (!t || typeof t !== "object") return DASH;
    return fmtNum(t[field], 0);
  }
  function telemetryText(t, field, humanize) {
    if (!t || typeof t !== "object") return DASH;
    var v = t[field];
    if (v === null || v === undefined || v === "") return DASH;
    return humanize ? humanize(v) : String(v);
  }

  function renderEntryTelemetry(m, s) {
    var t = (s && s.entry_telemetry && typeof s.entry_telemetry === "object")
      ? s.entry_telemetry : {};
    m.appendChild(hr());
    m.appendChild(sec("ENTRY TELEMETRY"));
    m.appendChild(kv("EVALUATIONS", telemetryCounter(t, "entry_evaluations")));
    m.appendChild(kv("ADX BLOCKED", telemetryCounter(t, "blocked_adx")));
    m.appendChild(kv("RSI BLOCKED", telemetryCounter(t, "blocked_rsi")));
    m.appendChild(kv("VO BLOCKED", telemetryCounter(t, "blocked_vo")));
    m.appendChild(kv("BB BLOCKED", telemetryCounter(t, "blocked_bb")));
    m.appendChild(kv("GRID BLOCKED", telemetryCounter(t, "blocked_grid")));
    m.appendChild(kv("BUDGET BLOCKED", telemetryCounter(t, "blocked_budget")));
    m.appendChild(kv("RISK BLOCKED", telemetryCounter(t, "blocked_risk")));
    m.appendChild(kv("COOLDOWN BLOCKED", telemetryCounter(t, "blocked_cooldown")));
    m.appendChild(kv("EXIT PRIORITY", telemetryCounter(t, "blocked_exit_priority")));
    m.appendChild(kv("TOTAL ENTRIES", telemetryCounter(t, "entries_total")));
    m.appendChild(hr());
    m.appendChild(kv("LAST ENTRY", telemetryText(t, "last_entry_ts", fmtTs)));
    m.appendChild(kv("LAST BLOCKER", telemetryText(t, "last_entry_blocker", humanBlocker)));
    m.appendChild(kv("LAST GRID REJECT", telemetryText(t, "last_grid_reject_reason", humanGridReject)));
  }

function renderGlobal(g) {
    set("k-equity", fmtNum(g.equity, 2));
    set("k-ref", fmtNum(g.reference_equity, 2));
    set("k-dd", g.drawdown === null || g.drawdown === undefined ? DASH : fmtPctFrac(g.drawdown).replace("+", ""));
    var maxdd = DASH;
    if (g.max_drawdown_percent !== null && g.max_drawdown_percent !== undefined) {
      maxdd = Number(g.max_drawdown_percent).toFixed(2) + "%";
    }
    set("k-maxdd", maxdd);
    set("k-open", g.open_orders === null || g.open_orders === undefined
      ? DASH : String(g.open_orders).padStart(3, "0"));
    set("k-pnl", fmtSigned(g.realized_pnl));
    set("k-fees", fmtNum(g.fees, 4));
    set("k-kill", g.kill_active ? "ACTIVE" : "INACTIVE");
    setCls("k-kill", "val " + (g.kill_active ? "danger" : "ok"));
    setCls("k-kill-box", "panel kpi kill" + (g.kill_active ? " hot" : ""));
    var drifting = typeof g.drawdown === "number" && g.drawdown > 0;
    setCls("k-dd-box", "panel kpi risk" + (g.kill_active ? " hot" : ""));
    setCls("k-dd", "val " + (g.kill_active ? "danger" : drifting ? "warn" : ""));

    // header identifiers — impossible to miss
    var online = g.runtime_status === "RUNNING";
    set("system", g.runtime_status === "KILL_ACTIVE" ? "KILL ACTIVE" : (online ? "ONLINE" : "OFFLINE"));
    setCls("ind-system", "ind " + (g.runtime_status === "KILL_ACTIVE" ? "offline" : online ? "online" : "offline"));
    var env = (g.binance_env || "").toUpperCase();
    var mode = (g.execution_mode || "").toUpperCase();   // PAPER / TESTNET / LIVE
    set("binance", g.binance_env_human || env || "UNKNOWN");
    setCls("ind-binance", "ind" + (env === "LIVE" ? " live" : " testnet"));
    set("execution", g.execution_mode_human || mode || "UNKNOWN");
    setCls("ind-exec", "ind" + (mode === "LIVE" ? " live" : mode === "PAPER" ? " paper" : " testnet"));
    set("lastcycle", fmtTs(g.last_cycle_ts));
    // mode-aware capital labels
    set("k-equity-label", mode === "PAPER" ? "PAPER EQUITY" : mode === "TESTNET" ? "EXCHANGE EQUITY" : "EQUITY");
    set("k-wallet", fmtNum(g.wallet_usdt, 2));

    // system safety telemetry — explicit text, never color alone
    set("s-runtime", g.runtime_status || "UNKNOWN");
    setCls("s-runtime", "v " + (online ? "ok" : g.runtime_status === "KILL_ACTIVE" ? "danger" : "warn"));
    set("s-db", g.database && g.database.ok ? "OK" : "UNAVAILABLE");
    setCls("s-db", "v " + (g.database && g.database.ok ? "ok" : "danger"));
    set("s-binance", g.binance_env_human || env || "UNKNOWN");
    setCls("s-binance", "v " + (env === "LIVE" ? "magenta" : "cyan"));
    set("s-exec", g.execution_mode_human || mode || "UNKNOWN");
    setCls("s-exec", "v " + (mode === "LIVE" ? "magenta" : mode === "PAPER" ? "warn" : "cyan"));
    var sess = g.session || {};
    set("s-session", sess.id
      ? String(sess.id).slice(0, 14) + " · " + String(sess.mode || "?").toUpperCase()
      : "—");
    set("s-start", fmtNum(sess.start_equity, 2));
    set("s-kill", g.kill_active ? "ACTIVE" : "INACTIVE");
    setCls("s-kill", "v " + (g.kill_active ? "danger" : "ok"));
    set("s-cycle", fmtTs(g.last_cycle_ts));
    var kr = $("killreason");
    if (g.kill_active && g.kill_reason) {
      kr.textContent = "KILL REASON: " + g.kill_reason;
      kr.className = "killreason show";
    } else { kr.className = "killreason"; }
  }

  function renderSymbols(symbols, activeConfig) {
    var grid = $("symbols");
    grid.textContent = "";
    if (!symbols || !symbols.length) {
      var empty = document.createElement("div");
      empty.className = "panel"; empty.style.gridColumn = "1 / -1";
      // Fail closed: when the active runtime has no configured symbol list,
      // say so explicitly instead of silently showing historical symbols.
      empty.textContent = activeConfig
        ? "NO SYMBOLS CONFIGURED"
        : "NO ACTIVE SYMBOL CONFIGURATION";
      grid.appendChild(empty);
      return;
    }
    symbols.forEach(function (s) {
      var m = document.createElement("section"); m.className = "panel sym";
      var head = document.createElement("header"); head.className = "symhead";
      var name = document.createElement("span"); name.className = "symname"; name.textContent = s.symbol;
      var badge = document.createElement("span");
      badge.className = "badge " + stateClass(s.strategy_state);
      badge.textContent = s.strategy_state_human || s.strategy_state || DASH;
      head.appendChild(name); head.appendChild(badge);
      m.appendChild(head);

      var riskOk = s.risk_status === "ok";
      m.appendChild(kv("STATE", s.strategy_state_human || s.strategy_state || DASH));
      m.appendChild(kv("RISK", s.risk_status_human || (riskOk ? "OK" : String(s.risk_status || DASH).toUpperCase()),
                      riskOk ? "ok" : "danger"));
      m.appendChild(kv("PRICE", fmtPrice(s.last_price), "cyan"));
      m.appendChild(kv("TIMEFRAME", s.timeframe ? String(s.timeframe).toUpperCase() : DASH));
      m.appendChild(kv("ENTRY", s.entry_status ? String(s.entry_status).toUpperCase() : DASH));
      m.appendChild(kv("BLOCKER", s.entry_blocker_human || s.block_reason_human || DASH,
                       (s.entry_blocker || s.block_reason) ? "warn" : ""));
      m.appendChild(kv("EXIT", s.exit_status === "triggered"
                       ? "TRIGGERED" + (s.exit_reason_human ? " \u00b7 " + s.exit_reason_human : "")
                       : "NONE", s.exit_status === "triggered" ? "warn" : ""));
      m.appendChild(kv("COOLDOWN", s.cooldown ? "UNTIL " + fmtTs(s.cooldown_until) : "INACTIVE",
                       s.cooldown ? "warn" : ""));
      m.appendChild(hr());
      m.appendChild(sec("INDICATORS"));
      m.appendChild(kv("ADX", fmtNum(s.adx, 2)));
      m.appendChild(kv("+DI", fmtNum(s.plus_di, 2)));
      m.appendChild(kv("-DI", fmtNum(s.minus_di, 2)));
      m.appendChild(kv("STOCH %K", fmtNum(s.stoch_k, 3)));
      m.appendChild(kv("STOCH %D", fmtNum(s.stoch_d, 3)));
      m.appendChild(kv("ATR", fmtNum(s.atr, 4)));
      m.appendChild(hr());
      m.appendChild(sec("GRID / POSITION"));
      m.appendChild(kv("MODE", s.grid_mode ? String(s.grid_mode).toUpperCase() : DASH));
      m.appendChild(kv("STEP", fmtNum(s.grid_step, 4)));
      m.appendChild(kv("COUNT", s.grid_count === null || s.grid_count === undefined
                       ? DASH : String(s.grid_count)));
      m.appendChild(kv("GROSS", fmtPctFrac(s.gross_pct)));
      m.appendChild(kv("NET", fmtPctFrac(s.net_pct)));
      m.appendChild(kv("INVENTORY", fmtNum(s.inventory_qty, 8)));
      m.appendChild(kv("AVG COST", fmtPrice(s.avg_cost)));
      m.appendChild(kv("OPEN", s.open_orders === null || s.open_orders === undefined
                       ? DASH : String(s.open_orders)));
      m.appendChild(kv("PNL", fmtSigned(s.realized_pnl),
                       typeof s.realized_pnl === "number"
                       ? (s.realized_pnl >= 0 ? "ok" : "danger") : ""));
      m.appendChild(kv("FEES", fmtNum(s.fees, 4)));
      renderEntryTelemetry(m, s);
      grid.appendChild(m);
    });
  }

  function drawChart(points) {
    var c = $("chart");
    if (!c) return;
    var dpr = window.devicePixelRatio || 1;
    var w = c.clientWidth || 600, h = 190;
    c.width = w * dpr; c.height = h * dpr;
    var ctx = c.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = "rgba(79,216,255,0.10)";
    ctx.lineWidth = 1;
    var gx;
    for (gx = 0; gx <= 8; gx++) {
      ctx.beginPath();
      ctx.moveTo(gx * w / 8, 0); ctx.lineTo(gx * w / 8, h); ctx.stroke();
    }
    var gy;
    for (gy = 0; gy <= 4; gy++) {
      ctx.beginPath();
      ctx.moveTo(0, gy * h / 4); ctx.lineTo(w, gy * h / 4); ctx.stroke();
    }
    if (!points || !points.length) {
      ctx.fillStyle = "rgba(95,118,136,0.9)";
      ctx.font = "11px monospace";
      ctx.textAlign = "center";
      ctx.fillText("NO TELEMETRY DATA", w / 2, h / 2);
      set("chart-lo", "MIN " + DASH); set("chart-hi", "MAX " + DASH);
      return;
    }
    var vals = points.map(function (p) { return p.net; });
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    if (hi - lo < 1e-9) { hi = lo + 1; }
    var pad = (hi - lo) * 0.1;
    lo -= pad; hi += pad;
    var zeroY = h - (0 - lo) / (hi - lo) * h;
    if (zeroY >= 0 && zeroY <= h) {
      ctx.strokeStyle = "rgba(255,181,69,0.35)";
      ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(0, zeroY); ctx.lineTo(w, zeroY); ctx.stroke();
      ctx.setLineDash([]);
    }
    ctx.strokeStyle = "#3dff9e";
    ctx.shadowColor = "rgba(61,255,158,0.55)";
    ctx.shadowBlur = 5;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    points.forEach(function (p, i) {
      var x = points.length === 1 ? w : i / (points.length - 1) * w;
      var y = h - (p.net - lo) / (hi - lo) * h;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.shadowBlur = 0;
    set("chart-lo", "MIN " + fmtNum(Math.min.apply(null, vals), 2));
    set("chart-hi", "MAX " + fmtNum(Math.max.apply(null, vals), 2));
  }

  function renderAll(p) {
    if (!p) return;
    var g = p.global || {};
    renderGlobal(g);
    renderSymbols(p.symbols || [], g.active_symbol_config);
  }

  function setDataFlag(live) {
    var flag = live ? "DATA LIVE" : (lastGood ? "DATA STALE" : "NO DATA");
    set("dataflag", flag);
    setCls("dataflag", "badge " + (live ? "ok" : lastGood ? "warn" : "danger"));
    if (live && lastGoodAt) {
      set("updated", "UPDATED " + Math.max(0, Math.round((Date.now() - lastGoodAt) / 1000)) + "S AGO");
    }
    if (!live) {
      set("updated", "LAST GOOD " + (lastGoodAt ? new Date(lastGoodAt).toLocaleTimeString("en-GB") : DASH));
    }
  }

  function tick() {
    fetch("/api/state").then(function (r) {
      if (!r.ok) throw new Error("http " + r.status);
      return r.json();
    }).then(function (p) {
      lastGood = p; lastGoodAt = Date.now(); dataLive = true;
      renderAll(p);
      setDataFlag(true);
    }).catch(function () {
      dataLive = false;
      setDataFlag(false);   // keep the last good values on screen
    });
    fetch("/api/history").then(function (r) {
      if (!r.ok) throw new Error("http " + r.status);
      return r.json();
    }).then(function (h) {
      lastHistory = h;
      drawChart(lastHistory);
    }).catch(function () { /* keep last good chart */ });
  }

  function clockTick() {
    set("clock", new Date().toLocaleTimeString("en-GB"));
    if (lastGoodAt) {
      set("updated", (dataLive ? "UPDATED " : "STALE SINCE ")
        + Math.max(0, Math.round((Date.now() - lastGoodAt) / 1000)) + "S AGO");
    }
  }

  window.addEventListener("resize", function () { drawChart(lastHistory); });
  clockTick();
  tick();
  setInterval(clockTick, 1000);
  setInterval(tick, REFRESH_MS);
})();
</script>
</body></html>
"""


def render_page() -> str:
    """The static console shell. Deliberately contains no server-side
    interpolation: dynamic values reach the DOM only via textContent."""
    return _PAGE


class DashboardHandler(BaseHTTPRequestHandler):
    db_path = "state.db"
    max_drawdown_percent: Optional[float] = None

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/api/state":
            payload = build_payload(self.db_path, self.max_drawdown_percent)
            body = json.dumps(payload, indent=2).encode()
            self._send(200, body, "application/json")
            return
        if self.path == "/api/history":
            history = build_history(self.db_path)
            self._send(200, json.dumps(history).encode(), "application/json")
            return
        if self.path in ("/", "/index.html"):
            self._send(200, render_page().encode(), "text/html; charset=utf-8")
            return
        self._send(404, b"not found\n", "text/plain")

    def _refuse(self) -> None:
        self._send(405, b"dashboard is read-only\n", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        self._refuse()

    def do_PUT(self) -> None:  # noqa: N802
        self._refuse()

    def do_PATCH(self) -> None:  # noqa: N802
        self._refuse()

    def do_DELETE(self) -> None:  # noqa: N802
        self._refuse()

    def log_message(self, fmt: str, *args) -> None:  # quiet default logging
        log.debug(fmt, *args)


def make_server(
    db_path: str,
    host: str = "127.0.0.1",
    port: int = 8080,
    max_drawdown_percent: Optional[float] = None,
) -> ThreadingHTTPServer:
    handler = type("BoundDashboardHandler", (DashboardHandler,), {
        "db_path": db_path,
        "max_drawdown_percent": max_drawdown_percent,
    })
    return ThreadingHTTPServer((host, port), handler)


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    server = make_server(db_path, host, port)
    log.info("dashboard listening on http://%s:%d (read-only)", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="adaptive-grid read-only operator dashboard")
    parser.add_argument("--db", default="state.db", help="path to the SQLite state database")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (e.g. 127.0.0.1 or 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    serve(args.db, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
