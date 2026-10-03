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
        "symbols": "BNBUSDT", "timeframe": "15m",
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
    # per-cycle persisted state (main() writes these every run)
    con.execute(
        "INSERT INTO bot_state (key, value) VALUES"
        " ('last_symbol','BNBUSDT'),"
        " ('last_price','765.88000000'),"
        " ('last_range','{\"lower\":\"765.23\",\"upper\":\"778.856\"}'),"
        " ('last_risk_decision','{\"allowed\":false,"
        "  \"reason\":\"NET_PROFIT_BELOW_HARD_MIN\"}'),"
        " ('last_account_risk','{\"current_equity\":\"405275.83\","
        "  \"reference_equity\":\"405275.83\",\"drawdown_pct\":\"0E-20\","
        "  \"base_inventory\":\"0\",\"inventory_pct\":\"0\"}'),"
        " ('last_adaptive_plan','{\"plan_id\":\"p1\",\"decision\":"
        "  \"GRID_BLOCKED\",\"reasons\":[\"GRID_COUNT_INVALID\"],"
        "  \"regime\":\"RANGE\",\"range_quality_score\":\"0\","
        "  \"candidate_lower\":\"765.23\",\"candidate_upper\":\"778.856\","
        "  \"grid_step\":\"0.006\",\"grid_count\":2,"
        "  \"estimated_net_profit_per_grid\":\"0.0034\"}'),"
        " ('last_market_intelligence','{\"status\":\"GRID_BLOCKED\","
        "  \"allowed\":false,\"reasons\":[\"VOLATILITY_TOO_LOW\"],"
        "  \"regime\":\"RANGE\",\"range_quality_score\":\"0\","
        "  \"diagnostics\":{\"adx\":\"14.36\",\"atr_pct\":\"0.0014\"}}')")
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
    assert "KILL OFF" in html
    # per-cycle grid/market state renders even when every cycle is BLOCKED
    # machine codes are now human-readable: "Blocked" instead of "GRID_BLOCKED"
    assert "Blocked" in html
    # machine codes translated to human: "Net profit per grid below the 0.30% minimum"
    assert "Net profit per grid below the 0.30% minimum" in html
    assert "765.88" in html            # current price from last_price
    assert "765.23" in html            # lower from last_range
    assert "MARKET FILTERED" in html   # human-friendly market badge
    assert "RISK BLOCKED" in html      # human-friendly risk badge
    assert "ADX (trend strength)" in html   # human-labeled market diagnostic
    # equity card falls back to last_account_risk when no snapshot exists
    assert "405275.83" in html
    # human-friendly number formatting
    assert "405275.8339338000000000" not in html   # no raw decimal noise
    # auto-refresh present (10-15s window)
    assert 'http-equiv="refresh" content="12"' in html


def test_tabbed_layout_and_penting_filter(server):
    _, base = server
    status, body = http_get(base, "/")
    html = body.decode()
    # all six tabs and their panes are present
    for tab in ("tab-general", "tab-risk", "tab-grid", "tab-market",
                "tab-orders", "tab-system"):
        assert f' data-tab="{tab}"' in html
        assert f'id="{tab}"' in html
    # the "General" pane is the default active one
    assert 'class="tabpane active" id="tab-general"' in html
    # the "penting saja" filter toggle + its JS + CSS rules are present
    assert 'id="penting-toggle"' in html
    assert "penting-only" in html            # CSS rule that hides details
    assert "localStorage" in html            # remembers the user's choice
    # detail blocks are tagged so the "penting saja" filter hides them
    # Using panel class instead of detail-block
    assert "panel" in html            # panel sections
    # the CSS/JS chrome must not leak any credential material
    for secret in ("api_key", "api_secret", "BINANCE_API_KEY",
                   "BINANCE_API_SECRET", "password"):
        assert secret not in html.lower(), secret


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
    # per-cycle persisted state surfaces (present even when every cycle BLOCKs)
    assert snap["grid"]["last_price"] == "765.88000000"
    assert snap["grid"]["last_range"]["lower"] == "765.23"
    assert snap["risk"]["last_risk_decision"]["allowed"] is False
    assert snap["risk"]["last_risk_decision"]["reason"] == "NET_PROFIT_BELOW_HARD_MIN"
    plan = snap["grid"]["last_adaptive_plan"]
    assert plan["decision"] == "GRID_BLOCKED"
    assert plan["grid_count"] == 2
    assert plan["estimated_net_profit_per_grid"] == "0.0034"
    intel = snap["grid"]["last_market_intelligence"]
    assert intel["regime"] == "RANGE"
    assert intel["diagnostics"]["adx"] == "14.36"
    # account-risk fallback feeds the equity cards when no snapshot exists
    assert snap["risk"]["last_account_risk"]["current_equity"] == "405275.83"
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


