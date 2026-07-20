import traceback
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from typing import final

import pytest
from pydantic import SecretStr
from vigi import (
    RecordDay,
    RecordDaysResponse,
    RecordSearchProcessResponse,
    RecordSearchResultsResponse,
)
from vigi import (
    RecordSegment as SdkRecordSegment,
)

from vigi_vision.recording import (
    RecordingPlanner,
    RecordingUnavailableError,
    RecordingWindow,
)
from vigi_vision.replay import (
    ReplayAuthenticationError,
    ReplayExtractionError,
    ReplayExtractor,
    ReplayTimeoutError,
    ReplayUnavailableError,
)


@final
class FakeRecords:
    results: tuple[SdkRecordSegment, ...]
    days_calls: list[tuple[int, str, str]]
    results_calls: list[tuple[int, int, str, int, int]]

    def __init__(self, results: tuple[SdkRecordSegment, ...]) -> None:
        self.results = results
        self.days_calls = []
        self.results_calls = []

    def list_days(self, channel_id: int, start_month: str, end_month: str) -> RecordDaysResponse:
        self.days_calls.append((channel_id, start_month, end_month))
        return RecordDaysResponse(days=(RecordDay(day="20260701"),), error_code=0)

    def get_free_process(self) -> RecordSearchProcessResponse:
        return RecordSearchProcessResponse(process_id=3, error_code=0)

    def list_results(
        self,
        channel_id: int,
        process_id: int,
        day: str,
        start_index: int = 0,
        end_index: int = 99,
    ) -> RecordSearchResultsResponse:
        self.results_calls.append((channel_id, process_id, day, start_index, end_index))
        return RecordSearchResultsResponse(results=self.results, error_code=0)


@final
class FakeStream:
    def build_replay_url(
        self,
        host: str,
        channel_id: int,
        start_time: str,
        end_time: str,
        stream: int = 1,
    ) -> str:
        return (
            f"rtsp://{host}/replay/{channel_id}/{stream}/avm?"
            f"starttime={start_time}&endtime={end_time}"
        )


@final
class FakeClient:
    records: FakeRecords
    stream: FakeStream

    def __init__(self, records: FakeRecords) -> None:
        self.records = records
        self.stream = FakeStream()


def _window() -> RecordingWindow:
    return RecordingWindow(
        channel_id=1,
        start_utc=datetime(2026, 6, 30, 16, 0, 10, tzinfo=timezone.utc),
        end_utc=datetime(2026, 6, 30, 16, 0, 40, tzinfo=timezone.utc),
    )


def test_recording_planner_builds_utc_replay_request_for_overlapping_segment() -> None:
    # Given
    records = FakeRecords((SdkRecordSegment(start_time="1782831600", end_time="1782850188"),))
    planner = RecordingPlanner(FakeClient(records), "nvr.example.test")

    # When
    request = planner.plan(_window())

    # Then
    assert request.window.duration_seconds == 30
    assert request.replay_url == (
        "rtsp://nvr.example.test/replay/1/1/avm?starttime=20260630t160010z&endtime=20260630t160040z"
    )
    assert records.days_calls == [(1, "202607", "202607")]
    assert records.results_calls == [(1, 3, "20260701", 0, 99)]


def test_recording_planner_raises_when_no_segment_overlaps_requested_window() -> None:
    # Given
    records = FakeRecords((SdkRecordSegment(start_time="1782831600", end_time="1782831610"),))
    planner = RecordingPlanner(FakeClient(records), "nvr.example.test")

    # When / Then
    with pytest.raises(RecordingUnavailableError, match="No recording"):
        _ = planner.plan(_window())


