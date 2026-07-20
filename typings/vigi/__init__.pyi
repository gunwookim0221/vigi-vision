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

@dataclass(frozen=True, slots=True)
class RecordDay:
    day: str

@dataclass(frozen=True, slots=True)
class RecordDaysResponse:
    days: tuple[RecordDay, ...]
    error_code: int

@dataclass(frozen=True, slots=True)
class RecordSearchProcessResponse:
    process_id: int
    error_code: int

@dataclass(frozen=True, slots=True)
class RecordSegment:
    start_time: str
    end_time: str

@dataclass(frozen=True, slots=True)
class RecordSearchResultsResponse:
    results: tuple[RecordSegment, ...]
    error_code: int

class DeviceService:
    def list_added_devices(self) -> AddedDevicesResponse: ...

class RecordService:
    def list_days(
        self, channel_id: int, start_month: str, end_month: str
    ) -> RecordDaysResponse: ...
    def get_free_process(self) -> RecordSearchProcessResponse: ...
    def list_results(
        self,
        channel_id: int,
        process_id: int,
        day: str,
        start_index: int = ...,
        end_index: int = ...,
    ) -> RecordSearchResultsResponse: ...

class StreamService:
    def build_live_url(self, host: str, channel_id: int, stream: StreamType = ...) -> str: ...
    def build_ipc_live_url(self, host: str, stream: StreamType = ...) -> str: ...
    def build_replay_url(
        self,
        host: str,
        channel_id: int,
        start_time: str,
        end_time: str,
        stream: int = ...,
    ) -> str: ...

class VigiClient:
    devices: DeviceService
    records: RecordService
    stream: StreamService
    def __init__(self, auth_config: AuthConfig) -> None: ...
    def login(self) -> None: ...
