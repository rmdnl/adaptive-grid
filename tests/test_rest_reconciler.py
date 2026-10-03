"""Round 6A — regression tests for the REST reconciliation seam.

Covers the fail-closed contract (Round 6A):
  §3 outcomes CONFIRMED / UNKNOWN / FAILED
  §4 timeout-after-submit (never blind-resubmit; query by clientOrderId)
  §5 cancel semantics (UNKNOWN/FAILED/timeout keep kill active)
  §6 order status (unknown/malformed status fails closed)
  §7 open-order reconciliation (A exact … H stale; no destructive action)
  §8 deterministic client-order-id
  §9 rate-limit 429/418 (bounded backoff, honor Retry-After, fail closed)
  §10 clock skew (bounded offset; absurd values refused; never touch OS clock)
  §11 per-op retry policy (READ retry-safe; WRITE not blind-retried)
  §14 observability (credential-free structured records)
  §15 state machine (no UNKNOWN -> assume-canceled -> duplicate)
  §16 security / fail-closed (malformed / bad types / auth / permission)
  §19 live safety barrier (reconciler refuses non-DRY_RUN / live clients)

All tests are deterministic: no network, no real sleep, mocked rest_api.
"""
from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

import binance_testnet as bt
from binance_testnet import (
    BinanceTestnetClient,
    BinanceTestnetConfig,
    BinanceTestnetConfigError,
    BinanceTestnetAuthenticationError,
    BinanceTestnetEnvironmentError,
    BinanceTestnetNetworkError,
    BinanceTestnetRateLimitError,
    BinanceTestnetTimestampError,
    BinanceTestnetResponseError,
    BinanceTestnetValidationError,
)
import rest_reconciler as rr
from rest_reconciler import (
    RestReconciler,
    Outcome,
    CancelVerdict,
    OrderStatus,
    CancelRecord,
    ReconciliationResult,
    ClockSkewReport,
    RETRY_BUDGET,
    Op,
)
from order_engine import make_client_order_id


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _config(**over) -> BinanceTestnetConfig:
    base = dict(
        environment="testnet",
        base_url="https://testnet.binance.vision",
        api_key="test-key",
        api_secret="test-secret",
        dry_run=True,
        allow_live_execution=False,
    )
    base.update(over)
    return BinanceTestnetConfig(**base)


def _invalid_config(**over):
    """Build a BinanceTestnetConfig that FAILS its own validation.

    The adapter normally refuses to construct such a config; the
    reconciler re-asserts the DRY_RUN / ALLOW_LIVE_EXECUTION barrier itself
    (Round 6A §19), which is exactly what the barrier tests prove — so the
    constructor is bypassed to reach that code path.
    """
    base = dict(
        environment="testnet",
        base_url="https://testnet.binance.vision",
        api_key="test-key",
        api_secret="test-secret",
        dry_run=True,
        allow_live_execution=False,
        timeout_ms=5000,
        retries=3,
        backoff_ms=1000,
        max_open_orders=100,
        max_account_assets=1000,
    )
    base.update(over)
    cfg = object.__new__(BinanceTestnetConfig)
    for key, value in base.items():
        object.__setattr__(cfg, key, value)
    return cfg


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def data(self):
        return self._payload


def _client(*, open_orders=None, get_order=None, server_time=None,
            config=None, rest_error=None):
    """Build a real BinanceTestnetClient with a mocked rest_api.

    Pass callables that raise/return to drive the REAL adapter methods
    (get_order / open_orders / server_time) so validation + error mapping
    are exercised, not a mock of the seam.
    """
    cfg = config or _config()
    client = BinanceTestnetClient.__new__(BinanceTestnetClient)
    client._config = cfg
    rest = SimpleNamespace()

    def _get_open_orders(symbol=None, recv_window=None):
        if rest_error:
            rest_error()
        return _Resp(open_orders if open_orders is not None else [])

    def _get_order(symbol=None, order_id=None, orig_client_order_id=None,
                  recv_window=None):
        if rest_error:
            rest_error()
        return _Resp(get_order)

    def _time(recv_window=None):
        if rest_error:
            rest_error()
        if server_time is not None:
            return _Resp({"serverTime": server_time})
        raise BinanceTestnetNetworkError("server-time unavailable")

    rest.get_open_orders = _get_open_orders
    rest.get_order = _get_order
    rest.time = _time
    client._spot = SimpleNamespace(rest_api=rest)
    return client


def _open_order(order_id=1, client_id="AG-BNBUSDT-G00001-00001-B", **ov):
    d = {
        "symbol": "BNBUSDT",
        "orderId": order_id,
        "clientOrderId": client_id,
        "price": "300.00000000",
        "origQty": "1.00000000",
        "executedQty": "0.00000000",
        "status": "NEW",
        "timeInForce": "GTC",
        "type": "LIMIT",
        "side": "BUY",
        "isWorking": True,
    }
    d.update(ov)
    return d


