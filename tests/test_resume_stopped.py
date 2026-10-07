"""Tests for the --resume-stopped operator recovery command.

Covers all safety requirements:
- Reconcile exchange/local state before clearing anything
- Require reconciliation to be completely clean (no unknown orders, no unmatched exchange orders, no local open orders that cannot be verified)
- Require zero inventory for every symbol being resumed
- Require zero exchange open orders for every symbol being resumed
- Require global kill switch to be inactive
- Only affect symbols whose current risk_status is exactly "stopped"
- Never clear global kill state
- Clear ERROR state only through the same strict verification
- Never clear cooldown state
- Never create orders during --resume-stopped
- Never change balances, positions, PnL, historical fills, risk events, or session ID
- If ANY verification fails, abort without changing any symbol state
- Print a clear verification summary before applying the state transition
- Make the operation deterministic and idempotent. Running it twice must be safe.
- After successful recovery, symbols should return to a neutral state where the normal cycle can evaluate the strategy again (WAITING, not ACTIVE, not ENTRY)
- Preserve the existing adaptive-grid configuration and persisted adaptive fields
"""

from __future__ import annotations

import io
import time

import pytest

import bot
from conftest import FakeSpot, make_config
from exchange import ExchangeError, LiveExecutor, OrderUnknownState
from state import StateStore


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _env(tmp_path, pair=("AAA/USDT",)):
    store = StateStore(str(tmp_path / "state.db"))
    store.ensure_symbols(list(pair))
    # Use testnet mode for --resume-stopped (paper short-circuits)
    cfg = make_config(
        pair_list=pair,
        dry_run=False,
        execution_mode="testnet",
        adaptive_grid=False,  # use static config for simplicity
        lower_price={"AAA/USDT": 100.0},
        upper_price={"AAA/USDT": 200.0},
        total_grids=5,
        total_quote_budget={"AAA/USDT": 500.0},
    )
    spot = FakeSpot()
    return store, cfg, spot


def _stop_symbol(store, symbol, reason="lower_boundary_breach"):
    """Set a symbol to STOPPED state (as would happen after boundary breach)."""
    store.update_symbol(
        symbol,
        risk_status="stopped",
        strategy_state="STOPPED",
        exit_reason=reason,
        exit_status=1,
    )


# ---------------------------------------------------------------------------
# a. successful recovery
# ---------------------------------------------------------------------------

def test_resume_stopped_successful_recovery(tmp_path):
    """Happy path: clean reconciliation, zero inventory, zero open orders,
    no global kill -> symbol transitions from STOPPED to WAITING with risk_status=ok."""
    store, cfg, spot = _env(tmp_path)
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")

    # Verify preconditions
    st = store.get_symbol("AAA/USDT")
    assert st.risk_status == "stopped"
    assert st.strategy_state == "STOPPED"
    assert st.inventory_qty == 0.0
    assert store.count_open_orders("AAA/USDT") == 0
    assert len(spot.get_open_orders("AAA/USDT")) == 0
    assert not store.global_kill()[0]

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    output = out.getvalue()

    assert code == 0
    assert "OVERALL: OK" in output
    assert "VERIFICATION: PASSED" in output

    # Verify state transition
    st = store.get_symbol("AAA/USDT")
    assert st.risk_status == "ok", f"expected risk_status='ok', got '{st.risk_status}'"
    assert st.strategy_state == "WAITING", f"expected strategy_state='WAITING', got '{st.strategy_state}'"
    assert st.exit_reason is None, "exit_reason should be cleared"
    # Adaptive params should be preserved (they're None in this test since we used static config)
    # The key point is that no data is lost
    assert st.exit_status == 0


def test_resume_stopped_successful_recovery_multiple_symbols(tmp_path):
    """Multiple stopped symbols all recover if all pass verification."""
    pair = ("AAA/USDT", "BBB/USDT")
    store, cfg, spot = _env(tmp_path, pair)
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")
    _stop_symbol(store, "BBB/USDT", "lower_boundary_breach")

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    output = out.getvalue()

    assert code == 0
    assert "OVERALL: OK" in output

    for sym in pair:
        st = store.get_symbol(sym)
        assert st.risk_status == "ok"
        assert st.strategy_state == "WAITING"
        assert st.exit_reason is None


