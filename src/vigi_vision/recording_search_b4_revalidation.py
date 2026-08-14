"""Mutex-scoped authoritative snapshot revalidation without media reopening."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Protocol

from vigi_vision.recording_search_a2_models import (
    ProbeRequestStatus,
    RecordingSearchManifestV2,
)
from vigi_vision.recording_search_a2_repository import (
    read_schema2_children_for_probe_admission,
)
from vigi_vision.recording_search_b2_identity import (
    baseline_observation_id_for,
    observation_id_for,
)
from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
from vigi_vision.recording_search_b2_policy import RecordingSearchPolicyV3
from vigi_vision.recording_search_b3_models import (
    ClassificationSnapshot,
    ClassifyRecordingProbeRequest,
    ProbeProvenanceSnapshot,
)
from vigi_vision.recording_search_models import RecordingSearchState

if TYPE_CHECKING:
    from pathlib import Path


class RevalidationRepository(Protocol):
    """Read-only record surface required while holding the run mutation mutex."""

    @property
    def root(self) -> Path:
        """Return the configured recording-search artifact root."""
        ...

    def run_path(self, investigation_id: str, search_run_id: str) -> Path:
        """Return the confined path for one recording-search run."""
        ...


def snapshot_matches_authoritative_state(  # noqa: PLR0911
    repository: RevalidationRepository,
    handle_baseline_bytes: bytes,
    current: RecordingSearchManifestV2 | RecordingSearchManifestV3,
    request: ClassifyRecordingProbeRequest,
    snapshot: ClassificationSnapshot,
) -> bool:
    """Revalidate every publication binding from current strict metadata."""
    state = (
        current.state.value if isinstance(current.state, RecordingSearchState) else current.state
    )
    if (
        state != "RUNNING"
        or current.investigation_id != request.investigation_id
        or current.search_run_id != request.search_run_id
        or hashlib.sha256(current.canonical_json().encode("utf-8")).hexdigest()
        != snapshot.manifest_identity
        or handle_baseline_bytes != snapshot.baseline_jpeg_bytes
        or hashlib.sha256(handle_baseline_bytes).hexdigest() != snapshot.baseline_jpeg_sha256
        or len(handle_baseline_bytes) != snapshot.baseline_jpeg_size_bytes
    ):
        return False
    confirmation = current.confirmation
    expected_baseline = baseline_observation_id_for(
        investigation_id=current.investigation_id,
        search_run_id=current.search_run_id,
        channel_id=confirmation.channel_id,
        reference_frame_resource_id=confirmation.reference_frame_resource_id,
        reference_requested_time_utc=confirmation.reference_requested_time_utc,
        source_width=confirmation.source_width,
        source_height=confirmation.source_height,
        roi=confirmation.roi.model_dump(mode="json"),
        jpeg_sha256=confirmation.jpeg_sha256,
        jpeg_size_bytes=confirmation.jpeg_size_bytes,
    )
    if (
        snapshot.channel_id != confirmation.channel_id
        or snapshot.reference_frame_resource_id != confirmation.reference_frame_resource_id
        or snapshot.source_width != confirmation.source_width
        or snapshot.source_height != confirmation.source_height
        or snapshot.confirmed_roi != confirmation.roi
        or snapshot.baseline_jpeg_sha256 != confirmation.jpeg_sha256
        or snapshot.baseline_jpeg_size_bytes != confirmation.jpeg_size_bytes
        or snapshot.baseline_observation_id != expected_baseline
    ):
        return False
    acquisition = (
        current.as_schema2() if isinstance(current, RecordingSearchManifestV3) else current
    )
    expected_policy = RecordingSearchPolicyV3.from_policies(acquisition.policy, snapshot.policy)
    if isinstance(current, RecordingSearchManifestV3) and (
        current.policy != expected_policy
        or current.baseline_observation_id != snapshot.baseline_observation_id
    ):
        return False
    if (
        snapshot.classifier_identity != snapshot.policy.classifier_policy_version
        or snapshot.preprocessing_identity != snapshot.policy.classifier_preprocessing_version
        or snapshot.checkpoint_sha256 != snapshot.policy.checkpoint_sha256
        or acquisition.policy.classifier_policy_version != snapshot.classifier_identity
        or acquisition.policy.checkpoint_sha256 != snapshot.checkpoint_sha256
    ):
        return False
    _, frames, requests = read_schema2_children_for_probe_admission(
        repository.root,
        repository.run_path(current.investigation_id, current.search_run_id),
        acquisition,
    )
    request_record = requests.get(request.probe_request_id)
    if (
        request_record is None
        or request_record.status is not ProbeRequestStatus.SUCCEEDED
        or request_record.canonical_frame_id is None
    ):
        return False
    frame = frames.get(request_record.canonical_frame_id)
    if frame is None:
        return False
    provenance = ProbeProvenanceSnapshot(
        canonical_frame_id=frame.canonical_frame_id,
        probe_request_id=request_record.probe_request_id,
        requested_time_utc=request_record.requested_time_utc,
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
        jpeg_sha256=frame.jpeg_sha256,
        jpeg_size_bytes=frame.jpeg_size_bytes,
    )
    expected_observation = observation_id_for(
        investigation_id=current.investigation_id,
        search_run_id=current.search_run_id,
        channel_id=confirmation.channel_id,
        baseline_observation_id=expected_baseline,
        canonical_frame_id=frame.canonical_frame_id,
        classifier_policy_version=snapshot.policy.classifier_policy_version,
    )
    return (
        provenance == snapshot.probe
        and snapshot.probe_jpeg_sha256 == frame.jpeg_sha256
        and snapshot.probe_jpeg_size_bytes == frame.jpeg_size_bytes
        and hashlib.sha256(snapshot.probe_jpeg_bytes).hexdigest() == frame.jpeg_sha256
        and len(snapshot.probe_jpeg_bytes) == frame.jpeg_size_bytes
        and snapshot.proposed_observation_id == expected_observation
    )
