"""Typed runtime configuration for VIGI Vision."""

from dataclasses import dataclass
from os import environ
from pathlib import Path
from typing import ClassVar, Final, Literal

from dotenv import dotenv_values
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing_extensions import Self, override

_IPC_SOURCE: Final = "ipc"
_NVR_SOURCE: Final = "nvr"
_VIGI_HOST: Final = "VIGI_HOST"
_VIGI_IPC_HOST: Final = "VIGI_IPC_HOST"
_VIGI_IPC_USERNAME: Final = "VIGI_IPC_USERNAME"
_VIGI_USERNAME: Final = "VIGI_USERNAME"

_OPTIONAL_ENVIRONMENT_KEYS: Final = frozenset(
    {
        "FFMPEG_PATH",
        "VIGI_CHANNEL_ID",
        "VIGI_HOST",
        "VIGI_IPC_HOST",
        "VIGI_IPC_PASSWORD",
        "VIGI_IPC_USERNAME",
        "VIGI_PASSWORD",
        "VIGI_USERNAME",
    }
)


@dataclass(frozen=True, slots=True)
class NvrConnection:
    """Validated NVR values consumed by the public SDK gateway."""

    host: str
    username: SecretStr
    password: SecretStr
    port: int
    verify_ssl: bool
    channel_id: int | None
    stream: Literal["main", "minor"]


@dataclass(frozen=True, slots=True)
class IpcConnection:
    """Validated standard-RTSP IPC values consumed by the IPC gateway."""

    host: str
    username: str
    password: SecretStr
    stream: Literal["main", "minor"]


@dataclass(frozen=True, slots=True)
class SourceConfigurationError(RuntimeError):
    """Raised only if an already-validated source connection is unavailable."""

    source: Literal["nvr", "ipc"]

    @override
    def __str__(self) -> str:
        """Return a secret-safe configuration invariant failure."""
        return f"Required {self.source} source settings are unavailable."


@dataclass(frozen=True, slots=True)
class MissingSourceSettingError(ValueError):
    """Raised when the selected source lacks one required environment value."""

    setting: str
    source: Literal["nvr", "ipc"]

    @override
    def __str__(self) -> str:
        """Return a safe source-specific environment-setting error."""
        return f"{self.setting} is required when VIGI_SOURCE={self.source}."


class Settings(BaseSettings):
    """Validate environment-supplied configuration without exposing secrets."""

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(extra="ignore")

    openai_api_key: SecretStr = Field(min_length=1, validation_alias="OPENAI_API_KEY")
    vigi_source: Literal["nvr", "ipc"] = Field(default=_NVR_SOURCE, validation_alias="VIGI_SOURCE")
    vigi_host: str | None = Field(default=None, min_length=1, validation_alias="VIGI_HOST")
    vigi_username: SecretStr | None = Field(default=None, validation_alias="VIGI_USERNAME")
    vigi_password: SecretStr | None = Field(default=None, validation_alias="VIGI_PASSWORD")
    vigi_port: int = Field(default=20443, gt=0, le=65535, validation_alias="VIGI_PORT")
    vigi_verify_ssl: bool = Field(default=True, validation_alias="VIGI_VERIFY_SSL")
    vigi_channel_id: int | None = Field(default=None, gt=0, validation_alias="VIGI_CHANNEL_ID")
    vigi_stream: Literal["main", "minor"] = Field(default="main", validation_alias="VIGI_STREAM")
    vigi_ipc_host: str | None = Field(default=None, min_length=1, validation_alias="VIGI_IPC_HOST")
    vigi_ipc_username: str | None = Field(
        default=None,
        min_length=1,
        validation_alias=_VIGI_IPC_USERNAME,
    )
    vigi_ipc_password: SecretStr | None = Field(
        default=None,
        validation_alias="VIGI_IPC_PASSWORD",
    )
    ffmpeg_path: Path | None = Field(default=None, validation_alias="FFMPEG_PATH")

    @model_validator(mode="after")
    def validate_selected_source(self) -> Self:
        """Require credentials only for the selected RTSP source."""
        match self.vigi_source:
            case "nvr":
                _require_nvr_values(self)
            case "ipc":
                _require_ipc_values(self)
        return self

    @property
    def nvr_connection(self) -> NvrConnection:
        """Return NVR values after selected-source validation."""
        match self.vigi_host, self.vigi_username, self.vigi_password:
            case str() as host, SecretStr() as username, SecretStr() as password:
                return NvrConnection(
                    host=host,
                    username=username,
                    password=password,
                    port=self.vigi_port,
                    verify_ssl=self.vigi_verify_ssl,
                    channel_id=self.vigi_channel_id,
                    stream=self.vigi_stream,
                )
            case _:
                raise SourceConfigurationError(_NVR_SOURCE)

    @property
    def ipc_connection(self) -> IpcConnection:
        """Return IPC values after selected-source validation."""
        match self.vigi_ipc_host, self.vigi_ipc_username, self.vigi_ipc_password:
            case str() as host, str() as username, SecretStr() as password:
                return IpcConnection(
                    host=host,
                    username=username,
                    password=password,
                    stream=self.vigi_stream,
                )
            case _:
                raise SourceConfigurationError(_IPC_SOURCE)


