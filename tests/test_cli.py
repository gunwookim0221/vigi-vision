from pathlib import Path
from secrets import token_urlsafe

import pytest
from typer.testing import CliRunner

from vigi_vision import cli, nvr
from vigi_vision.channel_selection import Channel
from vigi_vision.config import Settings

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
