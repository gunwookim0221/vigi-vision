from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_urlsafe

import pytest
from typer.testing import CliRunner

from vigi_vision import cli
from vigi_vision.config import NvrConnection, Settings
from vigi_vision.recording import RecordingWindow, ReplayRequest
from vigi_vision.replay import (
    ReplayAuthenticationError,
    ReplayClip,
    ReplayExtractor,
    ReplayUnavailableError,
)

_TEST_OPENAI_KEY = token_urlsafe()
_TEST_PASSWORD = token_urlsafe()


def _settings(_: Path) -> Settings:
    return Settings.model_validate(
        {
            "OPENAI_API_KEY": _TEST_OPENAI_KEY,
            "VIGI_HOST": "nvr.example.invalid",
            "VIGI_USERNAME": "operator",
            "VIGI_PASSWORD": _TEST_PASSWORD,
        }
    )


@dataclass(frozen=True, slots=True)
class FakePlanner:
    def plan(self, window: RecordingWindow) -> ReplayRequest:
        return ReplayRequest(window=window, replay_url="rtsp://nvr.example.invalid/replay")


def _clip(path: Path) -> ReplayClip:
    return ReplayClip(
        channel_id=1,
        requested_start_utc=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        requested_end_utc=datetime(2026, 7, 20, 12, 0, 30, tzinfo=timezone.utc),
        replay_url="rtsp://nvr.example.invalid/replay",
        temporary_mp4_path=path,
        duration_seconds=30,
    )


def test_cli_analyze_recording_reuses_video_analysis_and_removes_replay_clip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given
    replay_path = tmp_path / "replay.mp4"
    _ = replay_path.write_bytes(b"mp4")
    clip = _clip(replay_path)
    rendered: list[tuple[Path, str]] = []

    def _connect(_: NvrConnection) -> FakePlanner:
        return FakePlanner()

    def _extract(_: ReplayExtractor, request: ReplayRequest) -> ReplayClip:
        assert request.window.channel_id == 1
        assert request.window.duration_seconds == 30
        return clip

    def _analyze(video_path: Path, profile: str, _: Settings) -> str:
        assert video_path == replay_path
        assert profile == "counter"
        return "analysis"

    def _render(video_path: Path, analysis: str) -> None:
        rendered.append((video_path, analysis))

    monkeypatch.setattr("vigi_vision.recording_cli.load_settings", _settings)
    monkeypatch.setattr("vigi_vision.recording_cli.RecordingPlanner.connect", _connect)
    monkeypatch.setattr(ReplayExtractor, "extract", _extract)
    monkeypatch.setattr("vigi_vision.recording_cli.analyze_video_file", _analyze)
    monkeypatch.setattr("vigi_vision.recording_cli.print_temporal_report", _render)
    runner = CliRunner()

    # When
    result = runner.invoke(
        cli.app,
        [
            "analyze-recording",
            "--channel",
            "1",
            "--start",
            "2026-07-20 12:00:00",
            "--duration",
            "30s",
            "--profile",
            "counter",
        ],
    )

    # Then
    assert result.exit_code == 0
    assert rendered == [(replay_path, "analysis")]
    assert not replay_path.exists()


def test_cli_analyze_recording_removes_replay_clip_when_video_analysis_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given
    replay_path = tmp_path / "replay.mp4"
    _ = replay_path.write_bytes(b"mp4")
    clip = _clip(replay_path)

    def _connect(_: NvrConnection) -> FakePlanner:
        return FakePlanner()

    def _extract(_: ReplayExtractor, _request: ReplayRequest) -> ReplayClip:
        return clip

    def _fail(_: Path, _profile: str, _settings: Settings) -> str:
        raise ReplayAuthenticationError

    monkeypatch.setattr("vigi_vision.recording_cli.load_settings", _settings)
    monkeypatch.setattr("vigi_vision.recording_cli.RecordingPlanner.connect", _connect)
    monkeypatch.setattr(ReplayExtractor, "extract", _extract)
    monkeypatch.setattr("vigi_vision.recording_cli.analyze_video_file", _fail)
    runner = CliRunner()

    # When
    result = runner.invoke(
        cli.app,
        [
            "analyze-recording",
            "--channel",
            "1",
            "--start",
            "2026-07-20 12:00:00",
            "--duration",
            "30s",
            "--profile",
            "counter",
        ],
    )

    # Then
    assert result.exit_code == 1
    assert "The NVR rejected the RTSP credentials." in result.stdout
    assert not replay_path.exists()


def test_cli_analyze_recording_renders_unavailable_replay_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def _connect(_: NvrConnection) -> FakePlanner:
        return FakePlanner()

    def _extract(_: ReplayExtractor, _request: ReplayRequest) -> ReplayClip:
        raise ReplayUnavailableError

    monkeypatch.setattr("vigi_vision.recording_cli.load_settings", _settings)
    monkeypatch.setattr("vigi_vision.recording_cli.RecordingPlanner.connect", _connect)
    monkeypatch.setattr(ReplayExtractor, "extract", _extract)
    runner = CliRunner()

    # When
    result = runner.invoke(
        cli.app,
        [
            "analyze-recording",
            "--channel",
            "1",
            "--start",
            "2026-07-20 12:00:00",
            "--duration",
            "30s",
            "--profile",
            "counter",
        ],
    )

    # Then
    assert result.exit_code == 1
    assert "The NVR has no replay available for the requested time window." in result.stdout


def test_cli_analyze_recording_rejects_invalid_utc_window() -> None:
    # Given
    runner = CliRunner()

    # When
    result = runner.invoke(
        cli.app,
        [
            "analyze-recording",
            "--channel",
            "1",
            "--start",
            "2026-07-20T12:00:00Z",
            "--duration",
            "30",
            "--profile",
            "counter",
        ],
    )

    # Then
    assert result.exit_code == 1
    assert "Start must use UTC format YYYY-MM-DD HH:MM:SS." in result.stdout
