"""Public-SDK standalone IPC RTSP adapter."""

from dataclasses import dataclass
from typing import Literal

from vigi import StreamService, StreamType, VigiError

from vigi_vision.config import IpcConnection
from vigi_vision.nvr import diagnose_nvr_error
from vigi_vision.workflow import LiveStream


@dataclass(frozen=True, slots=True)
class SdkIpcGateway:
    """Build one standard IPC RTSP URL without IPC OpenAPI authentication."""

    connection: IpcConnection

    def stream(self) -> LiveStream:
        """Return the configured IPC RTSP stream and separate Digest credentials."""
        try:
            live_url = StreamService().build_ipc_live_url(
                self.connection.host,
                _stream_type(self.connection.stream),
            )
        except VigiError as error:
            raise diagnose_nvr_error(error) from error
        return LiveStream(
            label="Standalone IPC",
            channel=None,
            live_url=live_url,
            username=self.connection.username,
            password=self.connection.password.get_secret_value(),
            artifact_stem="ipc",
        )


def _stream_type(stream: Literal["main", "minor"]) -> StreamType:
    match stream:
        case "main":
            return StreamType.MAIN
        case "minor":
            return StreamType.MINOR
