"""Typed Phase 6 confirmation values, manifests, and domain failures."""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import ClassVar, Final, Literal, final

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator
from typing_extensions import override

from vigi_vision.durable_io import CanonicalUtc
from vigi_vision.reference_frame_models import FrameSelectionPolicy, TimingPrecisionStatus

CONFIRMATION_SCHEMA_VERSION: Final = 2
CONFIRMATION_KIND: Final = "object_disappearance"
CONFIRMATION_SCENARIO: Final = "object-disappearance"
MINIMUM_ROI_SIZE: Final = 4
MINIMUM_CANDIDATE_OFFSET: Final = -300
MAXIMUM_CANDIDATE_OFFSET: Final = 300
_RESOURCE_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,191}$")
_INVESTIGATION_ID_PATTERN: Final = re.compile(
    r"^object-disappearance-ch[1-9][0-9]*-[0-9]{8}T[0-9]{6}Z$"
)
_ARTIFACT_RELATIVE_ROOT: Final = "artifacts/investigations"


class ConfirmationError(RuntimeError):
    """Base class for safe confirmation-domain failures."""


@final
@dataclass(frozen=True, slots=True)
class ConfirmationRequestError(ConfirmationError):
    """Raised when confirmation input cannot be accepted as a typed request."""

    @override
    def __str__(self) -> str:
        return "The investigation confirmation request is invalid."


@final
@dataclass(frozen=True, slots=True)
class ConfirmationCandidateMismatchError(ConfirmationError):
    """Raised when trusted resource identity does not match the selected candidate."""

    @override
    def __str__(self) -> str:
        return "The selected reference frame is not the requested investigation candidate."


@final
@dataclass(frozen=True, slots=True)
class ConfirmationImageDimensionMismatchError(ConfirmationError):
    """Raised when client ROI context disagrees with trusted image dimensions."""

    @override
    def __str__(self) -> str:
        return "The selected reference frame dimensions no longer match the review."


@final
@dataclass(frozen=True, slots=True)
class ConfirmationInvalidRoiError(ConfirmationError):
    """Raised when the final source-pixel ROI is outside the trusted image."""

    @override
    def __str__(self) -> str:
        return "The investigation ROI is invalid for the selected reference frame."


@final
@dataclass(frozen=True, slots=True)
class ConfirmationConflictError(ConfirmationError):
    """Raised when an immutable investigation already contains different input."""

    @override
    def __str__(self) -> str:
        return "This investigation already has different confirmed conditions."


@final
@dataclass(frozen=True, slots=True)
class ConfirmationInProgressError(ConfirmationError):
    """Raised when another operation owns an unverifiable or active claim."""

    @override
    def __str__(self) -> str:
        return "Investigation confirmation is already in progress."


@final
@dataclass(frozen=True, slots=True)
class ConfirmationArtifactError(ConfirmationError):
    """Raised when confirmation publication cannot complete safely."""

    @override
    def __str__(self) -> str:
        return "Investigation confirmation could not be published safely."


@final
@dataclass(frozen=True, slots=True)
class ConfirmationCorruptError(ConfirmationError):
    """Raised when an existing confirmation package fails strict parsing."""

    @override
    def __str__(self) -> str:
        return "The stored investigation confirmation is corrupt."


@final
@dataclass(frozen=True, slots=True)
class InvestigationConfirmationNotFoundError(ConfirmationError):
    """Raised when a requested durable confirmation does not exist."""

    @override
    def __str__(self) -> str:
        return "The confirmed investigation was not found."


@final
@dataclass(frozen=True, slots=True)
class LegacyInvestigationError(ConfirmationError):
    """Raised when a legacy or unconfirmed package is not Phase 7 input."""

    @override
    def __str__(self) -> str:
        return "The investigation is not a confirmed Phase 7 input."


@final
@dataclass(frozen=True, slots=True)
class ConfirmedInputInvalidError(ConfirmationError):
    """Raised when a confirmed package or its trusted frame cannot be loaded."""

    @override
    def __str__(self) -> str:
        return "The confirmed investigation could not be loaded safely."


class RoiProvenance(str, Enum):
    """The three approved origins of a final ROI."""

    MANUAL = "manual"
    ASSISTED = "assisted"
    ASSISTED_THEN_ADJUSTED = "assisted_then_adjusted"


class ConfirmationOutcome(str, Enum):
    """The durable result of a confirmation request."""

    CREATED = "created"
    REUSED = "reused"


class ConfirmationRoi(BaseModel):
    """One canonical source-image-pixel rectangle."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    x: StrictInt = Field(ge=0)
    y: StrictInt = Field(ge=0)
    width: StrictInt = Field(ge=MINIMUM_ROI_SIZE)
    height: StrictInt = Field(ge=MINIMUM_ROI_SIZE)
    coordinate_space: Literal["source_pixels"]
    provenance: RoiProvenance


class ConfirmationRequest(BaseModel):
    """Typed user-controlled confirmation input before trusted resolution."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    reference_frame_resource_id: StrictStr = Field(min_length=1, max_length=192)
    reference_time: StrictStr = Field(min_length=1)
    source_timezone: StrictStr = Field(min_length=1)
    candidate_offset_seconds: StrictInt = Field(
        ge=MINIMUM_CANDIDATE_OFFSET,
        le=MAXIMUM_CANDIDATE_OFFSET,
    )
    source_width: StrictInt = Field(gt=0)
    source_height: StrictInt = Field(gt=0)
    roi: ConfirmationRoi

    @model_validator(mode="after")
    def require_safe_resource_id(self) -> "ConfirmationRequest":
        """Reject path-shaped identifiers before the trusted store is queried."""
        if _RESOURCE_ID_PATTERN.fullmatch(self.reference_frame_resource_id) is None:
            raise ValueError
        return self