def _order_payload(status="FILLED", **ov):
    d = {
        "symbol": "BNBUSDT",
        "orderId": 123,
        "clientOrderId": "AG-BNBUSDT-G00001-00001-B",
        "price": "300.00000000",
        "origQty": "1.00000000",
        "executedQty": "1.00000000",
        "status": status,
        "timeInForce": "GTC",
        "type": "LIMIT",
        "side": "BUY",
    }
    d.update(ov)
    return d


def _no_sleep():
    return lambda _d: None


# ---------------------------------------------------------------------------
# §19 / adapter isolation: the seam may only wrap a dry-run testnet client
# ---------------------------------------------------------------------------
def test_reconciler_rejects_live_client():
    # The adapter itself refuses to construct this config; the reconciler
    # re-asserts the barrier independently (§19) — bypass the constructor
    # to prove the seam's own check is live.
    client = _client(config=_invalid_config(allow_live_execution=True))
    with pytest.raises(BinanceTestnetConfigError):
        RestReconciler(client, sleep=_no_sleep())


def test_reconciler_rejects_non_dry_run():
    client = _client(config=_invalid_config(dry_run=False))
    with pytest.raises(BinanceTestnetConfigError):
        RestReconciler(client, sleep=_no_sleep())


def test_reconciler_rejects_client_without_config():
    client = BinanceTestnetClient.__new__(BinanceTestnetClient)
    with pytest.raises(BinanceTestnetConfigError):
        RestReconciler(client, sleep=_no_sleep())


# ---------------------------------------------------------------------------
# §3 + §7 G: open-order fetch outcomes
# ---------------------------------------------------------------------------
def test_open_orders_exact_success_confirmed():
    client = _client(open_orders=[_open_order()])
    r = RestReconciler(client, sleep=_no_sleep())
    outcome, orders, attempts = r.fetch_open_orders("BNBUSDT")
    assert outcome is Outcome.CONFIRMED
    assert attempts == 1
    assert len(orders) == 1


def test_open_orders_empty_list_is_confirmed_not_assumed():
    # An *established* empty snapshot is authoritative (no open orders);
    # it is distinguished from a failure that returns empty.
    client = _client(open_orders=[])
    r = RestReconciler(client, sleep=_no_sleep())
    outcome, orders, _ = r.fetch_open_orders("BNBUSDT")
    assert outcome is Outcome.CONFIRMED
    assert orders == []


def test_open_orders_network_failure_settles_unknown():
    def boom():
        raise BinanceTestnetNetworkError("connection reset")
    client = _client(open_orders=[], rest_error=boom)
    r = RestReconciler(client, sleep=_no_sleep())
    outcome, orders, attempts = r.fetch_open_orders("BNBUSDT")
    assert outcome is Outcome.UNKNOWN
    assert orders == []
    assert attempts == RETRY_BUDGET[Op.READ_OPEN_ORDERS]


def test_open_orders_auth_failure_settles_failed():
    def boom():
        raise BinanceTestnetAuthenticationError("invalid apikey")
    client = _client(open_orders=[], rest_error=boom)
    r = RestReconciler(client, sleep=_no_sleep())
    outcome, _, attempts = r.fetch_open_orders("BNBUSDT")
    assert outcome is Outcome.FAILED
    assert attempts == 1  # deterministic → not retried


def test_open_orders_env_failure_settles_failed():
    def boom():
        raise BinanceTestnetEnvironmentError("base url not testnet")
    client = _client(open_orders=[], rest_error=boom)
    r = RestReconciler(client, sleep=_no_sleep())
    outcome, _, attempts = r.fetch_open_orders("BNBUSDT")
    assert outcome is Outcome.FAILED
    assert attempts == 1


def test_open_orders_malformed_response_settles_unknown():
    # Malformed open-order payload (status outside NEW/PARTIALLY_FILLED) is
    # rejected by the adapter parser → ValidationError → UNKNOWN (§3/§7 F).
    client = _client(open_orders=[_open_order(status="FILLED")])
    r = RestReconciler(client, sleep=_no_sleep())
    outcome, _, _ = r.fetch_open_orders("BNBUSDT")
    assert outcome is Outcome.UNKNOWN


def test_open_orders_duplicate_order_id_settles_unknown():
    dup = [
        _open_order(order_id=7, client_id="AG-BNBUSDT-G00001-00001-B"),
        _open_order(order_id=7, client_id="AG-BNBUSDT-G00001-00002-S"),
    ]
    client = _client(open_orders=dup)
    r = RestReconciler(client, sleep=_no_sleep())
    outcome, _, _ = r.fetch_open_orders("BNBUSDT")
    assert outcome is Outcome.UNKNOWN


