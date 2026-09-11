import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest
from pydantic import SecretStr

from vigi_vision.recording import RecordingWindow, ReplayRequest
from vigi_vision.replay import ReplayExtractor, ReplayTimeoutError
from vigi_vision.replay_progress import ReplayProgressDiagnostics


def _lifecycle_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    prefix = "phase7e.replay_progress "
    return [
        json.loads(record.message.removeprefix(prefix))
        for record in caplog.records
        if record.message.startswith(prefix)
    ]


def test_progress_diagnostics_tracks_media_time_and_bytes_independently() -> None:
    diagnostics = ReplayProgressDiagnostics(requested_duration_seconds=6)

    diagnostics.observe_line("frame=10", now=1.0)
    diagnostics.observe_line("total_size=1024", now=1.0)
    diagnostics.observe_line("out_time_us=2000000", now=1.0)
    diagnostics.observe_line("progress=continue", now=1.0)
    diagnostics.observe_line("total_size=2048", now=3.0)
    diagnostics.observe_line("out_time_us=2000000", now=3.0)
    diagnostics.observe_line("progress=continue", now=3.0)

    summary = diagnostics.summary(now=5.0)

    assert summary.highest_frame == 10
    assert summary.highest_media_time_us == 2_000_000
    assert summary.highest_total_size == 2_048
    assert summary.last_progress_age_ms == 2_000
    assert summary.media_time_stalled_ms == 4_000
    assert summary.size_stalled_ms == 2_000
    assert summary.reached_requested_duration is False
    assert summary.progress_end_seen is False


def test_progress_diagnostics_ignores_malformed_unknown_and_partial_records() -> None:
    diagnostics = ReplayProgressDiagnostics(requested_duration_seconds=6)

    for line in ("unknown=secret", "frame=not-a-number", "out_time_us=-1", "partial"):
        diagnostics.observe_line(line, now=1.0)

    summary = diagnostics.summary(now=2.0)

    assert summary.highest_frame is None
    assert summary.highest_media_time_us is None
    assert summary.highest_total_size is None
    assert summary.last_progress_age_ms is None


def test_progress_diagnostics_prefers_microsecond_time_and_detects_end() -> None:
    diagnostics = ReplayProgressDiagnostics(requested_duration_seconds=6)

    diagnostics.observe_line("out_time_ms=500000", now=1.0)
    diagnostics.observe_line("out_time_us=6000000", now=1.0)
    diagnostics.observe_line("progress=end", now=1.0)

    summary = diagnostics.summary(now=2.0)

    assert summary.highest_media_time_us == 6_000_000
    assert summary.reached_requested_duration is True
    assert summary.progress_end_seen is True


def test_enabled_progress_adds_machine_protocol_and_logs_only_aggregates(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    start = datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc)
    request = ReplayRequest(
        RecordingWindow(1, start, start + timedelta(seconds=6)),
        "rtsp://nvr.example.test/replay",
    )

    def progress_timeout_runner(
        arguments: tuple[str, ...], timeout_seconds: float, diagnostics: ReplayProgressDiagnostics
    ) -> CompletedProcess[str]:
        assert "-progress" in arguments
        assert arguments[arguments.index("-progress") + 1] == "pipe:1"
        assert "-nostats" in arguments
        diagnostics.observe_line("frame=5", now=1.0)
        diagnostics.observe_line("total_size=1234", now=1.0)
        diagnostics.observe_line("out_time_us=2000000", now=1.0)
        diagnostics.observe_line("progress=continue", now=1.0)
        _ = Path(arguments[-1]).write_bytes(b"partial")
        raise TimeoutExpired(arguments, timeout_seconds)

    extractor = ReplayExtractor(
        executable=Path("ffmpeg.exe"),
        username="operator",
        password=SecretStr("password"),
        temporary_directory=tmp_path,
        progress_diagnostics=True,
        progress_runner=progress_timeout_runner,
    )

    with caplog.at_level("WARNING", logger="vigi_vision.replay"), pytest.raises(ReplayTimeoutError):
        _ = extractor.extract(request)

    progress_message = next(message for message in caplog.messages if "progress_timeout" in message)
    assert "frame=5" in progress_message
    assert "out_time_us=2000000" in progress_message
    assert "total_size=1234" in progress_message
    assert "operator" not in progress_message
    assert "nvr.example.test" not in progress_message
    assert "rtsp://" not in progress_message
    assert not tuple(tmp_path.glob("*.mp4"))


