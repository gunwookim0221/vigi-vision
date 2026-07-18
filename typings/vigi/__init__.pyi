from dataclasses import dataclass
from enum import Enum

class VigiError(Exception): ...
class VigiConnectionError(VigiError): ...
class ConnectionError(VigiConnectionError): ...  # noqa: A001
class VigiTimeoutError(VigiConnectionError): ...
class TimeoutError(VigiTimeoutError): ...  # noqa: A001
class VigiAuthenticationError(VigiError): ...
class AuthenticationError(VigiAuthenticationError): ...
class VigiTransportError(VigiError): ...
class TransportError(VigiTransportError): ...

class ChannelStatus(str, Enum):
    OFFLINE: ChannelStatus
    ONLINE: ChannelStatus
    UNKNOWN: ChannelStatus

class StreamType(str, Enum):
    MAIN: StreamType
    MINOR: StreamType

@dataclass(frozen=True, slots=True)
class AuthConfig:
    host: str
    username: str
    password: str
    port: int = ...
    verify_tls: bool = ...

@dataclass(frozen=True, slots=True)
class AddedDevice:
    channel_id: int
    name: str
    alias: str
    online: ChannelStatus

@dataclass(frozen=True, slots=True)
class AddedDevicesResponse:
    devices: tuple[AddedDevice, ...]

class DeviceService:
    def list_added_devices(self) -> AddedDevicesResponse: ...

class StreamService:
    def build_live_url(self, host: str, channel_id: int, stream: StreamType = ...) -> str: ...
    def build_ipc_live_url(self, host: str, stream: StreamType = ...) -> str: ...

class VigiClient:
    devices: DeviceService
    stream: StreamService
    def __init__(self, auth_config: AuthConfig) -> None: ...
    def login(self) -> None: ...
