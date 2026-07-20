from pathlib import Path
from secrets import token_urlsafe

import pytest
from typer.testing import CliRunner

from vigi_vision import cli
from vigi_vision.channel_selection import Channel
from vigi_vision.config import CaptureSettings, load_capture_settings
from vigi_vision.ffmpeg import FfmpegExtractionError, FfmpegExtractor
from vigi_vision.nvr import SdkNvrGateway

_TEST_PASSWORD = token_urlsafe()


def _capture_settings(source: str = "nvr") -> CaptureSettings:
    return CaptureSettings.model_validate(
        {
            "VIGI_SOURCE": source,
            "VIGI_HOST": "nvr.example.invalid",
            "VIGI_USERNAME": "operator",
            "VIGI_PASSWORD": _TEST_PASSWORD,
        }
    )


def _online_channel(channel_id: int = 1) -> Channel:
    return Channel(channel_id, "Front", "Entrance", online=True)


def test_capture_settings_do_not_require_openai_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_contents = (
        "\n".join(
            (
                "VIGI_SOURCE=nvr",
                "VIGI_HOST=nvr.example.invalid",
                "VIGI_USERNAME=operator",
                f"VIGI_PASSWORD={_TEST_PASSWORD}",
            )
        )
        + "\n"
    )
    _written = env_file.write_text(env_contents, encoding="utf-8")

    settings = load_capture_settings(env_file)

    assert settings.nvr_connection.host == "nvr.example.invalid"


def test_cli_snapshot_captures_selected_channel_without_openai_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given
    captured_channel_ids: list[int] = []

    def _settings(_: Path) -> CaptureSettings:
        return _capture_settings()

    def _channels(_: SdkNvrGateway) -> tuple[Channel, ...]:
        return (_online_channel(),)

    def _live_url(_: SdkNvrGateway, channel_id: int, stream: str) -> str:
        captured_channel_ids.append(channel_id)
        assert stream == "main"
        return "rtsp://nvr.example.invalid/live/1/1/avm"

    def _extract(
        _: FfmpegExtractor,
        live_url: str,
        *,
        username: str,
        password: str,
        output_path: Path,
    ) -> Path:
        assert live_url == "rtsp://nvr.example.invalid/live/1/1/avm"
        assert username == "operator"
        assert password == _TEST_PASSWORD
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _bytes_written = output_path.write_bytes(b"jpeg")
        return output_path

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("vigi_vision.snapshot_cli.load_capture_settings", _settings)
    monkeypatch.setattr("vigi_vision.snapshot_cli.SdkNvrGateway.channels", _channels)
    monkeypatch.setattr("vigi_vision.snapshot_cli.SdkNvrGateway.live_url", _live_url)
    monkeypatch.setattr("vigi_vision.snapshot_cli.FfmpegExtractor.extract", _extract)
    runner = CliRunner()

    # When
    result = runner.invoke(cli.app, ["snapshot", "--channel", "1"])

    # Then
    assert result.exit_code == 0
    assert captured_channel_ids == [1]
    assert "VIGI Vision — Channel Snapshot" in result.stdout
    assert "Channel\n-------\n1" in result.stdout
    assert "artifacts/channel-snapshots/channel-1-" in result.stdout
    assert "Snapshot captured successfully." in result.stdout
    assert tuple((tmp_path / "artifacts" / "channel-snapshots").glob("channel-1-*.jpg"))


@pytest.mark.parametrize("requested_channel", [2, 3])
def test_cli_snapshot_rejects_missing_or_offline_channel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, requested_channel: int
) -> None:
    # Given
    def _settings(_: Path) -> CaptureSettings:
        return _capture_settings()

    def _channels(_: SdkNvrGateway) -> tuple[Channel, ...]:
        return (_online_channel(1), Channel(2, "Back", "Delivery", online=False))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("vigi_vision.snapshot_cli.load_capture_settings", _settings)
    monkeypatch.setattr("vigi_vision.snapshot_cli.SdkNvrGateway.channels", _channels)
    runner = CliRunner()

    # When
    result = runner.invoke(cli.app, ["snapshot", "--channel", str(requested_channel)])

    # Then
    assert result.exit_code == 1
    assert "configured VIGI_CHANNEL_ID is missing or offline" in result.stdout
    assert "password" not in result.stdout
    assert not tuple(tmp_path.rglob("*.jpg"))


def test_cli_snapshot_rejects_unsupported_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given
    def _settings(_: Path) -> CaptureSettings:
        return _capture_settings("ipc")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("vigi_vision.snapshot_cli.load_capture_settings", _settings)
    runner = CliRunner()

    # When
    result = runner.invoke(cli.app, ["snapshot", "--channel", "1"])

    # Then
    assert result.exit_code == 1
    assert "snapshot is available only when VIGI_SOURCE=nvr" in result.stdout


def test_cli_snapshot_removes_partial_file_when_capture_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given
    def _settings(_: Path) -> CaptureSettings:
        return _capture_settings()

    def _channels(_: SdkNvrGateway) -> tuple[Channel, ...]:
        return (_online_channel(),)

    def _live_url(_: SdkNvrGateway, _channel_id: int, _stream: str) -> str:
        return "rtsp://nvr.example.invalid/live/1/1/avm"

    def _extract(
        _: FfmpegExtractor,
        _live_url: str,
        *,
        username: str,
        password: str,
        output_path: Path,
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if not username or not password:
            raise AssertionError
        _bytes_written = output_path.write_bytes(b"partial")
        raise FfmpegExtractionError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("vigi_vision.snapshot_cli.load_capture_settings", _settings)
    monkeypatch.setattr("vigi_vision.snapshot_cli.SdkNvrGateway.channels", _channels)
    monkeypatch.setattr("vigi_vision.snapshot_cli.SdkNvrGateway.live_url", _live_url)
    monkeypatch.setattr("vigi_vision.snapshot_cli.FfmpegExtractor.extract", _extract)
    runner = CliRunner()

    # When
    result = runner.invoke(cli.app, ["snapshot", "--channel", "1"])

    # Then
    assert result.exit_code == 1
    assert "ffmpeg could not extract a frame" in result.stdout
    assert not tuple(tmp_path.rglob("*.jpg"))


def test_cli_snapshot_rejects_non_positive_channel() -> None:
    # Given
    runner = CliRunner()

    # When
    result = runner.invoke(cli.app, ["snapshot", "--channel", "0"])

    # Then
    assert result.exit_code == 2
    assert "x>=1" in result.output