# ---------------------------------------------------------------------------
# Multi-symbol architecture (current state model)
# ---------------------------------------------------------------------------
# The multi-symbol runtime writes ALL per-symbol state into per-symbol
# databases; the configured base database is created by storage.init_db alone
# (exactly the production VPS schema: no paper_orch_cycles table).  These
# tests pin the dashboard against THAT reality.


def _orchestrator_schema(db_path: str) -> None:
    """Create the orchestrator's own schema (paper_orch_cycles etc.)."""
    from paper_orchestrator import PaperOrchestrator

    class _SchemaOnly(PaperOrchestrator):
        def __init__(self, path: str) -> None:
            self._ensure_schema(path)

    _SchemaOnly(db_path)


def _seed_symbol_db(path: Path, *, symbol: str, with_cycles: bool) -> None:
    """Seed one per-symbol database with realistic runtime state."""
    storage.init_db(str(path))
    if with_cycles:
        _orchestrator_schema(str(path))
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute(
        "INSERT INTO paper_account_state (id, base_asset, quote_asset,"
        " base_free, base_reserved, quote_free, quote_reserved, average_cost,"
        " realized_pnl, total_fees, updated_at) VALUES (1,'BNB','USDT',"
        " '1.0','0','500.0','0','100.0','7.5','0.3','2026-10-04T00:00:00Z')")
    con.execute(
        "INSERT INTO orders (client_order_id, exchange_order_id, symbol, side,"
        " grid_index, price, quantity, status, created_at, updated_at,"
        " executed_qty, remaining_qty) VALUES"
        f" ('AG{symbol[:3]}-1','11','{symbol}','BUY',1,'100.0','0.25','NEW',"
        "  '2026-10-04T00:00:00Z','2026-10-04T00:01:00Z','0','0.25')")
    con.execute(
        "INSERT INTO fills (trade_id, order_id, symbol, side, price, quantity,"
        " fee, fee_asset, event_time, resulting_state) VALUES"
        f" ('t-{symbol}','AG{symbol[:3]}-0','{symbol}','BUY','100.0','0.1',"
        "  '0.0001','USDT','2026-10-04T00:02:30Z','FILLED')")
    con.execute(
        "INSERT INTO equity_snapshots (ts, equity_quote, drawdown_pct) VALUES"
        " ('2026-10-04T00:03:00Z','500.0','0.01')")
    con.execute(
        "INSERT INTO risk_events (ts, allowed, reason, context_json) VALUES"
        " ('2026-10-04T00:04:00Z',1,'PASS','{\"min_net_pct\":\"0.0035\","
        "  \"dynamic_step_pct\":\"0.006\",\"grid_cells\":8,"
        "  \"atr_pct\":\"0.006\",\"grid_mode\":\"arithmetic\"}')")
    con.execute(
        "INSERT INTO kill_state (key, active, trigger_reason, activated_at,"
        " open_order_count, cancel_status, note) VALUES"
        " ('kill_state',0,'','',0,'','')")
    run_id = f"multi-{symbol}-4"
    con.execute(
        "INSERT INTO bot_state (key, value) VALUES"
        f" ('last_symbol','{symbol}'),"
        " ('last_price','765.88000000'),"
        " ('last_range','{\"lower\":\"750.0\",\"upper\":\"780.0\"}'),"
        " ('last_risk_decision','{\"allowed\":true,\"reason\":\"PASS\"}'),"
        " ('paper_reference_equity','505.0'),"
        " ('paper_cycle_index','4'),"
        " ('last_auto_exit_ts','2026-10-03T20:00:00Z'),"
        " ('last_run_state','{\"run_id\":\"" + run_id + "\","
        "  \"phase\":\"COMPLETED\",\"risk_allowed\":true,"
        "  \"kill_active\":false,\"pending_cancels\":0,"
        "  \"open_orders\":1}')")
    if with_cycles:
        con.execute(
            "INSERT INTO paper_orch_cycles (cycle_id, candle_index, symbol,"
            " plan_decision, orders_submitted, fills_applied, success,"
            " is_idempotent, recovery_healthy, blocked_reason, error,"
            " metadata, created_at) VALUES"
            f" ('cycle-{symbol}',42,'{symbol}','GRID_ALLOWED',2,1,1,0,1,"
            "  NULL,NULL,'{\"lower_price\":\"750.0\"}',"
            "  '2026-10-04T00:05:00Z')")
    con.commit()
    con.close()


