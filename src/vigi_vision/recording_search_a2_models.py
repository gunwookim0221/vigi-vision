"""Closed Phase 7A-2 acquisition records and deterministic identity helpers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from fractions import Fraction
from math import gcd
from typing import Annotated, ClassVar, Final, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_serializer,
    model_validator,
)

from vigi_vision.durable_io import CanonicalUtc  # noqa: TC001 - runtime Pydantic field type.
from vigi_vision.recording import (  # noqa: TC001 - runtime dataclass field types.
    RecordingSegment,
    RecordingWindow,
    ReplayRequest,
)
from vigi_vision.recording_search_models import (
    RecordingSearchBaseline,
    RecordingSearchPolicy,
    RecordingSearchState,
)
from vigi_vision.replay import ReplayClip  # noqa: TC001 - runtime dataclass field type.

_FRACTIONAL_UTC_PATTERN: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_INVESTIGATION_PATTERN: Final = re.compile(
    r"^object-disappearance-(?:v3-)?ch[1-9][0-9]*-[0-9]{8}T[0-9]{6}Z$"
)
_RUN_PATTERN: Final = re.compile(r"^search-run-[0-9a-f]{8,64}$")
_OPERATION_PATTERN: Final = re.compile(r"^acquisition-op-[a-z0-9-]{1,96}$")
_REQUEST_PATTERN: Final = re.compile(r"^probe-request-[a-z0-9-]{1,96}$")
_SESSION_PATTERN: Final = re.compile(r"^decode-session-[a-z0-9-]{1,96}$")
_ACQUISITION_PATTERN: Final = re.compile(r"^acquisition-[0-9a-f]{64}$")
_FRAME_PATTERN: Final = re.compile(r"^frame-[0-9a-f]{64}$")
_SEGMENT_PATTERN: Final = re.compile(r"^segment-\d{8}T\d{6}Z-\d{8}T\d{6}Z$")
_JPEG_PATTERN: Final = re.compile(r"^evidence/frames/frame-[0-9a-f]{64}\.jpg$")
_MAX_DIMENSION: Final = 16384
_MAX_JPEG_BYTES: Final = 256 * 1024 * 1024
_MAX_TIME_BASE: Final = 2**31 - 1
_MAX_ORDINAL: Final = 2**63 - 1


class AcquisitionOperationKind(str, Enum):
    """The only operation kind admitted by schema 2."""

    RECORDING_PROBE_ACQUISITION_V1 = "recording_probe_acquisition_v1"


class AcquisitionOperationState(str, Enum):
    """The immutable operation lifecycle state."""

    ADMITTED = "ADMITTED"


class ProbeRequestStatus(str, Enum):
    """Acquisition request lifecycle states."""

    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


_FAILURE_REASONS: Final = frozenset(
    {
        "recording_unavailable",
        "acquisition_failed",
        "decode_failed",
        "missing_provenance",
        "invalid_artifact",
        "publication_conflict",
        "interrupted",
        "process_lock_released",
        "unexpected_error",
    }
)


def _parse_fractional_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        if _FRACTIONAL_UTC_PATTERN.fullmatch(value) is None:
            raise ValueError
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError
    return parsed.astimezone(timezone.utc)


CanonicalFractionalUtc = Annotated[datetime, BeforeValidator(_parse_fractional_utc)]


class SourceTimeBase(BaseModel):
    """A reduced, strictly positive decoder time base."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    numerator: StrictInt = Field(gt=0, le=_MAX_TIME_BASE)
    denominator: StrictInt = Field(gt=0, le=_MAX_TIME_BASE)

    @model_validator(mode="after")
    def require_reduced(self) -> SourceTimeBase:
        """Require the decoder time base to be reduced."""
        if gcd(self.numerator, self.denominator) != 1:
            raise ValueError
        return self


