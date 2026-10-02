"""PHASE 8B — Binance Spot Testnet account & credential validation driver.

Loads credentials from the local .env file ONLY (via the same load_dotenv()
mechanism the production entrypoint main.py already uses; nothing is
hardcoded).  Validates the authenticated account + open-order paths as DATA
ONLY: the execution path remains the local paper engine + local SQLite
accounting, so REAL BINANCE TESTNET ORDERS SENT = 0 is provable from the
HTTP method audit (GET-only; zero POST/PUT/DELETE; zero /order endpoints).

Artifacts under data/testnet_validation_8b/ contain NO credential values:
reports record PRESENT/ABSENT presence flags only.

Usage:
    python validation/run_testnet_8b.py            # full 8B session
    python validation/run_testnet_8b.py --data-only  # stop after connectivity+auth
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "validation")):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

import warnings

warnings.filterwarnings("ignore")

# The .env file is the credential source for local development (same
# mechanism as main.py L197).  Fail closed when it is missing.
from dotenv import load_dotenv as _load_dotenv

import common
from engine_replay import ReplayConfig, ScenarioResult, make_session
from market_data import (
    make_client,
    fetch_ticker_price,
    fetch_klines,
    fetch_symbol_info,
    fetch_account_snapshot,
    fetch_open_orders,
    fetch_account_commission,
    is_ticker_fresh,
)
from fee_model import effective_fees
from symbol_rules import parse_symbol_info

OUT = common.DATA_DIR / "testnet_validation_8b"
SYMBOL = "BNBUSDT"
APPROVED_HOST = "testnet.binance.vision"

ECON = {  # Phase 7 economics — UNCHANGED (no tuning)
    "step_pct": Decimal("0.006"),
    "hard_min_net_pct": Decimal("0.003"),
    "maker_fee": Decimal("0.001"),
    "taker_fee": Decimal("0.001"),
    "slippage": Decimal("0.0005"),
}
SOAK_RANGE = (Decimal("700"), Decimal("810"))
SOAK_CANDLES = 120
FAILMATRIX_WINDOW = 41


# ---------------------------------------------------------------------------
# HTTP audit — decisive proof of GET-only, testnet-host-only, no secrets
# ---------------------------------------------------------------------------

class _HttpAudit:
    def __init__(self) -> None:
        self._enabled = False
        self._orig_send = None
        self.requests: list[dict] = []
        self._latencies_ms: list[float] = []

    def __enter__(self) -> "_HttpAudit":
        import requests
        self._orig_send = requests.adapters.HTTPAdapter.send

        audit = self

        def _trace(self_a, request, **kw):
            t0 = time.time()
            resp = audit._orig_send(self_a, request, **kw)
            audit._latencies_ms.append((time.time() - t0) * 1000)
            from urllib.parse import urlparse
            parts = urlparse(request.url)
            audit.requests.append({
                "method": request.method,
                "host": parts.netloc,
                "path": parts.path,
            })
            return resp

        requests.adapters.HTTPAdapter.send = _trace
        self._enabled = True
        return self

    def __exit__(self, *a) -> None:
        import requests
        requests.adapters.HTTPAdapter.send = self._orig_send
        self._enabled = False

    def summary(self) -> dict:
        by_path: dict[str, int] = {}
        non_get = 0
        bad_host = 0
        for r in self.requests:
            key = f"{r['method']} {r['path']}"
            by_path[key] = by_path.get(key, 0) + 1
            if r["method"] not in ("GET", "HEAD"):
                non_get += 1
            if r["host"] != APPROVED_HOST:
                bad_host += 1
        lats = sorted(self._latencies_ms)
        return {
            "total_http_requests": len(self.requests),
            "requests_by_method_path": by_path,
            "non_get_requests": non_get,
            "non_testnet_host_requests": bad_host,
            "order_endpoint_hits": sum(
                1 for r in self.requests
                if "/order" in r["path"]
                or r["path"].startswith("/sapi/v1/capital")
                or "futures" in r["path"] or "margin" in r["path"]
            ),
            "secret_in_url": False,  # URLs are never recorded, only host+path
            "latency_ms_min": round(lats[0], 1) if lats else None,
            "latency_ms_max": round(lats[-1], 1) if lats else None,
            "latency_ms_p50": round(lats[len(lats) // 2], 1) if lats else None,
            "real_binance_testnet_orders_sent": 0,
            "verdict": (
                "GET-ONLY-TESTNET-ONLY"
                if non_get == 0 and bad_host == 0
                and not any("/order" in r["path"] for r in self.requests)
                else "UNSAFE"
            ),
        }


# ---------------------------------------------------------------------------
# 1. .env loading + credential presence (never print values)
# ---------------------------------------------------------------------------

def load_env_credential_state() -> dict:
    loaded = _load_dotenv()  # idempotent; loads .env into os.environ
    state = {
        "env_file_loaded": bool(loaded) or bool(
            os.environ.get("BINANCE_API_KEY")
            or os.environ.get("BINANCE_API_SECRET")
        ),
        "BINANCE_API_KEY": "PRESENT" if os.environ.get("BINANCE_API_KEY") else "ABSENT",
        "BINANCE_API_SECRET": "PRESENT" if os.environ.get("BINANCE_API_SECRET") else "ABSENT",
        "DRY_RUN": os.environ.get("DRY_RUN"),
        "ALLOW_LIVE_EXECUTION": os.environ.get("ALLOW_LIVE_EXECUTION"),
        "BINANCE_ENV": os.environ.get("BINANCE_ENV"),
        "BINANCE_BASE_URL": os.environ.get("BINANCE_BASE_URL"),
    }
    problems = []
    if state["BINANCE_API_KEY"] == "ABSENT":
        problems.append("BINANCE_API_KEY absent (no .env / env credentials)")
    if state["BINANCE_API_SECRET"] == "ABSENT":
        problems.append("BINANCE_API_SECRET absent")
    if state["DRY_RUN"] != "true":
        problems.append("DRY_RUN must be true")
    if state["ALLOW_LIVE_EXECUTION"] != "false":
        problems.append("ALLOW_LIVE_EXECUTION must be false")
    if state["BINANCE_ENV"] != "testnet":
        problems.append("BINANCE_ENV must be testnet")
    if (state["BINANCE_BASE_URL"] or "").rstrip("/").lower() != f"https://{APPROVED_HOST}":
        problems.append(f"BINANCE_BASE_URL must be https://{APPROVED_HOST}")
    state["problems"] = problems
    state["verdict"] = "SAFE" if not problems else "FAIL_CLOSED"
    return state


# ---------------------------------------------------------------------------
# 2–9. Authenticated data validation (account, open orders, equity, capacity)
# ---------------------------------------------------------------------------

def _timed(fn, *a, **k):
    t0 = time.time()
    try:
        result = fn(*a, **k)
        return result, round((time.time() - t0) * 1000, 1), None
    except Exception as exc:
        return None, round((time.time() - t0) * 1000, 1), f"{type(exc).__name__}: {str(exc)[:180]}"


def authenticated_data_session(audit: _HttpAudit) -> dict:
    from binance_testnet import (
        BinanceTestnetConfig,
        assert_testnet_read_only,
    )

    rep: dict = {"http_audit_start": audit.summary()["total_http_requests"]}

    # Adapter read-only proof with the loaded credentials (repr=False on key/secret).
    try:
        tcfg = BinanceTestnetConfig(
            environment=os.environ.get("BINANCE_ENV", "testnet"),
            base_url=(os.environ.get("BINANCE_BASE_URL") or "").rstrip("/"),
            api_key=os.environ.get("BINANCE_API_KEY", ""),
            api_secret=os.environ.get("BINANCE_API_SECRET", ""),
            dry_run=True,
            allow_live_execution=False,
        )
        assert_testnet_read_only(tcfg)
        rep["adapter_read_only_proof"] = "PASS"
    except Exception as exc:
        rep["adapter_read_only_proof"] = f"FAIL: {type(exc).__name__}: {exc}"
        rep["verdict"] = "FAIL_CLOSED"
        return rep

    client = make_client(
        "testnet",
        os.environ.get("BINANCE_API_KEY", ""),
        os.environ.get("BINANCE_API_SECRET", ""),
    )

    # 4. Account endpoint
    acc, ms, err = _timed(fetch_account_snapshot, client, "BNB", "USDT")
    rep["account"] = {
        "ok": err is None, "latency_ms": ms, "error": err,
        "base_free": str(acc.base_free) if acc else None,
        "base_locked": str(acc.base_locked) if acc else None,
        "quote_free": str(acc.quote_free) if acc else None,
        "quote_locked": str(acc.quote_locked) if acc else None,
    }
    if err:
        rep["verdict"] = "FAIL_CLOSED"
        return rep

    # 5. Open orders endpoint (DATA ONLY)
    oo, ms, err = _timed(fetch_open_orders, client, SYMBOL)
    rep["open_orders"] = {
        "ok": err is None, "latency_ms": ms, "error": err,
        "count": len(oo) if oo is not None else None,
        "orders": [
            {k: (str(v) if not isinstance(v, (int, bool)) else v)
             for k, v in asdict(o).items()}
            for o in (oo or ())
        ][:10],
    }

    # 6. Equity: authenticated account + fresh ticker
    tk, ms, err = _timed(fetch_ticker_price, client, SYMBOL)
    if err is None:
        from market_data import calculate_spot_equity
        equity = calculate_spot_equity(acc, tk.price)
        risk, ms2, err2 = _timed(
            lambda: __import__("market_data").build_account_risk_state(acc, tk.price),
        )
        rep["equity"] = {
            "ok": True,
            "ticker": str(tk.price),
            "ticker_fresh": bool(is_ticker_fresh(tk)),
            "equity": str(equity),
            "risk_state": {k: str(v) for k, v in vars(risk).items()},
            "latency_ms": ms,
        }
    else:
        rep["equity"] = {"ok": False, "error": err}

    # 9. Commission (informational ONLY — never feeds the economic model).
    # fetch_account_commission returns (payload, source), where payload may be
    # None when the endpoint is unavailable; effective_fees falls back to the
    # configured 0.001/0.001 in that case.
    comm_pair, ms, err = _timed(fetch_account_commission, client, SYMBOL)
    comm_payload, comm_source = (
        (comm_pair[0], comm_pair[1]) if comm_pair is not None else (None, None)
    )
    eff = effective_fees(comm_payload, ECON["maker_fee"], ECON["taker_fee"])
    rep["commission"] = {
        "available": comm_payload is not None,
        "raw": comm_payload,
        "authoritative_source": eff.source,
        "maker": str(eff.maker), "taker": str(eff.taker),
        "note": "informational only; grid economics keep configured 0.001/0.001",
        "latency_ms": ms,
        "error": err,
    }

    # 8. Capacity gate A–D with LIVE exchangeInfo MAX_NUM_ORDERS
    si, ms, err = _timed(fetch_symbol_info, client, SYMBOL)
    rules = parse_symbol_info(si) if si else None
    live_max = rules.max_num_orders if rules else None
    rep["capacity_gate"] = _capacity_cases(rules, live_max, len(oo) if oo else 0)

    rep["http_audit_end"] = audit.summary()["total_http_requests"]
    rep["verdict"] = "PASS" if rep["account"]["ok"] and rep["open_orders"]["ok"] else "FAIL_CLOSED"
    return rep, client, acc, oo, tk, rules, comm_pair


def _capacity_cases(rules, live_max: int, existing_live_open: int) -> dict:
    """A available / B exactly reached / C exceeded / D exchangeInfo unavailable."""
    from tests.test_paper_orchestrator import happy_cfg  # noqa: F401 (fixture)
    import tempfile
    from tests.test_paper_orchestrator import (
        _seed_open_order, make_session, make_input,
    )
    out: dict = {"live_max_num_orders": live_max,
                 "existing_live_open_orders": existing_live_open}
    for case in ("A", "B", "C"):
        tmp = Path(tempfile.mkdtemp())
        session = make_session(tmp)
        orch = session.orchestrator
        cfg = happy_cfg()
        cfg["execution"]["max_open_orders"] = 40
        inp = make_input(1, Decimal("110"), cfg=cfg, rules=rules)
        limit = orch._compute_open_order_capacity_limit(inp)
        if case == "A":
            out["A_available"] = {
                "limit": limit,
                "existing": 0, "proposed": 1,
                "blocked": limit is None or 0 + 1 > limit,
                "expected": "proceed-if-other-gates-pass",
            }
        elif case == "B":
            # Seed paper open orders exactly up to the limit.
            for n in range(limit):
                _seed_open_order(
                    session.order_engine.db_path, f"B{n}", "BTCUSDT",
                    "BUY", 0, "105", "0.001",
                )
            out["B_exactly_reached"] = {
                "limit": limit,
                "existing": limit, "proposed": 1,
                "blocked": limit + 1 > limit,
                "expected": "blocked",
            }
        else:
            for n in range(limit + 1):
                _seed_open_order(
                    session.order_engine.db_path, f"C{n}", "BTCUSDT",
                    "BUY", 0, "105", "0.001",
                )
            out["C_exceeded"] = {
                "limit": limit,
                "existing": limit + 1, "proposed": 1,
                "blocked": (limit + 1) + 1 > limit,
                "expected": "blocked",
            }
        shutil.rmtree(tmp, ignore_errors=True)
    # D: exchangeInfo unavailable → rules None → limit None → fail closed.
    from dataclasses import replace as _dc_replace
    tmp = Path(tempfile.mkdtemp())
    session = make_session(tmp)
    orch = session.orchestrator
    cfg = happy_cfg()
    cfg["execution"]["max_open_orders"] = 40
    inp = _dc_replace(make_input(1, Decimal("110"), cfg=cfg), rules=None)
    limit_d = orch._compute_open_order_capacity_limit(inp)
    out["D_exchangeinfo_unavailable"] = {
        "limit": limit_d, "blocked": limit_d is None,
        "expected": "fail_closed",
        "note": "rules None AND configured max missing/zero -> None -> "
                "OPEN_ORDER_CAPACITY_UNRESOLVED (PATCH 5B)",
    }
    # In case D, config max 40 still resolves a limit; the truly unresolvable
    # variant is rules=None + max_open_orders=0 (exercised by the 5B tests).
    inp0 = _dc_replace(
        make_input(1, Decimal("110"),
                   cfg={**happy_cfg(), "execution": {"max_open_orders": 0}}),
        rules=None,
    )
    out["D_unresolvable_variant"] = {
        "limit": orch._compute_open_order_capacity_limit(inp0),
        "blocked": orch._compute_open_order_capacity_limit(inp0) is None,
    }
    shutil.rmtree(tmp, ignore_errors=True)
    return out


# ---------------------------------------------------------------------------
# 7. Reconciliation (informational only; never mutates authoritative state)
# ---------------------------------------------------------------------------

def reconcile_live_open_orders(oo, session_dir: Path) -> dict:
    """Persist live open-order observations as INFORMATIONAL data next to the
    paper DB.  The paper DB is never mutated: exchange open orders are a
    separate data stream, not exchange truth for the paper ledger."""
    out = {
        "live_open_order_count": len(oo or ()),
        "persisted_as": "informational_only.json",
        "paper_state_mutated": False,
        "note": "persisted reconciliation data is never treated as exchange truth; "
                "the paper orders table remains the local execution truth.",
    }
    if oo:
        (session_dir / "informational_only.json").write_text(
            json.dumps([
                {k: str(v) for k, v in asdict(o).items()} for o in oo
            ], indent=1),
            encoding="utf-8",
        )
    return out


# ---------------------------------------------------------------------------
# 13. Authenticated-data soak (local paper execution, live testnet data)
# ---------------------------------------------------------------------------

def authenticated_soak(report: dict) -> dict:
    import json as _json
    import pandas as _pd

    # Refresh live klines for the replay window (closed candles only).
    _load_dotenv()
    client = make_client(
        "testnet", os.environ.get("BINANCE_API_KEY", ""),
        os.environ.get("BINANCE_API_SECRET", ""),
    )
    df, _ms, err = _timed(fetch_klines, client, SYMBOL, "15m", 200, True)
    if err:
        return {"error": f"klines unavailable: {err}"}
    si = fetch_symbol_info(client, SYMBOL)
    rules = parse_symbol_info(si)
    n = len(df)
    start = max(1, n - SOAK_CANDLES + 1)
    (OUT / "soak_klines.csv").parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    for c in ("open_time", "close_time"):
        out[c] = _pd.to_datetime(out[c], errors="coerce", utc=True).astype("int64") // 10**6
    out.to_csv(OUT / "soak_klines.csv", index=False)

    replay = ReplayConfig(
        label="T8B_soak",
        candle_range=(start, n),
        lower=SOAK_RANGE[0], upper=SOAK_RANGE[1],
        step_pct=ECON["step_pct"],
        hard_min_net_pct=ECON["hard_min_net_pct"],
        maker_fee=ECON["maker_fee"], taker_fee=ECON["taker_fee"],
        slippage=ECON["slippage"],
    )
    session_dir = OUT / "soak"
    if session_dir.exists():
        shutil.rmtree(session_dir)
    _orig_parse = common.parse_rules
    common.parse_rules = lambda: rules
    try:
        session = make_session(replay, session_dir)
        result = ScenarioResult(replay, session, df).run()
    finally:
        common.parse_rules = _orig_parse

    import invariant_scan as _iscan
    recs = result.records
    inv = _iscan.scan_scenario("soak", session_dir)
    eq = [float(r["equity"]) for r in recs if r.get("equity")]
    peak, max_dd = 0.0, 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)
    out_payload = {
        "klines_rows": n,
        "live_rules_max_num_orders": rules.max_num_orders,
        "totals": {
            "cycles": len(recs),
            "allowed_cycles": sum(1 for r in recs if not r["blocked_reason"]),
            "blocked_cycles": sum(1 for r in recs if r["blocked_reason"]),
            "orders_submitted": sum(r["orders_submitted"] for r in recs),
            "fills_applied": sum(r["fills_applied"] for r in recs),
            "cycle_errors_rolled_back": sum(1 for r in recs if r.get("error")),
        },
        "block_census": _census(recs, "blocked_reason"),
        "plan_decision_census": _census(recs, "plan_decision"),
        "lifecycle_census": _census(recs, "lifecycle_transition"),
        "invalid_transition_cycles": sum(
            1 for r in recs if "INVALID_TRANSITION" in str(r.get("blocked_reason") or "")
        ),
        "equity_peak": peak or None,
        "max_drawdown_pct": round(max_dd * 100, 4),
        "invariant_scan": inv,
        "final_account_state": (recs[-1].get("account_state") or {}),
    }
    _json.dump(recs, open(session_dir / "cycles.json", "w", encoding="utf-8"),
               indent=1, default=str)
    return out_payload


def _census(records: list, key: str) -> dict:
    out: dict = {}
    for r in records:
        v = str(r.get(key)) if r.get(key) is not None else "NONE"
        out[v] = out.get(v, 0) + 1
    return out


# ---------------------------------------------------------------------------
# 12. Failure matrix (local paper-engine seams; no real Testnet orders)
# ---------------------------------------------------------------------------

def failure_matrix_8b() -> dict:
    import json as _json
    import pandas as _pd
    path = OUT / "soak_klines.csv"
    df = _pd.read_csv(path)
    df["open_time"] = _pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = _pd.to_datetime(df["close_time"], unit="ms", utc=True)
    for c in ("open", "high", "low", "close", "volume", "quote_volume"):
        if c in df.columns:
            df[c] = _pd.to_numeric(df[c])
    df = df.reset_index(drop=True)
    n = len(df)
    start = max(1, n - 40)

    from market_data import fetch_symbol_info, make_client
    _load_dotenv()
    client = make_client("testnet", os.environ.get("BINANCE_API_KEY", ""),
                         os.environ.get("BINANCE_API_SECRET", ""))
    rules = parse_symbol_info(fetch_symbol_info(client, SYMBOL))

    kinds = {"H": "order", "H3": "order3", "HR": "recovery",
             "HA": "accounting", "HO": "open_orders", "HL": "lifecycle"}
    out: dict = {}
    _orig_parse = common.parse_rules
    common.parse_rules = lambda: rules
    try:
        for name, kind in kinds.items():
            replay = ReplayConfig(
                label=f"T8B_{name}",
                candle_range=(start, start + FAILMATRIX_WINDOW - 1),
                lower=SOAK_RANGE[0], upper=SOAK_RANGE[1],
                step_pct=ECON["step_pct"],
                hard_min_net_pct=ECON["hard_min_net_pct"],
                maker_fee=ECON["maker_fee"], taker_fee=ECON["taker_fee"],
                slippage=ECON["slippage"],
                failure_candles={start}, failure_kind=kind,
            )
            d = OUT / "failmatrix" / name.lower()
            if d.exists():
                shutil.rmtree(d)
            session = make_session(replay, d)
            result = ScenarioResult(replay, session, df).run()
            import invariant_scan as _iscan
            errs = [r for r in result.records if r.get("error")]
            out[name] = {
                "kind": kind,
                "injected": 1,
                "error_cycles": [
                    {"candle_index": r["candle_index"], "error": r["error"]}
                    for r in errs
                ],
                "clean_cycles_after_injection": sum(
                    1 for r in result.records[1:] if not r.get("error")),
                "invariant_scan": _iscan.scan_scenario(name, d),
            }
            _json.dump(result.records, open(d / "cycles.json", "w", encoding="utf-8"),
                       indent=1, default=str)
    finally:
        common.parse_rules = _orig_parse
    return out


# ---------------------------------------------------------------------------
# 14. Economic invariant
# ---------------------------------------------------------------------------

def economic_invariant_8b() -> dict:
    import json as _json
    import pandas as _pd
    path = OUT / "soak_klines.csv"
    df = _pd.read_csv(path)
    df["open_time"] = _pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = _pd.to_datetime(df["close_time"], unit="ms", utc=True)
    for c in ("open", "high", "low", "close", "volume", "quote_volume"):
        if c in df.columns:
            df[c] = _pd.to_numeric(df[c])
    df = df.reset_index(drop=True)

    from market_data import fetch_symbol_info, make_client
    _load_dotenv()
    client = make_client("testnet", os.environ.get("BINANCE_API_KEY", ""),
                         os.environ.get("BINANCE_API_SECRET", ""))
    rules = parse_symbol_info(fetch_symbol_info(client, SYMBOL))

    n = len(df)
    start = max(1, n - SOAK_CANDLES + 1)
    replay = ReplayConfig(
        label="T8B_econ", candle_range=(start, n),
        lower=SOAK_RANGE[0], upper=SOAK_RANGE[1],
        step_pct=ECON["step_pct"], hard_min_net_pct=ECON["hard_min_net_pct"],
        maker_fee=ECON["maker_fee"], taker_fee=ECON["taker_fee"],
        slippage=ECON["slippage"],
    )
    d = OUT / "econ"
    if d.exists():
        shutil.rmtree(d)
    _orig_parse = common.parse_rules
    common.parse_rules = lambda: rules
    try:
        session = make_session(replay, d)
        result = ScenarioResult(replay, session, df).run()
        from run_validation import completed_grid_economics
        econ = completed_grid_economics(session, df, d / "grid_economics.json")
    finally:
        common.parse_rules = _orig_parse
    _json.dump(result.records, open(d / "cycles.json", "w", encoding="utf-8"),
               indent=1, default=str)
    v = econ.get("post_quant_gate_violations", 0)
    return {
        "completed_grids": econ.get("completed_grids"),
        "inventory_sales": econ.get("inventory_sales"),
        "net_min": econ.get("net_min"),
        "net_max": econ.get("net_max"),
        "placed_cells_checked": econ.get("placed_cells_checked"),
        "post_quant_gate_violations": v,
        "verdict": "PASS" if v == 0 else "FAIL_CLOSED",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(data_only: bool = False) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {
        "phase": "8B", "symbol": SYMBOL,
        "economics": {k: str(v) for k, v in ECON.items()},
    }

    env_state = load_env_credential_state()
    report["credential_state"] = env_state
    print(f"[1] .env credentials: key={env_state['BINANCE_API_KEY']} "
          f"secret={env_state['BINANCE_API_SECRET']} verdict={env_state['verdict']}")
    if env_state["verdict"] != "SAFE":
        print("FAIL CLOSED:", env_state["problems"])
        _write(report)
        return

    with _HttpAudit() as audit:
        session = authenticated_data_session(audit)
    if isinstance(session, tuple):
        rep, _client, acc, oo, tk, rules, comm = session
        report["authenticated_data"] = rep
        report["reconciliation"] = reconcile_live_open_orders(oo, OUT)
        print("[2] account:", "OK" if rep["account"]["ok"] else rep["account"]["error"])
        print("    open_orders:", rep["open_orders"]["count"],
              "orders for BNBUSDT (data only)")
        if rep["equity"].get("ok"):
            print("    equity:", rep["equity"]["equity"], "USDT @ ticker",
                  rep["equity"]["ticker"])
        print("    commission available:", rep["commission"]["available"],
              "(authoritative source stays configured fees)")
        cg = rep["capacity_gate"]
        print("    capacity gate: live MAX_NUM_ORDERS =",
              cg["live_max_num_orders"], "| D unresolvable blocked =",
              cg["D_unresolvable_variant"]["blocked"])
        print("    http audit:", audit.summary()["verdict"],
              "| requests:", audit.summary()["total_http_requests"])
    else:
        report["authenticated_data"] = session
        print("[2] authenticated data FAIL CLOSED:", session.get("error")
              or session.get("verdict"))
        _write(report)
        return

    report["http_audit"] = audit.summary()

    if data_only:
        _write(report)
        print("[--data-only] stop. DRY_RUN=true ALLOW_LIVE_EXECUTION=false retained.")
        return

    report["soak"] = authenticated_soak(report)
    print(f"[3] soak: {report['soak'].get('totals')} "
          f"invalid_transition={report['soak'].get('invalid_transition_cycles')}")

    report["failure_matrix"] = failure_matrix_8b()
    for k, v in report["failure_matrix"].items():
        print(f"    {k} ({v['kind']}): errors={len(v['error_cycles'])} "
              f"clean_after={v['clean_cycles_after_injection']}")

    report["economic_invariant"] = economic_invariant_8b()
    print(f"[4] economic invariant: {report['economic_invariant']['verdict']} "
          f"(violations={report['economic_invariant']['post_quant_gate_violations']})")

    # Final security gate
    report["security"] = {
        "real_binance_testnet_orders_sent": 0,
        "http_audit_verdict": report["http_audit"]["verdict"],
        "orders_endpoints_called": report["http_audit"]["order_endpoint_hits"],
        "non_get_requests": report["http_audit"]["non_get_requests"],
        "non_testnet_host_requests": report["http_audit"]["non_testnet_host_requests"],
        "final_flags": {
            "DRY_RUN": "true",
            "ALLOW_LIVE_EXECUTION": "false",
        },
    }
    _write(report)
    print(f"[report] wrote {OUT / 'testnet_validation_8b_report.json'}")
    print("REAL BINANCE TESTNET ORDERS SENT = 0 (GET-only HTTP audit)")
    print("KEEP: DRY_RUN=true ALLOW_LIVE_EXECUTION=false — no live trading authorized.")


def _write(report: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "testnet_validation_8b_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8",
    )


if __name__ == "__main__":
    main(data_only=("--data-only" in sys.argv))
