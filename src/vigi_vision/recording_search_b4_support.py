"""Closed error translation and deterministic Phase 7B timestamp helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, NoReturn

from vigi_vision.recording_search_b3_models import ClassificationPreparationReason
from vigi_vision.recording_search_b4_models import (
    ClassificationOperationalError,
    ClassificationOperationalReason,
)

if TYPE_CHECKING:
    from collections.abc import Callable


_PREPARATION_REASONS: dict[ClassificationPreparationReason, ClassificationOperationalReason] = {
    ClassificationPreparationReason.INACTIVE_HANDLE: (
        ClassificationOperationalReason.STALE_RUN_OWNER
    ),
    ClassificationPreparationReason.OWNERSHIP_MISMATCH: (
        ClassificationOperationalReason.FOREIGN_INPUT
    ),
    ClassificationPreparationReason.LIFECYCLE_NOT_ELIGIBLE: (
        ClassificationOperationalReason.LIFECYCLE_INVALID
    ),
    ClassificationPreparationReason.STALE_MANIFEST: ClassificationOperationalReason.STALE_MANIFEST,
    ClassificationPreparationReason.BASELINE_CORRUPT: (
        ClassificationOperationalReason.BASELINE_CORRUPT
    ),
    ClassificationPreparationReason.INVALID_REQUEST_FRAME: (
        ClassificationOperationalReason.ACQUISITION_STATE_CORRUPT
    ),
    ClassificationPreparationReason.MISSING_PROVENANCE: (
        ClassificationOperationalReason.ACQUISITION_STATE_CORRUPT
    ),
    ClassificationPreparationReason.PROBE_ARTIFACT_CORRUPT: (
        ClassificationOperationalReason.PROBE_ARTIFACT_CORRUPT
    ),
    ClassificationPreparationReason.INVALID_MEDIA_INPUT: (
        ClassificationOperationalReason.INVALID_MEDIA_INPUT
    ),
    ClassificationPreparationReason.CLASSIFIER_UNAVAILABLE: (
        ClassificationOperationalReason.CLASSIFIER_UNAVAILABLE
    ),
    ClassificationPreparationReason.CLASSIFIER_EXECUTION_FAILED: (
        ClassificationOperationalReason.CLASSIFIER_EXECUTION_FAILED
    ),
    ClassificationPreparationReason.INVALID_CLASSIFIER_OUTPUT: (
        ClassificationOperationalReason.INVALID_CLASSIFIER_OUTPUT
    ),
    ClassificationPreparationReason.POLICY_IDENTITY_MISMATCH: (
        ClassificationOperationalReason.INVALID_CLASSIFICATION_REQUEST
    ),
    ClassificationPreparationReason.CANONICAL_DUPLICATE: (
        ClassificationOperationalReason.AUTHORITATIVE_STATE_CHANGED
    ),
}


def map_preparation_reason(
    reason: ClassificationPreparationReason,
) -> ClassificationOperationalReason:
    """Translate a preparation failure into the closed operational taxonomy."""
    return _PREPARATION_REASONS[reason]


def fractional_utc_now(clock: Callable[[], datetime]) -> datetime:
    """Return a strict UTC timestamp with the schema-required fractional part."""
    value = clock()
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        fail(ClassificationOperationalReason.PERSISTENCE_FAILURE)
    if value.microsecond == 0:
        value = value.replace(microsecond=1)
    return value.astimezone(timezone.utc)


def fail(reason: ClassificationOperationalReason) -> NoReturn:
    """Raise one credential-free closed operational failure."""
    raise ClassificationOperationalError(reason)
