"""Handle-owned bounded classification and atomic Phase 7B publication."""

from __future__ import annotations

import math
from concurrent.futures import CancelledError, Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from vigi_vision.recording_search_b3_contracts import (
    ClassificationHandle,
    ClassificationHost,
)
from vigi_vision.recording_search_b3_models import (
    CanonicalDuplicateResult,
    ClassificationPreparationError,
    ClassificationSnapshot,
    ClassifyRecordingProbeRequest,
    NonAuthoritativeClassificationResult,
)
from vigi_vision.recording_search_b4_authority import (
    ClassificationAttemptSlot,
    InvocationAuthority,
)
from vigi_vision.recording_search_b4_executor import ClassificationExecutorBusyError
from vigi_vision.recording_search_b4_models import (
    ClassificationOperationalError,
    ClassificationOperationalReason,
    PublishedClassificationResult,
)
from vigi_vision.recording_search_b4_publication import ClassificationPublisher
from vigi_vision.recording_search_b4_revalidation import (
    snapshot_matches_authoritative_state,
)
from vigi_vision.recording_search_b4_support import fail, map_preparation_reason
from vigi_vision.recording_search_models import RecordingSearchBaselineError

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from vigi_vision.recording_search_b3_service import RecordingSearchClassificationService
    from vigi_vision.recording_search_b4_executor import SnapshotClassificationExecutor


class AuthoritativeClassificationHandle(ClassificationHandle, Protocol):
    """Active handle carrying its invocation-local classification marker."""

    @property
    def classification_attempts(self) -> ClassificationAttemptSlot:
        """Return the handle-owned active classification marker."""
        ...


