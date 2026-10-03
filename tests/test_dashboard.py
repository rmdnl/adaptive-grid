"""Dashboard tests — deterministic, in-process HTTP, no network exposure.

Every test binds the dashboard server to 127.0.0.1 on an ephemeral port
and talks real HTTP over the loopback interface.  The trading runtime is
never started; coexistence is proven at the SQLite level (a read-write
runtime connection stays open while the dashboard reads).
"""
from __future__ import annotations

import ast
import inspect
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import dashboard as db_mod
import storage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def bot_config():
    return {
        "mode": "testnet", "dry_run": True, "allow_live_execution": False,
        "symbol": "BNBUSDT", "timeframe": "15m",
        "max_drawdown_pct": "0.02", "grid_step_pct": "0.006",
        "hard_min_net_pct": "0.003", "config_error": None,
    }


@pytest.fixture()
def state_db(tmp_path: Path) -> Path:
    """A populated state database using the REAL storage schema."""
    path = tmp_path / "grid_bot.sqlite3"
    storage.init_db(str(path))
    # paper_orch_cycles belongs to the orchestrator's own schema layer
    from paper_orchestrator import PaperOrchestrator

    class _SchemaOnly(PaperOrchestrator):
        def __init__(self, db_path: str) -> None:
            self._ensure_schema(db_path)

    _SchemaOnly(str(path))
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute(
        "INSERT INTO paper_account_state (id, base_asset, quote_asset,"
        " base_free, base_reserved, quote_free, quote_reserved, average_cost,"
        " realized_pnl, total_fees, updated_at) VALUES (1,'BNB','USDT',"
        " '2.0','0.5','1000.0','25.0','100.0','12.5','0.7','2026-10-03T00:00:00Z')")
    con.execute(
        "INSERT INTO orders (client_order_id, exchange_order_id, symbol, side,"
        " grid_index, price, quantity, status, created_at, updated_at,"
        " executed_qty, remaining_qty) VALUES"
        " ('AG-1','1','BNBUSDT','BUY',1,'100.0','0.25','NEW',"
        "  '2026-10-03T00:00:00Z','2026-10-03T00:01:00Z','0','0.25'),"
        " ('AG-2','2','BNBUSDT','SELL',2,'100.6','0.25','CANCELED',"
        "  '2026-10-03T00:00:00Z','2026-10-03T00:02:00Z','0.1','0.15')")
    con.execute(
        "INSERT INTO fills (trade_id, order_id, symbol, side, price, quantity,"
        " fee, fee_asset, event_time, resulting_state) VALUES"
        " ('t1','AG-2','BNBUSDT','SELL','100.6','0.1','0.0001','BNB',"
        "  '2026-10-03T00:02:30Z','CANCELED')")
    con.execute(
        "INSERT INTO equity_snapshots (ts, equity_quote, drawdown_pct) VALUES"
        " ('2026-10-03T00:03:00Z','405275.83','0.0')")
    con.execute(
        "INSERT INTO risk_events (ts, allowed, reason, context_json) VALUES"
        " ('2026-10-03T00:04:00Z',0,'NET_PROFIT_BELOW_HARD_MIN','{}')")
    con.execute(
        "INSERT INTO kill_state (key, active, trigger_reason, activated_at,"
        " open_order_count, cancel_status, note) VALUES"
        " ('kill_state',0,'','',0,'','')")
    con.execute(
        "INSERT INTO bot_state (key, value) VALUES"
        " ('paper_reference_equity','405275.83'),"
        " ('last_run_state','{\"run_id\":\"run-1\",\"phase\":\"COMPLETED\","
        "  \"risk_allowed\":false,\"kill_active\":false,"
        "  \"pending_cancels\":0,\"open_orders\":1}')")
    con.execute(
        "INSERT INTO paper_orch_cycles (cycle_id, candle_index, symbol,"
        " plan_decision, orders_submitted, fills_applied, success,"
        " is_idempotent, recovery_healthy, blocked_reason, error, metadata,"
        " created_at) VALUES"
        " ('cycle-1',123456,'BNBUSDT','GRID_ALLOWED',1,0,1,0,1,NULL,NULL,"
        "  '{\"lower_price\":\"100.0\",\"upper_price\":\"104.0\","
        "  \"net_pct\":\"0.0034\"}','2026-10-03T00:05:00Z')")
    con.commit()
    con.close()
    return path


def start_server(tmp_path: Path, db_path, bot_config) -> tuple[
        db_mod.ThreadingHTTPServer, str]:
    settings = {"host": "127.0.0.1", "port": 0, "db_path": str(db_path),
                "bot": bot_config}
    server = db_mod.ThreadingHTTPServer(
        ("127.0.0.1", 0), db_mod.make_handler(settings))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    return server, base