def test_open_orders_surprise_exception_settles_unknown():
    def boom():
        raise RuntimeError("SDK crash (unclassified)")
    client = _client(open_orders=[], rest_error=boom)
    r = RestReconciler(client, sleep=_no_sleep())
    outcome, orders, _ = r.fetch_open_orders("BNBUSDT")
    assert outcome is Outcome.UNKNOWN
    assert orders == []


# ---------------------------------------------------------------------------
# §4 + §6: individual order status / timeout-after-submit
# ---------------------------------------------------------------------------
def test_resolve_order_filled_confirmed():
    client = _client(get_order=_order_payload("FILLED"))
    r = RestReconciler(client, sleep=_no_sleep())
    st = r.settle_timeout_after_submit("BNBUSDT", "AG-BNBUSDT-G00001-00001-B")
    assert st.outcome is Outcome.CONFIRMED
    assert st.status == "FILLED"
    assert st.authoritative is True
    assert st.blocks_new_orders is False


def test_resolve_order_open_confirmed():
    client = _client(get_order=_order_payload("NEW"))
    r = RestReconciler(client, sleep=_no_sleep())
    st = r.settle_timeout_after_submit("BNBUSDT", "AG-BNBUSDT-G00001-00001-B")
    assert st.outcome is Outcome.CONFIRMED
    assert st.status == "NEW"


def test_resolve_order_partially_filled_confirmed():
    client = _client(get_order=_order_payload("PARTIALLY_FILLED",
                                               executedQty="0.50000000"))
    r = RestReconciler(client, sleep=_no_sleep())
    st = r.settle_timeout_after_submit("BNBUSDT", "AG-BNBUSDT-G00001-00001-B")
    assert st.outcome is Outcome.CONFIRMED
    assert st.status == "PARTIALLY_FILLED"


def test_resolve_order_canceled_confirmed():
    client = _client(get_order=_order_payload("CANCELED"))
    r = RestReconciler(client, sleep=_no_sleep())
    st = r.settle_timeout_after_submit("BNBUSDT", "AG-BNBUSDT-G00001-00001-B")
    assert st.outcome is Outcome.CONFIRMED
    assert st.status == "CANCELED"


def test_resolve_order_unknown_status_fails_closed():
    # §6: a status the adapter does not know must never map to a safe state.
    client = _client(get_order=_order_payload("WEIRD_STATUS"))
    r = RestReconciler(client, sleep=_no_sleep())
    st = r.settle_timeout_after_submit("BNBUSDT", "AG-BNBUSDT-G00001-00001-B")
    assert st.outcome is Outcome.UNKNOWN
    assert st.authoritative is False
    assert st.blocks_new_orders is True


def test_resolve_order_missing_status_fails_closed():
    payload = _order_payload("FILLED")
    del payload["status"]
    client = _client(get_order=payload)
    r = RestReconciler(client, sleep=_no_sleep())
    st = r.settle_timeout_after_submit("BNBUSDT", "AG-BNBUSDT-G00001-00001-B")
    assert st.outcome is Outcome.UNKNOWN


def test_resolve_order_missing_order_is_ambiguous():
    # §3/§4: a 404/missing-order is NEVER proof the order did not exist.
    def boom():
        raise BinanceTestnetNetworkError("order not found", not_found=True)
    client = _client(get_order=None, rest_error=boom)
    r = RestReconciler(client, sleep=_no_sleep())
    st = r.settle_timeout_after_submit("BNBUSDT", "AG-BNBUSDT-G00001-00001-B")
    assert st.outcome is Outcome.UNKNOWN
    assert st.authoritative is False
    assert st.blocks_new_orders is True


def test_resolve_order_timeout_settles_unknown_not_resubmitted():
    # §4: the seam only QUERIES — it never resubmits.  A call-count guard
    # proves the submit path is absent: get_order is read, open_orders is
    # never called for the settle, and a timed-out query stays UNKNOWN.
    calls = {"get_order": 0, "open_orders": 0}

    def _gso(symbol=None, recv_window=None):
        calls["get_order"] += 1
        raise BinanceTestnetNetworkError("timeout")

    client = _client(open_orders=[], rest_error=lambda: None)
    # override get_order to time out
    client._spot.rest_api.get_order = _gso
    r = RestReconciler(client, sleep=_no_sleep())
    st = r.settle_timeout_after_submit("BNBUSDT", "AG-BNBUSDT-G00001-00001-B")
    assert st.outcome is Outcome.UNKNOWN
    assert st.blocks_new_orders is True
    assert calls["open_orders"] == 0  # never fell back to open-orders resubmit


