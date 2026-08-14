"""Immutable Phase 7B-3 commands, snapshots, and safe operational outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, final

from typing_extensions import override

if TYPE_CHECKING:
    from vigi_vision.durable_io import CanonicalUtc
    from vigi_vision.investigation_confirmation_models import ConfirmationRoi
    from vigi_vision.object_presence_models import ClassificationResult, DecodedRgbImage
    from vigi_vision.object_presence_policy import ObjectPresenceDecisionPolicy
    from vigi_vision.object_presence_values import ClassificationOutcome, VisualReason
    from vigi_vision.recording_search_a2_models import (
        CanonicalFractionalUtc,
        SourceTimeBase,
    )


@final
class ClassificationPreparationReason(str, Enum):
    """Closed safe categories for preparation failures."""

    INACTIVE_HANDLE = "inactive_handle"
    OWNERSHIP_MISMATCH = "ownership_mismatch"
    LIFECYCLE_NOT_ELIGIBLE = "lifecycle_not_eligible"
    STALE_MANIFEST = "stale_manifest"
    BASELINE_CORRUPT = "baseline_corrupt"
    INVALID_REQUEST_FRAME = "invalid_request_frame"
    MISSING_PROVENANCE = "missing_provenance"
    PROBE_ARTIFACT_CORRUPT = "probe_artifact_corrupt"
    INVALID_MEDIA_INPUT = "invalid_media_input"
    CLASSIFIER_UNAVAILABLE = "classifier_unavailable"
    CLASSIFIER_EXECUTION_FAILED = "classifier_execution_failed"
    INVALID_CLASSIFIER_OUTPUT = "invalid_classifier_output"
    POLICY_IDENTITY_MISMATCH = "policy_identity_mismatch"
    CANONICAL_DUPLICATE = "canonical_duplicate"


class ClassificationPreparationError(RuntimeError):
    """Fixed operational failure without native diagnostics."""

    def __init__(self, reason: ClassificationPreparationReason) -> None:
        """Store only the closed operational category."""
        super().__init__(reason.value)
        self.reason: ClassificationPreparationReason = reason

    @override
    def __str__(self) -> str:
        """Return the stable operational category."""
        return self.reason.value


@dataclass(frozen=True, slots=True)
class ClassifyRecordingProbeRequest:
    """Semantic lookup keys; authority comes only from the supplied handle."""

    investigation_id: str
    search_run_id: str
    probe_request_id: str


ClassificationCommand = ClassifyRecordingProbeRequest


@dataclass(frozen=True, slots=True)
class ProbeProvenanceSnapshot:
    """Frame provenance retained without a reopenable artifact path."""

    canonical_frame_id: str
    probe_request_id: str
    requested_time_utc: CanonicalUtc
    decoded_frame_utc: CanonicalFractionalUtc
    source_segment_id: str
    segment_start_utc: CanonicalUtc
    segment_end_utc: CanonicalUtc
    physical_replay_origin_utc: CanonicalFractionalUtc
    source_pts: int
    source_time_base: SourceTimeBase
    decoded_pts: int
    replay_time_base: SourceTimeBase
    decoded_ordinal: int
    source_width: int
    source_height: int
    jpeg_sha256: str
    jpeg_size_bytes: int


@dataclass(frozen=True, slots=True)
class ClassificationSnapshot:
    """Complete immutable in-memory input for later bounded execution."""

    investigation_id: str
    search_run_id: str
    channel_id: int
    manifest_identity: str
    baseline_observation_id: str
    reference_frame_resource_id: str
    baseline_jpeg_bytes: bytes = field(repr=False)
    probe_jpeg_bytes: bytes = field(repr=False)
    baseline_jpeg_sha256: str
    baseline_jpeg_size_bytes: int
    probe_jpeg_sha256: str
    probe_jpeg_size_bytes: int
    source_width: int
    source_height: int
    confirmed_roi: ConfirmationRoi
    probe: ProbeProvenanceSnapshot
    baseline_image: DecodedRgbImage = field(repr=False)
    probe_image: DecodedRgbImage = field(repr=False)
    policy: ObjectPresenceDecisionPolicy
    classifier_identity: str
    preprocessing_identity: str
    checkpoint_sha256: str
    proposed_observation_id: str


@dataclass(frozen=True, slots=True)
class NonAuthoritativeClassificationResult:
    """In-memory visual result that is not admitted evidence or publication."""

    snapshot: ClassificationSnapshot = field(repr=False)
    classification: ClassificationResult


@dataclass(frozen=True, slots=True)
class CanonicalDuplicateResult:
    """Existing canonical observation detected before a new classification."""

    observation_id: str
    canonical_frame_id: str
    probe_request_id: str
    state: ClassificationOutcome
    reason_code: VisualReason | None
    alias_id: str | None
    alias_required: bool


ClassificationPreparationResult = NonAuthoritativeClassificationResult | CanonicalDuplicateResult
