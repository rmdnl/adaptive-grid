"""Roadmap E: deterministic exchange-event applier regression tests.

Drives ``ExchangeEventApplier`` over a real paper DB (engine + controller)
with injected, deterministic exchange inputs.  Covers every required
scenario:

* duplicate events
* out-of-order events
* partial fills
* full fills
* cancel confirmation
* already-canceled orders
* unknown order state
* network failure
* stale local state
* restart recovery
* reconciliation convergence
* kill-state interaction
* no new orders while the kill latch is active
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from cancel_controller import CancelController
from exchange_events import (
    ExchangeEvent,
    ExchangeEventType,
    ExchangeEventApplier,
    RestReconciler,
)
from order_engine import (
    OrderIntent,
    OrderState,
    PaperOrderEngine,
    make_client_order_id,
)
from paper_accounting import PaperAccountingEngine
from risk_engine import RiskDecision
from storage import (
    get_exchange_event,
    get_kill_state,
    init_db,
)
from symbol_rules import parse_symbol_info
from tests.test_main_order_integration import _symbol_info

DB = "grid.sqlite3"


def _config(tmp_path):
    return {
        "environment": {"mode": "testnet", "dry_run": True, "allow_live_execution": False},
        "symbol": "BNBUSDT",
        "timeframe": "15m",
        "grid": {
            "step_pct": Decimal("0.006"),
            "hard_min_net_pct": Decimal("0.003"),
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
        },
        "logging": {
            "sqlite_path": str(tmp_path / DB),
            "log_path": str(tmp_path / "grid.log"),
            "csv_path": str(tmp_path / "trades.csv"),
        },
    }


def _make(tmp_path, reconciler=None):
    db = str(tmp_path / DB)
    init_db(db)
    cfg = _config(tmp_path)
    rules = parse_symbol_info(_symbol_info())
    accounting = PaperAccountingEngine(
        "BNB", "USDT", Decimal("2"), Decimal("1000"),
        Decimal("0.001"), Decimal("0.001"), "USDT",
    )
    engine = PaperOrderEngine(
        db,
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        accounting=accounting,
    )
    controller = CancelController(db, cfg, rules)
    applier = ExchangeEventApplier(
        db, engine, controller, scope="test",
        rest_reconciler=reconciler,
    )
    return db, engine, controller, applier, cfg


def _submit_open(engine, gen, index, side, price, quantity="0.02"):
    cid = make_client_order_id("AG", "BNBUSDT", gen, index, side)
    intent = OrderIntent(
        client_order_id=cid, symbol="BNBUSDT", side=side,
        order_type="LIMIT_MAKER", price=Decimal(price),
        quantity=Decimal(quantity), time_in_force="GTC",
        grid_index=index, generation=gen,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    engine.submit(intent, RiskDecision(True), Decimal("94"), Decimal("103"))
    return cid


# ---------------------------------------------------------------------------
# partial / full fills
# ---------------------------------------------------------------------------

def test_partial_fill_advances_watermark(tmp_path):
    db, engine, controller, applier, _ = _make(tmp_path)
    cid = _submit_open(engine, 1, 0, "BUY", "98.0")
    r = applier.apply(ExchangeEvent(
        event_id="ev-fill-1", event_type=ExchangeEventType.FILL,
        client_order_id=cid, exchange_order_id="ex-1", seq=0,
        raw={"quantity": "0.01", "price": "98.0"},
    ))
    assert r.applied is True
    assert r.ignore_reason is None
    assert engine.get(cid).state is OrderState.PARTIALLY_FILLED
    assert engine.get(cid).executed_qty == Decimal("0.01")
    assert applier.watermark == 0


def test_full_fill_transitions_to_filled(tmp_path):
    db, engine, controller, applier, _ = _make(tmp_path)
    cid = _submit_open(engine, 1, 0, "BUY", "98.0", quantity="0.01")
    r = applier.apply(ExchangeEvent(
        event_id="ev-fill-1", event_type=ExchangeEventType.FILL,
        client_order_id=cid, exchange_order_id="ex-1", seq=0,
        raw={"quantity": "0.01", "price": "98.0"},
    ))
    assert r.applied is True
    assert engine.get(cid).state is OrderState.FILLED


def test_fill_exceeding_remaining_quantity_is_rejected(tmp_path):
    db, engine, controller, applier, _ = _make(tmp_path)
    cid = _submit_open(engine, 1, 0, "BUY", "98.0", quantity="0.01")
    r = applier.apply(ExchangeEvent(
        event_id="ev-fill-1", event_type=ExchangeEventType.FILL,
        client_order_id=cid, exchange_order_id="ex-1", seq=0,
        raw={"quantity": "5.0", "price": "98.0"},  # exceeds remaining 0.01
    ))
    assert r.applied is False
    assert r.reconciliation_required is True
    # Local state must not move.
    assert engine.get(cid).state is OrderState.OPEN
    # The watermark must NOT advance past a failed apply.
    assert applier.watermark == 0


def test_fill_on_filled_order_is_no_op(tmp_path):
    db, engine, controller, applier, _ = _make(tmp_path)
    cid = _submit_open(engine, 1, 0, "BUY", "98.0", quantity="0.01")
    applier.apply(ExchangeEvent(
        event_id="ev-fill-1", event_type=ExchangeEventType.FILL,
        client_order_id=cid, exchange_order_id="ex-1", seq=0,
        raw={"quantity": "0.01", "price": "98.0"},
    ))
    r = applier.apply(ExchangeEvent(
        event_id="ev-fill-2", event_type=ExchangeEventType.FILL,
        client_order_id=cid, exchange_order_id="ex-2", seq=1,
        raw={"quantity": "0.01", "price": "98.0"},
    ))
    # A repeated fill on an already-FILLED order is a clean idempotent
    # no-op: recorded, not a contradiction, no reconciliation required.
    assert r.applied is True
    assert r.ignore_reason is None
    assert r.reconciliation_required is False
    assert engine.get(cid).state is OrderState.FILLED


# ---------------------------------------------------------------------------
# duplicate events
# ---------------------------------------------------------------------------

def test_duplicate_event_is_idempotent_no_op(tmp_path):
    db, engine, controller, applier, _ = _make(tmp_path)
    cid = _submit_open(engine, 1, 0, "BUY", "98.0", quantity="0.01")
    r1 = applier.apply(ExchangeEvent(
        event_id="ev-fill-1", event_type=ExchangeEventType.FILL,
        client_order_id=cid, exchange_order_id="ex-1", seq=0,
        raw={"quantity": "0.01", "price": "98.0"},
    ))
    assert r1.applied is True
    state_after_first = engine.get(cid).state
    executed_after_first = engine.get(cid).executed_qty
    r2 = applier.apply(ExchangeEvent(
        event_id="ev-fill-1", event_type=ExchangeEventType.FILL,
        client_order_id=cid, exchange_order_id="ex-1", seq=0,
        raw={"quantity": "0.01", "price": "98.0"},
    ))
    assert r2.applied is True
    assert r2.local_state_after is None
    # The replayed duplicate did not move the executed quantity a second time.
    assert engine.get(cid).state is state_after_first
    assert engine.get(cid).executed_qty == executed_after_first
    # The persisted record reflects the original applied outcome.
    stored = get_exchange_event(db, "ev-fill-1")
    assert stored["applied"] == 1


# ---------------------------------------------------------------------------
# out-of-order / sequence gap
# ---------------------------------------------------------------------------

def test_out_of_order_event_is_not_applied(tmp_path):
    db, engine, controller, applier, _ = _make(tmp_path)
    cid = _submit_open(engine, 1, 0, "BUY", "98.0")
    applier.apply(ExchangeEvent(
        event_id="ev-fill-1", event_type=ExchangeEventType.FILL,
        client_order_id=cid, exchange_order_id="ex-1", seq=5,
        raw={"quantity": "0.01", "price": "98.0"},
    ))
    assert applier.watermark == 5
    r = applier.apply(ExchangeEvent(
        event_id="ev-fill-0", event_type=ExchangeEventType.FILL,
        client_order_id=cid, exchange_order_id="ex-0", seq=3,
        raw={"quantity": "0.01", "price": "98.0"},
    ))
    assert r.applied is False
    assert r.ignore_reason == "OUT_OF_ORDER"
    assert r.reconciliation_required is True
    assert applier.watermark == 5  # not moved backwards


def test_sequence_gap_is_not_applied(tmp_path):
    db, engine, controller, applier, _ = _make(tmp_path)
    cid = _submit_open(engine, 1, 0, "BUY", "98.0")
    applier.apply(ExchangeEvent(
        event_id="ev-fill-1", event_type=ExchangeEventType.FILL,
        client_order_id=cid, exchange_order_id="ex-1", seq=1,
        raw={"quantity": "0.01", "price": "98.0"},
    ))
    assert applier.watermark == 1
    r = applier.apply(ExchangeEvent(
        event_id="ev-fill-4", event_type=ExchangeEventType.FILL,
        client_order_id=cid, exchange_order_id="ex-4", seq=4,
        raw={"quantity": "0.01", "price": "98.0"},
    ))
    assert r.applied is False
    assert r.ignore_reason == "SEQUENCE_GAP"
    assert r.reconciliation_required is True
    assert applier.watermark == 1


# ---------------------------------------------------------------------------
# cancel / reject / already-canceled
# ---------------------------------------------------------------------------

def test_cancel_confirmation_transitions_to_canceled(tmp_path):
    db, engine, controller, applier, _ = _make(tmp_path)
    cid = _submit_open(engine, 1, 0, "BUY", "98.0")
    r = applier.apply(ExchangeEvent(
        event_id="ev-cancel-1", event_type=ExchangeEventType.CANCELED,
        client_order_id=cid, exchange_order_id="ex-1", seq=0,
        raw={"reason": "cancel_ack"},
    ))
    assert r.applied is True
    assert engine.get(cid).state is OrderState.CANCELED


def test_already_canceled_order_is_no_op(tmp_path):
    db, engine, controller, applier, _ = _make(tmp_path)
    cid = _submit_open(engine, 1, 0, "BUY", "98.0")
    applier.apply(ExchangeEvent(
        event_id="ev-cancel-1", event_type=ExchangeEventType.CANCELED,
        client_order_id=cid, exchange_order_id="ex-1", seq=0,
        raw={},
    ))
    r = applier.apply(ExchangeEvent(
        event_id="ev-cancel-2", event_type=ExchangeEventType.CANCELED,
        client_order_id=cid, exchange_order_id="ex-2", seq=1,
        raw={},
    ))
    assert r.applied is True
    assert r.local_state_after == "CANCELED"
    assert engine.get(cid).state is OrderState.CANCELED


def test_reject_transitions_to_rejected(tmp_path):
    db, engine, controller, applier, _ = _make(tmp_path)
    cid = _submit_open(engine, 1, 0, "BUY", "98.0")
    r = applier.apply(ExchangeEvent(
        event_id="ev-reject-1", event_type=ExchangeEventType.REJECTED,
        client_order_id=cid, exchange_order_id="ex-1", seq=0,
        raw={"reason": "NOTIONAL"},
    ))
    assert r.applied is True
    assert engine.get(cid).state is OrderState.REJECTED


# ---------------------------------------------------------------------------
# unknown order state
# ---------------------------------------------------------------------------

def test_event_on_unknown_local_order_is_not_applied(tmp_path):
    db, engine, controller, applier, _ = _make(tmp_path)
    r = applier.apply(ExchangeEvent(
        event_id="ev-x-1", event_type=ExchangeEventType.FILL,
        client_order_id="AG-BNBUSDT-G00099-00099-B", exchange_order_id="ex-x",
        seq=0, raw={"quantity": "0.01", "price": "98.0"},
    ))
    assert r.applied is False
    assert r.ignore_reason == "UNKNOWN_ORDER"
    assert r.reconciliation_required is True
    assert applier.watermark == 0


def test_unknown_event_kind_is_not_applied(tmp_path):
    db, engine, controller, applier, _ = _make(tmp_path)
    cid = _submit_open(engine, 1, 0, "BUY", "98.0")
    r = applier.apply(ExchangeEvent(
        event_id="ev-unk-1", event_type=ExchangeEventType.UNKNOWN,
        client_order_id=cid, exchange_order_id="ex-1", seq=0,
        raw={"kind": "some_future_event"},
    ))
    assert r.applied is False
    assert r.ignore_reason == "STALE_LOCAL_STATE"
    assert r.reconciliation_required is True


# ---------------------------------------------------------------------------
# stale local state
# ---------------------------------------------------------------------------

def test_cancel_on_fully_filled_order_flags_reconcile(tmp_path):
    """A cancel that cannot be transitioned (the order already FILLED) is
    not forced: it is recorded and flagged for reconciliation."""
    db, engine, controller, applier, _ = _make(tmp_path)
    cid = _submit_open(engine, 1, 0, "BUY", "98.0", quantity="0.01")
    applier.apply(ExchangeEvent(
        event_id="ev-fill-1", event_type=ExchangeEventType.FILL,
        client_order_id=cid, exchange_order_id="ex-1", seq=0,
        raw={"quantity": "0.01", "price": "98.0"},
    ))
    r = applier.apply(ExchangeEvent(
        event_id="ev-cancel-1", event_type=ExchangeEventType.CANCELED,
        client_order_id=cid, exchange_order_id="ex-1", seq=1,
        raw={},
    ))
    # CANCELED on a FILLED order is a terminal no-op, recorded as applied.
    assert r.applied is True
    assert r.local_state_after == "FILLED"
    assert engine.get(cid).state is OrderState.FILLED


# ---------------------------------------------------------------------------
# network failure (reconciliation without a REST source is fail-closed)
# ---------------------------------------------------------------------------

class _NoReconciler(RestReconciler):
    def snapshot(self, symbol):
        raise ConnectionError("network down")


def test_reconcile_without_reconciler_is_fail_closed(tmp_path):
    db, engine, controller, applier, _ = _make(tmp_path, reconciler=None)
    cid = _submit_open(engine, 1, 0, "BUY", "98.0")
    try:
        applier.reconcile("BNBUSDT")
        raised = False
    except Exception:
        raised = True
    assert raised
    # Local state unchanged.
    assert engine.get(cid).state is OrderState.OPEN


def test_reconcile_propagates_network_failure(tmp_path):
    db, engine, controller, applier, _ = _make(
        tmp_path, reconciler=_NoReconciler(),
    )
    cid = _submit_open(engine, 1, 0, "BUY", "98.0")
    try:
        applier.reconcile("BNBUSDT")
        raised = False
    except ConnectionError:
        raised = True
    assert raised
    assert engine.get(cid).state is OrderState.OPEN
    assert applier.watermark == 0  # watermark not re-baselined on failure


# ---------------------------------------------------------------------------
# restart recovery
# ---------------------------------------------------------------------------

def test_restart_recovery_resumes_watermark_and_dedup(tmp_path):
    db, engine, controller, applier, cfg = _make(tmp_path)
    cid = _submit_open(engine, 1, 0, "BUY", "98.0", quantity="0.01")
    applier.apply(ExchangeEvent(
        event_id="ev-fill-1", event_type=ExchangeEventType.FILL,
        client_order_id=cid, exchange_order_id="ex-1", seq=0,
        raw={"quantity": "0.01", "price": "98.0"},
    ))
    state_before = engine.get(cid).state
    wm_before = applier.watermark

    # Simulate a process restart: a fresh applier over the same DB.
    engine2 = PaperOrderEngine(
        db, clock=lambda: datetime(2026, 1, 2, tzinfo=timezone.utc),
        accounting=PaperAccountingEngine(
            "BNB", "USDT", Decimal("2"), Decimal("1000"),
            Decimal("0.001"), Decimal("0.001"), "USDT"),
    )
    controller2 = CancelController(db, cfg, parse_symbol_info(_symbol_info()))
    applier2 = ExchangeEventApplier(
        db, engine2, controller2, scope="test",
    )
    # Watermark and event log survive the restart.
    assert applier2.watermark == wm_before == 0
    assert get_exchange_event(db, "ev-fill-1")["applied"] == 1
    # A replayed duplicate after restart is an idempotent no-op.
    r = applier2.apply(ExchangeEvent(
        event_id="ev-fill-1", event_type=ExchangeEventType.FILL,
        client_order_id=cid, exchange_order_id="ex-1", seq=0,
        raw={"quantity": "0.01", "price": "98.0"},
    ))
    assert r.applied is True
    assert r.local_state_after is None
    assert engine2.get(cid).state is state_before


# ---------------------------------------------------------------------------
# reconciliation convergence
# ---------------------------------------------------------------------------

class _StaticSnapshotReconciler(RestReconciler):
    def __init__(self, orders):
        self._orders = orders
        self.calls = 0

    def snapshot(self, symbol):
        self.calls += 1
        return {"orders": self._orders}


def test_reconciliation_is_convergent(tmp_path):
    db, engine, controller, applier, _ = _make(tmp_path)
    cid = _submit_open(engine, 1, 0, "BUY", "98.0")
    # Exchange says the order is CANCELED (operator cancelled on exchange).
    reconciler = _StaticSnapshotReconciler([
        {"client_order_id": cid, "state": "CANCELED",
         "executed_qty": "0", "quantity": "0.02"},
    ])
    applier = ExchangeEventApplier(
        db, engine, controller, scope="test", rest_reconciler=reconciler,
    )
    out1 = applier.reconcile("BNBUSDT")
    state1 = engine.get(cid).state.value
    out2 = applier.reconcile("BNBUSDT")
    state2 = engine.get(cid).state.value
    assert state1 == state2 == "CANCELED"
    # Re-running reconciliation is idempotent (no further state change).
    assert out1["aligned"] == out2["aligned"]
    assert applier.watermark == 0  # re-baselined after reconciliation


def test_reconcile_flags_exchange_only_orders_for_review(tmp_path):
    db, engine, controller, applier, _ = _make(tmp_path)
    cid = _submit_open(engine, 1, 0, "BUY", "98.0")
    reconciler = _StaticSnapshotReconciler([
        {"client_order_id": cid, "state": "FILLED",
         "executed_qty": "0.02", "price": "98.0", "quantity": "0.02"},
        # An order the exchange knows about but local state does not:
        {"client_order_id": "AG-BNBUSDT-G00042-00042-B", "state": "OPEN",
         "executed_qty": "0", "quantity": "0.02"},
    ])
    applier = ExchangeEventApplier(
        db, engine, controller, scope="test", rest_reconciler=reconciler,
    )
    out = applier.reconcile("BNBUSDT")
    assert "AG-BNBUSDT-G00042-00042-B" in out["unknown_exchange_orders"]
    # Local order FILLED via the fill engine so accounting stays authoritative.
    assert engine.get(cid).state is OrderState.FILLED


# ---------------------------------------------------------------------------
# kill-state interaction + no new orders
# ---------------------------------------------------------------------------

def test_applier_never_places_orders(tmp_path):
    db, engine, controller, applier, _ = _make(tmp_path)
    try:
        applier.place_order("any", "thing")
        raised = False
    except Exception:
        raised = True
    assert raised
    # No orders appear from the applier alone.
    from storage import connect
    con = connect(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    finally:
        con.close()


def test_kill_latch_stays_active_through_cancel_events(tmp_path):
    db, engine, controller, applier, cfg = _make(tmp_path)
    cid = _submit_open(engine, 1, 0, "BUY", "98.0")
    # Latch the kill state (F-H2 path).
    controller.pre_latch("EQUITY_DRAWDOWN_KILL", actor="risk_engine")
    assert get_kill_state(db)["active"] is True

    # A cancel event during the active kill is recorded and applied locally;
    # it must NOT release the kill latch.
    r = applier.apply(ExchangeEvent(
        event_id="ev-cancel-1", event_type=ExchangeEventType.CANCELED,
        client_order_id=cid, exchange_order_id="ex-1", seq=0,
        raw={},
    ))
    assert r.applied is True
    assert engine.get(cid).state is OrderState.CANCELED
    assert get_kill_state(db)["active"] is True
    # The applier exposes no order placement, and no new orders exist.
    from storage import connect
    con = connect(db)
    try:
        orders = con.execute(
            "SELECT COUNT(*) FROM orders WHERE status IN ('OPEN','PARTIALLY_FILLED')"
        ).fetchone()[0]
    finally:
        con.close()
    assert orders == 0


def test_fill_event_while_kill_active_does_not_release_latch(tmp_path):
    db, engine, controller, applier, cfg = _make(tmp_path)
    cid = _submit_open(engine, 1, 0, "BUY", "98.0", quantity="0.01")
    controller.pre_latch("RANGE_BREAK_BELOW_BUFFER", actor="risk_engine")
    r = applier.apply(ExchangeEvent(
        event_id="ev-fill-1", event_type=ExchangeEventType.FILL,
        client_order_id=cid, exchange_order_id="ex-1", seq=0,
        raw={"quantity": "0.01", "price": "98.0"},
    ))
    assert r.applied is True
    assert engine.get(cid).state is OrderState.FILLED
    # Kill latch is untouched by fills.
    assert get_kill_state(db)["active"] is True
    assert get_kill_state(db)["trigger"] == "RANGE_BREAK_BELOW_BUFFER"