class LocalAnalysisSettings(BaseSettings):
    """Validate only the values needed for analysis of an existing local file."""

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(extra="ignore")

    openai_api_key: SecretStr = Field(min_length=1, validation_alias="OPENAI_API_KEY")
    ffmpeg_path: Path | None = Field(default=None, validation_alias="FFMPEG_PATH")


class CaptureSettings(BaseSettings):
    """Validate only the source and media-tool values needed for snapshots."""

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(extra="ignore")

    vigi_source: Literal["nvr", "ipc"] = Field(default=_NVR_SOURCE, validation_alias="VIGI_SOURCE")
    vigi_host: str | None = Field(default=None, min_length=1, validation_alias="VIGI_HOST")
    vigi_username: SecretStr | None = Field(default=None, validation_alias="VIGI_USERNAME")
    vigi_password: SecretStr | None = Field(default=None, validation_alias="VIGI_PASSWORD")
    vigi_port: int = Field(default=20443, gt=0, le=65535, validation_alias="VIGI_PORT")
    vigi_verify_ssl: bool = Field(default=True, validation_alias="VIGI_VERIFY_SSL")
    vigi_stream: Literal["main", "minor"] = Field(default="main", validation_alias="VIGI_STREAM")
    ffmpeg_path: Path | None = Field(default=None, validation_alias="FFMPEG_PATH")

    @model_validator(mode="after")
    def validate_selected_source(self) -> Self:
        """Require NVR credentials only when the NVR source is selected."""
        if self.vigi_source == _NVR_SOURCE:
            if self.vigi_host is None:
                raise MissingSourceSettingError(_VIGI_HOST, _NVR_SOURCE)
            if self.vigi_username is None:
                raise MissingSourceSettingError(_VIGI_USERNAME, _NVR_SOURCE)
            if self.vigi_password is None:
                raise MissingSourceSettingError(_secret_key(_VIGI_HOST), _NVR_SOURCE)
        return self

    @property
    def nvr_connection(self) -> NvrConnection:
        """Return validated NVR values without requiring an OpenAI key."""
        match self.vigi_host, self.vigi_username, self.vigi_password:
            case str() as host, SecretStr() as username, SecretStr() as password:
                return NvrConnection(
                    host=host,
                    username=username,
                    password=password,
                    port=self.vigi_port,
                    verify_ssl=self.vigi_verify_ssl,
                    channel_id=None,
                    stream=self.vigi_stream,
                )
            case _:
                raise SourceConfigurationError(_NVR_SOURCE)


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from process variables and an optional local dotenv file."""
    dotenv_file = Path(".env") if env_file is None else env_file
    file_values = {
        key: value for key, value in dotenv_values(dotenv_file).items() if value is not None
    }
    values = {**file_values, **environ}
    normalized_values = {
        key: value
        for key, value in values.items()
        if value != "" or key not in _OPTIONAL_ENVIRONMENT_KEYS
    }
    return Settings.model_validate(normalized_values)


def load_local_analysis_settings(env_file: Path | None = None) -> LocalAnalysisSettings:
    """Load the API-key and media-tool settings without requiring a camera source."""
    dotenv_file = Path(".env") if env_file is None else env_file
    file_values = {
        key: value for key, value in dotenv_values(dotenv_file).items() if value is not None
    }
    values = {**file_values, **environ}
    normalized_values = {
        key: value
        for key, value in values.items()
        if value != "" or key not in _OPTIONAL_ENVIRONMENT_KEYS
    }
    return LocalAnalysisSettings.model_validate(normalized_values)


def load_capture_settings(env_file: Path | None = None) -> CaptureSettings:
    """Load capture settings while ignoring the optional OpenAI configuration."""
    dotenv_file = Path(".env") if env_file is None else env_file
    file_values = {
        key: value for key, value in dotenv_values(dotenv_file).items() if value is not None
    }
    values = {**file_values, **environ}
    normalized_values = {
        key: value
        for key, value in values.items()
        if value != "" or key not in _OPTIONAL_ENVIRONMENT_KEYS
    }
    return CaptureSettings.model_validate(normalized_values)


def _require_nvr_values(settings: Settings) -> None:
    if settings.vigi_host is None:
        raise MissingSourceSettingError(_VIGI_HOST, _NVR_SOURCE)
    if settings.vigi_username is None:
        raise MissingSourceSettingError(_VIGI_USERNAME, _NVR_SOURCE)
    if settings.vigi_password is None:
        raise MissingSourceSettingError(_secret_key(_VIGI_HOST), _NVR_SOURCE)


def _require_ipc_values(settings: Settings) -> None:
    if settings.vigi_ipc_host is None:
        raise MissingSourceSettingError(_VIGI_IPC_HOST, _IPC_SOURCE)
    if settings.vigi_ipc_username is None:
        raise MissingSourceSettingError(_VIGI_IPC_USERNAME, _IPC_SOURCE)
    if settings.vigi_ipc_password is None:
        raise MissingSourceSettingError(_secret_key(_VIGI_IPC_HOST), _IPC_SOURCE)


def _secret_key(host_key: str) -> str:
    return f"{host_key[:-4]}PASSWORD"
