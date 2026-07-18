from pathlib import Path
from secrets import token_urlsafe

import pytest
from typer.testing import CliRunner

from vigi_vision import cli, nvr
from vigi_vision.analysis import OpenAiAnalyzer, SceneAnalysis
from vigi_vision.channel_selection import Channel
from vigi_vision.config import Settings
from vigi_vision.profiles import EntranceAnalysis, ProfileDefinition
from vigi_vision.workflow import InspectionResult, InspectionWorkflow

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


def _ipc_settings(_: Path) -> Settings:
    return Settings.model_validate(
        {
            "OPENAI_API_KEY": _TEST_OPENAI_KEY,
            "VIGI_SOURCE": "ipc",
            "VIGI_IPC_HOST": "ipc.example.invalid",
            "VIGI_IPC_USERNAME": "operator",
            "VIGI_IPC_PASSWORD": _TEST_PASSWORD,
        }
    )


def _channels(_: nvr.SdkNvrGateway) -> tuple[Channel, ...]:
    return (
        Channel(channel_id=1, name="Front", alias="Entrance", online=True),
        Channel(channel_id=2, name="Back", alias="Delivery", online=False),
    )


def test_cli_shows_help_for_inspect_command() -> None:
    # Given
    runner = CliRunner()

    # When
    result = runner.invoke(cli.app, ["inspect", "--help"])

    # Then
    assert result.exit_code == 0
    assert "Acquire and analyze one current frame" in result.stdout


def test_cli_channels_displays_only_safe_channel_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    monkeypatch.setattr(cli, "load_settings", _settings)
    monkeypatch.setattr(nvr.SdkNvrGateway, "channels", _channels)
    runner = CliRunner()

    # When
    result = runner.invoke(cli.app, ["channels"])

    # Then
    assert result.exit_code == 0
    assert result.stdout == (
        "channel=1 name=Front alias=Entrance online=yes\n"
        "channel=2 name=Back alias=Delivery online=no\n"
    )


def test_cli_channels_rejects_ipc_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    monkeypatch.setattr(cli, "load_settings", _ipc_settings)
    runner = CliRunner()

    # When
    result = runner.invoke(cli.app, ["channels"])

    # Then
    assert result.exit_code == 1
    assert result.stdout == "Error: channels is available only when VIGI_SOURCE=nvr.\n"


def test_cli_inspect_renders_a_wrapped_demonstration_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(cli, "load_settings", _settings)

    def _run(_: InspectionWorkflow) -> InspectionResult:
        return InspectionResult(
            label="Standalone IPC",
            channel=None,
            snapshot_path=Path("artifacts/snapshots/latest.jpg"),
            analysis=SceneAnalysis(
                summary="A very long scene summary " * 8,
                person_visible=False,
                notable_observations=("A delivery box is visible.", "Lighting is limited."),
                limitations="This result reflects one current frame only.",
            ),
        )

    monkeypatch.setattr(InspectionWorkflow, "run", _run)
    runner = CliRunner()

    # When
    result = runner.invoke(cli.app, ["inspect"])

    # Then
    assert result.exit_code == 0
    assert "VIGI Vision — Live Scene Inspection" in result.stdout
    assert "Not available" in result.stdout
    assert "Scene Summary" in result.stdout
    assert "• A delivery box is visible." in result.stdout
    assert "• Lighting is limited." in result.stdout
    assert "Inspection completed successfully." in result.stdout
    assert all(len(line) <= 100 for line in result.stdout.splitlines())


def test_cli_analyze_image_uses_profile_without_ffmpeg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given
    image_path = tmp_path / "captured-frame.jpg"
    _ = image_path.write_bytes(b"jpeg")
    monkeypatch.setattr(cli, "load_settings", _settings)

    def _analyze_profile(
        _: OpenAiAnalyzer, analyzed_path: Path, profile: ProfileDefinition
    ) -> EntranceAnalysis:
        assert analyzed_path == image_path
        assert profile.name == "entrance"
        return EntranceAnalysis(
            estimated_shoe_pairs_on_rack=3,
            estimated_shoe_pairs_on_floor=1,
            person_near_entrance=False,
            entrance_clear=True,
            scattered_footwear=True,
            notable_observations=("Shoes are visible near the entrance.",),
            limitations="Counts are estimates from one still frame.",
        )

    def _ffmpeg_must_not_run() -> Path:
        raise AssertionError

    monkeypatch.setattr(OpenAiAnalyzer, "analyze_profile", _analyze_profile)
    monkeypatch.setattr(cli, "resolve_ffmpeg", _ffmpeg_must_not_run)
    runner = CliRunner()

    # When
    result = runner.invoke(cli.app, ["analyze-image", str(image_path), "--profile", "entrance"])

    # Then
    assert result.exit_code == 0
    assert "VIGI Vision — Image Analysis (entrance)" in result.stdout
    assert "Estimated Shoe Pairs on Rack" in result.stdout
    assert "Image analysis completed successfully." in result.stdout
