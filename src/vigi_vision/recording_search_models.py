"""Strict recording-search request, policy, and manifest models."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from enum import Enum
from typing import TYPE_CHECKING, ClassVar, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from vigi_vision.durable_io import CanonicalUtc  # noqa: TC001 - runtime Pydantic field type.
from vigi_vision.investigation_confirmation_models import (  # noqa: TC001 - runtime field type.
    ConfirmationRoi,
)

if TYPE_CHECKING:
    from datetime import datetime

RECORDING_SEARCH_SCHEMA_VERSION: Final = 1
RECORDING_SEARCH_POLICY_VERSION: Final = "recording-search-mvp-v1"
RECORDING_SEARCH_ACQUISITION_POLICY_VERSION: Final = "phase7-batch-decoder-v1"
RECORDING_SEARCH_CLASSIFIER_POLICY_VERSION: Final = "efficient-sam-ti-roi-ncc-v1"
RECORDING_SEARCH_CHECKPOINT_SHA256: Final = (
    "dff858b19600a46461cbb7de98f796b23a7a888d9f5e34c0b033f7d6eb9e4e6a"
)
_INVESTIGATION_ID_PATTERN: Final = re.compile(
    r"^object-disappearance-(?:v3-)?ch[1-9][0-9]*-[0-9]{8}T[0-9]{6}Z$"
)
_RUN_ID_PATTERN: Final = re.compile(r"^search-run-[0-9a-f]{8,64}$")


class RecordingSearchError(RuntimeError):
    """Base recording-search error."""


class RecordingSearchBaselineError(RecordingSearchError):
    """Baseline validation failed."""


class ReconfirmationRequiredError(RecordingSearchBaselineError):
    """A schema 2 confirmation requires explicit reconfirmation."""


class RecordingSearchNotFoundError(RecordingSearchError):
    """A requested run is absent."""


class RecordingSearchManifestCorruptError(RecordingSearchError):
    """A persisted manifest is invalid."""


class RecordingSearchArtifactError(RecordingSearchError):
    """Run storage failed safely."""


class RecordingSearchLockError(RecordingSearchError):
    """The local lock failed safely."""


class RecordingSearchTransitionError(RecordingSearchError):
    """A lifecycle transition is invalid."""


class RecordingSearchTerminalConflictError(RecordingSearchError):
    """A different terminal proposal already committed."""


class RecordingSearchPublicationInProgressError(RecordingSearchError):
    """A terminal publication could not acquire the bounded run lock."""


class RecordingSearchTerminalReopenCategory(str, Enum):
    """Closed internal categories for strict terminal reopen failures."""

    MANIFEST_MISSING = "manifest_missing"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    MALFORMED_MANIFEST = "malformed_manifest"
    FOREIGN_OWNERSHIP = "foreign_ownership"
    MISSING_RECORD = "missing_record"
    MALFORMED_RECORD = "malformed_record"
    MISSING_JPEG = "missing_jpeg"
    JPEG_PATH_VIOLATION = "jpeg_path_violation"
    JPEG_INTEGRITY_MISMATCH = "jpeg_integrity_mismatch"
    EVIDENCE_OWNERSHIP_MISMATCH = "evidence_ownership_mismatch"
    IDENTITY_MISMATCH = "identity_mismatch"
    SUPPORT_ORDER_VIOLATION = "support_order_violation"
    TERMINAL_CONTRADICTION = "terminal_contradiction"
    POST_TERMINAL_EVIDENCE = "post_terminal_evidence"
    VALIDATOR_FAILURE = "validator_failure"
    READ_FAILURE = "read_failure"


class RecordingSearchTerminalReopenError(RecordingSearchError):
    """A Schema 4 terminal state failed closed during strict reopen."""

    def __init__(self, category: RecordingSearchTerminalReopenCategory) -> None:
        """Retain only the closed safe category for internal translation."""
        super().__init__()
        self.category: RecordingSearchTerminalReopenCategory = category


class RecordingSearchOutcome(str, Enum):
    """Outcome of a start request."""

    STARTED = "started"
    ALREADY_RUNNING = "already_running"


class RecordingSearchState(str, Enum):
    """Persisted run state."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INDETERMINATE = "INDETERMINATE"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class Phase8HandoffStatus(str, Enum):
    """Persisted Phase 8 handoff state."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"


class RecordingSearchRequest(BaseModel):
    """Caller-controlled search request."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    investigation_id: StrictStr = Field(min_length=1, max_length=128)
    search_end_time_text: StrictStr = Field(min_length=1, max_length=128)
    source_timezone: StrictStr = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_identity(self) -> RecordingSearchRequest:
        """Reject non-canonical investigation identities."""
        if _INVESTIGATION_ID_PATTERN.fullmatch(self.investigation_id) is None:
            raise ValueError
        return self


