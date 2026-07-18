from pathlib import Path
from secrets import token_urlsafe

import pytest

from vigi_vision.config import IpcConnection, Settings, load_settings

_TEST_OPENAI_KEY = token_urlsafe()
_TEST_PASSWORD = token_urlsafe()
_CONFIGURATION_KEYS = (
    "OPENAI_API_KEY",
    "VIGI_HOST",
    "VIGI_USERNAME",
    "VIGI_PASSWORD",
    "VIGI_PORT",
    "VIGI_VERIFY_SSL",
    "VIGI_CHANNEL_ID",
    "VIGI_STREAM",
    "VIGI_SOURCE",
    "VIGI_IPC_HOST",
    "VIGI_IPC_USERNAME",
    "VIGI_IPC_PASSWORD",
    "FFMPEG_PATH",
)


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "OPENAI_API_KEY": _TEST_OPENAI_KEY,
            "VIGI_HOST": "nvr.example.invalid",
            "VIGI_USERNAME": "operator",
            "VIGI_PASSWORD": _TEST_PASSWORD,
        }
    )


def _clear_configuration_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _CONFIGURATION_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_load_settings_loads_dotenv_from_current_working_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given
    monkeypatch.chdir(tmp_path)
    _clear_configuration_environment(monkeypatch)
    env_file = tmp_path / ".env"
    _ = env_file.write_text(
        "\n".join(
            (
                f"OPENAI_API_KEY={_TEST_OPENAI_KEY}",
                "VIGI_HOST=nvr.example.invalid",
                "VIGI_USERNAME=operator",
                f"VIGI_PASSWORD={_TEST_PASSWORD}",
            )
        ),
        encoding="utf-8",
    )

    # When
    settings = load_settings()

    # Then
    assert settings.vigi_port == 20443
    assert settings.vigi_verify_ssl is True
    assert settings.vigi_channel_id is None
    assert settings.vigi_stream == "main"


def test_load_settings_prefers_operating_system_environment_over_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given
    monkeypatch.chdir(tmp_path)
    _clear_configuration_environment(monkeypatch)
    _ = (tmp_path / ".env").write_text(
        "\n".join(
            (
                f"OPENAI_API_KEY={_TEST_OPENAI_KEY}",
                "VIGI_HOST=dotenv.example.invalid",
                "VIGI_USERNAME=operator",
                f"VIGI_PASSWORD={_TEST_PASSWORD}",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("VIGI_HOST", "environment.example.invalid")

    # When
    settings = load_settings()

    # Then
    assert settings.vigi_host == "environment.example.invalid"


def test_load_settings_rejects_missing_required_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given
    monkeypatch.chdir(tmp_path)
    _clear_configuration_environment(monkeypatch)

    # When / Then
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        _ = load_settings()


def test_load_settings_rejects_invalid_channel_id(tmp_path: Path) -> None:
    # Given
    env_file = tmp_path / ".env"
    _ = env_file.write_text(
        "\n".join(
            (
                f"OPENAI_API_KEY={_TEST_OPENAI_KEY}",
                "VIGI_HOST=nvr.example.invalid",
                "VIGI_USERNAME=operator",
                f"VIGI_PASSWORD={_TEST_PASSWORD}",
                "VIGI_CHANNEL_ID=0",
            )
        ),
        encoding="utf-8",
    )

    # When / Then
    with pytest.raises(ValueError, match="VIGI_CHANNEL_ID"):
        _ = load_settings(env_file)


def test_load_settings_treats_blank_optional_values_as_unset(tmp_path: Path) -> None:
    # Given
    env_file = tmp_path / ".env"
    _ = env_file.write_text(
        "\n".join(
            (
                f"OPENAI_API_KEY={_TEST_OPENAI_KEY}",
                "VIGI_HOST=nvr.example.invalid",
                "VIGI_USERNAME=operator",
                f"VIGI_PASSWORD={_TEST_PASSWORD}",
                "VIGI_CHANNEL_ID=",
                "FFMPEG_PATH=",
            )
        ),
        encoding="utf-8",
    )

    # When
    settings = load_settings(env_file)

    # Then
    assert settings.vigi_channel_id is None
    assert settings.ffmpeg_path is None


def test_settings_redacts_secret_values() -> None:
    # Given
    settings = _settings()

    # When
    representation = repr(settings)

    # Then
    assert _TEST_OPENAI_KEY not in representation
    assert _TEST_PASSWORD not in representation


def test_load_settings_accepts_ipc_values_without_nvr_values(tmp_path: Path) -> None:
    # Given
    env_file = tmp_path / ".env"
    _ = env_file.write_text(
        "\n".join(
            (
                f"OPENAI_API_KEY={_TEST_OPENAI_KEY}",
                "VIGI_SOURCE=ipc",
                "VIGI_IPC_HOST=ipc.example.invalid",
                "VIGI_IPC_USERNAME=operator",
                f"VIGI_IPC_PASSWORD={_TEST_PASSWORD}",
                "VIGI_STREAM=minor",
            )
        ),
        encoding="utf-8",
    )

    # When
    settings = load_settings(env_file)

    # Then
    assert settings.vigi_source == "ipc"
    assert settings.vigi_stream == "minor"
    assert settings.ipc_connection == IpcConnection(
        host="ipc.example.invalid",
        username="operator",
        password=settings.ipc_connection.password,
        stream="minor",
    )


def test_load_settings_requires_values_for_selected_source(tmp_path: Path) -> None:
    # Given
    env_file = tmp_path / ".env"
    _ = env_file.write_text(
        "\n".join(
            (
                f"OPENAI_API_KEY={_TEST_OPENAI_KEY}",
                "VIGI_SOURCE=ipc",
                "VIGI_IPC_HOST=ipc.example.invalid",
            )
        ),
        encoding="utf-8",
    )

    # When / Then
    with pytest.raises(ValueError, match="VIGI_IPC_USERNAME"):
        _ = load_settings(env_file)
