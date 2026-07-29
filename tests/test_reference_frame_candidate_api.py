from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient

from vigi_vision import reference_frame_api
from vigi_vision.recording import RecordingSegment, RecordingWindow
from vigi_vision.reference_frame_api import create_reference_frame_app
from vigi_vision.reference_frame_candidate_api_models import (
    ReferenceFrameCandidateSetResponse,
    ReferenceFrameCandidateSuccessResponse,
)
from vigi_vision.reference_frame_candidate_models import ReferenceFrameCandidateSetRequest
from vigi_vision.reference_frame_candidate_service import ReferenceFrameCandidateSetResult
from vigi_vision.reference_frame_models import (
    FrameSelectionPolicy,
    ReferenceFrameNoCandidateError,
    ReferenceFrameOutcome,
    ReferenceFrameRequest,
    ReferenceFrameResolution,
    ReferenceFrameResult,
    TimingPrecisionStatus,
)
from vigi_vision.reference_frame_resources import ReferenceFrameImageResource

CandidateApiOutcome = ReferenceFrameOutcome | ReferenceFrameNoCandidateError | RuntimeError


@dataclass(frozen=True, slots=True)
class FakeExecutor:
    outcomes: Mapping[datetime, CandidateApiOutcome]

    def execute_or_resolve(self, request: ReferenceFrameRequest) -> ReferenceFrameResolution:
        outcome = self.outcomes[request.requested_time_utc]
        if isinstance(outcome, ReferenceFrameOutcome):
            return _resolution(request, outcome)
        raise outcome


@dataclass(frozen=True, slots=True)
class FakeResources:
    image: ReferenceFrameImageResource

    def resolve_image(self, resource_id: str) -> ReferenceFrameImageResource:
        _ = resource_id
        return self.image


def test_candidate_api_uses_default_offsets_in_stable_order(tmp_path: Path) -> None:
    reference_time = datetime(2026, 7, 20, 3, 34, 18, tzinfo=timezone.utc)
    outcomes = {
        reference_time + timedelta(seconds=offset): ReferenceFrameOutcome.CREATED
        for offset in (-60, -10, 0, 10, 60)
    }
    client = _client(tmp_path, outcomes)

    response = client.post(
        "/api/v1/reference-frame-candidate-sets",
        json={"channel_id": 1, "reference_time": "2026-07-20 12:34:18"},
    )

    assert response.status_code == 200
    payload = ReferenceFrameCandidateSetResponse.model_validate_json(response.content)
    assert payload.reference_time_utc == "2026-07-20T03:34:18+00:00"
    assert payload.source_timezone == "Asia/Seoul"
    assert payload.offsets_seconds == (-60, -10, 0, 10, 60)
    assert tuple(item.offset_seconds for item in payload.candidates) == (-60, -10, 0, 10, 60)
    assert tuple(item.candidate_requested_time_utc for item in payload.candidates) == (
        "2026-07-20T03:33:18+00:00",
        "2026-07-20T03:34:08+00:00",
        "2026-07-20T03:34:18+00:00",
        "2026-07-20T03:34:28+00:00",
        "2026-07-20T03:35:18+00:00",
    )
    assert (payload.summary.created, payload.summary.reused, payload.summary.failed) == (5, 0, 0)
    for item in payload.candidates:
        if isinstance(item, ReferenceFrameCandidateSuccessResponse):
            assert item.reference_frame.image_url.startswith("/api/v1/reference-frames/")
        else:
            pytest.fail("default candidates unexpectedly failed")


@pytest.mark.parametrize(
    ("outcomes", "expected_summary"),
    [
        ((ReferenceFrameOutcome.REUSED,), {"created": 0, "reused": 1, "failed": 0}),
        (
            (ReferenceFrameOutcome.CREATED, ReferenceFrameOutcome.REUSED),
            {"created": 1, "reused": 1, "failed": 0},
        ),
        (
            (ReferenceFrameOutcome.CREATED, ReferenceFrameNoCandidateError()),
            {"created": 1, "reused": 0, "failed": 1},
        ),
        (
            (ReferenceFrameNoCandidateError(), ReferenceFrameNoCandidateError()),
            {"created": 0, "reused": 0, "failed": 2},
        ),
    ],
)
def test_candidate_api_returns_created_reused_and_partial_outcomes(
    tmp_path: Path,
    outcomes: tuple[ReferenceFrameOutcome | ReferenceFrameNoCandidateError, ...],
    expected_summary: Mapping[str, int],
) -> None:
    reference_time = datetime(2026, 7, 20, 3, 34, 18, tzinfo=timezone.utc)
    offsets = tuple(index * 10 for index in range(len(outcomes)))
    client = _client(
        tmp_path,
        {
            reference_time + timedelta(seconds=offset): outcome
            for offset, outcome in zip(offsets, outcomes, strict=True)
        },
    )

    response = client.post(
        "/api/v1/reference-frame-candidate-sets",
        json={
            "channel_id": 1,
            "reference_time": "2026-07-20T03:34:18Z",
            "offsets_seconds": list(offsets),
        },
    )

    assert response.status_code == 200
    summary = ReferenceFrameCandidateSetResponse.model_validate_json(response.content).summary
    assert (summary.created, summary.reused, summary.failed) == (
        expected_summary["created"],
        expected_summary["reused"],
        expected_summary["failed"],
    )


