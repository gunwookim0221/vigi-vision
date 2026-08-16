from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NoReturn

import pytest
from tests.test_recording_search_a2 import successful_a2_run

from vigi_vision.object_presence_values import ClassificationOutcome
from vigi_vision.recording_search_c1_models import (
    CoarseSampleResult,
    CoarseSampleStatus,
    CoarseSamplingResult,
    CoarseSupportResult,
)
from vigi_vision.recording_search_c1_planner import (
    CoarseSamplingIdentity,
    build_coarse_sampling_plan,
    confirmation_run_id_for,
)
from vigi_vision.recording_search_c2_interpreter import interpret_coarse_evidence
from vigi_vision.recording_search_c2_models import (
    CoarseEvidenceSnapshot,
    CoarseInterpretationStatus,
    CoarseTargetEvidence,
)
from vigi_vision.recording_search_models import default_policy

_UTC = timezone.utc
_START = datetime(2026, 7, 20, 3, 0, tzinfo=_UTC)
_NO_ALIASES: frozenset[int] = frozenset()
_IDENTITY = CoarseSamplingIdentity(
    "object-disappearance-v3-ch1-20260720T030000Z",
    "search-run-abcdef12",
    "object-disappearance-v3-ch1-20260720T030000Z",
    "baseline-test",
)


def _snapshot(  # noqa: PLR0913 - keeps test policy variants explicit.
    states: tuple[ClassificationOutcome, ...],
    *,
    aliases: frozenset[int] = _NO_ALIASES,
    sessions: tuple[str, ...] | None = None,
    maximum_consecutive_unusable: int = 3,
    baseline_requested_time: datetime | None = None,
    baseline_observation_id: str = "baseline-test",
    absence_confirmation_frames: int = 3,
) -> CoarseEvidenceSnapshot:
    policy = default_policy(_START, _START + timedelta(seconds=8)).model_copy(
        update={
            "coarse_interval_seconds": 1,
            "maximum_consecutive_indeterminate_targets": maximum_consecutive_unusable,
            "absence_confirmation_frames": absence_confirmation_frames,
        }
    )
    plan = build_coarse_sampling_plan(policy)
    session_values = sessions or ("decode-session-test",) * len(plan.target_times)
    samples = tuple(
        CoarseSampleResult(
            requested_time_utc=target,
            status=CoarseSampleStatus.SUCCESS,
            probe_request_id=f"probe-request-{index:02d}",
            classification=state,
        )
        for index, (target, state) in enumerate(zip(plan.target_times, states, strict=True), 1)
    )
    targets = tuple(
        CoarseTargetEvidence(
            requested_time_utc=target,
            status=CoarseSampleStatus.SUCCESS,
            classification=state,
            probe_request_id=f"probe-request-{index:02d}",
            observation_id=f"observation-{index:02d}",
            canonical_frame_id=f"frame-{index:02d}",
            decode_session_id=session_values[index - 1],
            decoded_frame_utc=target + timedelta(microseconds=100_000),
            decoded_pts=index,
            decoded_ordinal=index,
            is_alias=index in aliases,
        )
        for index, (target, state) in enumerate(zip(plan.target_times, states, strict=True), 1)
    )
    support_results: list[CoarseSupportResult] = []
    support_targets: list[CoarseTargetEvidence] = []
    targets_by_time = {target.requested_time_utc: target for target in targets}
    for origin_index, origin in enumerate(plan.target_times):
        if origin_index + absence_confirmation_frames - 1 >= len(states):
            continue
        if any(
            state is not ClassificationOutcome.ABSENT
            for state in states[origin_index : origin_index + absence_confirmation_frames]
        ):
            continue
        confirmation_id = confirmation_run_id_for(plan, origin, _IDENTITY)
        support_samples = [samples[origin_index]]
        support_evidence = [
            replace(
                targets[origin_index],
                origin_coarse_target_utc=origin,
                confirmation_run_id=confirmation_id,
                support_identity=_IDENTITY,
            )
        ]
        for offset in range(1, absence_confirmation_frames):
            support_target = origin + offset * plan.absence_cadence_seconds * timedelta(seconds=1)
            target = targets_by_time.get(support_target)
            if target is None:
                support_samples = []
                break
            support_samples.append(
                CoarseSampleResult(
                    requested_time_utc=support_target,
                    status=CoarseSampleStatus.SUCCESS,
                    probe_request_id=target.probe_request_id,
                    classification=ClassificationOutcome.ABSENT,
                )
            )
            support_evidence.append(
                replace(
                    target,
                    origin_coarse_target_utc=origin,
                    confirmation_run_id=confirmation_id,
                    support_identity=_IDENTITY,
                )
            )
        if not support_samples:
            continue
        support_results.append(
            CoarseSupportResult(
                identity=_IDENTITY,
                origin_target_utc=origin,
                confirmation_run_id=confirmation_id,
                support_indices=tuple(range(absence_confirmation_frames)),
                samples=tuple(support_samples),
            )
        )
        support_targets.extend(support_evidence)
    execution = CoarseSamplingResult(
        identity=_IDENTITY,
        plan=plan,
        samples=samples,
        complete=True,
        support_results=tuple(support_results),
    )
    return CoarseEvidenceSnapshot(
        investigation_id="object-disappearance-v3-ch1-20260720T030000Z",
        search_run_id="search-run-abcdef12",
        identity=_IDENTITY,
        plan=plan,
        policy_version=policy.policy_version,
        absence_confirmation_frames=policy.absence_confirmation_frames,
        absence_cadence_seconds=policy.absence_cadence_seconds,
        baseline_observation_id=baseline_observation_id,
        manifest_digest="a" * 64,
        execution=execution,
        targets=targets + tuple(support_targets),
        maximum_consecutive_indeterminate_targets=maximum_consecutive_unusable,
        baseline_requested_time_utc=baseline_requested_time,
    )