def canonical_frame_id_for(
    investigation_id: str,
    search_run_id: str,
    channel_id: int,
    source_segment_id: str,
    decoded_frame_utc: datetime,
) -> str:
    """Derive the immutable canonical ID from the approved five-field tuple."""
    serialized = json.dumps(
        {
            "investigation_id": investigation_id,
            "search_run_id": search_run_id,
            "channel_id": channel_id,
            "source_segment_id": source_segment_id,
            "decoded_frame_utc": _fractional_utc_text(decoded_frame_utc),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"frame-{hashlib.sha256(serialized).hexdigest()}"


def acquisition_id_for(
    source_segment_id: str,
    extraction_start_utc: datetime,
    extraction_end_utc: datetime,
    acquisition_policy_version: str,
) -> str:
    """Derive a credential-free acquisition identity."""
    serialized = json.dumps(
        [
            source_segment_id,
            _whole_utc_text(extraction_start_utc),
            _whole_utc_text(extraction_end_utc),
            acquisition_policy_version,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"acquisition-{hashlib.sha256(serialized).hexdigest()}"


def probe_request_identity(
    search_run_id: str, channel_id: int, requested_time_utc: datetime
) -> tuple[str, int, str]:
    """Return the exact duplicate-request identity tuple."""
    return search_run_id, channel_id, _whole_utc_text(requested_time_utc)


def decoded_frame_utc_for(
    physical_replay_origin_utc: datetime,
    source_pts: int,
    source_time_base: SourceTimeBase,
) -> datetime:
    """Map raw source ticks to UTC with one ties-to-even microsecond rounding."""
    if source_pts < 0:
        raise ValueError
    origin = _parse_fractional_utc(_fractional_utc_text(physical_replay_origin_utc))
    offset_microseconds = Fraction(
        source_pts * source_time_base.numerator * 1_000_000,
        source_time_base.denominator,
    )
    rounded = _round_fraction_to_even(offset_microseconds)
    return origin + timedelta(microseconds=rounded)


class AcquisitionOperationRecord(BaseModel):
    """Immutable server-created operation admission record."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    record_type: Literal["acquisition_operation"]
    operation_id: StrictStr
    investigation_id: StrictStr
    search_run_id: StrictStr
    operation_kind: Literal["recording_probe_acquisition_v1"]
    state: Literal["ADMITTED"]
    admitted_at_utc: CanonicalFractionalUtc

    @field_serializer("admitted_at_utc")
    def serialize_admitted_at(self, value: datetime) -> str:
        """Serialize operation admission with exactly six UTC fractional digits."""
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    @model_validator(mode="after")
    def validate_identity(self) -> AcquisitionOperationRecord:
        """Require server-owned operation identity fields."""
        if (
            _OPERATION_PATTERN.fullmatch(self.operation_id) is None
            or _INVESTIGATION_PATTERN.fullmatch(self.investigation_id) is None
            or _RUN_PATTERN.fullmatch(self.search_run_id) is None
        ):
            raise ValueError
        return self


class CanonicalProbeFrameRecord(BaseModel):
    """Immutable canonical frame provenance and artifact metadata."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    record_type: Literal["canonical_probe_frame"]
    canonical_frame_id: StrictStr
    investigation_id: StrictStr
    search_run_id: StrictStr
    operation_id: StrictStr
    channel_id: StrictInt = Field(gt=0, le=2**31 - 1)
    acquisition_id: StrictStr
    source_segment_id: StrictStr
    segment_start_utc: CanonicalUtc
    segment_end_utc: CanonicalUtc
    extraction_start_utc: CanonicalUtc
    extraction_end_utc: CanonicalUtc
    decode_session_id: StrictStr
    physical_replay_origin_utc: CanonicalFractionalUtc
    source_pts: StrictInt = Field(ge=0)
    source_time_base: SourceTimeBase
    decoded_frame_utc: CanonicalFractionalUtc
    decoded_pts: StrictInt = Field(ge=0)
    replay_time_base: SourceTimeBase
    decoded_ordinal: StrictInt = Field(ge=0, le=_MAX_ORDINAL)
    source_width: StrictInt = Field(gt=0, le=_MAX_DIMENSION)
    source_height: StrictInt = Field(gt=0, le=_MAX_DIMENSION)
    jpeg_relative_path: StrictStr
    jpeg_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    jpeg_size_bytes: StrictInt = Field(gt=0, le=_MAX_JPEG_BYTES)
    acquired_at_utc: CanonicalFractionalUtc

    @field_serializer(
        "physical_replay_origin_utc",
        "decoded_frame_utc",
        "acquired_at_utc",
    )
    def serialize_fractional_utc(self, value: datetime) -> str:
        """Serialize frame timing with exactly six UTC fractional digits."""
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    @model_validator(mode="after")
    def validate_relationships(self) -> CanonicalProbeFrameRecord:
        """Require internally consistent canonical-frame provenance."""
        if (
            _FRAME_PATTERN.fullmatch(self.canonical_frame_id) is None
            or _INVESTIGATION_PATTERN.fullmatch(self.investigation_id) is None
            or _RUN_PATTERN.fullmatch(self.search_run_id) is None
            or _OPERATION_PATTERN.fullmatch(self.operation_id) is None
            or _ACQUISITION_PATTERN.fullmatch(self.acquisition_id) is None
            or _SEGMENT_PATTERN.fullmatch(self.source_segment_id) is None
            or _SESSION_PATTERN.fullmatch(self.decode_session_id) is None
            or _JPEG_PATTERN.fullmatch(self.jpeg_relative_path) is None
        ):
            raise ValueError
        if self.segment_start_utc >= self.segment_end_utc:
            raise ValueError
        if self.extraction_start_utc >= self.extraction_end_utc:
            raise ValueError
        if (
            self.extraction_start_utc < self.segment_start_utc
            or self.extraction_end_utc > self.segment_end_utc
            or self.decoded_frame_utc < self.segment_start_utc
            or self.decoded_frame_utc >= self.segment_end_utc
        ):
            raise ValueError
        if self.acquired_at_utc < self.decoded_frame_utc:
            raise ValueError
        expected_time = decoded_frame_utc_for(
            self.physical_replay_origin_utc, self.source_pts, self.source_time_base
        )
        if expected_time != self.decoded_frame_utc:
            raise ValueError
        if self.canonical_frame_id != canonical_frame_id_for(
            self.investigation_id,
            self.search_run_id,
            self.channel_id,
            self.source_segment_id,
            self.decoded_frame_utc,
        ):
            raise ValueError
        return self


class ProbeFrameRequestRecord(BaseModel):
    """Immutable request identity and acquisition outcome."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    record_type: Literal["probe_frame_request"]
    probe_request_id: StrictStr
    investigation_id: StrictStr
    search_run_id: StrictStr
    operation_id: StrictStr
    channel_id: StrictInt = Field(gt=0, le=2**31 - 1)
    requested_time_utc: CanonicalUtc
    status: ProbeRequestStatus
    canonical_frame_id: StrictStr | None
    alias_of_probe_request_id: StrictStr | None
    failure_reason: StrictStr | None
    created_at_utc: CanonicalUtc
    completed_at_utc: CanonicalUtc | None

    @model_validator(mode="after")
    def validate_relationships(self) -> ProbeFrameRequestRecord:
        """Require one closed request lifecycle shape."""
        if (
            _REQUEST_PATTERN.fullmatch(self.probe_request_id) is None
            or _INVESTIGATION_PATTERN.fullmatch(self.investigation_id) is None
            or _RUN_PATTERN.fullmatch(self.search_run_id) is None
            or _OPERATION_PATTERN.fullmatch(self.operation_id) is None
        ):
            raise ValueError
        if self.completed_at_utc is not None and self.completed_at_utc < self.created_at_utc:
            raise ValueError
        match self.status:
            case ProbeRequestStatus.PENDING:
                if (
                    self.completed_at_utc is not None
                    or self.canonical_frame_id is not None
                    or self.alias_of_probe_request_id is not None
                    or self.failure_reason is not None
                ):
                    raise ValueError
            case ProbeRequestStatus.SUCCEEDED:
                if (
                    self.completed_at_utc is None
                    or self.canonical_frame_id is None
                    or self.failure_reason is not None
                ):
                    raise ValueError
                if self.alias_of_probe_request_id == self.probe_request_id:
                    raise ValueError
            case ProbeRequestStatus.FAILED:
                if (
                    self.completed_at_utc is None
                    or self.canonical_frame_id is not None
                    or self.alias_of_probe_request_id is not None
                    or self.failure_reason not in _FAILURE_REASONS
                ):
                    raise ValueError
        return self


class RecordingSearchManifestV2(BaseModel):
    """Strict schema-2 acquisition-only run manifest."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2]
    investigation_id: StrictStr
    search_run_id: StrictStr
    state: RecordingSearchState
    created_at_utc: CanonicalUtc
    started_at_utc: CanonicalUtc | None
    completed_at_utc: CanonicalUtc | None
    confirmation: RecordingSearchBaseline
    policy: RecordingSearchPolicy
    acquisition_operation_ids: tuple[StrictStr, ...]
    probe_request_ids: tuple[StrictStr, ...]
    canonical_frame_ids: tuple[StrictStr, ...]
    failure_reason: StrictStr | None

    @model_validator(mode="after")
    def validate_manifest(self) -> RecordingSearchManifestV2:
        """Require the closed acquisition-only manifest shape."""
        self._validate_identity_and_times()
        self._validate_state()
        self._validate_indexes()
        return self

    def _validate_identity_and_times(self) -> None:
        if (
            _INVESTIGATION_PATTERN.fullmatch(self.investigation_id) is None
            or _RUN_PATTERN.fullmatch(self.search_run_id) is None
            or self.state
            not in {
                RecordingSearchState.PENDING,
                RecordingSearchState.RUNNING,
                RecordingSearchState.FAILED,
                RecordingSearchState.INTERRUPTED,
            }
            or self.confirmation.source_width > _MAX_DIMENSION
            or self.confirmation.source_height > _MAX_DIMENSION
        ):
            raise ValueError
        if self.started_at_utc is not None and self.started_at_utc < self.created_at_utc:
            raise ValueError
        if self.completed_at_utc is not None and self.completed_at_utc < self.created_at_utc:
            raise ValueError
        if (
            self.started_at_utc is not None
            and self.completed_at_utc is not None
            and self.completed_at_utc < self.started_at_utc
        ):
            raise ValueError

    def _validate_state(self) -> None:
        if self.state in {
            RecordingSearchState.PENDING,
            RecordingSearchState.RUNNING,
        }:
            if self.completed_at_utc is not None or self.failure_reason is not None:
                raise ValueError
            if self.state is RecordingSearchState.PENDING and self.started_at_utc is not None:
                raise ValueError
            if self.state is RecordingSearchState.RUNNING and self.started_at_utc is None:
                raise ValueError
        elif self.completed_at_utc is None or self.failure_reason not in _FAILURE_REASONS:
            raise ValueError

    def _validate_indexes(self) -> None:
        if len(set(self.acquisition_operation_ids)) != len(self.acquisition_operation_ids):
            raise ValueError
        if len(set(self.probe_request_ids)) != len(self.probe_request_ids):
            raise ValueError
        if len(set(self.canonical_frame_ids)) != len(self.canonical_frame_ids):
            raise ValueError
        if any(
            _OPERATION_PATTERN.fullmatch(value) is None for value in self.acquisition_operation_ids
        ):
            raise ValueError
        if any(_REQUEST_PATTERN.fullmatch(value) is None for value in self.probe_request_ids):
            raise ValueError
        if any(_FRAME_PATTERN.fullmatch(value) is None for value in self.canonical_frame_ids):
            raise ValueError

    def canonical_json(self) -> str:
        """Serialize the manifest deterministically."""
        return (
            json.dumps(self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        )


@dataclass(frozen=True, slots=True)
class BatchDecodeRequest:
    """One replay clip and its ordered target times for the A2 decoder."""

    channel_id: int
    segment: RecordingSegment
    extraction_window: RecordingWindow
    replay_request: ReplayRequest
    replay_clip: ReplayClip


@dataclass(frozen=True, slots=True)
class DecodedTargetResult:
    """Credential-free selected frame facts returned by an A2 decoder."""

    requested_time_utc: datetime
    physical_replay_origin_utc: datetime
    source_pts: int
    source_time_base: SourceTimeBase
    decoded_pts: int
    replay_time_base: SourceTimeBase
    decoded_ordinal: int
    source_width: int
    source_height: int
    jpeg_bytes: bytes = field(repr=False)
    decode_session_id: str
    segment: RecordingSegment | None = field(default=None, repr=False)
    extraction_window: RecordingWindow | None = field(default=None, repr=False)


def _round_fraction_to_even(value: Fraction) -> int:
    quotient, remainder = divmod(value.numerator, value.denominator)
    doubled = remainder * 2
    if doubled > value.denominator or (doubled == value.denominator and quotient % 2):
        quotient += 1
    return quotient


def _fractional_utc_text(value: datetime) -> str:
    normalized = _parse_fractional_utc(value.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    return normalized.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _whole_utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond != 0:
        raise ValueError
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