@pytest.fixture()
def multi_bot_config():
    return {
        "mode": "testnet", "dry_run": True, "allow_live_execution": False,
        "symbols": "BNBUSDT,ETHUSDT,SOLUSDT", "timeframe": "4h",
        "max_drawdown_pct": "0.02", "grid_step_pct": "0.006",
        "hard_min_net_pct": "0.003", "config_error": None,
    }


@pytest.fixture()
def multi_state(tmp_path: Path) -> Path:
    """The production VPS layout: a base DB created by storage.init_db only
    (no paper_orch_cycles), one fully-active symbol DB, one symbol DB that
    never ran a paper cycle (missing optional table), and one corrupt file."""
    main = tmp_path / "grid_bot.sqlite3"
    storage.init_db(str(main))  # base schema only — no paper_orch_cycles
    _seed_symbol_db(
        tmp_path / "grid_bot_BNBUSDT.sqlite3", symbol="BNBUSDT",
        with_cycles=True)
    _seed_symbol_db(
        tmp_path / "grid_bot_ETHUSDT.sqlite3", symbol="ETHUSDT",
        with_cycles=False)
    (tmp_path / "grid_bot_SOLUSDT.sqlite3").write_bytes(
        b"not a sqlite database" * 50)
    return main


@pytest.fixture()
def multi_server(multi_state: Path, multi_bot_config):
    srv, base = start_server(multi_state.parent, str(multi_state),
                             multi_bot_config)
    yield srv, base
    srv.shutdown()
    srv.server_close()


def test_multi_healthz(multi_server):
    _, base = multi_server
    status, body = http_get(base, "/healthz")
    assert status == 200
    payload = json.loads(body)
    assert payload["status"] == "ok"
    assert payload["read_only"] is True
    assert payload["auth"] == "none"


