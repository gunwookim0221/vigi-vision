from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest
from pydantic import SecretStr

from vigi_vision.recording import RecordingWindow, ReplayRequest
from vigi_vision.replay import ReplayExtractor, ReplayTimeoutError


def _request(duration_seconds: int) -> ReplayRequest:
    start = datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc)
    return ReplayRequest(
        window=RecordingWindow(
            channel_id=1,
            start_utc=start,
            end_utc=start.replace(second=start.second + duration_seconds),
        ),
        replay_url="rtsp://nvr.example.test/replay",
    )


@pytest.mark.parametrize(
    ("duration_seconds", "expected_timeout_seconds"),
    [(10, 50.0), (30, 70.0)],
)
def test_replay_timeout_budget_includes_startup_and_finalization_margin(
    tmp_path: Path,
    duration_seconds: int,
    expected_timeout_seconds: float,
) -> None:
    # Given
    observed_timeout: list[float] = []

    def successful_runner(
        arguments: tuple[str, ...], timeout_seconds: float
    ) -> CompletedProcess[str]:
        observed_timeout.append(timeout_seconds)
        _ = Path(arguments[-1]).write_bytes(b"mp4")
        return CompletedProcess(arguments, 0)

    extractor = ReplayExtractor(
        executable=Path("ffmpeg.exe"),
        username="operator",
        password=SecretStr("password"),
        temporary_directory=tmp_path,
        runner=successful_runner,
    )

    # When
    clip = extractor.extract(_request(duration_seconds))

    # Then
    assert observed_timeout == [expected_timeout_seconds]
    clip.remove()


def test_replay_extraction_succeeds_after_observed_startup_latency(
    tmp_path: Path,
) -> None:
    # Given
    startup_latency_seconds = 5.56
    observed_timeout: list[float] = []

    def startup_runner(arguments: tuple[str, ...], timeout_seconds: float) -> CompletedProcess[str]:
        observed_timeout.append(timeout_seconds)
        assert timeout_seconds > startup_latency_seconds + 30
        _ = Path(arguments[-1]).write_bytes(b"mp4")
        return CompletedProcess(arguments, 0)

    extractor = ReplayExtractor(
        executable=Path("ffmpeg.exe"),
        username="operator",
        password=SecretStr("password"),
        temporary_directory=tmp_path,
        runner=startup_runner,
    )

    # When
    clip = extractor.extract(_request(30))

    # Then
    assert observed_timeout == [70.0]
    assert clip.temporary_mp4_path.is_file()
    clip.remove()


def test_replay_timeout_removes_partial_file_and_redacts_credentials(
    tmp_path: Path,
) -> None:
    # Given
    def timing_out_runner(
        arguments: tuple[str, ...], timeout_seconds: float
    ) -> CompletedProcess[str]:
        _ = Path(arguments[-1]).write_bytes(b"partial")
        raise TimeoutExpired(arguments, timeout_seconds)

    extractor = ReplayExtractor(
        executable=Path("ffmpeg.exe"),
        username="operator",
        password=SecretStr("password"),
        temporary_directory=tmp_path,
        runner=timing_out_runner,
    )

    # When / Then
    with pytest.raises(ReplayTimeoutError) as exception_info:
        _ = extractor.extract(_request(30))

    assert "password" not in str(exception_info.value)
    assert not tuple(tmp_path.glob("*.mp4"))