def test_replay_lifecycle_logs_success_and_cleans_up_without_sensitive_values(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("vigi_vision.replay._PROGRESS_POLL_INTERVAL_SECONDS", 0.01)

    def successful_runner(arguments: tuple[str, ...], _: float) -> CompletedProcess[str]:
        output_path = Path(arguments[-1])
        _ = output_path.write_bytes(b"first")
        time.sleep(0.03)
        _ = output_path.write_bytes(b"first-and-more")
        return CompletedProcess(arguments, 0, "", "stderr contains no event payload")

    extractor = ReplayExtractor(
        executable=Path("ffmpeg.exe"),
        username="operator",
        password=SecretStr("password"),
        temporary_directory=tmp_path,
        runner=successful_runner,
    )
    request = ReplayRequest(
        RecordingWindow(
            1,
            datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 20, 3, 0, 5, tzinfo=timezone.utc),
        ),
        "rtsp://nvr.example.test/replay",
    )

    with caplog.at_level("INFO"):
        clip = extractor.extract(request)
        clip.remove()

    events = _lifecycle_events(caplog)
    stages = [event["stage"] for event in events]
    assert stages[0] == "started"
    assert "first_output" in stages
    assert "output_progress" in stages
    assert stages[-2:] == ["process_exited", "cleanup_completed"]
    assert all(event["channel_id"] == 1 for event in events)
    joined = " ".join(record.message for record in caplog.records)
    assert "operator" not in joined
    assert "password" not in joined
    assert "rtsp://" not in joined
    assert str(tmp_path) not in joined
    assert "stderr contains" not in joined
    assert not tuple(tmp_path.glob("*.mp4"))


def test_replay_lifecycle_records_late_first_byte(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("vigi_vision.replay._PROGRESS_POLL_INTERVAL_SECONDS", 0.005)

    def delayed_runner(arguments: tuple[str, ...], _: float) -> CompletedProcess[str]:
        time.sleep(0.04)
        _ = Path(arguments[-1]).write_bytes(b"late")
        return CompletedProcess(arguments, 0)

    extractor = ReplayExtractor(
        executable=Path("ffmpeg.exe"),
        username="operator",
        password=SecretStr("password"),
        temporary_directory=tmp_path,
        runner=delayed_runner,
    )
    request = ReplayRequest(
        RecordingWindow(
            1,
            datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 20, 3, 0, 5, tzinfo=timezone.utc),
        ),
        "rtsp://nvr.example.test/replay",
    )

    with caplog.at_level("INFO"):
        clip = extractor.extract(request)
        clip.remove()

    events = _lifecycle_events(caplog)
    first_output = next(event for event in events if event["stage"] == "first_output")
    assert first_output["output_created"] is True
    assert first_output["output_size_bytes"] == 4
    assert first_output["elapsed_ms"] >= 20
    clip_path = tuple(tmp_path.glob("*.mp4"))
    assert not clip_path


@pytest.mark.parametrize("partial_size", [0, 7])
def test_replay_lifecycle_logs_timeout_and_cleanup_state(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    partial_size: int,
) -> None:
    partial = partial_size > 0

    def timeout_runner(arguments: tuple[str, ...], timeout_seconds: float) -> CompletedProcess[str]:
        if partial:
            _ = Path(arguments[-1]).write_bytes(b"partial")
        raise TimeoutExpired(arguments, timeout_seconds)

    extractor = ReplayExtractor(
        executable=Path("ffmpeg.exe"),
        username="operator",
        password=SecretStr("password"),
        temporary_directory=tmp_path,
        runner=timeout_runner,
    )

    with caplog.at_level("INFO"), pytest.raises(ReplayTimeoutError):
        _ = extractor.extract(
            ReplayRequest(
                RecordingWindow(
                    1,
                    datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc),
                    datetime(2026, 7, 20, 3, 0, 5, tzinfo=timezone.utc),
                ),
                "rtsp://nvr.example.test/replay",
            )
        )

    events = _lifecycle_events(caplog)
    stages = [event["stage"] for event in events]
    assert stages[0] == "started"
    assert "timeout_started" in stages
    assert "termination_requested" in stages
    assert "termination_completed" in stages
    assert stages[-1] == "cleanup_completed"
    timeout_event = next(event for event in events if event["stage"] == "timeout_started")
    assert timeout_event["output_created"] is partial
    assert timeout_event["output_size_bytes"] == partial_size
    cleanup_event = events[-1]
    assert cleanup_event["cleanup_outcome"] == "completed"
    assert not tuple(tmp_path.glob("*.mp4"))


