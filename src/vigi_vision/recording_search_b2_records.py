"""Strict immutable Phase 7B schema-3 child records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_serializer,
    model_validator,
)

from vigi_vision.durable_io import CanonicalUtc  # noqa: TC001 - Pydantic runtime field type.
from vigi_vision.investigation_confirmation_models import ConfirmationRoi  # noqa: TC001
from vigi_vision.object_presence_evidence import ClassificationResult, RawComparison
from vigi_vision.object_presence_values import ClassificationOutcome, VisualReason  # noqa: TC001
from vigi_vision.recording_search_a2_models import CanonicalFractionalUtc  # noqa: TC001
from vigi_vision.recording_search_b2_identity import (
    alias_id_for,
    baseline_observation_id_for,
    is_alias_id,
    is_baseline_id,
    is_classification_operation_id,
    is_observation_id,
    observation_id_for,
)


class ConfirmedReferenceBaselineRecord(BaseModel):
    """Run-owned reference observation without invented recording provenance."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    record_type: Literal["confirmed_reference_baseline"]
    observation_id: StrictStr
    investigation_id: StrictStr
    search_run_id: StrictStr
    channel_id: StrictInt = Field(gt=0)
    reference_frame_resource_id: StrictStr = Field(min_length=1, max_length=192)
    reference_requested_time_utc: CanonicalUtc
    source_width: StrictInt = Field(gt=0)
    source_height: StrictInt = Field(gt=0)
    roi: ConfirmationRoi
    jpeg_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    jpeg_size_bytes: StrictInt = Field(gt=0)
    timing_precision_status: StrictStr = Field(min_length=1, max_length=64)
    warnings: tuple[StrictStr, ...]
    state: Literal["PRESENT"]
    reason_code: Literal["user_confirmed_reference"]
    published_at_utc: CanonicalFractionalUtc

    @field_serializer("published_at_utc")
    def serialize_published_at(self, value: datetime) -> str:
        """Serialize the administrative timestamp in canonical UTC form."""
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    @model_validator(mode="after")
    def validate_record(self) -> ConfirmedReferenceBaselineRecord:
        """Require the deterministic baseline identity and source-pixel ROI."""
        if not is_baseline_id(self.observation_id) or self.roi.coordinate_space != "source_pixels":
            raise ValueError
        expected = baseline_observation_id_for(
            investigation_id=self.investigation_id,
            search_run_id=self.search_run_id,
            channel_id=self.channel_id,
            reference_frame_resource_id=self.reference_frame_resource_id,
            reference_requested_time_utc=self.reference_requested_time_utc,
            source_width=self.source_width,
            source_height=self.source_height,
            roi=self.roi.model_dump(mode="json"),
            jpeg_sha256=self.jpeg_sha256,
            jpeg_size_bytes=self.jpeg_size_bytes,
        )
        if self.observation_id != expected or self.roi.x + self.roi.width > self.source_width:
            raise ValueError
        if self.roi.y + self.roi.height > self.source_height:
            raise ValueError
        return self


class ClassificationOperationRecord(BaseModel):
    """Immutable successful classification admission record."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    record_type: Literal["classification_operation"]
    classification_operation_id: StrictStr
    investigation_id: StrictStr
    search_run_id: StrictStr
    operation_kind: Literal["recording_probe_classification_v1"]
    state: Literal["ADMITTED"]
    probe_request_id: StrictStr
    canonical_frame_id: StrictStr
    baseline_observation_id: StrictStr
    classifier_policy_version: StrictStr = Field(min_length=1, max_length=128)
    admitted_at_utc: CanonicalFractionalUtc

    @field_serializer("admitted_at_utc")
    def serialize_admitted_at(self, value: datetime) -> str:
        """Serialize the administrative timestamp in canonical UTC form."""
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    @model_validator(mode="after")
    def validate_record(self) -> ClassificationOperationRecord:
        """Validate the closed operation identity shape."""
        if not is_classification_operation_id(self.classification_operation_id):
            raise ValueError
        if not is_baseline_id(self.baseline_observation_id):
            raise ValueError
        return self


class RecordingProbeObservationRecord(BaseModel):
    """Immutable classifier evidence bound to one canonical acquired frame."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    record_type: Literal["recording_probe"]
    observation_id: StrictStr
    investigation_id: StrictStr
    search_run_id: StrictStr
    channel_id: StrictInt = Field(gt=0)
    classification_operation_id: StrictStr
    baseline_observation_id: StrictStr
    canonical_frame_id: StrictStr
    primary_probe_request_id: StrictStr
    primary_requested_time_utc: CanonicalUtc
    classifier_policy_version: StrictStr = Field(min_length=1, max_length=128)
    state: ClassificationOutcome
    reason_code: VisualReason | None
    classifier_evidence: RawComparison
    published_at_utc: CanonicalFractionalUtc

    @field_serializer("published_at_utc")
    def serialize_published_at(self, value: datetime) -> str:
        """Serialize the administrative timestamp in canonical UTC form."""
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    @model_validator(mode="after")
    def validate_record(self) -> RecordingProbeObservationRecord:
        """Validate the closed observation identity and evidence shape."""
        if (
            not is_observation_id(self.observation_id)
            or not is_classification_operation_id(self.classification_operation_id)
            or not is_baseline_id(self.baseline_observation_id)
        ):
            raise ValueError
        expected_id = observation_id_for(
            investigation_id=self.investigation_id,
            search_run_id=self.search_run_id,
            channel_id=self.channel_id,
            baseline_observation_id=self.baseline_observation_id,
            canonical_frame_id=self.canonical_frame_id,
            classifier_policy_version=self.classifier_policy_version,
        )
        if self.observation_id != expected_id:
            raise ValueError
        _ = ClassificationResult(
            outcome=self.state,
            reason_code=self.reason_code,
            comparison=self.classifier_evidence,
        )
        return self


class TargetAliasRecord(BaseModel):
    """Immutable request-to-observation relationship that is not new evidence."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    record_type: Literal["target_alias"]
    alias_id: StrictStr
    investigation_id: StrictStr
    search_run_id: StrictStr
    channel_id: StrictInt = Field(gt=0)
    probe_request_id: StrictStr
    requested_time_utc: CanonicalUtc
    canonical_observation_id: StrictStr
    reason_code: Literal["same_decoded_frame"]
    published_at_utc: CanonicalFractionalUtc

    @field_serializer("published_at_utc")
    def serialize_published_at(self, value: datetime) -> str:
        """Serialize the administrative timestamp in canonical UTC form."""
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    @model_validator(mode="after")
    def validate_record(self) -> TargetAliasRecord:
        """Validate the closed alias identity shape."""
        expected = alias_id_for(
            self.search_run_id, self.probe_request_id, self.canonical_observation_id
        )
        if not is_alias_id(self.alias_id) or self.alias_id != expected:
            raise ValueError
        if not is_observation_id(self.canonical_observation_id):
            raise ValueError
        return self


ChildRecord = (
    ConfirmedReferenceBaselineRecord
    | ClassificationOperationRecord
    | RecordingProbeObservationRecord
    | TargetAliasRecord
)
