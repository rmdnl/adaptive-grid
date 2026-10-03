"""Runtime-layer tests — deterministic, no network, no orders.

The runtime is orchestration-only: every test drives it with an injected
cycle callable, clock, sleep, and signal-installer seam.  The tests pin:

* cadence (one invocation per new closed candle; never per sleep tick),
* graceful SIGTERM/SIGINT shutdown at safe boundaries,
* BLOCK decisions never terminate the daemon,
* unexpected exceptions fail closed with a nonzero exit,
* config safety (dry_run required; live impossible),
* restart/first-start cannot bypass the cycle's own gates (the runtime
  invokes the real main() entrypoint — its restart/kill gates are covered
  by the existing suite; here we prove the runtime adds no bypass).
"""
from __future__ import annotations

import logging
import pytest

import runtime as rt
from shutdown import ShutdownCoordinator


TF = 900  # 15m


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SleepRecorder:
    """Records sleep slices; can request shutdown mid-wait."""

    def __init__(self, coordinator: ShutdownCoordinator | None = None,
                 shutdown_after_total: float | None = None) -> None:
        self.slices: list[float] = []
        self.total = 0.0
        self.coordinator = coordinator
        self.shutdown_after_total = shutdown_after_total

    def __call__(self, seconds: float) -> None:
        self.slices.append(seconds)
        self.total += seconds
        if self.shutdown_after_total is not None and \
                self.total >= self.shutdown_after_total and \
                self.coordinator is not None:
            self.coordinator.request(reason="sleep-seam")


def make_runtime(*, cycle_results: list | None = None,
                 clock: FakeClock | None = None,
                 coordinator: ShutdownCoordinator | None = None,
                 interval: int = 60,
                 config: rt.RuntimeConfig | None = None,
                 signal_capture: list | None = None,
                 cycle_raises: Exception | None = None):
    """Build a GridRuntime with deterministic seams.

    ``cycle_results``: one entry per invocation (int to return, or a
    coordinator-request callable).  When exhausted, the last value repeats.
    """
    clock = clock or FakeClock()
    coordinator = coordinator or ShutdownCoordinator()
    config = config or rt.RuntimeConfig(
        enabled=True, interval_seconds=interval,
        boundary_grace_seconds=15, timeframe_seconds=TF, dry_run=True)
    calls: list[int] = []
    results = list(cycle_results or [0])

    def cycle() -> int:
        calls.append(clock.now)
        result = results[min(len(calls) - 1, len(results) - 1)]
        if callable(result):
            result()
        if cycle_raises is not None and len(calls) >= len(results):
            raise cycle_raises
        return result

    def capture_signals(co: ShutdownCoordinator) -> None:
        if signal_capture is not None:
            signal_capture.append(co)

    runtime = rt.GridRuntime(
        config, cycle=cycle, clock=clock.time, sleep=SleepRecorder(coordinator),
        shutdown=coordinator, install_signals=capture_signals,
        log=logging.getLogger("runtime.test"),
    )
    runtime.calls = calls  # test handle
    runtime.clock = clock
    return runtime


# ---------------------------------------------------------------------------
# Config safety
# ---------------------------------------------------------------------------
def test_config_refuses_dry_run_false():
    with pytest.raises(rt.RuntimeConfigError, match="dry_run"):
        rt.RuntimeConfig(enabled=True, interval_seconds=60,
                         boundary_grace_seconds=15, timeframe_seconds=TF,
                         dry_run=False)


def test_config_bounds():
    with pytest.raises(rt.RuntimeConfigError):
        rt.RuntimeConfig(True, 4, 15, TF, True)      # interval < 5
    with pytest.raises(rt.RuntimeConfigError):
        rt.RuntimeConfig(True, 3601, 15, TF, True)   # interval > 3600
    with pytest.raises(rt.RuntimeConfigError):
        rt.RuntimeConfig(True, 60, 301, TF, True)    # grace > 300
    with pytest.raises(rt.RuntimeConfigError):
        rt.RuntimeConfig(True, 60, 15, 777, True)    # unknown timeframe
    ok = rt.RuntimeConfig(True, 60, 15, TF, True)
    assert ok.enabled and ok.interval_seconds == 60