def test_replay_lifecycle_logger_failure_does_not_change_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_logger(*_: object, **__: object) -> None:
        raise RuntimeError from None

    monkeypatch.setattr("vigi_vision.replay._PROGRESS_LOGGER.info", fail_logger)

    def successful_runner(arguments: tuple[str, ...], _: float) -> CompletedProcess[str]:
        _ = Path(arguments[-1]).write_bytes(b"mp4")
        return CompletedProcess(arguments, 0)

    extractor = ReplayExtractor(
        executable=Path("ffmpeg.exe"),
        username="operator",
        password=SecretStr("password"),
        temporary_directory=tmp_path,
        runner=successful_runner,
    )
    clip = extractor.extract(
        ReplayRequest(
            RecordingWindow(
                1,
                datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc),
                datetime(2026, 7, 20, 3, 0, 5, tzinfo=timezone.utc),
            ),
            "rtsp://nvr.example.test/replay",
        )
    )
    clip.remove()


def test_replay_lifecycle_retains_media_progress_when_process_stalls(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def progress_timeout_runner(
        arguments: tuple[str, ...], timeout_seconds: float, diagnostics: ReplayProgressDiagnostics
    ) -> CompletedProcess[str]:
        diagnostics.observe_line("out_time_us=5000000", now=1.0)
        diagnostics.observe_line("progress=continue", now=1.0)
        _ = Path(arguments[-1]).write_bytes(b"partial")
        raise TimeoutExpired(arguments, timeout_seconds)

    extractor = ReplayExtractor(
        executable=Path("ffmpeg.exe"),
        username="operator",
        password=SecretStr("password"),
        temporary_directory=tmp_path,
        progress_diagnostics=True,
        progress_runner=progress_timeout_runner,
    )

    with caplog.at_level("INFO"), pytest.raises(ReplayTimeoutError):
        _ = extractor.extract(
            ReplayRequest(
                RecordingWindow(
                    1,
                    datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc),
                    datetime(2026, 7, 20, 3, 0, 5, tzinfo=timezone.utc),
                ),
                "rtsp://nvr.example.test/replay",
            )
        )

    events = _lifecycle_events(caplog)
    timeout_event = next(event for event in events if event["stage"] == "timeout_started")
    assert timeout_event["ffmpeg_out_time_ms"] == 5_000
    assert timeout_event["process_running"] is True
    assert events[-1]["stage"] == "cleanup_completed"


def test_replay_lifecycle_caps_output_progress_events(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("vigi_vision.replay._PROGRESS_POLL_INTERVAL_SECONDS", 0.005)

    def noisy_runner(arguments: tuple[str, ...], _: float) -> CompletedProcess[str]:
        output_path = Path(arguments[-1])
        for _ in range(40):
            with output_path.open("ab") as output:
                _ = output.write(b"x" * 5_000)
            time.sleep(0.006)
        return CompletedProcess(arguments, 0)

    extractor = ReplayExtractor(
        executable=Path("ffmpeg.exe"),
        username="operator",
        password=SecretStr("password"),
        temporary_directory=tmp_path,
        runner=noisy_runner,
    )
    request = ReplayRequest(
        RecordingWindow(
            1,
            datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 20, 3, 0, 5, tzinfo=timezone.utc),
        ),
        "rtsp://nvr.example.test/replay",
    )

    with caplog.at_level("INFO"):
        clip = extractor.extract(request)
        clip.remove()

    events = _lifecycle_events(caplog)
    progress_events = [event for event in events if event["stage"] == "output_progress"]
    assert 1 <= len(progress_events) <= 16
    assert events[-2]["stage"] == "process_exited"
    assert events[-1]["stage"] == "cleanup_completed"
