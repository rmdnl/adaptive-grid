"""Production runtime for the adaptive-grid bot (TESTNET/PAPER only).

A long-running orchestration wrapper that repeatedly invokes the existing
authoritative single-cycle entrypoint (``main.main``) at a controlled
cadence, with graceful SIGTERM/SIGINT shutdown and systemd compatibility.

Architecture rules (enforced by construction, see tests):

* **Orchestration only.**  This module places no orders, computes no grid,
  and touches no risk logic.  Every trading decision belongs to the
  existing ``main()`` cycle with its full gate stack (Risk Engine, kill
  switch, drawdown, lower-boundary, range-break, reconciliation).
* **Reuses the authoritative entrypoint.**  ``main()`` is re-entrant: it
  loads/validates config, verifies restart safety, honors the persisted
  kill latch, and is idempotent per closed candle (deterministic
  ``cycle_id`` replay) — so a restart or a repeat invocation on the same
  candle cannot create duplicate orders.
* **Closed-candle cadence.**  The runtime invokes the cycle once per NEW
  closed candle of the configured timeframe (15m by default, derived from
  the existing ``config.timeframe`` — no new trading timing model), plus
  bounded interval retries after a cycle did not succeed cleanly.  Within
  one candle it never re-invokes merely because time passed: same-candle
  re-invocation is left to the cycle's own idempotent replay and is not
  scheduled (so the daemon does not blindly execute against unchanged
  market state).  A configurable minimum interval paces wake-ups.
* **Fail-closed.**  An unexpected exception from the cycle is logged with
  a traceback and terminates the process with a nonzero exit so systemd
  (``Restart=on-failure``) restarts it fresh; no orders are invented or
  retried here.  A normal BLOCK decision is a healthy outcome and the
  daemon keeps monitoring.
* **Graceful shutdown.**  SIGTERM/SIGINT only flip the shutdown coordinator
  flag; the loop observes it at safe boundaries (between cycles and inside
  the sleep seam), never interrupting an in-flight cycle mid-operation.
  The coordinator's second-request ``forced`` semantics are preserved but
  nothing is ever aborted mid-cycle here.
* **No live capability.**  The runtime refuses to start unless the config
  is explicitly ``dry_run=true`` (``main()`` independently raises if it is
  ever false).  It never sets or changes environment safety flags and
  never touches ``TESTNET_ORDERS_ENABLED``.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from shutdown import ShutdownCoordinator, install_signal_handlers

logger = logging.getLogger("adaptive_grid.runtime")

#: Timeframe → seconds (read-only copy for scheduling; the timeframe itself
#: remains locked and validated by config_loader — 15m for this project).
_TIMEFRAME_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400,
}

#: Exit codes for the runtime process itself.
EXIT_OK = 0
EXIT_CYCLE_FAILURE = 1  # unexpected exception → systemd restart
EXIT_CONFIG = 2         # invalid/unsafe runtime configuration


class RuntimeConfigError(RuntimeError):
    """Runtime configuration invalid or unsafe.  Fail-closed."""


@dataclass(frozen=True)
class RuntimeConfig:
    """Validated runtime-only configuration (orchestration parameters —
    no strategy, risk, or execution parameters live here)."""

    enabled: bool
    interval_seconds: int
    boundary_grace_seconds: int
    timeframe_seconds: int
    dry_run: bool

    def __post_init__(self) -> None:
        if not self.dry_run:
            raise RuntimeConfigError(
                "runtime refuses to run when dry_run is not true "
                "(live execution is disabled)")
        if not self.enabled:
            raise RuntimeConfigError(
                "runtime.enabled is false — daemon start refused")
        if int(self.interval_seconds) != self.interval_seconds \
                or self.interval_seconds < 5:
            raise RuntimeConfigError(
                "runtime.interval_seconds must be an integer >= 5")
        if self.interval_seconds > 3600:
            raise RuntimeConfigError(
                "runtime.interval_seconds must be <= 3600")
        if int(self.boundary_grace_seconds) != self.boundary_grace_seconds \
                or self.boundary_grace_seconds < 0 \
                or self.boundary_grace_seconds > 300:
            raise RuntimeConfigError(
                "runtime.boundary_grace_seconds must be an integer "
                "in [0, 300]")
        if self.timeframe_seconds not in _TIMEFRAME_SECONDS.values():
            raise RuntimeConfigError(
                f"unsupported timeframe seconds: {self.timeframe_seconds}")


def load_runtime_config(cfg: dict) -> RuntimeConfig:
    """Build the runtime config from the validated config dict.

    Only the new ``runtime:`` block is read; every safety flag stays
    exactly where it is (``environment.dry_run`` etc.) and is enforced,
    never modified.
    """
    block = cfg.get("runtime") or {}
    enabled = bool(block.get("enabled", True))
    interval = int(block.get("interval_seconds", 60))
    grace = int(block.get("boundary_grace_seconds", 15))
    timeframe = str(cfg["timeframe"]).lower()
    if timeframe not in _TIMEFRAME_SECONDS:
        raise RuntimeConfigError(f"unsupported timeframe: {timeframe!r}")
    return RuntimeConfig(
        enabled=enabled,
        interval_seconds=interval,
        boundary_grace_seconds=grace,
        timeframe_seconds=_TIMEFRAME_SECONDS[timeframe],
        dry_run=bool(cfg["environment"]["dry_run"]),
    )


def closed_boundary(epoch_s: float, timeframe_seconds: int) -> int:
    """Index of the most recently CLOSED candle at ``epoch_s``.

    Candle boundaries are deterministic wall-clock arithmetic: the candle
    opening at ``k * timeframe_seconds`` closes at ``(k+1) * timeframe_seconds``.
    The index advances exactly when a new closed candle exists.  This is a
    schedule hint ONLY — the cycle itself validates candle data fail-closed.
    """
    return int(epoch_s // timeframe_seconds)


class GridRuntime:
    """Long-running orchestration loop over the authoritative cycle."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        cycle: Callable[[], int],
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        shutdown: Optional[ShutdownCoordinator] = None,
        install_signals: Callable[[ShutdownCoordinator], None] =
            install_signal_handlers,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self._cycle = cycle
        self._clock = clock
        self._sleep = sleep
        self.shutdown = shutdown or ShutdownCoordinator()
        self._install_signals = install_signals
        self.log = log or logger
        if self.config.interval_seconds >= self.config.timeframe_seconds:
            self.log.warning(
                "RUNTIME CONFIG interval_seconds (%ds) >= timeframe (%ds): "
                "cycles will not run on every closed candle (documented "
                "limitation); use interval < timeframe for candle-aligned "
                "operation", self.config.interval_seconds,
                self.config.timeframe_seconds)
        #: candle-boundary index of the last cycle invocation
        self._last_invoked_boundary: Optional[int] = None
        self._cycle_count = 0

    # -- scheduling ----------------------------------------------------------
    def _wake_delay(self, now: float) -> float:
        """Seconds to sleep before the next cycle invocation.

        Waits for the NEXT closed-candle boundary (+ configured grace) when
        the current closed candle was already processed; never sleeps less
        than ``interval_seconds`` between invocations.
        """
        tf = self.config.timeframe_seconds
        boundary = closed_boundary(now, tf)
        next_boundary_s = (boundary + 1) * tf + self.config.boundary_grace_seconds
        delay = next_boundary_s - now
        # pace: never wake more often than the configured interval
        return max(delay, float(self.config.interval_seconds))

    def _should_invoke(self, now: float) -> tuple[bool, str]:
        boundary = closed_boundary(now, self.config.timeframe_seconds)
        if self._last_invoked_boundary is None:
            return True, "STARTUP"
        if boundary > self._last_invoked_boundary:
            return True, "NEW_CLOSED_CANDLE"
        return False, "SAME_CANDLE"

    # -- sleep seam ------------------------------------------------------------
    def _sleep_interruptible(self, seconds: float) -> None:
        """Sleep in <=1s slices so SIGTERM/SIGINT respond promptly; the
        shutdown flag is only observed at this safe boundary — a cycle in
        flight is never interrupted here."""
        remaining = seconds
        while remaining > 0 and not self.shutdown.is_requested:
            slice_s = min(1.0, remaining)
            self._sleep(slice_s)
            remaining -= slice_s

    # -- main loop ---------------------------------------------------------------
    def run(self, *, max_cycles: Optional[int] = None) -> int:
        """Run until shutdown (or ``max_cycles`` for bounded observation).

        Returns the process exit code: 0 on graceful stop, 1 when a cycle
        raised an unexpected exception (fail closed — systemd restarts).
        """
        self._install_signals(self.shutdown)
        self.log.info(
            "RUNTIME START mode=paper dry_run=true interval=%ds "
            "timeframe_s=%ds grace=%ds",
            self.config.interval_seconds, self.config.timeframe_seconds,
            self.config.boundary_grace_seconds)
        stop_reason = "SHUTDOWN_REQUESTED"
        try:
            while not self.shutdown.is_requested:
                now = self._clock()
                invoke, reason = self._should_invoke(now)
                if not invoke:
                    delay = self._wake_delay(now)
                    self.log.info("RUNTIME WAIT reason=%s sleep=%.0fs",
                                  reason, delay)
                    self._sleep_interruptible(delay)
                    continue
                self.log.info("CYCLE START n=%d reason=%s",
                              self._cycle_count + 1, reason)
                try:
                    result = self._cycle()
                except Exception:
                    # Requirement: unexpected exception → log, fail closed,
                    # exit nonzero so systemd restarts fresh.  No orders are
                    # invented or retried here.
                    self.log.exception(
                        "CYCLE UNEXPECTED EXCEPTION n=%d — failing closed; "
                        "no retry, no order invention; systemd may restart",
                        self._cycle_count + 1)
                    self.log.info("RUNTIME STOP reason=CYCLE_FAILURE")
                    return EXIT_CYCLE_FAILURE
                self._cycle_count += 1
                self._last_invoked_boundary = closed_boundary(
                    now, self.config.timeframe_seconds)
                if result == EXIT_OK:
                    self.log.info("CYCLE RESULT n=%d status=OK", self._cycle_count)
                else:
                    # Nonzero from the authoritative cycle (config error or
                    # restart-refusal): a fail-closed state, never a reason
                    # to invent work.  Keep monitoring at the poll interval.
                    self.log.warning(
                        "CYCLE BLOCKED n=%d exit=%d — cycle refused safely; "
                        "monitoring continues", self._cycle_count, result)
                if max_cycles is not None and self._cycle_count >= max_cycles:
                    stop_reason = "MAX_CYCLES"
                    break
            stop_reason = "SHUTDOWN_REQUESTED" if self.shutdown.is_requested \
                else stop_reason
        finally:
            self.shutdown.complete()
        self.log.info("RUNTIME STOP reason=%s cycles=%d",
                      stop_reason, self._cycle_count)
        return EXIT_OK