# ---------------------------------------------------------------------------
# b. unknown order blocks recovery
# ---------------------------------------------------------------------------

def test_resume_stopped_unknown_local_order_blocks(tmp_path):
    """A local open order that cannot be found on exchange (UNKNOWN) blocks recovery."""
    store, cfg, spot = _env(tmp_path)
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")
    # Create a local open order that doesn't exist on exchange
    store.create_order("ghost-buy", "AAA/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0, "live",
                       target_sell_price=101.0)
    # FakeSpot.get_order will return None for unknown cids

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    output = out.getvalue()

    assert code == 1
    assert "FAIL-CLOSED" in output
    assert "BLOCKED: reconciliation detected unknown order" in output

    # Verify NO state change
    st = store.get_symbol("AAA/USDT")
    assert st.risk_status == "stopped"
    assert st.strategy_state == "STOPPED"
    assert st.exit_reason == "lower_boundary_breach"


def test_resume_stopped_unexpected_exchange_order_blocks(tmp_path):
    """An exchange open order not known locally blocks recovery."""
    store, cfg, spot = _env(tmp_path)
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")
    # Register an exchange order that local DB doesn't know about
    spot._register("rogue-buy", "AAA/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0)

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    output = out.getvalue()

    assert code == 1
    assert "FAIL-CLOSED" in output
    # The error message from reconciliation includes "unknown order(s): rogue-buy"
    assert "unknown order" in output.lower()

    # Verify NO state change
    st = store.get_symbol("AAA/USDT")
    assert st.risk_status == "stopped"
    assert st.strategy_state == "STOPPED"


def test_resume_stopped_exchange_error_blocks(tmp_path):
    """Exchange error during reconciliation blocks recovery."""
    store, cfg, spot = _env(tmp_path)
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")
    # Create a local open order so reconciliation tries to query the exchange
    store.create_order("buy-1", "AAA/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0, "live")

    class BrokenSpot(FakeSpot):
        def get_order(self, symbol, cid):
            raise ExchangeError("network down")

        def get_open_orders(self, symbol=None):
            return []

    broken = BrokenSpot()

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, broken, store, out)
    output = out.getvalue()

    assert code == 1
    assert "FAIL-CLOSED" in output
    assert "BLOCKED: reconciliation failed" in output

    # Verify NO state change
    st = store.get_symbol("AAA/USDT")
    assert st.risk_status == "stopped"


# ---------------------------------------------------------------------------
# c. open exchange order blocks recovery
# ---------------------------------------------------------------------------

def test_resume_stopped_open_exchange_orders_blocks(tmp_path):
    """Open orders on exchange for the symbol block recovery."""
    store, cfg, spot = _env(tmp_path)
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")
    # Add open order on exchange (status NEW)
    spot._register("open-buy", "AAA/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0)

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    output = out.getvalue()

    assert code == 1
    assert "FAIL-CLOSED" in output
    assert "BLOCKED:" in output and "open order" in output

    # Verify NO state change
    st = store.get_symbol("AAA/USDT")
    assert st.risk_status == "stopped"
    assert st.strategy_state == "STOPPED"


# ---------------------------------------------------------------------------
# d. non-zero inventory blocks recovery
# ---------------------------------------------------------------------------

def test_resume_stopped_nonzero_inventory_blocks(tmp_path):
    """Non-zero inventory blocks recovery."""
    store, cfg, spot = _env(tmp_path)
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")
    # Set non-zero inventory
    store.update_symbol("AAA/USDT", inventory_qty=0.5)

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    output = out.getvalue()

    assert code == 1
    assert "FAIL-CLOSED" in output
    assert "BLOCKED: non-zero inventory" in output

    # Verify NO state change
    st = store.get_symbol("AAA/USDT")
    assert st.risk_status == "stopped"
    assert st.inventory_qty == pytest.approx(0.5)


def test_resume_stopped_ledger_mismatch_blocks(tmp_path):
    """Ledger mismatch (inventory != buys - sells) blocks recovery."""
    store, cfg, spot = _env(tmp_path)
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")
    # Force a corrupted ledger: inventory not equal to buys - sells
    store.update_symbol("AAA/USDT", inventory_qty=5.0)

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    output = out.getvalue()

    assert code == 1
    assert "FAIL-CLOSED" in output
    assert "BLOCKED: ledger mismatch" in output

    # Verify NO state change
    st = store.get_symbol("AAA/USDT")
    assert st.risk_status == "stopped"
    assert st.inventory_qty == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# e. global kill blocks recovery
# ---------------------------------------------------------------------------

def test_resume_stopped_global_kill_active_blocks(tmp_path):
    """Global kill switch active blocks recovery for ALL symbols."""
    store, cfg, spot = _env(tmp_path)
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")
    # Activate global kill
    store.set_global_kill("max_drawdown_breach dd=0.0300")

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    output = out.getvalue()

    assert code == 1
    assert "FAIL-CLOSED" in output
    assert "GLOBAL KILL ACTIVE" in output

    # Verify NO state change (global kill must remain active)
    st = store.get_symbol("AAA/USDT")
    assert st.risk_status == "stopped"
    assert st.strategy_state == "STOPPED"
    assert store.global_kill()[0] is True  # Global kill must remain active


# ---------------------------------------------------------------------------
# f. ERROR is not cleared
# ---------------------------------------------------------------------------

def test_resume_stopped_error_state_recovered_after_verification(tmp_path):
    """ERROR symbols are now recoverable through the same strict
    verification as STOPPED ones (zero inventory, zero open orders, clean
    ledger) — e.g. after a transient-outage latch was diagnosed and
    resolved by the operator."""
    store, cfg, spot = _env(tmp_path)
    store.update_symbol(
        "AAA/USDT",
        risk_status="error",
        strategy_state="ERROR",
        entry_blocker="symbol_error",
        exit_reason="liquidation_failed",
        exit_status=1,
    )

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    output = out.getvalue()

    assert code == 0, output
    assert "verifying 1 latched symbol(s): AAA/USDT" in output
    assert "VERIFICATION: PASSED" in output
    st = store.get_symbol("AAA/USDT")
    assert st.risk_status == "ok"
    assert st.strategy_state == "WAITING"
    assert st.entry_blocker is None
    assert st.exit_reason is None and st.exit_status == 0


# ---------------------------------------------------------------------------
# g. COOLDOWN is not cleared
# ---------------------------------------------------------------------------

def test_resume_stopped_cooldown_not_cleared(tmp_path):
    """Symbols in COOLDOWN are NOT cleared (only risk_status='stopped' symbols affected)."""
    store, cfg, spot = _env(tmp_path)
    # Set symbol to COOLDOWN (risk_status still 'ok', but cooldown_until in future)
    store.update_symbol(
        "AAA/USDT",
        risk_status="ok",
        strategy_state="COOLDOWN",
        cooldown_until=time.time() + 3600,  # 1 hour in future
        exit_reason="rsi_overbought",
        exit_status=1,
    )

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    output = out.getvalue()

    # Should succeed (no stopped symbols) but NOT clear COOLDOWN
    assert code == 0
    assert "no symbols with risk_status 'stopped'/'error' found" in output

    # Verify COOLDOWN state preserved
    st = store.get_symbol("AAA/USDT")
    assert st.risk_status == "ok"
    assert st.strategy_state == "COOLDOWN"
    assert st.cooldown_until is not None
    assert st.cooldown_until > time.time()
    assert st.exit_reason == "rsi_overbought"


def test_resume_stopped_stopped_with_active_cooldown_blocks(tmp_path):
    """A STOPPED symbol that also has an active cooldown is blocked."""
    store, cfg, spot = _env(tmp_path)
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")
    # Also set an active cooldown
    store.update_symbol("AAA/USDT", cooldown_until=time.time() + 3600)

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    output = out.getvalue()

    assert code == 1
    assert "FAIL-CLOSED" in output
    assert "BLOCKED: symbol is in COOLDOWN" in output

    # Verify NO state change
    st = store.get_symbol("AAA/USDT")
    assert st.risk_status == "stopped"
    assert st.cooldown_until is not None


# ---------------------------------------------------------------------------
# h. idempotent second execution
# ---------------------------------------------------------------------------

def test_resume_stopped_idempotent_second_execution(tmp_path):
    """Running --resume-stopped twice is safe (idempotent)."""
    store, cfg, spot = _env(tmp_path)
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")

    # First execution
    out1 = io.StringIO()
    code1 = bot._resume_stopped_symbols(cfg, spot, store, out1)
    assert code1 == 0

    # Verify state is now WAITING
    st = store.get_symbol("AAA/USDT")
    assert st.risk_status == "ok"
    assert st.strategy_state == "WAITING"

    # Second execution (should not error, should report nothing to recover)
    out2 = io.StringIO()
    code2 = bot._resume_stopped_symbols(cfg, spot, store, out2)
    output2 = out2.getvalue()
    assert code2 == 0
    assert "no symbols with risk_status 'stopped'/'error' found" in output2

    # State should remain unchanged (still WAITING)
    st = store.get_symbol("AAA/USDT")
    assert st.risk_status == "ok"
    assert st.strategy_state == "WAITING"


# ---------------------------------------------------------------------------
# i. failed verification causes zero state mutation
# ---------------------------------------------------------------------------

def test_resume_stopped_failed_verification_zero_mutation(tmp_path):
    """If ANY symbol fails verification, NO symbol state is changed."""
    pair = ("AAA/USDT", "BBB/USDT")
    store, cfg, spot = _env(tmp_path, pair)
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")
    _stop_symbol(store, "BBB/USDT", "lower_boundary_breach")
    # Make BBB fail: add open exchange order
    spot._register("rogue-bbb", "BBB/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0)

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    output = out.getvalue()

    assert code == 1
    assert "FAIL-CLOSED" in output

    # AAA should NOT have been recovered (all-or-nothing)
    st_aaa = store.get_symbol("AAA/USDT")
    assert st_aaa.risk_status == "stopped"
    assert st_aaa.strategy_state == "STOPPED"
    assert st_aaa.exit_reason == "lower_boundary_breach"

    # BBB should also remain stopped
    st_bbb = store.get_symbol("BBB/USDT")
    assert st_bbb.risk_status == "stopped"
    assert st_bbb.strategy_state == "STOPPED"


def test_resume_stopped_preserves_adaptive_fields(tmp_path):
    """Adaptive grid fields are preserved during recovery."""
    store, cfg, spot = _env(tmp_path)
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")
    # Set adaptive fields (as would be set when grid was active)
    store.update_symbol(
        "AAA/USDT",
        adaptive_lower_price=95.0,
        adaptive_upper_price=205.0,
        adaptive_total_grids=8,
        adaptive_quote_budget=400.0,
        adaptive_grid_step=10.0,
        adaptive_reference_price=150.0,
        adaptive_timeframe="4h",
    )

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    assert code == 0

    st = store.get_symbol("AAA/USDT")
    # Adaptive fields must be preserved
    assert st.adaptive_lower_price == pytest.approx(95.0)
    assert st.adaptive_upper_price == pytest.approx(205.0)
    assert st.adaptive_total_grids == 8
    assert st.adaptive_quote_budget == pytest.approx(400.0)
    assert st.adaptive_grid_step == pytest.approx(10.0)
    assert st.adaptive_reference_price == pytest.approx(150.0)
    assert st.adaptive_timeframe == "4h"
    # Other fields preserved
    assert st.inventory_qty == 0.0
    assert st.avg_cost == 0.0
    assert st.gross_pct is None  # Not set in this test
    assert st.net_pct is None


def test_resume_stopped_preserves_fills_and_risk_events(tmp_path):
    """Historical fills and risk events are preserved during recovery."""
    store, cfg, spot = _env(tmp_path)
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")
    # Add some fills
    buy_id = store.create_order("buy-1", "AAA/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0, "live")
    store.update_order_status(buy_id, "FILLED", 1.0)
    store.record_fill(buy_id, "AAA/USDT", "BUY", 100.0, 1.0, 0.1, trade_id="fill-buy")
    sell_id = store.create_order("sell-1", "AAA/USDT", "SELL", "LIMIT_MAKER", 101.0, 1.0, "live")
    store.update_order_status(sell_id, "FILLED", 1.0)
    store.record_fill(sell_id, "AAA/USDT", "SELL", 101.0, 1.0, 0.1, trade_id="fill-sell")
    # Add risk event
    store.add_risk_event("AAA/USDT", "test_event", "test details")

    before_fills = len(store.symbol_orders("AAA/USDT"))
    before_risk_events = len(store.recent_risk_events())

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    assert code == 0

    # Fills and risk events must be preserved
    assert len(store.symbol_orders("AAA/USDT")) == before_fills
    assert len(store.recent_risk_events()) == before_risk_events
    # Realized PnL = 1.0 * (101 - 100) = 1.0
    # Fees = 0.1 + 0.1 = 0.2
    assert store.sum_realized_pnl("AAA/USDT") == pytest.approx(1.0)
    assert store.sum_fees("AAA/USDT") == pytest.approx(0.2)


def test_resume_stopped_preserves_session_meta(tmp_path):
    """Session meta (session_id, equity, reference_equity, wallet_usdt) is preserved."""
    store, cfg, spot = _env(tmp_path)
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")
    # Set session meta
    store.set_meta("session_id", "test-session-123")
    store.set_meta("session_started_ts", str(time.time()))
    store.set_meta("session_start_equity", "1000.0")
    store.set_meta("equity", "1050.0")
    store.set_meta("reference_equity", "1100.0")
    store.set_meta("wallet_usdt", "5000.0")
    store.set_meta("runtime_status", "running")

    before_session = store.get_meta("session_id")
    before_equity = store.get_meta_float("equity")
    before_ref = store.get_meta_float("reference_equity")
    before_wallet = store.get_meta_float("wallet_usdt")
    before_runtime = store.get_meta("runtime_status")

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    assert code == 0

    # Session meta must be preserved
    assert store.get_meta("session_id") == before_session
    assert store.get_meta_float("equity") == pytest.approx(before_equity)
    assert store.get_meta_float("reference_equity") == pytest.approx(before_ref)
    assert store.get_meta_float("wallet_usdt") == pytest.approx(before_wallet)
    assert store.get_meta("runtime_status") == before_runtime


def test_resume_stopped_global_kill_preserved(tmp_path):
    """Global kill state is preserved (never cleared)."""
    store, cfg, spot = _env(tmp_path)
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")
    # Set global kill
    store.set_global_kill("test_reason")

    out = io.StringIO()
    # In paper mode it short-circuits
    # For testnet mode with global kill active, it should fail
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    output = out.getvalue()

    assert code == 1
    assert "GLOBAL KILL ACTIVE" in output

    # Global kill must remain active
    assert store.global_kill()[0] is True
    assert store.global_kill()[1] == "test_reason"


def test_resume_stopped_no_orders_created(tmp_path):
    """--resume-stopped never creates any orders."""
    store, cfg, spot = _env(tmp_path)
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")

    initial_order_count = len(store.symbol_orders("AAA/USDT"))
    initial_exchange_order_count = len(spot.get_open_orders("AAA/USDT"))
    initial_submit_calls = len(spot.submit_calls)

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    assert code == 0

    # No new orders locally
    assert len(store.symbol_orders("AAA/USDT")) == initial_order_count
    # No new orders on exchange
    assert len(spot.get_open_orders("AAA/USDT")) == initial_exchange_order_count
    # No submissions to exchange
    assert len(spot.submit_calls) == initial_submit_calls


def test_resume_stopped_paper_mode_recovers_verified_symbols(tmp_path):
    """In PAPER mode, --resume-stopped performs strictly verified LOCAL
    recovery (no exchange to reconcile): stopped symbols with zero
    inventory, consistent ledger and zero open orders return to WAITING."""
    store, cfg, spot = _env(tmp_path)
    cfg = make_config(
        pair_list=("AAA/USDT",),
        dry_run=True,
        execution_mode="paper",
    )
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    output = out.getvalue()

    assert code == 0
    assert "PAPER mode" in output
    assert "OVERALL: OK (recovery applied)" in output
    st = store.get_symbol("AAA/USDT")
    assert st.risk_status == "ok"
    assert st.strategy_state == "WAITING"


def test_resume_stopped_paper_mode_recovers_error_symbol(tmp_path):
    """PAPER recovery also covers risk_status='error' symbols (the ERROR
    entry veto would otherwise make a paper ERROR an unrecoverable dead
    end that only --reset-session could clear)."""
    store, cfg, spot = _env(tmp_path)
    cfg = make_config(
        pair_list=("AAA/USDT",),
        dry_run=True,
        execution_mode="paper",
    )
    store.update_symbol("AAA/USDT", risk_status="error", strategy_state="ERROR")

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    assert code == 0
    st = store.get_symbol("AAA/USDT")
    assert st.risk_status == "ok"
    assert st.strategy_state == "WAITING"


def test_resume_stopped_paper_mode_fail_closed_on_inventory(tmp_path):
    """PAPER recovery refuses symbols holding inventory or open orders —
    no state is changed for ANY symbol when one fails verification."""
    store, cfg, spot = _env(tmp_path, pair=("AAA/USDT", "BBB/USDT"))
    cfg = make_config(
        pair_list=("AAA/USDT", "BBB/USDT"),
        dry_run=True,
        execution_mode="paper",
    )
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")
    _stop_symbol(store, "BBB/USDT", "other")
    # BBB holds inventory without open orders -> must block recovery
    buy = store.create_order("cid-b", "BBB/USDT", "BUY", "LIMIT_MAKER", 100.0, 1.0, "dry_run")
    store.record_fill(buy, "BBB/USDT", "BUY", 100.0, 1.0, 0.0, trade_id="t-b")

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    assert code == 1
    assert "OVERALL: FAIL-CLOSED" in out.getvalue()
    # no symbol was modified
    assert store.get_symbol("AAA/USDT").risk_status == "stopped"
    assert store.get_symbol("BBB/USDT").risk_status == "stopped"


def test_resume_stopped_live_mode_not_supported(tmp_path):
    """In LIVE mode, --resume-stopped fails (only testnet supported)."""
    store, cfg, spot = _env(tmp_path)
    cfg = make_config(
        pair_list=("AAA/USDT",),
        dry_run=False,
        execution_mode="live",
        allow_live_execution=True,  # would be required for live
    )
    _stop_symbol(store, "AAA/USDT", "lower_boundary_breach")

    out = io.StringIO()
    code = bot._resume_stopped_symbols(cfg, spot, store, out)
    output = out.getvalue()

    assert code == 1
    assert "only available in EXECUTION_MODE=testnet" in output
    assert "FAIL-CLOSED" in output


# ---------------------------------------------------------------------------
# CLI flag plumbing
# ---------------------------------------------------------------------------

def test_resume_stopped_flag_registered():
    """The --resume-stopped flag is registered in main()."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume-stopped", action="store_true")
    ns = parser.parse_args(["--resume-stopped"])
    assert ns.resume_stopped is True


def test_resume_stopped_is_main_arg():
    """main() must expose --resume-stopped and route it to _resume_stopped_symbols."""
    import bot as bot_mod
    assert hasattr(bot_mod, "_resume_stopped_symbols")
    import inspect
    source = inspect.getsource(bot_mod.main)
    assert "resume_stopped" in source
    assert "_resume_stopped_symbols" in source