class RecordingSearchBaseline(BaseModel):
    """Server-owned schema 3 baseline facts."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    channel_id: StrictInt = Field(gt=0)
    reference_frame_resource_id: StrictStr = Field(min_length=1, max_length=192)
    anchor_time_utc: CanonicalUtc
    reference_requested_time_utc: CanonicalUtc
    source_timezone: StrictStr = Field(min_length=1, max_length=64)
    source_width: StrictInt = Field(gt=0)
    source_height: StrictInt = Field(gt=0)
    roi: ConfirmationRoi
    jpeg_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    jpeg_size_bytes: StrictInt = Field(gt=0)
    candidate_offset_seconds: StrictInt = Field(ge=-300, le=300)
    generation_policy_version: StrictInt = Field(gt=0)
    frame_selection_policy: StrictStr = Field(min_length=1, max_length=128)
    estimated_source_time_utc: CanonicalUtc | None
    decoded_local_pts_seconds: StrictFloat | None
    timing_precision_status: StrictStr = Field(min_length=1, max_length=64)
    warnings: tuple[StrictStr, ...]


class RecordingSearchPolicy(BaseModel):
    """Versioned policy snapshot."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    search_start_utc: CanonicalUtc
    search_end_utc: CanonicalUtc
    maximum_requested_span_seconds: StrictInt = Field(gt=0)
    coarse_interval_seconds: StrictInt = Field(gt=0)
    binary_stop_resolution_seconds: StrictInt = Field(gt=0)
    absence_confirmation_frames: StrictInt = Field(gt=0)
    absence_cadence_seconds: StrictInt = Field(gt=0)
    maximum_consecutive_indeterminate_targets: StrictInt = Field(gt=0)
    acquisition_policy_version: StrictStr = Field(min_length=1, max_length=128)
    classifier_policy_version: StrictStr = Field(min_length=1, max_length=128)
    efficient_sam_source_commit: StrictStr = Field(pattern=r"^[0-9a-f]{40}$")
    checkpoint_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_rule: StrictStr = Field(min_length=1, max_length=128)
    maximum_roi_mask_coverage_ratio: StrictFloat = Field(gt=0, lt=1)
    minimum_roi_pixels: StrictInt = Field(gt=0)
    minimum_clipped_mask_pixels: StrictInt = Field(gt=0)
    present_mask_iou_minimum: StrictFloat = Field(ge=0, le=1)
    present_luma_ncc_minimum: StrictFloat = Field(ge=-1, le=1)
    absent_mask_iou_maximum: StrictFloat = Field(ge=0, le=1)
    absent_luma_ncc_maximum: StrictFloat = Field(ge=-1, le=1)
    policy_version: StrictStr = Field(min_length=1, max_length=128)


