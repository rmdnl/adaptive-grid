"""Dashboard tests: read-only payload, global + per-symbol state, missing
data handling, no credentials, no mutation endpoints."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import json
import pytest

from conftest import wait_for_server
from dashboard import KNOWN_STATES, build_payload, build_history, make_server, render_page
from state import StateStore


@pytest.fixture
def seeded(tmp_path):
    path = str(tmp_path / "state.db")
    store = StateStore(path)
    store.ensure_symbols(["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"])
    store.set_meta("configured_symbols", json.dumps(
        ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"], separators=(",", ":")))
    store.update_symbol(
        "BTC/USDT",
        timeframe="4h",
        last_price=50000.0,
        adx=15.0, rsi=30.0, percent_b=-0.1, volume_osc=0.2, zscore=0.5, atr=350.0,
        strategy_state="ACTIVE",
        grid_mode="arithmetic", grid_step=350.0, grid_lower=48250.0,
        gross_pct=0.007, net_pct=0.004,
        inventory_qty=0.00021, avg_cost=49650.0,
    )
    oid = store.create_order("cid-open", "BTC/USDT", "BUY", "LIMIT_MAKER", 49300.0, 0.00021, "dry_run")
    assert oid > 0
    bid = store.create_order("cid-buy", "BTC/USDT", "BUY", "LIMIT_MAKER", 49650.0, 0.00021, "dry_run")
    store.update_order_status(bid, "FILLED", 0.00021)
    store.record_fill(bid, "BTC/USDT", "BUY", 49650.0, 0.00021, 0.0104, trade_id="t-b1")
    sid = store.create_order("cid-done", "BTC/USDT", "SELL", "LIMIT_MAKER", 50000.0, 0.00021, "dry_run")
    store.update_order_status(sid, "FILLED", 0.00021)
    store.record_fill(sid, "BTC/USDT", "SELL", 50000.0, 0.00021, 0.0105, trade_id="t-s1")
    store.set_meta_float("equity", 100.0)
    store.set_meta_float("reference_equity", 102.0)
    store.set_runtime("RUNNING", 1234.0)
    return path


def test_known_states_are_the_nine_specified():
    assert KNOWN_STATES == (
        "WAITING", "ENTRY_BLOCKED", "GRID_BLOCKED", "ACTIVE", "COOLDOWN",
        "EXITING", "STOPPED", "KILL_ACTIVE", "ERROR",
    )


def test_payload_global_fields(seeded):
    payload = build_payload(seeded, max_drawdown_percent=2.0)
    glob = payload["global"]
    for key in (
        "equity", "reference_equity", "drawdown", "max_drawdown_percent",
        "kill_active", "kill_reason", "open_orders", "realized_pnl", "fees",
        "runtime_status", "database",
    ):
        assert key in glob
    assert glob["equity"] == pytest.approx(100.0)
    assert glob["reference_equity"] == pytest.approx(102.0)
    assert glob["drawdown"] == pytest.approx(2.0 / 102.0)
    assert glob["max_drawdown_percent"] == 2.0
    assert glob["kill_active"] is False
    assert glob["open_orders"] == 1
    assert glob["realized_pnl"] == pytest.approx(0.0735)
    assert glob["runtime_status"] == "RUNNING"
    assert glob["database"]["ok"] is True


def test_payload_lists_all_four_symbols(seeded):
    payload = build_payload(seeded)
    # Dashboard ordering follows the active session's PAIR_LIST, not the DB
    assert [s["symbol"] for s in payload["symbols"]] == [
        "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT",
    ]


def test_payload_symbol_fields(seeded):
    btc = next(s for s in build_payload(seeded)["symbols"] if s["symbol"] == "BTC/USDT")
    assert btc["strategy_state"] == "ACTIVE"
    assert btc["timeframe"] == "4h"
    assert btc["adx"] == pytest.approx(15.0)
    assert btc["atr"] == pytest.approx(350.0)
    assert btc["entry_status"] == "allowed"
    assert btc["exit_status"] == "none"
    assert btc["cooldown"] is False
    assert btc["grid_mode"] == "arithmetic"
    assert btc["grid_step"] == pytest.approx(350.0)
    assert btc["grid_count"] == 1
    assert btc["gross_pct"] == pytest.approx(0.007)
    assert btc["net_pct"] == pytest.approx(0.004)
    assert btc["inventory_qty"] == pytest.approx(0.00021)
    assert btc["avg_cost"] == pytest.approx(49650.0)
    assert btc["unrealized_pnl"] == pytest.approx(0.00021 * (50000.0 - 49650.0))
    assert btc["open_orders"] == 1
    assert btc["realized_pnl"] == pytest.approx(0.0735)
    assert btc["fees"] == pytest.approx(0.0104 + 0.0105)
    assert btc["risk_status"] == "ok"


def test_payload_missing_data_is_none_not_inferred(seeded):
    # ETH has no market data yet: fields are None, never fabricated.
    eth = next(s for s in build_payload(seeded)["symbols"] if s["symbol"] == "ETH/USDT")
    assert eth["strategy_state"] == "WAITING"
    assert eth["adx"] is None
    assert eth["plus_di"] is None
    assert eth["stoch_k"] is None
    assert eth["last_price"] is None
    assert eth["grid_mode"] is None
    assert eth["grid_count"] == 0
    assert eth["unrealized_pnl"] is None


def test_payload_state_is_verbatim_from_db(seeded):
    store = StateStore(seeded)
    store.set_symbol_state("ETH/USDT", "GRID_BLOCKED", block_reason="gross_below_minimum")
    eth = next(s for s in build_payload(seeded)["symbols"] if s["symbol"] == "ETH/USDT")
    assert eth["strategy_state"] == "GRID_BLOCKED"
    assert eth["block_reason"] == "gross_below_minimum"


def test_payload_kill_state(seeded):
    store = StateStore(seeded)
    store.set_global_kill("max_drawdown_breach")
    payload = build_payload(seeded)
    assert payload["global"]["kill_active"] is True
    assert "max_drawdown" in payload["global"]["kill_reason"]
    # the console shell carries the kill-switch instrumentation
    html = render_page()
    assert "KILL SWITCH" in html
    assert "killreason" in html


def test_payload_contains_no_credentials(seeded):
    text = json.dumps(build_payload(seeded)).lower()
    for forbidden in ("api_key", "api_secret", "secret", "credential", "token"):
        assert forbidden not in text


def test_payload_on_missing_database(tmp_path):
    payload = build_payload(str(tmp_path / "nope.db"))
    assert payload["global"]["database"]["ok"] is False
    assert payload["symbols"] == []


def test_payload_drawdown_none_before_first_cycle(tmp_path):
    """A fresh database has no equity/reference yet: drawdown must be None
    (dash), never a fabricated 0.00%."""
    path = str(tmp_path / "state.db")
    store = StateStore(path)
    store.ensure_symbols(["BTC/USDT"])
    payload = build_payload(path)
    assert payload["global"]["equity"] is None
    assert payload["global"]["reference_equity"] is None
    assert payload["global"]["drawdown"] is None


def test_payload_max_drawdown_from_runtime_meta(tmp_path):
    """Standalone deployments get the MAX DRAWDOWN limit from the runtime's
    own persisted meta record; an explicit caller argument overrides it."""
    path = str(tmp_path / "state.db")
    store = StateStore(path)
    store.ensure_symbols(["BTC/USDT"])
    store.set_meta_float("risk_max_drawdown_percent", 2.0)
    assert build_payload(path)["global"]["max_drawdown_percent"] == pytest.approx(2.0)
    assert build_payload(path, max_drawdown_percent=1.5)[
        "global"]["max_drawdown_percent"] == pytest.approx(1.5)
    # Without the meta record and without a caller argument: unknown.
    store2 = StateStore(str(tmp_path / "fresh.db"))
    store2.ensure_symbols(["BTC/USDT"])
    assert build_payload(str(tmp_path / "fresh.db"))[
        "global"]["max_drawdown_percent"] is None


def test_dashboard_server_read_only(seeded):
    from http.server import ThreadingHTTPServer

    from dashboard import DashboardHandler

    handler = type("H", (DashboardHandler,), {"db_path": seeded, "max_drawdown_percent": 2.0})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wait_for_server(server)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(f"{base}/api/state", timeout=5) as resp:
            assert resp.status == 200
            payload = json.loads(resp.read().decode())
        assert payload["global"]["equity"] == pytest.approx(100.0)
        with urllib.request.urlopen(f"{base}/", timeout=5) as resp:
            assert resp.status == 200
            page = resp.read()
            assert b"ADAPTIVE-GRID" in page          # console brand
            assert b"textContent" in page            # safe DOM rendering
            assert b"innerHTML" not in page          # no unsafe injection path

        try:
            urllib.request.urlopen(f"{base}/api/state", data=b"{}", timeout=5)
            raise AssertionError("POST should have been refused")
        except urllib.error.HTTPError as exc:
            assert exc.code == 405
    finally:
        server.shutdown()
        server.server_close()
