"""Roadmap G: graceful-shutdown coordination.

The coordinator implements stop-at-safest-boundary semantics:

* A shutdown request (operator SIGINT/SIGTERM, or an in-process request)
  only flips a flag.  The handler NEVER mutates state, never writes to the
  database, and never touches order logic — so it cannot corrupt anything,
  and it is safe to fire at any instant.
* The run loop consults :meth:`ShutdownCoordinator.is_requested` at well-
  defined safe boundaries (before the paper cycle starts, after it
  commits).  In between, the orchestrator's cycle transaction is atomic,
  so there is no point at which an interrupt can leave half-applied work.
* A second request within a short window sets ``forced``: the run still
  stops at the next safe boundary, but skips the remaining non-essential
  work and returns a non-zero exit code to the caller.

Determinism: the coordinator is driven by explicit ``request()`` calls in
tests; real OS signals are installed only via :func:`install_signal_handlers`
(never called by the test suite, never required for the paper path).
"""
from __future__ import annotations

import threading
from enum import Enum
from typing import Callable, Optional


class ShutdownPhase(str, Enum):
    IDLE = "IDLE"
    REQUESTED = "REQUESTED"
    COMPLETED = "COMPLETED"


class ShutdownCoordinator:
    """Thread-safe, deterministic shutdown state machine.

    Properties:
    * Idempotent: N requests collapse to the first; ``forced`` flips on the
      second request that arrives while one is already pending.
    * No side effects on request: only flags change; the run loop decides
      where to stop.
    * ``complete()`` is terminal: after it, requests are ignored (a
      finished run cannot be re-stopped) and the phase reports COMPLETED.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._phase = ShutdownPhase.IDLE
        self._reason: Optional[str] = None
        self._signal: Optional[str] = None
        self._forced = False

    # -- request path ------------------------------------------------------
    def request(self, reason: str = "operator", signal_name: Optional[str] = None) -> bool:
        """Record a shutdown request.  Returns True when this call CHANGED
        the state (first request, or a request that newly set ``forced``);
        False when the request is absorbed with no state change (duplicate,
        or post-completion).  The returned bool lets a signal handler decide
        whether to log without the handler itself mutating shared state."""
        with self._lock:
            if self._phase is ShutdownPhase.COMPLETED:
                return False
            if self._phase is ShutdownPhase.IDLE:
                self._phase = ShutdownPhase.REQUESTED
                self._reason = reason or "operator"
                self._signal = signal_name
                return True
            # already REQUESTED: a second request is an explicit "stop now"
            if not self._forced:
                self._forced = True
                return True
            return False

    # -- read path (the run loop consults these at safe boundaries) -------
    @property
    def is_requested(self) -> bool:
        with self._lock:
            return self._phase is not ShutdownPhase.IDLE

    @property
    def is_forced(self) -> bool:
        with self._lock:
            return self._forced

    @property
    def phase(self) -> ShutdownPhase:
        with self._lock:
            return self._phase

    @property
    def reason(self) -> Optional[str]:
        with self._lock:
            return self._reason

    @property
    def signal_name(self) -> Optional[str]:
        with self._lock:
            return self._signal

    def describe(self) -> str:
        """Single-line human-readable status (safe to log at any time)."""
        with self._lock:
            if self._phase is ShutdownPhase.IDLE:
                return "shutdown:IDLE"
            flag = "FORCED" if self._forced else "REQUESTED"
            src = self._signal or self._reason or "operator"
            return f"shutdown:{flag}:{src}"

    # -- completion ---------------------------------------------------------
    def complete(self) -> None:
        """Terminal transition.  Idempotent: a coordinator that has
        completed ignores all later requests, from whichever phase it
        entered completion (a run that finished without a request is also
        terminal — late requests must not revive it)."""
        with self._lock:
            if self._phase is not ShutdownPhase.COMPLETED:
                self._phase = ShutdownPhase.COMPLETED


def install_signal_handlers(
    coordinator: ShutdownCoordinator,
    *,
    on_complete: Optional[Callable[[], None]] = None,
) -> None:
    """Wire SIGINT / SIGTERM to the coordinator.

    The handlers do exactly one thing: call ``coordinator.request(...)``.
    No state is touched and no work is scheduled, so the handlers are safe
    to fire from the main thread at any point (including during network I/O
    inside the run loop; the loop itself observes the flag at its next safe
    boundary).  ``on_complete`` is never invoked by the handler — it exists
    so operators can observe completion through a separate, explicit path.

    This function is opt-in and is NOT called by the paper run path or the
    test suite; it is provided for a future supervised process wrapper.
    """
    import signal

    def _handler(signum, _frame):
        name = signal.Signals(signum).name if signum in signal.Signals._value2member_map_ else str(signum)
        coordinator.request(reason="signal", signal_name=name)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def run_loop_boundary_check(coordinator: ShutdownCoordinator) -> bool:
    """Run-loop seam: True when the loop must stop at this safe boundary.

    Centralizes the check so every call site reads the same semantics:
    stop only when a request is pending.  The forced flag is a pacing hint
    for the loop (skip non-essential remaining work), not a separate stop
    trigger.
    """
    return coordinator.is_requested


__all__ = [
    "ShutdownPhase",
    "ShutdownCoordinator",
    "install_signal_handlers",
    "run_loop_boundary_check",
]