def test_qualifying_absence_returns_exact_nonpersistent_bracket() -> None:
    result = interpret_coarse_evidence(
        _snapshot(
            (
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.ABSENT,
            )
        )
    )

    assert result.status is CoarseInterpretationStatus.BRACKET_READY
    assert result.bracket is not None
    assert result.bracket.last_present_requested_time_utc == _START + timedelta(seconds=1)
    assert result.bracket.first_absent_requested_time_utc == _START + timedelta(seconds=2)
    assert result.bracket.support_target_times == (
        _START + timedelta(seconds=2),
        _START + timedelta(seconds=3),
        _START + timedelta(seconds=4),
    )


@pytest.mark.parametrize("support_count", [1, 2, 4])
def test_interpreter_accepts_each_positive_support_count(support_count: int) -> None:
    result = interpret_coarse_evidence(
        _snapshot(
            (ClassificationOutcome.PRESENT,) + (ClassificationOutcome.ABSENT,) * 7,
            absence_confirmation_frames=support_count,
        )
    )

    assert result.status is CoarseInterpretationStatus.BRACKET_READY
    assert result.bracket is not None
    assert len(result.bracket.support_target_times) == support_count
    assert len(result.bracket.support_probe_request_ids) == support_count


def test_all_present_evidence_returns_no_candidate() -> None:
    result = interpret_coarse_evidence(_snapshot((ClassificationOutcome.PRESENT,) * 8))

    assert result.status is CoarseInterpretationStatus.NO_CANDIDATE
    assert result.safe_reason == "no_supported_transition"
    assert result.bracket is None


def test_first_supported_absence_without_present_lower_bound_is_inconclusive() -> None:
    result = interpret_coarse_evidence(
        _snapshot(
            (
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
            )
        )
    )

    assert result.status is CoarseInterpretationStatus.INCONCLUSIVE
    assert result.safe_reason == "missing_present_lower_bound"


def test_validated_phase6_baseline_is_the_initial_present_lower_bound() -> None:
    result = interpret_coarse_evidence(
        _snapshot(
            (ClassificationOutcome.ABSENT,) * 8,
            baseline_requested_time=_START,
        )
    )

    assert result.status is CoarseInterpretationStatus.BRACKET_READY
    assert result.bracket is not None
    assert result.bracket.last_present_is_baseline is True
    assert result.bracket.last_present_observation_id == "baseline-test"
    assert result.bracket.last_present_probe_request_id is None
    assert result.bracket.last_present_canonical_frame_id is None


