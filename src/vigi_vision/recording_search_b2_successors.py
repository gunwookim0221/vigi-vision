"""Validated append-only schema-3 manifest successors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
from vigi_vision.recording_search_models import (
    RecordingSearchState,
    RecordingSearchTransitionError,
)

if TYPE_CHECKING:
    from datetime import datetime

    from vigi_vision.recording_search_b2_records import (
        ClassificationOperationRecord,
        RecordingProbeObservationRecord,
        TargetAliasRecord,
    )


@dataclass(frozen=True, slots=True)
class Schema3LifecycleUpdate:
    """The only mutable administrative fields in a schema-3 successor."""

    target: RecordingSearchState
    completed_at_utc: datetime
    failure_reason: str


def lifecycle_successor(
    manifest: RecordingSearchManifestV3,
    update: Schema3LifecycleUpdate,
) -> RecordingSearchManifestV3:
    """Create a terminal successor while preserving every evidence field."""
    if manifest.state != "RUNNING":
        raise RecordingSearchTransitionError
    match update.target:
        case RecordingSearchState.FAILED:
            if update.failure_reason not in {"artifact_failure", "unexpected_error"}:
                raise RecordingSearchTransitionError
        case RecordingSearchState.INTERRUPTED:
            if update.failure_reason != "process_lock_released":
                raise RecordingSearchTransitionError
        case (
            RecordingSearchState.PENDING
            | RecordingSearchState.RUNNING
            | RecordingSearchState.FOUND
            | RecordingSearchState.NOT_FOUND
            | RecordingSearchState.INDETERMINATE
        ):
            raise RecordingSearchTransitionError
    return RecordingSearchManifestV3.model_validate(
        manifest.model_copy(
            update={
                "state": update.target.value,
                "completed_at_utc": update.completed_at_utc,
                "failure_reason": update.failure_reason,
            }
        ).model_dump(mode="python"),
        strict=True,
    )


def append_classification_successor(
    manifest: RecordingSearchManifestV3,
    operation: ClassificationOperationRecord,
    observation: RecordingProbeObservationRecord,
    aliases: tuple[TargetAliasRecord, ...] = (),
) -> RecordingSearchManifestV3:
    """Append one new operation and canonical observation without rewriting prior evidence."""
    if manifest.state != "RUNNING":
        raise ValueError
    if (
        operation.investigation_id != manifest.investigation_id
        or operation.search_run_id != manifest.search_run_id
        or operation.baseline_observation_id != manifest.baseline_observation_id
        or operation.classifier_policy_version != manifest.policy.classifier_policy_version
        or observation.investigation_id != manifest.investigation_id
        or observation.search_run_id != manifest.search_run_id
        or observation.baseline_observation_id != manifest.baseline_observation_id
        or observation.classification_operation_id != operation.classification_operation_id
        or observation.primary_probe_request_id != operation.probe_request_id
        or observation.canonical_frame_id != operation.canonical_frame_id
        or observation.classifier_policy_version != manifest.policy.classifier_policy_version
        or operation.classification_operation_id in manifest.classification_operation_ids
        or observation.observation_id in manifest.canonical_observation_ids
    ):
        raise ValueError
    _validate_aliases(manifest, observation, aliases)
    return RecordingSearchManifestV3.model_validate(
        manifest.model_copy(
            update={
                "classification_operation_ids": (
                    *manifest.classification_operation_ids,
                    operation.classification_operation_id,
                ),
                "canonical_observation_ids": (
                    *manifest.canonical_observation_ids,
                    observation.observation_id,
                ),
                "target_alias_ids": (
                    *manifest.target_alias_ids,
                    *(alias.alias_id for alias in aliases),
                ),
            }
        ).model_dump(mode="python"),
        strict=True,
    )


def append_alias_successor(
    manifest: RecordingSearchManifestV3, alias: TargetAliasRecord
) -> RecordingSearchManifestV3:
    """Append one request alias while preserving the canonical evidence indexes."""
    if (
        manifest.state != "RUNNING"
        or alias.investigation_id != manifest.investigation_id
        or alias.search_run_id != manifest.search_run_id
        or alias.channel_id != manifest.confirmation.channel_id
        or alias.canonical_observation_id not in manifest.canonical_observation_ids
        or alias.alias_id in manifest.target_alias_ids
    ):
        raise ValueError
    return RecordingSearchManifestV3.model_validate(
        manifest.model_copy(
            update={"target_alias_ids": (*manifest.target_alias_ids, alias.alias_id)}
        ).model_dump(mode="python"),
        strict=True,
    )


def _validate_aliases(
    manifest: RecordingSearchManifestV3,
    observation: RecordingProbeObservationRecord,
    aliases: tuple[TargetAliasRecord, ...],
) -> None:
    if len({alias.alias_id for alias in aliases}) != len(aliases):
        raise ValueError
    for alias in aliases:
        if (
            alias.investigation_id != manifest.investigation_id
            or alias.search_run_id != manifest.search_run_id
            or alias.channel_id != manifest.confirmation.channel_id
            or alias.canonical_observation_id != observation.observation_id
            or alias.probe_request_id == observation.primary_probe_request_id
            or alias.alias_id in manifest.target_alias_ids
        ):
            raise ValueError
