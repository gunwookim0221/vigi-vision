"""Authoritative Phase 7B record construction from validated snapshots."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vigi_vision.recording_search_b2_identity import alias_id_for
from vigi_vision.recording_search_b2_records import (
    ClassificationOperationRecord,
    ConfirmedReferenceBaselineRecord,
    RecordingProbeObservationRecord,
    TargetAliasRecord,
)
from vigi_vision.recording_search_b4_models import (
    ClassificationPublicationOutcome,
    PublishedClassificationResult,
)

if TYPE_CHECKING:
    from datetime import datetime

    from vigi_vision.object_presence_evidence import ClassificationResult
    from vigi_vision.recording_search_a2_models import (
        ProbeFrameRequestRecord,
        RecordingSearchManifestV2,
    )
    from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
    from vigi_vision.recording_search_b3_models import (
        CanonicalDuplicateResult,
        ClassificationSnapshot,
    )


def build_baseline_record(
    snapshot: ClassificationSnapshot,
    manifest: RecordingSearchManifestV2 | RecordingSearchManifestV3,
    published_at: datetime,
) -> ConfirmedReferenceBaselineRecord:
    """Build the immutable confirmed-reference baseline child record."""
    confirmation = manifest.confirmation
    return ConfirmedReferenceBaselineRecord(
        record_type="confirmed_reference_baseline",
        observation_id=snapshot.baseline_observation_id,
        investigation_id=snapshot.investigation_id,
        search_run_id=snapshot.search_run_id,
        channel_id=confirmation.channel_id,
        reference_frame_resource_id=confirmation.reference_frame_resource_id,
        reference_requested_time_utc=confirmation.reference_requested_time_utc,
        source_width=confirmation.source_width,
        source_height=confirmation.source_height,
        roi=confirmation.roi,
        jpeg_sha256=confirmation.jpeg_sha256,
        jpeg_size_bytes=confirmation.jpeg_size_bytes,
        timing_precision_status=confirmation.timing_precision_status,
        warnings=confirmation.warnings,
        state="PRESENT",
        reason_code="user_confirmed_reference",
        published_at_utc=published_at,
    )


def build_operation_record(
    snapshot: ClassificationSnapshot,
    operation_id: str,
    admitted_at: datetime,
) -> ClassificationOperationRecord:
    """Build the admitted classification-operation child record."""
    return ClassificationOperationRecord(
        record_type="classification_operation",
        classification_operation_id=operation_id,
        investigation_id=snapshot.investigation_id,
        search_run_id=snapshot.search_run_id,
        operation_kind="recording_probe_classification_v1",
        state="ADMITTED",
        probe_request_id=snapshot.probe.probe_request_id,
        canonical_frame_id=snapshot.probe.canonical_frame_id,
        baseline_observation_id=snapshot.baseline_observation_id,
        classifier_policy_version=snapshot.policy.classifier_policy_version,
        admitted_at_utc=admitted_at,
    )


def build_observation_record(
    snapshot: ClassificationSnapshot,
    operation: ClassificationOperationRecord,
    result: ClassificationResult,
    published_at: datetime,
) -> RecordingProbeObservationRecord:
    """Build one durable visual observation from a timely worker result."""
    return RecordingProbeObservationRecord(
        record_type="recording_probe",
        observation_id=snapshot.proposed_observation_id,
        investigation_id=snapshot.investigation_id,
        search_run_id=snapshot.search_run_id,
        channel_id=snapshot.channel_id,
        classification_operation_id=operation.classification_operation_id,
        baseline_observation_id=snapshot.baseline_observation_id,
        canonical_frame_id=snapshot.probe.canonical_frame_id,
        primary_probe_request_id=snapshot.probe.probe_request_id,
        primary_requested_time_utc=snapshot.probe.requested_time_utc,
        classifier_policy_version=snapshot.policy.classifier_policy_version,
        state=result.outcome,
        reason_code=result.reason_code,
        classifier_evidence=result.comparison,
        published_at_utc=published_at,
    )


def build_alias_record(
    duplicate: CanonicalDuplicateResult,
    request: ProbeFrameRequestRecord,
    published_at: datetime,
) -> TargetAliasRecord:
    """Build a target alias for a request resolving to a known physical frame."""
    return TargetAliasRecord(
        record_type="target_alias",
        alias_id=alias_id_for(
            request.search_run_id,
            request.probe_request_id,
            duplicate.observation_id,
        ),
        investigation_id=request.investigation_id,
        search_run_id=request.search_run_id,
        channel_id=request.channel_id,
        probe_request_id=request.probe_request_id,
        requested_time_utc=request.requested_time_utc,
        canonical_observation_id=duplicate.observation_id,
        reason_code="same_decoded_frame",
        published_at_utc=published_at,
    )


def result_from_duplicate(
    duplicate: CanonicalDuplicateResult, alias_id: str | None = None
) -> PublishedClassificationResult:
    """Return a canonical reuse result without exposing repository details."""
    return PublishedClassificationResult(
        outcome=ClassificationPublicationOutcome.REUSED,
        observation_id=duplicate.observation_id,
        alias_id=duplicate.alias_id if alias_id is None else alias_id,
        probe_request_id=duplicate.probe_request_id,
        canonical_frame_id=duplicate.canonical_frame_id,
        state=duplicate.state,
        reason_code=duplicate.reason_code,
    )


def result_from_observation(
    observation: RecordingProbeObservationRecord,
    outcome: ClassificationPublicationOutcome,
) -> PublishedClassificationResult:
    """Return a canonical result for a newly durable observation."""
    return PublishedClassificationResult(
        outcome=outcome,
        observation_id=observation.observation_id,
        alias_id=None,
        probe_request_id=observation.primary_probe_request_id,
        canonical_frame_id=observation.canonical_frame_id,
        state=observation.state,
        reason_code=observation.reason_code,
    )
