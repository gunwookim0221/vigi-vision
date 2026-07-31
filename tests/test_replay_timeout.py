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


def test_replay_timeout_preserves_only_opted_in_diagnostic_partial(tmp_path: Path) -> None:
    # Given
    diagnostic_directory = tmp_path / "timeout-diagnostics"

    def timing_out_runner(
        arguments: tuple[str, ...], timeout_seconds: float
    ) -> CompletedProcess[str]:
        _ = Path(arguments[-1]).write_bytes(b"partial")
        raise TimeoutExpired(arguments, timeout_seconds)

    extractor = ReplayExtractor(
        executable=Path("ffmpeg.exe"),
        username="operator",
        password=SecretStr("password"),
        temporary_directory=tmp_path / "temporary",
        timeout_diagnostic_directory=diagnostic_directory,
        runner=timing_out_runner,
    )

    # When / Then
    with pytest.raises(ReplayTimeoutError):
        _ = extractor.extract(_request(6))

    preserved = tuple(diagnostic_directory.glob("*.mp4"))
    assert len(preserved) == 1
    assert preserved[0].read_bytes() == b"partial"
    assert preserved[0].name == "channel-1-20260720T030000Z-timeout.mp4"
    assert "operator" not in preserved[0].name
    assert "password" not in preserved[0].name
    assert "nvr.example.test" not in preserved[0].name
    assert not tuple((tmp_path / "temporary").glob("*.mp4"))


def test_replay_timeout_never_overwrites_existing_diagnostic_file(tmp_path: Path) -> None:
    # Given
    diagnostic_directory = tmp_path / "timeout-diagnostics"
    diagnostic_directory.mkdir()
    existing = diagnostic_directory / "channel-1-20260720T030000Z-timeout.mp4"
    _ = existing.write_bytes(b"existing")

    def timing_out_runner(
        arguments: tuple[str, ...], timeout_seconds: float
    ) -> CompletedProcess[str]:
        _ = Path(arguments[-1]).write_bytes(b"partial")
        raise TimeoutExpired(arguments, timeout_seconds)

    extractor = ReplayExtractor(
        executable=Path("ffmpeg.exe"),
        username="operator",
        password=SecretStr("password"),
        temporary_directory=tmp_path / "temporary",
        timeout_diagnostic_directory=diagnostic_directory,
        runner=timing_out_runner,
    )

    # When / Then
    with pytest.raises(ReplayTimeoutError):
        _ = extractor.extract(_request(6))

    assert existing.read_bytes() == b"existing"
    assert not tuple((tmp_path / "temporary").glob("*.mp4"))


def test_replay_timeout_logs_only_safe_window_and_output_facts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
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

    # When
    with caplog.at_level("WARNING", logger="vigi_vision.replay"), pytest.raises(ReplayTimeoutError):
        _ = extractor.extract(_request(6))

    # Then
    assert len(caplog.messages) == 1
    message = caplog.messages[0]
    assert "replay.timeout" in message
    assert "channel_id=1" in message
    assert "window_start_utc=2026-07-20T03:00:00+00:00" in message
    assert "window_end_utc=2026-07-20T03:00:06+00:00" in message
    assert "duration_seconds=6" in message
    assert "elapsed_ms=" in message
    assert "partial_output_bytes=7" in message
    assert "operator" not in message
    assert "password" not in message
    assert "nvr.example.test" not in message
    assert "rtsp://" not in message


def test_replay_extraction_removes_partial_file_on_keyboard_interrupt(tmp_path: Path) -> None:
    # Given
    def interrupted_runner(arguments: tuple[str, ...], _: float) -> CompletedProcess[str]:
        _ = Path(arguments[-1]).write_bytes(b"partial")
        raise KeyboardInterrupt

    extractor = ReplayExtractor(
        executable=Path("ffmpeg.exe"),
        username="operator",
        password=SecretStr("password"),
        temporary_directory=tmp_path,
        runner=interrupted_runner,
    )

    # When / Then
    with pytest.raises(KeyboardInterrupt):
        _ = extractor.extract(_request(30))

    assert not tuple(tmp_path.glob("*.mp4"))