@dataclass(frozen=True, slots=True)
class ObservationClassificationService:
    """Orchestrate capture, bounded execution, revalidation, and publication."""

    host: ClassificationHost
    preparer: RecordingSearchClassificationService
    executor: SnapshotClassificationExecutor
    timeout_seconds: float
    now_utc: Callable[[], datetime]
    attempt_id_factory: Callable[[], str]
    operation_id_factory: Callable[[], str]

    def __post_init__(self) -> None:
        """Reject non-positive or unbounded classifier deadlines."""
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError

    def classify(
        self,
        handle: AuthoritativeClassificationHandle,
        request: ClassifyRecordingProbeRequest,
    ) -> PublishedClassificationResult:
        """Classify one admitted probe through the sole authoritative entry point."""
        attempt: InvocationAuthority | None = None
        try:
            with self.host.a2_mutation(handle):
                if handle.classification_attempts.active:
                    fail(ClassificationOperationalReason.CLASSIFICATION_IN_PROGRESS)
                captured = self.preparer.capture_locked(handle, request)
                if isinstance(captured, CanonicalDuplicateResult):
                    return self._publisher().reuse(captured, request)
                attempt = InvocationAuthority(self.attempt_id_factory())
                if not handle.classification_attempts.claim(attempt):
                    fail(ClassificationOperationalReason.CLASSIFICATION_IN_PROGRESS)
        except ClassificationPreparationError as error:
            raise ClassificationOperationalError(map_preparation_reason(error.reason)) from None
        except RecordingSearchBaselineError:
            fail(ClassificationOperationalReason.STALE_RUN_OWNER)
        future = self._submit(captured, attempt, handle)
        prepared = self._wait(future, attempt, handle)
        return self._publish_timely(handle, request, captured, prepared, attempt)

    def close(self) -> None:
        """Close the bounded worker executor without waiting for abandoned work."""
        self.executor.close()

    def _submit(
        self,
        snapshot: ClassificationSnapshot,
        attempt: InvocationAuthority,
        handle: AuthoritativeClassificationHandle,
    ) -> Future[NonAuthoritativeClassificationResult]:
        try:
            return self.executor.submit(snapshot)
        except ClassificationExecutorBusyError:
            _ = attempt.revoke()
            self._release_attempt(handle, attempt)
            fail(ClassificationOperationalReason.CLASSIFICATION_IN_PROGRESS)
        except (RuntimeError, ValueError, TypeError):
            _ = attempt.revoke()
            self._release_attempt(handle, attempt)
            fail(ClassificationOperationalReason.CLASSIFIER_EXECUTION_FAILED)

    def _wait(
        self,
        future: Future[NonAuthoritativeClassificationResult],
        attempt: InvocationAuthority,
        handle: AuthoritativeClassificationHandle,
    ) -> NonAuthoritativeClassificationResult:
        try:
            result = future.result(timeout=self.timeout_seconds)
        except FutureTimeoutError:
            _ = attempt.revoke_timeout()
            _ = future.cancel()
            future.add_done_callback(lambda _completed: attempt.discard())
            self._release_attempt(handle, attempt)
            fail(ClassificationOperationalReason.CLASSIFIER_TIMEOUT)
        except CancelledError:
            _ = attempt.revoke_abandoned()
            _ = future.cancel()
            future.add_done_callback(lambda _completed: attempt.discard())
            self._release_attempt(handle, attempt)
            fail(ClassificationOperationalReason.CALLER_ABANDONED)
        except (KeyboardInterrupt, SystemExit):
            _ = attempt.revoke_abandoned()
            _ = future.cancel()
            future.add_done_callback(lambda _completed: attempt.discard())
            self._release_attempt(handle, attempt)
            fail(ClassificationOperationalReason.CALLER_ABANDONED)
        except ClassificationPreparationError as error:
            _ = attempt.revoke()
            self._release_attempt(handle, attempt)
            raise ClassificationOperationalError(map_preparation_reason(error.reason)) from None
        except (RuntimeError, ValueError, TypeError, OSError):
            _ = attempt.revoke()
            self._release_attempt(handle, attempt)
            fail(ClassificationOperationalReason.CLASSIFIER_EXECUTION_FAILED)
        if not attempt.complete_in_time():
            _ = attempt.discard()
            self._release_attempt(handle, attempt)
            fail(ClassificationOperationalReason.STALE_RUN_OWNER)
        checked = cast("object", result)
        if not isinstance(checked, NonAuthoritativeClassificationResult):
            _ = attempt.revoke()
            self._release_attempt(handle, attempt)
            fail(ClassificationOperationalReason.INVALID_CLASSIFIER_OUTPUT)
        return checked

    def _publish_timely(
        self,
        handle: AuthoritativeClassificationHandle,
        request: ClassifyRecordingProbeRequest,
        captured: ClassificationSnapshot,
        prepared: NonAuthoritativeClassificationResult,
        attempt: InvocationAuthority,
    ) -> PublishedClassificationResult:
        snapshot = prepared.snapshot
        try:
            with self.host.a2_mutation(handle):
                if not handle.classification_attempts.owns(attempt) or not attempt.can_publish:
                    _ = attempt.discard()
                    fail(ClassificationOperationalReason.STALE_RUN_OWNER)
                current = self._publisher().current(request)
                duplicate = self.preparer.find_duplicate_locked(current, request)
                if duplicate is not None:
                    _ = attempt.discard()
                    _ = handle.classification_attempts.release(attempt)
                    return self._publisher().reuse(duplicate, request)
                if captured != snapshot or prepared.snapshot != snapshot:
                    _ = attempt.revoke()
                    _ = attempt.discard()
                    _ = handle.classification_attempts.release(attempt)
                    fail(ClassificationOperationalReason.AUTHORITATIVE_STATE_CHANGED)
                if not snapshot_matches_authoritative_state(
                    self.host.repository,
                    handle.baseline_bytes,
                    current,
                    request,
                    snapshot,
                ):
                    _ = attempt.revoke()
                    _ = attempt.discard()
                    _ = handle.classification_attempts.release(attempt)
                    fail(ClassificationOperationalReason.AUTHORITATIVE_STATE_CHANGED)
                result = self._publisher().publish(current, snapshot, prepared, request)
                if not attempt.mark_published():
                    fail(ClassificationOperationalReason.STALE_RUN_OWNER)
                _ = handle.classification_attempts.release(attempt)
                return result
        except ClassificationOperationalError:
            _ = attempt.revoke()
            _ = attempt.discard()
            _ = handle.classification_attempts.release(attempt)
            raise
        except RecordingSearchBaselineError:
            _ = attempt.revoke()
            _ = attempt.discard()
            _ = handle.classification_attempts.release(attempt)
            fail(ClassificationOperationalReason.STALE_RUN_OWNER)

    def _release_attempt(
        self,
        handle: AuthoritativeClassificationHandle,
        attempt: InvocationAuthority,
    ) -> None:
        try:
            with self.host.a2_mutation(handle):
                _ = handle.classification_attempts.release(attempt)
        except RecordingSearchBaselineError:
            _ = handle.classification_attempts.release(attempt)

    def _publisher(self) -> ClassificationPublisher:
        return ClassificationPublisher(
            host=self.host,
            preparer=self.preparer,
            now_utc=self.now_utc,
            operation_id_factory=self.operation_id_factory,
        )