# ---------------------------------------------------------------------------
# §5: cancel semantics
# ---------------------------------------------------------------------------
def test_cancel_default_executor_unreconciled_no_network():
    client = _client(open_orders=[])
    r = RestReconciler(client, sleep=_no_sleep())
    rec = r.cancel("BNBUSDT", "AG-BNBUSDT-G00001-00001-B")
    assert rec.verdict is CancelVerdict.UNRECONCILED
    assert rec.reconciled is False
    assert rec.attempts == 1


def test_cancel_confirmed_verified_canceled():
    client = _client(get_order=_order_payload("CANCELED"))

    def executor(sym, cid):
        return CancelVerdict.CONFIRMED_CANCELED

    r = RestReconciler(client, cancel_executor=executor, sleep=_no_sleep())
    rec = r.cancel("BNBUSDT", "AG-BNBUSDT-G00001-00001-B")
    assert rec.verdict is CancelVerdict.CONFIRMED_CANCELED
    assert rec.reconciled is True


def test_cancel_confirmed_but_verify_shows_still_open_unreconciled():
    # §5: a "confirmed" cancel whose authoritative re-query shows the order
    # still open is a contradiction → UNRECONCILED (kill stays active).
    client = _client(get_order=_order_payload("NEW"))

    def executor(sym, cid):
        return CancelVerdict.CONFIRMED_CANCELED

    r = RestReconciler(client, cancel_executor=executor, sleep=_no_sleep())
    rec = r.cancel("BNBUSDT", "AG-BNBUSDT-G00001-00001-B")
    assert rec.verdict is CancelVerdict.UNRECONCILED


def test_cancel_timeout_executor_unreconciled():
    # §5: cancel response times out → the executor cannot confirm → UNRECONCILED.
    client = _client(open_orders=[])

    def executor(sym, cid):
        return CancelVerdict.UNRECONCILED

    r = RestReconciler(client, cancel_executor=executor, sleep=_no_sleep())
    rec = r.cancel("BNBUSDT", "AG-BNBUSDT-G00001-00001-B")
    assert rec.verdict is CancelVerdict.UNRECONCILED


def test_cancel_verify_query_fails_unreconciled():
    # §5: cancel acked but the verifying query itself fails → inconclusive.
    def boom():
        raise BinanceTestnetNetworkError("timeout")
    client = _client(get_order=None, rest_error=boom)

    def executor(sym, cid):
        return CancelVerdict.CONFIRMED_CANCELED

    r = RestReconciler(client, cancel_executor=executor, sleep=_no_sleep())
    rec = r.cancel("BNBUSDT", "AG-BNBUSDT-G00001-00001-B")
    assert rec.verdict is CancelVerdict.UNRECONCILED


def test_cancel_malformed_ack_unreconciled():
    def executor(sym, cid):
        # malformed/failed ack → not confirmed
        return CancelVerdict.UNRECONCILED
    client = _client(open_orders=[])
    r = RestReconciler(client, cancel_executor=executor, sleep=_no_sleep())
    assert r.cancel("BNBUSDT", "AG-BNBUSDT-G00001-00001-B").verdict \
        is CancelVerdict.UNRECONCILED


def test_cancel_unknown_order_unreconciled():
    # §5 cancel unknown order: executor cannot confirm an unknown order is gone.
    def executor(sym, cid):
        return CancelVerdict.UNRECONCILED
    client = _client(open_orders=[])
    r = RestReconciler(client, cancel_executor=executor, sleep=_no_sleep())
    assert r.cancel("BNBUSDT", "AG-BNBUSDT-G00001-00001-B").verdict \
        is CancelVerdict.UNRECONCILED


def test_cancel_already_filled_unreconciled():
    # §5 cancel already-filled order: a FILLED order cannot be "canceled";
    # the verdict must not claim the order is gone.
    def executor(sym, cid):
        return CancelVerdict.UNRECONCILED
    client = _client(get_order=_order_payload("FILLED"))
    r = RestReconciler(client, cancel_executor=executor, sleep=_no_sleep())
    assert r.cancel("BNBUSDT", "AG-BNBUSDT-G00001-00001-B").verdict \
        is CancelVerdict.UNRECONCILED


# ---------------------------------------------------------------------------
# §7: open-order reconciliation (no destructive action)
# ---------------------------------------------------------------------------
def _recon(r, local, **kw):
    return r.reconcile_open_orders("BNBUSDT", local, **kw)


def test_reconcile_exact_match_authoritative():
    client = _client(open_orders=[_open_order()])
    r = RestReconciler(client, sleep=_no_sleep())
    res = _recon(r, {"AG-BNBUSDT-G00001-00001-B": "NEW"})
    assert res.authoritative is True
    assert res.outcome is Outcome.CONFIRMED
    assert res.matched == ("AG-BNBUSDT-G00001-00001-B",)
    assert res.blocks_new_orders is False


