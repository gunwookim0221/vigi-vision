from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import cast

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_c1_planner import build_coarse_sampling_plan
from vigi_vision.recording_search_c2_models import CoarseCandidateBracket
from vigi_vision.recording_search_d1_identity import policy_identity
from vigi_vision.recording_search_d2_enums import (
    D2EvidenceRole,
    OperationalStopReason,
    VisualStopReason,
)
from vigi_vision.recording_search_d2_evidence import (
    D2EvidenceReference,
    D2EvidenceSnapshot,
    D2SourceRevision,
)
from vigi_vision.recording_search_d2_identity import evidence_snapshot_digest
from vigi_vision.recording_search_d2_results import (
    C2BracketReady,
    C2NoCandidate,
    C2OperationalStop,
    C2VisualInconclusive,
)
from vigi_vision.recording_search_d2_terminal import (
    InconclusiveResult,
    NonTerminalOutcome,
    NotFoundResult,
    OperationalOutcome,
    TerminalInputSnapshot,
    TerminalResultKind,
    interpret_terminal,
    terminal_result_id,
)
from vigi_vision.recording_search_models import default_policy

UTC = timezone.utc
START = datetime(2026, 7, 20, 3, 0, tzinfo=UTC)


def _context(*, indeterminate_index: int | None = None) -> TerminalInputSnapshot:
    policy = default_policy(START, START + timedelta(seconds=4)).model_copy(
        update={"coarse_interval_seconds": 1}
    )
    plan = build_coarse_sampling_plan(policy)
    references = [
        D2EvidenceReference(
            role=D2EvidenceRole.BASELINE,
            target_id=None,
            requested_time_utc=START,
            acquisition_operation_id=None,
            probe_request_id=None,
            classification_operation_id=None,
            observation_id="baseline-observation",
            canonical_frame_id=None,
            alias_id=None,
            decode_session_id=None,
            decoded_frame_utc=None,
            decoded_pts=None,
            decoded_ordinal=None,
            support_group_id=None,
            support_index=None,
            is_phase6_baseline=True,
            classification=ClassificationOutcome.PRESENT,
        )
    ]
    for index, requested in enumerate(plan.target_times):
        state = (
            ClassificationOutcome.INDETERMINATE
            if index == indeterminate_index
            else ClassificationOutcome.PRESENT
        )
        references.append(
            D2EvidenceReference(
                role=D2EvidenceRole.COARSE_TARGET,
                target_id=f"target-{index}",
                requested_time_utc=requested,
                acquisition_operation_id=f"acquisition-{index}",
                probe_request_id=f"probe-{index}",
                classification_operation_id=f"classification-{index}",
                observation_id=f"observation-{index}",
                canonical_frame_id=f"frame-{index}",
                alias_id=None,
                decode_session_id="decode-1",
                decoded_frame_utc=requested + timedelta(seconds=1),
                decoded_pts=index + 1,
                decoded_ordinal=index + 1,
                support_group_id=None,
                support_index=None,
                is_phase6_baseline=False,
                classification=state,
            )
        )
    snapshot = D2EvidenceSnapshot(
        investigation_id="investigation-1",
        search_run_id="search-run-1",
        phase6_confirmation_id="confirmation-1",
        baseline_observation_id="baseline-observation",
        plan_id=plan.plan_id,
        policy_identity=policy_identity(policy),
        source_revision=D2SourceRevision(
            manifest_digest="a" * 64,
            c2_bracket_id="coarse-bracket-1",
            d1_source_bracket_id="coarse-bracket-1",
        ),
        references=tuple(references),
        support_groups=(),
    )
    digest = evidence_snapshot_digest(snapshot)
    if indeterminate_index is None:
        c2 = C2NoCandidate(tuple(snapshot.references[1:]), digest)
    else:
        c2 = C2VisualInconclusive(
            VisualStopReason.INSUFFICIENT_VISUAL_EVIDENCE,
            (snapshot.references[indeterminate_index + 1],),
            digest,
        )
    return TerminalInputSnapshot(snapshot, plan, policy, c2)


def test_complete_inclusive_present_grid_produces_not_found() -> None:
    outcome = interpret_terminal(_context())

    assert isinstance(outcome, NotFoundResult)
    assert outcome.result_kind is TerminalResultKind.NOT_FOUND
    assert outcome.terminal_reason == "no_transition_in_window"
    assert outcome.search_end_utc == "2026-07-20T03:00:04Z"
    assert outcome.result_id == terminal_result_id(outcome)


def test_missing_end_target_cannot_prove_not_found() -> None:
    context = _context()
    c2 = context.c2_result
    assert isinstance(c2, C2NoCandidate)
    missing = replace(
        context,
        c2_result=replace(c2, complete_present_grid=c2.complete_present_grid[:-1]),
    )

    outcome = interpret_terminal(missing)

    assert isinstance(outcome, OperationalOutcome)
    assert outcome.reason is OperationalStopReason.CORRUPT_PERSISTED_EVIDENCE


def test_visual_indeterminate_is_inconclusive_not_operational() -> None:
    outcome = interpret_terminal(_context(indeterminate_index=1))

    assert isinstance(outcome, InconclusiveResult)
    assert outcome.result_kind is TerminalResultKind.INCONCLUSIVE
    assert outcome.visual_reason is VisualStopReason.INSUFFICIENT_VISUAL_EVIDENCE


def test_operational_stop_never_becomes_visual_terminal() -> None:
    context = _context()
    operational = replace(
        context,
        c2_result=C2OperationalStop(OperationalStopReason.TIMEOUT, ("target-0",)),
    )

    outcome = interpret_terminal(operational)

    assert isinstance(outcome, OperationalOutcome)
    assert outcome.reason is OperationalStopReason.TIMEOUT


def test_incomplete_narrowing_is_nonterminal() -> None:
    context = _context()
    bracket_ready = replace(context, d1_result=None)
    c2 = C2BracketReady(
        cast("CoarseCandidateBracket", object()),
        evidence_snapshot_digest(context.evidence_snapshot),
    )

    outcome = interpret_terminal(replace(bracket_ready, c2_result=c2))

    assert isinstance(outcome, NonTerminalOutcome)


def test_terminal_identity_changes_with_semantic_result_kind_only() -> None:
    context = _context()
    not_found = interpret_terminal(context)
    assert isinstance(not_found, NotFoundResult)
    altered = replace(not_found, result_kind=TerminalResultKind.FOUND)

    assert terminal_result_id(altered) != terminal_result_id(not_found)


def test_forged_snapshot_digest_fails_closed() -> None:
    context = _context()
    c2 = context.c2_result
    assert isinstance(c2, C2NoCandidate)

    outcome = interpret_terminal(
        replace(context, c2_result=replace(c2, evidence_snapshot_digest="f"))
    )

    assert isinstance(outcome, OperationalOutcome)
    assert outcome.reason is OperationalStopReason.CORRUPT_PERSISTED_EVIDENCE


def test_result_identity_excludes_runtime_result_id() -> None:
    outcome = interpret_terminal(_context())
    assert isinstance(outcome, NotFoundResult)

    altered = replace(outcome, result_id="runtime-value")

    assert terminal_result_id(altered) == terminal_result_id(outcome)
