"""State tests: persistence, restart recovery, orders, fills, PnL, fees,
cooldown and kill state."""

from __future__ import annotations

import sqlite3

import pytest

from state import StateStore


@pytest.fixture
def store(tmp_path):
    s = StateStore(str(tmp_path / "state.db"))
    s.ensure_symbols(["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"])
    return s


def test_ensure_symbols_creates_rows_with_waiting_state(store):
    st = store.get_symbol("BTC/USDT")
    assert st is not None
    assert st.strategy_state == "WAITING"
    assert store.all_symbols()  # four configured symbols
    assert len(store.all_symbols()) == 4


def test_symbol_state_update_roundtrip(store):
    store.update_symbol(
        "BTC/USDT",
        timeframe="4h",
        last_price=50000.0,
        adx=15.0,
        rsi=30.0,
        percent_b=-0.1,
        volume_osc=0.2,
        zscore=0.5,
        atr=350.0,
        strategy_state="ACTIVE",
        grid_mode="arithmetic",
        grid_step=350.0,
        grid_lower=48250.0,
        gross_pct=0.0070,
        net_pct=0.0040,
    )
    st = store.get_symbol("BTC/USDT")
    assert st.strategy_state == "ACTIVE"
    assert st.last_price == pytest.approx(50000.0)
    assert st.adx == pytest.approx(15.0)
    assert st.percent_b == pytest.approx(-0.1)
    assert st.grid_lower == pytest.approx(48250.0)
    assert st.net_pct == pytest.approx(0.0040)


def test_unknown_update_fields_are_ignored(store):
    store.update_symbol("BTC/USDT", not_a_column=1)
    assert store.get_symbol("BTC/USDT").strategy_state == "WAITING"


def test_orders_lifecycle(store):
    oid = store.create_order("cid-1", "BTC/USDT", "BUY", "LIMIT_MAKER", 49650.0, 0.001, "dry_run")
    assert store.count_open_orders("BTC/USDT") == 1
    order = store.get_order(oid)
    assert order["client_order_id"] == "cid-1"
    assert order["status"] == "NEW"
    assert order["target_sell_price"] is None

    store.update_order_status(oid, "FILLED", 0.001)
    assert store.count_open_orders("BTC/USDT") == 0
    assert store.get_order(oid)["status"] == "FILLED"


def test_duplicate_client_order_id_rejected(store):
    store.create_order("cid-dup", "BTC/USDT", "BUY", "LIMIT_MAKER", 1.0, 1.0, "dry_run")
    with pytest.raises(sqlite3.IntegrityError):
        store.create_order("cid-dup", "ETH/USDT", "BUY", "LIMIT_MAKER", 1.0, 1.0, "dry_run")


def test_open_orders_filtering(store):
    b = store.create_order("cid-b", "BTC/USDT", "BUY", "LIMIT_MAKER", 1.0, 1.0, "dry_run")
    store.create_order("cid-e", "ETH/USDT", "BUY", "LIMIT_MAKER", 1.0, 1.0, "dry_run")
    assert store.count_open_orders() == 2
    assert len(store.open_orders("BTC/USDT")) == 1
    store.update_order_status(b, "CANCELED")
    assert store.count_open_orders() == 1
    assert store.open_orders("BTC/USDT") == []


def test_get_order_by_client_id(store):
    store.create_order("cid-x", "BTC/USDT", "BUY", "LIMIT_MAKER", 1.0, 1.0, "dry_run")
    order = store.get_order_by_client_id("cid-x")
    assert order is not None
    assert order["symbol"] == "BTC/USDT"
    assert store.get_order_by_client_id("missing") is None


def test_fills_pnl_and_fees_sums(store):
    b = store.create_order("cid-b2", "BTC/USDT", "BUY", "LIMIT_MAKER", 49650.0, 0.1, "dry_run")
    s = store.create_order("cid-s2", "BTC/USDT", "SELL", "LIMIT_MAKER", 50000.0, 0.1, "dry_run")
    store.record_fill(b, "BTC/USDT", "BUY", 49650.0, 0.1, 0.0104, trade_id="t-b1")
    store.record_fill(s, "BTC/USDT", "SELL", 50000.0, 0.1, 0.0105, trade_id="t-s1")
    e = store.create_order("cid-e2", "ETH/USDT", "SELL", "MARKET", 200.0, 0.05, "dry_run")
    store.record_fill(e, "ETH/USDT", "SELL", 200.0, 0.05, 0.01, trade_id="t-e1")
    # realized PnL is computed against average cost by the store itself
    assert store.sum_realized_pnl("BTC/USDT") == pytest.approx(0.1 * (50000.0 - 49650.0))
    assert store.sum_realized_pnl() == pytest.approx(0.1 * (50000.0 - 49650.0))
    # a SELL without held inventory realizes nothing
    assert store.get_symbol("ETH/USDT").inventory_qty == pytest.approx(0.0)
    assert store.sum_fees("BTC/USDT") == pytest.approx(0.0209)
    assert store.sum_fees() == pytest.approx(0.0309)
    assert store.count_completed_grids("BTC/USDT") == 1
    assert store.count_completed_grids("ETH/USDT") == 1


def test_record_fill_is_idempotent_by_trade_id(store):
    oid = store.create_order("cid-idem", "BTC/USDT", "BUY", "LIMIT_MAKER", 100.0, 2.0, "live")
    assert store.record_fill(oid, "BTC/USDT", "BUY", 100.0, 2.0, 0.002, trade_id="t-1") is True
    assert store.record_fill(oid, "BTC/USDT", "BUY", 100.0, 2.0, 0.002, trade_id="t-1") is False
    assert store.sum_fees("BTC/USDT") == pytest.approx(0.002)
    st = store.get_symbol("BTC/USDT")
    assert st.inventory_qty == pytest.approx(2.0)  # recorded exactly once
    assert st.avg_cost == pytest.approx(100.0)


