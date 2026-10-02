"""Operator command: explicitly reset the persisted reference (peak) equity.

This is the ONLY supported way to change the drawdown reference apart from the
automatic high-water-mark raise performed on every healthy run.  The reset is
never automatic: it requires an explicit operator action with a non-empty
--reason, and it is recorded in the durable ``reference_equity_audits`` table
(timestamp, previous value, new value, reason, actor) so every change is
attributable.

Safety:
* Paper/dry-run only.  Refuses to run when ``environment.dry_run`` is false or
  ``allow_live_execution`` is true (there is no live-execution path anyway).
* A corrupt (unparsable) persisted reference is never silently repaired by this
  command: with no --value the corrupt value is simply cleared and the next run
  re-bootstraps; with an explicit --value the new value is written and the old
  corrupt value is recorded in the audit row.
* No other risk control is weakened.  The 2% drawdown kill still fires against
  whatever reference value is persisted afterwards.

Usage:
    python scripts/reset_reference_equity.py --db <sqlite_path> \
        [--value <number> | --clear] --reason "<why>" [--actor <who>]

    --value <number>   set the reference to this finite positive value
    --clear            forget the reference; the next run re-bootstraps it
                       from the first valid observed equity
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import clear_state, get_state, init_db, set_state


def _is_paper_dry_run(cfg: dict) -> bool:
    env = cfg.get("environment", {})
    dry_run = env.get("dry_run")
    allow_live = env.get("allow_live_execution", False)
    # Fail-closed: only a config that is explicitly dry-run AND explicitly
    # disallows live execution may use this command.
    return bool(dry_run) and not bool(allow_live)


def _read_env_from_config_file(path: str) -> dict:
    """Minimal, dependency-free read of the ``environment:`` block.

    The full config loader validates against YAML; this command only needs the
    two safety flags.  If the file is missing or the flags cannot be found, we
    fail closed and refuse to run.
    """
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("config is not a mapping")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Explicitly reset the persisted reference (peak) equity "
                    "(paper/dry-run only).",
    )
    parser.add_argument("--db", required=True, help="Path to the bot SQLite DB")
    parser.add_argument("--config", default="config.yaml",
                        help="Config path used to verify paper/dry-run mode")
    parser.add_argument("--value", help="New reference equity value (finite positive)")
    parser.add_argument("--clear", action="store_true",
                        help="Clear the reference; the next run re-bootstraps it")
    parser.add_argument("--reason", required=True,
                        help="Non-empty operator reason (recorded in the audit log)")
    parser.add_argument("--actor", default="operator",
                        help="Operator identity recorded in the audit log")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the action without mutating the DB")
    args = parser.parse_args(argv)

    # Safety gate: paper/dry-run only.
    try:
        cfg = _read_env_from_config_file(args.config)
    except Exception as exc:
        print(f"REFUSE: cannot verify paper/dry-run mode from config ({exc})")
        print("The reset is available only when the config is explicitly "
              "dry-run AND allow_live_execution=false.")
        return 1
    if not _is_paper_dry_run(cfg):
        print("REFUSE: this command is available only in paper/dry-run mode "
              "(environment.dry_run=true and allow_live_execution=false).")
        return 1

    # Exactly one of --value / --clear.
    if args.value is not None and args.clear:
        print("REFUSE: provide either --value or --clear, not both.")
        return 1
    if args.value is None and not args.clear:
        print("REFUSE: provide --value <number> or --clear.")
        return 1

    # Parse/validate the new value.
    new_value: Decimal | None = None
    if args.clear:
        new_value = None
    else:
        raw_value = args.value
        if raw_value is None:
            print("REFUSE: provide --value <number> or --clear.")
            return 1
        try:
            new_value = Decimal(str(raw_value))
        except (InvalidOperation, ValueError) as exc:
            print(f"REFUSE: --value must be a finite positive number ({exc})")
            return 1
        if not new_value.is_finite() or new_value <= 0:
            print("REFUSE: --value must be a finite positive number.")
            return 1

    db_path = args.db
    init_db(db_path)
    previous = get_state(db_path, "paper_reference_equity")

    if args.dry_run:
        print(f"[DRY] would {'clear' if new_value is None else 'set'} "
              f"paper_reference_equity: {previous!r} -> "
              f"{str(new_value) if new_value is not None else 'CLEARED'} "
              f"reason={args.reason!r} actor={args.actor!r}")
        return 0

    # Perform the state change.
    if new_value is None:
        clear_state(db_path, "paper_reference_equity")
        new_value_str = "CLEARED"
    else:
        set_state(db_path, "paper_reference_equity", str(new_value))
        new_value_str = str(new_value)

    # Durable, attributable audit row (same transactional layer as the state).
    from storage import record_reference_equity_audit
    record_reference_equity_audit(
        db_path,
        previous_value=previous,
        new_value=new_value_str,
        reason=args.reason.strip(),
        actor=args.actor.strip() or "operator",
    )

    print(f"RESET OK: paper_reference_equity {previous!r} -> {new_value_str}")
    print(f"  reason: {args.reason.strip()}")
    print(f"  actor : {args.actor.strip() or 'operator'}")
    print("  audit row recorded in reference_equity_audits")
    if new_value is None:
        print("  note: reference cleared; the next run re-bootstraps it from "
              "the first valid observed equity.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
