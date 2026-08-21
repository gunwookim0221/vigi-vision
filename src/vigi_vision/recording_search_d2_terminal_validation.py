"""Strict reconstruction and terminal-candidate validation for D2-2."""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, cast

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_c2_models import CoarseCandidateBracket
from vigi_vision.recording_search_d1_history import D1BracketState, reconstruct_history
from vigi_vision.recording_search_d1_identity import policy_identity, source_bracket_identity
from vigi_vision.recording_search_d2_enums import D2EvidenceRole
from vigi_vision.recording_search_d2_evidence import D2EvidenceSnapshot
from vigi_vision.recording_search_d2_identity import evidence_snapshot_digest
from vigi_vision.recording_search_d2_results import (
    C2BracketReady,
    C2NoCandidate,
    C2OperationalStop,
    C2VisualInconclusive,
    D1BracketReady,
    D1NonTerminalStop,
    D1OperationalStop,
    D1VisualTerminal,
)
from vigi_vision.recording_search_d2_terminal_models import TerminalInputSnapshot

if TYPE_CHECKING:
    from vigi_vision.recording_search_d1_identity import D1InputBracket, D1LowerBoundReference
    from vigi_vision.recording_search_d1_models import NarrowedBracket
    from vigi_vision.recording_search_d2_evidence import D2EvidenceReference


def validate_input(value: TerminalInputSnapshot) -> str:  # noqa: C901
    """Validate authoritative context and return its recomputed snapshot digest."""
    if type(value) is not TerminalInputSnapshot:
        raise TypeError
    if type(value.c2_result) not in {
        C2BracketReady,
        C2NoCandidate,
        C2OperationalStop,
        C2VisualInconclusive,
    }:
        raise ValueError
    if value.d1_result is not None and type(value.d1_result) not in {
        D1BracketReady,
        D1NonTerminalStop,
        D1OperationalStop,
        D1VisualTerminal,
    }:
        raise ValueError
    snapshot = value.evidence_snapshot
    if type(snapshot) is not D2EvidenceSnapshot:
        raise TypeError
    if snapshot.plan_id != value.plan.plan_id:
        raise ValueError
    if snapshot.policy_identity != policy_identity(value.policy):
        raise ValueError
    if (
        value.plan.search_start_utc != value.policy.search_start_utc
        or value.plan.search_end_utc != value.policy.search_end_utc
        or value.plan.interval_seconds != value.policy.coarse_interval_seconds
        or value.plan.absence_confirmation_frames != value.policy.absence_confirmation_frames
        or value.plan.absence_cadence_seconds != value.policy.absence_cadence_seconds
    ):
        raise ValueError
    if isinstance(value.c2_result, C2BracketReady) and value.d1_result is not None:
        _validate_c2_bracket(value, value.c2_result.bracket)
    if value.d1_input_bracket is not None:
        _validate_d1_input(value, value.d1_input_bracket)
    digest = evidence_snapshot_digest(snapshot)
    if not digest:
        raise ValueError
    return digest


def validate_no_candidate(
    value: TerminalInputSnapshot, result: C2NoCandidate
) -> tuple[D2EvidenceReference, ...]:
    """Validate the complete inclusive all-PRESENT coarse grid."""
    digest = evidence_snapshot_digest(value.evidence_snapshot)
    if result.evidence_snapshot_digest != digest:
        raise ValueError
    grid = result.complete_present_grid
    if type(grid) is not tuple or len(grid) != len(value.plan.target_times):
        raise ValueError
    if tuple(item.requested_time_utc for item in grid) != value.plan.target_times:
        raise ValueError
    by_observation = _snapshot_refs(value.evidence_snapshot)
    seen_frames: set[str] = set()
    for reference in grid:
        if (
            reference not in by_observation.values()
            or reference.role is not D2EvidenceRole.COARSE_TARGET
        ):
            raise ValueError
        if (
            reference.classification is not ClassificationOutcome.PRESENT
            or reference.alias_id is not None
        ):
            raise ValueError
        if reference.canonical_frame_id is None or reference.canonical_frame_id in seen_frames:
            raise ValueError
        seen_frames.add(reference.canonical_frame_id)
    coarse = tuple(
        item
        for item in value.evidence_snapshot.references
        if item.role is D2EvidenceRole.COARSE_TARGET
    )
    if coarse != grid:
        raise ValueError
    return grid


