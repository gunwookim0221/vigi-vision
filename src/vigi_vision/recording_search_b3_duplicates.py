"""Canonical schema-3 observation and alias lookup."""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

from vigi_vision.recording_search_a2_models import ProbeRequestStatus
from vigi_vision.recording_search_a2_repository import read_schema2_children
from vigi_vision.recording_search_b2_validation import read_schema3_children
from vigi_vision.recording_search_b3_models import (
    CanonicalDuplicateResult,
    ClassificationPreparationError,
    ClassificationPreparationReason,
)
from vigi_vision.recording_search_models import RecordingSearchError

if TYPE_CHECKING:
    from vigi_vision.recording_search_b2_models import RecordingSearchManifestV3
    from vigi_vision.recording_search_b3_contracts import ClassificationRepository
    from vigi_vision.recording_search_b3_models import ClassifyRecordingProbeRequest


def find_canonical_duplicate(
    repository: ClassificationRepository,
    manifest: RecordingSearchManifestV3,
    request: ClassifyRecordingProbeRequest,
) -> CanonicalDuplicateResult | None:
    """Resolve the canonical observation for an already classified physical frame."""
    try:
        _, _, observations, aliases = read_schema3_children(
            repository.root,
            repository.run_path(manifest.investigation_id, manifest.search_run_id),
            manifest,
        )
        _, frames, requests = read_schema2_children(
            repository.root,
            repository.run_path(manifest.investigation_id, manifest.search_run_id),
            manifest.as_schema2(),
        )
    except RecordingSearchError:
        _fail(ClassificationPreparationReason.STALE_MANIFEST)
    record = requests.get(request.probe_request_id)
    if (
        record is None
        or record.status is not ProbeRequestStatus.SUCCEEDED
        or record.canonical_frame_id is None
        or record.canonical_frame_id not in frames
    ):
        _fail(ClassificationPreparationReason.INVALID_REQUEST_FRAME)
    for alias in aliases.values():
        if alias.probe_request_id == record.probe_request_id:
            observation = observations.get(alias.canonical_observation_id)
            if observation is None:
                _fail(ClassificationPreparationReason.STALE_MANIFEST)
            return CanonicalDuplicateResult(
                observation_id=observation.observation_id,
                canonical_frame_id=record.canonical_frame_id,
                probe_request_id=record.probe_request_id,
                state=observation.state,
                reason_code=observation.reason_code,
                alias_id=alias.alias_id,
                alias_required=False,
            )
    for observation in observations.values():
        if observation.canonical_frame_id == record.canonical_frame_id:
            return CanonicalDuplicateResult(
                observation_id=observation.observation_id,
                canonical_frame_id=record.canonical_frame_id,
                probe_request_id=record.probe_request_id,
                state=observation.state,
                reason_code=observation.reason_code,
                alias_id=None,
                alias_required=(observation.primary_probe_request_id != record.probe_request_id),
            )
    return None


def _fail(reason: ClassificationPreparationReason) -> NoReturn:
    raise ClassificationPreparationError(reason)