def test_corrupt_baseline_identity_fails_closed() -> None:
    with pytest.raises(ValueError, match=r"^$"):
        _ = _snapshot(
            (ClassificationOutcome.ABSENT,) * 8,
            baseline_requested_time=_START,
            baseline_observation_id="",
        )


def test_support_identity_mismatch_fails_closed() -> None:
    snapshot = _snapshot(
        (
            ClassificationOutcome.PRESENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.PRESENT,
            ClassificationOutcome.PRESENT,
            ClassificationOutcome.PRESENT,
            ClassificationOutcome.PRESENT,
        )
    )
    tampered_targets = tuple(
        replace(
            target,
            support_identity=replace(_IDENTITY, baseline_identity="foreign-baseline"),
        )
        if target.origin_coarse_target_utc is not None
        else target
        for target in snapshot.targets
    )

    result = interpret_coarse_evidence(replace(snapshot, targets=tampered_targets))

    assert result.status is CoarseInterpretationStatus.CORRUPT
    assert result.safe_reason == "authoritative_evidence_invalid"


def test_reordered_targets_recompute_the_same_result() -> None:
    snapshot = _snapshot(
        (
            ClassificationOutcome.PRESENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
        )
    )

    ordered = interpret_coarse_evidence(snapshot)
    reordered = interpret_coarse_evidence(
        replace(snapshot, targets=tuple(reversed(snapshot.targets)))
    )

    assert reordered == ordered


def test_nonmonotonic_decoded_provenance_cannot_support_absence() -> None:
    snapshot = _snapshot(
        (
            ClassificationOutcome.PRESENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
        )
    )
    invalid_targets = tuple(
        replace(target, decoded_pts=1)
        if target.classification is ClassificationOutcome.ABSENT
        else target
        for target in snapshot.targets
    )

    result = interpret_coarse_evidence(replace(snapshot, targets=invalid_targets))

    assert result.status is CoarseInterpretationStatus.INCONCLUSIVE
    assert result.bracket is None


def test_recording_gap_cannot_bridge_to_absence_support() -> None:
    snapshot = _snapshot(
        (
            ClassificationOutcome.PRESENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
            ClassificationOutcome.ABSENT,
        )
    )
    gap_target = snapshot.targets[1]
    gap_sample = replace(
        snapshot.execution.samples[1],
        status=CoarseSampleStatus.RECORDING_UNAVAILABLE,
        classification=None,
        probe_request_id=None,
        safe_reason="recording_unavailable",
    )
    execution = replace(
        snapshot.execution,
        samples=(snapshot.execution.samples[0], gap_sample, *snapshot.execution.samples[2:]),
        support_results=(),
    )
    targets = (
        snapshot.targets[0],
        replace(
            gap_target,
            status=CoarseSampleStatus.RECORDING_UNAVAILABLE,
            classification=None,
            probe_request_id=None,
            observation_id=None,
            canonical_frame_id=None,
            decode_session_id=None,
            decoded_frame_utc=None,
            decoded_pts=None,
            decoded_ordinal=None,
        ),
        *snapshot.targets[2:],
    )

    result = interpret_coarse_evidence(
        replace(
            snapshot,
            execution=execution,
            targets=tuple(target for target in targets if target.origin_coarse_target_utc is None),
        )
    )

    assert result.status is CoarseInterpretationStatus.INCONCLUSIVE
    assert result.bracket is None


def test_incomplete_and_interrupted_execution_never_produce_bracket() -> None:
    snapshot = _snapshot((ClassificationOutcome.PRESENT,) * 8)
    incomplete = replace(
        snapshot,
        execution=CoarseSamplingResult(
            identity=snapshot.identity,
            plan=snapshot.plan,
            samples=snapshot.execution.samples[:2],
            complete=False,
        ),
    )
    interrupted = replace(
        incomplete,
        execution=CoarseSamplingResult(
            identity=snapshot.identity,
            plan=snapshot.plan,
            samples=(
                CoarseSampleResult(
                    requested_time_utc=snapshot.plan.target_times[0],
                    status=CoarseSampleStatus.INTERRUPTED,
                    safe_reason="inactive_handle",
                ),
            ),
            complete=False,
        ),
    )

    assert interpret_coarse_evidence(incomplete).status is CoarseInterpretationStatus.INCOMPLETE
    assert interpret_coarse_evidence(interrupted).status is CoarseInterpretationStatus.INTERRUPTED