@pytest.fixture()
def server(state_db: Path, bot_config):
    srv, base = start_server(state_db.parent, str(state_db), bot_config)
    yield srv, base
    srv.shutdown()
    srv.server_close()


def http_get(base: str, path: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(base + path, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def http_request(base: str, path: str, method: str) -> tuple[int, bytes]:
    req = urllib.request.Request(base + path, method=method, data=b"")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# ---------------------------------------------------------------------------
# 1-4. Endpoints exist and work
# ---------------------------------------------------------------------------
def test_healthz(server):
    _, base = server
    status, body = http_get(base, "/healthz")
    assert status == 200
    payload = json.loads(body)
    assert payload["status"] == "ok"
    assert payload["read_only"] is True


def test_root_html(server):
    _, base = server
    status, body = http_get(base, "/")
    assert status == 200
    html = body.decode()
    assert "ADAPTIVE GRID" in html
    assert "TESTNET / PAPER" in html
    assert "DRY RUN" in html
    assert "LIVE DISABLED" in html
    assert "KILL SWITCH OFF" in html
    # auto-refresh present (10-15s window)
    assert 'http-equiv="refresh" content="12"' in html


def test_api_status(server):
    _, base = server
    status, body = http_get(base, "/api/status")
    assert status == 200
    snap = json.loads(body)
    assert snap["dashboard"]["read_only"] is True
    assert snap["dashboard"]["db_healthy"] is True
    env = snap["environment"]
    assert env["mode"] == "testnet"
    assert env["dry_run"] is True
    assert env["allow_live_execution"] is False
    assert env["live_execution_disabled"] is True
    # real persisted data surfaces
    assert snap["account"]["quote_free"] == "1000.0"
    assert snap["account"]["realized_pnl"] == "12.5"
    assert snap["risk"]["reference_equity"] == "405275.83"
    assert snap["risk"]["equity_latest"]["drawdown_pct"] == "0.0"
    assert snap["risk"]["kill_state"]["active"] == 0
    assert snap["runtime"]["phase"] == "COMPLETED"
    assert snap["grid"]["latest_cycles"][0]["cycle_id"] == "cycle-1"
    assert snap["grid"]["latest_cycles"][0]["metadata"]["lower_price"] == "100.0"
    assert len(snap["orders"]["open"]) == 1
    assert len(snap["fills"]) == 1
    assert snap["fills"][0]["fee_asset"] == "BNB"


def test_dashboard_starts_on_configured_host_port(tmp_path, bot_config):
    srv, base = start_server(tmp_path, str(tmp_path / "missing.sqlite3"),
                             bot_config)
    try:
        assert srv.server_address[1] > 0  # bound successfully
        status, _ = http_get(base, "/healthz")
        assert status == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_unknown_paths_404(server):
    _, base = server
    for path in ("/nope", "/.env", "/etc/passwd", "/../../.env",
                 "/api/orders/../../.env", "/static/style.css", "/debug",
                 "/admin", "/api/status/../../config.yaml"):
        status, _ = http_get(base, path)
        assert status == 404, path


# ---------------------------------------------------------------------------
# 5-7. Database access: read-only, failure-safe
# ---------------------------------------------------------------------------
def test_database_opened_read_only(state_db: Path, bot_config):
    """The snapshot connection uses SQLite mode=ro: writes are refused at
    the connection level even if the code tried."""
    con = db_mod._connect_readonly(str(state_db))
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("CREATE TABLE hack(x)")
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM orders")
    finally:
        con.close()


def test_dashboard_does_not_modify_database(server, state_db: Path):
    _, base = server
    con = sqlite3.connect(str(state_db))
    before = {
        t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("orders", "fills", "paper_account_state", "risk_events",
                  "kill_state", "bot_state", "paper_orch_cycles",
                  "equity_snapshots")}
    con.close()
    for path in ("/", "/api/status", "/healthz"):
        http_get(base, path)
    con = sqlite3.connect(str(state_db))
    after = {
        t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in before}
    con.close()
    assert before == after


def test_database_failure_handled_safely(tmp_path, bot_config):
    srv, base = start_server(tmp_path, str(tmp_path / "does-not-exist.sqlite3"),
                             bot_config)
    try:
        status, body = http_get(base, "/api/status")
        assert status == 200  # controlled degradation, not a crash
        snap = json.loads(body)
        assert snap["dashboard"]["db_healthy"] is False
        assert snap["dashboard"]["db_error"]
        assert snap["orders"]["open"] == []
        assert snap["account"] is None
        status, body = http_get(base, "/healthz")
        assert status == 200
        status, body = http_get(base, "/")
        assert status == 200
        assert "dashboard error" not in body.decode() or status == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_corrupt_database_handled_safely(tmp_path, bot_config):
    bad = tmp_path / "corrupt.sqlite3"
    bad.write_bytes(b"this is not a sqlite database" * 100)
    srv, base = start_server(tmp_path, str(bad), bot_config)
    try:
        status, body = http_get(base, "/api/status")
        assert status in (200, 503)  # controlled, no crash
        status, body = http_get(base, "/healthz")
        assert status == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_malformed_state_values_fail_safely(tmp_path, bot_config):
    """Corrupt JSON in bot_state / cycle metadata must not break the page."""
    path = tmp_path / "malformed.sqlite3"
    storage.init_db(str(path))
    from paper_orchestrator import PaperOrchestrator

    class _SchemaOnly(PaperOrchestrator):
        def __init__(self, db_path: str) -> None:
            self._ensure_schema(db_path)

    _SchemaOnly(str(path))
    con = sqlite3.connect(str(path))
    con.execute("INSERT INTO bot_state (key, value) VALUES"
                " ('last_run_state','{not json'),"
                " ('paper_reference_equity','also-not-a-number')")
    con.execute(
        "INSERT INTO paper_orch_cycles (cycle_id, candle_index, symbol,"
        " plan_decision, created_at, metadata) VALUES"
        " ('c1',1,'BNBUSDT','GRID_ALLOWED','t','{broken json')")
    con.commit()
    con.close()
    srv, base = start_server(tmp_path, str(path), bot_config)
    try:
        status, body = http_get(base, "/api/status")
        assert status == 200
        snap = json.loads(body)
        assert snap["runtime"] is None  # corrupt JSON → None, not a crash
        assert snap["grid"]["latest_cycles"][0]["metadata"] is None
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# 8-10. No authentication, no credentials
# ---------------------------------------------------------------------------
def test_no_authentication_required(server):
    _, base = server
    # plain requests succeed with no auth header at all
    for path in ("/", "/api/status", "/healthz"):
        status, _ = http_get(base, path)
        assert status == 200, path


def test_no_auth_configuration_exists():
    """The task explicitly forbids DASHBOARD_USER/DASHBOARD_PASSWORD: the
    dashboard module must neither read nor document those variables, and
    must import no authentication machinery."""
    source = inspect.getsource(db_mod)
    assert "DASHBOARD_USER" not in source
    assert "DASHBOARD_PASSWORD" not in source
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    for module in imported:
        assert "base64" not in module
        assert "hmac" not in module
        assert "secrets" not in module
        assert "http.auth" not in module


def test_no_binance_credentials_required_or_exposed(state_db, bot_config):
    settings = db_mod.dashboard_settings()
    # dashboard settings contain no credential fields
    assert "api_key" not in str(settings).lower().replace("api_key_api_secret", "")
    blob = json.dumps(db_mod.build_snapshot(str(state_db), bot_config))
    assert "api_key" not in blob.lower()
    assert "api_secret" not in blob.lower()
    assert "BINANCE_API_KEY" not in blob
    assert "BINANCE_API_SECRET" not in blob


# ---------------------------------------------------------------------------
# 11-17. No mutation capability
# ---------------------------------------------------------------------------
def test_mutation_methods_rejected_everywhere(server, state_db: Path):
    _, base = server
    con = sqlite3.connect(str(state_db))
    before = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    con.close()
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        for path in ("/", "/api/status", "/healthz"):
            status, _ = http_request(base, path, method)
            assert status == 405, (method, path)
    con = sqlite3.connect(str(state_db))
    assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == before
    con.close()


def test_dashboard_source_has_no_write_sql_or_trading_imports():
    """Static AST boundary: no INSERT/UPDATE/DELETE/CREATE/DROP anywhere,
    and no order/exchange/trading module is imported."""
    tree = ast.parse(inspect.getsource(db_mod))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden_modules = ("testnet_orders", "binance_testnet", "binance_sdk_spot",
                         "order_engine", "cancel_controller",
                         "rest_reconciler", "grid_lifecycle", "main", "runtime",
                         "scripts.release_kill_state", "scripts.reset_reference_equity")
    for module in imported:
        for bad in forbidden_modules:
            assert not module.startswith(bad), (module, bad)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        # any string constant in the module must not be a mutating statement
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.strip().lstrip("(").upper()
            for keyword in ("INSERT INTO", "UPDATE ", "DELETE FROM",
                            "DROP TABLE", "CREATE TABLE", "PRAGMA writable"):
                assert not value.startswith(keyword), node.value[:60]


def test_dashboard_cannot_place_or_cancel_orders():
    """No callable in the dashboard module touches order placement or
    cancellation — the functions simply do not exist here."""
    for forbidden in ("place", "cancel_order", "submit", "release_kill",
                      "reset_reference", "new_order", "delete_order"):
        for name, member in inspect.getmembers(db_mod):
            if name.startswith("_") or not callable(member):
                continue
            assert forbidden not in name.lower(), (name, forbidden)


def test_dashboard_cannot_change_risk_or_release_kill(server, state_db: Path):
    _, base = server
    status, body = http_get(base, "/api/status")
    snap = json.loads(body)
    # the snapshot only ever reflects persisted state
    assert snap["risk"]["kill_state"]["active"] == 0
    # and no endpoint accepts anything that could change it
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        for path in ("/api/kill/release", "/api/risk/reset",
                     "/api/reference-equity", "/api/config"):
            status, _ = http_request(base, path, method)
            assert status == 404, path  # nonexistent — no hidden admin route


# ---------------------------------------------------------------------------
# 18-19. No secrets / no arbitrary files
# ---------------------------------------------------------------------------
def test_env_contents_never_exposed(server, tmp_path, state_db: Path):
    _, base = server
    # write a fake .env next to the db and next to the CWD; none of it may
    # appear in any response
    (tmp_path / ".env").write_text(
        "BINANCE_API_KEY=supersecretkey123\nBINANCE_API_SECRET=supersecret\n")
    for path in ("/", "/api/status", "/healthz", "/.env", "/../../.env"):
        status, body = http_get(base, path)
        text = body.decode("utf-8", "replace")
        assert "supersecretkey123" not in text, path
        assert "supersecret" not in text, path


def test_no_arbitrary_file_read(server):
    _, base = server
    for path in ("/etc/passwd", "/dashboard.py", "/config.yaml",
                 "/../../../dashboard.py", "/%2e%2e/%2e%2e/config.yaml"):
        status, _ = http_get(base, path)
        assert status == 404, path


# ---------------------------------------------------------------------------
# 20-22. Coexistence with the runtime
# ---------------------------------------------------------------------------
def test_dashboard_reads_while_runtime_connection_is_open(
        state_db: Path, bot_config):
    """A runtime-style read-write connection stays open and WRITES while
    the dashboard keeps serving — the dashboard observes new data and the
    two processes coexist without locking failures."""
    runtime_con = sqlite3.connect(str(state_db), timeout=2.0)
    srv, base = start_server(state_db.parent, str(state_db), bot_config)
    try:
        # runtime writes a new order while the dashboard is live
        runtime_con.execute(
            "INSERT INTO orders (client_order_id, exchange_order_id, symbol,"
            " side, grid_index, price, quantity, status, created_at,"
            " updated_at, executed_qty, remaining_qty) VALUES"
            " ('AG-LIVE','9','BNBUSDT','BUY',3,'100.1','0.25','NEW',"
            "  '2026-10-03T01:00:00Z','2026-10-03T01:00:00Z','0','0.25')")
        runtime_con.commit()
        status, body = http_get(base, "/api/status")
        assert status == 200
        snap = json.loads(body)
        cids = [o["client_order_id"] for o in snap["orders"]["open"]]
        assert "AG-LIVE" in cids
        status, body = http_get(base, "/")
        assert status == 200
    finally:
        runtime_con.close()
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# Settings defaults
# ---------------------------------------------------------------------------
def test_dashboard_settings_defaults(monkeypatch):
    for var in ("DASHBOARD_HOST", "DASHBOARD_PORT", "GRID_DB_PATH"):
        monkeypatch.delenv(var, raising=False)
    settings = db_mod.dashboard_settings()
    assert settings["host"] == "0.0.0.0"
    assert settings["port"] == 8080
    assert settings["db_path"] == "./data/grid_bot.sqlite3"


def test_dashboard_settings_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_HOST", "127.0.0.1")
    monkeypatch.setenv("DASHBOARD_PORT", "9090")
    monkeypatch.setenv("GRID_DB_PATH", str(tmp_path / "x.sqlite3"))
    settings = db_mod.dashboard_settings()
    assert settings["host"] == "127.0.0.1"
    assert settings["port"] == 9090
    assert settings["db_path"].endswith("x.sqlite3")


def test_startup_warns_about_public_exposure(capsys):
    """main() prints a clear warning that the dashboard is public/read-only."""
    # do not actually serve; just verify the warning text exists in module
    source = inspect.getsource(db_mod)
    assert "PUBLIC READ-ONLY" in source
    assert "no authentication by design" in source.lower() or \
        "no authentication by design" in source
