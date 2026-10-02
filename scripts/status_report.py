"""Operator command: print a structured, read-only health / status report.

Surfaces the bot's persisted operational state (kill latch, reconciliation
health, reference-equity validity, local order counts, last risk / plan /
cycle decisions, and the last-run marker) without touching any trading
logic.  Exits 0/1/2 by operational status so it can be wired into a
supervisor or cron.

Safety: read-only.  Refuses to run unless the config is explicitly
``dry_run=true`` AND ``allow_live_execution=false`` (fail-closed, mirroring
the other operator commands).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from health import collect_health_report, write_health_jsonl
from runstate import has_paper_activity, verify_restart_safety
from storage import init_db


def _is_paper_dry_run(cfg: dict) -> bool:
    env = cfg.get("environment", {})
    return bool(env.get("dry_run")) and not bool(env.get("allow_live_execution", False))


def _read_cfg(path: str) -> dict:
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("config is not a mapping")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print a structured, read-only health/status report "
                    "(paper/dry-run only).",
    )
    parser.add_argument("--db", required=True, help="Path to the bot SQLite DB")
    parser.add_argument("--config", default="config.yaml",
                        help="Config path (used to verify paper/dry-run mode)")
    parser.add_argument("--log", default=None,
                        help="Optional log path; when given, the JSON health "
                             "record is also appended to <log>.jsonl")
    parser.add_argument("--json", action="store_true",
                        help="Emit the machine-readable JSON payload instead "
                             "of a human table")
    args = parser.parse_args(argv)

    try:
        cfg = _read_cfg(args.config)
    except Exception as exc:
        print(f"REFUSE: cannot read config ({exc})")
        return 1
    if not _is_paper_dry_run(cfg):
        print("REFUSE: available only in paper/dry-run mode "
              "(environment.dry_run=true and allow_live_execution=false).")
        return 1

    db_path = args.db
    init_db(db_path)

    restart = verify_restart_safety(db_path)
    report = collect_health_report(db_path, cfg)

    # Mirror the run-phase into the health sidecar's last_run_state read.
    if args.log:
        write_health_jsonl(args.log, {**report.as_dict(), "restart": restart})

    if args.json:
        print(json.dumps(
            {**report.as_dict(), "restart": restart},
            sort_keys=True, indent=2, default=str,
        ))
    else:
        print("Health / status report (read-only, paper mode)")
        print(f"  Status        : {report.status.value}"
              f"  (exit {report.status.exit_code()})")
        print(f"  Environment   : mode={report.environment['mode']} "
              f"dry_run={report.environment['dry_run']} "
              f"allow_live={report.environment['allow_live_execution']}")
        # A fresh DB with no paper activity reports "effective healthy" even
        # though the raw reconciliation lists MISSING_ACCOUNT_STATE (the
        # account row does not exist yet).  Show which view applies so the
        # operator is not misled by the raw error count.
        raw_errors = len(report.recovery["errors"])
        raw_healthy = report.recovery.get("raw_healthy", report.recovery["healthy"])
        fresh_no_activity = (not raw_healthy and not has_paper_activity(db_path))
        print(f"  Recovery      : healthy={report.recovery['healthy']} "
              f"(effective) raw={raw_healthy} raw_errors={raw_errors}"
              f"{' [fresh: no paper activity yet]' if fresh_no_activity else ''}")
        if report.recovery["errors"] and not fresh_no_activity:
            for err in report.recovery["errors"]:
                print(f"    - {err}")
        kill = report.kill_state
        if kill:
            print(f"  Kill state    : active={kill['active']} "
                  f"trigger={kill.get('trigger')} "
                  f"cancel_status={kill.get('cancel_status')}")
        else:
            print("  Kill state    : none")
        ref = report.reference_equity
        print(f"  Reference     : present={ref['present']} valid={ref['valid']} "
              f"value={ref['value']}")
        oo = report.open_orders
        print(f"  Open orders   : {oo['open']} "
              f"(by_status={oo['by_status']})")
        print(f"  Pending cancel: {list(report.pending_cancels) or 'none'}")
        print(f"  Restart       : action={restart['restart_action']} "
              f"previous_phase={restart['previous_run']['phase']} "
              f"kill_active={restart['kill_active']}")
        if report.last_risk_decision is not None:
            print(f"  Last risk     : allowed={report.last_risk_decision.get('allowed')} "
                  f"reason={report.last_risk_decision.get('reason')}")
        if report.last_paper_cycle is not None:
            cyc = report.last_paper_cycle
            print(f"  Last cycle    : submitted={cyc.get('orders_submitted')} "
                  f"skipped={cyc.get('orders_skipped')} "
                  f"success={cyc.get('success')}")
        print("  Execution     : read-only; no order placement")

    return report.status.exit_code()


if __name__ == "__main__":
    sys.exit(main())