def test_replay_extractor_writes_removable_mp4_with_tcp_and_client_duration(
    tmp_path: Path,
) -> None:
    # Given
    captured_arguments: tuple[str, ...] = ()

    def successful_runner(arguments: tuple[str, ...], _: float) -> CompletedProcess[str]:
        nonlocal captured_arguments
        captured_arguments = arguments
        _ = Path(arguments[-1]).write_bytes(b"mp4")
        return CompletedProcess(arguments, 0)

    extractor = ReplayExtractor(
        executable=Path("ffmpeg.exe"),
        username="operator",
        password=SecretStr("password"),
        temporary_directory=tmp_path,
        runner=successful_runner,
    )
    planner = RecordingPlanner(
        FakeClient(
            FakeRecords((SdkRecordSegment(start_time="1782831600", end_time="1782850188"),))
        ),
        "nvr.example.test",
    )

    # When
    clip = extractor.extract(planner.plan(_window()))

    # Then
    assert clip.duration_seconds == 30
    assert clip.temporary_mp4_path.is_file()
    assert clip.replay_url.startswith("rtsp://nvr.example.test/")
    assert "password" not in clip.replay_url
    assert captured_arguments[5:7] == ("-rtsp_transport", "tcp")
    assert captured_arguments[11:13] == ("-t", "30")
    clip.remove()
    assert not clip.temporary_mp4_path.exists()


@pytest.mark.parametrize(
    ("stderr", "error_type"),
    [
        ("method DESCRIBE failed: 401 (Unauthorized)", ReplayAuthenticationError),
        ("method PLAY failed: 454 (Session Not Found)", ReplayUnavailableError),
    ],
)
def test_replay_extractor_classifies_rtsp_failure_without_persisting_partial_file(
    tmp_path: Path,
    stderr: str,
    error_type: type[RuntimeError],
) -> None:
    # Given
    def failed_runner(arguments: tuple[str, ...], _: float) -> CompletedProcess[str]:
        _ = Path(arguments[-1]).write_bytes(b"partial")
        return CompletedProcess(arguments, 1, stderr=stderr)

    extractor = ReplayExtractor(
        executable=Path("ffmpeg.exe"),
        username="operator",
        password=SecretStr("password"),
        temporary_directory=tmp_path,
        runner=failed_runner,
    )
    planner = RecordingPlanner(
        FakeClient(
            FakeRecords((SdkRecordSegment(start_time="1782831600", end_time="1782850188"),))
        ),
        "nvr.example.test",
    )

    # When / Then
    with pytest.raises(error_type):
        _ = extractor.extract(planner.plan(_window()))

    assert not tuple(tmp_path.glob("*.mp4"))


def test_replay_extractor_timeout_redacts_credentials_and_removes_partial_file(
    tmp_path: Path,
) -> None:
    # Given
    def timing_out_runner(arguments: tuple[str, ...], timeout: float) -> CompletedProcess[str]:
        _ = Path(arguments[-1]).write_bytes(b"partial")
        raise TimeoutExpired(arguments, timeout)

    extractor = ReplayExtractor(
        executable=Path("ffmpeg.exe"),
        username="operator",
        password=SecretStr("password"),
        temporary_directory=tmp_path,
        runner=timing_out_runner,
    )
    planner = RecordingPlanner(
        FakeClient(
            FakeRecords((SdkRecordSegment(start_time="1782831600", end_time="1782850188"),))
        ),
        "nvr.example.test",
    )

    # When / Then
    with pytest.raises(ReplayTimeoutError, match="timed out") as exc_info:
        _ = extractor.extract(planner.plan(_window()))

    rendered_traceback = "".join(
        traceback.format_exception(exc_info.type, exc_info.value, exc_info.tb)
    )
    assert "password" not in rendered_traceback

    assert not tuple(tmp_path.glob("*.mp4"))


def test_replay_extractor_removes_temp_file_when_replay_url_validation_fails(
    tmp_path: Path,
) -> None:
    extractor = ReplayExtractor(
        executable=Path("ffmpeg.exe"),
        username="operator",
        password=SecretStr("password"),
        temporary_directory=tmp_path,
    )
    request = RecordingPlanner(
        FakeClient(
            FakeRecords((SdkRecordSegment(start_time="1782831600", end_time="1782850188"),))
        ),
        "nvr.example.test",
    ).plan(_window())
    malformed_request = request.__class__(window=request.window, replay_url="http://invalid")

    with pytest.raises(ReplayExtractionError):
        _ = extractor.extract(malformed_request)

    assert not tuple(tmp_path.glob("*.mp4"))