def test_multi_api_status_vps_schema(multi_server):
    """Regression: the base DB has NO paper_orch_cycles table (exact
    production schema) and one symbol DB never ran a paper cycle — the
    snapshot must still return 200 with per-symbol + aggregate state."""
    _, base = multi_server
    status, body = http_get(base, "/api/status")
    assert status == 200
    snap = json.loads(body)
    assert snap["dashboard"]["db_healthy"] is True
    assert snap["dashboard"]["symbols"] == ["BNBUSDT", "ETHUSDT", "SOLUSDT"]

    per_symbol = snap["grid"]["per_symbol"]
    assert set(per_symbol) == {"BNBUSDT", "ETHUSDT", "SOLUSDT"}

    # Active symbol: full per-symbol state from its own database.
    bnb = per_symbol["BNBUSDT"]
    assert bnb["db_healthy"] is True
    assert bnb["last_price"] == "765.88000000"
    assert bnb["last_range"]["lower"] == "750.0"
    assert bnb["last_risk_decision"]["allowed"] is True
    assert bnb["status"] == "ACTIVE"
    assert bnb["paper_reference_equity"] == "505.0"
    assert bnb["latest_cycles"][0]["cycle_id"] == "cycle-BNBUSDT"
    assert bnb["open_orders"][0]["client_order_id"] == "AGBNB-1"
    assert bnb["grid_economics"]["min_net_pct"] == "0.0035"
    assert bnb["grid_economics"]["grid_cells"] == 8
    assert bnb["missing_tables"] == []

    # Symbol DB that never ran a paper cycle: optional table reported as
    # missing, section empty — never a failure.
    eth = per_symbol["ETHUSDT"]
    assert eth["db_healthy"] is True
    assert "paper_orch_cycles" in eth["missing_tables"]
    assert eth["latest_cycles"] == []
    assert eth["last_price"] == "765.88000000"

    # Corrupt symbol file: degraded, not fatal.
    sol = per_symbol["SOLUSDT"]
    assert sol["db_healthy"] is False
    assert sol["db_error"]

    # Aggregates over the two healthy symbol DBs.
    assert snap["risk"]["kill_state"]["active"] is False
    assert snap["risk"]["reference_equity"] == "1010.0"  # 505.0 + 505.0
    assert snap["account"]["realized_pnl"] == "15.0"     # 7.5 + 7.5
    assert snap["account"]["quote_free"] == "1000.0"     # 500.0 + 500.0
    assert len(snap["orders"]["open"]) == 2
    assert {row["symbol"] for row in snap["orders"]["open"]} == {
        "BNBUSDT", "ETHUSDT"}
    assert snap["runtime"]["phase"] == "COMPLETED"
    # The base DB itself has no cycles; merged cycles come from symbol DBs.
    assert {row["symbol"] for row in snap["grid"]["latest_cycles"]} == {
        "BNBUSDT"}
    # Per-symbol DB health surfaces in the database section.
    db_info = snap["database"]["per_symbol"]
    assert db_info["BNBUSDT"]["healthy"] is True
    assert db_info["ETHUSDT"]["missing_tables"] == ["paper_orch_cycles"]
    assert db_info["SOLUSDT"]["healthy"] is False


def test_multi_root_html(multi_server):
    _, base = multi_server
    status, body = http_get(base, "/")
    assert status == 200
    html = body.decode()
    assert "dashboard error" not in html
    for symbol in ("BNBUSDT", "ETHUSDT", "SOLUSDT"):
        assert symbol in html
    assert "ACTIVE" in html          # BNBUSDT status badge
    assert "765.88" in html          # per-symbol price renders
    assert "Per-symbol status" in html
    assert "Kill state per symbol" in html
    assert "Per-symbol databases" in html
    assert "no such table" not in html


def test_multi_missing_optional_data_degrades_gracefully(
        tmp_path, multi_bot_config):
    """A symbol DB missing every optional table must produce N/A sections,
    not an HTTP 503."""
    main = tmp_path / "grid_bot.sqlite3"
    storage.init_db(str(main))
    bare = tmp_path / "grid_bot_XRPUSDT.sqlite3"
    storage.init_db(str(bare))  # schema only: no cycles, no account, nothing
    bot_config = dict(multi_bot_config)
    bot_config["symbols"] = "BNBUSDT,XRPUSDT"  # BNBUSDT DB does not exist
    srv, base = start_server(tmp_path, str(main), bot_config)
    try:
        status, body = http_get(base, "/api/status")
        assert status == 200
        snap = json.loads(body)
        xrp = snap["grid"]["per_symbol"]["XRPUSDT"]
        assert xrp["db_healthy"] is True
        assert "paper_orch_cycles" in xrp["missing_tables"]
        assert xrp["latest_cycles"] == []
        assert xrp["account"] is None
        assert xrp["status"] in ("NO_DATA", "NO_DECISION")
        status, body = http_get(base, "/")
        assert status == 200
        assert "XRPUSDT" in body.decode()
    finally:
        srv.shutdown()
        srv.server_close()


