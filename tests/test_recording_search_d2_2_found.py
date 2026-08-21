from __future__ import annotations

from datetime import datetime, timedelta, timezone

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_c1_planner import (
    CoarseSamplingIdentity,
    build_coarse_sampling_plan,
)
from vigi_vision.recording_search_c2_models import CoarseCandidateBracket
from vigi_vision.recording_search_d1_history import (
    D1BracketState,
    history_digest,
    narrowed_bracket_id,
)
from vigi_vision.recording_search_d1_identity import (
    build_d1_input_bracket,
    d1_input_bracket_id,
    source_bracket_identity,
)
from vigi_vision.recording_search_d1_models import (
    NarrowedBracket,
    NarrowingBoundEvidence,
    NarrowingStopReason,
)
from vigi_vision.recording_search_d2_enums import D2EvidenceRole
from vigi_vision.recording_search_d2_evidence import (
    D2EvidenceReference,
    D2EvidenceSnapshot,
    D2SourceRevision,
    D2SupportGroup,
)
from vigi_vision.recording_search_d2_identity import evidence_snapshot_digest
from vigi_vision.recording_search_d2_results import C2BracketReady, D1BracketReady
from vigi_vision.recording_search_d2_terminal import (
    FoundResult,
    TerminalInputSnapshot,
    TerminalResultKind,
    interpret_terminal,
)
from vigi_vision.recording_search_models import default_policy

UTC = timezone.utc
START = datetime(2026, 7, 20, 3, 0, tzinfo=UTC)
MANIFEST_DIGEST = "a" * 64