class RecordingSearchCandidateInterval(BaseModel):
    """Future candidate interval shape."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    last_present_observation_id: StrictStr = Field(min_length=1)
    last_present_requested_time_utc: CanonicalUtc
    first_absent_observation_id: StrictStr = Field(min_length=1)
    first_absent_requested_time_utc: CanonicalUtc
    absence_support_observation_ids: tuple[StrictStr, ...] = Field(min_length=1)


class RecordingSearchManifest(BaseModel):
    """Strict durable recording-search manifest."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    investigation_id: StrictStr
    search_run_id: StrictStr
    state: RecordingSearchState
    created_at_utc: CanonicalUtc
    started_at_utc: CanonicalUtc | None
    completed_at_utc: CanonicalUtc | None
    confirmation: RecordingSearchBaseline
    policy: RecordingSearchPolicy
    canonical_observation_ids: tuple[StrictStr, ...]
    target_alias_ids: tuple[StrictStr, ...]
    candidate_interval: RecordingSearchCandidateInterval | None
    failure_reason: StrictStr | None
    phase8_handoff_status: Phase8HandoffStatus
    phase8_failure_reason: StrictStr | None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> RecordingSearchManifest:
        """Reject invalid lifecycle field combinations."""
        if _INVESTIGATION_ID_PATTERN.fullmatch(self.investigation_id) is None:
            raise ValueError
        if _RUN_ID_PATTERN.fullmatch(self.search_run_id) is None:
            raise ValueError
        _validate_state_fields(self)
        _validate_manifest_times(self)
        _validate_handoff_fields(self)
        return self

    def canonical_json(self) -> str:
        """Serialize the manifest deterministically."""
        return (
            json.dumps(self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        )


def default_policy(start_utc: datetime, end_utc: datetime) -> RecordingSearchPolicy:
    """Build the documented MVP policy snapshot."""
    return RecordingSearchPolicy(
        search_start_utc=start_utc,
        search_end_utc=end_utc,
        maximum_requested_span_seconds=86400,
        coarse_interval_seconds=300,
        binary_stop_resolution_seconds=1,
        absence_confirmation_frames=3,
        absence_cadence_seconds=1,
        maximum_consecutive_indeterminate_targets=3,
        acquisition_policy_version=RECORDING_SEARCH_ACQUISITION_POLICY_VERSION,
        classifier_policy_version=RECORDING_SEARCH_CLASSIFIER_POLICY_VERSION,
        efficient_sam_source_commit="d525f622e6f640acf5a0fc37c7ca1f243da5bde0",
        checkpoint_sha256=RECORDING_SEARCH_CHECKPOINT_SHA256,
        prompt_rule="confirmed_roi_center_v1",
        maximum_roi_mask_coverage_ratio=0.95,
        minimum_roi_pixels=64,
        minimum_clipped_mask_pixels=64,
        present_mask_iou_minimum=0.5,
        present_luma_ncc_minimum=0.6,
        absent_mask_iou_maximum=0.1,
        absent_luma_ncc_maximum=0.2,
        policy_version=RECORDING_SEARCH_POLICY_VERSION,
    )


def is_recording_search_investigation_id(value: str) -> bool:
    """Return whether a value is a supported investigation identity."""
    return _INVESTIGATION_ID_PATTERN.fullmatch(value) is not None


def is_recording_search_run_id(value: str) -> bool:
    """Return whether a value is a generated run identity."""
    return _RUN_ID_PATTERN.fullmatch(value) is not None


_PHASE7A1_STATES: Final = frozenset(
    {
        RecordingSearchState.PENDING,
        RecordingSearchState.RUNNING,
        RecordingSearchState.FAILED,
        RecordingSearchState.INTERRUPTED,
    }
)
_PHASE7A1_FAILURE_REASONS: Final = frozenset({"baseline_validation_failed", "unexpected_error"})


def validate_phase7a1_manifest(manifest: RecordingSearchManifest) -> None:
    """Reject manifest fields that belong to later Phase 7 slices."""
    if manifest.canonical_observation_ids or manifest.target_alias_ids:
        raise ValueError
    if (
        manifest.candidate_interval is not None
        or manifest.phase8_handoff_status is not Phase8HandoffStatus.NOT_APPLICABLE
        or manifest.phase8_failure_reason is not None
    ):
        raise ValueError
    if manifest.state not in _PHASE7A1_STATES:
        raise ValueError
    if manifest.state in (RecordingSearchState.PENDING, RecordingSearchState.RUNNING):
        if manifest.failure_reason is not None:
            raise ValueError
    elif manifest.state is RecordingSearchState.FAILED:
        if manifest.failure_reason not in _PHASE7A1_FAILURE_REASONS:
            raise ValueError
    elif manifest.failure_reason != "process_lock_released":
        raise ValueError


def _validate_canonical_timestamp(value: datetime | None) -> None:
    if value is None:
        return
    if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond != 0:
        raise ValueError


def _validate_state_fields(manifest: RecordingSearchManifest) -> None:
    if manifest.state in (RecordingSearchState.PENDING, RecordingSearchState.RUNNING):
        if manifest.completed_at_utc is not None or manifest.failure_reason is not None:
            raise ValueError
        if manifest.state is RecordingSearchState.PENDING and manifest.started_at_utc is not None:
            raise ValueError
        if manifest.state is RecordingSearchState.RUNNING and manifest.started_at_utc is None:
            raise ValueError
    elif manifest.completed_at_utc is None:
        raise ValueError
    if manifest.state is RecordingSearchState.FOUND:
        if manifest.candidate_interval is None or manifest.failure_reason is not None:
            raise ValueError
    elif manifest.candidate_interval is not None:
        raise ValueError


def _validate_manifest_times(manifest: RecordingSearchManifest) -> None:
    _validate_canonical_timestamp(manifest.created_at_utc)
    _validate_canonical_timestamp(manifest.started_at_utc)
    _validate_canonical_timestamp(manifest.completed_at_utc)
    if manifest.started_at_utc is not None and manifest.started_at_utc < manifest.created_at_utc:
        raise ValueError
    if (
        manifest.completed_at_utc is not None
        and manifest.completed_at_utc < manifest.created_at_utc
    ):
        raise ValueError
    if (
        manifest.started_at_utc is not None
        and manifest.completed_at_utc is not None
        and manifest.completed_at_utc < manifest.started_at_utc
    ):
        raise ValueError


def _validate_handoff_fields(manifest: RecordingSearchManifest) -> None:
    if (
        manifest.state is RecordingSearchState.RUNNING
        and manifest.phase8_handoff_status is not Phase8HandoffStatus.NOT_APPLICABLE
    ):
        raise ValueError
    if (
        manifest.state is not RecordingSearchState.FOUND
        and manifest.phase8_handoff_status is Phase8HandoffStatus.READY
    ):
        raise ValueError
