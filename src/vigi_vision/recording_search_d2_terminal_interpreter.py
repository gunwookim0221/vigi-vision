"""Pure D2-2 terminal interpretation and candidate discrimination."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, cast

from vigi_vision.recording_search_d1_history import (
    D1BracketState,
    history_digest,
    reconstruct_history,
)
from vigi_vision.recording_search_d1_identity import d1_input_bracket_id
from vigi_vision.recording_search_d2_enums import (
    D2EvidenceRole,
    OperationalStopReason,
    VisualStopReason,
)
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
from vigi_vision.recording_search_d2_terminal_identity import terminal_result_id
from vigi_vision.recording_search_d2_terminal_models import (
    CoarseTerminalCandidate,
    FoundCandidate,
    FoundResult,
    InconclusiveResult,
    NarrowingVisualTerminalCandidate,
    NonTerminalOutcome,
    NotFoundResult,
    OperationalOutcome,
    TerminalInputSnapshot,
    TerminalLimitation,
    TerminalNonTerminalReason,
    TerminalOutcome,
    TerminalResultKind,
    TerminalSourceStage,
)
from vigi_vision.recording_search_d2_terminal_validation import (
    validate_found,
    validate_input,
    validate_no_candidate,
    validate_visual,
)

if TYPE_CHECKING:
    from vigi_vision.recording_search_d1_history import NarrowingHistoryEntry
    from vigi_vision.recording_search_d1_models import NarrowedBracket
    from vigi_vision.recording_search_d2_evidence import D2EvidenceReference, D2EvidenceSnapshot


def interpret_terminal(value: TerminalInputSnapshot) -> TerminalOutcome:  # noqa: C901, PLR0911
    """Interpret validated C2/D1 outputs using one closed precedence order."""
    try:
        _ = validate_input(value)
    except (AttributeError, TypeError, ValueError):
        return OperationalOutcome(OperationalStopReason.CORRUPT_PERSISTED_EVIDENCE, ())
    c2 = value.c2_result
    if isinstance(c2, C2OperationalStop):
        if value.d1_result is not None:
            return OperationalOutcome(OperationalStopReason.ADAPTER_UNKNOWN_RESULT, ())
        return OperationalOutcome(c2.reason, c2.attempted_target_ids)
    if isinstance(c2, C2NoCandidate):
        if value.d1_result is not None:
            return OperationalOutcome(OperationalStopReason.ADAPTER_UNKNOWN_RESULT, ())
        try:
            grid = validate_no_candidate(value, c2)
        except (AttributeError, TypeError, ValueError):
            return OperationalOutcome(OperationalStopReason.CORRUPT_PERSISTED_EVIDENCE, ())
        return _not_found(value, grid)
    if isinstance(c2, C2VisualInconclusive):
        if value.d1_result is not None:
            return OperationalOutcome(OperationalStopReason.ADAPTER_UNKNOWN_RESULT, ())
        try:
            references = validate_visual(value, c2)
        except (AttributeError, TypeError, ValueError):
            return OperationalOutcome(OperationalStopReason.CORRUPT_PERSISTED_EVIDENCE, ())
        return _inconclusive(value, TerminalSourceStage.COARSE, c2.reason, references)
    if type(c2) is C2BracketReady:
        return _interpret_narrowing(value)
    return OperationalOutcome(OperationalStopReason.ADAPTER_UNKNOWN_RESULT, ())


def interpret_terminal_candidate(
    value: TerminalInputSnapshot,
    candidate: FoundCandidate | CoarseTerminalCandidate | NarrowingVisualTerminalCandidate,
) -> TerminalOutcome:
    """Interpret one explicitly typed terminalization candidate."""
    try:
        _ = validate_input(value)
        if isinstance(candidate, FoundCandidate):
            d1 = D1BracketReady(
                candidate.narrowed_bracket, evidence_snapshot_digest(value.evidence_snapshot)
            )
            narrowed, lower, support, narrowing = validate_found(value, d1)
            return _found(value, narrowed, lower, support, narrowing)
        if isinstance(candidate, CoarseTerminalCandidate):
            if type(candidate.result) is C2NoCandidate:
                grid = validate_no_candidate(value, candidate.result)
                return _not_found(value, grid)
            if type(candidate.result) is C2VisualInconclusive:
                refs = validate_visual(value, candidate.result)
                return _inconclusive(
                    value, TerminalSourceStage.COARSE, candidate.result.reason, refs
                )
        if type(candidate) is NarrowingVisualTerminalCandidate:
            refs = validate_visual(value, candidate.narrowing_result)
            history = cast(
                "tuple[NarrowingHistoryEntry, ...]", candidate.narrowing_result.narrowing_history
            )
            _validate_visual_history(
                value,
                history,
            )
            return _inconclusive(
                value,
                TerminalSourceStage.NARROWING,
                candidate.narrowing_result.reason,
                refs,
            )
    except (AttributeError, TypeError, ValueError):
        return OperationalOutcome(OperationalStopReason.CORRUPT_PERSISTED_EVIDENCE, ())
    return OperationalOutcome(OperationalStopReason.ADAPTER_UNKNOWN_RESULT, ())


def _interpret_narrowing(value: TerminalInputSnapshot) -> TerminalOutcome:  # noqa: C901, PLR0911
    d1 = value.d1_result
    if d1 is None:
        return NonTerminalOutcome(TerminalNonTerminalReason.INCOMPLETE_EVIDENCE)
    if isinstance(d1, D1OperationalStop):
        return OperationalOutcome(d1.reason, d1.attempted_target_ids)
    if isinstance(d1, D1NonTerminalStop):
        try:
            reason = TerminalNonTerminalReason(d1.reason.value)
            if value.d1_input_bracket is not None:
                _validate_visual_history(
                    value,
                    cast("tuple[NarrowingHistoryEntry, ...]", d1.narrowing_history),
                )
        except ValueError:
            return OperationalOutcome(OperationalStopReason.ADAPTER_UNKNOWN_RESULT, ())
        return NonTerminalOutcome(reason)
    if isinstance(d1, D1BracketReady):
        try:
            narrowed, lower, support, narrowing = validate_found(value, d1)
        except (AttributeError, TypeError, ValueError):
            return OperationalOutcome(OperationalStopReason.CORRUPT_PERSISTED_EVIDENCE, ())
        return _found(value, narrowed, lower, support, narrowing)
    if type(d1) is D1VisualTerminal:
        try:
            refs = validate_visual(value, d1)
            if type(d1) is not D1VisualTerminal:
                return OperationalOutcome(OperationalStopReason.ADAPTER_UNKNOWN_RESULT, ())
            _validate_visual_history(
                value, cast("tuple[NarrowingHistoryEntry, ...]", d1.narrowing_history)
            )
        except (AttributeError, TypeError, ValueError):
            return OperationalOutcome(OperationalStopReason.CORRUPT_PERSISTED_EVIDENCE, ())
        return _inconclusive(value, TerminalSourceStage.NARROWING, d1.reason, refs)
    return OperationalOutcome(OperationalStopReason.ADAPTER_UNKNOWN_RESULT, ())


def _not_found(
    value: TerminalInputSnapshot, grid: tuple[D2EvidenceReference, ...]
) -> NotFoundResult:
    limitations = (
        TerminalLimitation.CONFIGURED_SAMPLES_ONLY,
        TerminalLimitation.POLICY_PENDING_PHASE7E_VALIDATION,
    )
    result = NotFoundResult(
        result_id="",
        result_kind=TerminalResultKind.NOT_FOUND,
        investigation_id=value.evidence_snapshot.investigation_id,
        search_run_id=value.evidence_snapshot.search_run_id,
        phase6_confirmation_id=value.evidence_snapshot.phase6_confirmation_id,
        baseline_observation_id=value.evidence_snapshot.baseline_observation_id,
        plan_id=value.plan.plan_id,
        policy_identity=value.evidence_snapshot.policy_identity,
        source_manifest_digest=value.evidence_snapshot.source_revision.manifest_digest,
        evidence_snapshot_digest=evidence_snapshot_digest(value.evidence_snapshot),
        terminal_reason="no_transition_in_window",
        limitations=limitations,
        search_start_utc=_format_utc(value.plan.search_start_utc),
        search_end_utc=_format_utc(value.plan.search_end_utc),
        coarse_grid=grid,
    )
    return replace(result, result_id=terminal_result_id(result))


def _found(
    value: TerminalInputSnapshot,
    narrowed: NarrowedBracket,
    lower: D2EvidenceReference,
    support: tuple[D2EvidenceReference, ...],
    narrowing: tuple[D2EvidenceReference, ...],
) -> FoundResult:
    limitations = [TerminalLimitation.REQUESTED_TIME_INTERVAL_NOT_EXACT_EVENT]
    if any(item.decoded_frame_utc != item.requested_time_utc for item in (*support, *narrowing)):
        limitations.append(TerminalLimitation.DECODED_TIME_DIFFERS_FROM_REQUESTED)
    limitations.append(TerminalLimitation.POLICY_PENDING_PHASE7E_VALIDATION)
    result = FoundResult(
        result_id="",
        result_kind=TerminalResultKind.FOUND,
        investigation_id=value.evidence_snapshot.investigation_id,
        search_run_id=value.evidence_snapshot.search_run_id,
        phase6_confirmation_id=value.evidence_snapshot.phase6_confirmation_id,
        baseline_observation_id=value.evidence_snapshot.baseline_observation_id,
        plan_id=value.plan.plan_id,
        policy_identity=value.evidence_snapshot.policy_identity,
        source_manifest_digest=value.evidence_snapshot.source_revision.manifest_digest,
        evidence_snapshot_digest=evidence_snapshot_digest(value.evidence_snapshot),
        terminal_reason="candidate_interval_found",
        limitations=tuple(limitations),
        source_bracket_id=narrowed.source_bracket_id,
        narrowed_bracket_id=narrowed.narrowed_bracket_id or "",
        lower_bound_requested_time_utc=_format_utc(narrowed.lower_bound_utc),
        upper_bound_requested_time_utc=_format_utc(narrowed.upper_bound_utc),
        achieved_precision_seconds=narrowed.achieved_precision_seconds,
        lower_reference=lower,
        upper_support=support,
        narrowing_evidence=narrowing,
        d1_input_bracket_id=(
            d1_input_bracket_id(narrowed.d1_input_bracket)
            if narrowed.d1_input_bracket is not None
            else None
        ),
        history_digest=narrowed.history_digest,
        iterations=narrowed.iterations,
        stop_reason=narrowed.stop_reason.value,
        upper_support_group_id=narrowed.upper_support_group_id,
    )
    return replace(result, result_id=terminal_result_id(result))


def _inconclusive(
    value: TerminalInputSnapshot,
    stage: TerminalSourceStage,
    reason: VisualStopReason,
    references: tuple[D2EvidenceReference, ...],
) -> InconclusiveResult:
    limitation = TerminalLimitation(reason.value)
    result = InconclusiveResult(
        result_id="",
        result_kind=TerminalResultKind.INCONCLUSIVE,
        investigation_id=value.evidence_snapshot.investigation_id,
        search_run_id=value.evidence_snapshot.search_run_id,
        phase6_confirmation_id=value.evidence_snapshot.phase6_confirmation_id,
        baseline_observation_id=value.evidence_snapshot.baseline_observation_id,
        plan_id=value.plan.plan_id,
        policy_identity=value.evidence_snapshot.policy_identity,
        source_manifest_digest=value.evidence_snapshot.source_revision.manifest_digest,
        evidence_snapshot_digest=evidence_snapshot_digest(value.evidence_snapshot),
        terminal_reason=reason.value,
        limitations=(limitation, TerminalLimitation.POLICY_PENDING_PHASE7E_VALIDATION),
        source_stage=stage,
        visual_reason=reason,
        evidence=references,
    )
    return replace(result, result_id=terminal_result_id(result))


def _validate_history_references(
    snapshot: D2EvidenceSnapshot,
    entries: tuple[NarrowingHistoryEntry, ...],
) -> None:
    refs = {
        item.observation_id: item for item in snapshot.references if item.observation_id is not None
    }
    for entry in entries:
        for evidence in entry.evidence:
            reference = refs.get(evidence.observation_id)
            expected_role = (
                D2EvidenceRole.D1_MIDPOINT
                if evidence.role == "MIDPOINT"
                else D2EvidenceRole.ABSENCE_SUPPORT
            )
            if reference is None or reference.role is not expected_role:
                raise ValueError
            if (
                reference.alias_id is not None
                or reference.target_id != evidence.target_id
                or reference.probe_request_id != evidence.probe_request_id
                or reference.acquisition_operation_id != evidence.acquisition_operation_id
                or reference.classification_operation_id != evidence.classification_operation_id
                or reference.canonical_frame_id != evidence.canonical_frame_id
                or reference.decode_session_id != evidence.decode_session_id
                or reference.decoded_frame_utc != evidence.decoded_frame_utc
                or reference.decoded_pts != evidence.decoded_pts
                or reference.decoded_ordinal != evidence.decoded_ordinal
                or reference.requested_time_utc != evidence.requested_time_utc
                or reference.classification is not evidence.classification
            ):
                raise ValueError


def _validate_visual_history(
    value: TerminalInputSnapshot, entries: tuple[NarrowingHistoryEntry, ...]
) -> None:
    if value.d1_input_bracket is None:
        raise ValueError
    if type(entries) is not tuple:
        raise ValueError
    _validate_history_references(value.evidence_snapshot, entries)
    expected = history_digest(
        value.d1_input_bracket,
        d1_input_bracket_id(value.d1_input_bracket),
        entries,
    )
    final = (
        entries[-1].bracket_after
        if entries
        else D1BracketState(
            value.d1_input_bracket.lower_bound.requested_time_utc,
            value.d1_input_bracket.upper_support.requested_support_times[0],
            value.d1_input_bracket.lower_bound,
            value.d1_input_bracket.upper_support.support_group_id,
        )
    )
    _ = reconstruct_history(value.d1_input_bracket, entries, expected, None, final_bracket=final)


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond != 0:
        raise ValueError
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