def _found_context(*, support_count: int = 3) -> TerminalInputSnapshot:
    policy = default_policy(START, START + timedelta(seconds=8)).model_copy(
        update={
            "absence_confirmation_frames": support_count,
            "coarse_interval_seconds": 1,
            "binary_stop_resolution_seconds": 4,
        }
    )
    plan = build_coarse_sampling_plan(policy)
    identity = CoarseSamplingIdentity(
        "investigation-1", "search-run-1", "confirmation-1", "baseline-identity"
    )
    support_times = tuple(START + timedelta(seconds=value) for value in range(4, 4 + support_count))
    support_ids = tuple(f"probe-support-{index}" for index in range(support_count))
    observation_ids = tuple(f"observation-support-{index}" for index in range(support_count))
    frame_ids = tuple(f"frame-support-{index}" for index in range(support_count))
    bracket = CoarseCandidateBracket(
        investigation_id=identity.investigation_id,
        search_run_id=identity.search_run_id,
        identity=identity,
        plan_id=plan.plan_id,
        policy_version=policy.policy_version,
        baseline_observation_id="baseline-observation",
        last_present_observation_id="observation-lower",
        last_present_probe_request_id="probe-lower",
        last_present_canonical_frame_id="frame-lower",
        last_present_requested_time_utc=START,
        first_absent_requested_time_utc=support_times[0],
        support_target_times=support_times,
        support_probe_request_ids=support_ids,
        support_observation_ids=observation_ids,
        support_canonical_frame_ids=frame_ids,
        support_decode_session_id="decode-1",
        support_decoded_frame_times=support_times,
        support_decoded_pts=tuple(range(4, 4 + support_count)),
        support_decoded_ordinals=tuple(range(4, 4 + support_count)),
        manifest_digest=MANIFEST_DIGEST,
        last_present_is_baseline=False,
        last_present_target_id="target-lower",
        support_group_id="coarse-support-1",
    )
    d1_input = build_d1_input_bracket(
        bracket,
        phase6_confirmation_id=identity.phase6_confirmation_id,
        baseline_identity=identity.baseline_identity,
        policy=policy,
    )
    lower = NarrowingBoundEvidence(
        target_id="target-lower",
        requested_time_utc=START,
        state=ClassificationOutcome.PRESENT,
        observation_id="observation-lower",
        probe_request_id="probe-lower",
        canonical_frame_id="frame-lower",
        operation_id="operation-lower",
        decode_session_id="decode-1",
        decoded_frame_utc=START,
        decoded_pts=1,
        decoded_ordinal=1,
        is_baseline=False,
    )
    support = tuple(
        NarrowingBoundEvidence(
            target_id=f"target-support-{index}",
            requested_time_utc=requested,
            state=ClassificationOutcome.ABSENT,
            observation_id=observation_ids[index],
            probe_request_id=support_ids[index],
            canonical_frame_id=frame_ids[index],
            operation_id=f"operation-support-{index}",
            decode_session_id="decode-1",
            decoded_frame_utc=requested,
            decoded_pts=index + 4,
            decoded_ordinal=index + 4,
        )
        for index, requested in enumerate(support_times)
    )
    final_state_value = D1BracketState(
        START,
        support_times[0],
        d1_input.lower_bound,
        d1_input.upper_support.support_group_id,
    )
    history_value = history_digest(d1_input, d1_input_bracket_id(d1_input), ())
    narrowed_id = narrowed_bracket_id(
        d1_input,
        (),
        final_state_value,
        history_value,
        0,
        4,
        NarrowingStopReason.TARGET_PRECISION_REACHED.value,
        MANIFEST_DIGEST,
        source_bracket=bracket,
    )
    final = NarrowedBracket(
        investigation_id=identity.investigation_id,
        search_run_id=identity.search_run_id,
        phase6_confirmation_id=identity.phase6_confirmation_id,
        baseline_identity=identity.baseline_identity,
        source_bracket_id=source_bracket_identity(bracket),
        policy_version=policy.policy_version,
        lower_bound_utc=START,
        upper_bound_utc=support_times[0],
        lower_evidence=lower,
        upper_support_evidence=support,
        target_ids=(),
        evidence=(),
        iterations=0,
        achieved_precision_seconds=4,
        stop_reason=NarrowingStopReason.TARGET_PRECISION_REACHED,
        manifest_digest=MANIFEST_DIGEST,
        d1_input_bracket=d1_input,
        source_bracket=bracket,
        upper_support_group_id=d1_input.upper_support.support_group_id,
        history=(),
        history_digest=history_value,
        narrowed_bracket_id=narrowed_id,
    )
    references = (
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
        ),
        D2EvidenceReference(
            role=D2EvidenceRole.COARSE_TARGET,
            target_id="target-lower",
            requested_time_utc=START,
            acquisition_operation_id="operation-lower",
            probe_request_id="probe-lower",
            classification_operation_id="classification-lower",
            observation_id="observation-lower",
            canonical_frame_id="frame-lower",
            alias_id=None,
            decode_session_id="decode-1",
            decoded_frame_utc=START,
            decoded_pts=1,
            decoded_ordinal=1,
            support_group_id=None,
            support_index=None,
            is_phase6_baseline=False,
            classification=ClassificationOutcome.PRESENT,
        ),
        *tuple(
            D2EvidenceReference(
                role=D2EvidenceRole.ABSENCE_SUPPORT,
                target_id=f"target-support-{index}",
                requested_time_utc=requested,
                acquisition_operation_id=f"operation-support-{index}",
                probe_request_id=support_ids[index],
                classification_operation_id=f"classification-support-{index}",
                observation_id=observation_ids[index],
                canonical_frame_id=frame_ids[index],
                alias_id=None,
                decode_session_id="decode-1",
                decoded_frame_utc=requested,
                decoded_pts=index + 4,
                decoded_ordinal=index + 4,
                support_group_id=d1_input.upper_support.support_group_id,
                support_index=index,
                is_phase6_baseline=False,
                classification=ClassificationOutcome.ABSENT,
            )
            for index, requested in enumerate(support_times)
        ),
    )
    snapshot = D2EvidenceSnapshot(
        investigation_id=identity.investigation_id,
        search_run_id=identity.search_run_id,
        phase6_confirmation_id=identity.phase6_confirmation_id,
        baseline_observation_id="baseline-observation",
        plan_id=plan.plan_id,
        policy_identity=d1_input.policy_identity,
        source_revision=D2SourceRevision(
            manifest_digest=MANIFEST_DIGEST,
            c2_bracket_id=source_bracket_identity(bracket),
            d1_source_bracket_id=source_bracket_identity(bracket),
        ),
        references=references,
        support_groups=(
            D2SupportGroup(
                support_group_id=d1_input.upper_support.support_group_id,
                origin_target_id="target-support-0",
                support_count=support_count,
                cadence_seconds=1,
                decode_session_id="decode-1",
                member_target_ids=tuple(
                    f"target-support-{index}" for index in range(support_count)
                ),
                member_observation_ids=observation_ids,
                member_canonical_frame_ids=frame_ids,
            ),
        ),
    )
    digest = evidence_snapshot_digest(snapshot)
    return TerminalInputSnapshot(
        snapshot,
        plan,
        policy,
        C2BracketReady(bracket, digest),
        D1BracketReady(final, digest),
    )


def test_valid_found_candidate_produces_requested_interval() -> None:
    outcome = interpret_terminal(_found_context())

    assert isinstance(outcome, FoundResult)
    assert outcome.result_kind is TerminalResultKind.FOUND
    assert outcome.lower_bound_requested_time_utc == "2026-07-20T03:00:00Z"
    assert outcome.upper_bound_requested_time_utc == "2026-07-20T03:00:04Z"
    assert outcome.achieved_precision_seconds == 4


def test_non_default_absence_support_count_remains_found() -> None:
    outcome = interpret_terminal(_found_context(support_count=2))

    assert isinstance(outcome, FoundResult)
    assert len(outcome.upper_support) == 2
