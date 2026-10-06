"""Dashboard hardening tests: host/port binding, history telemetry, mode
display from the runtime's persisted record, missing-DB resilience, and
the read-only contract across every write method."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from conftest import wait_for_server
from dashboard import build_history, build_payload, make_server, render_page
from state import StateStore


@pytest.fixture
def seeded(tmp_path):
    path = str(tmp_path / "state.db")
    store = StateStore(path)
    store.ensure_symbols(["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"])
    store.set_meta("configured_symbols", json.dumps(
        ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"], separators=(",", ":")))
    store.set_meta("mode_binance_env", "testnet")
    store.set_meta("mode_execution", "paper")
    store.set_meta("session_id", "paper-abc123def456")
    store.set_meta("session_mode", "paper")
    store.set_meta("session_env", "testnet")
    store.set_meta_float("session_start_equity", 10000.0)
    store.set_meta_float("session_initial_cash", 10000.0)
    buy = store.create_order("cid-b", "BTC/USDT", "BUY", "LIMIT_MAKER", 49650.0, 0.00021, "dry_run")
    store.update_order_status(buy, "FILLED", 0.00021)
    store.record_fill(buy, "BTC/USDT", "BUY", 49650.0, 0.00021, 0.0104, trade_id="t-b1")
    sell = store.create_order("cid-s", "BTC/USDT", "SELL", "LIMIT_MAKER", 50000.0, 0.00021, "dry_run")
    store.update_order_status(sell, "FILLED", 0.00021)
    store.record_fill(sell, "BTC/USDT", "SELL", 50000.0, 0.00021, 0.0105, trade_id="t-s1")
    return path


# ----- operating mode display (runtime's persisted record) -----

def test_payload_carries_runtime_mode(seeded):
    payload = build_payload(seeded)
    glob = payload["global"]
    assert glob["binance_env"] == "testnet"
    assert glob["execution_mode"] == "paper"
    assert glob["session"]["mode"] == "paper"
    assert glob["session"]["env"] == "testnet"
    assert glob["wallet_usdt"] is None  # no wallet telemetry in this fixture
    assert glob["session"]["start_equity"] == pytest.approx(10000.0)
    page = render_page()
    assert "TESTNET WALLET" in page
    assert "PAPER EQUITY" in page
    # The equity KPI is the session PnL model in every mode; the actual
    # exchange read is the adjacent TESTNET WALLET KPI.
    assert "SESSION EQUITY" in page
    assert "EXCHANGE EQUITY" not in page


def test_payload_mode_reflects_runtime_record(seeded):
    store = StateStore(seeded)
    store.set_meta("mode_binance_env", "testnet")
    store.set_meta("mode_execution", "testnet")
    store.set_meta_float("wallet_usdt", 10000.0)
    glob = build_payload(seeded)["global"]
    assert glob["binance_env"] == "testnet"
    assert glob["execution_mode"] == "testnet"
    assert glob["wallet_usdt"] == pytest.approx(10000.0)


def test_bot_stamps_mode_into_state_for_the_dashboard(tmp_path):
    from bot import Bot, CycleView
    from conftest import make_config
    from exchange import DryRunExecutor

    store = StateStore(str(tmp_path / "state.db"))
    cfg = make_config()  # DRY_RUN=true, BINANCE_ENV=testnet

    class NoMarket:
        def snapshot(self, symbol, cfg, now_ms):
            return CycleView.__new__(CycleView)

    Bot(cfg, store, NoMarket(), DryRunExecutor(cfg, store))
    glob = build_payload(str(tmp_path / "state.db"))["global"]
    assert glob["binance_env"] == "testnet"
    assert glob["execution_mode"] == "paper"
    # session created from the configured capital override
    assert glob["session"]["start_equity"] == pytest.approx(1000.0)
    assert glob["session"]["mode"] == "paper"


# ----- equity / PnL history from the fills ledger -----

def test_history_is_reconstructed_from_fills_ledger(seeded):
    history = build_history(seeded)
    assert len(history) == 2
    # first point: the buy (net = -fee); second: the sell
    assert history[0]["net"] == pytest.approx(-0.0104, abs=1e-12)
    assert history[1]["net"] == pytest.approx(0.0735 - 0.0104 - 0.0105, abs=1e-12)
    assert history[0]["ts"] <= history[1]["ts"]


def test_history_empty_without_fills(tmp_path):
    path = str(tmp_path / "empty.db")
    StateStore(path).ensure_symbols(["BTC/USDT"])
    assert build_history(path) == []


def test_history_unavailable_on_missing_database(tmp_path):
    assert build_history(str(tmp_path / "nope.db")) == []


# ----- host / port binding (deterministic CLI configuration) -----

def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as resp:
        return resp.status, resp.read()


def test_binds_localhost_explicitly(seeded):
    server = make_server(seeded, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wait_for_server(server)
    try:
        assert server.server_address[0] == "127.0.0.1"
        base = f"http://127.0.0.1:{server.server_address[1]}"
        status, _ = _get(base, "/api/state")
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_binds_all_interfaces_when_configured(seeded):
    server = make_server(seeded, host="0.0.0.0", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wait_for_server(server)
    try:
        assert server.server_address[0] == "0.0.0.0"
        base = f"http://127.0.0.1:{server.server_address[1]}"
        status, _ = _get(base, "/")
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()


# ----- API surface -----

def test_api_history_endpoint(seeded):
    server = make_server(seeded, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wait_for_server(server)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        status, body = _get(base, "/api/history")
        assert status == 200
        points = json.loads(body.decode())
        assert len(points) == 2
    finally:
        server.shutdown()
        server.server_close()


def test_every_write_method_returns_405(seeded):
    server = make_server(seeded, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wait_for_server(server)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            req = urllib.request.Request(base + "/api/state", data=b"{}", method=method)
            _expect_405(base, method)
    finally:
        server.shutdown()
        server.server_close()


def _expect_405(base: str, method: str) -> None:
    """Assert a write method is refused with 405, tolerating transient
    socket teardown races (WinError 10053 / connection reset) under test
    load by retrying the request; an HTTP 405 is definitive and must not
    be retried. No assertion is weakened — every attempt must end in a
    405."""
    import time as _time

    attempts = 0
    while True:
        req = urllib.request.Request(base + "/api/state", data=b"{}", method=method)
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 405, method
            return
        except (ConnectionError, ConnectionResetError, ConnectionAbortedError) as exc:
            attempts += 1
            if attempts >= 10:
                raise  # give up: this is a real failure, not a transient race
            _time.sleep(0.05 * attempts)
            continue


def test_unknown_path_returns_404(seeded):
    server = make_server(seeded, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wait_for_server(server)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            _get(base, "/api/cancel-order")
            raise AssertionError("unknown path should 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_stays_available_when_database_missing(tmp_path):
    """A missing/corrupt database must show DATABASE UNAVAILABLE, never
    crash the dashboard."""
    missing = str(tmp_path / "nope.db")
    payload = build_payload(missing)
    assert payload["global"]["database"]["ok"] is False
    assert payload["global"]["equity"] is None

    server = make_server(missing, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wait_for_server(server)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        status, body = _get(base, "/api/state")
        assert status == 200
        data = json.loads(body.decode())
        assert data["global"]["database"]["ok"] is False
        status, _ = _get(base, "/")
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_shell_uses_safe_dom_rendering_only():
    """The console shell must be fully static: no server-side value
    interpolation, and dynamic rendering exclusively via textContent."""
    from dashboard import render_page

    page = render_page()
    assert "textContent" in page
    assert "innerHTML" not in page
    assert "document.write" not in page
    # the served markup (before the script) is fully static: no format
    # placeholders or template braces survive into it
    body = page.split("<body>")[1].split("<script>")[0]
    assert "{" not in body and "}" not in body
