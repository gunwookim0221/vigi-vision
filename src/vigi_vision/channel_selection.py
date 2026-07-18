"""Safe camera-channel selection."""

from dataclasses import dataclass
from enum import Enum
from typing import final

from typing_extensions import override


class ChannelSelectionReason(Enum):
    """Public failure reasons emitted when channel selection is unsafe."""

    CONFIGURED = "configured"
    NONE = "none"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class Channel:
    """Non-secret NVR channel metadata used by the inspection workflow."""

    channel_id: int
    name: str
    alias: str
    online: bool


@final
@dataclass(frozen=True, slots=True)
class ChannelSelectionError(ValueError):
    """Raised when safe automatic channel selection is impossible."""

    reason: ChannelSelectionReason
    channels: tuple[Channel, ...]

    @override
    def __str__(self) -> str:
        """Return the safe user-facing selection failure."""
        match self.reason:
            case ChannelSelectionReason.CONFIGURED:
                return "The configured VIGI_CHANNEL_ID is missing or offline."
            case ChannelSelectionReason.NONE:
                return "No online NVR channels are available."
            case ChannelSelectionReason.AMBIGUOUS:
                return (
                    "Multiple online NVR channels are available; set VIGI_CHANNEL_ID to choose one."
                )


def select_channel(channels: tuple[Channel, ...], configured_channel_id: int | None) -> Channel:
    """Return the configured or sole online channel without arbitrary selection."""
    if configured_channel_id is not None:
        for channel in channels:
            if channel.channel_id == configured_channel_id and channel.online:
                return channel
        raise ChannelSelectionError(ChannelSelectionReason.CONFIGURED, channels)

    online_channels = tuple(channel for channel in channels if channel.online)
    if not online_channels:
        raise ChannelSelectionError(ChannelSelectionReason.NONE, channels)
    if len(online_channels) == 1:
        return online_channels[0]
    raise ChannelSelectionError(ChannelSelectionReason.AMBIGUOUS, channels)


def format_channels(channels: tuple[Channel, ...]) -> str:
    """Format safe channel metadata without network identifiers or secrets."""
    return "\n".join(_format_channel(channel) for channel in channels)


def _format_channel(channel: Channel) -> str:
    online = "yes" if channel.online else "no"
    return f"channel={channel.channel_id} name={channel.name} alias={channel.alias} online={online}"
