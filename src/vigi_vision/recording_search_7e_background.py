"""Bounded in-process lifecycle for browser-initiated Phase 7E work."""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Event, Lock
from typing import TYPE_CHECKING, Protocol, final

from vigi_vision.recording_search_7e_1d import Phase7EStatus
from vigi_vision.recording_search_7e_public import (
    Phase7EPreparedRequest,
    Phase7EPublicError,
    Phase7EPublicStatus,
)

_UNAVAILABLE = "recording_search_execution_unavailable"
_CONFLICT = "request_conflict"
_ALREADY_RUNNING = "already_running"

if TYPE_CHECKING:
    from collections.abc import Callable


class Phase7EBackgroundService(Protocol):
    """Production methods required by the bounded background lifecycle."""

    def prepare_http(
        self,
        investigation_id: str,
        search_end: str,
        request_id: str,
    ) -> Phase7EPreparedRequest: ...

    def resolve_existing(self, prepared: Phase7EPreparedRequest) -> Phase7EPublicStatus | None: ...

    def execute_prepared(
        self,
        prepared: Phase7EPreparedRequest,
        *,
        cancellation: Callable[[], bool] | None = None,
        create_phase8_handoff: bool = False,
    ) -> Phase7EPublicStatus: ...

    def status(self, investigation_id: str, run_id: str) -> Phase7EPublicStatus: ...

    def recover_abandoned(self) -> int: ...


@dataclass(frozen=True, slots=True)
class Phase7EStartReceipt:
    """Credential-free receipt returned by the HTTP start boundary."""

    request_id: str
    investigation_id: str
    run_id: str
    status: str

    @property
    def status_url(self) -> str:
        """Return the stable read-only status route for this run."""
        return f"/api/v1/recording-searches/{self.investigation_id}/{self.run_id}"


@dataclass(slots=True)
class _Job:
    request_id: str
    investigation_id: str
    search_end: str
    prepared: Phase7EPreparedRequest
    cancellation: Event = field(default_factory=Event, repr=False)
    status: str = "ACCEPTED"
    error_code: str | None = None
    future: Future[None] | None = field(default=None, repr=False)

    @property
    def run_id(self) -> str:
        return self.prepared.request.run_id

    def receipt(self) -> Phase7EStartReceipt:
        return Phase7EStartReceipt(
            self.request_id,
            self.investigation_id,
            self.run_id,
            self.status,
        )


@final
class Phase7EBackgroundManager:
    """Admit at most one live job and retain a bounded retry ledger."""

    _MAX_RECENT = 64

    def __init__(self, service: Phase7EBackgroundService) -> None:
        """Create one fixed worker and an empty bounded request ledger."""
        self._service = service
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="phase7e-browser")
        self._admission_lock = Lock()
        self._lock = Lock()
        self._jobs: OrderedDict[str, _Job] = OrderedDict()
        self._active_request_id: str | None = None
        self._closed = False

    def start(
        self,
        investigation_id: str,
        search_end: str,
        request_id: str,
    ) -> Phase7EStartReceipt:
        """Validate synchronously, deduplicate, then admit one bounded worker."""
        prepared = self._service.prepare_http(investigation_id, search_end, request_id)
        with self._admission_lock:
            return self._admit(investigation_id, search_end, request_id, prepared)

    def _admit(
        self,
        investigation_id: str,
        search_end: str,
        request_id: str,
        prepared: Phase7EPreparedRequest,
    ) -> Phase7EStartReceipt:
        """Serialize durable retry resolution with process-local admission."""
        with self._lock:
            if self._closed:
                raise Phase7EPublicError(_UNAVAILABLE)
            prior = self._jobs.get(request_id)
            if prior is not None:
                if (
                    prior.investigation_id != investigation_id
                    or prior.search_end != search_end
                    or prior.run_id != prepared.request.run_id
                ):
                    raise Phase7EPublicError(_CONFLICT)
                return prior.receipt()

        existing = self._service.resolve_existing(prepared)
        if existing is not None:
            job = _Job(request_id, investigation_id, search_end, prepared)
            job.status = existing.phase7.status
            with self._lock:
                self._remember(job)
            return job.receipt()

        with self._lock:
            if self._closed:
                raise Phase7EPublicError(_UNAVAILABLE)
            prior = self._jobs.get(request_id)
            if prior is not None:
                if (
                    prior.investigation_id != investigation_id
                    or prior.search_end != search_end
                    or prior.run_id != prepared.request.run_id
                ):
                    raise Phase7EPublicError(_CONFLICT)
                return prior.receipt()
            if self._active_request_id is not None:
                raise Phase7EPublicError(_ALREADY_RUNNING)
            job = _Job(request_id, investigation_id, search_end, prepared)
            self._remember(job)
            self._active_request_id = request_id
            job.future = self._executor.submit(self._run, job)
            return Phase7EStartReceipt(request_id, investigation_id, job.run_id, "ACCEPTED")

    def status(self, investigation_id: str, run_id: str) -> Phase7EPublicStatus:
        """Read durable status first, then project only pre-admission worker state."""
        durable = self._service.status(investigation_id, run_id)
        if durable.phase7.status != "UNAVAILABLE":
            return durable
        with self._lock:
            job = next(
                (
                    candidate
                    for candidate in self._jobs.values()
                    if candidate.investigation_id == investigation_id and candidate.run_id == run_id
                ),
                None,
            )
            if job is None:
                return durable
            return Phase7EPublicStatus(
                Phase7EStatus(
                    investigation_id,
                    run_id,
                    0,
                    job.status,
                    job.error_code,
                    None,
                )
            )

    def close(self) -> None:
        """Cancel the sole live job and wait for its bounded cleanup path."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = self._jobs.get(self._active_request_id or "")
            if active is not None:
                active.cancellation.set()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def recover_startup(self) -> None:
        """Apply bounded durable interruption recovery before serving requests."""
        _ = self._service.recover_abandoned()

    def _run(self, job: _Job) -> None:
        with self._lock:
            job.status = "RUNNING"
        try:
            result = self._service.execute_prepared(
                job.prepared,
                cancellation=job.cancellation.is_set,
            )
            with self._lock:
                job.status = result.phase7.status
                job.error_code = result.phase7.reason_code
        except Phase7EPublicError as error:
            self._record_failure(job, error.code)
        except Exception:  # noqa: BLE001 - worker state is a fixed safe projection.
            self._record_failure(job, "internal_error")
        finally:
            self._recover_unexpected_running(job)
            with self._lock:
                if self._active_request_id == job.request_id:
                    self._active_request_id = None

    def _record_failure(self, job: _Job, code: str) -> None:
        with self._lock:
            job.status = "INTERRUPTED" if job.cancellation.is_set() else "FAILED"
            job.error_code = code

    def _recover_unexpected_running(self, job: _Job) -> None:
        try:
            current = self._service.resolve_existing(job.prepared)
        except Phase7EPublicError:
            return
        if current is None:
            return
        with self._lock:
            job.status = current.phase7.status
            job.error_code = current.phase7.reason_code

    def _remember(self, job: _Job) -> None:
        self._jobs[job.request_id] = job
        self._jobs.move_to_end(job.request_id)
        while len(self._jobs) > self._MAX_RECENT:
            evictable_id = next(
                (
                    request_id
                    for request_id, candidate in self._jobs.items()
                    if request_id != self._active_request_id
                    and (candidate.future is None or candidate.future.done())
                ),
                None,
            )
            if evictable_id is None:
                break
            del self._jobs[evictable_id]


__all__ = ["Phase7EBackgroundManager", "Phase7EStartReceipt"]
