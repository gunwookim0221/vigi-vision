"""Pure validation for the D2-3 terminal publication boundary."""

from __future__ import annotations

from datetime import datetime, timezone

from vigi_vision.recording_search_d1_identity import d1_input_bracket_id
from vigi_vision.recording_search_d2_identity import evidence_snapshot_digest
from vigi_vision.recording_search_d2_results import (
    C2NoCandidate,
    C2VisualInconclusive,
    D1BracketReady,
    D1VisualTerminal,
)
from vigi_vision.recording_search_d2_terminal_identity import terminal_result_id
from vigi_vision.recording_search_d2_terminal_models import (
    FoundResult,
    InconclusiveResult,
    NotFoundResult,
    TerminalInputSnapshot,
    TerminalResult,
)
from vigi_vision.recording_search_d2_terminal_validation import (
    validate_found,
    validate_input,
    validate_no_candidate,
    validate_visual,
)


def validate_terminal_publication(snapshot: TerminalInputSnapshot, result: TerminalResult) -> None:
    """Reject any terminal proposal that cannot be reconstructed from current evidence."""
    if type(snapshot) is not TerminalInputSnapshot or type(result) not in {
        FoundResult,
        NotFoundResult,
        InconclusiveResult,
    }:
        raise TypeError
    digest = validate_input(snapshot)
    if terminal_result_id(result) != result.result_id:
        raise ValueError
    if not _identity_matches(snapshot, result, digest):
        raise ValueError
    if isinstance(result, NotFoundResult):
        _validate_not_found(snapshot)
        return
    if isinstance(result, InconclusiveResult):
        _validate_inconclusive(snapshot, result)
        return
    _validate_found(snapshot, result)


def _identity_matches(snapshot: TerminalInputSnapshot, result: TerminalResult, digest: str) -> bool:
    evidence = snapshot.evidence_snapshot
    return (
        result.investigation_id == evidence.investigation_id
        and result.search_run_id == evidence.search_run_id
        and result.phase6_confirmation_id == evidence.phase6_confirmation_id
        and result.baseline_observation_id == evidence.baseline_observation_id
        and result.plan_id == snapshot.plan.plan_id
        and result.policy_identity == evidence.policy_identity
        and result.source_manifest_digest == evidence.source_revision.manifest_digest
        and result.evidence_snapshot_digest == digest
        and result.evidence_snapshot_digest == evidence_snapshot_digest(evidence)
    )


def _validate_not_found(snapshot: TerminalInputSnapshot) -> None:
    if not isinstance(snapshot.c2_result, C2NoCandidate):
        raise TypeError
    _ = validate_no_candidate(snapshot, snapshot.c2_result)


def _validate_inconclusive(snapshot: TerminalInputSnapshot, result: InconclusiveResult) -> None:
    if isinstance(snapshot.c2_result, C2VisualInconclusive):
        _ = validate_visual(snapshot, snapshot.c2_result)
    elif isinstance(snapshot.d1_result, D1VisualTerminal):
        _ = validate_visual(snapshot, snapshot.d1_result)
    else:
        raise TypeError
    if result.visual_reason.value != result.terminal_reason:
        raise ValueError


def _validate_found(snapshot: TerminalInputSnapshot, result: FoundResult) -> None:
    if not isinstance(snapshot.d1_result, D1BracketReady):
        raise TypeError
    narrowed, lower, support, narrowing = validate_found(snapshot, snapshot.d1_result)
    if result.terminal_reason != "candidate_interval_found":
        raise ValueError
    if (
        result.source_bracket_id != narrowed.source_bracket_id
        or result.narrowed_bracket_id != (narrowed.narrowed_bracket_id or "")
        or result.lower_bound_requested_time_utc != _format_utc(narrowed.lower_bound_utc)
        or result.upper_bound_requested_time_utc != _format_utc(narrowed.upper_bound_utc)
        or result.achieved_precision_seconds != narrowed.achieved_precision_seconds
        or result.lower_reference != lower
        or result.upper_support != support
        or result.narrowing_evidence != narrowing
        or result.d1_input_bracket_id
        != (
            d1_input_bracket_id(narrowed.d1_input_bracket)
            if narrowed.d1_input_bracket is not None
            else None
        )
        or result.history_digest != narrowed.history_digest
        or result.iterations != narrowed.iterations
        or result.stop_reason != narrowed.stop_reason.value
        or result.upper_support_group_id != narrowed.upper_support_group_id
    ):
        raise ValueError


def _format_utc(value: datetime) -> str:
    """Format a validated whole-second UTC bound exactly as result models do."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
