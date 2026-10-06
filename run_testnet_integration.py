#!/usr/bin/env python3
"""TESTNET Integration Gate — real production-cycle execution.

Runs the ACTUAL production runtime (Bot.run_once) for one or more cycles
against Binance Spot TESTNET. Every gate the runtime enforces is exercised
exactly as in production: market-data freshness, 15m boundary, cooldown,
strategy entry/exit, risk veto, adaptive planner, grid economics, order
lifecycle and reconciliation. Nothing is bypassed.

Does NOT modify production code. Does NOT commit/push/deploy.
LIVE gates remain disabled (ALLOW_LIVE_EXECUTION=false).

Verdict semantics (a gate that cannot fail is decoration):
  FAIL  — global kill latched, any symbol ERROR, or reconciliation not clean
  PASS  — runtime completed its cycle(s); symbols legitimately without
          entry conditions (WAITING / GRID_BLOCKED / COOLDOWN) are a PASS
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from bot import build_runtime, load_config
from config import Config
from exchange import BinanceSpot
from state import StateStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("testnet_integration")

REPORT_FILE = Path("testnet_integration_report.txt")


class IntegrationReporter:
    """Collects and formats integration test results."""

    def __init__(self):
        self.lines = []

    def add(self, line: str):
        self.lines.append(line)
        print(line)

    def section(self, title: str):
        self.add("")
        self.add("=" * 70)
        self.add(f" {title}")
        self.add("=" * 70)

    def kv(self, key: str, value: str):
        self.add(f"  {key}: {value}")

    def write_file(self):
        with open(REPORT_FILE, "w") as f:
            f.write("\n".join(self.lines))
        log.info("Integration report written to %s", REPORT_FILE)


def run_integration_test(cycles: int = 1) -> bool:
    reporter = IntegrationReporter()
    failures: list = []

    # ==================================================
    # 1. PRE-FLIGHT
    # ==================================================
    reporter.section("1. PRE-FLIGHT CONFIGURATION VERIFICATION")

    cfg = load_config()

    reporter.kv("BINANCE_ENV", cfg.binance_env)
    reporter.kv("EXECUTION_MODE", cfg.execution_mode)
    reporter.kv("DRY_RUN", str(cfg.dry_run))
    reporter.kv("ALLOW_LIVE_EXECUTION", str(cfg.allow_live_execution))
    reporter.kv("PAIR_LIST", str(cfg.pair_list))
    reporter.kv("INDICATOR_TIMEFRAME", cfg.indicator_timeframe)
    reporter.kv("ADAPTIVE_GRID", str(cfg.adaptive_grid))
    reporter.kv("MIN_GRIDS", str(cfg.min_grids))
    reporter.kv("MAX_GRIDS", str(cfg.max_grids))
    reporter.kv("QUOTE_RESERVE_PERCENT", f"{cfg.quote_reserve_percent}%")
    reporter.kv("MAX_QUOTE_ALLOCATION_PERCENT", f"{cfg.max_quote_allocation_percent}%")
    reporter.kv("GRID_STEP_ATR_MULTIPLIER", str(cfg.grid_step_atr_multiplier))
    reporter.kv("GRID_GROSS_MIN", f"{cfg.grid_gross_min:.4%}")
    reporter.kv("MIN_NET_PROFIT_PER_GRID", f"{cfg.min_net_profit_per_grid:.4%}")
    reporter.kv("STOP_IF_BELOW_LOWER", f"{cfg.stop_if_below_lower:.2%}")
    reporter.kv("MAX_DRAWDOWN", f"{cfg.max_drawdown:.2%}")

    # Safety assertions — the harness refuses to run outside the testnet gates
    assert cfg.binance_env == "testnet", f"Expected testnet, got {cfg.binance_env}"
    assert cfg.execution_mode == "testnet", f"Expected testnet, got {cfg.execution_mode}"
    assert cfg.dry_run is False, f"Expected DRY_RUN=false, got {cfg.dry_run}"
    assert cfg.allow_live_execution is False, f"Expected ALLOW_LIVE_EXECUTION=false, got {cfg.allow_live_execution}"
    assert cfg.indicator_timeframe == "1h", f"Expected 1h, got {cfg.indicator_timeframe}"
    assert cfg.adaptive_grid is True, f"Expected adaptive_grid=true, got {cfg.adaptive_grid}"

    reporter.add("")
    reporter.add("✓ PRE-FLIGHT PASSED - All safety gates verified")

    # ==================================================
    # 2. RUNTIME INITIALIZATION (production path)
    # ==================================================
    reporter.section("2. RUNTIME INITIALIZATION")

    store = StateStore("state.db", read_only=False)
    spot = BinanceSpot(cfg)

    try:
        usdt_balance = spot.get_balance("USDT")
        reporter.kv("USDT Balance (TESTNET)", f"{usdt_balance:.2f}")
        assert usdt_balance > 0, "Insufficient TESTNET USDT balance"
    except Exception as e:
        reporter.add(f"✗ FAIL: Exchange connectivity failed: {e}")
        reporter.write_file()
        return False

    # Build runtime (TESTNET mode -> LiveExecutor) exactly as production does
    bot, market = build_runtime(cfg, store, spot)
    reporter.kv("Executor Mode", bot.executor.mode)
    reporter.kv("Risk Engine", "RiskEngine (global DD + 15m boundary)")
    reporter.add("✓ Runtime initialized successfully")

    # ==================================================
    # 3. PRODUCTION CYCLE (no gates bypassed)
    # ==================================================
    reporter.section(f"3. PRODUCTION CYCLE EXECUTION ({cycles} cycle(s))")

    for i in range(cycles):
        if i:
            reporter.add("")
            reporter.add(f"  --- cycle {i + 1}/{cycles}: sleeping 30s between cycles ---")
            time.sleep(30)
        reporter.add("")
        reporter.add(f"--- cycle {i + 1}/{cycles}: bot.run_once() ---")
        bot.run_once()

    # ==================================================
    # 4. POST-CYCLE STATE VERIFICATION
    # ==================================================
    reporter.section("4. POST-CYCLE STATE VERIFICATION")

    kill_active, kill_reason = store.global_kill()
    reporter.kv("Global Kill", f"{kill_active} ({kill_reason})" if kill_active else "inactive")
    if kill_active:
        failures.append(f"global kill latched: {kill_reason}")

    for symbol in cfg.pair_list:
        st = store.get_symbol(symbol)
        if not st:
            failures.append(f"{symbol}: no symbol row")
            continue
        reporter.add("")
        reporter.add(f"Symbol: {symbol}")
        reporter.kv("State", st.strategy_state)
        reporter.kv("Risk", st.risk_status)
        reporter.kv("Last Price (live)", f"{st.last_price:.8f}" if st.last_price else "N/A")
        reporter.kv("Inventory", f"{st.inventory_qty:.8f}")
        reporter.kv("Avg Cost", f"{st.avg_cost:.8f}" if st.avg_cost else "N/A")
        reporter.kv("Open Orders", str(store.count_open_orders(symbol)))
        reporter.kv("Completed Grids", str(store.count_completed_grids(symbol)))
        reporter.kv("Exit", f"{st.exit_status} ({st.exit_reason})" if st.exit_reason else "none")
        if st.entry_blocker:
            reporter.kv("Entry Blocker", st.entry_blocker)
        if st.block_reason:
            reporter.kv("Block Reason", st.block_reason)
        if st.adaptive_lower_price is not None:
            reporter.kv("Adaptive Lower (state)", f"{st.adaptive_lower_price:.8f}")
            reporter.kv("Adaptive Upper (state)", f"{st.adaptive_upper_price:.8f}")
            reporter.kv("Adaptive Grids (state)", str(st.adaptive_total_grids))
            reporter.kv("Adaptive Budget (state)", f"{st.adaptive_quote_budget:.2f}")

        # Fail-closed verdicts
        if st.risk_status == "error":
            failures.append(f"{symbol}: risk_status=error (state={st.strategy_state})")
        if st.strategy_state == "STOPPED":
            failures.append(f"{symbol}: STOPPED ({st.exit_reason})")

    # Ledger consistency: inventory must equal BUY qty - SELL qty.
    for symbol in cfg.pair_list:
        st = store.get_symbol(symbol)
        if not st:
            continue
        buy_qty, sell_qty = store.fill_quantities(symbol)
        expected = buy_qty - sell_qty
        if abs(float(st.inventory_qty or 0.0) - expected) > 1e-9:
            failures.append(
                f"{symbol}: ledger mismatch inventory={st.inventory_qty} expected={expected}"
            )

    # ==================================================
    # 5. RECONCILIATION TEST
    # ==================================================
    reporter.section("5. RESTART RECONCILIATION TEST")

    total_open = store.count_open_orders()
    reporter.kv("Open orders", str(total_open))
    if total_open > 0:
        from bot import _reconcile_state
        from io import StringIO
        output = StringIO()
        result = _reconcile_state(cfg, spot, store, out=output)
        reporter.add(output.getvalue())
        reporter.kv("Reconciliation Exit Code", str(result))
        if result != 0:
            failures.append("reconciliation not clean (exit != 0)")

    # ==================================================
    # 6. SAFETY VERIFICATION
    # ==================================================
    reporter.section("6. SAFETY VERIFICATION")

    reporter.kv("BINANCE_ENV", cfg.binance_env)
    reporter.kv("EXECUTION_MODE", cfg.execution_mode)
    reporter.kv("DRY_RUN", str(cfg.dry_run))
    reporter.kv("ALLOW_LIVE_EXECUTION", str(cfg.allow_live_execution))
    reporter.kv("BINANCE_TESTNET_API_KEY configured", "YES" if cfg.api_credentials[0] else "NO")

    assert cfg.binance_env == "testnet", "LIVE environment detected!"
    assert cfg.allow_live_execution is False, "Live execution gate unexpectedly enabled!"
    reporter.add("✓ No mainnet orders possible - gates verified")

    # ==================================================
    # 7. VERDICT (a gate that cannot fail is decoration)
    # ==================================================
    reporter.section("7. FINAL VERDICT")

    reporter.add("")
    reporter.add("SYMBOL RESULTS:")
    for symbol in cfg.pair_list:
        st = store.get_symbol(symbol)
        if st:
            reporter.add(
                f"  {symbol}: state={st.strategy_state} risk={st.risk_status} "
                f"orders={store.count_open_orders(symbol)} inv={st.inventory_qty:.8f}"
            )

    if failures:
        reporter.add("")
        reporter.add("FAILURES:")
        for f in failures:
            reporter.add(f"  ✗ {f}")
        verdict = "FAIL"
    else:
        reporter.add("")
        reporter.add("No fail-closed conditions detected.")
        verdict = "PASS"

    reporter.add("")
    reporter.add(f"FINAL VERDICT: {verdict}")
    reporter.add("")

    reporter.write_file()
    return verdict == "PASS"


if __name__ == "__main__":
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    try:
        success = run_integration_test(cycles=cycles)
        sys.exit(0 if success else 1)
    except Exception as e:
        log.exception("Integration test failed")
        with open(REPORT_FILE, "a") as f:
            f.write(f"\n\nFATAL ERROR: {e}\n")
        sys.exit(1)