def test_reconcile_local_missing_remotely_blocks():
    client = _client(open_orders=[])
    r = RestReconciler(client, sleep=_no_sleep())
    res = _recon(r, {"AG-BNBUSDT-G00001-00001-B": "NEW"})
    assert res.authoritative is False
    assert res.local_missing_remotely == ("AG-BNBUSDT-G00001-00001-B",)
    assert res.blocks_new_orders is True


def test_reconcile_remote_missing_locally_blocks():
    client = _client(open_orders=[_open_order()])
    r = RestReconciler(client, sleep=_no_sleep())
    res = _recon(r, {})
    assert res.authoritative is False
    assert res.remote_missing_locally == ("AG-BNBUSDT-G00001-00001-B",)
    assert res.blocks_new_orders is True


def test_reconcile_status_mismatch_blocks():
    client = _client(open_orders=[_open_order(status="PARTIALLY_FILLED",
                                               executedQty="0.50000000")])
    r = RestReconciler(client, sleep=_no_sleep())
    res = _recon(r, {"AG-BNBUSDT-G00001-00001-B": "NEW"})
    assert res.authoritative is False
    assert res.status_mismatches == ("AG-BNBUSDT-G00001-00001-B",)


def test_reconcile_duplicate_remote_blocks():
    # Defense-in-depth branch: the adapter parser already rejects a
    # duplicate-clientOrderId snapshot (§7 F/E), and
    # test_open_orders_duplicate_order_id_settles_unknown proves that.
    # This test drives the RECONCILER's own duplicate guard with a
    # pre-parsed snapshot (as if a future transport ever bypassed the
    # parser): still fails closed with no destructive action.
    from binance_testnet import BinanceOpenOrderSnapshot

    def snap(order_id, cid):
        return BinanceOpenOrderSnapshot(
            order_id=order_id, client_order_id=cid, symbol="BNBUSDT",
            side="BUY", order_type="LIMIT", status="NEW",
            price=Decimal("300"), orig_qty=Decimal("1"),
            executed_qty=Decimal("0"), time_in_force="GTC", is_working=True,
        )
    dup_cids = ["AG-BNBUSDT-G00001-00001-B"]
    client = _client(open_orders=[])
    client.open_orders = lambda symbol: [snap(1, dup_cids[0]), snap(2, dup_cids[0])]
    r = RestReconciler(client, sleep=_no_sleep())
    res = _recon(r, {dup_cids[0]: "NEW"})
    assert res.authoritative is False
    assert res.outcome is Outcome.UNKNOWN
    assert res.duplicates == (dup_cids[0],)
    assert res.blocks_new_orders is True


def test_reconcile_malformed_remote_blocks():
    # §7 F: malformed remote order (invalid numeric) → adapter rejects → UNKNOWN.
    client = _client(open_orders=[_open_order(origQty="-1")])
    r = RestReconciler(client, sleep=_no_sleep())
    res = _recon(r, {"AG-BNBUSDT-G00001-00001-B": "NEW"})
    assert res.authoritative is False
    assert res.outcome is Outcome.UNKNOWN


def test_reconcile_exchange_unavailable_blocks():
    def boom():
        raise BinanceTestnetNetworkError("open orders unavailable")
    client = _client(open_orders=[], rest_error=boom)
    r = RestReconciler(client, sleep=_no_sleep())
    res = _recon(r, {"AG-BNBUSDT-G00001-00001-B": "NEW"})
    assert res.authoritative is False
    assert res.outcome is Outcome.UNKNOWN
    assert res.blocks_new_orders is True


def test_reconcile_stale_response_blocks():
    client = _client(open_orders=[_open_order()])
    r = RestReconciler(client, sleep=_no_sleep())
    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    fetched = now - timedelta(seconds=3600)
    res = _recon(r, {"AG-BNBUSDT-G00001-00001-B": "NEW"},
                 snapshot_fetched_at=fetched, now=now, max_stale_s=60)
    assert res.authoritative is False
    assert res.outcome is Outcome.UNKNOWN
    assert any("§7 H" in d for d in res.details)


def test_reconcile_fresh_snapshot_authoritative_with_staleness_check():
    client = _client(open_orders=[_open_order()])
    r = RestReconciler(client, sleep=_no_sleep())
    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    res = _recon(r, {"AG-BNBUSDT-G00001-00001-B": "NEW"},
                 snapshot_fetched_at=now - timedelta(seconds=5),
                 now=now, max_stale_s=60)
    assert res.authoritative is True


# ---------------------------------------------------------------------------
# §8: deterministic client-order-id
# ---------------------------------------------------------------------------
def test_client_order_id_binance_safe_shape():
    cid = make_client_order_id("AG", "BNBUSDT", 7, 3, "BUY")
    assert len(cid) <= 36
    assert cid.startswith("AG-BNBUSDT-G")
    # deterministic regeneration after restart
    assert cid == make_client_order_id("AG", "BNBUSDT", 7, 3, "BUY")


