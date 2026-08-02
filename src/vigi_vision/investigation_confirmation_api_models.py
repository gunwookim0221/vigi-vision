"""Strict HTTP schemas for the durable investigation-confirmation boundary."""

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from vigi_vision.investigation_confirmation_models import (
    ConfirmationManifest,
    ConfirmationOutcome,
    ConfirmationRequest,
    ConfirmationResult,
    ConfirmationRoi,
    RoiProvenance,
)
from vigi_vision.reference_frame_models import TimingPrecisionStatus


class InvestigationConfirmationRoiBody(BaseModel):
    """User-controlled source-pixel ROI fields accepted by the API."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    x: StrictInt = Field(ge=0)
    y: StrictInt = Field(ge=0)
    width: StrictInt = Field(ge=4)
    height: StrictInt = Field(ge=4)
    coordinate_space: Literal["source_pixels"]
    provenance: RoiProvenance

    def to_domain(self) -> ConfirmationRoi:
        """Parse the transport ROI into the strict confirmation value object."""
        return ConfirmationRoi(
            x=self.x,
            y=self.y,
            width=self.width,
            height=self.height,
            coordinate_space=self.coordinate_space,
            provenance=self.provenance,
        )


class InvestigationConfirmationCreateBody(BaseModel):
    """Caller-controlled confirmation fields; trusted facts are excluded."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    reference_frame_resource_id: StrictStr = Field(
        min_length=1,
        max_length=192,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,191}$",
    )
    reference_time: StrictStr = Field(min_length=1)
    source_timezone: StrictStr = Field(min_length=1)
    candidate_offset_seconds: StrictInt = Field(ge=-300, le=300)
    source_width: StrictInt = Field(gt=0)
    source_height: StrictInt = Field(gt=0)
    roi: InvestigationConfirmationRoiBody

    def to_domain(self) -> ConfirmationRequest:
        """Parse the transport body into the existing confirmation service request."""
        return ConfirmationRequest(
            reference_frame_resource_id=self.reference_frame_resource_id,
            reference_time=self.reference_time,
            source_timezone=self.source_timezone,
            candidate_offset_seconds=self.candidate_offset_seconds,
            source_width=self.source_width,
            source_height=self.source_height,
            roi=self.roi.to_domain(),
        )


class InvestigationConfirmationTimingResponse(BaseModel):
    """Safe timing evidence copied from the trusted reference-frame manifest."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    estimated_source_time_utc: datetime | None
    timing_precision_status: TimingPrecisionStatus


class InvestigationConfirmationRoiResponse(BaseModel):
    """Canonical source-pixel ROI returned after durable confirmation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    x: StrictInt
    y: StrictInt
    width: StrictInt
    height: StrictInt
    coordinate_space: Literal["source_pixels"]
    provenance: RoiProvenance


class InvestigationConfirmationSummaryResponse(BaseModel):
    """Safe canonical confirmation facts suitable for browser draft replacement."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    channel_id: StrictInt = Field(gt=0)
    candidate_offset_seconds: StrictInt = Field(ge=-300, le=300)
    reference_frame_resource_id: StrictStr
    requested_time_utc: datetime
    timing: InvestigationConfirmationTimingResponse
    source_width: StrictInt = Field(gt=0)
    source_height: StrictInt = Field(gt=0)
    roi: InvestigationConfirmationRoiResponse


class InvestigationConfirmationResponse(BaseModel):
    """Public immutable schema 2 confirmation representation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    investigation_id: StrictStr
    outcome: ConfirmationOutcome
    status: Literal["confirmed"]
    schema_version: Literal[2]
    confirmed_at_utc: datetime
    artifact_directory_relative: StrictStr
    confirmation: InvestigationConfirmationSummaryResponse


def confirmation_response(result: ConfirmationResult) -> InvestigationConfirmationResponse:
    """Serialize a newly created or reused durable result without filesystem paths."""
    return _response_from_manifest(result.manifest, result.outcome)


def loaded_confirmation_response(
    loaded: ConfirmationManifest,
) -> InvestigationConfirmationResponse:
    """Serialize a strictly revalidated manifest as a read response."""
    return _response_from_manifest(loaded, ConfirmationOutcome.CREATED)


def _response_from_manifest(
    manifest: ConfirmationManifest, outcome: ConfirmationOutcome
) -> InvestigationConfirmationResponse:
    confirmation = manifest.confirmation
    reference = confirmation.reference_frame
    timing = confirmation.timing
    roi = confirmation.roi
    return InvestigationConfirmationResponse(
        investigation_id=manifest.investigation_id,
        outcome=outcome,
        status=manifest.status,
        schema_version=manifest.schema_version,
        confirmed_at_utc=manifest.confirmed_at_utc,
        artifact_directory_relative=manifest.artifact_directory_relative,
        confirmation=InvestigationConfirmationSummaryResponse(
            channel_id=confirmation.channel_id,
            candidate_offset_seconds=confirmation.candidate_offset_seconds,
            reference_frame_resource_id=reference.resource_id,
            requested_time_utc=reference.requested_time_utc,
            timing=InvestigationConfirmationTimingResponse(
                estimated_source_time_utc=timing.estimated_source_time_utc,
                timing_precision_status=timing.timing_precision_status,
            ),
            source_width=reference.width,
            source_height=reference.height,
            roi=InvestigationConfirmationRoiResponse(
                x=roi.x,
                y=roi.y,
                width=roi.width,
                height=roi.height,
                coordinate_space=roi.coordinate_space,
                provenance=roi.provenance,
            ),
        ),
    )
