from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vigi_vision.recording import RecordingSegment, RecordingWindow
from vigi_vision.reference_frame_candidate_api_models import (
    ReferenceFrameCandidateSetBody,
    parse_reference_frame_candidate_set_request,
)
from vigi_vision.reference_frame_candidate_models import ReferenceFrameCandidateSetRequest
from vigi_vision.reference_frame_candidate_service import (
    ReferenceFrameCandidateFailure,
    ReferenceFrameCandidateSetService,
    ReferenceFrameCandidateSuccess,
)
from vigi_vision.reference_frame_models import (
    FrameSelectionPolicy,
    ReferenceFrameNoCandidateError,
    ReferenceFrameOutcome,
    ReferenceFrameRequest,
    ReferenceFrameResolution,
    ReferenceFrameResult,
    TimingPrecisionStatus,
)

CandidateServiceOutcome = ReferenceFrameResolution | ReferenceFrameNoCandidateError | RuntimeError


@dataclass(frozen=True, slots=True)
class FakeExecutor:
    outcomes: dict[datetime, CandidateServiceOutcome]
    requests: list[ReferenceFrameRequest] = field(default_factory=list)

    def execute_or_resolve(self, request: ReferenceFrameRequest) -> ReferenceFrameResolution:
        self.requests.append(request)
        outcome = self.outcomes[request.requested_time_utc]
        if isinstance(outcome, ReferenceFrameResolution):
            return outcome
        raise outcome


def test_candidate_service_preserves_order_and_isolates_known_media_failure() -> None:
    request = _request((-10, 0, 10))
    candidates = request.candidates()
    executor = FakeExecutor(
        {
            candidates[0].request.requested_time_utc: _resolution(ReferenceFrameOutcome.CREATED),
            candidates[1].request.requested_time_utc: ReferenceFrameNoCandidateError(),
            candidates[2].request.requested_time_utc: _resolution(ReferenceFrameOutcome.REUSED),
        }
    )

    result = ReferenceFrameCandidateSetService(executor).execute(request)

    assert tuple(item.candidate.offset_seconds for item in result.items) == (-10, 0, 10)
    assert isinstance(result.items[0], ReferenceFrameCandidateSuccess)
    assert isinstance(result.items[1], ReferenceFrameCandidateFailure)
    assert isinstance(result.items[2], ReferenceFrameCandidateSuccess)
    assert result.summary.created == 1
    assert result.summary.reused == 1
    assert result.summary.failed == 1
    assert executor.requests == [candidate.request for candidate in candidates]


def test_candidate_service_returns_valid_all_media_failure_result() -> None:
    request = _request((-10, 0))
    executor = FakeExecutor(
        {
            candidate.request.requested_time_utc: ReferenceFrameNoCandidateError()
            for candidate in request.candidates()
        }
    )

    result = ReferenceFrameCandidateSetService(executor).execute(request)

    assert all(isinstance(item, ReferenceFrameCandidateFailure) for item in result.items)
    assert result.summary.created == 0
    assert result.summary.reused == 0
    assert result.summary.failed == 2


def test_candidate_service_marks_future_child_without_running_executor() -> None:
    now = datetime(2026, 7, 20, 3, 34, 18, tzinfo=timezone.utc)
    request = _request((0, 1), now_utc=now)
    first_candidate = request.candidates()[0]
    executor = FakeExecutor(
        {first_candidate.request.requested_time_utc: _resolution(ReferenceFrameOutcome.CREATED)}
    )

    result = ReferenceFrameCandidateSetService(executor).execute(request)

    assert isinstance(result.items[0], ReferenceFrameCandidateSuccess)
    assert isinstance(result.items[1], ReferenceFrameCandidateFailure)
    assert result.items[1].code == "invalid_candidate_time"


def test_candidate_service_propagates_unexpected_failure() -> None:
    request = _request((0,))
    marker = "rtsp://user:password@nvr.example/private"
    candidate = request.candidates()[0]
    executor = FakeExecutor({candidate.request.requested_time_utc: RuntimeError(marker)})

    with pytest.raises(RuntimeError, match=marker):
        _ = ReferenceFrameCandidateSetService(executor).execute(request)


def _request(
    offsets_seconds: tuple[int, ...], now_utc: datetime | None = None
) -> ReferenceFrameCandidateSetRequest:
    comparison_now = now_utc or datetime(2026, 7, 21, tzinfo=timezone.utc)
    return parse_reference_frame_candidate_set_request(
        body=ReferenceFrameCandidateSetBody(
            channel_id=1,
            reference_time="2026-07-20T03:34:18Z",
            offsets_seconds=offsets_seconds,
        ),
        now_utc=comparison_now,
    )


def _resolution(outcome: ReferenceFrameOutcome) -> ReferenceFrameResolution:
    requested_time = datetime(2026, 7, 20, 3, 34, 18, tzinfo=timezone.utc)
    segment = RecordingSegment(
        1,
        requested_time.date(),
        int(requested_time.timestamp()),
        int((requested_time + timedelta(minutes=1)).timestamp()),
        requested_time,
        requested_time + timedelta(minutes=1),
    )
    window = RecordingWindow(1, requested_time, requested_time + timedelta(seconds=6))
    result = ReferenceFrameResult(
        resource_id="channel-1_reference",
        manifest_schema_version=1,
        generation_policy_version=1,
        channel_id=1,
        requested_time_text="2026-07-20T03:34:18Z",
        source_timezone="UTC",
        requested_time_utc=requested_time,
        selected_segment=segment,
        extraction_window=window,
        frame_selection_policy=FrameSelectionPolicy.NEAREST_DECODED_FRAME,
        jpeg_relative_path=Path("channel-1_reference/frame.jpg"),
        manifest_relative_path=Path("channel-1_reference/manifest.json"),
        width=2560,
        height=1440,
        decoded_local_pts_seconds=2.0,
        estimated_source_time_utc=None,
        offset_from_requested_seconds=None,
        timing_precision_status=TimingPrecisionStatus.MEASURED_CLIP_RELATIVE,
        warnings=(),
    )
    return ReferenceFrameResolution(result, outcome)
