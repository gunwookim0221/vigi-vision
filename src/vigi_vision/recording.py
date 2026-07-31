"""Recording search, overlap planning, and credential-free replay requests."""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Protocol

from vigi import (
    AuthConfig,
    RecordDaysResponse,
    RecordSearchProcessResponse,
    RecordSearchResultsResponse,
    VigiClient,
    VigiError,
)

from vigi_vision.config import NvrConnection
from vigi_vision.nvr import diagnose_nvr_error
from vigi_vision.recording_models import (
    RecordingDataError,
    RecordingSegment,
    RecordingUnavailableError,
    RecordingWindow,
    RecordingWindowError,
    ReplayRequest,
)

__all__ = (
    "RecordingDataError",
    "RecordingPlanner",
    "RecordingSegment",
    "RecordingUnavailableError",
    "RecordingWindow",
    "RecordingWindowError",
    "ReplayRequest",
)

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


class _RecordingSearchProcess:
    """Mutable process state held outside the planner's value identity."""

    __slots__: tuple[str, ...] = ("process_id",)

    def __init__(self) -> None:
        self.process_id: int | None = None


@dataclass(frozen=True, slots=True)
class RecordingPlanner:
    """Use public SDK recording APIs to plan a replay interval without ffmpeg."""

    client: RecordingClient = field(repr=False)
    host: str = field(repr=False)
    recording_timezone: tzinfo = _RECORDING_TIMEZONE
    _search_process: _RecordingSearchProcess = field(
        default_factory=_RecordingSearchProcess,
        init=False,
        repr=False,
        compare=False,
    )

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
            raise diagnose_nvr_error(error) from None
        return cls(client, connection.host)

    def plan(self, window: RecordingWindow) -> ReplayRequest:
        """Build a replay request when an NVR segment overlaps ``window``."""
        try:
            matching_days = self._matching_days(window)
            process_id = self._process_id()
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
            raise diagnose_nvr_error(error) from None
        raise RecordingUnavailableError

    def find_covering_segment(self, channel_id: int, instant_utc: datetime) -> RecordingSegment:
        """Return the deterministic segment covering one whole UTC second."""
        window = RecordingWindow(channel_id, instant_utc, instant_utc + timedelta(seconds=1))
        try:
            process_id = self._process_id()
            candidates = tuple(
                segment
                for recording_day in self._matching_days(window)
                for segment in self._segments(channel_id, process_id, recording_day)
                if segment.start_utc <= instant_utc < segment.end_utc
            )
        except VigiError as error:
            raise diagnose_nvr_error(error) from None
        if not candidates:
            raise RecordingUnavailableError
        return min(candidates, key=lambda segment: (segment.start_utc, segment.end_utc))

    def plan_for_segment(self, segment: RecordingSegment, window: RecordingWindow) -> ReplayRequest:
        """Build a replay request only when its whole window stays in ``segment``."""
        if (
            segment.channel_id != window.channel_id
            or window.start_utc < segment.start_utc
            or window.end_utc > segment.end_utc
        ):
            raise RecordingUnavailableError
        try:
            replay_url = self.client.stream.build_replay_url(
                self.host,
                window.channel_id,
                window.start_utc.strftime(_REPLAY_TIME_FORMAT),
                window.end_utc.strftime(_REPLAY_TIME_FORMAT),
            )
        except VigiError as error:
            raise diagnose_nvr_error(error) from None
        return ReplayRequest(window, replay_url)

    def _process_id(self) -> int:
        process_id = self._search_process.process_id
        if process_id is None:
            process_id = self.client.records.get_free_process().process_id
            self._search_process.process_id = process_id
        return process_id

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
    except ValueError:
        raise RecordingDataError from None


def _overlaps(window: RecordingWindow, segment: RecordingSegment) -> bool:
    return segment.start_utc < window.end_utc and window.start_utc < segment.end_utc