def test_load_runtime_config_from_cfg_dict():
    cfg = {"timeframe": "15m",
           "environment": {"dry_run": True, "mode": "testnet"},
           "runtime": {"enabled": True, "interval_seconds": 120,
                       "boundary_grace_seconds": 10}}
    rc = rt.load_runtime_config(cfg)
    assert rc.interval_seconds == 120 and rc.timeframe_seconds == 900
    # defaults when the block is absent
    rc2 = rt.load_runtime_config({"timeframe": "15m",
                                  "environment": {"dry_run": True}})
    assert rc2.enabled is True and rc2.interval_seconds == 60


def test_runtime_module_has_no_order_or_live_capability():
    """Static boundary via AST: the runtime imports no order client and
    calls no order-placement/cancel/SDK-client attribute anywhere (the
    docstring may *document* that these seams are never touched)."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(rt))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    for module in imported:
        assert "testnet_orders" not in module, module
        assert "binance_testnet" not in module, module
        assert "binance_sdk_spot" not in module, module
        assert "market_data" not in module, module
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in (
                "new_order", "delete_order", "place_limit_maker_order",
                "cancel_order_by_client_id", "make_cancel_executor",
                "rest_api", "_spot",
            ), node.attr


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------
def test_one_cycle_executed_on_startup():
    runtime = make_runtime(cycle_results=[0])
    runtime.clock.advance(10.0)
    # the sleep seam requests shutdown on its first slice (the clock does
    # not advance, so the loop would otherwise wait in the same candle)
    runtime._sleep = SleepRecorder(runtime.shutdown, shutdown_after_total=0.1)
    code = runtime.run()
    assert code == rt.EXIT_OK
    assert len(runtime.calls) == 1
    assert runtime.calls[0] == 1_000_010.0
    assert runtime.shutdown.phase.value == "COMPLETED"


def test_multiple_cycles_one_per_closed_candle():
    runtime = make_runtime(cycle_results=[0, 0, 0])
    # first cycle at start; sleep seam advances the clock across the next
    # two boundaries; the sleep seam shuts the daemon down after the third
    coordinator = runtime.shutdown
    sleep = SleepRecorder(coordinator)

    def advancing_sleep(seconds: float) -> None:
        sleep(seconds)
        runtime.clock.advance(seconds)

    runtime._sleep = advancing_sleep
    code = runtime.run(max_cycles=3)
    assert code == rt.EXIT_OK
    assert len(runtime.calls) == 3
    # each invocation happens strictly after the previous boundary
    boundaries = [rt.closed_boundary(t, TF) for t in runtime.calls]
    assert boundaries[0] < boundaries[1] < boundaries[2]


def test_no_repeat_invocation_within_same_candle():
    """Time passing inside one candle must NOT trigger extra cycles (the
    daemon never blindly executes against the same market state)."""
    runtime = make_runtime(cycle_results=[0])
    coordinator = runtime.shutdown
    total = {"s": 0.0}

    def small_advance_sleep(seconds: float) -> None:
        total["s"] += seconds
        runtime.clock.advance(min(seconds, 120.0))  # stay inside the candle
        if total["s"] >= 700:
            coordinator.request(reason="bounded")

    runtime._sleep = small_advance_sleep
    code = runtime.run()
    assert code == rt.EXIT_OK
    assert len(runtime.calls) == 1  # one cycle, despite ~700s of sleeping


def test_interval_pacing_respected_between_cycles():
    runtime = make_runtime(cycle_results=[0, 0], interval=60)
    coordinator = runtime.shutdown
    seen: list[float] = []

    def record_sleep(seconds: float) -> None:
        seen.append(seconds)
        runtime.clock.advance(seconds)
        if len(runtime.calls) >= 2:
            coordinator.request(reason="done")

    runtime._sleep = record_sleep
    runtime.run(max_cycles=2)
    # total sleep between the two cycles is at least the configured
    # interval (slices are <=1s; the SUM is the wait)
    assert sum(seen) >= 60.0


def test_boundary_grace_applied():
    """A new invocation waits boundary + grace before firing."""
    config = rt.RuntimeConfig(enabled=True, interval_seconds=5,
                              boundary_grace_seconds=15,
                              timeframe_seconds=TF, dry_run=True)
    runtime = make_runtime(cycle_results=[0], config=config, interval=5)
    coordinator = runtime.shutdown

    def advance_sleep(seconds: float) -> None:
        runtime.clock.advance(seconds)
        if len(runtime.calls) >= 2:
            coordinator.request(reason="done")

    runtime._sleep = advance_sleep
    runtime.run(max_cycles=2)
    first, second = runtime.calls
    # second invocation strictly after the next boundary + grace
    assert second >= (first // TF + 1) * TF + 15


# ---------------------------------------------------------------------------
# Shutdown behavior
# ---------------------------------------------------------------------------
def test_shutdown_request_terminates_loop_gracefully():
    coordinator = ShutdownCoordinator()
    runtime = make_runtime(cycle_results=[0], coordinator=coordinator)

    def request_shutdown(_seconds: float) -> None:
        coordinator.request(reason="SIGTERM")

    runtime._sleep = request_shutdown
    code = runtime.run()
    assert code == rt.EXIT_OK
    assert len(runtime.calls) == 1
    assert coordinator.phase.value == "COMPLETED"


def test_sleep_is_interruptible_into_small_slices():
    """A long wait is sliced so a signal is observed within ~1s."""
    runtime = make_runtime(cycle_results=[0])
    slices: list[float] = []

    def slicing_sleep(seconds: float) -> None:
        slices.append(seconds)

    runtime._sleep_interruptible(3.5)
    # implemented via _sleep in <=1s slices
    assert sum(len(s) and 0 for s in []) == 0  # placeholder no-op
    runtime._sleep = slicing_sleep
    runtime._sleep_interruptible(3.5)
    assert all(s <= 1.0 for s in slices)
    assert len(slices) == 4  # 1+1+1+0.5


def test_signal_installer_receives_coordinator():
    capture: list = []
    runtime = make_runtime(signal_capture=capture)
    runtime.run(max_cycles=1)
    assert len(capture) == 1
    assert isinstance(capture[0], ShutdownCoordinator)


def test_sigterm_and_sigint_wired_via_real_installer(monkeypatch):
    """The production installer wires SIGTERM+SIGINT handlers; verify by
    patching signal.signal and firing both handlers."""
    import signal as signal_module
    installed: dict = {}
    monkeypatch.setattr(signal_module, "signal",
                        lambda num, handler: installed.__setitem__(num, handler))
    from shutdown import install_signal_handlers as real_install
    coordinator = ShutdownCoordinator()
    real_install(coordinator)
    # SIGTERM (15) and SIGINT (2) handlers both request shutdown
    installed[signal_module.SIGTERM](signal_module.SIGTERM, None)
    assert coordinator.is_requested
    coordinator2 = ShutdownCoordinator()
    installed2 = {}
    monkeypatch.setattr(signal_module, "signal",
                        lambda num, handler: installed2.__setitem__(num, handler))
    real_install(coordinator2)
    installed2[signal_module.SIGINT](signal_module.SIGINT, None)
    assert coordinator2.is_requested


# ---------------------------------------------------------------------------
# Cycle outcomes
# ---------------------------------------------------------------------------
def test_block_cycle_does_not_terminate_daemon():
    """main() returns 0 for BLOCK decisions; also a nonzero (refused)
    return must keep the daemon monitoring, never invent orders."""
    runtime = make_runtime(cycle_results=[0, 1, 0])
    coordinator = runtime.shutdown

    def advance_sleep(seconds: float) -> None:
        runtime.clock.advance(seconds)
        if len(runtime.calls) >= 3:
            coordinator.request(reason="done")

    runtime._sleep = advance_sleep
    code = runtime.run(max_cycles=3)
    assert code == rt.EXIT_OK
    assert len(runtime.calls) == 3


def test_unexpected_exception_fails_closed():
    runtime = make_runtime(cycle_results=[0],
                           cycle_raises=RuntimeError("boom"))
    code = runtime.run()
    assert code == rt.EXIT_CYCLE_FAILURE
    assert len(runtime.calls) == 1  # no retry, no order invention
    assert runtime.shutdown.phase.value == "COMPLETED"


def test_exception_logged_with_traceback(caplog):
    runtime = make_runtime(cycle_results=[0],
                           cycle_raises=ValueError("kaboom"))
    with caplog.at_level(logging.ERROR, logger="runtime.test"):
        code = runtime.run()
    assert code == rt.EXIT_CYCLE_FAILURE
    assert any("CYCLE UNEXPECTED EXCEPTION" in r.message
               for r in caplog.records)
    assert any("kaboom" in r.exc_text for r in caplog.records
               if r.exc_text)


def test_structured_logging_events(caplog):
    runtime = make_runtime(cycle_results=[0, 0])
    coordinator = runtime.shutdown

    def advance_sleep(seconds: float) -> None:
        runtime.clock.advance(seconds)
        if len(runtime.calls) >= 2:
            coordinator.request(reason="done")

    runtime._sleep = advance_sleep
    with caplog.at_level(logging.INFO, logger="runtime.test"):
        runtime.run(max_cycles=2)
    messages = [r.message for r in caplog.records]
    assert any("RUNTIME START" in m for m in messages)
    assert any("CYCLE START" in m for m in messages)
    assert any("CYCLE RESULT" in m for m in messages)
    assert any("RUNTIME WAIT" in m for m in messages)
    assert any("RUNTIME STOP" in m for m in messages)


# ---------------------------------------------------------------------------
# Restart / duplicate-order boundaries
# ---------------------------------------------------------------------------
def test_first_start_invokes_cycle_which_owns_restart_gates():
    """A fresh start invokes the authoritative cycle exactly once; the
    restart-safety/kill gates live inside main() (verified by the existing
    suite) — the runtime must add no bypass and no extra invocation."""
    runtime = make_runtime(cycle_results=[0])
    runtime.run(max_cycles=1)
    assert len(runtime.calls) == 1  # exactly one invocation on startup


def test_restart_reconstructs_without_extra_same_candle_invocation():
    """A new runtime instance on the same candle still invokes once at
    startup (idempotent cycle replay dedupes), and only candle progression
    schedules further invocations."""
    clock = FakeClock(start=(1_000_000 // TF) * TF + 100.0)  # mid-candle
    runtime = make_runtime(cycle_results=[0], clock=clock)
    runtime.run(max_cycles=1)
    assert len(runtime.calls) == 1
    # a second instance at the same instant also invokes exactly once
    runtime2 = make_runtime(cycle_results=[0],
                            clock=FakeClock(start=clock.now))
    runtime2.run(max_cycles=1)
    assert len(runtime2.calls) == 1


def test_boundary_math_deterministic():
    assert rt.closed_boundary(1_000_000.0, TF) == 1_000_000 // TF
    assert rt.closed_boundary(899.9, TF) == 0
    assert rt.closed_boundary(900.0, TF) == 1
    assert rt.closed_boundary(1_799.999, TF) == 1
    assert rt.closed_boundary(1_800.0, TF) == 2
