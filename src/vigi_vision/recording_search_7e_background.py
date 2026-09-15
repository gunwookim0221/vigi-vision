"""Bounded in-process lifecycle for browser-initiated Phase 7E work."""

from __future__ import annotations

import json
import logging
import math
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Event, Lock, Timer
from typing import TYPE_CHECKING, Protocol, cast, final

from vigi_vision.recording_search_7e_1d import Phase7EStatus
from vigi_vision.recording_search_7e_public import (
    Phase7EFailureDiagnostic,
    Phase7EPreparedRequest,
    Phase7EPublicError,
    Phase7EPublicStatus,
)

_UNAVAILABLE = "recording_search_execution_unavailable"
_CONFLICT = "request_conflict"
_ALREADY_RUNNING = "already_running"
_DEFAULT_EXECUTION_DEADLINE_SECONDS = 60.0 * 60.0
_LOGGER = logging.getLogger("uvicorn.error.vigi_vision.phase7e")

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

    def publish_background_terminal(
        self,
        prepared: Phase7EPreparedRequest,
        *,
        status: str,
        reason_code: str,
    ) -> Phase7EPublicStatus: ...


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
    failure_diagnostic: Phase7EFailureDiagnostic | None = None
    future: Future[None] | None = field(default=None, repr=False)
    watchdog: Timer | None = field(default=None, repr=False)

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

    def __init__(
        self,
        service: Phase7EBackgroundService,
        *,
        execution_deadline_seconds: float = _DEFAULT_EXECUTION_DEADLINE_SECONDS,
    ) -> None:
        """Create one fixed worker and an empty bounded request ledger."""
        if not math.isfinite(execution_deadline_seconds) or execution_deadline_seconds <= 0:
            raise ValueError
        self._service = service
        self._execution_deadline_seconds = float(execution_deadline_seconds)
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
            job.watchdog = Timer(
                self._execution_deadline_seconds,
                self._watchdog_expired,
                args=(job,),
            )
            job.watchdog.daemon = True
            job.watchdog.start()
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

    def pre_run_failure_diagnostic(
        self,
        investigation_id: str,
        run_id: str,
    ) -> Phase7EFailureDiagnostic | None:
        """Return closed process-local context without changing public JSON."""
        with self._lock:
            job = next(
                (
                    candidate
                    for candidate in self._jobs.values()
                    if candidate.investigation_id == investigation_id and candidate.run_id == run_id
                ),
                None,
            )
            return None if job is None else job.failure_diagnostic

    def close(self) -> None:
        """Cancel the sole live job and wait for its bounded cleanup path."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = self._jobs.get(self._active_request_id or "")
            if active is not None:
                active.cancellation.set()
                if active.watchdog is not None:
                    active.watchdog.cancel()
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
                job.failure_diagnostic = None
        except Phase7EPublicError as error:
            diagnostic = error.diagnostic or _fallback_public_diagnostic(error.code)
            _safe_log(
                "phase7e.worker_lifecycle",
                stage="worker_exception",
                phase="successor_execution",
                status="RUNNING",
                error_code=diagnostic.category,
            )
            self._record_failure(
                job,
                diagnostic,
            )
            self._publish_worker_terminal(job)
        except Exception:  # noqa: BLE001 - worker state is a fixed safe projection.
            _safe_log(
                "phase7e.worker_lifecycle",
                stage="worker_exception",
                phase="successor_execution",
                status="RUNNING",
                error_code="internal_error",
            )
            self._record_failure(
                job,
                Phase7EFailureDiagnostic(
                    "internal",
                    "internal_error",
                    "unexpected_exception",
                    "unknown",
                ),
            )
            self._publish_worker_terminal(job)
        finally:
            if job.watchdog is not None:
                job.watchdog.cancel()
            self._recover_unexpected_running(job)
            with self._lock:
                if self._active_request_id == job.request_id:
                    self._active_request_id = None

    def _publish_worker_terminal(self, job: _Job) -> None:
        """Persist a safe terminal when the worker exits without one."""
        publisher = getattr(self._service, "publish_background_terminal", None)
        if not callable(publisher):
            return
        publisher = cast("Callable[..., Phase7EPublicStatus]", publisher)
        if job.cancellation.is_set():
            status, reason_code = "INTERRUPTED", "cancelled"
        else:
            status, reason_code = "FAILED", "internal_error"
        try:
            result = publisher(
                job.prepared,
                status=status,
                reason_code=reason_code,
            )
        except Exception:  # noqa: BLE001 - retain the closed in-memory diagnostic.
            _safe_log(
                "phase7e.worker_lifecycle",
                stage="terminal_publication_failed",
                phase="successor_execution",
                status=status,
                reason_code=reason_code,
            )
            return
        if result.phase7.status != "UNAVAILABLE":
            with self._lock:
                job.status = result.phase7.status
                job.error_code = result.phase7.reason_code
        _safe_log(
            "phase7e.worker_lifecycle",
            stage="terminal_published",
            phase="successor_execution",
            status=result.phase7.status,
            reason_code=result.phase7.reason_code,
        )

    def _watchdog_expired(self, job: _Job) -> None:
        """Stop an unbounded worker and publish a durable safe terminal."""
        with self._lock:
            if self._closed or job.future is None or job.future.done():
                return
            if job.status not in {"ACCEPTED", "RUNNING"}:
                return
            job.cancellation.set()
        _safe_log(
            "phase7e.execution_watchdog",
            stage="deadline_expired",
            phase="successor_execution",
            status="RUNNING",
            reason_code="execution_deadline_exhausted",
        )
        publisher = getattr(self._service, "publish_background_terminal", None)
        if not callable(publisher):
            self._record_failure(
                job,
                Phase7EFailureDiagnostic(
                    "internal",
                    "internal_error",
                    "unexpected_exception",
                    "unknown",
                ),
            )
            return
        publisher = cast("Callable[..., Phase7EPublicStatus]", publisher)
        try:
            result = publisher(
                job.prepared,
                status="INCONCLUSIVE",
                reason_code="execution_deadline_exhausted",
            )
        except Exception:  # noqa: BLE001 - the worker will perform final cleanup.
            self._record_failure(
                job,
                Phase7EFailureDiagnostic(
                    "internal",
                    "internal_error",
                    "unexpected_exception",
                    "unknown",
                ),
            )
            _safe_log(
                "phase7e.execution_watchdog",
                stage="terminal_publication_failed",
                phase="successor_execution",
                status="RUNNING",
                reason_code="execution_deadline_exhausted",
            )
            return
        if result.phase7.status != "UNAVAILABLE":
            with self._lock:
                job.status = result.phase7.status
                job.error_code = result.phase7.reason_code
                job.failure_diagnostic = None
        _safe_log(
            "phase7e.execution_watchdog",
            stage="terminal_published",
            phase="successor_execution",
            status=result.phase7.status,
            reason_code=result.phase7.reason_code,
        )

    def _record_failure(
        self,
        job: _Job,
        diagnostic: Phase7EFailureDiagnostic,
    ) -> None:
        with self._lock:
            if job.cancellation.is_set():
                job.status = "INTERRUPTED"
                job.error_code = "interrupted"
                job.failure_diagnostic = Phase7EFailureDiagnostic(
                    "invocation_input",
                    "interrupted",
                    "CommonSessionCancelledError",
                    diagnostic.cleanup_outcome,
                )
            else:
                job.status = "FAILED"
                job.error_code = diagnostic.category
                job.failure_diagnostic = diagnostic

    def _recover_unexpected_running(self, job: _Job) -> None:
        try:
            current = self._service.resolve_existing(job.prepared)
        except Phase7EPublicError:
            return
        if current is None:
            return
        with self._lock:
            existing_diagnostic = job.failure_diagnostic
            job.status = current.phase7.status
            job.error_code = current.phase7.reason_code
            if not (
                current.phase7.status == "FAILED"
                and existing_diagnostic is not None
                and existing_diagnostic.category == current.phase7.reason_code
            ):
                job.failure_diagnostic = None

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


def _fallback_public_diagnostic(code: str) -> Phase7EFailureDiagnostic:
    """Classify a worker-raised public error that predates diagnostic context."""
    if code in {_ALREADY_RUNNING, _CONFLICT}:
        return Phase7EFailureDiagnostic(
            "invocation_input",
            code,
            "Phase7EPublicError",
            "not_required",
        )
    return Phase7EFailureDiagnostic(
        "internal",
        "internal_error",
        "Phase7EPublicError",
        "unknown",
    )


def _safe_log(event: str, **fields: object) -> None:
    try:
        _LOGGER.info("%s %s", event, json.dumps(fields, sort_keys=True, separators=(",", ":")))
    except Exception:  # noqa: BLE001 - diagnostics never alter lifecycle behavior.
        return


__all__ = ["Phase7EBackgroundManager", "Phase7EStartReceipt"]
