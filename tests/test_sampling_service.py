from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import final

import pytest

from vigi_vision.investigation_snapshot import AnchorSnapshotError
from vigi_vision.recording import RecordingUnavailableError, RecordingWindow, ReplayRequest
from vigi_vision.replay import ReplayClip, ReplayExtractionError
from vigi_vision.sampling import RecordingCoverage, SamplingChunk, SamplingRequest
from vigi_vision.sampling_service import (
    SamplingCancelledError,
    SamplingExecutionError,
    SamplingService,
)


@final
class FakeResolver:
    def __init__(self, coverage: tuple[RecordingCoverage, ...]) -> None:
        self._coverage = coverage

    def coverage(self, request: SamplingRequest) -> tuple[RecordingCoverage, ...]:
        _ = request
        return self._coverage

    def replay_request(self, chunk: SamplingChunk, channel_id: int) -> ReplayRequest:
        start = chunk.start_utc
        end = chunk.end_utc
        return ReplayRequest(RecordingWindow(channel_id, start, end), "rtsp://safe.example/replay")


@final
class FakeReplayExtractor:
    def __init__(self, temporary_directory: Path, fail_after: int | None = None) -> None:
        self._temporary_directory = temporary_directory
        self._fail_after = fail_after
        self._index = 0

    def extract(self, request: ReplayRequest) -> ReplayClip:
        self._index += 1
        if self._fail_after is not None and self._index > self._fail_after:
            raise ReplayExtractionError
        path = self._temporary_directory / f"replay-{self._index}.mp4"
        _ = path.write_bytes(b"mp4")
        return ReplayClip(
            request.window.channel_id,
            request.window.start_utc,
            request.window.end_utc,
            request.replay_url,
            path,
            request.window.duration_seconds,
        )


@final
class FakeFrameExtractor:
    def __init__(self, fail_after: int | None = None, interrupt_after: int | None = None) -> None:
        self._fail_after = fail_after
        self._interrupt_after = interrupt_after
        self._calls = 0

    def extract(self, video_path: Path, anchor_offset_seconds: int, output_path: Path) -> Path:
        _ = video_path
        _ = anchor_offset_seconds
        self._calls += 1
        if self._interrupt_after is not None and self._calls > self._interrupt_after:
            raise KeyboardInterrupt
        if self._fail_after is not None and self._calls > self._fail_after:
            raise AnchorSnapshotError
        _ = output_path.write_bytes(b"jpeg")
        return output_path


def test_sampling_service_writes_safe_complete_package(tmp_path: Path) -> None:
    # Given
    request = _request()
    service = SamplingService(
        FakeResolver((RecordingCoverage(request.start_utc, request.end_utc),)),
        FakeReplayExtractor(tmp_path),
        FakeFrameExtractor(),
        tmp_path / "artifacts",
    )

    # When
    result = service.execute(request)

    # Then
    manifest_text = (result.artifact_directory / "manifest.json").read_text()
    assert result.status == "completed"
    assert result.written_frame_count == 5
    assert tuple(path.name for path in (result.artifact_directory / "frames").glob("*.jpg")) == (
        "20260726T090000Z.jpg",
        "20260726T090005Z.jpg",
        "20260726T090010Z.jpg",
        "20260726T090015Z.jpg",
        "20260726T090020Z.jpg",
    )
    assert '"source_timezone": "Asia/Seoul"' in manifest_text
    assert '"status": "completed"' in manifest_text
    assert "rtsp://" not in manifest_text
    assert "password" not in manifest_text


def test_sampling_service_preserves_partial_package_after_frame_failure(tmp_path: Path) -> None:
    # Given
    request = _request()
    service = SamplingService(
        FakeResolver((RecordingCoverage(request.start_utc, request.end_utc),)),
        FakeReplayExtractor(tmp_path),
        FakeFrameExtractor(fail_after=2),
        tmp_path / "artifacts",
    )

    # When / Then
    with pytest.raises(SamplingExecutionError) as error:
        _ = service.execute(request)

    manifest_text = (error.value.artifact_directory / "manifest.json").read_text()
    assert '"status": "failed"' in manifest_text
    assert '"status": "failed_extraction"' in manifest_text
    assert not tuple(tmp_path.glob("replay-*.mp4"))


def test_sampling_service_records_replay_failure_in_partial_manifest(tmp_path: Path) -> None:
    # Given
    request = _request()
    service = SamplingService(
        FakeResolver((RecordingCoverage(request.start_utc, request.end_utc),)),
        FakeReplayExtractor(tmp_path, fail_after=1),
        FakeFrameExtractor(),
        tmp_path / "artifacts",
    )

    # When / Then
    with pytest.raises(SamplingExecutionError) as error:
        _ = service.execute(request)

    manifest_text = (error.value.artifact_directory / "manifest.json").read_text()
    assert '"failure_category": "replay_extraction_failed"' in manifest_text


def test_sampling_service_rejects_existing_complete_artifact_path(tmp_path: Path) -> None:
    # Given
    request = _request()
    service = SamplingService(
        FakeResolver((RecordingCoverage(request.start_utc, request.end_utc),)),
        FakeReplayExtractor(tmp_path),
        FakeFrameExtractor(),
        tmp_path / "artifacts",
    )
    _ = service.execute(request)

    # When / Then
    with pytest.raises(FileExistsError):
        _ = service.execute(request)


def test_sampling_service_preserves_cancelled_partial_package(tmp_path: Path) -> None:
    # Given
    request = _request()
    service = SamplingService(
        FakeResolver((RecordingCoverage(request.start_utc, request.end_utc),)),
        FakeReplayExtractor(tmp_path),
        FakeFrameExtractor(interrupt_after=2),
        tmp_path / "artifacts",
    )

    # When / Then
    with pytest.raises(SamplingCancelledError) as error:
        _ = service.execute(request)

    manifest_text = (error.value.artifact_directory / "manifest.json").read_text()
    assert '"status": "cancelled"' in manifest_text
    assert not tuple(tmp_path.glob("replay-*.mp4"))


def test_sampling_service_refuses_empty_recording_coverage(tmp_path: Path) -> None:
    # Given
    service = SamplingService(
        FakeResolver(()),
        FakeReplayExtractor(tmp_path),
        FakeFrameExtractor(),
        tmp_path / "artifacts",
    )

    # When / Then
    with pytest.raises(RecordingUnavailableError):
        _ = service.execute(_request())

    assert not (tmp_path / "artifacts").exists()


def _request() -> SamplingRequest:
    start = datetime(2026, 7, 26, 9, 0, tzinfo=timezone.utc)
    return SamplingRequest(
        3,
        "2026-07-26 18:00:00",
        "Asia/Seoul",
        start,
        start + timedelta(seconds=25),
        5,
        10,
    )
