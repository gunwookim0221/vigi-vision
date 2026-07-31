"""Recording value objects shared by retrieval and reference-frame boundaries."""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import final

from typing_extensions import override
from vigi import RecordSegment as SdkRecordSegment


@final
@dataclass(frozen=True, slots=True)
class RecordingWindowError(ValueError):
    """Raised when a replay window cannot be represented by the NVR contract."""

    @override
    def __str__(self) -> str:
        return "Recording windows must use whole UTC seconds and have a positive duration."


@final
@dataclass(frozen=True, slots=True)
class RecordingDataError(RuntimeError):
    """Raised when the SDK response cannot be converted into a recording segment."""

    @override
    def __str__(self) -> str:
        return "The NVR returned recording metadata that could not be interpreted."


@final
@dataclass(frozen=True, slots=True)
class RecordingUnavailableError(RuntimeError):
    """Raised when no NVR recording overlaps the requested UTC window."""

    @override
    def __str__(self) -> str:
        return "No recording is available for the requested time window."


@dataclass(frozen=True, slots=True)
class RecordingWindow:
    """A requested whole-second UTC interval for one NVR channel."""

    channel_id: int
    start_utc: datetime
    end_utc: datetime

    def __post_init__(self) -> None:
        """Reject intervals that cannot be expressed by the whole-second RTSP API."""
        if (
            self.channel_id <= 0
            or self.start_utc.tzinfo is None
            or self.end_utc.tzinfo is None
            or self.start_utc.utcoffset() != timedelta(0)
            or self.end_utc.utcoffset() != timedelta(0)
            or self.start_utc.microsecond != 0
            or self.end_utc.microsecond != 0
            or self.end_utc <= self.start_utc
        ):
            raise RecordingWindowError

    @property
    def duration(self) -> timedelta:
        """Return the requested UTC interval."""
        return self.end_utc - self.start_utc

    @property
    def duration_seconds(self) -> int:
        """Return the exact client-side ffmpeg duration limit."""
        return int(self.duration.total_seconds())


@dataclass(frozen=True, slots=True)
class RecordingSegment:
    """One NVR recording segment with raw epoch seconds and UTC instants."""

    channel_id: int
    recording_day: date
    start_epoch_seconds: int
    end_epoch_seconds: int
    start_utc: datetime
    end_utc: datetime

    @property
    def duration_seconds(self) -> int:
        """Return the segment duration in whole seconds."""
        return self.end_epoch_seconds - self.start_epoch_seconds

    @classmethod
    def from_sdk(
        cls, channel_id: int, recording_day: date, segment: SdkRecordSegment
    ) -> "RecordingSegment":
        """Convert public SDK epoch strings into UTC recording facts."""
        try:
            start_epoch_seconds = int(segment.start_time)
            end_epoch_seconds = int(segment.end_time)
        except ValueError:
            raise RecordingDataError from None
        if end_epoch_seconds <= start_epoch_seconds:
            raise RecordingDataError
        return cls(
            channel_id=channel_id,
            recording_day=recording_day,
            start_epoch_seconds=start_epoch_seconds,
            end_epoch_seconds=end_epoch_seconds,
            start_utc=datetime.fromtimestamp(start_epoch_seconds, timezone.utc),
            end_utc=datetime.fromtimestamp(end_epoch_seconds, timezone.utc),
        )


@dataclass(frozen=True, slots=True)
class ReplayRequest:
    """A credential-free NVR replay request ready for ffmpeg extraction."""

    window: RecordingWindow
    replay_url: str = field(repr=False)
