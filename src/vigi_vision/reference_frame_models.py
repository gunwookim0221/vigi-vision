"""Typed inputs, outputs, and safe errors for recorded reference frames."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Final, TypeGuard, final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from typing_extensions import override

from vigi_vision.recording import RecordingSegment, RecordingWindow

MANIFEST_SCHEMA_VERSION: Final = 1
GENERATION_POLICY_VERSION: Final = 1
REPLAY_DURATION: Final = timedelta(seconds=6)
_KST: Final = timezone(timedelta(hours=9), "KST")
_INVALID_CHANNEL: Final = "invalid_channel"
_INVALID_TIME_OR_TIMEZONE: Final = "invalid_time_or_timezone"
_FRACTIONAL_SECONDS: Final = "fractional_seconds"
_FUTURE_TIME: Final = "future_time"
_AMBIGUOUS_OR_NONEXISTENT: Final = "ambiguous_or_nonexistent_local_time"
_INVALID_GENERATION_POLICY: Final = "invalid_generation_policy"


@final
class FrameSelectionPolicy(str, Enum):
    """The supported deterministic decoded-frame selection policy."""

    NEAREST_DECODED_FRAME = "nearest_decoded_frame"


@final
class TimingPrecisionStatus(str, Enum):
    """Truthful level of available timing evidence."""

    MEASURED_CLIP_RELATIVE = "measured_clip_relative"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"
    INDETERMINATE = "indeterminate"


@final
class ReferenceFrameOutcome(str, Enum):
    """The durable-resource outcome of one reference-frame execution."""

    CREATED = "created"
    REUSED = "reused"


class ReferenceFrameError(RuntimeError):
    """Base class for safe reference-frame domain failures."""


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameInputError(ReferenceFrameError):
    """Raised when a reference-frame request cannot be normalized safely."""

    code: str

    @override
    def __str__(self) -> str:
        return "The reference-frame request is invalid."


@final
@dataclass(frozen=True, slots=True)
class UnsupportedReferenceFrameSourceError(ReferenceFrameError):
    """Raised before media work for unsupported non-NVR sources."""

    @override
    def __str__(self) -> str:
        return "Reference-frame extraction is available only for NVR recordings."


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameChannelNotFoundError(ReferenceFrameError):
    """Raised when an injected NVR inventory proves the requested channel is absent."""

    @override
    def __str__(self) -> str:
        return "The requested NVR channel was not found."


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameSegmentMismatchError(ReferenceFrameError):
    """Raised when a replay plan cannot be proven to stay in the selected segment."""

    @override
    def __str__(self) -> str:
        return "The selected recording segment could not be matched to a safe replay plan."


@dataclass(frozen=True, slots=True)
class ReferenceFrameDecodeError(ReferenceFrameError):
    """Raised for a safe local replay-frame decoding failure."""

    @override
    def __str__(self) -> str:
        return "The replay clip could not be decoded into a reference frame."


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameNoCandidateError(ReferenceFrameError):
    """Raised when no decoded frame can satisfy the requested policy."""

    @override
    def __str__(self) -> str:
        return "No acceptable decoded frame is available for the requested recording time."


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameArtifactConflictError(ReferenceFrameError):
    """Raised instead of replacing an existing reference-frame resource."""

    @override
    def __str__(self) -> str:
        return "A reference-frame artifact already exists for this request."


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameArtifactError(ReferenceFrameError):
    """Raised when a durable reference-frame package cannot be published safely."""

    @override
    def __str__(self) -> str:
        return "Reference-frame artifacts could not be created safely."


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameResourceNotFoundError(ReferenceFrameError):
    """Raised when a completed resource cannot be found safely."""

    @override
    def __str__(self) -> str:
        return "The requested reference-frame resource was not found."


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameResourceIncompatibleError(ReferenceFrameError):
    """Raised when a durable resource cannot satisfy the current request semantics."""

    @override
    def __str__(self) -> str:
        return "The existing reference-frame resource is not compatible with this request."


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameResourceCorruptError(ReferenceFrameError):
    """Raised when a durable resource is incomplete or fails integrity checks."""

    @override
    def __str__(self) -> str:
        return "The stored reference-frame resource could not be read safely."


@final
@dataclass(frozen=True, slots=True)
class ReferenceFrameCleanupError(ReferenceFrameError):
    """Raised when invocation-owned temporary replay media cannot be removed."""

    @override
    def __str__(self) -> str:
        return "Temporary reference-frame media could not be removed safely."


@dataclass(frozen=True, slots=True)
class ReferenceFrameRequest:
    """Validated NVR-only request with a whole-second UTC planning instant."""

    channel_id: int
    requested_time_text: str
    source_timezone: str
    requested_time_utc: datetime
    frame_selection_policy: FrameSelectionPolicy = FrameSelectionPolicy.NEAREST_DECODED_FRAME
    generation_policy_version: int = GENERATION_POLICY_VERSION
    source_kind: str = "nvr"

    def __post_init__(self) -> None:
        """Enforce the invariants required by downstream path and time planning."""
        parsed = _parse_requested_time(self.requested_time_text)
        if parsed.microsecond != 0:
            raise ReferenceFrameInputError(_FRACTIONAL_SECONDS)
        parsed, normalized_source_timezone = _normalize_source_time(parsed, self.source_timezone)
        if (
            type(self.channel_id) is not int
            or self.channel_id <= 0
            or self.requested_time_utc.tzinfo is None
            or self.requested_time_utc.utcoffset() != timedelta(0)
            or self.requested_time_utc.microsecond != 0
            or parsed.astimezone(timezone.utc) != self.requested_time_utc
            or normalized_source_timezone != self.source_timezone
            or not _is_frame_selection_policy(self.frame_selection_policy)
            or self.source_kind != "nvr"
        ):
            raise ReferenceFrameInputError(_INVALID_TIME_OR_TIMEZONE)
        if type(self.generation_policy_version) is not int or self.generation_policy_version <= 0:
            raise ReferenceFrameInputError(_INVALID_GENERATION_POLICY)


@dataclass(frozen=True, slots=True)
class DecodedFrameEvidence:
    """One selected local JPEG and its measured clip-relative timing facts."""

    jpeg_path: Path = field(repr=False)
    local_pts_seconds: float | None
    width: int
    height: int
    timing_precision_status: TimingPrecisionStatus
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReferenceFrameResult:
    """Credential-safe durable reference-frame result returned by the service."""

    resource_id: str
    manifest_schema_version: int
    generation_policy_version: int
    channel_id: int
    requested_time_text: str
    source_timezone: str
    requested_time_utc: datetime
    selected_segment: RecordingSegment
    extraction_window: RecordingWindow
    frame_selection_policy: FrameSelectionPolicy
    jpeg_relative_path: Path
    manifest_relative_path: Path
    width: int
    height: int
    decoded_local_pts_seconds: float | None
    estimated_source_time_utc: datetime | None
    offset_from_requested_seconds: float | None
    timing_precision_status: TimingPrecisionStatus
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReferenceFrameResolution:
    """One completed reference-frame result with its durable-resource outcome."""

    result: ReferenceFrameResult
    outcome: ReferenceFrameOutcome


def parse_reference_frame_request(
    *,
    channel_id: int,
    requested_time_text: str,
    source_timezone: str | None = None,
    now_utc: datetime | None = None,
    source_kind: str = "nvr",
) -> ReferenceFrameRequest:
    """Parse one external request into an internal whole-second UTC value object."""
    if channel_id <= 0:
        raise ReferenceFrameInputError(_INVALID_CHANNEL)
    if source_kind != "nvr":
        raise UnsupportedReferenceFrameSourceError
    parsed = _parse_requested_time(requested_time_text)
    if parsed.microsecond != 0:
        raise ReferenceFrameInputError(_FRACTIONAL_SECONDS)
    parsed, normalized_source_timezone = _normalize_source_time(parsed, source_timezone)
    normalized_utc = parsed.astimezone(timezone.utc)
    if normalized_utc.microsecond != 0:
        raise ReferenceFrameInputError(_FRACTIONAL_SECONDS)
    comparison_now = datetime.now(timezone.utc) if now_utc is None else now_utc
    if comparison_now.tzinfo is None or normalized_utc > comparison_now.astimezone(timezone.utc):
        raise ReferenceFrameInputError(_FUTURE_TIME)
    return ReferenceFrameRequest(
        channel_id=channel_id,
        requested_time_text=requested_time_text,
        source_timezone=normalized_source_timezone,
        requested_time_utc=normalized_utc,
        source_kind=source_kind,
    )


def build_reference_replay_window(
    request: ReferenceFrameRequest, segment: RecordingSegment
) -> RecordingWindow:
    """Build a six-second replay window contained entirely in the selected segment."""
    if segment.channel_id != request.channel_id or not (
        segment.start_utc <= request.requested_time_utc < segment.end_utc
    ):
        raise ReferenceFrameSegmentMismatchError
    start = max(request.requested_time_utc - timedelta(seconds=2), segment.start_utc)
    end = min(
        request.requested_time_utc + REPLAY_DURATION - timedelta(seconds=2),
        segment.end_utc,
    )
    return RecordingWindow(request.channel_id, start, end)


def segment_identity(segment: RecordingSegment) -> str:
    """Return a deterministic credential-free recording-segment identifier."""
    return (
        f"segment-{segment.start_utc.strftime('%Y%m%dT%H%M%SZ')}-"
        f"{segment.end_utc.strftime('%Y%m%dT%H%M%SZ')}"
    )


def _source_zone(source_timezone: str) -> ZoneInfo | timezone:
    if source_timezone == "Asia/Seoul":
        try:
            return ZoneInfo(source_timezone)
        except ZoneInfoNotFoundError:
            return _KST
    return ZoneInfo(source_timezone)


def _parse_requested_time(requested_time_text: str) -> datetime:
    normalized_text = (
        f"{requested_time_text[:-1]}+00:00"
        if requested_time_text.endswith("Z")
        else requested_time_text
    )
    try:
        return datetime.fromisoformat(normalized_text)
    except (TypeError, ValueError):
        raise ReferenceFrameInputError(_INVALID_TIME_OR_TIMEZONE) from None


def _normalize_source_time(parsed: datetime, source_timezone: str | None) -> tuple[datetime, str]:
    if parsed.tzinfo is None:
        normalized_source_timezone = source_timezone or "Asia/Seoul"
        source_zone = _parse_source_zone(normalized_source_timezone)
        return _localize_unambiguous(parsed, source_zone), normalized_source_timezone
    if source_timezone is None or source_timezone == str(parsed.tzinfo):
        return parsed, str(parsed.tzinfo)
    source_zone = _parse_source_zone(source_timezone)
    if parsed.utcoffset() != parsed.astimezone(source_zone).utcoffset():
        raise ReferenceFrameInputError(_INVALID_TIME_OR_TIMEZONE)
    return parsed, source_timezone


def _parse_source_zone(source_timezone: str) -> ZoneInfo | timezone:
    try:
        return _source_zone(source_timezone)
    except (ValueError, ZoneInfoNotFoundError):
        raise ReferenceFrameInputError(_INVALID_TIME_OR_TIMEZONE) from None


def _is_frame_selection_policy(value: object) -> TypeGuard[FrameSelectionPolicy]:
    return isinstance(value, FrameSelectionPolicy)


def _localize_unambiguous(value: datetime, source_zone: ZoneInfo | timezone) -> datetime:
    first = value.replace(tzinfo=source_zone, fold=0)
    second = value.replace(tzinfo=source_zone, fold=1)
    round_trip = first.astimezone(timezone.utc).astimezone(source_zone).replace(tzinfo=None)
    if first.utcoffset() != second.utcoffset() or round_trip != value:
        raise ReferenceFrameInputError(_AMBIGUOUS_OR_NONEXISTENT)
    return first