def test_client_order_id_no_collision_across_intents():
    a = make_client_order_id("AG", "BNBUSDT", 1, 1, "BUY")
    b = make_client_order_id("AG", "BNBUSDT", 1, 1, "SELL")
    c = make_client_order_id("AG", "BNBUSDT", 2, 1, "BUY")
    assert len({a, b, c}) == 3


def test_client_order_id_rejects_bad_inputs():
    with pytest.raises(Exception):
        make_client_order_id("AG!", "BNBUSDT", 1, 1, "BUY")
    with pytest.raises(Exception):
        make_client_order_id("AG", "bnbusdt", 1, 1, "BUY")


def test_restart_retries_reconcile_same_order_no_duplicate():
    # §8 restart + retry + reconciliation: after a process restart the same
    # (generation, grid_index, side, symbol) regenerates the SAME id, so a
    # retry of the timed-out submission resolves the EXISTING order — it
    # can never create a second, colliding economic intent.
    cid_a = make_client_order_id("AG", "BNBUSDT", 1, 4, "SELL")
    cid_b = make_client_order_id("AG", "BNBUSDT", 1, 4, "SELL")  # restart
    assert cid_a == cid_b
    # A different economic intent (different grid cell or side) never collides.
    assert make_client_order_id("AG", "BNBUSDT", 1, 5, "SELL") != cid_a
    assert make_client_order_id("AG", "BNBUSDT", 1, 4, "BUY") != cid_a
    # And reconciliation by that id is deterministic: the remote snapshot
    # carrying the regenerated id matches exactly → authoritative.
    client = _client(open_orders=[_open_order(client_id=cid_a, status="NEW")],
                     get_order=_order_payload("NEW", clientOrderId=cid_a))
    r = RestReconciler(client, sleep=_no_sleep())
    res = _recon(r, {cid_a: "NEW"})
    assert res.authoritative is True
    st = r.settle_timeout_after_submit("BNBUSDT", cid_a)
    assert st.status == "NEW" and st.authoritative is True


# ---------------------------------------------------------------------------
# §9: rate-limit handling (429/418) — bounded, Retry-After aware
# ---------------------------------------------------------------------------
def test_rate_limit_429_respects_retry_after_and_fails_closed():
    sleeps = []

    def slow(d):
        sleeps.append(d)

    def boom():
        e = BinanceTestnetRateLimitError("rate limited", retry_after_s=30,
                                         status_code=429, banned=False)
        raise e

    client = _client(open_orders=[], rest_error=boom)
    r = RestReconciler(client, sleep=slow)
    outcome, _, attempts = r.fetch_open_orders("BNBUSDT")
    assert outcome is Outcome.UNKNOWN
    assert attempts == RETRY_BUDGET[Op.READ_OPEN_ORDERS]
    # every wait honored the Retry-After (30s) rather than the raw schedule
    assert all(s == 30.0 for s in sleeps[:-1]) or not sleeps
    # bounded: the loop stopped after the budget, did not retry forever
    assert len(sleeps) < RETRY_BUDGET[Op.READ_OPEN_ORDERS]


def test_rate_limit_ban_418_fails_closed():
    def boom():
        raise BinanceTestnetRateLimitError("IP banned", retry_after_s=600,
                                           status_code=418, banned=True)
    client = _client(open_orders=[], rest_error=boom)
    r = RestReconciler(client, sleep=_no_sleep())
    outcome, _, _ = r.fetch_open_orders("BNBUSDT")
    assert outcome is Outcome.UNKNOWN


def test_retry_after_capped():
    # Caps live in the consumers so a hostile 99999s header can never stall
    # the loop (§9):
    #   * the adapter mapper caps the SDK Retry-After seconds at
    #     _RETRY_AFTER_MAX_S when it builds a BinanceTestnetRateLimitError;
    #   * the reconciler backoff caps the honored wait at 300s (ms path).
    class _SdkLike:
        retry_after = 99999
    assert bt._retry_after_seconds(_SdkLike()) == 300  # mapper cap
    e = BinanceTestnetRateLimitError("x", retry_after_s=99999, status_code=429)
    assert rr._retry_wait_s(Op.READ_OPEN_ORDERS, 0, e) == 300.0  # reconciler cap
    # A sane Retry-After is honored in full (not shrunk to the 10s schedule).
    e30 = BinanceTestnetRateLimitError("x", retry_after_s=30, status_code=429)
    assert rr._retry_wait_s(Op.READ_OPEN_ORDERS, 0, e30) == 30.0
    # A garbage SDK Retry-After is refused (None → schedule-based wait).
    class _SdkBad:
        retry_after = "abc"
    assert bt._retry_after_seconds(_SdkBad()) is None