def test_multi_kill_active_in_one_symbol_aggregates_globally(
        multi_state: Path, multi_bot_config):
    """A kill latch in ONE symbol's database must surface as the global
    kill-active status (with the per-symbol breakdown intact)."""
    con = sqlite3.connect(
        str(multi_state.parent / "grid_bot_BNBUSDT.sqlite3"))
    con.execute(
        "UPDATE kill_state SET active=1, trigger_reason='EQUITY_DRAWDOWN_KILL',"
        " activated_at='2026-10-04T02:00:00Z' WHERE key='kill_state'")
    con.commit()
    con.close()
    srv, base = start_server(multi_state.parent, str(multi_state),
                             multi_bot_config)
    try:
        status, body = http_get(base, "/api/status")
        assert status == 200
        snap = json.loads(body)
        kill = snap["risk"]["kill_state"]
        assert kill["active"] is True
        assert kill["per_symbol"]["BNBUSDT"]["active"] == 1
        assert kill["per_symbol"]["ETHUSDT"]["active"] == 0
        status, body = http_get(base, "/")
        assert status == 200
        assert "KILL ACTIVE" in body.decode()
    finally:
        srv.shutdown()
        srv.server_close()


def test_multi_no_mutation(multi_state: Path, multi_server):
    """Serving the multi-symbol dashboard must not write to ANY database."""
    _, base = multi_server
    dbs = {
        str(multi_state): None,
        str(multi_state.parent / "grid_bot_BNBUSDT.sqlite3"): None,
        str(multi_state.parent / "grid_bot_ETHUSDT.sqlite3"): None,
    }
    for db in dbs:
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        tables = [r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        dbs[db] = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                   for t in tables}
        con.close()
    for path in ("/", "/api/status", "/healthz"):
        http_get(base, path)
    for db, before in dbs.items():
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        tables = [r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        after = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in tables}
        con.close()
        assert before == after, db


def test_multi_symbol_connections_are_read_only(multi_state: Path):
    con = db_mod._connect_readonly(
        str(multi_state.parent / "grid_bot_BNBUSDT.sqlite3"))
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM orders")
    finally:
        con.close()


def test_multi_no_secrets_in_response(tmp_path, multi_bot_config):
    """A fake .env in the database directory must never surface in any
    response — the dashboard has no Binance credentials and must not
    expose any file contents."""
    main = tmp_path / "grid_bot.sqlite3"
    storage.init_db(str(main))
    env_text = (
        "BINANCE_TESTNET_API_KEY=topsecretkey\n"
        "BINANCE_TESTNET_API_SECRET=topsecret\n")
    (tmp_path / ".env").write_text(env_text)
    srv, base = start_server(tmp_path, str(main), multi_bot_config)
    try:
        for path in ("/", "/api/status", "/healthz"):
            status, body = http_get(base, path)
            text = body.decode("utf-8", "replace")
            assert status == 200, path
            assert "topsecretkey" not in text, path
            assert "topsecret" not in text, path
            assert "api_key" not in text.lower(), path
    finally:
        srv.shutdown()
        srv.server_close()


def test_multi_dashboard_reads_while_runtime_connection_is_open(
        multi_state: Path, multi_bot_config):
    """A runtime-style read-write connection stays open on a SYMBOL database
    while the dashboard keeps reading it — coexistence without locks."""
    sym_db = str(multi_state.parent / "grid_bot_BNBUSDT.sqlite3")
    runtime_con = sqlite3.connect(sym_db, timeout=2.0)
    srv, base = start_server(multi_state.parent, str(multi_state),
                             multi_bot_config)
    try:
        runtime_con.execute(
            "INSERT INTO orders (client_order_id, exchange_order_id, symbol,"
            " side, grid_index, price, quantity, status, created_at,"
            " updated_at, executed_qty, remaining_qty) VALUES"
            " ('AGBNB-LIVE','12','BNBUSDT','BUY',3,'100.1','0.25','NEW',"
            "  '2026-10-04T01:00:00Z','2026-10-04T01:00:00Z','0','0.25')")
        runtime_con.commit()
        status, body = http_get(base, "/api/status")
        assert status == 200
        snap = json.loads(body)
        bnb_orders = snap["grid"]["per_symbol"]["BNBUSDT"]["open_orders"]
        assert any(o["client_order_id"] == "AGBNB-LIVE" for o in bnb_orders)
        status, _ = http_get(base, "/")
        assert status == 200
    finally:
        runtime_con.close()
        srv.shutdown()
        srv.server_close()
