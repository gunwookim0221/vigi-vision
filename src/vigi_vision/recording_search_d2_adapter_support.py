"""Shared safe helpers for the pure D2-0 result adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_d2_enums import (
    D2EvidenceRole,
    OperationalStopReason,
    VisualStopReason,
)
from vigi_vision.recording_search_d2_identity import evidence_snapshot_digest

if TYPE_CHECKING:
    from vigi_vision.recording_search_d2_evidence import D2EvidenceReference, D2EvidenceSnapshot


def target_ids(snapshot: D2EvidenceSnapshot | None) -> tuple[str, ...]:
    """Return ordered unique target identities for an operational stop."""
    if snapshot is None:
        return ()
    values: list[str] = []
    for reference in snapshot.references:
        if reference.target_id is not None and reference.target_id not in values:
            values.append(reference.target_id)
    return tuple(values)


def digest(snapshot: D2EvidenceSnapshot | None) -> str | None:
    """Compute a digest only for a present, valid snapshot."""
    if snapshot is None:
        return None
    try:
        return evidence_snapshot_digest(snapshot)
    except (AttributeError, TypeError, ValueError):
        return None


def missing_snapshot_reason(snapshot: D2EvidenceSnapshot | None) -> OperationalStopReason:
    """Classify absent versus malformed authoritative snapshot input."""
    return (
        OperationalStopReason.INCOMPLETE_EVIDENCE
        if snapshot is None
        else OperationalStopReason.CORRUPT_PERSISTED_EVIDENCE
    )


def map_operational_reason(reason: str | None) -> OperationalStopReason:
    """Map known legacy reason strings to the closed operational union."""
    if reason is None:
        return OperationalStopReason.ADAPTER_UNKNOWN_RESULT
    return {
        "recording_unavailable": OperationalStopReason.RECORDING_COVERAGE_GAP,
        "acquisition_timeout": OperationalStopReason.TIMEOUT,
        "timeout": OperationalStopReason.TIMEOUT,
        "classification_failed": OperationalStopReason.CLASSIFICATION_FAILED,
        "decode_failed": OperationalStopReason.DECODE_FAILED,
        "acquisition_failed": OperationalStopReason.ACQUISITION_FAILED,
        "unexpected_error": OperationalStopReason.UNEXPECTED_ERROR,
        "narrowing_evidence_unusable": OperationalStopReason.UNEXPECTED_ERROR,
        "inactive_run_handle": OperationalStopReason.INACTIVE_AUTHORITY,
        "stale_authoritative_evidence": OperationalStopReason.STALE_AUTHORITY,
        "authoritative_evidence_invalid": OperationalStopReason.CORRUPT_PERSISTED_EVIDENCE,
        "interrupted": OperationalStopReason.INTERRUPTED,
    }.get(reason, OperationalStopReason.ADAPTER_UNKNOWN_RESULT)


def visual_refs(
    snapshot: D2EvidenceSnapshot,
    reason: VisualStopReason,
) -> tuple[D2EvidenceReference, ...]:
    """Select only role-appropriate visual references from a snapshot."""
    if reason is VisualStopReason.INSUFFICIENT_VISUAL_EVIDENCE:
        return tuple(
            reference
            for reference in snapshot.references
            if reference.classification is ClassificationOutcome.INDETERMINATE
        )
    if reason is VisualStopReason.NONMONOTONIC_VISUAL_EVIDENCE:
        return tuple(
            reference
            for reference in snapshot.references
            if reference.classification
            in {ClassificationOutcome.PRESENT, ClassificationOutcome.ABSENT}
        )
    return support_refs(snapshot)


def support_refs(snapshot: D2EvidenceSnapshot) -> tuple[D2EvidenceReference, ...]:
    """Select strictly indexed absence-support references."""
    return tuple(
        reference
        for reference in snapshot.references
        if reference.role is D2EvidenceRole.ABSENCE_SUPPORT
        and reference.classification
        in {ClassificationOutcome.ABSENT, ClassificationOutcome.INDETERMINATE}
    )


def present_grid(snapshot: D2EvidenceSnapshot) -> tuple[D2EvidenceReference, ...]:
    """Select a distinct canonical PRESENT coarse grid."""
    grid = tuple(
        reference
        for reference in snapshot.references
        if reference.role is D2EvidenceRole.COARSE_TARGET
        and reference.classification is ClassificationOutcome.PRESENT
        and reference.alias_id is None
    )
    if not grid or len({reference.canonical_frame_id for reference in grid}) != len(grid):
        return ()
    return grid
