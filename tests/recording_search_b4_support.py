from __future__ import annotations

from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from threading import Event
from typing import TYPE_CHECKING, cast, final

from tests.test_recording_search_a2 import successful_a2_run
from typing_extensions import override

from vigi_vision.investigation_confirmation_integrity import compute_jpeg_integrity_from_bytes
from vigi_vision.object_presence_evidence import RawComparison
from vigi_vision.object_presence_models import DecodedRgbImage
from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
from vigi_vision.object_presence_values import VisualReason, VisualStatus, quantize_metric
from vigi_vision.recording_search_b3_media import DecodedMedia
from vigi_vision.recording_search_b3_models import (
    ClassificationSnapshot,
    ClassifyRecordingProbeRequest,
    NonAuthoritativeClassificationResult,
)
from vigi_vision.recording_search_b3_service import RecordingSearchClassificationService
from vigi_vision.recording_search_b4_service import ObservationClassificationService

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vigi_vision.recording_search_a2_models import (
        ProbeFrameRequestRecord,
        RecordingSearchManifestV2,
    )
    from vigi_vision.recording_search_service import (
        RecordingSearchRunHandle,
        RecordingSearchService,
    )


class _Decoder:
    def decode(self, payload: bytes, width: int, height: int) -> DecodedMedia:
        row = ((32, 64, 96),) * width
        image = DecodedRgbImage.from_rows((row,) * height)
        return DecodedMedia(compute_jpeg_integrity_from_bytes(payload, width, height), image)


@final
class _ControlledFuture(Future[NonAuthoritativeClassificationResult]):
    def __init__(self, *, timeout_immediately: bool = False) -> None:
        super().__init__()
        self.timeout_immediately: bool = timeout_immediately

    @override
    def result(self, timeout: float | None = None) -> NonAuthoritativeClassificationResult:
        if self.timeout_immediately:
            raise FutureTimeoutError
        return super().result(timeout)


@dataclass(slots=True)
class ControlledExecutor:
    factory: Callable[[ClassificationSnapshot, int], Future[NonAuthoritativeClassificationResult]]
    submitted: Event = field(default_factory=Event)
    submissions: int = 0
    snapshots: tuple[ClassificationSnapshot, ...] = ()
    closed: bool = False

    def submit(
        self, snapshot: ClassificationSnapshot
    ) -> Future[NonAuthoritativeClassificationResult]:
        self.submissions += 1
        self.snapshots = (*self.snapshots, snapshot)
        future = self.factory(snapshot, self.submissions)
        self.submitted.set()
        return future

    def close(self) -> None:
        self.closed = True


@dataclass(slots=True)
class Harness:
    service: RecordingSearchService
    investigation_id: str
    handle: RecordingSearchRunHandle
    manifest: RecordingSearchManifestV2
    request_record: ProbeFrameRequestRecord
    command: ClassifyRecordingProbeRequest
    preparer: RecordingSearchClassificationService


def build_harness(tmp_path: Path) -> Harness:
    service, investigation_id, handle, manifest, request = successful_a2_run(tmp_path)
    command = ClassifyRecordingProbeRequest(
        investigation_id=investigation_id,
        search_run_id=manifest.search_run_id,
        probe_request_id=request.probe_request_id,
    )
    preparer = RecordingSearchClassificationService(
        host=service,
        media_decoder=_Decoder(),
        policy=ObjectPresenceDecisionPolicy(
            minimum_mask_overlap_for_comparison=0.1,
            minimum_comparison_area=1,
            minimum_clipped_mask_pixels=1,
        ),
    )
    return Harness(service, investigation_id, handle, manifest, request, command, preparer)


def install_executor(
    harness: Harness,
    executor: ControlledExecutor,
    *,
    timeout_seconds: float = 5.0,
) -> ObservationClassificationService:
    authority = ObservationClassificationService(
        host=harness.service,
        preparer=harness.preparer,
        executor=executor,
        timeout_seconds=timeout_seconds,
        now_utc=harness.service.repository.now_utc,
        attempt_id_factory=lambda: f"classification-attempt-{executor.submissions + 1}",
        operation_id_factory=lambda: f"classification-op-{executor.submissions}",
    )
    harness.service.classification_service = authority
    return authority


def completed_result(
    snapshot: ClassificationSnapshot,
    outcome: str,
) -> NonAuthoritativeClassificationResult:
    policy = snapshot.policy
    comparison = _comparison(outcome)
    return NonAuthoritativeClassificationResult(snapshot, policy.decide(comparison))


def completed_future(
    snapshot: ClassificationSnapshot, outcome: str
) -> Future[NonAuthoritativeClassificationResult]:
    future: Future[NonAuthoritativeClassificationResult] = Future()
    future.set_result(completed_result(snapshot, outcome))
    return future


def timed_out_future() -> _ControlledFuture:
    future = _ControlledFuture(timeout_immediately=True)
    assert future.set_running_or_notify_cancel()
    return future


def _comparison(outcome: str) -> RawComparison:
    if outcome == "INDETERMINATE_UNUSABLE":
        return RawComparison(
            baseline_mask_pixel_count=None,
            probe_mask_pixel_count=None,
            roi_pixel_count=9600,
            mask_intersection_pixel_count=None,
            mask_union_pixel_count=None,
            baseline_mask_coverage=None,
            probe_mask_coverage=None,
            mask_iou=None,
            effective_comparison_area=None,
            roi_luma_ncc=None,
            visual_status=VisualStatus.UNUSABLE,
            unusable_reason=VisualReason.INVALID_MASK,
        )
    baseline_count, probe_count, intersection, ncc = {
        "PRESENT": (100, 100, 100, 1.0),
        "ABSENT": (55, 55, 10, -1.0),
        "INDETERMINATE": (100, 100, 50, 0.5),
    }[outcome]
    union = baseline_count + probe_count - intersection
    return RawComparison(
        baseline_mask_pixel_count=baseline_count,
        probe_mask_pixel_count=probe_count,
        roi_pixel_count=9600,
        mask_intersection_pixel_count=intersection,
        mask_union_pixel_count=union,
        baseline_mask_coverage=quantize_metric(baseline_count / 9600),
        probe_mask_coverage=quantize_metric(probe_count / 9600),
        mask_iou=quantize_metric(intersection / union),
        effective_comparison_area=intersection,
        roi_luma_ncc=ncc,
        visual_status=VisualStatus.COMPARABLE,
        unusable_reason=None,
    )


def unsafe_future(value: object) -> Future[NonAuthoritativeClassificationResult]:
    future: Future[object] = Future()
    future.set_result(value)
    return cast("Future[NonAuthoritativeClassificationResult]", future)
