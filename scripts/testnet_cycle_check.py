"""Round 8 — bounded continuous testnet cycle CLI.

Gated, bounded, controllable: this is NOT a 24/7 order loop.

Modes:
    rehearsal (default)  full cycle, read-only — intents computed, nothing
                         submitted.  Requires no order gate.
    orders               full cycle WITH placement on Binance Spot TESTNET.
                         Requires TESTNET_ORDERS_ENABLED=true.

    --cleanup-only       reconcile + cancel + zero-open-order proof, then exit.
    --status             print the persisted ledger status.

Safety:
* Testnet URL/config barrier re-asserted by every client.
* Cycles are bounded (1..100) and each cycle ends flat (verified cancel).
* Cleanup fails with the exact unresolved ids when zero open orders
  cannot be proven; foreign orders are never touched.
* No secrets are printed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config_loader  # noqa: E402
from binance_testnet import (  # noqa: E402
    BinanceTestnetClient,
    BinanceTestnetConfigError,
    load_testnet_config_from_env,
)
from shutdown import ShutdownCoordinator, install_signal_handlers  # noqa: E402
from testnet_cycle import (  # noqa: E402
    TestnetCycleConfigError,
    TestnetCycleError,
    load_cycle_config,
)
from testnet_orders import (  # noqa: E402
    BinanceTestnetOrderClient,
    load_testnet_orders_enabled_from_env,
)


def _build_runner(args, *, place: bool):
    repo = Path(__file__).resolve().parent.parent
    cfg = config_loader.load_config(str(repo / "config.yaml"))
    config_loader.validate_config(cfg)
    adapter_cfg = load_testnet_config_from_env()
    read_client = BinanceTestnetClient(adapter_cfg)
    order_client = None
    if place:
        if not load_testnet_orders_enabled_from_env():
            raise TestnetCycleConfigError(
                "orders mode requires TESTNET_ORDERS_ENABLED=true "
                "(fail closed)")
        order_client = BinanceTestnetOrderClient(
            adapter_cfg, orders_enabled=True)
    config = load_cycle_config(
        cfg,
        symbol=args.symbol,
        max_cycles=args.cycles,
        poll_interval_s=args.interval,
        max_orders_per_cycle=args.max_orders,
        db_path=args.db,
    )
    coordinator = ShutdownCoordinator()
    install_signal_handlers(coordinator)
    from testnet_cycle import CycleLedger, TestnetCycleRunner
    import time as _time
    ledger = CycleLedger(config.db_path,
                         clock_ms=lambda: int(_time.time() * 1000))
    runner = TestnetCycleRunner(
        config,
        read_client=read_client,
        order_client=order_client,
        ledger=ledger,
        shutdown=coordinator,
        config_json=json.dumps(
            {"mode": args.mode, "cycles": args.cycles,
             "symbol": config.symbol}, sort_keys=True),
    )
    return runner


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bounded continuous testnet cycle (gated, fail-closed).")
    parser.add_argument("--mode", choices=["rehearsal", "orders"],
                        default="rehearsal")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--cycles", type=int, default=2)
    parser.add_argument("--interval", type=float, default=5.0,
                        help="seconds between cycles")
    parser.add_argument("--max-orders", type=int, default=2,
                        help="max orders per cycle")
    parser.add_argument("--db", default="./data/testnet_cycle.sqlite3")
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    try:
        if args.status:
            runner = _build_runner(args, place=False)
            status = runner.status()
            print(json.dumps(status, indent=2, sort_keys=True, default=str)
                  if args.as_json else _human_status(status))
            return 0

        if args.cleanup_only:
            place = args.mode == "orders"
            runner = _build_runner(args, place=place)
            cleanup = runner.cleanup()
            _emit(cleanup, args.as_json)
            return 0 if cleanup.get("ok") else 1

        place = args.mode == "orders"
        runner = _build_runner(args, place=place)
        summary = runner.run(place_orders=place,
                             config_json=json.dumps(
                                 {"mode": args.mode}, sort_keys=True))
        _emit(summary, args.as_json)
        cleanup = summary.get("cleanup") or {}
        ok = summary.get("stopped_reason") is None and cleanup.get("ok")
        return 0 if ok else 1
    except (TestnetCycleConfigError, TestnetCycleError,
            BinanceTestnetConfigError) as exc:
        print(f"CYCLE: FAIL reason={type(exc).__name__}: {exc}")
        return 1


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _human_status(status: dict) -> str:
    lines = [
        f"symbol:          {status['symbol']}",
        f"kill_active:     {status['kill_active']}",
        f"reference_eq:    {status['reference_equity']}",
        f"orders:          {len(status['orders'])}",
        f"non_terminal:    {status['non_terminal']}",
        f"events:          {len(status['events'])}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