def validate_visual(
    value: TerminalInputSnapshot,
    result: C2VisualInconclusive | D1VisualTerminal,
) -> tuple[D2EvidenceReference, ...]:
    """Validate closed visual evidence and its snapshot ownership."""
    digest = evidence_snapshot_digest(value.evidence_snapshot)
    if result.evidence_snapshot_digest != digest:
        raise ValueError
    references = (
        result.evidence if isinstance(result, C2VisualInconclusive) else result.blocking_evidence
    )
    if type(references) is not tuple or not references:
        raise ValueError
    by_observation = _snapshot_refs(value.evidence_snapshot)
    for reference in references:
        if (
            reference.observation_id not in by_observation
            or reference != by_observation[reference.observation_id]
        ):
            raise ValueError
        if reference.classification not in {
            ClassificationOutcome.PRESENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.INDETERMINATE,
        }:
            raise ValueError
    if result.reason.value == "insufficient_visual_evidence" and any(
        reference.classification is not ClassificationOutcome.INDETERMINATE
        for reference in references
    ):
        raise ValueError
    if result.reason.value == "nonmonotonic_visual_evidence":
        states = {reference.classification for reference in references}
        if states != {ClassificationOutcome.PRESENT, ClassificationOutcome.ABSENT}:
            raise ValueError
    if result.reason.value == "insufficient_distinct_visual_support" and not any(
        reference.alias_id is not None for reference in references
    ):
        raise ValueError
    return references


def validate_found(
    value: TerminalInputSnapshot, result: D1BracketReady
) -> tuple[
    NarrowedBracket,
    D2EvidenceReference,
    tuple[D2EvidenceReference, ...],
    tuple[D2EvidenceReference, ...],
]:
    """Reconstruct D1 history and validate a precise FOUND candidate."""
    digest = evidence_snapshot_digest(value.evidence_snapshot)
    if result.evidence_snapshot_digest != digest:
        raise ValueError
    narrowed = result.narrowed_bracket
    input_bracket = narrowed.d1_input_bracket
    source_bracket = narrowed.source_bracket
    if input_bracket is None:
        raise ValueError
    if source_bracket is None or type(source_bracket) is not CoarseCandidateBracket:
        raise ValueError
    if narrowed.stop_reason.value != "target_precision_reached":
        raise ValueError
    if narrowed.achieved_precision_seconds > value.policy.binary_stop_resolution_seconds:
        raise ValueError
    if narrowed.manifest_digest != value.evidence_snapshot.source_revision.manifest_digest:
        raise ValueError
    if (
        narrowed.investigation_id != value.evidence_snapshot.investigation_id
        or narrowed.search_run_id != value.evidence_snapshot.search_run_id
        or narrowed.phase6_confirmation_id != value.evidence_snapshot.phase6_confirmation_id
        or narrowed.policy_version != value.policy.policy_version
        or narrowed.source_bracket_id
        != value.evidence_snapshot.source_revision.d1_source_bracket_id
        or source_bracket.plan_id != value.plan.plan_id
        or source_bracket.policy_version != value.policy.policy_version
        or source_bracket.manifest_digest != value.evidence_snapshot.source_revision.manifest_digest
        or source_bracket_identity(source_bracket)
        != value.evidence_snapshot.source_revision.c2_bracket_id
        or narrowed.baseline_identity != source_bracket.identity.baseline_identity
    ):
        raise ValueError
    _validate_d1_input(value, input_bracket)
    final_bracket = D1BracketState(
        narrowed.lower_bound_utc,
        narrowed.upper_bound_utc,
        _history_lower_reference(narrowed),
        narrowed.upper_support_group_id or "",
    )
    if narrowed.history_digest is None or narrowed.narrowed_bracket_id is None:
        raise ValueError
    _ = reconstruct_history(
        input_bracket,
        narrowed.history,
        narrowed.history_digest,
        narrowed.narrowed_bracket_id,
        final_bracket=final_bracket,
        iteration_count=narrowed.iterations,
        achieved_precision_seconds=narrowed.achieved_precision_seconds,
        stop_reason=narrowed.stop_reason.value,
        manifest_digest=narrowed.manifest_digest,
        source_bracket=source_bracket,
    )
    if narrowed.iterations != len(narrowed.history):
        raise ValueError
    lower = _lower_reference(value.evidence_snapshot, narrowed)
    support = _support_references(value.evidence_snapshot, narrowed, value)
    narrowing = _narrowing_references(value.evidence_snapshot, narrowed)
    _validate_narrowing_evidence(value.evidence_snapshot, narrowed)
    _validate_no_contradiction(value.evidence_snapshot, narrowed)
    return narrowed, lower, support, narrowing


