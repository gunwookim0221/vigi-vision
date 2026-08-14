"""Bounded non-authoritative classifier execution."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Condition, Lock
from typing import TYPE_CHECKING, Protocol, final

if TYPE_CHECKING:
    from collections.abc import Callable

    from vigi_vision.recording_search_b3_models import (
        ClassificationSnapshot,
        NonAuthoritativeClassificationResult,
    )


class SnapshotClassificationExecutor(Protocol):
    """Submit immutable snapshots without granting publication capability."""

    def submit(
        self, snapshot: ClassificationSnapshot
    ) -> Future[NonAuthoritativeClassificationResult]:
        """Submit one immutable snapshot for non-authoritative computation."""
        ...

    def close(self) -> None:
        """Release executor resources without granting late publication."""
        ...


@final
class ClassificationExecutorBusyError(RuntimeError):
    """Report that bounded classification admission is exhausted."""


@final
class _AdmissionGate:
    __slots__ = ("_admitted", "_capacity", "_condition")

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._admitted = 0
        self._condition = Condition(Lock())

    def claim(self) -> _AdmissionPermit | None:
        with self._condition:
            if self._admitted >= self._capacity:
                return None
            self._admitted += 1
        return _AdmissionPermit(self)

    def release(self) -> None:
        with self._condition:
            self._admitted -= 1
            self._condition.notify_all()

    @property
    def admitted(self) -> int:
        with self._condition:
            return self._admitted

    def wait_until_idle(self, timeout: float) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: self._admitted == 0, timeout)


@final
class _AdmissionPermit:
    __slots__ = ("_gate", "_lock", "_released")

    def __init__(self, gate: _AdmissionGate) -> None:
        self._gate = gate
        self._lock = Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._gate.release()


@final
class ThreadedSnapshotClassificationExecutor:
    """Own a fixed-size worker pool that never waits for abandoned work on close."""

    __slots__ = ("_executor", "_gate", "_maximum_admitted_work", "_worker")

    def __init__(
        self,
        worker: Callable[[ClassificationSnapshot], NonAuthoritativeClassificationResult],
        max_workers: int = 2,
        max_queue_size: int = 2,
    ) -> None:
        """Create a fixed worker pool with bounded queued-work admission."""
        if (
            isinstance(max_workers, bool)
            or isinstance(max_queue_size, bool)
            or max_workers <= 0
            or max_queue_size <= 0
        ):
            raise ValueError
        self._worker = worker
        self._maximum_admitted_work = max_workers + max_queue_size
        self._gate = _AdmissionGate(self._maximum_admitted_work)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="vigi-phase7b-classifier",
        )

    def submit(
        self, snapshot: ClassificationSnapshot
    ) -> Future[NonAuthoritativeClassificationResult]:
        """Submit one snapshot without repository or handle capability."""
        permit = self._gate.claim()
        if permit is None:
            raise ClassificationExecutorBusyError
        try:
            future = self._executor.submit(self._execute, snapshot, permit)
        except (RuntimeError, TypeError, ValueError):
            permit.release()
            raise
        future.add_done_callback(
            lambda completed: permit.release() if completed.cancelled() else None
        )
        return future

    @property
    def admitted_work(self) -> int:
        """Return the current running-plus-queued admission count."""
        return self._gate.admitted

    @property
    def maximum_admitted_work(self) -> int:
        """Return the fixed running-plus-queued admission capacity."""
        return self._maximum_admitted_work

    def wait_until_idle(self, timeout: float) -> bool:
        """Wait a bounded interval for all admitted work to release."""
        return self._gate.wait_until_idle(timeout)

    def _execute(
        self,
        snapshot: ClassificationSnapshot,
        permit: _AdmissionPermit,
    ) -> NonAuthoritativeClassificationResult:
        try:
            return self._worker(snapshot)
        finally:
            permit.release()

    def close(self) -> None:
        """Cancel queued work and abandon running work without waiting."""
        self._executor.shutdown(wait=False, cancel_futures=True)
