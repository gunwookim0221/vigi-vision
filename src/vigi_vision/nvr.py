"""Public TP-Link VIGI SDK adapter."""

from builtins import TimeoutError as BuiltinTimeoutError
from dataclasses import dataclass
from enum import Enum
from socket import gaierror
from ssl import SSLError
from typing import Literal, final
from urllib.error import URLError

from typing_extensions import override
from vigi import (
    AuthConfig,
    AuthenticationError,
    ChannelStatus,
    StreamType,
    VigiClient,
    VigiError,
)
from vigi import (
    ConnectionError as SdkConnectionError,
)
from vigi import (
    TimeoutError as SdkTimeoutError,
)

from vigi_vision.channel_selection import Channel, select_channel
from vigi_vision.config import NvrConnection
from vigi_vision.workflow import LiveStream


class NvrErrorKind(Enum):
    """Safe diagnostic categories for NVR requests in development builds."""

    AUTHENTICATION = "Authentication failure"
    TLS_VERIFICATION = "TLS verification failure"
    TIMEOUT = "Timeout"
    CONNECTION_REFUSED = "Connection refused"
    HOST_RESOLUTION = "Host resolution failure"
    SDK_REQUEST = "SDK request failure"
    UNEXPECTED = "Unexpected exception"


@final
@dataclass(frozen=True, slots=True)
class NvrRequestError(RuntimeError):
    """Raised with a redacted, classified NVR request failure."""

    kind: NvrErrorKind
    exception_type: str

    @override
    def __str__(self) -> str:
        """Return a concise diagnostic that omits exception text and secrets."""
        match self.kind:
            case NvrErrorKind.AUTHENTICATION:
                detail = "Verify the NVR credentials."
            case NvrErrorKind.TLS_VERIFICATION:
                detail = "Verify the NVR certificate or VIGI_VERIFY_SSL."
            case NvrErrorKind.TIMEOUT:
                detail = "The NVR did not respond before the request timeout."
            case NvrErrorKind.CONNECTION_REFUSED:
                detail = "Verify that the NVR service is reachable."
            case NvrErrorKind.HOST_RESOLUTION:
                detail = "Verify VIGI_HOST DNS or host configuration."
            case NvrErrorKind.SDK_REQUEST:
                detail = "The public SDK request failed."
            case NvrErrorKind.UNEXPECTED:
                detail = "An unexpected development failure occurred."
        return f"{self.kind.value} [{self.exception_type}]. {detail}"


@dataclass(frozen=True, slots=True)
class SdkNvrGateway:
    """Use documented SDK authentication, inventory, and live URL APIs only."""

    connection: NvrConnection

    def channels(self) -> tuple[Channel, ...]:
        """Authenticate and return non-secret inventory metadata."""
        client = self._client()
        try:
            client.login()
            devices = client.devices.list_added_devices()
        except VigiError as error:
            raise diagnose_nvr_error(error) from error
        except Exception as error:
            raise diagnose_nvr_error(error) from error
        return tuple(
            Channel(
                channel_id=device.channel_id,
                name=device.name,
                alias=device.alias,
                online=device.online is ChannelStatus.ONLINE,
            )
            for device in devices.devices
        )

    def live_url(self, channel_id: int, stream: Literal["main", "minor"]) -> str:
        """Build a public SDK live URL without credentials or network access."""
        try:
            return self._client().stream.build_live_url(
                host=self.connection.host,
                channel_id=channel_id,
                stream=_stream_type(stream),
            )
        except VigiError as error:
            raise diagnose_nvr_error(error) from error

    def stream(self) -> LiveStream:
        """Select one NVR channel and return its public RTSP stream."""
        channels = self.channels()
        channel = select_channel(channels, self.connection.channel_id)
        return LiveStream(
            label=f"NVR channel {channel.channel_id}",
            channel=channel,
            live_url=self.live_url(channel.channel_id, self.connection.stream),
            username=self.connection.username.get_secret_value(),
            password=self.connection.password.get_secret_value(),
            artifact_stem=f"channel-{channel.channel_id}",
        )

    def _client(self) -> VigiClient:
        return VigiClient(
            AuthConfig(
                host=self.connection.host,
                port=self.connection.port,
                username=self.connection.username.get_secret_value(),
                password=self.connection.password.get_secret_value(),
                verify_tls=self.connection.verify_ssl,
            )
        )


def _stream_type(stream: Literal["main", "minor"]) -> StreamType:
    match stream:
        case "main":
            return StreamType.MAIN
        case "minor":
            return StreamType.MINOR


def diagnose_nvr_error(error: BaseException) -> NvrRequestError:
    """Classify an SDK error without using its potentially sensitive text."""
    match error:
        case AuthenticationError():
            return NvrRequestError(NvrErrorKind.AUTHENTICATION, type(error).__name__)
        case SdkTimeoutError() | BuiltinTimeoutError():
            return NvrRequestError(NvrErrorKind.TIMEOUT, type(error).__name__)
        case SdkConnectionError() as connection_error:
            return _diagnose_connection_error(connection_error)
        case VigiError():
            return NvrRequestError(NvrErrorKind.SDK_REQUEST, type(error).__name__)
        case unexpected_error:
            return NvrRequestError(NvrErrorKind.UNEXPECTED, type(unexpected_error).__name__)


def _diagnose_connection_error(error: SdkConnectionError) -> NvrRequestError:
    kind: NvrErrorKind
    cause: BaseException
    match error.__cause__:
        case URLError(reason=SSLError() as cause):
            kind = NvrErrorKind.TLS_VERIFICATION
        case SSLError() as cause:
            kind = NvrErrorKind.TLS_VERIFICATION
        case URLError(reason=BuiltinTimeoutError() as cause):
            kind = NvrErrorKind.TIMEOUT
        case BuiltinTimeoutError() as cause:
            kind = NvrErrorKind.TIMEOUT
        case URLError(reason=ConnectionRefusedError() as cause):
            kind = NvrErrorKind.CONNECTION_REFUSED
        case ConnectionRefusedError() as cause:
            kind = NvrErrorKind.CONNECTION_REFUSED
        case URLError(reason=gaierror() as cause):
            kind = NvrErrorKind.HOST_RESOLUTION
        case gaierror() as cause:
            kind = NvrErrorKind.HOST_RESOLUTION
        case _:
            kind = NvrErrorKind.SDK_REQUEST
            cause = error
    return NvrRequestError(kind, type(cause).__name__)
