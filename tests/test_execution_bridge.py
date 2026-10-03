"""Gated testnet execution bridge: specification and regression tests.

Covers the roadmap-B feature: risk-gated paper orders mirrored to real
Binance Spot TESTNET LIMIT_MAKER orders.

- double gate resolution (config AND env; fail-closed both ways);
- ledger state machine incl. crash-safe PENDING_PLACE;
- mirroring: ack paths (OPEN/FILLED/EXPIRED), deterministic rejection,
  UNKNOWN outcomes (never retried, never raised into the paper cycle),
  duplicate-cid idempotency;
- authoritative reconciliation (lost-ack recovery, partial-fill progress,
  query-unavailable stays UNKNOWN);
- cancel propagation (confirmed, ambiguous-resolved-by-requery,
  PENDING_PLACE never canceled blind);
- fail-closed unknown-remote-order guard;
- build_bridge_from_env with the real gate machinery;
- runtime wiring: mirror after submission, cancel on kill.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from types import SimpleNamespace

import pytest

import multi_symbol_main as msm
from execution_bridge import (
    ExecutionLedger,
    TestnetExecutionBridge,
    build_bridge_from_env,
    resolve_bridge_gate,
    STATE_CANCELED,
    STATE_EXPIRED,
    STATE_FILLED,
    STATE_OPEN,
    STATE_PENDING_PLACE,
    STATE_REJECTED,
    STATE_UNKNOWN,
)
from order_engine import make_client_order_id
from testnet_orders import (
    BinanceTestnetOrderRejectedError,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class _StubOrderClient:
    """Records placements/cancels; scripted outcomes per cid."""

    def __init__(self):
        self.placements = []
        self.cancels = []
        self.outcomes = {}  # cid -> "NEW"|"FILLED"|... | Exception

    def place_limit_maker_order(self, symbol, side, quantity, price, cid):
        self.placements.append((symbol, side, quantity, price, cid))
        outcome = self.outcomes.get(cid, "NEW")
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(
            symbol=symbol, order_id=4242, client_order_id=cid, side=side,
            order_type="LIMIT_MAKER", status=outcome, price=price,
            orig_qty=quantity, executed_qty=Decimal("0"),
            transact_time=None)

    def cancel_order_by_client_id(self, symbol, cid):
        self.cancels.append(cid)
        outcome = self.outcomes.get(f"cancel:{cid}", "CANCELED")
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(order_id=4242, client_order_id=cid,
                               status="CANCELED", executed_qty=Decimal("0"))


class _StubMarketClient:
    """Scripted get_order/open_orders payloads."""

    def __init__(self):
        self.order_status = {}   # cid -> payload dict or Exception
        self.open_orders_result = []

    def get_order(self, symbol, cid):
        payload = self.order_status.get(cid)
        if isinstance(payload, Exception):
            raise payload
        return payload

    def open_orders(self, symbol):
        return self.open_orders_result


def _intent(cid="AGBNB-BNBUSDT-G00001-00001-B", side="BUY",
            price=Decimal("100"), quantity=Decimal("0.25")):
    return SimpleNamespace(client_order_id=cid, symbol="BNBUSDT", side=side,
                           order_type="LIMIT_MAKER", price=price,
                           quantity=quantity, grid_index=1, generation=1,
                           time_in_force="GTC", created_at=None)


def _bridge(tmp_path, order_client=None, market_client=None):
    ledger = ExecutionLedger(str(tmp_path / "grid_BNBUSDT.sqlite3"))
    return TestnetExecutionBridge(
        order_client or _StubOrderClient(),
        market_client or _StubMarketClient(),
        ledger, "BNBUSDT")


def _order_payload(cid, status="NEW", executed="0", order_id=4242):
    return {"symbol": "BNBUSDT", "clientOrderId": cid, "status": status,
            "executedQty": executed, "orderId": order_id, "side": "BUY",
            "type": "LIMIT_MAKER", "price": "100", "origQty": "0.25"}


# ---------------------------------------------------------------------------
# Gate resolution
# ---------------------------------------------------------------------------

def test_gate_requires_both_flags():
    gate = resolve_bridge_gate({"execution": {"testnet_execution": True}},
                               orders_enabled_env=False)
    assert gate.enabled is False and "ENV_GATE_DISABLED" in gate.reasons[0]

    gate = resolve_bridge_gate({"execution": {"testnet_execution": False}},
                               orders_enabled_env=True)
    assert gate.enabled is False and "CONFIG_DISABLED" in gate.reasons[0]

    gate = resolve_bridge_gate({}, orders_enabled_env=False)
    assert gate.enabled is False and len(gate.reasons) == 2

    gate = resolve_bridge_gate({"execution": {"testnet_execution": True}},
                               orders_enabled_env=True)
    assert gate.enabled is True and gate.reasons == ()


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def test_ledger_records_pending_before_ack(tmp_path):
    ledger = ExecutionLedger(str(tmp_path / "db.sqlite3"))
    ledger.record_pending("cid-1", "BNBUSDT", "BUY", Decimal("100"),
                          Decimal("0.25"))
    row = ledger.get("cid-1")
    assert row["state"] == STATE_PENDING_PLACE
    assert row["price"] == "100" and row["quantity"] == "0.25"
    # Idempotent insert: a repeat PENDING_PLACE write does not duplicate.
    ledger.record_pending("cid-1", "BNBUSDT", "BUY", Decimal("100"),
                          Decimal("0.25"))
    assert len(ledger.all()) == 1


def test_ledger_state_transitions(tmp_path):
    ledger = ExecutionLedger(str(tmp_path / "db.sqlite3"))
    ledger.record_pending("cid-1", "BNBUSDT", "BUY", Decimal("100"),
                          Decimal("0.25"))
    ledger.record_ack("cid-1", STATE_OPEN, 4242, Decimal("0"))
    assert ledger.get("cid-1")["state"] == STATE_OPEN
    ledger.record_progress("cid-1", Decimal("0.1"), 4242)
    assert ledger.get("cid-1")["executed_qty"] == "0.1"
    ledger.record_ack("cid-1", STATE_FILLED, 4242, Decimal("0.25"))
    row = ledger.get("cid-1")
    assert row["state"] == STATE_FILLED
    assert ledger.orders_in_states(STATE_PENDING_PLACE, STATE_OPEN,
                                   STATE_UNKNOWN) == []


# ---------------------------------------------------------------------------
# Mirroring
# ---------------------------------------------------------------------------

def test_mirror_places_real_order_with_same_identity(tmp_path):
    client = _StubOrderClient()
    bridge = _bridge(tmp_path, order_client=client)
    result = bridge.mirror_order(_intent())
    assert result["mirrored"] is True and result["state"] == STATE_OPEN
    symbol, side, qty, price, cid = client.placements[0]
    assert (side, qty, price, cid) == ("BUY", Decimal("0.25"),
                                       Decimal("100"), _intent().client_order_id)
    assert bridge.ledger.get(_intent().client_order_id)["state"] == STATE_OPEN


def test_mirror_duplicate_cid_is_never_replaced(tmp_path):
    client = _StubOrderClient()
    bridge = _bridge(tmp_path, order_client=client)
    bridge.mirror_order(_intent())
    result = bridge.mirror_order(_intent())
    assert result["mirrored"] is False
    assert result["reason"] == "ALREADY_IN_LEDGER"
    assert len(client.placements) == 1


def test_mirror_deterministic_rejection_is_terminal(tmp_path):
    client = _StubOrderClient()
    client.outcomes["cid-X"] = BinanceTestnetOrderRejectedError(
        "would cross", code=-1013)
    bridge = _bridge(tmp_path, order_client=client)
    result = bridge.mirror_order(_intent(cid="cid-X"))
    assert result["state"] == STATE_REJECTED
    assert bridge.ledger.get("cid-X")["state"] == STATE_REJECTED


def test_mirror_unknown_outcome_never_raises(tmp_path):
    client = _StubOrderClient()
    client.outcomes["cid-X"] = BinanceTestnetOrderError_probe()
    bridge = _bridge(tmp_path, order_client=client)
    result = bridge.mirror_order(_intent(cid="cid-X"))
    assert result["state"] == STATE_UNKNOWN
    assert bridge.ledger.get("cid-X")["state"] == STATE_UNKNOWN


def BinanceTestnetOrderError_probe():
    from testnet_orders import BinanceTestnetOrderError
    return BinanceTestnetOrderError("network lost mid-POST")


def test_mirror_unexpected_exception_is_recorded_unknown(tmp_path):
    client = _StubOrderClient()
    client.outcomes["cid-X"] = RuntimeError("boom")
    bridge = _bridge(tmp_path, order_client=client)
    result = bridge.mirror_order(_intent(cid="cid-X"))
    assert result["state"] == STATE_UNKNOWN


def test_mirror_filled_and_expired_acks(tmp_path):
    client = _StubOrderClient()
    bridge = _bridge(tmp_path, order_client=client)
    bridge.mirror_order(_intent(cid="cid-A"))
    assert bridge.ledger.get("cid-A")["state"] == STATE_OPEN
    client.outcomes["cid-B"] = "FILLED"
    bridge.mirror_order(_intent(cid="cid-B"))
    assert bridge.ledger.get("cid-B")["state"] == STATE_FILLED
    client.outcomes["cid-C"] = "EXPIRED"
    bridge.mirror_order(_intent(cid="cid-C"))
    assert bridge.ledger.get("cid-C")["state"] == STATE_EXPIRED


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def test_reconcile_resolves_lost_place_ack(tmp_path):
    market = _StubMarketClient()
    bridge = _bridge(tmp_path, market_client=market)
    bridge.ledger.record_pending("cid-1", "BNBUSDT", "BUY", Decimal("100"),
                                 Decimal("0.25"))
    # No ack was ever received, but the order EXISTS on the exchange and
    # even filled: reconciliation must settle the ledger authoritatively.
    market.order_status["cid-1"] = _order_payload("cid-1", status="FILLED",
                                                  executed="0.25")
    report = bridge.reconcile()
    assert report["checked"] == 1
    assert report["resolved"][0]["state"] == STATE_FILLED
    assert bridge.ledger.get("cid-1")["state"] == STATE_FILLED


def test_reconcile_records_partial_progress(tmp_path):
    market = _StubMarketClient()
    bridge = _bridge(tmp_path, market_client=market)
    bridge.ledger.record_pending("cid-1", "BNBUSDT", "BUY", Decimal("100"),
                                 Decimal("0.25"))
    bridge.ledger.record_ack("cid-1", STATE_OPEN, 4242, Decimal("0"))
    market.order_status["cid-1"] = _order_payload("cid-1", status="NEW",
                                                  executed="0.1")
    report = bridge.reconcile()
    assert report["resolved"][0]["state"] == STATE_OPEN
    row = bridge.ledger.get("cid-1")
    assert row["state"] == STATE_OPEN
    assert row["executed_qty"] == "0.1"


def test_reconcile_query_unavailable_stays_unknown(tmp_path):
    market = _StubMarketClient()
    from binance_testnet import BinanceTestnetResponseError
    market.order_status["cid-1"] = BinanceTestnetResponseError("timeout")
    bridge = _bridge(tmp_path, market_client=market)
    bridge.ledger.record_pending("cid-1", "BNBUSDT", "BUY", Decimal("100"),
                                 Decimal("0.25"))
    report = bridge.reconcile()
    assert report["unknown"] and report["unknown"][0]["cid"] == "cid-1"
    assert bridge.ledger.get("cid-1")["state"] == STATE_PENDING_PLACE


def test_reconcile_canceled_remote_order(tmp_path):
    market = _StubMarketClient()
    bridge = _bridge(tmp_path, market_client=market)
    bridge.ledger.record_pending("cid-1", "BNBUSDT", "SELL", Decimal("101"),
                                 Decimal("0.25"))
    bridge.ledger.record_ack("cid-1", STATE_OPEN, 4242, Decimal("0"))
    market.order_status["cid-1"] = _order_payload("cid-1", status="CANCELED")
    report = bridge.reconcile()
    assert bridge.ledger.get("cid-1")["state"] == STATE_CANCELED
    assert report["resolved"][0]["state"] == STATE_CANCELED


# ---------------------------------------------------------------------------
# Cancel propagation
# ---------------------------------------------------------------------------

def test_cancel_all_open_confirms_and_records(tmp_path):
    client = _StubOrderClient()
    bridge = _bridge(tmp_path, order_client=client)
    bridge.ledger.record_pending("cid-1", "BNBUSDT", "BUY", Decimal("100"),
                                 Decimal("0.25"))
    bridge.ledger.record_ack("cid-1", STATE_OPEN, 4242, Decimal("0"))
    report = bridge.cancel_all_open("strategy_exit")
    assert report["cancels"][0]["canceled"] is True
    assert bridge.ledger.get("cid-1")["state"] == STATE_CANCELED
    assert client.cancels == ["cid-1"]


def test_cancel_pending_place_is_never_blind(tmp_path):
    client = _StubOrderClient()
    bridge = _bridge(tmp_path, order_client=client)
    bridge.ledger.record_pending("cid-1", "BNBUSDT", "BUY", Decimal("100"),
                                 Decimal("0.25"))
    report = bridge.cancel_all_open("kill")
    assert report["cancels"][0]["canceled"] is False
    assert report["cancels"][0]["reason"] == "PENDING_PLACE"
    assert client.cancels == []


def test_cancel_ambiguous_resolved_by_requery(tmp_path):
    client = _StubOrderClient()
    market = _StubMarketClient()
    bridge = _bridge(tmp_path, order_client=client, market_client=market)
    bridge.ledger.record_pending("cid-1", "BNBUSDT", "BUY", Decimal("100"),
                                 Decimal("0.25"))
    bridge.ledger.record_ack("cid-1", STATE_OPEN, 4242, Decimal("0"))
    # The cancel arrived after the order filled: -2011 is ambiguous; the
    # authoritative re-query proves FILLED (not CANCELED).
    client.outcomes["cancel:cid-1"] = BinanceTestnetOrderRejectedError(
        "unknown order", code=-2011, ambiguous=True)
    market.order_status["cid-1"] = _order_payload("cid-1", status="FILLED",
                                                  executed="0.25")
    report = bridge.cancel_all_open("strategy_exit")
    assert report["cancels"][0]["canceled"] is True
    assert report["cancels"][0]["state"] == STATE_FILLED
    assert bridge.ledger.get("cid-1")["state"] == STATE_FILLED


# ---------------------------------------------------------------------------
# Unknown-remote guard
# ---------------------------------------------------------------------------

def test_unknown_remote_orders_fail_closed(tmp_path):
    market = _StubMarketClient()
    bridge = _bridge(tmp_path, market_client=market)
    known = _order_payload("cid-known", status="NEW")
    market.open_orders_result = [
        SimpleNamespace(client_order_id="AGBNB-BNBUSDT-G00001-00001-B",
                        side="BUY", price=Decimal("100"), orig_qty=Decimal("0.25")),
        SimpleNamespace(client_order_id="FOREIGN-ORDER-1",
                        side="SELL", price=Decimal("101"), orig_qty=Decimal("1")),
    ]
    bridge.ledger.record_pending("AGBNB-BNBUSDT-G00001-00001-B", "BNBUSDT",
                                 "BUY", Decimal("100"), Decimal("0.25"))
    unknown = bridge.unknown_remote_orders()
    # Own namespace, not in ledger -> flagged.  Foreign namespace -> ignored
    # (outside this runtime's authority), ledger-known -> not flagged.
    assert [u["cid"] for u in unknown] == ["FOREIGN-ORDER-1"]


# ---------------------------------------------------------------------------
# build_bridge_from_env (real gate machinery, no network)
# ---------------------------------------------------------------------------

def test_build_bridge_disabled_by_config(monkeypatch, tmp_path):
    monkeypatch.setenv("TESTNET_ORDERS_ENABLED", "true")
    cfg = {"execution": {"testnet_execution": False}}
    bridge, gate = build_bridge_from_env(cfg, "BNBUSDT", str(tmp_path / "db.sqlite3"))
    assert bridge is None and gate.enabled is False


def test_build_bridge_disabled_by_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TESTNET_ORDERS_ENABLED", "false")
    cfg = {"execution": {"testnet_execution": True}}
    bridge, gate = build_bridge_from_env(cfg, "BNBUSDT", str(tmp_path / "db.sqlite3"))
    assert bridge is None and gate.enabled is False


def test_build_bridge_enabled_constructs(monkeypatch, tmp_path):
    # Full gate open: constructs real (offline-capable) clients with dummy
    # credentials; no network call happens during construction.
    monkeypatch.setenv("BINANCE_ENV", "testnet")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "test-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "test-secret")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.setenv("TESTNET_ORDERS_ENABLED", "true")
    cfg = {"execution": {"testnet_execution": True}}
    bridge, gate = build_bridge_from_env(
        cfg, "BNBUSDT", str(tmp_path / "db.sqlite3"))
    assert gate.enabled is True
    assert isinstance(bridge, TestnetExecutionBridge)


# ---------------------------------------------------------------------------
# Runtime wiring
# ---------------------------------------------------------------------------

def test_runtime_mirrors_submitted_orders(monkeypatch, tmp_path):
    from tests.test_multi_symbol import (
        _install_runner_stubs, _features, _config, _account_snapshot,
    )
    cfg = _install_runner_stubs(monkeypatch, tmp_path, features=_features())
    monkeypatch.setattr(msm, "fetch_account_snapshot",
                        lambda c, b, q: _account_snapshot())
    mirrored = []

    class _StubBridge:
        def reconcile(self):
            return {"symbol": "BNBUSDT", "checked": 0, "resolved": [],
                    "unknown": [], "errors": []}

        def unknown_remote_orders(self):
            return []

        def mirror_order(self, intent):
            mirrored.append(intent.client_order_id)
            return {"mirrored": True, "cid": intent.client_order_id,
                    "state": STATE_OPEN}

        def cancel_all_open(self, actor):
            return {"symbol": "BNBUSDT", "cancels": []}

        def summary(self):
            return {"symbol": "BNBUSDT", "open_mirrored": len(mirrored)}

    db_path = msm._symbol_db_path(cfg["logging"]["sqlite_path"], "BNBUSDT")
    from shutdown import ShutdownCoordinator
    import logging
    runner = msm.SymbolCycleRunner("BNBUSDT", cfg, db_path, object(),
                                   logging.getLogger("test"),
                                   ShutdownCoordinator(),
                                   bridge=_StubBridge())
    result = runner.run_cycle()

    assert result["success"] is True, result["error"]
    assert result["status"] == "OK"
    assert result["cycle_result"]["orders_submitted"] > 0
    # Every paper submission was mirrored to a real testnet order.
    assert len(mirrored) == result["cycle_result"]["orders_submitted"]
    assert result["bridge_mirrored"][0]["mirrored"] is True


def test_runtime_bridge_unknown_remote_blocks_cycle(monkeypatch, tmp_path):
    from tests.test_multi_symbol import (
        _install_runner_stubs, _features, _config,
    )
    cfg = _install_runner_stubs(monkeypatch, tmp_path, features=_features())

    class _StubBridge:
        def reconcile(self):
            return {"symbol": "BNBUSDT", "checked": 0, "resolved": [],
                    "unknown": [], "errors": []}

        def unknown_remote_orders(self):
            return [{"cid": "AGBNB-BNBUSDT-G99999-99999-B", "side": "BUY",
                     "price": "1", "quantity": "1"}]

    db_path = msm._symbol_db_path(cfg["logging"]["sqlite_path"], "BNBUSDT")
    from shutdown import ShutdownCoordinator
    import logging
    runner = msm.SymbolCycleRunner("BNBUSDT", cfg, db_path, object(),
                                   logging.getLogger("test"),
                                   ShutdownCoordinator(),
                                   bridge=_StubBridge())
    result = runner.run_cycle()

    assert result["success"] is True
    assert result["status"] == "RISK_BLOCKED"
    assert "EXECUTION_UNKNOWN_REMOTE_ORDERS" in result["combined_reason"]
    # Fail-closed: no paper cycle, therefore nothing mirrored.
    assert "cycle_result" not in result
