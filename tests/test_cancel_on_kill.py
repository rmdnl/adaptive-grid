"""F-H2: cancel-on-kill controller regression tests.

Drives ``CancelController`` over a real paper DB to prove the fail-closed
safety properties:

* confirmed cancels transition local orders to CANCELED and release the
  remaining reservation;
* UNKNOWN / FAILED cancels NEVER move local state, keep the reservation, and
  keep the kill latch active with ``PENDING_RECONCILIATION``;
* the kill state persists so a restart re-enters the kill branch;
* release is refused (fail-closed) while any open order is unreconciled;
* the cancel pass is idempotent and bounded-retry;
* the default paper canceler is CONFIRMED (deterministic, no live path);
* no replacement orders are ever placed.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from cancel_controller import (
    CancelController,
    CancelOutcome,
    ReleaseBlockedError,
)
from order_engine import OrderIntent, make_client_order_id
from risk_engine import RiskDecision
from storage import (
    get_cancel_record,
    get_kill_state,
    get_paper_reservation,
    init_db,
)
from symbol_rules import parse_symbol_info

DB = "grid.sqlite3"


def _symbol_info():
    return {
        "symbol": "BNBUSDT",
        "baseAsset": "BNB",
        "quoteAsset": "USDT",
        "status": "TRADING",
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "100000", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "10000", "stepSize": "0.001"},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "5000", "stepSize": "0.001"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
            {"filterType": "NOTIONAL", "minNotional": "10", "maxNotional": "100000"},
            {"filterType": "PERCENT_PRICE", "multiplierUp": "1.05", "multiplierDown": "0.95", "avgPriceMins": 5},
            {"filterType": "MAX_NUM_ORDERS", "maxNumOrders": 40},
        ],
    }


def _config(tmp_path):
    """Mirror the integration config (BNBUSDT paper account)."""
    return {
        "environment": {"mode": "testnet", "dry_run": True, "allow_live_execution": False},
        "symbols": "BNBUSDT",
        "timeframe": "15m",
        "grid": {
            "mode_by_symbol": {
                "BNBUSDT": "arithmetic"
            },
            "min_gross_profit_pct": Decimal("0.005"),
            "hard_min_net_pct": Decimal("0.002"),
            "preferred_net_max_pct": Decimal("0.004"),
            "min_cells": 6,
            "max_levels": 40,
        },
        "range": {
            "mode": "manual",
            "lower_price": Decimal("94"),
            "upper_price": Decimal("103"),
            "lookback": 200,
            "buffer_pct": Decimal("0.01"),
            "auto": {},
        },
        "strategy": {
            "entry": {
                "adx_max": Decimal("20"),
                "rsi_max": Decimal("35"),
                "bb_percent_b_max": Decimal("0"),
                "volume_oscillator_min": Decimal("0"),
            },
            "exit": {
                "rsi_min": Decimal("70"),
                "adx_min": Decimal("25"),
                "bb_percent_b_min": Decimal("1"),
                "zscore_threshold": Decimal("2.5"),
            },
            "cooldown_hours": 3,
        },
        "market_filter": {
            "adx_max": Decimal("28"),
            "atr_pct_max": Decimal("0.025"),
            "bb_width_max": Decimal("0.06"),
            "volume_spike_max": Decimal("2.5"),
        },
        "execution": {
            "prefer_limit_maker": True,
            "stale_order_minutes": 30,
            "max_open_orders": 40,
            "order_quote_size": Decimal("25"),
            "total_quote_budget": Decimal("0"),
            "max_inventory_pct": Decimal("0.70"),
        },
        "fees": {
            "maker_fee_fallback": Decimal("0.001"),
            "taker_fee_fallback": Decimal("0.001"),
            "slippage_roundtrip_pct": Decimal("0.0005"),
        },
        "paper": {
            "initial_base_balance": Decimal("2"),
            "initial_quote_balance": Decimal("1000"),
            "maker_fee": Decimal("0.001"),
            "taker_fee": Decimal("0.001"),
            "fee_asset": "USDT",
        },
        "risk": {
            "max_equity_drawdown_pct": Decimal("0.02"),
            "range_break_buffer_pct": Decimal("0.01"),
            "daily_profit_lock_pct": Decimal("0.01"),
            "cooldown_minutes": 30,
            "stop_if_below_lower_pct": Decimal("0.02"),
        },
        "logging": {
            "sqlite_path": str(tmp_path / DB),
            "log_path": str(tmp_path / "grid.log"),
            "csv_path": str(tmp_path / "trades.csv"),
        },
    }


def _open_order_intent(gen, index, side, price, created):
    cid = make_client_order_id("AG", "BNBUSDT", gen, index, side)
    return OrderIntent(
        client_order_id=cid,
        symbol="BNBUSDT",
        side=side,
        order_type="LIMIT_MAKER",
        price=Decimal(price),
        quantity=Decimal("0.01"),
        time_in_force="GTC",
        grid_index=index,
        generation=gen,
        created_at=created,
    )


def _submit_open_order(db, engine, gen, index, side, price, now):
    intent = _open_order_intent(gen, index, side, price, now)
    engine.submit(intent, RiskDecision(True), Decimal("94"), Decimal("103"))
    return intent


def _open_states(db):
    with sqlite3.connect(db) as con:
        return [r[0] for r in con.execute(
            "SELECT status FROM orders WHERE status IN ('OPEN','PARTIALLY_FILLED')"
        )]


# ---------------------------------------------------------------------------
# 1. Successful cancellation
# ---------------------------------------------------------------------------

def test_successful_cancel_transitions_orders_and_releases_reservations(tmp_path):
    db = str(tmp_path / DB)
    init_db(db)
    cfg = _config(tmp_path)
    controller = CancelController(db, cfg, parse_symbol_info(_symbol_info()))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    e = controller._engine
    i0 = _submit_open_order(db, e, 1, 0, "BUY", "98.0", now)
    i1 = _submit_open_order(db, e, 1, 1, "SELL", "99.0", now)
    assert _open_states(db) == ["OPEN", "OPEN"]

    report = controller.cancel_open_orders()
    assert report.overall_status == "CANCELLED"
    assert report.cancelled == 2
    assert report.unknown == 0 and report.failed == 0
    assert report.all_reconciled

    # Local orders are CANCELED, reservations fully released.
    assert "OPEN" not in _open_states(db) and "PARTIALLY_FILLED" not in _open_states(db)
    for intent in (i0, i1):
        order = e.get(intent.client_order_id)
        assert order.state.value == "CANCELED"
        assert get_paper_reservation(db, intent.client_order_id)["remaining_amount"] == Decimal("0")
    # Kill state latched, reconciled.
    latch = controller.latch_kill_state("EQUITY_DRAWDOWN_KILL", report)
    assert latch["active"] is True
    assert controller.is_kill_active()


def test_no_open_orders_is_all_reconciled(tmp_path):
    db = str(tmp_path / DB)
    init_db(db)
    controller = CancelController(db, _config(tmp_path), parse_symbol_info(_symbol_info()))
    report = controller.cancel_open_orders()
    assert report.overall_status == "NO_OPEN_ORDERS"
    assert report.considered == 0
    assert report.all_reconciled


# ---------------------------------------------------------------------------
# 2. Failed / unknown cancels keep the kill state active, fail closed
# ---------------------------------------------------------------------------

def test_failed_cancel_keeps_state_and_does_not_move_orders(tmp_path):
    db = str(tmp_path / DB)
    init_db(db)
    cfg = _config(tmp_path)
    controller = CancelController(
        db, cfg, parse_symbol_info(_symbol_info()),
        canceler=lambda order: CancelOutcome.FAILED,
    )
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    intent = _submit_open_order(db, controller._engine, 1, 0, "BUY", "98.0", now)

    report = controller.cancel_open_orders()
    assert report.overall_status == "PENDING_RECONCILIATION"
    assert report.failed == 1 and report.cancelled == 0
    # Order stays OPEN locally; reservation is retained.
    order = controller._engine.get(intent.client_order_id)
    assert order.state.value == "OPEN"
    assert get_paper_reservation(db, intent.client_order_id)["remaining_amount"] > Decimal("0")
    # Latch is active and pending.
    latch = controller.latch_kill_state("EQUITY_DRAWDOWN_KILL", report)
    assert latch["active"] is True
    assert latch["cancel_status"] in {"PENDING_RECONCILIATION", "FAILED"}
    # Release is refused while unreconciled.
    try:
        controller.release(reason="test", actor="op")
        raised = False
    except ReleaseBlockedError:
        raised = True
    assert raised


def test_unknown_cancel_is_fail_closed(tmp_path):
    db = str(tmp_path / DB)
    init_db(db)
    cfg = _config(tmp_path)
    controller = CancelController(
        db, cfg, parse_symbol_info(_symbol_info()),
        canceler=lambda order: CancelOutcome.UNKNOWN,
        max_attempts=3,
    )
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    intent = _submit_open_order(db, controller._engine, 1, 0, "BUY", "98.0", now)
    report = controller.cancel_open_orders()
    assert report.overall_status == "PENDING_RECONCILIATION"
    assert report.unknown == 1 and report.cancelled == 0
    # Unknown => bounded retry: with max_attempts=3 the canceler was called 3x.
    rec = get_cancel_record(db, intent.client_order_id)
    assert rec["attempts"] == 3
    assert rec["reconciled"] == 0
    # Order untouched locally.
    assert controller._engine.get(intent.client_order_id).state.value == "OPEN"


def test_failed_partial_then_operator_reconcile_allows_release(tmp_path):
    db = str(tmp_path / DB)
    init_db(db)
    cfg = _config(tmp_path)
    controller = CancelController(
        db, cfg, parse_symbol_info(_symbol_info()),
        canceler=lambda order: CancelOutcome.FAILED,
    )
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    intent = _submit_open_order(db, controller._engine, 1, 0, "BUY", "98.0", now)
    controller.cancel_open_orders()
    controller.latch_kill_state("EQUITY_DRAWDOWN_KILL", controller.cancel_open_orders())

    # Operator confirms on the exchange that the order is gone.
    res = controller.reconcile_cancelled(intent.client_order_id, confirmed_canceled=True)
    assert res.outcome == "RECONCILED"
    assert controller._engine.get(intent.client_order_id).state.value == "CANCELED"
    assert get_paper_reservation(db, intent.client_order_id)["remaining_amount"] == Decimal("0")
    # Now the release is permitted.
    released = controller.release(reason="orders confirmed canceled", actor="op")
    assert released["active"] is False


def test_reconcile_refuses_unconfirmed_and_filled(tmp_path):
    db = str(tmp_path / DB)
    init_db(db)
    controller = CancelController(db, _config(tmp_path), parse_symbol_info(_symbol_info()))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    intent = _submit_open_order(db, controller._engine, 1, 0, "BUY", "98.0", now)
    # Without positive confirmation the reconcile is refused (fail-closed).
    try:
        controller.reconcile_cancelled(intent.client_order_id, confirmed_canceled=False)
        raised = False
    except Exception:
        raised = True
    assert raised
    # Unknown order is rejected.
    try:
        controller.reconcile_cancelled("AG-NOPE-G00001-00000-B")
        raised2 = False
    except Exception:
        raised2 = True
    assert raised2


# ---------------------------------------------------------------------------
# 3. Idempotency / retry / already-canceled
# ---------------------------------------------------------------------------

def test_repeated_cancel_pass_is_idempotent(tmp_path):
    db = str(tmp_path / DB)
    init_db(db)
    controller = CancelController(db, _config(tmp_path), parse_symbol_info(_symbol_info()))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    intent = _submit_open_order(db, controller._engine, 1, 0, "BUY", "98.0", now)
    r1 = controller.cancel_open_orders()
    assert r1.overall_status == "CANCELLED"
    # Second pass: the order is already CANCELED, no open orders remain.
    r2 = controller.cancel_open_orders()
    assert r2.overall_status == "NO_OPEN_ORDERS"
    # The order is still CANCELED; nothing moved twice, no error.
    assert controller._engine.get(intent.client_order_id).state.value == "CANCELED"


def test_already_canceled_outcome_is_reconciled(tmp_path):
    db = str(tmp_path / DB)
    init_db(db)
    controller = CancelController(
        db, _config(tmp_path), parse_symbol_info(_symbol_info()),
        canceler=lambda order: CancelOutcome.ALREADY_CANCELED,
    )
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    intent = _submit_open_order(db, controller._engine, 1, 0, "BUY", "98.0", now)
    report = controller.cancel_open_orders()
    assert report.already_canceled == 1
    assert report.all_reconciled
    assert controller._engine.get(intent.client_order_id).state.value == "CANCELED"


def test_retry_then_confirm_within_bounded_attempts(tmp_path):
    db = str(tmp_path / DB)
    init_db(db)
    calls = {"n": 0}

    def flaky(order):
        calls["n"] += 1
        # Fail the first two attempts, confirm on the third.
        return CancelOutcome.FAILED if calls["n"] <= 2 else CancelOutcome.CONFIRMED

    controller = CancelController(
        db, _config(tmp_path), parse_symbol_info(_symbol_info()),
        canceler=flaky, max_attempts=3,
    )
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    intent = _submit_open_order(db, controller._engine, 1, 0, "BUY", "98.0", now)
    report = controller.cancel_open_orders()
    assert report.overall_status == "CANCELLED"
    assert controller._engine.get(intent.client_order_id).state.value == "CANCELED"
    assert calls["n"] == 3  # bounded to max_attempts


def test_malformed_canceler_result_is_unknown(tmp_path):
    db = str(tmp_path / DB)
    init_db(db)
    controller = CancelController(
        db, _config(tmp_path), parse_symbol_info(_symbol_info()),
        canceler=lambda order: 12345,  # not a CancelOutcome -> UNKNOWN
        max_attempts=1,
    )
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    intent = _submit_open_order(db, controller._engine, 1, 0, "BUY", "98.0", now)
    report = controller.cancel_open_orders()
    assert report.unknown == 1 and report.cancelled == 0
    assert controller._engine.get(intent.client_order_id).state.value == "OPEN"


# ---------------------------------------------------------------------------
# 4. Kill-state persistence / restart safety / no new orders
# ---------------------------------------------------------------------------

def test_kill_state_persists_and_blocks_restart(tmp_path):
    db = str(tmp_path / DB)
    init_db(db)
    controller = CancelController(db, _config(tmp_path), parse_symbol_info(_symbol_info()))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _submit_open_order(db, controller._engine, 1, 0, "BUY", "98.0", now)
    controller.latch_kill_state("EQUITY_DRAWDOWN_KILL", controller.cancel_open_orders())

    # Simulate a fresh process: a brand-new controller sees the latch.
    controller2 = CancelController(db, _config(tmp_path), parse_symbol_info(_symbol_info()))
    assert controller2.is_kill_active() is True
    assert get_kill_state(db)["active"] is True
    # No replacement orders may be placed while the latch is active: the
    # engine still refuses a RISK-VETOED submit (the risk path is the gate),
    # and the controller itself exposes no submit path.
    rejected = False
    try:
        controller2._engine.submit(
            _open_order_intent(2, 0, "BUY", "98.0", now),
            RiskDecision(False, ("EQUITY_DRAWDOWN_KILL",)),
            Decimal("94"), Decimal("103"),
        )
    except Exception:
        rejected = True
    assert rejected
    assert controller2._engine.get(
        make_client_order_id("AG", "BNBUSDT", 2, 0, "BUY")
    ) is None


def test_release_preserves_audit_trail(tmp_path):
    db = str(tmp_path / DB)
    init_db(db)
    controller = CancelController(db, _config(tmp_path), parse_symbol_info(_symbol_info()))
    report = controller.cancel_open_orders()
    controller.latch_kill_state("RANGE_BREAK_BELOW_BUFFER", report)
    controller.release(reason="operator verified no open orders", actor="operator")
    with sqlite3.connect(db) as con:
        rows = con.execute(
            "SELECT action, previous_active, new_active FROM kill_state_audits ORDER BY id"
        ).fetchall()
    actions = [r[0] for r in rows]
    assert "ACTIVATE" in actions
    assert "RELEASE" in actions
    # The release row records the transition off.
    release = [r for r in rows if r[0] == "RELEASE"][0]
    assert release[1] == 1 and release[2] == 0


# ---------------------------------------------------------------------------
# 5. Crash safety: pre-latch persists the kill before the cancel pass
# ---------------------------------------------------------------------------

def test_pre_latch_survives_crash_before_cancel(tmp_path):
    """The kill latch is written BEFORE the cancel pass runs.

    Simulates a process that dies between ``pre_latch`` and
    ``cancel_open_orders``: the persisted kill state must still be active,
    so a restart re-enters the kill branch and re-attempts cancellation.
    """
    db = str(tmp_path / DB)
    init_db(db)
    controller = CancelController(db, _config(tmp_path), parse_symbol_info(_symbol_info()))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _submit_open_order(db, controller._engine, 1, 0, "BUY", "98.0", now)

    # Latch first — this is the crash-safe guarantee.
    controller.pre_latch("EQUITY_DRAWDOWN_KILL", actor="risk_engine")

    # Simulate the process dying before the cancel pass runs: a fresh
    # controller sees the latch active even though no cancel happened.
    fresh = CancelController(db, _config(tmp_path), parse_symbol_info(_symbol_info()))
    assert fresh.is_kill_active() is True
    latch = get_kill_state(db)
    assert latch["active"] is True
    assert "EQUITY_DRAWDOWN_KILL" in latch["trigger"]
    assert latch["cancel_status"] == "LATCHED"

    # The restart path re-attempts the cancel and marks progress.
    report = fresh.cancel_open_orders()
    fresh.latch_kill_state(latch["trigger"], report, actor="risk_engine")
    assert get_kill_state(db)["active"] is True
    assert get_kill_state(db)["cancel_status"] == "CANCELLED"


def test_pre_latch_on_already_active_preserves_progress(tmp_path):
    """Re-latching an active kill must not clobber the recorded progress."""
    db = str(tmp_path / DB)
    init_db(db)
    controller = CancelController(db, _config(tmp_path), parse_symbol_info(_symbol_info()))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _submit_open_order(db, controller._engine, 1, 0, "BUY", "98.0", now)
    report = controller.cancel_open_orders()
    controller.latch_kill_state("EQUITY_DRAWDOWN_KILL", report)
    assert get_kill_state(db)["cancel_status"] == "CANCELLED"

    # A second pre-latch (retry) keeps the progress; it must not reset to
    # LATCHED or drop the cancel outcome.
    controller.pre_latch("EQUITY_DRAWDOWN_KILL", actor="risk_engine")
    assert get_kill_state(db)["cancel_status"] == "CANCELLED"
    assert get_kill_state(db)["active"] is True