def main(argv: Optional[list[str]] = None) -> int:
    """Runtime process entrypoint (testnet/paper only)."""
    import argparse

    from dotenv import load_dotenv

    from config_loader import ConfigError, load_config, validate_config

    parser = argparse.ArgumentParser(
        description="Adaptive-grid continuous runtime (testnet/paper only).")
    parser.add_argument("--max-cycles", type=int, default=None,
                        help="stop after N cycles (bounded observation only)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    load_dotenv()
    try:
        cfg = load_config()
        validate_config(cfg)
        runtime_cfg = load_runtime_config(cfg)
    except (ConfigError, RuntimeConfigError) as exc:
        print(f"RUNTIME CONFIG BLOCK: {exc}")
        return EXIT_CONFIG

    # Belt and braces: the daemon is paper/testnet-only by construction.
    if not cfg["environment"]["dry_run"] or \
            cfg["environment"].get("allow_live_execution", True):
        print("RUNTIME CONFIG BLOCK: dry_run must be true and "
              "allow_live_execution false")
        return EXIT_CONFIG

    # Invoke the existing authoritative single-cycle entrypoint in-process.
    # Imported lazily so --help/config errors never touch trading modules.
    from main import main as cycle_main

    runtime = GridRuntime(runtime_cfg, cycle=cycle_main)
    return runtime.run(max_cycles=args.max_cycles)


if __name__ == "__main__":
    raise SystemExit(main())