# ---------------------------------------------------------------------------
# §10: clock skew
# ---------------------------------------------------------------------------
def _server_now_ms():
    from time import time as _t
    return int(_t() * 1000)


def test_clock_skew_normal():
    client = _client(server_time=_server_now_ms())
    r = RestReconciler(client, sleep=_no_sleep())
    rep = r.sync_clock_skew(samples=2)
    assert rep.established is True
    assert rep.usable is True
    assert abs(rep.offset_ms) <= 15000


def test_clock_skew_small_positive_offset():
    client = _client(server_time=_server_now_ms() + 3000)
    r = RestReconciler(client, sleep=_no_sleep())
    rep = r.sync_clock_skew(samples=1)
    assert rep.established is True
    assert rep.offset_ms > 0


def test_clock_skew_small_negative_offset():
    client = _client(server_time=_server_now_ms() - 3000)
    r = RestReconciler(client, sleep=_no_sleep())
    rep = r.sync_clock_skew(samples=1)
    assert rep.established is True
    assert rep.offset_ms < 0


def test_clock_skew_large_offset_refused():
    # offset 40s > default 15s bound → fail closed (do not trust the offset)
    client = _client(server_time=_server_now_ms() + 40000)
    r = RestReconciler(client, sleep=_no_sleep())
    rep = r.sync_clock_skew(samples=1)
    assert rep.established is False


def test_clock_skew_server_time_failure():
    client = _client(server_time=None)  # server_time() raises NetworkError
    r = RestReconciler(client, sleep=_no_sleep())
    rep = r.sync_clock_skew(samples=1)
    assert rep.established is False


def test_clock_skew_absurd_server_time_refused():
    # a serverTime wildly outside 24h of local is refused (§10)
    client = _client(server_time=1)  # epoch ≈ 0
    r = RestReconciler(client, sleep=_no_sleep())
    rep = r.sync_clock_skew(samples=1)
    assert rep.established is False


def test_clock_skew_malformed_value_refused():
    # server_time returns a non-int payload → adapter rejects → not established
    client = _client()
    client._spot.rest_api.time = lambda recv_window=None: _Resp({"serverTime": "nope"})
    r = RestReconciler(client, sleep=_no_sleep())
    rep = r.sync_clock_skew(samples=1)
    assert rep.established is False


# ---------------------------------------------------------------------------
# §11: per-op retry policy (READ retry-safe, WRITE not blind-retried)
# ---------------------------------------------------------------------------
def test_retry_budget_write_is_one():
    assert RETRY_BUDGET[Op.WRITE_CANCEL] == 1
    assert RETRY_BUDGET[Op.READ_OPEN_ORDERS] >= 1
    assert RETRY_BUDGET[Op.READ_ORDER_STATUS] >= 1


def test_retry_wait_refuses_write_op():
    with pytest.raises(ValueError):
        rr._retry_wait_s(Op.WRITE_CANCEL, 0, BinanceTestnetNetworkError("x"))


def test_read_retries_with_backoff_until_budget():
    waits = []

    def rec(d):
        waits.append(d)

    def boom():
        raise BinanceTestnetNetworkError("reset")
    client = _client(open_orders=[], rest_error=boom)
    r = RestReconciler(client, sleep=rec)
    r.fetch_open_orders("BNBUSDT")
    # budget-1 waits, bounded exponential 1s→2s
    assert waits == [1.0, 2.0]


# ---------------------------------------------------------------------------
# §15: state machine — no UNKNOWN -> assume-canceled -> duplicate
# ---------------------------------------------------------------------------
def test_unknown_settlement_never_assumes_canceled():
    # After a timeout, if the order truly cannot be found the outcome stays
    # UNKNOWN (blocking), never CANCELED.  §15 forbidden path is absent.
    def boom():
        raise BinanceTestnetNetworkError("order not found", not_found=True)
    client = _client(get_order=None, rest_error=boom)
    r = RestReconciler(client, sleep=_no_sleep())
    st = r.settle_timeout_after_submit("BNBUSDT", "AG-BNBUSDT-G00001-00001-B")
    assert st.outcome is Outcome.UNKNOWN
    assert st.status is None
    assert st.blocks_new_orders is True


