from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vigi_vision import cli
from vigi_vision.config import CaptureSettings
from vigi_vision.sampling import (
    RawSamplingInput,
    RecordingCoverage,
    SamplingInputError,
    SamplingRequest,
    build_sampling_plan,
    parse_sampling_request,
)


def test_parse_sampling_request_converts_source_time_to_utc() -> None:
    # Given
    start = "2026-07-26 18:00:00"

    # When
    request = parse_sampling_request(RawSamplingInput(3, start, "Asia/Seoul", "2h", "5s", "10m"))

    # Then
    assert request.start_utc == datetime(2026, 7, 26, 9, 0, tzinfo=timezone.utc)
    assert request.end_utc == datetime(2026, 7, 26, 11, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", ["0s", "1d", "five", "5.5s"])
def test_parse_sampling_request_rejects_invalid_duration_values(value: str) -> None:
    # Given / When / Then
    with pytest.raises(SamplingInputError):
        _ = parse_sampling_request(
            RawSamplingInput(3, "2026-07-26 18:00:00", "Asia/Seoul", value, "5s", "10m")
        )


def test_build_sampling_plan_keeps_schedule_across_chunks_and_gaps() -> None:
    # Given
    start = datetime(2026, 7, 26, 9, 0, tzinfo=timezone.utc)
    request = SamplingRequest(
        3,
        "2026-07-26 18:00:00",
        "Asia/Seoul",
        start,
        start + timedelta(seconds=25),
        5,
        10,
    )
    coverage = (
        RecordingCoverage(start, start + timedelta(seconds=12)),
        RecordingCoverage(start + timedelta(seconds=18), start + timedelta(seconds=25)),
    )

    # When
    plan = build_sampling_plan(request, coverage)

    # Then
    assert tuple(point.timestamp_utc for point in plan.written_points) == (
        start,
        start + timedelta(seconds=5),
        start + timedelta(seconds=10),
        start + timedelta(seconds=20),
    )
    assert tuple(point.timestamp_utc for point in plan.skipped_points) == (
        start + timedelta(seconds=15),
    )
    assert tuple((chunk.start_utc, chunk.end_utc) for chunk in plan.chunks) == (
        (start, start + timedelta(seconds=10)),
        (start + timedelta(seconds=10), start + timedelta(seconds=12)),
        (start + timedelta(seconds=18), start + timedelta(seconds=25)),
    )


def test_sample_recording_help_lists_bounded_sampling_options() -> None:
    # Given
    runner = CliRunner()

    # When
    result = runner.invoke(cli.app, ["sample-recording", "--help"])

    # Then
    assert result.exit_code == 0
    assert "--interval" in result.stdout
    assert "--chunk-duration" in result.stdout
    assert "[default: 10m]" in result.stdout


def test_sample_recording_rejects_ipc_before_sdk_or_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    settings = CaptureSettings.model_validate(
        {
            "VIGI_SOURCE": "ipc",
            "VIGI_IPC_HOST": "ipc.example.invalid",
            "VIGI_IPC_USERNAME": "operator",
            "VIGI_IPC_PASSWORD": "test-password",
        }
    )

    def load_ipc_settings(_: Path) -> CaptureSettings:
        return settings

    monkeypatch.setattr("vigi_vision.sampling_cli.load_capture_settings", load_ipc_settings)
    runner = CliRunner()

    # When
    result = runner.invoke(
        cli.app,
        [
            "sample-recording",
            "--channel",
            "3",
            "--start",
            "2026-07-26 18:00:00",
            "--duration",
            "2h",
            "--interval",
            "5s",
        ],
    )

    # Then
    assert result.exit_code == 1
    assert "sample-recording is available only when VIGI_SOURCE=nvr." in result.stdout