def test_cooldown_persists_across_restart(store, tmp_path):
    store.set_cooldown("BTC/USDT", 1234567890.0)
    reopened = StateStore(str(tmp_path / "state.db"))
    assert reopened.get_symbol("BTC/USDT").cooldown_until == pytest.approx(1234567890.0)


def test_kill_state_persists_across_restart(store, tmp_path):
    store.set_global_kill("drawdown")
    reopened = StateStore(str(tmp_path / "state.db"))
    active, reason = reopened.global_kill()
    assert active is True
    assert reason == "drawdown"


def test_risk_events_persist(store, tmp_path):
    store.add_risk_event("BTC/USDT", "auto_exit", "rsi_overbought")
    reopened = StateStore(str(tmp_path / "state.db"))
    events = reopened.recent_risk_events()
    assert events[0]["event"] == "auto_exit"
    assert events[0]["details"] == "rsi_overbought"


def test_meta_and_equity_roundtrip(store):
    store.set_meta_float("equity", 12.34)
    store.set_meta_float("reference_equity", 20.0)
    store.set_runtime("RUNNING", 1000.0)
    assert store.get_meta_float("equity") == pytest.approx(12.34)
    assert store.get_meta_float("reference_equity") == pytest.approx(20.0)
    status, ts = store.last_runtime()
    assert status == "RUNNING"
    assert ts == pytest.approx(1000.0)


def test_restart_recovery_preserves_full_symbol_state(tmp_path):
    first = StateStore(str(tmp_path / "state.db"))
    first.ensure_symbols(["BTC/USDT"])
    first.set_symbol_state("BTC/USDT", "ACTIVE", last_price=50000.0, inventory_qty=0.5, avg_cost=49500.0)
    first.set_global_kill("drawdown")
    second = StateStore(str(tmp_path / "state.db"))
    st = second.get_symbol("BTC/USDT")
    assert st.strategy_state == "ACTIVE"
    assert st.inventory_qty == pytest.approx(0.5)
    assert st.avg_cost == pytest.approx(49500.0)
    assert second.global_kill()[0] is True


def test_database_status_ok(store):
    status = store.database_status()
    assert status["ok"] is True
    assert status["size_bytes"] > 0


def test_schema_migration_v1_to_v2(tmp_path):
    """A pre-existing v1 database (no child_sell_qty) migrates
    deterministically: column added, version stamped, data preserved."""
    import sqlite3

    from state import SCHEMA_VERSION

    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE symbols (
            symbol TEXT PRIMARY KEY, strategy_state TEXT DEFAULT 'WAITING',
            inventory_qty REAL DEFAULT 0, avg_cost REAL DEFAULT 0,
            risk_status TEXT DEFAULT 'ok', updated_at REAL DEFAULT 0
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_order_id TEXT UNIQUE, symbol TEXT, side TEXT, type TEXT,
            price REAL, qty REAL, filled_qty REAL DEFAULT 0,
            status TEXT DEFAULT 'NEW', mode TEXT, parent_order_id INTEGER,
            target_sell_price REAL, created_at REAL, updated_at REAL
        );
        CREATE TABLE fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER,
            symbol TEXT, side TEXT, price REAL, qty REAL, fee REAL DEFAULT 0,
            realized_pnl REAL DEFAULT 0, trade_id TEXT UNIQUE, ts REAL
        );
        CREATE TABLE risk_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, scope TEXT, event TEXT, details TEXT
        );
        INSERT INTO symbols(symbol, updated_at) VALUES('BTC/USDT', 1.0);
        """
    )
    conn.commit()
    conn.close()

    store = StateStore(path)  # triggers migration
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
    version = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()["value"]
    conn.close()
    assert "child_sell_qty" in cols
    assert int(version) == SCHEMA_VERSION
    # pre-existing data survives the migration
    assert store.get_symbol("BTC/USDT").strategy_state == "WAITING"
    # migrated store accepts child-sell tracking
    parent = store.create_order("cid-p", "BTC/USDT", "BUY", "LIMIT_MAKER", 1.0, 1.0, "dry_run")
    child = store.create_child_sell_order("cid-c", "BTC/USDT", 2.0, 1.0, parent, "dry_run")
    assert store.get_order(child)["parent_order_id"] == parent
    assert store.get_order(parent)["child_sell_qty"] == pytest.approx(1.0)


def test_fresh_database_is_stamped_with_current_schema_version(tmp_path):
    from state import SCHEMA_VERSION

    store = StateStore(str(tmp_path / "fresh.db"))
    assert store.get_meta("schema_version") == str(SCHEMA_VERSION)


def test_migration_is_idempotent_across_repeated_startups(tmp_path):
    """Opening an already-migrated database again and again must be a
    no-op: version stays, columns are not re-added, data intact."""
    from state import SCHEMA_VERSION

    path = str(tmp_path / "idem.db")
    first = StateStore(path)
    first.ensure_symbols(["BTC/USDT"])
    first.set_symbol_state("BTC/USDT", "ACTIVE", last_price=1.0)

    for _ in range(3):
        again = StateStore(path)
        assert again.get_meta("schema_version") == str(SCHEMA_VERSION)
        assert again.get_symbol("BTC/USDT").strategy_state == "ACTIVE"
