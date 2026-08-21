"""Pure adapter from the current D1 result to the closed D2 union."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vigi_vision.recording_search_d1_models import (
    NarrowedBracket,
    NarrowingResult,
    NarrowingStatus,
    NarrowingStopReason,
)
from vigi_vision.recording_search_d2_adapter_support import (
    digest,
    map_operational_reason,
    missing_snapshot_reason,
    support_refs,
    target_ids,
    visual_refs,
)
from vigi_vision.recording_search_d2_enums import (
    D1NonTerminalReason,
    OperationalStopReason,
    VisualStopReason,
)
from vigi_vision.recording_search_d2_results import (
    D1BracketReady,
    D1NonTerminalStop,
    D1OperationalStop,
    D1Result,
    D1VisualTerminal,
)

if TYPE_CHECKING:
    from vigi_vision.recording_search_d2_enums import D2HistoryEntry
    from vigi_vision.recording_search_d2_evidence import D2EvidenceSnapshot


def adapt_d1_result(
    result: NarrowingResult,
    snapshot: D2EvidenceSnapshot | None = None,
    history: tuple[D2HistoryEntry, ...] | None = None,
) -> D1Result:
    """Convert D1 output without constructing history owned by D2-1."""
    effective_history = getattr(result, "history", ()) if history is None else history
    try:
        if type(result.status) is not NarrowingStatus or not _shape_is_valid(result):
            return D1OperationalStop(OperationalStopReason.ADAPTER_UNKNOWN_RESULT, ())
        adapted: D1Result
        status_value = result.status.value
        if status_value == NarrowingStatus.READY.value:
            adapted = _ready(result.narrowed_bracket, snapshot, effective_history)
        elif status_value == NarrowingStatus.INDETERMINATE.value:
            adapted = _indeterminate(result.safe_reason, snapshot, effective_history)
        elif status_value == NarrowingStatus.INTERRUPTED.value:
            adapted = _interrupted(result.safe_reason, snapshot)
        elif status_value == NarrowingStatus.CORRUPT.value:
            adapted = _corrupt(result.safe_reason, snapshot)
        elif status_value == NarrowingStatus.INCOMPLETE.value:
            adapted = _nonterminal(
                D1NonTerminalReason.INCOMPLETE_EVIDENCE, effective_history, snapshot
            )
        elif status_value == NarrowingStatus.RESOURCE_EXHAUSTED.value:
            adapted = D1OperationalStop(
                OperationalStopReason.CAPACITY_EXHAUSTED, target_ids(snapshot)
            )
        else:
            return D1OperationalStop(OperationalStopReason.ADAPTER_UNKNOWN_RESULT, ())
    except (AttributeError, TypeError, ValueError):
        return D1OperationalStop(OperationalStopReason.ADAPTER_UNKNOWN_RESULT, ())
    else:
        return adapted


def _shape_is_valid(result: NarrowingResult) -> bool:
    if result.status is NarrowingStatus.READY:
        return isinstance(result.narrowed_bracket, NarrowedBracket) and result.safe_reason is None
    return (
        result.narrowed_bracket is None
        and type(result.safe_reason) is str
        and bool(result.safe_reason)
    )


def _ready(
    bracket: NarrowedBracket | None,
    snapshot: D2EvidenceSnapshot | None,
    history: tuple[D2HistoryEntry, ...] | None,
) -> D1Result:
    if not isinstance(bracket, NarrowedBracket):
        return D1OperationalStop(
            OperationalStopReason.CORRUPT_PERSISTED_EVIDENCE, target_ids(snapshot)
        )
    if bracket.stop_reason is NarrowingStopReason.MAXIMUM_ITERATIONS:
        return _nonterminal(D1NonTerminalReason.MAXIMUM_ITERATIONS, history, snapshot)
    if bracket.stop_reason is not NarrowingStopReason.TARGET_PRECISION_REACHED:
        return D1OperationalStop(OperationalStopReason.ADAPTER_UNKNOWN_RESULT, target_ids(snapshot))
    snapshot_digest = digest(snapshot)
    if snapshot_digest is None:
        return D1OperationalStop(missing_snapshot_reason(snapshot), target_ids(snapshot))
    if type(history) is not tuple:
        return D1OperationalStop(OperationalStopReason.INCOMPLETE_EVIDENCE, target_ids(snapshot))
    return D1BracketReady(bracket, snapshot_digest)


def _indeterminate(
    reason: str | None,
    snapshot: D2EvidenceSnapshot | None,
    history: tuple[D2HistoryEntry, ...] | None,
) -> D1Result:
    if reason == "no_distinct_midpoint":
        return _nonterminal(D1NonTerminalReason.NO_DISTINCT_MIDPOINT, history, snapshot)
    if reason == "visual_indeterminate":
        return _visual(VisualStopReason.INSUFFICIENT_VISUAL_EVIDENCE, snapshot, history)
    if reason == "absence_support_unusable":
        return _visual(
            VisualStopReason.INSUFFICIENT_DISTINCT_VISUAL_SUPPORT,
            snapshot,
            history,
            support=True,
        )
    return D1OperationalStop(map_operational_reason(reason), target_ids(snapshot))


def _interrupted(reason: str | None, snapshot: D2EvidenceSnapshot | None) -> D1Result:
    if reason == "inactive_run_handle":
        mapped = OperationalStopReason.INACTIVE_AUTHORITY
    elif reason == "interrupted":
        mapped = OperationalStopReason.INTERRUPTED
    else:
        mapped = OperationalStopReason.ADAPTER_UNKNOWN_RESULT
    return D1OperationalStop(mapped, target_ids(snapshot))


def _corrupt(reason: str | None, snapshot: D2EvidenceSnapshot | None) -> D1Result:
    if reason == "authoritative_evidence_invalid":
        mapped = OperationalStopReason.CORRUPT_PERSISTED_EVIDENCE
    elif reason == "stale_authoritative_evidence":
        mapped = OperationalStopReason.STALE_AUTHORITY
    else:
        mapped = OperationalStopReason.ADAPTER_UNKNOWN_RESULT
    return D1OperationalStop(mapped, target_ids(snapshot))


def _visual(
    reason: VisualStopReason,
    snapshot: D2EvidenceSnapshot | None,
    history: tuple[D2HistoryEntry, ...] | None,
    *,
    support: bool = False,
) -> D1Result:
    if snapshot is None or type(history) is not tuple:
        return D1OperationalStop(OperationalStopReason.INCOMPLETE_EVIDENCE, target_ids(snapshot))
    evidence = support_refs(snapshot) if support else visual_refs(snapshot, reason)
    snapshot_digest = digest(snapshot)
    if not evidence or snapshot_digest is None:
        return D1OperationalStop(OperationalStopReason.INCOMPLETE_EVIDENCE, target_ids(snapshot))
    return D1VisualTerminal(reason, history, evidence, snapshot_digest)


def _nonterminal(
    reason: D1NonTerminalReason,
    history: tuple[D2HistoryEntry, ...] | None,
    snapshot: D2EvidenceSnapshot | None,
) -> D1Result:
    if type(history) is not tuple:
        return D1OperationalStop(OperationalStopReason.INCOMPLETE_EVIDENCE, target_ids(snapshot))
    return D1NonTerminalStop(reason, history)


adapt_d1 = adapt_d1_result
