"""Deterministic immutable classification snapshot construction."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from vigi_vision.recording_search_b2_identity import (
    baseline_observation_id_for,
    observation_id_for,
)
from vigi_vision.recording_search_b3_models import (
    ClassificationSnapshot,
    ProbeProvenanceSnapshot,
)

if TYPE_CHECKING:
    from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
    from vigi_vision.recording_search_a2_models import (
        RecordingSearchManifestV2,
    )
    from vigi_vision.recording_search_a2_service import AdmittedProbeFrame
    from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
    from vigi_vision.recording_search_b3_media import DecodedMedia
    from vigi_vision.recording_search_b3_models import ClassifyRecordingProbeRequest


def build_classification_snapshot(  # noqa: PLR0913
    manifest: RecordingSearchManifestV2 | RecordingSearchManifestV3,
    request: ClassifyRecordingProbeRequest,
    probe: AdmittedProbeFrame,
    baseline_bytes: bytes,
    baseline_media: DecodedMedia,
    probe_media: DecodedMedia,
    policy: ObjectPresenceDecisionPolicy,
) -> ClassificationSnapshot:
    """Bind every authoritative byte, provenance, model, and policy identity."""
    confirmation = manifest.confirmation
    baseline_id = baseline_observation_id_for(
        investigation_id=manifest.investigation_id,
        search_run_id=manifest.search_run_id,
        channel_id=confirmation.channel_id,
        reference_frame_resource_id=confirmation.reference_frame_resource_id,
        reference_requested_time_utc=confirmation.reference_requested_time_utc,
        source_width=confirmation.source_width,
        source_height=confirmation.source_height,
        roi=confirmation.roi.model_dump(mode="json"),
        jpeg_sha256=confirmation.jpeg_sha256,
        jpeg_size_bytes=confirmation.jpeg_size_bytes,
    )
    frame = probe.frame
    provenance = ProbeProvenanceSnapshot(
        canonical_frame_id=frame.canonical_frame_id,
        probe_request_id=request.probe_request_id,
        requested_time_utc=probe.request.requested_time_utc,
        decoded_frame_utc=frame.decoded_frame_utc,
        source_segment_id=frame.source_segment_id,
        segment_start_utc=frame.segment_start_utc,
        segment_end_utc=frame.segment_end_utc,
        physical_replay_origin_utc=frame.physical_replay_origin_utc,
        source_pts=frame.source_pts,
        source_time_base=frame.source_time_base,
        decoded_pts=frame.decoded_pts,
        replay_time_base=frame.replay_time_base,
        decoded_ordinal=frame.decoded_ordinal,
        source_width=frame.source_width,
        source_height=frame.source_height,
        jpeg_sha256=probe.jpeg_sha256,
        jpeg_size_bytes=probe.jpeg_size_bytes,
    )
    proposed = observation_id_for(
        investigation_id=manifest.investigation_id,
        search_run_id=manifest.search_run_id,
        channel_id=confirmation.channel_id,
        baseline_observation_id=baseline_id,
        canonical_frame_id=frame.canonical_frame_id,
        classifier_policy_version=policy.classifier_policy_version,
    )
    return ClassificationSnapshot(
        investigation_id=manifest.investigation_id,
        search_run_id=manifest.search_run_id,
        channel_id=confirmation.channel_id,
        manifest_identity=hashlib.sha256(manifest.canonical_json().encode("utf-8")).hexdigest(),
        baseline_observation_id=baseline_id,
        reference_frame_resource_id=confirmation.reference_frame_resource_id,
        baseline_jpeg_bytes=baseline_bytes,
        probe_jpeg_bytes=probe.jpeg_bytes,
        baseline_jpeg_sha256=baseline_media.integrity.sha256,
        baseline_jpeg_size_bytes=baseline_media.integrity.size_bytes,
        probe_jpeg_sha256=probe_media.integrity.sha256,
        probe_jpeg_size_bytes=probe_media.integrity.size_bytes,
        source_width=confirmation.source_width,
        source_height=confirmation.source_height,
        confirmed_roi=confirmation.roi,
        probe=provenance,
        baseline_image=baseline_media.image,
        probe_image=probe_media.image,
        policy=policy,
        classifier_identity=policy.classifier_policy_version,
        preprocessing_identity=policy.classifier_preprocessing_version,
        checkpoint_sha256=policy.checkpoint_sha256,
        proposed_observation_id=proposed,
    )
