"""Pure adapter from the current C2 result to the closed D2 union."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vigi_vision.recording_search_c2_models import (
    CoarseCandidateBracket,
    CoarseInterpretationResult,
    CoarseInterpretationStatus,
)
from vigi_vision.recording_search_d2_adapter_support import (
    digest,
    map_operational_reason,
    missing_snapshot_reason,
    present_grid,
    support_refs,
    target_ids,
    visual_refs,
)
from vigi_vision.recording_search_d2_enums import OperationalStopReason, VisualStopReason
from vigi_vision.recording_search_d2_results import (
    C2BracketReady,
    C2NoCandidate,
    C2OperationalStop,
    C2Result,
    C2VisualInconclusive,
)

if TYPE_CHECKING:
    from vigi_vision.recording_search_d2_evidence import D2EvidenceSnapshot


def adapt_c2_result(
    result: CoarseInterpretationResult,
    snapshot: D2EvidenceSnapshot | None = None,
) -> C2Result:
    """Convert C2 output, refusing visual claims without strict evidence."""
    try:
        if type(result.status) is not CoarseInterpretationStatus or not _shape_is_valid(result):
            return C2OperationalStop(OperationalStopReason.ADAPTER_UNKNOWN_RESULT, ())
        adapted: C2Result
        status_value = result.status.value
        if status_value == CoarseInterpretationStatus.BRACKET_READY.value:
            adapted = _bracket(result, snapshot)
        elif status_value == CoarseInterpretationStatus.NO_CANDIDATE.value:
            adapted = _no_candidate(result, snapshot)
        elif status_value == CoarseInterpretationStatus.INCONCLUSIVE.value:
            adapted = _inconclusive(result.safe_reason, snapshot)
        elif status_value == CoarseInterpretationStatus.INTERRUPTED.value:
            adapted = _interrupted(result.safe_reason, snapshot)
        elif status_value == CoarseInterpretationStatus.INCOMPLETE.value:
            adapted = _incomplete(result.safe_reason, snapshot)
        elif status_value == CoarseInterpretationStatus.CORRUPT.value:
            adapted = _corrupt(result.safe_reason, snapshot)
        else:
            return C2OperationalStop(OperationalStopReason.ADAPTER_UNKNOWN_RESULT, ())
    except (AttributeError, TypeError, ValueError):
        return C2OperationalStop(OperationalStopReason.ADAPTER_UNKNOWN_RESULT, ())
    else:
        return adapted


def _shape_is_valid(result: CoarseInterpretationResult) -> bool:
    if result.status is CoarseInterpretationStatus.BRACKET_READY:
        return isinstance(result.bracket, CoarseCandidateBracket) and result.safe_reason is None
    return result.bracket is None and type(result.safe_reason) is str and bool(result.safe_reason)


def _bracket(result: CoarseInterpretationResult, snapshot: D2EvidenceSnapshot | None) -> C2Result:
    if not isinstance(result.bracket, CoarseCandidateBracket) or result.safe_reason is not None:
        return C2OperationalStop(OperationalStopReason.CORRUPT_PERSISTED_EVIDENCE, ())
    snapshot_digest = digest(snapshot)
    if snapshot_digest is None:
        return C2OperationalStop(missing_snapshot_reason(snapshot), ())
    return C2BracketReady(result.bracket, snapshot_digest)


def _no_candidate(
    result: CoarseInterpretationResult, snapshot: D2EvidenceSnapshot | None
) -> C2Result:
    if result.safe_reason != "no_supported_transition":
        return C2OperationalStop(OperationalStopReason.ADAPTER_UNKNOWN_RESULT, target_ids(snapshot))
    if snapshot is None:
        return C2OperationalStop(OperationalStopReason.INCOMPLETE_EVIDENCE, ())
    grid = present_grid(snapshot)
    snapshot_digest = digest(snapshot)
    if not grid or snapshot_digest is None:
        return C2OperationalStop(OperationalStopReason.INCOMPLETE_EVIDENCE, target_ids(snapshot))
    return C2NoCandidate(grid, snapshot_digest)


def _interrupted(reason: str | None, snapshot: D2EvidenceSnapshot | None) -> C2Result:
    mapped = (
        OperationalStopReason.INTERRUPTED
        if reason == "coarse_execution_interrupted"
        else OperationalStopReason.ADAPTER_UNKNOWN_RESULT
    )
    return C2OperationalStop(mapped, target_ids(snapshot))


def _incomplete(reason: str | None, snapshot: D2EvidenceSnapshot | None) -> C2Result:
    mapped = (
        OperationalStopReason.INTERRUPTED
        if reason == "coarse_execution_interrupted"
        else OperationalStopReason.INCOMPLETE_EVIDENCE
        if reason == "coarse_execution_incomplete"
        else OperationalStopReason.ADAPTER_UNKNOWN_RESULT
    )
    return C2OperationalStop(mapped, target_ids(snapshot))


def _corrupt(reason: str | None, snapshot: D2EvidenceSnapshot | None) -> C2Result:
    mapped = (
        OperationalStopReason.CORRUPT_PERSISTED_EVIDENCE
        if reason in {"coarse_plan_mismatch", "authoritative_evidence_invalid"}
        else OperationalStopReason.ADAPTER_UNKNOWN_RESULT
    )
    return C2OperationalStop(mapped, target_ids(snapshot))


def _inconclusive(  # noqa: PLR0911 - reason mapping is intentionally explicit
    reason: str | None, snapshot: D2EvidenceSnapshot | None
) -> C2Result:
    if reason == "nonmonotonic_visual_evidence":
        return _visual(VisualStopReason.NONMONOTONIC_VISUAL_EVIDENCE, snapshot)
    if reason == "insufficient_visual_evidence":
        return _visual(VisualStopReason.INSUFFICIENT_VISUAL_EVIDENCE, snapshot)
    if reason == "maximum_consecutive_unusable_targets":
        if snapshot is not None and support_refs(snapshot):
            snapshot_digest = digest(snapshot)
            if snapshot_digest is not None:
                return C2VisualInconclusive(
                    VisualStopReason.INSUFFICIENT_DISTINCT_VISUAL_SUPPORT,
                    support_refs(snapshot),
                    snapshot_digest,
                )
        return C2OperationalStop(OperationalStopReason.INCOMPLETE_EVIDENCE, target_ids(snapshot))
    if reason == "coarse_execution_interrupted":
        return C2OperationalStop(OperationalStopReason.INTERRUPTED, target_ids(snapshot))
    if reason == "missing_present_lower_bound":
        return C2OperationalStop(OperationalStopReason.INCOMPLETE_EVIDENCE, target_ids(snapshot))
    return C2OperationalStop(map_operational_reason(reason), target_ids(snapshot))


def _visual(reason: VisualStopReason, snapshot: D2EvidenceSnapshot | None) -> C2Result:
    if snapshot is None:
        return C2OperationalStop(OperationalStopReason.INCOMPLETE_EVIDENCE, ())
    evidence = visual_refs(snapshot, reason)
    snapshot_digest = digest(snapshot)
    if not evidence or snapshot_digest is None:
        return C2OperationalStop(OperationalStopReason.INCOMPLETE_EVIDENCE, target_ids(snapshot))
    return C2VisualInconclusive(reason, evidence, snapshot_digest)


adapt_c2 = adapt_c2_result
