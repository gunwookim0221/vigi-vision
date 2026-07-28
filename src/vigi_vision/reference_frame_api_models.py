"""Pydantic HTTP schemas for the reference-frame API boundary."""

from datetime import datetime
from typing import ClassVar, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import Self

from vigi_vision.reference_frame_models import (
    FrameSelectionPolicy,
    ReferenceFrameOutcome,
    ReferenceFrameResolution,
    TimingPrecisionStatus,
    segment_identity,
)

_NAIVE_TIMEZONE_MESSAGE: Final = "source_timezone is required for naive requested_time"


class ReferenceFrameCreateBody(BaseModel):
    """The only user-controlled inputs for a synchronous reference-frame request."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    channel_id: int = Field(gt=0, description="Positive NVR channel identifier.")
    requested_time: str = Field(
        description="Whole-second ISO 8601/RFC 3339 source timestamp; aware form is preferred."
    )
    source_timezone: str | None = Field(
        default=None,
        description=(
            "IANA timezone required for naive timestamps and optional for aware timestamps."
        ),
    )

    @model_validator(mode="after")
    def require_timezone_for_naive_time(self) -> Self:
        """Reject a naive API timestamp without its explicit source interpretation."""
        normalized_time = (
            f"{self.requested_time[:-1]}+00:00"
            if self.requested_time.endswith("Z")
            else self.requested_time
        )
        try:
            parsed = datetime.fromisoformat(normalized_time)
        except ValueError:
            return self
        if parsed.tzinfo is None and self.source_timezone is None:
            raise ValueError(_NAIVE_TIMEZONE_MESSAGE)
        return self


class ReferenceFrameSegmentResponse(BaseModel):
    """Credential-free facts identifying the recording segment that supplied a frame."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    id: str
    start_utc: datetime
    end_utc: datetime


class ReferenceFrameWindowResponse(BaseModel):
    """UTC bounds of the replay interval supplied to the existing replay boundary."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    start_utc: datetime
    end_utc: datetime


class ReferenceFrameImageResponse(BaseModel):
    """Safe durable JPEG facts suitable for later ROI coordinate mapping."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    media_type: Literal["image/jpeg"]
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class ReferenceFrameTimingResponse(BaseModel):
    """Conservative frame-time evidence without an unsupported absolute timing claim."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    precision_status: TimingPrecisionStatus
    decoded_clip_relative_pts_seconds: float | None
    estimated_source_time_utc: datetime | None
    offset_from_requested_seconds: float | None


class ReferenceFrameCreateResponse(BaseModel):
    """One completed durable reference-frame resource returned by the API."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    resource_id: str
    outcome: ReferenceFrameOutcome
    manifest_schema_version: int = Field(gt=0)
    generation_policy_version: int = Field(gt=0)
    channel_id: int = Field(gt=0)
    requested_time_utc: datetime
    source_timezone: str
    selected_segment: ReferenceFrameSegmentResponse
    extraction_window: ReferenceFrameWindowResponse
    frame_selection_policy: FrameSelectionPolicy
    image_url: str
    image: ReferenceFrameImageResponse
    timing: ReferenceFrameTimingResponse
    warnings: tuple[str, ...]


class ReferenceFrameErrorDetail(BaseModel):
    """A stable safe field-level error category without rejected input values."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    field: str
    code: str


class ReferenceFrameErrorBody(BaseModel):
    """The stable credential-safe error payload shared by both endpoints."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    details: tuple[ReferenceFrameErrorDetail, ...] | None = None


class ReferenceFrameErrorResponse(BaseModel):
    """The HTTP error envelope for fixed safe API diagnostics."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    error: ReferenceFrameErrorBody


def reference_frame_response(
    resolution: ReferenceFrameResolution,
) -> ReferenceFrameCreateResponse:
    """Serialize existing domain facts without exposing paths or replay details."""
    result = resolution.result
    return ReferenceFrameCreateResponse(
        resource_id=result.resource_id,
        outcome=resolution.outcome,
        manifest_schema_version=result.manifest_schema_version,
        generation_policy_version=result.generation_policy_version,
        channel_id=result.channel_id,
        requested_time_utc=result.requested_time_utc,
        source_timezone=result.source_timezone,
        selected_segment=ReferenceFrameSegmentResponse(
            id=segment_identity(result.selected_segment),
            start_utc=result.selected_segment.start_utc,
            end_utc=result.selected_segment.end_utc,
        ),
        extraction_window=ReferenceFrameWindowResponse(
            start_utc=result.extraction_window.start_utc,
            end_utc=result.extraction_window.end_utc,
        ),
        frame_selection_policy=result.frame_selection_policy,
        image_url=f"/api/v1/reference-frames/{result.resource_id}/image",
        image=ReferenceFrameImageResponse(
            media_type="image/jpeg",
            width=result.width,
            height=result.height,
        ),
        timing=ReferenceFrameTimingResponse(
            precision_status=result.timing_precision_status,
            decoded_clip_relative_pts_seconds=result.decoded_local_pts_seconds,
            estimated_source_time_utc=result.estimated_source_time_utc,
            offset_from_requested_seconds=result.offset_from_requested_seconds,
        ),
        warnings=result.warnings,
    )
