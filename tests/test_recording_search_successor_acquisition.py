from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typing_extensions import override

from vigi_vision.recording_models import RecordingSegment, RecordingWindow, ReplayRequest
from vigi_vision.recording_search_successor import (
    MultiSegmentCoarsePlan,
    SuccessorPlanRequest,
    TargetAvailability,
    build_successor_plan,
)
from vigi_vision.recording_search_successor_acquisition import (
    SuccessorTargetAcquisitionService,
    SuccessorTargetStatus,
    build_successor_target_window,
    successor_target_id,
)
from vigi_vision.reference_frame_decoder import (
    FfmpegReferenceFrameDecoder,
    ReferenceFrameDecoder,
    ReferenceFrameDecodeRequest,
)
from vigi_vision.reference_frame_models import (
    DecodedFrameEvidence,
    ReferenceFrameNoCandidateError,
    TimingPrecisionStatus,
)
from vigi_vision.replay import (
    ReplayClip,
    ReplayExtractionError,
    ReplayTimeoutError,
    ReplayUnavailableError,
)

UTC = timezone.utc
ANCHOR = datetime(2026, 9, 4, 5, 17, 32, tzinfo=UTC)


def _segment(start: datetime, end: datetime) -> RecordingSegment:
    return RecordingSegment(
        1,
        start.date(),
        int(start.timestamp()),
        int(end.timestamp()),
        start,
        end,
    )


def _plan(
    segments: tuple[RecordingSegment, ...] | None = None,
    duration_seconds: int = 1_800,
) -> MultiSegmentCoarsePlan:
    request = SuccessorPlanRequest(
        1,
        ANCHOR,
        ANCHOR + timedelta(seconds=duration_seconds),
        "Asia/Seoul",
    )
    return build_successor_plan(
        request,
        segments if segments is not None else (_segment(ANCHOR, request.search_end_utc),),
    )


class _FakePlanner:
    def __init__(self) -> None:
        self.windows: list[tuple[RecordingSegment, RecordingWindow]] = []

    def plan_for_segment(self, segment: RecordingSegment, window: RecordingWindow) -> ReplayRequest:
        self.windows.append((segment, window))
        return ReplayRequest(window, "rtsp://example.invalid/replay")


class _FakeExtractor:
    def __init__(self, temporary_directory: Path, outcomes: list[object] | None = None) -> None:
        self.temporary_directory: Path = temporary_directory
        self.outcomes: list[object] = list(outcomes or ())
        self.calls: list[ReplayRequest] = []
        self.paths: list[Path] = []

    def extract(self, request: ReplayRequest) -> ReplayClip:
        self.calls.append(request)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
        path = self.temporary_directory / f"clip-{len(self.calls)}.mp4"
        _ = path.write_bytes(b"bounded-mp4")
        self.paths.append(path)
        return ReplayClip(
            request.window.channel_id,
            request.window.start_utc,
            request.window.end_utc,
            request.replay_url,
            path,
            request.window.duration_seconds,
        )


class _FakeDecoder:
    def __init__(self, pts_seconds: float = 5.0) -> None:
        self.pts_seconds: float = pts_seconds
        self.calls: list[ReferenceFrameDecodeRequest] = []

    def decode(self, request: ReferenceFrameDecodeRequest) -> DecodedFrameEvidence:
        self.calls.append(request)
        _ = request.output_path.write_bytes(b"jpeg-frame")
        return DecodedFrameEvidence(
            request.output_path,
            self.pts_seconds,
            64,
            48,
            TimingPrecisionStatus.MEASURED_CLIP_RELATIVE,
            (),
        )


def _service(
    tmp_path: Path,
    *,
    decoder: ReferenceFrameDecoder | None = None,
    outcomes: list[object] | None = None,
) -> tuple[SuccessorTargetAcquisitionService, _FakePlanner, _FakeExtractor]:
    planner = _FakePlanner()
    extractor = _FakeExtractor(tmp_path, outcomes)
    service = SuccessorTargetAcquisitionService(
        planner,
        extractor,
        decoder or _FakeDecoder(),
        temporary_directory=tmp_path,
    )
    return service, planner, extractor


def test_normal_target_window_is_at_most_ten_seconds() -> None:
    plan = _plan()
    target = plan.targets[0]

    window = build_successor_target_window(plan, target)

    assert window is not None
    assert window.start_utc == target.requested_time_utc - timedelta(seconds=5)
    assert window.end_utc == target.requested_time_utc + timedelta(seconds=5)
    assert window.duration_seconds == 10


def test_window_clips_to_segment_start_and_end() -> None:
    target_time = ANCHOR + timedelta(minutes=10)
    plan = _plan(
        (
            _segment(ANCHOR, target_time - timedelta(seconds=2)),
            _segment(target_time - timedelta(seconds=2), target_time + timedelta(seconds=2)),
            _segment(target_time + timedelta(seconds=2), ANCHOR + timedelta(minutes=30)),
        )
    )
    target = plan.targets[0]

    window = build_successor_target_window(plan, target)

    assert window is not None
    assert window.start_utc == target_time - timedelta(seconds=2)
    assert window.end_utc == target_time + timedelta(seconds=2)


def test_final_target_window_clips_to_search_end() -> None:
    plan = _plan(duration_seconds=600)
    target = plan.targets[-1]

    window = build_successor_target_window(plan, target)

    assert window is not None
    assert window.start_utc == target.requested_time_utc - timedelta(seconds=5)
    assert window.end_utc == plan.search_end_utc


