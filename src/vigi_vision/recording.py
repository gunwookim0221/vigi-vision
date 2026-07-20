"""Recording search, overlap planning, and credential-free replay requests."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Protocol, final

from typing_extensions import override
from vigi import (
    AuthConfig,
    RecordDaysResponse,
    RecordSearchProcessResponse,
    RecordSearchResultsResponse,
    VigiClient,
    VigiError,
)
from vigi import (
    RecordSegment as SdkRecordSegment,
)

from vigi_vision.config import NvrConnection
from vigi_vision.nvr import diagnose_nvr_error

_RECORDING_TIMEZONE = timezone(timedelta(hours=9), "KST")
_REPLAY_TIME_FORMAT = "%Y%m%dt%H%M%Sz"
_RESULT_PAGE_SIZE = 100


class RecordingApi(Protocol):
    """Public SDK recording operations required for replay planning."""

    def list_days(self, channel_id: int, start_month: str, end_month: str) -> RecordDaysResponse:
        """List NVR-local recording days within an inclusive month range."""
        ...

    def get_free_process(self) -> RecordSearchProcessResponse:
        """Reserve an SDK recording-search process."""
        ...

    def list_results(
        self,
        channel_id: int,
        process_id: int,
        day: str,
        start_index: int = 0,
        end_index: int = 99,
    ) -> RecordSearchResultsResponse:
        """List one indexed page of recording segments for an NVR-local day."""
        ...


class ReplayUrlApi(Protocol):
    """Public SDK replay URL operation required for replay planning."""

    def build_replay_url(
        self, host: str, channel_id: int, start_time: str, end_time: str, stream: int = 1
    ) -> str:
        """Build a credential-free UTC replay URL."""
        ...


class RecordingClient(Protocol):
    """Authenticated public SDK surface consumed by ``RecordingPlanner``."""

    @property
    def records(self) -> RecordingApi:
        """Return the public SDK recording capability."""
        ...

    @property
    def stream(self) -> ReplayUrlApi:
        """Return the public SDK replay-URL capability."""
        ...


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
        except ValueError as error:
            raise RecordingDataError from error
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
    replay_url: str


@dataclass(frozen=True, slots=True)
class RecordingPlanner:
    """Use public SDK recording APIs to plan a replay interval without ffmpeg."""

    client: RecordingClient
    host: str
    recording_timezone: tzinfo = _RECORDING_TIMEZONE

    @classmethod
    def connect(cls, connection: NvrConnection) -> "RecordingPlanner":
        """Authenticate a public SDK client for replay planning."""
        client = VigiClient(
            AuthConfig(
                host=connection.host,
                port=connection.port,
                username=connection.username.get_secret_value(),
                password=connection.password.get_secret_value(),
                verify_tls=connection.verify_ssl,
            )
        )
        try:
            client.login()
        except VigiError as error:
            raise diagnose_nvr_error(error) from error
        return cls(client, connection.host)

    def plan(self, window: RecordingWindow) -> ReplayRequest:
        """Build a replay request when an NVR segment overlaps ``window``."""
        try:
            matching_days = self._matching_days(window)
            process_id = self.client.records.get_free_process().process_id
            for recording_day in matching_days:
                for segment in self._segments(window.channel_id, process_id, recording_day):
                    if _overlaps(window, segment):
                        return ReplayRequest(
                            window=window,
                            replay_url=self.client.stream.build_replay_url(
                                self.host,
                                window.channel_id,
                                window.start_utc.strftime(_REPLAY_TIME_FORMAT),
                                window.end_utc.strftime(_REPLAY_TIME_FORMAT),
                            ),
                        )
        except VigiError as error:
            raise diagnose_nvr_error(error) from error
        raise RecordingUnavailableError

    def _matching_days(self, window: RecordingWindow) -> tuple[date, ...]:
        local_start = window.start_utc.astimezone(self.recording_timezone).date()
        local_end = window.end_utc.astimezone(self.recording_timezone).date()
        response = self.client.records.list_days(
            window.channel_id,
            local_start.strftime("%Y%m"),
            local_end.strftime("%Y%m"),
        )
        available_days = tuple(_parse_recording_day(record.day) for record in response.days)
        return tuple(day for day in available_days if local_start <= day <= local_end)

    def _segments(
        self, channel_id: int, process_id: int, recording_day: date
    ) -> tuple[RecordingSegment, ...]:
        segments: list[RecordingSegment] = []
        start_index = 0
        while True:
            response = self.client.records.list_results(
                channel_id,
                process_id,
                recording_day.strftime("%Y%m%d"),
                start_index,
                start_index + _RESULT_PAGE_SIZE - 1,
            )
            segments.extend(
                RecordingSegment.from_sdk(channel_id, recording_day, segment)
                for segment in response.results
            )
            if len(response.results) < _RESULT_PAGE_SIZE:
                return tuple(segments)
            start_index += _RESULT_PAGE_SIZE


def _parse_recording_day(value: str) -> date:
    try:
        return date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:]}")
    except ValueError as error:
        raise RecordingDataError from error


def _overlaps(window: RecordingWindow, segment: RecordingSegment) -> bool:
    return segment.start_utc < window.end_utc and window.start_utc < segment.end_utc
