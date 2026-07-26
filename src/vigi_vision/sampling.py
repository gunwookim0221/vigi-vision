"""Pure request parsing and coverage-aware frame-sampling planning."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final, final

from typing_extensions import override

_START_FORMAT: Final = "%Y-%m-%d %H:%M:%S"
_DURATION_PATTERN: Final = re.compile(r"([1-9][0-9]*)([smh])")
_SOURCE_TIMEZONES: Final = {
    "UTC": timezone.utc,
    "Asia/Seoul": timezone(timedelta(hours=9), "KST"),
}


@final
class SamplingInputError(ValueError):
    """Raised when a sampling CLI value cannot form a safe request."""

    @override
    def __str__(self) -> str:
        return "Sampling inputs must use valid whole-second times and durations."


@dataclass(frozen=True, slots=True)
class RawSamplingInput:
    """Unparsed CLI values accepted by the sampling boundary."""

    channel_id: int
    start_text: str
    source_timezone: str
    duration_text: str
    interval_text: str
    chunk_duration_text: str


@dataclass(frozen=True, slots=True)
class SamplingRequest:
    """Canonical, credential-free sampling request after CLI parsing."""

    channel_id: int
    start_text: str
    source_timezone: str
    start_utc: datetime
    end_utc: datetime
    interval_seconds: int
    chunk_duration_seconds: int

    def __post_init__(self) -> None:
        """Reject non-UTC, non-positive, or incompatible canonical values."""
        if (
            self.channel_id <= 0
            or self.start_utc.tzinfo is None
            or self.end_utc.tzinfo is None
            or self.start_utc.utcoffset() != timedelta(0)
            or self.end_utc.utcoffset() != timedelta(0)
            or self.start_utc.microsecond != 0
            or self.end_utc.microsecond != 0
            or self.end_utc <= self.start_utc
            or self.interval_seconds <= 0
            or self.chunk_duration_seconds < self.interval_seconds
        ):
            raise SamplingInputError


@dataclass(frozen=True, slots=True)
class RecordingCoverage:
    """One known contiguous UTC range returned by the public SDK."""

    start_utc: datetime
    end_utc: datetime

    def __post_init__(self) -> None:
        """Require a nonempty timezone-aware coverage interval."""
        if (
            self.start_utc.tzinfo is None
            or self.end_utc.tzinfo is None
            or self.end_utc <= self.start_utc
        ):
            raise SamplingInputError


@dataclass(frozen=True, slots=True)
class SamplePoint:
    """One requested source timestamp assigned to coverage or a gap."""

    timestamp_utc: datetime


@dataclass(frozen=True, slots=True)
class SamplingChunk:
    """One bounded replay interval and the schedule points it owns."""

    start_utc: datetime
    end_utc: datetime
    source_coverage: RecordingCoverage
    points: tuple[SamplePoint, ...]


@dataclass(frozen=True, slots=True)
class SamplingPlan:
    """Stable interval schedule partitioned across known recording coverage."""

    request: SamplingRequest
    chunks: tuple[SamplingChunk, ...]
    written_points: tuple[SamplePoint, ...]
    skipped_points: tuple[SamplePoint, ...]
    coverage: tuple[RecordingCoverage, ...]


def parse_sampling_request(values: RawSamplingInput) -> SamplingRequest:
    """Parse CLI values into an unambiguous whole-second UTC request."""
    local_start = _parse_local_time(values.start_text, values.source_timezone)
    duration_seconds = _parse_duration(values.duration_text)
    return SamplingRequest(
        values.channel_id,
        values.start_text,
        values.source_timezone,
        local_start.astimezone(timezone.utc),
        local_start.astimezone(timezone.utc) + timedelta(seconds=duration_seconds),
        _parse_duration(values.interval_text),
        _parse_duration(values.chunk_duration_text),
    )


def build_sampling_plan(
    request: SamplingRequest, coverage: tuple[RecordingCoverage, ...]
) -> SamplingPlan:
    """Keep a source-anchored schedule while assigning only covered timestamps."""
    normalized_coverage = tuple(sorted((_clip(item, request) for item in coverage), key=_start))
    schedule = tuple(
        SamplePoint(timestamp)
        for timestamp in _timestamps(request.start_utc, request.end_utc, request.interval_seconds)
    )
    covered: list[SamplePoint] = []
    assigned: set[datetime] = set()
    chunks: list[SamplingChunk] = []
    for item in normalized_coverage:
        points = tuple(
            point
            for point in schedule
            if _contains(item, point) and point.timestamp_utc not in assigned
        )
        assigned.update(point.timestamp_utc for point in points)
        if points:
            covered.extend(points)
            chunks.extend(_chunks_for(item, points, request.chunk_duration_seconds))
    skipped = [point for point in schedule if point.timestamp_utc not in assigned]
    return SamplingPlan(request, tuple(chunks), tuple(covered), tuple(skipped), normalized_coverage)


def _parse_local_time(value: str, timezone_name: str) -> datetime:
    try:
        zone = _source_timezone(timezone_name)
        naive = datetime.strptime(value, _START_FORMAT).replace(tzinfo=zone)
    except ValueError as error:
        raise SamplingInputError from error
    return naive


def _source_timezone(value: str) -> timezone:
    try:
        return _SOURCE_TIMEZONES[value]
    except KeyError as error:
        raise SamplingInputError from error


def _parse_duration(value: str) -> int:
    match = _DURATION_PATTERN.fullmatch(value)
    if match is None:
        raise SamplingInputError
    amount = int(match.group(1))
    unit = match.group(2)
    multiplier = {"s": 1, "m": 60, "h": 3_600}[unit]
    return amount * multiplier


def _timestamps(start: datetime, end: datetime, interval_seconds: int) -> tuple[datetime, ...]:
    count = int((end - start).total_seconds())
    return tuple(start + timedelta(seconds=offset) for offset in range(0, count, interval_seconds))


def _clip(item: RecordingCoverage, request: SamplingRequest) -> RecordingCoverage:
    return RecordingCoverage(
        max(item.start_utc, request.start_utc), min(item.end_utc, request.end_utc)
    )


def _start(item: RecordingCoverage) -> datetime:
    return item.start_utc


def _contains(item: RecordingCoverage, point: SamplePoint) -> bool:
    return item.start_utc <= point.timestamp_utc < item.end_utc


def _chunks_for(
    coverage: RecordingCoverage, points: tuple[SamplePoint, ...], duration_seconds: int
) -> tuple[SamplingChunk, ...]:
    chunks: list[SamplingChunk] = []
    start = coverage.start_utc
    while start < coverage.end_utc:
        end = min(start + timedelta(seconds=duration_seconds), coverage.end_utc)
        owned = tuple(point for point in points if start <= point.timestamp_utc < end)
        if owned:
            chunks.append(SamplingChunk(start, end, coverage, owned))
        start = end
    return tuple(chunks)
