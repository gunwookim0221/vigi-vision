"""Sequential Phase 7C-1 execution through the existing A2 and B4 boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_a2_models import ProbeFrameRequestRecord, ProbeRequestStatus
from vigi_vision.recording_search_b3_models import ClassifyRecordingProbeRequest
from vigi_vision.recording_search_b4_models import (
    ClassificationOperationalError,
    ClassificationOperationalReason,
    PublishedClassificationResult,
)
from vigi_vision.recording_search_c1_models import (
    CoarseSampleResult,
    CoarseSampleStatus,
    CoarseSamplingResult,
    CoarseSupportResult,
)
from vigi_vision.recording_search_c1_planner import (
    CoarseSamplingIdentity,
    confirmation_run_id_for,
)
from vigi_vision.recording_search_models import RecordingSearchBaselineError

if TYPE_CHECKING:
    from vigi_vision.recording_search_c1_planner import CoarseSamplingPlan


class CoarseSamplingHandle(Protocol):
    """Active run handle required for every coarse operation."""

    @property
    def investigation_id(self) -> str:
        """Return the investigation identity bound to the active run."""
        ...

    @property
    def search_run_id(self) -> str:
        """Return the search-run identity bound to the active run."""
        ...

    @property
    def phase6_confirmation_id(self) -> str:
        """Return the Phase 6 package identity bound to the run."""
        ...

    @property
    def baseline_identity(self) -> str:
        """Return the canonical identity of the validated baseline."""
        ...

    @property
    def closed(self) -> bool:
        """Return whether the active run has released its authority."""
        ...


HandleT_contra = TypeVar("HandleT_contra", bound=CoarseSamplingHandle, contravariant=True)


class CoarseSamplingHost(Protocol[HandleT_contra]):
    """Existing service boundary used by the coarse executor."""

    def acquire_targets(
        self,
        handle: HandleT_contra,
        requested_times: tuple[datetime, ...],
    ) -> tuple[ProbeFrameRequestRecord, ...]:
        """Delegate acquisition to Phase 7A-2."""
        ...

    def classify(
        self,
        handle: HandleT_contra,
        request: ClassifyRecordingProbeRequest,
    ) -> PublishedClassificationResult:
        """Delegate classification to Phase 7B-4."""
        ...


@dataclass(frozen=True, slots=True)
class CoarseSamplingExecutor(Generic[HandleT_contra]):
    """Execute one plan in order without interpreting visual outcomes."""

    host: CoarseSamplingHost[HandleT_contra]

    def execute(  # noqa: C901, PLR0912 - isolates bounded boundary failures.
        self,
        handle: HandleT_contra,
        plan: CoarseSamplingPlan,
    ) -> CoarseSamplingResult:
        """Acquire and classify each target, retaining safe per-target failures."""
        samples: list[CoarseSampleResult | None] = [None] * len(plan.target_times)
        support_results: list[CoarseSupportResult] = []
        identity = CoarseSamplingIdentity(
            handle.investigation_id,
            handle.search_run_id,
            handle.phase6_confirmation_id,
            handle.baseline_identity,
        )
        acquired_target_times: set[datetime] = set()
        batches: list[tuple[int, tuple[datetime, ...], tuple[ProbeFrameRequestRecord, ...]]] = []
        for index, target in enumerate(plan.target_times):
            if handle.closed:
                samples[index] = CoarseSampleResult(
                    target,
                    CoarseSampleStatus.INTERRUPTED,
                    safe_reason="inactive_handle",
                )
                return self._partial_result(identity, plan, samples, support_results)
            try:
                candidate_support_targets = _support_targets(plan, target)
                support_targets = (
                    candidate_support_targets
                    if not acquired_target_times.intersection(candidate_support_targets)
                    else ()
                )
                acquisition_targets = support_targets or (target,)
                requests = self.host.acquire_targets(handle, acquisition_targets)
                acquired_target_times.update(acquisition_targets)
                if len(requests) != len(acquisition_targets):
                    samples[index] = CoarseSampleResult(
                        target,
                        CoarseSampleStatus.UNEXPECTED_ERROR,
                        safe_reason="invalid_acquisition_result",
                    )
                else:
                    batches.append((index, support_targets, requests))
            except RecordingSearchBaselineError:
                samples[index] = CoarseSampleResult(
                    target,
                    CoarseSampleStatus.INTERRUPTED,
                    safe_reason="stale_run_owner",
                )
                return self._partial_result(identity, plan, samples, support_results)
            except Exception:  # noqa: BLE001 - safe boundary category for one target.
                samples[index] = CoarseSampleResult(
                    target,
                    CoarseSampleStatus.UNEXPECTED_ERROR,
                    safe_reason="coarse_target_failed",
                )
        classified_samples: dict[datetime, CoarseSampleResult] = {}
        for index, support_targets, requests in batches:
            target = plan.target_times[index]
            sample = classified_samples.get(target)
            if sample is None:
                try:
                    sample = self._execute_request(handle, target, (requests[0],))
                except RecordingSearchBaselineError:
                    samples[index] = CoarseSampleResult(
                        target,
                        CoarseSampleStatus.INTERRUPTED,
                        safe_reason="stale_run_owner",
                    )
                    return self._partial_result(identity, plan, samples, support_results)
                except Exception:  # noqa: BLE001 - safe boundary category for one target.
                    sample = CoarseSampleResult(
                        target,
                        CoarseSampleStatus.UNEXPECTED_ERROR,
                        safe_reason="coarse_target_failed",
                    )
                classified_samples[target] = sample
            samples[index] = sample
            if (
                support_targets
                and sample.status is CoarseSampleStatus.SUCCESS
                and sample.classification is ClassificationOutcome.ABSENT
            ):
                try:
                    support_samples = [sample]
                    for support_target, request in zip(
                        support_targets[1:], requests[1:], strict=True
                    ):
                        support_sample = classified_samples.get(support_target)
                        if support_sample is None:
                            support_sample = self._execute_support_request(
                                handle, support_target, request
                            )
                            classified_samples[support_target] = support_sample
                        support_samples.append(support_sample)
                except RecordingSearchBaselineError:
                    return self._partial_result(identity, plan, samples, support_results)
                support_results.append(
                    CoarseSupportResult(
                        identity,
                        target,
                        _confirmation_run_id(plan, target, identity),
                        tuple(range(len(support_samples))),
                        tuple(support_samples),
                    )
                )
        return CoarseSamplingResult(
            identity,
            plan,
            tuple(sample for sample in samples if sample is not None),
            complete=True,
            support_results=tuple(support_results),
        )

    def _partial_result(
        self,
        identity: CoarseSamplingIdentity,
        plan: CoarseSamplingPlan,
        samples: list[CoarseSampleResult | None],
        support_results: list[CoarseSupportResult],
    ) -> CoarseSamplingResult:
        prefix: list[CoarseSampleResult] = []
        for sample in samples:
            if sample is None:
                break
            prefix.append(sample)
        return CoarseSamplingResult(
            identity,
            plan,
            tuple(prefix),
            complete=False,
            support_results=tuple(support_results),
        )

    def _execute_request(
        self,
        handle: HandleT_contra,
        target: datetime,
        requests: tuple[ProbeFrameRequestRecord, ...],
    ) -> CoarseSampleResult:
        if len(requests) != 1:
            return CoarseSampleResult(
                target,
                CoarseSampleStatus.UNEXPECTED_ERROR,
                safe_reason="invalid_acquisition_result",
            )
        request = requests[0]
        if request.requested_time_utc != target:
            return CoarseSampleResult(
                target,
                CoarseSampleStatus.UNEXPECTED_ERROR,
                safe_reason="request_time_mismatch",
            )
        if request.status is ProbeRequestStatus.FAILED:
            return CoarseSampleResult(
                target,
                _acquisition_status(request.failure_reason),
                probe_request_id=request.probe_request_id,
                safe_reason=request.failure_reason or "acquisition_failed",
            )
        if request.status is not ProbeRequestStatus.SUCCEEDED:
            return CoarseSampleResult(
                target,
                CoarseSampleStatus.ACQUISITION_FAILED,
                probe_request_id=request.probe_request_id,
                safe_reason="request_not_ready",
            )
        return self._classify_request(handle, target, request)

    def _classify_request(
        self,
        handle: HandleT_contra,
        target: datetime,
        request: ProbeFrameRequestRecord,
    ) -> CoarseSampleResult:
        try:
            result = self.host.classify(
                handle,
                ClassifyRecordingProbeRequest(
                    investigation_id=handle.investigation_id,
                    search_run_id=handle.search_run_id,
                    probe_request_id=request.probe_request_id,
                ),
            )
        except ClassificationOperationalError as error:
            return CoarseSampleResult(
                target,
                _classification_status(error.reason),
                probe_request_id=request.probe_request_id,
                safe_reason=error.reason.value,
            )
        if (
            result.probe_request_id != request.probe_request_id
            or result.canonical_frame_id != request.canonical_frame_id
        ):
            return CoarseSampleResult(
                target,
                CoarseSampleStatus.UNEXPECTED_ERROR,
                probe_request_id=request.probe_request_id,
                safe_reason="classification_identity_mismatch",
            )
        return CoarseSampleResult(
            target,
            CoarseSampleStatus.SUCCESS,
            probe_request_id=result.probe_request_id,
            classification=result.state,
        )

    def _execute_support_request(
        self,
        handle: HandleT_contra,
        target: datetime,
        request: ProbeFrameRequestRecord,
    ) -> CoarseSampleResult:
        try:
            return self._execute_request(handle, target, (request,))
        except RecordingSearchBaselineError:
            raise
        except Exception:  # noqa: BLE001 - isolate one support target.
            return CoarseSampleResult(
                target,
                CoarseSampleStatus.UNEXPECTED_ERROR,
                probe_request_id=request.probe_request_id,
                safe_reason="coarse_support_target_failed",
            )


def _acquisition_status(reason: str | None) -> CoarseSampleStatus:
    if reason == "recording_unavailable":
        return CoarseSampleStatus.RECORDING_UNAVAILABLE
    if reason == "decode_failed":
        return CoarseSampleStatus.ACQUISITION_FAILED
    return CoarseSampleStatus.ACQUISITION_FAILED


def _classification_status(reason: ClassificationOperationalReason) -> CoarseSampleStatus:
    if reason is ClassificationOperationalReason.CLASSIFIER_TIMEOUT:
        return CoarseSampleStatus.TIMEOUT
    return CoarseSampleStatus.CLASSIFICATION_FAILED


def _support_targets(plan: CoarseSamplingPlan, target: datetime) -> tuple[datetime, ...]:
    """Return an in-window confirmation batch or no batch at the boundary."""
    if plan.absence_confirmation_frames <= 0:
        return ()
    last = target + (
        plan.absence_confirmation_frames - 1
    ) * plan.absence_cadence_seconds * timedelta(seconds=1)
    if last > plan.search_end_utc:
        return ()
    return tuple(
        target + index * plan.absence_cadence_seconds * timedelta(seconds=1)
        for index in range(plan.absence_confirmation_frames)
    )


def _confirmation_run_id(
    plan: CoarseSamplingPlan, target: datetime, identity: CoarseSamplingIdentity
) -> str:
    return confirmation_run_id_for(plan, target, identity)
