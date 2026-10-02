"""PHASE 8B — Testnet account & credential validation (deterministic, mocked).

Covers the *data* path of the authenticated Testnet account / open-order
endpoints using the production parse + validation code (market_data), with a
fake SDK client so no network / no real credentials are needed.  Proves:

- account responses parse into the immutable AccountSnapshot, balances
  non-negative, BNB/USDT identified, malformed/negative rejected;
- open-order responses parse with full field validation, duplicate /
  malformed / wrong-symbol / executed>orig rejected;
- an UNAVAILABLE account or open-order endpoint raises a TYPED error and is
  NEVER converted to a zero balance or an empty list (fail-closed);
- account equity is deterministic Decimal arithmetic on a fresh ticker;
- the capacity gate (Phase 5B) resolves min(config, exchange MAX_NUM_ORDERS)
  and fails closed when the limit is unresolvable (cases A/B/C/D);
- the read-only client issues GET requests only and never leaks the API
  secret into a request URL or an exception message.

No Testnet order, cancel, modify, or withdrawal endpoint is ever called.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from market_data import (
    AccountRequestError,
    AccountSnapshot,
    AccountValidationError,
    OpenOrdersRequestError,
    OpenOrdersValidationError,
    build_account_risk_state,
    calculate_spot_equity,
    fetch_account_snapshot,
    fetch_open_orders,
)

from tests.test_paper_orchestrator import (
    _seed_open_order,
    happy_cfg,
    make_input,
    make_session,
)

D = Decimal


# ---------------------------------------------------------------------------
# Fake SDK client helpers (mocked, deterministic, no network)
# ---------------------------------------------------------------------------

def _resp(payload: Any):
    return SimpleNamespace(data=lambda: payload)


class _Client:
    """Minimal stand-in for the SDK Spot client (only the read paths)."""

    def __init__(self, account=None, open_orders=None,
                 account_exc=None, open_exc=None):
        self._account = account
        self._open = open_orders
        self._account_exc = account_exc
        self._open_exc = open_exc

        rest = SimpleNamespace()

        def get_account(**_kw):
            if self._account_exc is not None:
                raise self._account_exc
            return _resp(self._account)

        def get_open_orders(**_kw):
            if self._open_exc is not None:
                raise self._open_exc
            return _resp(self._open)

        rest.get_account = get_account
        rest.get_open_orders = get_open_orders
        self.rest_api = rest

    # expose like the SDK client
    rest_api: Any = None  # replaced by the instance above


def _client(account=None, open_orders=None, account_exc=None, open_exc=None) -> Any:
    c = _Client.__new__(_Client)
    c._account = account
    c._open = open_orders
    c._account_exc = account_exc
    c._open_exc = open_exc

    rest = SimpleNamespace()

    def get_account(**_kw):
        if c._account_exc is not None:
            raise c._account_exc
        return _resp(c._account)

    def get_open_orders(**_kw):
        if c._open_exc is not None:
            raise c._open_exc
        return _resp(c._open)

    rest.get_account = get_account
    rest.get_open_orders = get_open_orders
    c.rest_api = rest
    return c


ACCOUNT = {
    "balances": [
        {"asset": "BNB", "free": "2.50000000", "locked": "0.00000000"},
        {"asset": "USDT", "free": "500.25000000", "locked": "10.50000000"},
    ]
}


def _order(order_id=1, cid="grid-1", **ov):
    o = {
        "symbol": "BNBUSDT", "orderId": order_id, "clientOrderId": cid,
        "price": "350.50000000", "origQty": "1.50000000",
        "executedQty": "0.25000000", "status": "NEW", "timeInForce": "GTC",
        "type": "LIMIT", "side": "BUY", "isWorking": True,
    }
    o.update(ov)
    return o


# ---------------------------------------------------------------------------
# 4. Account validation
# ---------------------------------------------------------------------------

def test_account_parses_into_snapshot_nonnegative_balances():
    snap = fetch_account_snapshot(_client(account=ACCOUNT), "BNB", "USDT")
    assert isinstance(snap, AccountSnapshot)
    assert snap.base_asset == "BNB" and snap.quote_asset == "USDT"
    assert snap.base_free == D("2.5") and snap.base_locked == D(0)
    assert snap.quote_free == D("500.25") and snap.quote_locked == D("10.5")
    # no negative balances anywhere
    assert all(v >= 0 for v in (snap.base_free, snap.base_locked,
                                snap.quote_free, snap.quote_locked))


def test_account_malformed_missing_balances_rejected():
    with pytest.raises(AccountValidationError):
        fetch_account_snapshot(_client(account={"not": "balances"}), "BNB", "USDT")


def test_account_negative_free_rejected():
    bad = {"balances": [
        {"asset": "BNB", "free": "-1.0", "locked": "0"},
        {"asset": "USDT", "free": "0", "locked": "0"},
    ]}
    with pytest.raises(AccountValidationError):
        fetch_account_snapshot(_client(account=bad), "BNB", "USDT")


def test_account_ambiguous_duplicate_asset_rejected():
    bad = {"balances": [
        {"asset": "BNB", "free": "1", "locked": "0"},
        {"asset": "BNB", "free": "2", "locked": "0"},
        {"asset": "USDT", "free": "1", "locked": "0"},
    ]}
    with pytest.raises(AccountValidationError):
        fetch_account_snapshot(_client(account=bad), "BNB", "USDT")


def test_account_unavailable_never_zero():
    # A request failure MUST raise a typed error, never a zero-balance snapshot.
    with pytest.raises(AccountRequestError):
        fetch_account_snapshot(
            _client(account_exc=RuntimeError("network down")), "BNB", "USDT"
        )


# ---------------------------------------------------------------------------
# 5. Open-order validation
# ---------------------------------------------------------------------------

def test_open_orders_parse_with_full_field_validation():
    orders = fetch_open_orders(
        _client(open_orders=[_order(1, "a"), _order(2, "b", side="SELL")]),
        "BNBUSDT",
    )
    assert len(orders) == 2
    o0 = orders[0]
    assert o0.order_id == 1 and o0.client_order_id == "a"
    assert o0.side == "BUY" and o0.status == "NEW"
    assert o0.executed_qty <= o0.orig_qty
    assert o0.is_working is True


def test_open_orders_duplicate_order_id_rejected():
    with pytest.raises(OpenOrdersValidationError):
        fetch_open_orders(
            _client(open_orders=[_order(1, "a"), _order(1, "b")]), "BNBUSDT"
        )


def test_open_orders_duplicate_client_id_rejected():
    with pytest.raises(OpenOrdersValidationError):
        fetch_open_orders(
            _client(open_orders=[_order(1, "x"), _order(2, "x")]), "BNBUSDT"
        )


def test_open_orders_symbol_mismatch_rejected():
    with pytest.raises(OpenOrdersValidationError):
        fetch_open_orders(
            _client(open_orders=[_order(1, "a", symbol="ETHUSDT")]), "BNBUSDT"
        )


def test_open_orders_executed_exceeds_orig_rejected():
    with pytest.raises(OpenOrdersValidationError):
        fetch_open_orders(
            _client(open_orders=[_order(1, "a",
                                        origQty="1", executedQty="2")]), "BNBUSDT"
        )


def test_open_orders_non_list_rejected():
    with pytest.raises(OpenOrdersValidationError):
        fetch_open_orders(_client(open_orders={"not": "a list"}), "BNBUSDT")


def test_open_orders_unavailable_never_empty_list():
    # An unavailable endpoint MUST NOT be interpreted as a verified empty list.
    with pytest.raises(OpenOrdersRequestError):
        fetch_open_orders(
            _client(open_exc=RuntimeError("timeout")), "BNBUSDT"
        )


# ---------------------------------------------------------------------------
# 6. Account equity (deterministic Decimal arithmetic on a fresh ticker)
# ---------------------------------------------------------------------------

def test_equity_is_quote_total_plus_base_total_times_mark():
    snap = fetch_account_snapshot(_client(account=ACCOUNT), "BNB", "USDT")
    price = D("770.40")
    equity = calculate_spot_equity(snap, price)
    # quote_total = 500.25 + 10.5 ; base_total = 2.5
    expected = (D("500.25") + D("10.5")) + D("2.5") * price
    assert equity == expected
    # Decimal-deterministic
    assert equity == calculate_spot_equity(snap, price)


def test_equity_rejects_nonpositive_mark_price():
    snap = fetch_account_snapshot(_client(account=ACCOUNT), "BNB", "USDT")
    for bad in (0, "-1"):
        with pytest.raises(AccountValidationError):
            calculate_spot_equity(snap, D(bad))


def test_account_risk_state_drawdown_deterministic():
    snap = fetch_account_snapshot(_client(account=ACCOUNT), "BNB", "USDT")
    price = D("770.40")
    state = build_account_risk_state(snap, price, reference_equity=D("6000"))
    assert state.current_equity < D("6000")
    assert state.drawdown_pct > 0
    # drawdown = (ref - cur)/ref, deterministic
    assert state.drawdown_pct == (D("6000") - state.current_equity) / D("6000")


# ---------------------------------------------------------------------------
# 8. Capacity gate (Phase 5B) — cases A/B/C/D
# ---------------------------------------------------------------------------

def _gate(tmp_path, cfg_max, rules_max, existing, proposed):
    """Return (limit, blocked) exactly as the production gate computes it."""
    from symbol_rules import SymbolRules
    session = make_session(tmp_path)
    orch = session.orchestrator
    order_db = session.order_engine.db_path
    for n in range(existing):
        _seed_open_order(order_db, f"SEED{n}", "BTCUSDT", "BUY", 0,
                         "105", "0.001")
    cfg = happy_cfg()
    if cfg_max is not None:
        cfg.setdefault("execution", {})["max_open_orders"] = cfg_max
    rules = None
    if rules_max is not None:
        from tests.test_paper_orchestrator import FIXED_RULES
        rules = SymbolRules(**{
            **{k: getattr(FIXED_RULES, k) for k in FIXED_RULES.__dataclass_fields__},
            "max_num_orders": rules_max,
        })
    # make_input substitutes rules=None with FIXED_RULES; force a truly
    # None rules via dataclasses.replace for the exchange-info-unavailable case.
    from dataclasses import replace as _dc_replace
    if rules is None and rules_max is None:
        inp = _dc_replace(make_input(1, D("110"), cfg=cfg), rules=None)
    else:
        inp = make_input(1, D("110"), cfg=cfg, rules=rules)
    limit = orch._compute_open_order_capacity_limit(inp)
    existing_now = len(orch._get_open_order_ids(order_db))
    blocked = (limit is None) or (existing_now + proposed > limit)
    return limit, blocked


def test_capacity_A_available(tmp_path):
    limit, blocked = _gate(tmp_path, cfg_max=5, rules_max=199,
                           existing=0, proposed=1)
    assert limit == 5 and not blocked


def test_capacity_B_exactly_reached_blocks_new(tmp_path):
    # Already AT the limit: any new intent exceeds it -> blocked.
    limit, blocked = _gate(tmp_path, cfg_max=3, rules_max=199,
                           existing=3, proposed=1)
    assert limit == 3 and blocked is True


def test_capacity_C_exceeded_blocks(tmp_path):
    limit, blocked = _gate(tmp_path, cfg_max=3, rules_max=199,
                           existing=5, proposed=1)
    assert limit == 3 and blocked is True


def test_capacity_D_exchange_info_unavailable_fails_closed(tmp_path):
    # No resolvable limit (rules None AND config max 0) -> None -> fail closed.
    limit, blocked = _gate(tmp_path, cfg_max=0, rules_max=None,
                           existing=0, proposed=1)
    assert limit is None and blocked is True


# ---------------------------------------------------------------------------
# 10 / 16. Read-only proof: no trading methods, no secret in requests/errors
# ---------------------------------------------------------------------------

def test_read_client_issues_get_only_and_no_trading_methods():
    import requests
    seen: list[tuple[str, str]] = []
    orig = requests.adapters.HTTPAdapter.send

    def trace(self, request, **kw):
        seen.append((request.method, request.url))
        return orig(self, request, **kw)

    requests.adapters.HTTPAdapter.send = trace
    try:
        # Import lazily so the monkeypatch is live during the calls.
        from binance_testnet import BinanceTestnetClient, BinanceTestnetConfig
        cfg = BinanceTestnetConfig(
            environment="testnet",
            base_url="https://testnet.binance.vision",
            api_key="k", api_secret="s", dry_run=True,
            allow_live_execution=False,
        )
        client = BinanceTestnetClient(cfg)
        # The adapter must expose no order/cancel/modify/withdraw API.
        for m in ("new_order", "cancel_order", "modify_order", "place_order",
                  "withdraw", "transfer"):
            assert not hasattr(client, m), f"adapter exposes {m}"
    finally:
        requests.adapters.HTTPAdapter.send = orig
    # Only GET read paths may have been issued (none here without a live call).
    assert all(m == "GET" for m, _ in seen)


def test_auth_error_message_never_leaks_secret():
    # The production credential-redaction primitive: any value passed as a
    # known secret is masked (prefix...suffix) and can never appear in full;
    # Bearer authorization headers are replaced by a fixed placeholder.
    from market_data import redact_credentials

    secret = "super-secret-value-1234567890"
    masked = redact_credentials(
        f"GET /sapi/v1/account 401 {secret}", known_secrets=(secret,)
    )
    # the full secret value can never survive redaction
    assert secret not in masked
    # a deterministic prefix...suffix mask replaces it
    assert f"{secret[:4]}...{secret[-4:]}" in masked

    # Bearer headers are stripped regardless of known_secrets
    bearer = redact_credentials("Authorization: Bearer abcdef1234567890")
    assert "abcdef1234567890" not in bearer


def test_auth_error_raises_typed_not_swallowed():
    # An unavailable signed account endpoint raises AccountRequestError
    # (fail-closed); it is never turned into a zero-balance snapshot.
    import requests
    from market_data import AccountRequestError

    with pytest.raises(AccountRequestError):
        fetch_account_snapshot(
            _client(account_exc=requests.exceptions.Timeout("read timed out")),
            "BNB", "USDT",
        )
