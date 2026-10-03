"""Round 7 — Binance Spot TESTNET order-path end-to-end verification.

Exercises the skill §21 checklist against the real testnet:
connectivity, clock skew, symbol filters, balance retrieval, order
creation (LIMIT_MAKER only), authoritative resolution, open-order
reconciliation, verified cancellation, post-cancel re-resolution,
in-process restart recovery, duplicate-clientOrderId prevention.

Safety:
* Testnet only — every client re-asserts the isolation barrier.
* Order placement requires BOTH the env gate (TESTNET_ORDERS_ENABLED=true)
  AND the explicit ``--place-order`` flag.  Without it the script is
  strictly read-only.
* LIMIT_MAKER only, exchange filters validated before submission,
  fail-closed on every UNKNOWN outcome, exit 0 only if ALL steps pass.
* Never prints credentials or secret material.

Modes:
    python scripts/testnet_order_path_check.py
        read-only verification (no orders).

    python scripts/testnet_order_path_check.py --place-order
        full order-path verification (requires TESTNET_ORDERS_ENABLED=true).

    python scripts/testnet_order_path_check.py --verify-cid AGTV-...
        fresh-process resolution of a prior order (restart-recovery proof).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from binance_testnet import (
    BinanceTestnetConfigError,
    BinanceTestnetError,
    BinanceTestnetClient,
    load_testnet_config_from_env,
)
from rest_reconciler import (
    CancelVerdict,
    Outcome,
    RestReconciler,
)
from symbol_rules import (
    SymbolRuleError,
    parse_symbol_info,
    quantize_price,
    validate_notional,
    validate_percent_price,
)
from testnet_orders import (
    BinanceTestnetOrderClient,
    BinanceTestnetOrderRejectedError,
    load_testnet_orders_enabled_from_env,
)

#: Fraction below the last price where the verification BUY rests.
#: Far enough to not cross the spread, well inside any PERCENT_PRICE band.
_BUY_OFFSET_PCT = Decimal("0.005")
#: Notional target multiplier over the symbol minimum (fee/slippage headroom).
_NOTIONAL_BUFFER = Decimal("1.5")
#: Quote-balance headroom required above the order notional.
_BALANCE_BUFFER = Decimal("1.01")


class Check:
    """One recorded verification step."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.status = "FAIL"
        self.detail = ""

    def ok(self, detail: str = "") -> "Check":
        self.status, self.detail = "PASS", detail
        return self

    def fail(self, detail: str) -> "Check":
        self.status, self.detail = "FAIL", detail
        return self


