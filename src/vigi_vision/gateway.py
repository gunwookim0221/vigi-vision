"""Configured source-gateway selection."""

from vigi_vision.config import Settings
from vigi_vision.ipc import SdkIpcGateway
from vigi_vision.nvr import SdkNvrGateway
from vigi_vision.workflow import SourceGateway


def select_source_gateway(settings: Settings) -> SourceGateway:
    """Return the one configured source adapter for an inspection."""
    match settings.vigi_source:
        case "nvr":
            return SdkNvrGateway(settings.nvr_connection)
        case "ipc":
            return SdkIpcGateway(settings.ipc_connection)