def _validate_d1_input(value: TerminalInputSnapshot, bracket: D1InputBracket) -> None:
    if not all(
        type(item) is str and bool(item)
        for item in (
            bracket.investigation_id,
            bracket.search_run_id,
            bracket.phase6_confirmation_id,
            bracket.plan_id,
            bracket.policy_identity,
            bracket.baseline_identity,
        )
    ):
        raise ValueError
    if (
        bracket.investigation_id != value.evidence_snapshot.investigation_id
        or bracket.search_run_id != value.evidence_snapshot.search_run_id
        or bracket.phase6_confirmation_id != value.evidence_snapshot.phase6_confirmation_id
        or bracket.plan_id != value.plan.plan_id
        or bracket.policy_identity != value.evidence_snapshot.policy_identity
        or bracket.source_revision.c2_bracket_id
        != value.evidence_snapshot.source_revision.c2_bracket_id
        or bracket.source_revision.c2_manifest_digest
        != value.evidence_snapshot.source_revision.manifest_digest
    ):
        raise ValueError
    if isinstance(value.c2_result, C2BracketReady):
        source = value.c2_result.bracket
        if (
            type(source) is not CoarseCandidateBracket
            or bracket.baseline_identity != source.identity.baseline_identity
        ):
            raise ValueError


def _validate_c2_bracket(value: TerminalInputSnapshot, bracket: object) -> None:
    if type(bracket) is not CoarseCandidateBracket:
        raise TypeError
    snapshot = value.evidence_snapshot
    if (
        bracket.investigation_id != snapshot.investigation_id
        or bracket.search_run_id != snapshot.search_run_id
        or bracket.identity.phase6_confirmation_id != snapshot.phase6_confirmation_id
        or bracket.plan_id != value.plan.plan_id
        or bracket.policy_version != value.policy.policy_version
        or bracket.baseline_observation_id != snapshot.baseline_observation_id
        or bracket.manifest_digest != snapshot.source_revision.manifest_digest
        or source_bracket_identity(bracket) != snapshot.source_revision.c2_bracket_id
    ):
        raise ValueError


def _snapshot_refs(snapshot: D2EvidenceSnapshot) -> dict[str, D2EvidenceReference]:
    return {
        item.observation_id: item for item in snapshot.references if item.observation_id is not None
    }


def _history_lower_reference(narrowed: NarrowedBracket) -> D1LowerBoundReference:
    if narrowed.history:
        return narrowed.history[-1].bracket_after.lower_reference
    if narrowed.d1_input_bracket is None:
        raise ValueError
    return narrowed.d1_input_bracket.lower_bound


def _lower_reference(
    snapshot: D2EvidenceSnapshot, narrowed: NarrowedBracket
) -> D2EvidenceReference:
    evidence = narrowed.lower_evidence
    lower_reference = _history_lower_reference(narrowed)
    reference = _snapshot_refs(snapshot).get(evidence.observation_id)
    if reference is None or reference.classification is not ClassificationOutcome.PRESENT:
        raise ValueError
    if (
        reference.observation_id != lower_reference.observation_id
        or reference.requested_time_utc != lower_reference.requested_time_utc
        or reference.probe_request_id != lower_reference.probe_request_id
        or reference.canonical_frame_id != lower_reference.canonical_frame_id
    ):
        raise ValueError
    if evidence.is_baseline:
        if (
            not reference.is_phase6_baseline
            or reference.observation_id != snapshot.baseline_observation_id
            or narrowed.d1_input_bracket is None
            or reference.requested_time_utc
            != narrowed.d1_input_bracket.lower_bound.requested_time_utc
        ):
            raise ValueError
    elif (
        reference.is_phase6_baseline
        or reference.role not in {D2EvidenceRole.COARSE_TARGET, D2EvidenceRole.D1_MIDPOINT}
        or reference.alias_id is not None
        or reference.target_id != evidence.target_id
        or reference.probe_request_id != evidence.probe_request_id
        or reference.acquisition_operation_id != evidence.operation_id
        or reference.decode_session_id != evidence.decode_session_id
        or reference.decoded_frame_utc != evidence.decoded_frame_utc
        or reference.decoded_pts != evidence.decoded_pts
        or reference.decoded_ordinal != evidence.decoded_ordinal
        or reference.canonical_frame_id != evidence.canonical_frame_id
    ):
        raise ValueError
    return reference


