"""Schema-3 manifest and successor validation around immutable child records."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, StrictStr, model_validator

from vigi_vision.durable_io import CanonicalUtc  # noqa: TC001 - Pydantic runtime field type.
from vigi_vision.recording_search_a2_models import (
    RecordingSearchManifestV2,
)
from vigi_vision.recording_search_b2_identity import (
    is_alias_id,
    is_baseline_id,
    is_classification_operation_id,
    is_observation_id,
)
from vigi_vision.recording_search_b2_policy import RecordingSearchPolicyV3  # noqa: TC001
from vigi_vision.recording_search_b2_records import (
    ChildRecord,
    ClassificationOperationRecord,
    ConfirmedReferenceBaselineRecord,
    RecordingProbeObservationRecord,
    TargetAliasRecord,
)
from vigi_vision.recording_search_models import RecordingSearchBaseline, RecordingSearchState


class RecordingSearchManifestV3(BaseModel):
    """Closed schema-3 manifest for the active Phase 7B persistence boundary."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[3]
    investigation_id: StrictStr
    search_run_id: StrictStr
    state: Literal["RUNNING", "FAILED", "INTERRUPTED"]
    created_at_utc: CanonicalUtc
    started_at_utc: CanonicalUtc
    completed_at_utc: CanonicalUtc | None
    confirmation: RecordingSearchBaseline
    policy: RecordingSearchPolicyV3
    acquisition_operation_ids: tuple[StrictStr, ...]
    probe_request_ids: tuple[StrictStr, ...]
    canonical_frame_ids: tuple[StrictStr, ...]
    baseline_observation_id: StrictStr
    classification_operation_ids: tuple[StrictStr, ...]
    canonical_observation_ids: tuple[StrictStr, ...]
    target_alias_ids: tuple[StrictStr, ...]
    failure_reason: StrictStr | None

    @model_validator(mode="after")
    def validate_manifest(self) -> RecordingSearchManifestV3:
        """Validate lifecycle fields and deterministic index shape."""
        _validate_manifest_times(self)
        if self.state == "RUNNING":
            if self.completed_at_utc is not None or self.failure_reason is not None:
                raise ValueError
        else:
            if self.completed_at_utc is None:
                raise ValueError
            if self.state == "FAILED" and self.failure_reason not in {
                "artifact_failure",
                "unexpected_error",
            }:
                raise ValueError
            if self.state == "INTERRUPTED" and self.failure_reason != "process_lock_released":
                raise ValueError
        _validate_indexes(self)
        return self

    def canonical_json(self) -> str:
        """Serialize this manifest using the durable repository convention."""
        return (
            json.dumps(self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        )

    def as_schema2(self) -> RecordingSearchManifestV2:
        """Return the preserved A2 view used by existing child validators."""
        return RecordingSearchManifestV2(
            schema_version=2,
            investigation_id=self.investigation_id,
            search_run_id=self.search_run_id,
            state=RecordingSearchState.RUNNING,
            created_at_utc=self.created_at_utc,
            started_at_utc=self.started_at_utc,
            completed_at_utc=None,
            confirmation=self.confirmation,
            policy=self.policy.to_acquisition_policy(),
            acquisition_operation_ids=self.acquisition_operation_ids,
            probe_request_ids=self.probe_request_ids,
            canonical_frame_ids=self.canonical_frame_ids,
            failure_reason=None,
        )

    def as_status_manifest(self) -> RecordingSearchManifestV2:
        """Project schema 3 onto the existing evidence-free public status shape."""
        return RecordingSearchManifestV2(
            schema_version=2,
            investigation_id=self.investigation_id,
            search_run_id=self.search_run_id,
            state=RecordingSearchState(self.state),
            created_at_utc=self.created_at_utc,
            started_at_utc=self.started_at_utc,
            completed_at_utc=self.completed_at_utc,
            confirmation=self.confirmation,
            policy=self.policy.to_acquisition_policy(),
            acquisition_operation_ids=self.acquisition_operation_ids,
            probe_request_ids=self.probe_request_ids,
            canonical_frame_ids=self.canonical_frame_ids,
            failure_reason=self.failure_reason,
        )


def build_schema3_successor(  # noqa: PLR0913
    manifest: RecordingSearchManifestV2,
    policy: RecordingSearchPolicyV3,
    baseline: ConfirmedReferenceBaselineRecord,
    operation: ClassificationOperationRecord,
    observation: RecordingProbeObservationRecord,
    aliases: tuple[TargetAliasRecord, ...] = (),
) -> RecordingSearchManifestV3:
    """Construct and validate one complete schema-3 publication successor."""
    if manifest.state is not RecordingSearchState.RUNNING:
        raise ValueError
    if manifest.started_at_utc is None:
        raise ValueError
    if (
        baseline.investigation_id != manifest.investigation_id
        or baseline.search_run_id != manifest.search_run_id
        or operation.investigation_id != manifest.investigation_id
        or operation.search_run_id != manifest.search_run_id
        or observation.investigation_id != manifest.investigation_id
        or observation.search_run_id != manifest.search_run_id
        or operation.baseline_observation_id != baseline.observation_id
        or observation.baseline_observation_id != baseline.observation_id
        or observation.classification_operation_id != operation.classification_operation_id
        or observation.classifier_policy_version != policy.classifier_policy_version
    ):
        raise ValueError
    confirmation = manifest.confirmation
    if (
        baseline.channel_id != confirmation.channel_id
        or baseline.reference_frame_resource_id != confirmation.reference_frame_resource_id
        or baseline.reference_requested_time_utc != confirmation.reference_requested_time_utc
        or baseline.source_width != confirmation.source_width
        or baseline.source_height != confirmation.source_height
        or baseline.roi != confirmation.roi
        or baseline.jpeg_sha256 != confirmation.jpeg_sha256
        or baseline.jpeg_size_bytes != confirmation.jpeg_size_bytes
        or baseline.timing_precision_status != confirmation.timing_precision_status
        or baseline.warnings != confirmation.warnings
    ):
        raise ValueError
    if observation.primary_probe_request_id != operation.probe_request_id:
        raise ValueError
    for alias in aliases:
        if (
            alias.investigation_id != manifest.investigation_id
            or alias.search_run_id != manifest.search_run_id
            or alias.canonical_observation_id != observation.observation_id
            or alias.probe_request_id == operation.probe_request_id
        ):
            raise ValueError
    return RecordingSearchManifestV3(
        schema_version=3,
        investigation_id=manifest.investigation_id,
        search_run_id=manifest.search_run_id,
        state="RUNNING",
        created_at_utc=manifest.created_at_utc,
        started_at_utc=manifest.started_at_utc,
        completed_at_utc=None,
        confirmation=manifest.confirmation,
        policy=policy,
        acquisition_operation_ids=manifest.acquisition_operation_ids,
        probe_request_ids=manifest.probe_request_ids,
        canonical_frame_ids=manifest.canonical_frame_ids,
        baseline_observation_id=baseline.observation_id,
        classification_operation_ids=(operation.classification_operation_id,),
        canonical_observation_ids=(observation.observation_id,),
        target_alias_ids=tuple(alias.alias_id for alias in aliases),
        failure_reason=None,
    )


def child_record_id(record: ChildRecord) -> str:
    """Return the immutable filename key for one schema-3 child."""
    match record:
        case ConfirmedReferenceBaselineRecord() | RecordingProbeObservationRecord():
            return record.observation_id
        case ClassificationOperationRecord():
            return record.classification_operation_id
        case TargetAliasRecord():
            return record.alias_id


def _validate_manifest_times(manifest: RecordingSearchManifestV3) -> None:
    if manifest.started_at_utc < manifest.created_at_utc:
        raise ValueError
    if (
        manifest.completed_at_utc is not None
        and manifest.completed_at_utc < manifest.started_at_utc
    ):
        raise ValueError
    for value in (manifest.created_at_utc, manifest.started_at_utc, manifest.completed_at_utc):
        if value is not None and (value.tzinfo is None or value.utcoffset() != timedelta(0)):
            raise ValueError


def _validate_indexes(manifest: RecordingSearchManifestV3) -> None:
    groups = (
        manifest.acquisition_operation_ids,
        manifest.probe_request_ids,
        manifest.canonical_frame_ids,
        manifest.classification_operation_ids,
        manifest.canonical_observation_ids,
        manifest.target_alias_ids,
    )
    if any(len(set(group)) != len(group) for group in groups):
        raise ValueError
    if not is_baseline_id(manifest.baseline_observation_id):
        raise ValueError
    if any(
        not is_classification_operation_id(value) for value in manifest.classification_operation_ids
    ):
        raise ValueError
    if any(not is_observation_id(value) for value in manifest.canonical_observation_ids):
        raise ValueError
    if any(not is_alias_id(value) for value in manifest.target_alias_ids):
        raise ValueError