def _print_checks(checks: list[Check], as_json: bool) -> None:
    if as_json:
        payload = {
            c.name: {"status": c.status, "detail": c.detail} for c in checks
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for c in checks:
            print(f"[{c.status:4s}] {c.name}: {c.detail}")


def _resolve_symbol(symbol: str) -> str:
    return str(symbol).upper()


def _build_price_and_quantity(symbol: str, rules, ticker_price: Decimal):
    """Filter-safe LIMIT_MAKER BUY price/quantity, fail-closed."""
    raw_price = ticker_price * (Decimal("1") - _BUY_OFFSET_PCT)
    price = quantize_price(raw_price, rules)
    validate_percent_price(price, ticker_price, "BUY", rules)
    target_notional = rules.min_notional * _NOTIONAL_BUFFER if rules.min_notional > 0 \
        else Decimal("10")
    raw_qty = target_notional / price
    if rules.step_size > 0:
        qty = (raw_qty / rules.step_size).to_integral_value(
            rounding=ROUND_CEILING) * rules.step_size
    else:
        raise SymbolRuleError("stepSize must be > 0")
    if rules.min_qty > 0 and qty < rules.min_qty:
        qty = rules.min_qty
    notional = validate_notional(price, qty, rules)
    if rules.max_qty > 0 and qty > rules.max_qty:
        raise SymbolRuleError(f"quantity {qty} exceeds maxQty {rules.max_qty}")
    return price, qty, notional


def run_read_only(symbol: str, as_json: bool) -> int:
    checks: list[Check] = []
    try:
        config = load_testnet_config_from_env()
    except BinanceTestnetConfigError as exc:
        _print_checks([Check("config").fail(type(exc).__name__)], as_json)
        return 1
    try:
        client = BinanceTestnetClient(config)
        snap = client.connectivity_check(symbol)
        checks.append(Check("connectivity").ok(
            f"overall={snap.overall} ticker={snap.ticker_price} "
            f"skew_ms={snap.skew_ms}"
        ) if snap.overall == "PASS" else Check("connectivity").fail(snap.reason))
        if snap.overall != "PASS":
            _print_checks(checks, as_json)
            return 1

        reconciler = RestReconciler(client, sleep=time.sleep)
        skew = reconciler.sync_clock_skew(samples=3)
        checks.append(Check("clock_skew").ok(
            f"offset_ms={skew.offset_ms} samples={skew.samples}"
        ) if skew.established else Check("clock_skew").fail(skew.detail))

        rules_snap = client.symbol_snapshot(symbol)
        rules = parse_symbol_info(rules_snap.raw_exchange_info)
        checks.append(Check("symbol_rules").ok(
            f"tick={rules.tick_size} step={rules.step_size} "
            f"minNotional={rules.min_notional} status={rules_snap.status}"
        ))

        acct = client.account()
        bal = {b.asset: b for b in acct.balances}
        quote = bal.get(rules.quote_asset)
        base = bal.get(rules.base_asset)
        checks.append(Check("balances").ok(
            f"{rules.base_asset} free={base.free if base else 0} "
            f"{rules.quote_asset} free={quote.free if quote else 0}"
        ))

        open_before = client.open_orders(symbol)
        checks.append(Check("open_orders").ok(f"count={len(open_before)}"))

        _print_checks(checks, as_json)
        return 0 if all(c.status == "PASS" for c in checks) else 1
    except BinanceTestnetError as exc:
        checks.append(Check("read_only_suite").fail(type(exc).__name__))
        _print_checks(checks, as_json)
        return 1


def run_verify_cid(symbol: str, client_order_id: str, as_json: bool) -> int:
    """Fresh-process authoritative resolution of a prior order."""
    checks: list[Check] = []
    try:
        config = load_testnet_config_from_env()
        client = BinanceTestnetClient(config)
        reconciler = RestReconciler(client, sleep=time.sleep)
        st = reconciler.resolve_order(symbol, client_order_id)
        if st.outcome is Outcome.CONFIRMED and st.authoritative:
            checks.append(Check("restart_recovery_resolve").ok(
                f"status={st.status} exchange_order_id={st.exchange_order_id}"
            ))
            _print_checks(checks, as_json)
            return 0
        checks.append(Check("restart_recovery_resolve").fail(
            f"outcome={st.outcome.value} detail={st.detail}"
        ))
        _print_checks(checks, as_json)
        return 1
    except BinanceTestnetError as exc:
        checks.append(Check("restart_recovery_resolve").fail(type(exc).__name__))
        _print_checks(checks, as_json)
        return 1


def run_order_path(symbol: str, as_json: bool) -> int:
    checks: list[Check] = []
    normalized = _resolve_symbol(symbol)
    try:
        config = load_testnet_config_from_env()
    except BinanceTestnetConfigError as exc:
        _print_checks([Check("config").fail(type(exc).__name__)], as_json)
        return 1

    if not load_testnet_orders_enabled_from_env():
        checks.append(Check("orders_enabled_gate").fail(
            "TESTNET_ORDERS_ENABLED is not true — order path stays disabled"
        ))
        _print_checks(checks, as_json)
        return 1
    checks.append(Check("orders_enabled_gate").ok(
        "TESTNET_ORDERS_ENABLED=true (testnet-only, dry_run, no-live)"
    ))

    read_client = BinanceTestnetClient(config)
    snap = read_client.connectivity_check(normalized)
    if snap.overall != "PASS":
        checks.append(Check("connectivity").fail(snap.reason))
        _print_checks(checks, as_json)
        return 1
    checks.append(Check("connectivity").ok(
        f"ticker={snap.ticker_price} skew_ms={snap.skew_ms}"
    ))

    reconciler = RestReconciler(read_client, sleep=time.sleep)
    skew = reconciler.sync_clock_skew(samples=3)
    if not skew.established:
        checks.append(Check("clock_skew").fail(skew.detail))
        _print_checks(checks, as_json)
        return 1
    if abs(skew.offset_ms) > 2000:
        checks.append(Check("clock_skew").fail(
            f"|offset| {abs(skew.offset_ms)}ms exceeds 2000ms order-path bound"
        ))
        _print_checks(checks, as_json)
        return 1
    checks.append(Check("clock_skew").ok(f"offset_ms={skew.offset_ms}"))

    rules_snap = read_client.symbol_snapshot(normalized)
    if rules_snap.status.upper() != "TRADING":
        checks.append(Check("symbol_rules").fail(
            f"status={rules_snap.status}"
        ))
        _print_checks(checks, as_json)
        return 1
    rules = parse_symbol_info(rules_snap.raw_exchange_info)
    checks.append(Check("symbol_rules").ok(
        f"tick={rules.tick_size} step={rules.step_size} "
        f"minNotional={rules.min_notional}"
    ))

    ticker = read_client.ticker_price(normalized)
    acct = read_client.account()
    bal = {b.asset: b for b in acct.balances}
    quote_bal = bal.get(rules.quote_asset)
    if quote_bal is None or quote_bal.free <= 0:
        checks.append(Check("quote_balance").fail(
            f"no free {rules.quote_asset}"
        ))
        _print_checks(checks, as_json)
        return 1
    checks.append(Check("quote_balance").ok(
        f"{rules.quote_asset} free={quote_bal.free}"
    ))

    try:
        price, qty, notional = _build_price_and_quantity(
            normalized, rules, ticker.price
        )
    except SymbolRuleError as exc:
        checks.append(Check("order_construction").fail(str(exc)))
        _print_checks(checks, as_json)
        return 1
    required = notional * _BALANCE_BUFFER
    if quote_bal.free < required:
        checks.append(Check("order_construction").fail(
            f"required {required} {rules.quote_asset} > free {quote_bal.free} "
            "(fail-closed: not placing)"
        ))
        _print_checks(checks, as_json)
        return 1
    checks.append(Check("order_construction").ok(
        f"BUY {qty} {rules.base_asset} @ {price} "
        f"(notional={notional}, ticker={ticker.price})"
    ))

    open_before = read_client.open_orders(normalized)
    pre_local = {o.client_order_id: o.status for o in open_before}

    order_client = BinanceTestnetOrderClient(config, orders_enabled=True)

    def _resolve_status(sym: str, c_id: str):
        """Authoritative re-query used to settle ambiguous cancels (§5)."""
        payload = read_client.get_order(sym, c_id)
        return payload.get("status")

    executor = order_client.make_cancel_executor(resolver=_resolve_status)
    order_reconciler = RestReconciler(
        read_client, cancel_executor=executor, sleep=time.sleep
    )

    cid = f"AGTV-{normalized}-{int(time.time())}"
    try:
        ack = order_client.place_limit_maker_order(
            normalized, "BUY", qty, price, cid
        )
    except BinanceTestnetOrderRejectedError as exc:
        checks.append(Check("place_order").fail(
            f"deterministic rejection code={exc.code}"
        ))
        _print_checks(checks, as_json)
        return 1
    except BinanceTestnetError as exc:
        checks.append(Check("place_order").fail(
            f"UNKNOWN outcome: {type(exc).__name__} (resolve before retry)"
        ))
        # §4: resolve by cid — the order may exist despite the error.
        st = reconciler.resolve_order(normalized, cid)
        detail = f"status={st.status} outcome={st.outcome.value}"
        if st.authoritative and st.status in ("NEW", "PARTIALLY_FILLED"):
            # Best-effort cleanup so a failed verification leaves no open
            # order behind; a non-confirmed cancel is reported, not assumed.
            cleanup = order_reconciler.cancel(normalized, cid, verify=True)
            detail += f"; cleanup_cancel={cleanup.verdict.value}"
        checks.append(Check("post_error_resolution").fail(detail))
        _print_checks(checks, as_json)
        return 1
    checks.append(Check("place_order").ok(
        f"status={ack.status} order_id={ack.order_id} cid={cid}"
    ))

    if ack.status != "NEW":
        # FILLED/PARTIALLY_FILLED/EXPIRED: the cancel path cannot be
        # verified with this order this run — report fail-closed.
        st = reconciler.resolve_order(normalized, cid)
        checks.append(Check("immediate_fill_handling").fail(
            f"ack status={ack.status}; authoritative resolve={st.status} "
            "(re-run to verify the cancel path)"
        ))
        _print_checks(checks, as_json)
        return 1

    st = reconciler.resolve_order(normalized, cid)
    if not (st.outcome is Outcome.CONFIRMED and st.authoritative
            and st.status == "NEW"):
        checks.append(Check("resolve_open_order").fail(
            f"status={st.status} outcome={st.outcome.value} {st.detail}"
        ))
        _print_checks(checks, as_json)
        return 1
    checks.append(Check("resolve_open_order").ok(
        f"status=NEW exchange_order_id={st.exchange_order_id}"
    ))

    local_open = {**pre_local, cid: "NEW"}
    rec = order_reconciler.reconcile_open_orders(normalized, local_open)
    if not rec.authoritative:
        checks.append(Check("reconcile_open_orders").fail(
            "; ".join(rec.details)
        ))
        _print_checks(checks, as_json)
        return 1
    checks.append(Check("reconcile_open_orders").ok(
        f"exact match ({len(rec.matched)} orders)"
    ))

    # Duplicate prevention (while the order is OPEN): Binance rejects a
    # resubmission carrying the clientOrderId of a still-open order.  After
    # a CANCEL the id becomes reusable again, so this check must run before
    # the cancel step.
    duplicate_blocked = False
    try:
        order_client.place_limit_maker_order(
            normalized, "BUY", qty, price, cid
        )
        duplicate_blocked = False
    except BinanceTestnetOrderRejectedError as exc:
        duplicate_blocked = True
        dup_code = exc.code
    except BinanceTestnetError:
        duplicate_blocked = False
    open_mid = read_client.open_orders(normalized)
    if not duplicate_blocked:
        detail = (
            "resubmission with the open order's clientOrderId did not "
            "settle to a deterministic rejection"
        )
        # Best-effort cleanup of the unexpected duplicate (verify with the
        # §5 resolver so a lost ack still settles).
        cleanup = order_reconciler.cancel(normalized, cid, verify=True)
        detail += f"; cleanup_cancel={cleanup.verdict.value}"
        checks.append(Check("duplicate_cid_prevention").fail(detail))
        _print_checks(checks, as_json)
        return 1
    if len(open_mid) != len(open_before) + 1:
        checks.append(Check("duplicate_cid_prevention").fail(
            f"open-order count changed unexpectedly: "
            f"{len(open_before)} -> {len(open_mid)}"
        ))
        _print_checks(checks, as_json)
        return 1
    checks.append(Check("duplicate_cid_prevention").ok(
        f"same clientOrderId deterministically rejected (code={dup_code}); "
        f"open-order count unchanged ({len(open_mid)})"
    ))

    cancel_record = order_reconciler.cancel(normalized, cid, verify=True)
    if cancel_record.verdict is not CancelVerdict.CONFIRMED_CANCELED:
        # §5: a non-confirmed cancel settles only via authoritative re-query.
        settled = False
        detail = f"verdict={cancel_record.verdict.value} {cancel_record.detail}"
        try:
            final = read_client.get_order(normalized, cid)
            if final.get("status") == "CANCELED":
                settled = True
                detail = (
                    "unconfirmed cancel settled by authoritative re-query: "
                    f"CANCELED executedQty={final.get('executedQty')} "
                    f"(original: {cancel_record.detail})"
                )
        except BinanceTestnetError:
            settled = False
        if not settled:
            checks.append(Check("verified_cancel").fail(detail))
            _print_checks(checks, as_json)
            return 1
        checks.append(Check("verified_cancel").ok(detail))
    else:
        checks.append(Check("verified_cancel").ok(cancel_record.detail))

    st = reconciler.resolve_order(normalized, cid)
    if not (st.authoritative and st.status == "CANCELED"):
        checks.append(Check("post_cancel_status").fail(
            f"status={st.status} outcome={st.outcome.value}"
        ))
        _print_checks(checks, as_json)
        return 1
    checks.append(Check("post_cancel_status").ok("status=CANCELED"))

    # In-process restart recovery: brand-new client + reconciler instances
    # must still resolve the canceled order authoritatively.  (For a true
    # process-restart proof run --verify-cid in a new shell.)
    fresh_client = BinanceTestnetClient(load_testnet_config_from_env())
    fresh_reconciler = RestReconciler(fresh_client, sleep=time.sleep)
    st = fresh_reconciler.resolve_order(normalized, cid)
    if not (st.authoritative and st.status == "CANCELED"):
        checks.append(Check("restart_recovery_inprocess").fail(
            f"status={st.status} outcome={st.outcome.value}"
        ))
        _print_checks(checks, as_json)
        return 1
    checks.append(Check("restart_recovery_inprocess").ok(
        f"fresh instances resolve cid={cid} as CANCELED"
    ))

    _print_checks(checks, as_json)
    return 0 if all(c.status == "PASS" for c in checks) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Testnet order-path verification (LIMIT_MAKER, gated)."
    )
    parser.add_argument("--symbol", default="BNBUSDT")
    parser.add_argument(
        "--place-order", action="store_true",
        help="run the full order path; requires TESTNET_ORDERS_ENABLED=true",
    )
    parser.add_argument(
        "--verify-cid", metavar="CLIENT_ORDER_ID",
        help="fresh-process resolve of a prior order (restart recovery)",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    if args.verify_cid:
        return run_verify_cid(args.symbol, args.verify_cid, args.as_json)
    if args.place_order:
        return run_order_path(args.symbol, args.as_json)
    return run_read_only(args.symbol, args.as_json)


if __name__ == "__main__":
    sys.exit(main())