def _support_references(  # noqa: C901, PLR0912
    snapshot: D2EvidenceSnapshot, narrowed: NarrowedBracket, value: TerminalInputSnapshot
) -> tuple[D2EvidenceReference, ...]:
    evidence = narrowed.upper_support_evidence
    if len(evidence) != value.policy.absence_confirmation_frames:
        raise ValueError
    refs = _snapshot_refs(snapshot)
    selected: list[D2EvidenceReference] = []
    for item in evidence:
        reference = refs.get(item.observation_id)
        if reference is None or reference.classification is not ClassificationOutcome.ABSENT:
            raise ValueError
        if (
            reference.role is not D2EvidenceRole.ABSENCE_SUPPORT
            or reference.alias_id is not None
            or reference.support_group_id != narrowed.upper_support_group_id
            or reference.support_index != len(selected)
            or reference.target_id != item.target_id
            or reference.probe_request_id != item.probe_request_id
            or reference.acquisition_operation_id != item.operation_id
            or reference.canonical_frame_id != item.canonical_frame_id
            or reference.decode_session_id != item.decode_session_id
            or reference.decoded_frame_utc != item.decoded_frame_utc
            or reference.decoded_pts != item.decoded_pts
            or reference.decoded_ordinal != item.decoded_ordinal
        ):
            raise ValueError
        selected.append(reference)
    if len({item.target_id for item in selected}) != len(selected):
        raise ValueError
    if len({item.observation_id for item in selected}) != len(selected):
        raise ValueError
    if len({item.canonical_frame_id for item in selected}) != len(selected):
        raise ValueError
    if any(
        left.requested_time_utc >= right.requested_time_utc for left, right in pairwise(selected)
    ):
        raise ValueError
    cadence = value.policy.absence_cadence_seconds
    if any(
        right.requested_time_utc - left.requested_time_utc != timedelta(seconds=cadence)
        for left, right in pairwise(selected)
    ):
        raise ValueError
    decoded = tuple(item.decoded_frame_utc for item in selected)
    pts = tuple(item.decoded_pts for item in selected)
    ordinals = tuple(item.decoded_ordinal for item in selected)
    if any(item is None for item in (*decoded, *pts, *ordinals)):
        raise ValueError
    decoded_values = cast("tuple[datetime, ...]", decoded)
    pts_values = cast("tuple[int, ...]", pts)
    ordinal_values = cast("tuple[int, ...]", ordinals)
    if any(left >= right for left, right in pairwise(decoded_values)):
        raise ValueError
    if any(left >= right for left, right in pairwise(pts_values)):
        raise ValueError
    if any(left >= right for left, right in pairwise(ordinal_values)):
        raise ValueError
    sessions = {item.decode_session_id for item in selected}
    if (
        len(sessions) != 1
        or selected[0].requested_time_utc != narrowed.upper_bound_utc
        or selected[-1].requested_time_utc > value.plan.search_end_utc
    ):
        raise ValueError
    return tuple(selected)


def _narrowing_references(
    snapshot: D2EvidenceSnapshot, narrowed: NarrowedBracket
) -> tuple[D2EvidenceReference, ...]:
    refs = tuple(item for item in snapshot.references if item.role is D2EvidenceRole.D1_MIDPOINT)
    expected = tuple(item.observation_id for item in narrowed.evidence)
    if any(item is None for item in expected):
        raise ValueError
    if tuple(item.observation_id for item in refs) != expected:
        raise ValueError
    return refs


def _validate_narrowing_evidence(snapshot: D2EvidenceSnapshot, narrowed: NarrowedBracket) -> None:
    refs = _snapshot_refs(snapshot)
    for item in narrowed.evidence:
        if item.observation_id is None:
            raise ValueError
        reference = refs.get(item.observation_id)
        if reference is None or reference.role is not D2EvidenceRole.D1_MIDPOINT:
            raise ValueError
        if (
            reference.target_id != item.target_id
            or reference.probe_request_id != item.probe_request_id
            or reference.acquisition_operation_id != item.operation_id
            or reference.classification_operation_id != item.classification_operation_id
            or reference.canonical_frame_id != item.canonical_frame_id
            or reference.decode_session_id != item.decode_session_id
            or reference.decoded_frame_utc != item.decoded_frame_utc
            or reference.decoded_pts != item.decoded_pts
            or reference.decoded_ordinal != item.decoded_ordinal
            or reference.classification is not item.state
        ):
            raise ValueError


def _validate_no_contradiction(snapshot: D2EvidenceSnapshot, narrowed: NarrowedBracket) -> None:
    for reference in snapshot.references:
        if reference.is_phase6_baseline:
            continue
        if (
            reference.requested_time_utc <= narrowed.lower_bound_utc
            and reference.classification is ClassificationOutcome.ABSENT
        ):
            raise ValueError
        if (
            reference.requested_time_utc >= narrowed.upper_bound_utc
            and reference.classification is ClassificationOutcome.PRESENT
        ):
            raise ValueError
