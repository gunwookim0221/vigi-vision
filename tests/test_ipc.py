from secrets import token_urlsafe

from vigi_vision.config import Settings
from vigi_vision.gateway import select_source_gateway
from vigi_vision.ipc import SdkIpcGateway

_TEST_OPENAI_KEY = token_urlsafe()
_TEST_PASSWORD = token_urlsafe()


def _ipc_settings() -> Settings:
    return Settings.model_validate(
        {
            "OPENAI_API_KEY": _TEST_OPENAI_KEY,
            "VIGI_SOURCE": "ipc",
            "VIGI_IPC_HOST": "ipc.example.invalid",
            "VIGI_IPC_USERNAME": "operator",
            "VIGI_IPC_PASSWORD": _TEST_PASSWORD,
            "VIGI_STREAM": "minor",
        }
    )


def test_ipc_gateway_uses_public_sdk_builder_with_separate_credentials() -> None:
    # Given
    settings = _ipc_settings()
    gateway = SdkIpcGateway(settings.ipc_connection)

    # When
    stream = gateway.stream()

    # Then
    assert stream.live_url == "rtsp://ipc.example.invalid/stream2"
    assert stream.username == "operator"
    assert stream.password == _TEST_PASSWORD
    assert _TEST_PASSWORD not in stream.live_url


def test_source_selection_returns_ipc_gateway() -> None:
    # Given
    settings = _ipc_settings()

    # When
    gateway = select_source_gateway(settings)

    # Then
    assert isinstance(gateway, SdkIpcGateway)
