"""Closed Phase 7B authoritative classification outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, final

from typing_extensions import override

if TYPE_CHECKING:
    from vigi_vision.object_presence_values import ClassificationOutcome, VisualReason


@final
class ClassificationPublicationOutcome(str, Enum):
    """Closed durable publication disposition."""

    CREATED = "CREATED"
    REUSED = "REUSED"


@final
class ClassificationOperationalReason(str, Enum):
    """Closed non-visual failure categories for authoritative execution."""

    INVALID_CLASSIFICATION_REQUEST = "invalid_classification_request"
    PROBE_NOT_READY = "probe_not_ready"
    INVALID_BASELINE = "invalid_baseline"
    BASELINE_CORRUPT = "baseline_corrupt"
    FOREIGN_INPUT = "foreign_input"
    ACQUISITION_STATE_CORRUPT = "acquisition_state_corrupt"
    PROBE_ARTIFACT_CORRUPT = "probe_artifact_corrupt"
    INVALID_MEDIA_INPUT = "invalid_media_input"
    CLASSIFIER_UNAVAILABLE = "classifier_unavailable"
    CLASSIFIER_TIMEOUT = "classifier_timeout"
    CALLER_ABANDONED = "caller_abandoned"
    CLASSIFIER_EXECUTION_FAILED = "classifier_execution_failed"
    CLASSIFICATION_IN_PROGRESS = "classification_in_progress"
    INVALID_CLASSIFIER_OUTPUT = "invalid_classifier_output"
    STALE_RUN_OWNER = "stale_run_owner"
    LIFECYCLE_INVALID = "lifecycle_invalid"
    STALE_MANIFEST = "stale_manifest"
    AUTHORITATIVE_STATE_CHANGED = "authoritative_state_changed"
    PUBLICATION_CONFLICT = "publication_conflict"
    PERSISTENCE_FAILURE = "persistence_failure"


@dataclass(frozen=True, slots=True)
class PublishedClassificationResult:
    """Credential-free canonical result returned by the internal service."""

    outcome: ClassificationPublicationOutcome
    observation_id: str
    alias_id: str | None
    probe_request_id: str
    canonical_frame_id: str
    state: ClassificationOutcome
    reason_code: VisualReason | None


@final
class ClassificationOperationalError(RuntimeError):
    """Safe typed operational failure that never contains native diagnostics."""

    def __init__(self, reason: ClassificationOperationalReason) -> None:
        """Create an exception containing only its closed safe category."""
        super().__init__(reason.value)
        self.reason = reason

    @override
    def __str__(self) -> str:
        return self.reason.value