@pytest.mark.parametrize(
    "offsets_seconds",
    [[], [0, 1, 2, 3, 4, 5], [0, 0], [-301], [301], [True], [1.0], ["1"]],
)
def test_candidate_api_rejects_invalid_offsets_safely(
    tmp_path: Path, offsets_seconds: list[int | bool | float | str]
) -> None:
    client = _client(tmp_path, {})

    response = client.post(
        "/api/v1/reference-frame-candidate-sets",
        json={
            "channel_id": 1,
            "reference_time": "2026-07-20T03:34:18Z",
            "offsets_seconds": offsets_seconds,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_candidate_api_redacts_unexpected_executor_failure(tmp_path: Path) -> None:
    marker = "rtsp://user:password@nvr.example/private"
    reference_time = datetime(2026, 7, 20, 3, 34, 18, tzinfo=timezone.utc)
    client = _client(tmp_path, {reference_time: RuntimeError(marker)})

    response = client.post(
        "/api/v1/reference-frame-candidate-sets",
        json={
            "channel_id": 1,
            "reference_time": "2026-07-20T03:34:18Z",
            "offsets_seconds": [0],
        },
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert marker not in response.text


def test_candidate_api_rejects_extra_secret_like_input_without_echoing_it(tmp_path: Path) -> None:
    marker = "rtsp://user:password@nvr.example/private"
    client = _client(tmp_path, {})

    response = client.post(
        "/api/v1/reference-frame-candidate-sets",
        json={
            "channel_id": 1,
            "reference_time": "2026-07-20T03:34:18Z",
            "unexpected": marker,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert marker not in response.text


def test_candidate_api_openapi_describes_candidate_set_route(tmp_path: Path) -> None:
    client = _client(tmp_path, {})

    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert "/api/v1/reference-frame-candidate-sets" in response.json()["paths"]


def test_candidate_api_uses_the_injected_single_slot_limiter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed_limiters: list[anyio.CapacityLimiter] = []

    async def fake_run_sync(
        function: Callable[[ReferenceFrameCandidateSetRequest], ReferenceFrameCandidateSetResult],
        request: ReferenceFrameCandidateSetRequest,
        limiter: anyio.CapacityLimiter,
    ) -> ReferenceFrameCandidateSetResult:
        observed_limiters.append(limiter)
        return function(request)

    monkeypatch.setattr(reference_frame_api, "run_sync", fake_run_sync)
    shared_limiter = anyio.CapacityLimiter(1)
    reference_time = datetime(2026, 7, 20, 3, 34, 18, tzinfo=timezone.utc)
    client = _client(
        tmp_path,
        {reference_time: ReferenceFrameOutcome.CREATED},
        limiter=shared_limiter,
    )

    response = client.post(
        "/api/v1/reference-frame-candidate-sets",
        json={
            "channel_id": 1,
            "reference_time": "2026-07-20T03:34:18Z",
            "offsets_seconds": [0],
        },
    )

    assert response.status_code == 200
    assert observed_limiters == [shared_limiter]
    assert shared_limiter.total_tokens == 1


def _client(
    tmp_path: Path,
    outcomes: Mapping[datetime, CandidateApiOutcome],
    limiter: anyio.CapacityLimiter | None = None,
) -> TestClient:
    image_path = tmp_path / "frame.jpg"
    _ = image_path.write_bytes(b"\xff\xd8frame\xff\xd9")
    return TestClient(
        create_reference_frame_app(
            FakeExecutor(outcomes),
            FakeResources(ReferenceFrameImageResource("channel-1_reference", image_path)),
            limiter,
        )
    )


def _resolution(
    request: ReferenceFrameRequest, outcome: ReferenceFrameOutcome
) -> ReferenceFrameResolution:
    requested_time = request.requested_time_utc
    segment = RecordingSegment(
        request.channel_id,
        requested_time.date(),
        int(requested_time.timestamp()),
        int((requested_time + timedelta(minutes=1)).timestamp()),
        requested_time,
        requested_time + timedelta(minutes=1),
    )
    result = ReferenceFrameResult(
        resource_id=f"channel-{request.channel_id}_{requested_time:%Y%m%dT%H%M%SZ}",
        manifest_schema_version=1,
        generation_policy_version=1,
        channel_id=request.channel_id,
        requested_time_text=request.requested_time_text,
        source_timezone=request.source_timezone,
        requested_time_utc=requested_time,
        selected_segment=segment,
        extraction_window=RecordingWindow(1, requested_time, requested_time + timedelta(seconds=6)),
        frame_selection_policy=FrameSelectionPolicy.NEAREST_DECODED_FRAME,
        jpeg_relative_path=Path("frame.jpg"),
        manifest_relative_path=Path("manifest.json"),
        width=2560,
        height=1440,
        decoded_local_pts_seconds=2.0,
        estimated_source_time_utc=None,
        offset_from_requested_seconds=None,
        timing_precision_status=TimingPrecisionStatus.MEASURED_CLIP_RELATIVE,
        warnings=(),
    )
    return ReferenceFrameResolution(result, outcome)