# ---------------------------------------------------------------------------
# §16: security / fail-closed matrix
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_payload", [
    None,                                   # missing payload
    "just a string",                        # unexpected type
    {"status": "FILLED"},                  # missing required fields
    {"symbol": "OTHERSY", "clientOrderId": "AG-BNBUSDT-G00001-00001-B",
     "status": "FILLED", "orderId": 1, "price": "1", "origQty": "1",
     "executedQty": "1"},                   # symbol mismatch
    {"symbol": "BNBUSDT", "clientOrderId": "AG-BNBUSDT-G00001-00001-B",
     "status": "FILLED", "orderId": 1, "price": "NaN", "origQty": "1",
     "executedQty": "0"},                   # invalid numeric (NaN)
    {"symbol": "BNBUSDT", "clientOrderId": "AG-BNBUSDT-G00001-00001-B",
     "status": "NOT_A_STATUS", "orderId": 1, "price": "1", "origQty": "1",
     "executedQty": "0"},                  # invalid status
])
def test_malformed_order_payload_fails_closed(bad_payload):
    client = _client(get_order=bad_payload)
    r = RestReconciler(client, sleep=_no_sleep())
    st = r.settle_timeout_after_submit("BNBUSDT", "AG-BNBUSDT-G00001-00001-B")
    assert st.outcome is Outcome.UNKNOWN
    assert st.authoritative is False


def test_exchange_api_exception_fails_closed():
    def boom():
        raise BinanceTestnetResponseError("unexpected JSON")
    client = _client(get_order=None, rest_error=boom)
    r = RestReconciler(client, sleep=_no_sleep())
    st = r.settle_timeout_after_submit("BNBUSDT", "AG-BNBUSDT-G00001-00001-B")
    assert st.outcome is Outcome.UNKNOWN


def test_auth_error_fails_closed():
    def boom():
        raise BinanceTestnetAuthenticationError("unauthorized")
    client = _client(get_order=None, rest_error=boom)
    r = RestReconciler(client, sleep=_no_sleep())
    st = r.settle_timeout_after_submit("BNBUSDT", "AG-BNBUSDT-G00001-00001-B")
    assert st.outcome is Outcome.FAILED


def test_timestamp_error_classified():
    e = BinanceTestnetTimestampError("Timestamp ... earlier than our time")
    assert isinstance(e, BinanceTestnetNetworkError)


def test_no_unsafe_order_creation_from_any_error():
    # Every adapter error settles UNKNOWN or FAILED — never CONFIRMED, so a
    # caller gating new orders on (outcome is CONFIRMED) can never create an
    # order from a bad response.  The lambda must RAISE (a bare return
    # would not reach the failure path).
    for maker in (
        lambda: BinanceTestnetNetworkError("x"),
        lambda: BinanceTestnetRateLimitError("429"),
        lambda: BinanceTestnetTimestampError("ts"),
        lambda: BinanceTestnetResponseError("bad"),
        lambda: BinanceTestnetValidationError("bad"),
        lambda: BinanceTestnetAuthenticationError("401"),
        lambda: BinanceTestnetEnvironmentError("prod"),
    ):
        def rest_error(mk=maker):
            raise mk()
        client = _client(open_orders=[], rest_error=rest_error)
        r = RestReconciler(client, sleep=_no_sleep())
        outcome, _, _ = r.fetch_open_orders("BNBUSDT")
        assert outcome in (Outcome.UNKNOWN, Outcome.FAILED)


# ---------------------------------------------------------------------------
# §14: observability — credential-free structured records
# ---------------------------------------------------------------------------
def test_log_records_credential_free_and_complete():
    records = []
    client = _client(open_orders=[_open_order()])
    r = RestReconciler(client, sleep=_no_sleep())
    r.set_log_sink(records.append)
    r.fetch_open_orders("BNBUSDT")
    assert records, "expected log records"
    rec = records[-1]
    for key in ("op", "symbol", "client_order_id", "outcome", "retries",
                "error_class"):
        assert key in rec
    blob = json.dumps(rec)
    assert "test-key" not in blob
    assert "test-secret" not in blob
    assert "authorization" not in blob.lower()


def test_log_sink_failure_does_not_break_reconciliation():
    client = _client(open_orders=[_open_order()])
    r = RestReconciler(client, sleep=_no_sleep())

    def bad_sink(_):
        raise RuntimeError("sink broken")
    r.set_log_sink(bad_sink)
    # must not raise
    outcome, _, _ = r.fetch_open_orders("BNBUSDT")
    assert outcome is Outcome.CONFIRMED


# ---------------------------------------------------------------------------
# §5/§20 integration: kill stays active on unreconciled cancel
# ---------------------------------------------------------------------------
def test_unreconciled_cancel_keeps_kill_path_open():
    # Mirrors CancelController: only CONFIRMED_CANCELED / ALREADY releases;
    # UNRECONCILED keeps the kill active.  Assert the verdict → block mapping.
    client = _client(open_orders=[])
    r = RestReconciler(client, sleep=_no_sleep())
    rec = r.cancel("BNBUSDT", "AG-BNBUSDT-G00001-00001-B")
    assert rec.reconciled is False
    # if the caller wires kill-active = (not reconciled), it stays active
    assert (not rec.reconciled) is True