def test_gap_target_never_calls_replay(tmp_path: Path) -> None:
    gap_start = ANCHOR + timedelta(minutes=5)
    gap_end = ANCHOR + timedelta(minutes=15)
    plan = _plan((_segment(ANCHOR, gap_start), _segment(gap_end, ANCHOR + timedelta(minutes=30))))
    target = next(
        item for item in plan.targets if item.availability is TargetAvailability.UNAVAILABLE
    )
    service, planner, extractor = _service(tmp_path)

    result = service.acquire(plan, target)

    assert result.status is SuccessorTargetStatus.UNAVAILABLE_GAP
    assert result.replay_window is None
    assert not planner.windows
    assert not extractor.calls


def test_nearest_actual_pts_and_offset_are_retained(tmp_path: Path) -> None:
    plan = _plan()
    target = plan.targets[0]
    service, planner, extractor = _service(tmp_path, decoder=_FakeDecoder(4.25))

    result = service.acquire(plan, target)

    assert result.status is SuccessorTargetStatus.FRAME_AVAILABLE
    assert result.requested_time_utc == target.requested_time_utc
    assert result.frame_pts_seconds is not None
    assert abs(result.frame_pts_seconds - 4.25) < 0.000001
    assert result.replay_window is not None
    assert result.frame_utc == result.replay_window.start_utc + timedelta(seconds=4.25)
    assert result.frame_utc != result.requested_time_utc
    assert result.frame_offset_seconds is not None
    assert abs(result.frame_offset_seconds + 0.75) < 0.000001
    assert result.assigned_segment_id == target.segment_id
    assert result.frame_bytes == b"jpeg-frame"
    assert len(planner.windows) == len(extractor.calls) == 1
    assert not extractor.paths[0].exists()


def test_decoder_without_frames_is_explicitly_unavailable(tmp_path: Path) -> None:
    class NoFrameDecoder(_FakeDecoder):
        @override
        def decode(self, request: ReferenceFrameDecodeRequest) -> DecodedFrameEvidence:
            raise ReferenceFrameNoCandidateError

    plan = _plan()
    service, _, extractor = _service(tmp_path, decoder=NoFrameDecoder())

    result = service.acquire(plan, plan.targets[0])

    assert result.status is SuccessorTargetStatus.DECODE_UNAVAILABLE
    assert result.frame_bytes is None
    assert not extractor.paths[0].exists()


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ReplayUnavailableError(), SuccessorTargetStatus.RECORDING_UNAVAILABLE),
        (ReplayTimeoutError(), SuccessorTargetStatus.REPLAY_TIMEOUT),
        (ReplayExtractionError(), SuccessorTargetStatus.REPLAY_FAILED),
    ],
)
def test_replay_failures_are_target_local(
    tmp_path: Path, error: Exception, status: SuccessorTargetStatus
) -> None:
    plan = _plan()
    service, _, extractor = _service(tmp_path, outcomes=[error])

    result = service.acquire(plan, plan.targets[0])

    assert result.status is status
    assert result.frame_bytes is None
    assert not extractor.paths


def test_one_target_failure_does_not_stop_other_targets(tmp_path: Path) -> None:
    plan = _plan()
    service, _, extractor = _service(tmp_path, outcomes=[ReplayUnavailableError()])

    results = service.acquire_plan(plan)

    assert results[0].status is SuccessorTargetStatus.RECORDING_UNAVAILABLE
    assert results[1].status is SuccessorTargetStatus.FRAME_AVAILABLE
    assert len(extractor.calls) == len(plan.targets)
    assert all(not path.exists() for path in extractor.paths)


def test_same_input_has_stable_target_and_acquisition_identity(tmp_path: Path) -> None:
    plan = _plan()
    target = plan.targets[0]
    service, _, extractor = _service(tmp_path)

    first = service.acquire(plan, target)
    second = service.acquire(plan, target)

    assert successor_target_id(plan, target) == first.target_id == second.target_id
    assert first.acquisition_id == second.acquisition_id
    assert first is not second
    assert first.status is second.status is SuccessorTargetStatus.FRAME_AVAILABLE
    assert first.frame_sha256 == second.frame_sha256
    assert len(extractor.calls) == 2
    assert not hasattr(service, "_cache")


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required for the production-shaped media check",
)
def test_production_decoder_selects_actual_pts_from_short_media(tmp_path: Path) -> None:
    ffmpeg = Path(shutil.which("ffmpeg") or "ffmpeg")
    ffprobe = Path(shutil.which("ffprobe") or "ffprobe")
    clip_path = tmp_path / "short.mp4"
    generated = subprocess.run(  # noqa: S603
        (
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x48:rate=10",
            "-t",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(clip_path),
        ),
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert generated.returncode == 0
    plan = _plan()

    class FixtureExtractor:
        def extract(self, request: ReplayRequest) -> ReplayClip:
            return ReplayClip(
                request.window.channel_id,
                request.window.start_utc,
                request.window.end_utc,
                request.replay_url,
                clip_path,
                request.window.duration_seconds,
            )

    planner = _FakePlanner()
    service = SuccessorTargetAcquisitionService(
        planner,
        FixtureExtractor(),
        FfmpegReferenceFrameDecoder(ffmpeg, ffprobe),
        temporary_directory=tmp_path,
    )

    result = service.acquire(plan, plan.targets[0])

    assert result.status is SuccessorTargetStatus.FRAME_AVAILABLE
    assert result.frame_pts_seconds is not None
    assert result.frame_utc is not None
    assert result.frame_utc != result.requested_time_utc
    assert result.frame_size_bytes == len(result.frame_bytes or b"")
    assert not clip_path.exists()
