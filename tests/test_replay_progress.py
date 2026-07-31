from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest
from pydantic import SecretStr

from vigi_vision.recording import RecordingWindow, ReplayRequest
from vigi_vision.replay import ReplayExtractor, ReplayTimeoutError
from vigi_vision.replay_progress import ReplayProgressDiagnostics


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
