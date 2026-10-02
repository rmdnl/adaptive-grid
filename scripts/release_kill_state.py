"""Operator command: reconcile and release the persisted kill state.

F-H2 safety model:
* While the kill state is ACTIVE the bot places no new orders and every
  restart re-enters the kill branch.  The kill state is only ever cleared by
  this explicit, audited operator command — never automatically.
* A release is REFUSED (fail-closed) while any open local order is not yet
  reconciled as canceled: unknown/failed cancels must be resolved first,
  because releasing with unknown exposure is exactly the failure the kill
  switch exists to prevent.
* Per-order reconciliation: ``--reconcile <client_order_id>`` records the
  operator's exchange-side confirmation that an order is gone (transitioning
  it locally to CANCELED and releasing its remaining reservation).  An order
  that is FILLED can never be reconciled as canceled.

Safety:
* Paper/dry-run only (refused when the config is not explicitly dry-run with
  allow_live_execution=false).
* Every action is recorded in ``kill_state_audits``.
* Releasing does NOT place orders: the next run still has to pass the full
  risk gate; this command only removes the kill latch.

Usage:
    python scripts/release_kill_state.py --db <path> --reason "<why>" \
        [--actor <who>] [--reconcile AG-BTCUSDT-G00001-00000-B ...] \
        [--dry-run]

``--reconcile`` may be repeated for several client_order_ids.  The release
proceeds only when no open order remains unreconciled.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import init_db
from cancel_controller import CancelController, ReleaseBlockedError


def _read_env_from_config_file(path: str) -> dict:
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("config is not a mapping")
    return data


def _is_paper_dry_run(cfg: dict) -> bool:
    env = cfg.get("environment", {})
    return bool(env.get("dry_run")) and not bool(env.get("allow_live_execution", False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Explicitly reconcile and release the persisted kill state "
                    "(paper/dry-run only).",
    )
    parser.add_argument("--db", required=True, help="Path to the bot SQLite DB")
    parser.add_argument("--config", default="config.yaml",
                        help="Config path used to verify paper/dry-run mode")
    parser.add_argument("--reason", required=True,
                        help="Non-empty operator reason (recorded in the audit log)")
    parser.add_argument("--actor", default="operator",
                        help="Operator identity recorded in the audit log")
    parser.add_argument("--reconcile", action="append", default=[],
                        metavar="CLIENT_ORDER_ID",
                        help="Mark one open order as exchange-confirmed canceled "
                             "(repeatable)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the action without mutating the DB")
    args = parser.parse_args(argv)

    try:
        cfg = _read_env_from_config_file(args.config)
    except Exception as exc:
        print(f"REFUSE: cannot verify paper/dry-run mode from config ({exc})")
        return 1
    if not _is_paper_dry_run(cfg):
        print("REFUSE: available only in paper/dry-run mode "
              "(environment.dry_run=true and allow_live_execution=false).")
        return 1

    db_path = args.db
    init_db(db_path)

    # Reconstruct the minimal inputs the controller needs (rules + paper
    # balances).  A real DB path always has the orders table; rules come from
    # the symbol recorded on any order, with a paper-accounting fallback.
    from storage import connect
    con = connect(db_path)
    try:
        row = con.execute(
            "SELECT symbol FROM orders LIMIT 1"
        ).fetchone()
        order_symbol = row["symbol"] if row else None
        acct = con.execute(
            "SELECT base_asset, quote_asset FROM paper_account_state WHERE id=1"
        ).fetchone()
    finally:
        con.close()

    rules = None
    if order_symbol:
        # Local DB only; no exchange call.  The controller only uses the
        # asset names for accounting state matching.
        rules = _LocalRules(order_symbol, acct)
    elif acct:
        rules = _LocalRules("", acct)
    else:
        # No orders and no accounting state: nothing to reconcile.  The
        # release is allowed only if no kill latch exists OR it can be
        # released cleanly (no open orders by construction).
        rules = _LocalRules("", None)

    controller = CancelController(db_path, cfg, rules)

    # Current latch state, for reporting.
    from storage import get_kill_state
    latch = get_kill_state(db_path)
    if latch is None or not latch.get("active"):
        print("KILL STATE: not active. Nothing to release.")
        return 0

    pending = [o.intent.client_order_id for o in controller.unreconciled_open_orders()]

    if args.dry_run:
        print(f"[DRY] kill state active (trigger={latch.get('trigger')})")
        for cid in args.reconcile:
            print(f"[DRY] would reconcile {cid} as exchange-canceled")
        still_pending = [c for c in pending if c not in args.reconcile]
        if still_pending:
            print(f"[DRY] release REFUSED: unreconciled orders remain: "
                  f"{still_pending}")
        else:
            print(f"[DRY] would release kill state; reason={args.reason!r} "
                  f"actor={args.actor!r}")
        return 0

    # 1. Reconcile operator-confirmed cancellations.
    for cid in args.reconcile:
        result = controller.reconcile_cancelled(
            cid, confirmed_canceled=True, note="operator-confirmed",
        )
        print(f"RECONCILE: {cid} -> {result.outcome} "
              f"(state {result.local_state_before} -> {result.local_state_after}, "
              f"reservation_released={result.reservation_released})")

    # 2. Release the latch.  Fails closed while any open order is
    #    unreconciled — the operator must reconcile every open order first.
    try:
        controller.release(
            reason=args.reason,
            actor=args.actor,
        )
    except ReleaseBlockedError as exc:
        print(f"RELEASE REFUSED: {exc}")
        return 1

    print("RELEASE OK: kill state inactive; next run still requires a "
          "PASSING risk gate to place orders.")
    print(f"  reason: {args.reason.strip()}")
    print(f"  actor : {(args.actor.strip() or 'operator')}")
    return 0


class _LocalRules:
    """Minimal rules shim for the controller's accounting-state match.

    The release path only needs the base/quote asset names to build a
    PaperAccountingEngine; no exchange filters are required because no new
    orders are ever placed here.
    """

    def __init__(self, symbol, acct_row):
        self.symbol = symbol or ""
        if acct_row is not None:
            self.base_asset = acct_row["base_asset"]
            self.quote_asset = acct_row["quote_asset"]
        else:
            self.base_asset = "BASE"
            self.quote_asset = "QUOTE"

    # The controller only reads .base_asset / .quote_asset on rules.
    def __getattr__(self, name):
        raise AttributeError(f"unsupported rules attribute {name!r}")


if __name__ == "__main__":
    sys.exit(main())