def test_alias_or_invalid_session_cannot_satisfy_absence_support() -> None:
    aliased = interpret_coarse_evidence(
        _snapshot(
            (
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.ABSENT,
            ),
            aliases=frozenset({3}),
        )
    )
    invalid_session = interpret_coarse_evidence(
        _snapshot(
            (
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
            ),
            sessions=(
                "decode-session-test",
                "decode-session-test",
                "other-session",
                "decode-session-test",
                "decode-session-test",
                "decode-session-test",
                "decode-session-test",
                "decode-session-test",
            ),
        )
    )

    assert aliased.status is CoarseInterpretationStatus.INCONCLUSIVE
    assert invalid_session.status is CoarseInterpretationStatus.INCONCLUSIVE


def test_indeterminate_and_nonmonotonic_evidence_fail_closed() -> None:
    indeterminate = interpret_coarse_evidence(
        _snapshot(
            (
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.INDETERMINATE,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
            )
        )
    )
    nonmonotonic = interpret_coarse_evidence(
        _snapshot(
            (
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.ABSENT,
                ClassificationOutcome.ABSENT,
            )
        )
    )

    assert indeterminate.status is CoarseInterpretationStatus.INCONCLUSIVE
    assert nonmonotonic.status is CoarseInterpretationStatus.INCONCLUSIVE


def test_indeterminate_threshold_uses_its_independent_policy_counter() -> None:
    result = interpret_coarse_evidence(
        _snapshot(
            (
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.INDETERMINATE,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
            ),
            maximum_consecutive_unusable=1,
        )
    )

    assert result.status is CoarseInterpretationStatus.INCONCLUSIVE
    assert result.safe_reason == "maximum_consecutive_unusable_targets"


def test_indeterminate_limit_above_support_count_remains_independent() -> None:
    result = interpret_coarse_evidence(
        _snapshot(
            (
                ClassificationOutcome.INDETERMINATE,
                ClassificationOutcome.INDETERMINATE,
                ClassificationOutcome.INDETERMINATE,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
                ClassificationOutcome.PRESENT,
            ),
            maximum_consecutive_unusable=4,
        )
    )

    assert result.status is CoarseInterpretationStatus.INCONCLUSIVE
    assert result.safe_reason == "insufficient_visual_evidence"


def test_production_service_snapshot_is_read_only_and_does_not_execute_work(tmp_path: Path) -> None:
    service, _investigation_id, handle, _manifest, _request = successful_a2_run(tmp_path)
    try:
        plan = service.build_coarse_plan(handle)
        execution = CoarseSamplingResult(
            identity=CoarseSamplingIdentity(
                handle.investigation_id,
                handle.search_run_id,
                handle.phase6_confirmation_id,
                handle.baseline_identity,
            ),
            plan=plan,
            samples=tuple(
                CoarseSampleResult(
                    requested_time_utc=target,
                    status=CoarseSampleStatus.RECORDING_UNAVAILABLE,
                    safe_reason="recording_unavailable",
                )
                for target in plan.target_times
            ),
            complete=True,
        )
        manifest_path = (
            service.repository.run_path(handle.investigation_id, handle.search_run_id)
            / "manifest.json"
        )
        before = manifest_path.read_bytes()
        monkeypatch = pytest.MonkeyPatch()

        def _unexpected(*_args: str) -> NoReturn:
            raise AssertionError

        monkeypatch.setattr(type(service), "acquire_targets", _unexpected)
        monkeypatch.setattr(type(service), "classify", _unexpected)

        result = service.interpret_coarse_sampling(handle, execution)

        assert result.status is CoarseInterpretationStatus.INCONCLUSIVE
        assert manifest_path.read_bytes() == before
        monkeypatch.undo()
        foreign_execution = replace(
            execution,
            identity=replace(execution.identity, search_run_id="search-run-foreign"),
        )
        foreign_result = service.interpret_coarse_sampling(handle, foreign_execution)
        assert foreign_result.status is CoarseInterpretationStatus.CORRUPT
    finally:
        handle.release()