class ConfirmationReferenceFrame(BaseModel):
    """Server-copied immutable resource facts required for traceability."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    resource_id: StrictStr
    schema_version: StrictInt = Field(gt=0)
    generation_policy_version: StrictInt = Field(gt=0)
    requested_time: StrictStr
    requested_time_utc: CanonicalUtc
    source_timezone: StrictStr
    frame_selection_policy: FrameSelectionPolicy
    width: StrictInt = Field(gt=0)
    height: StrictInt = Field(gt=0)


class ConfirmationTiming(BaseModel):
    """Copied decoder evidence without inventing absolute source timing."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    decoded_local_pts_seconds: float | None
    estimated_source_time_utc: CanonicalUtc | None
    offset_from_requested_seconds: float | None
    timing_precision_status: TimingPrecisionStatus
    warnings: tuple[StrictStr, ...]


class ConfirmationRecord(BaseModel):
    """Trusted confirmation facts nested inside the schema 2 manifest."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    channel_id: StrictInt = Field(gt=0)
    candidate_offset_seconds: StrictInt = Field(
        ge=MINIMUM_CANDIDATE_OFFSET,
        le=MAXIMUM_CANDIDATE_OFFSET,
    )
    reference_frame: ConfirmationReferenceFrame
    timing: ConfirmationTiming
    roi: ConfirmationRoi


class ConfirmationManifest(BaseModel):
    """Immutable schema 2 confirmation manifest persisted under investigations."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2]
    investigation_id: StrictStr
    investigation_kind: Literal["object_disappearance"]
    scenario_id: Literal["object-disappearance"]
    status: Literal["confirmed"]
    anchor_time_utc: CanonicalUtc
    source_timezone: StrictStr
    confirmed_at_utc: CanonicalUtc
    artifact_directory_relative: StrictStr
    confirmation: ConfirmationRecord

    @model_validator(mode="after")
    def validate_identity_and_times(self) -> "ConfirmationManifest":
        """Enforce canonical identity, UTC timestamps, and artifact ownership."""
        if _INVESTIGATION_ID_PATTERN.fullmatch(self.investigation_id) is None:
            raise ValueError
        if self.artifact_directory_relative != artifact_relative_path(self.investigation_id):
            raise ValueError
        for value in (self.anchor_time_utc, self.confirmed_at_utc):
            _require_utc_whole_second(value)
        _require_utc_whole_second(self.confirmation.reference_frame.requested_time_utc)
        if self.confirmation.timing.estimated_source_time_utc is not None:
            _require_utc_whole_second(self.confirmation.timing.estimated_source_time_utc)
        return self

    def material_json(self) -> str:
        """Return canonical material content excluding the volatile confirmation time."""
        document = self.model_dump(mode="json", exclude={"confirmed_at_utc"})
        return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@final
@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    """The safe result of create-or-resolve confirmation execution."""

    manifest: ConfirmationManifest
    outcome: ConfirmationOutcome
    artifact_directory: Path = field(repr=False)


@final
@dataclass(frozen=True, slots=True)
class ConfirmedInvestigationInput:
    """Strict Phase 7 input resolved from one published immutable confirmation."""

    investigation_id: str
    channel_id: int
    anchor_time_utc: datetime
    source_timezone: str
    candidate_offset_seconds: int
    reference_frame_resource_id: str
    requested_time_text: str
    requested_time_utc: datetime
    generation_policy_version: int
    frame_selection_policy: str
    estimated_source_time_utc: datetime | None
    decoded_local_pts_seconds: float | None
    timing_precision_status: str
    warnings: tuple[str, ...]
    source_width: int
    source_height: int
    roi: ConfirmationRoi
    jpeg_path: Path = field(repr=False)


def artifact_relative_path(investigation_id: str) -> str:
    """Return the fixed safe relative location for one confirmation package."""
    return f"{_ARTIFACT_RELATIVE_ROOT}/{investigation_id}"


def is_investigation_id(value: str) -> bool:
    """Return whether a value is a canonical credential-free investigation ID."""
    return _INVESTIGATION_ID_PATTERN.fullmatch(value) is not None


def investigation_id_for(channel_id: int, anchor_time_utc: datetime) -> str:
    """Return the deterministic credential-free object-disappearance identity."""
    if type(channel_id) is not int or channel_id <= 0:
        raise ConfirmationRequestError
    _require_utc_whole_second(anchor_time_utc)
    return f"object-disappearance-ch{channel_id}-{anchor_time_utc.strftime('%Y%m%dT%H%M%SZ')}"


def canonical_manifest_json(manifest: ConfirmationManifest) -> str:
    """Serialize a manifest with stable UTC text, ordering, and trailing newline."""
    document = manifest.model_dump(mode="json")
    return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _require_utc_whole_second(value: datetime) -> None:
    """Require a timezone-aware UTC whole-second datetime."""
    if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond != 0:
        raise ValueError
