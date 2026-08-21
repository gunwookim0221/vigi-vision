"""Pure validation for the D2-3 terminal publication boundary."""

from __future__ import annotations

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
    _ = validate_found(snapshot, snapshot.d1_result)
    if result.terminal_reason != "candidate_interval_found":
        raise ValueError
