from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from vigi_vision.recording_search_successor_acquisition import SuccessorTargetStatus
from vigi_vision.recording_search_successor_candidate_search import (
    SuccessorCandidateInterval,
    SuccessorSearchSample,
)
from vigi_vision.recording_search_successor_classification import (
    SuccessorObservation,
    SuccessorObservationState,
)
from vigi_vision.recording_search_successor_search_evidence import (
    SearchEvidence,
    SearchEvidenceBand,
)
from vigi_vision.recording_search_successor_verification import (
    CandidateVerificationCompletion,
    CandidateVerificationStatus,
    verify_disappearance_candidates,
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _candidate(  # noqa: PLR0913
    *,
    qualified: bool = True,
    provisional: bool | None = None,
    coverage_incomplete: bool = False,
    recovery: str | None = None,
    suffix: str = "a",
    start_seconds: int = 0,
) -> SuccessorCandidateInterval:
    if provisional is None:
        provisional = not qualified
    start = BASE + timedelta(seconds=start_seconds)
    return SuccessorCandidateInterval(
        f"successor-candidate-v1-{suffix}",
        f"anchor-{suffix}",
        f"drop-{suffix}",
        start,
        start + timedelta(seconds=60),
        qualified,
        provisional,
        coverage_incomplete,
        recovery,
        (f"anchor-{suffix}", f"drop-{suffix}"),
    )


def _sample(  # noqa: PLR0913
    observation_id: str,
    seconds: int,
    state: str,
    band: SearchEvidenceBand,
    *,
    actual_seconds: int | None = None,
    frame_missing: bool = False,
) -> SuccessorSearchSample:
    actual = None if frame_missing else (seconds if actual_seconds is None else actual_seconds)
    evidence = (
        SearchEvidence(
            band,
            f"test_{band.value.lower()}",
            scene_stable=True,
            object_degradation=band is SearchEvidenceBand.MATERIAL_DROP,
        )
        if actual is not None
        else None
    )
    return SuccessorSearchSample(
        observation_id,
        None if actual is None else BASE + timedelta(seconds=actual),
        evidence,
        state,
        BASE + timedelta(seconds=seconds),
    )


def _observation(
    observation_id: str,
    seconds: int,
    state: SuccessorObservationState,
    *,
    target_id: str = "target-dedup",
    acquisition_id: str = "acquisition-dedup",
) -> SuccessorObservation:
    actual = BASE + timedelta(seconds=seconds)
    return SuccessorObservation(
        "successor-plan-v1-dedup",
        target_id,
        acquisition_id,
        1,
        actual,
        actual,
        1.0,
        0.0,
        "successor-authority-v1-dedup",
        "reference-dedup",
        "successor-roi-v1-dedup",
        "classifier-policy-dedup",
        SuccessorTargetStatus.FRAME_AVAILABLE,
        state,
        (
            None
            if state.is_visual and state is not SuccessorObservationState.INDETERMINATE
            else "insufficient_visual_evidence"
        ),
        1,
        observation_id,
        frame_sha256="a" * 64,
        classifier_stage="completed",
    )


def test_qualified_present_to_absent_is_verified_internally() -> None:
    candidate = _candidate()
    report = verify_disappearance_candidates(
        (candidate,),
        (
            _sample("anchor-a", 0, "PRESENT", SearchEvidenceBand.STRONG_REFERENCE),
            _sample("drop-a", 60, "ABSENT", SearchEvidenceBand.MATERIAL_DROP),
        ),
    )
    assert report.completion is CandidateVerificationCompletion.COMPLETED
    assert report.results[0].status is CandidateVerificationStatus.VERIFIED
    assert report.results[0].reason_code == "qualified_present_to_absent"
    assert report.metrics.additional_v3_invocations == 0


def test_provisional_and_ambiguous_candidates_are_not_verified() -> None:
    candidate = _candidate(qualified=False, suffix="p")
    report = verify_disappearance_candidates(
        (candidate,),
        (
            _sample("anchor-p", 0, "PRESENT", SearchEvidenceBand.STRONG_REFERENCE),
            _sample("drop-p", 60, "ABSENT", SearchEvidenceBand.MATERIAL_DROP),
        ),
    )
    assert report.results[0].status is CandidateVerificationStatus.PARTIAL
    assert report.results[0].reason_code == "provisional_candidate_requires_later_evidence"


def test_gap_recovery_and_operational_evidence_remain_unresolved() -> None:
    gap = _candidate(coverage_incomplete=True, suffix="g")
    recovery = _candidate(recovery="recovery-r", suffix="r")
    operational = _candidate(suffix="o")
    samples = (
        _sample("anchor-g", 0, "PRESENT", SearchEvidenceBand.STRONG_REFERENCE),
        _sample("drop-g", 60, "ABSENT", SearchEvidenceBand.MATERIAL_DROP),
        _sample("anchor-r", 0, "PRESENT", SearchEvidenceBand.STRONG_REFERENCE),
        _sample("drop-r", 60, "ABSENT", SearchEvidenceBand.MATERIAL_DROP),
        _sample("recovery-r", 70, "PRESENT", SearchEvidenceBand.STRONG_REFERENCE),
        _sample("anchor-o", 0, "CLASSIFIER_TIMEOUT", SearchEvidenceBand.STRONG_REFERENCE),
        _sample("drop-o", 60, "ABSENT", SearchEvidenceBand.MATERIAL_DROP),
    )
    report = verify_disappearance_candidates((gap, recovery, operational), samples)
    assert [item.status for item in report.results] == [
        CandidateVerificationStatus.UNRESOLVED,
        CandidateVerificationStatus.PARTIAL,
        CandidateVerificationStatus.UNRESOLVED,
    ]
    assert report.results[1].reason_code == "recovery_ambiguous"
    assert report.results[2].reason_code == "operational_failure"


def test_multiple_candidates_and_overflow_are_reported_without_collapsing_order() -> None:
    first = _candidate(suffix="1")
    second = _candidate(suffix="2", start_seconds=120)
    report = verify_disappearance_candidates(
        (first, second),
        (
            _sample("anchor-1", 0, "PRESENT", SearchEvidenceBand.STRONG_REFERENCE),
            _sample("drop-1", 60, "ABSENT", SearchEvidenceBand.MATERIAL_DROP),
            _sample("anchor-2", 120, "PRESENT", SearchEvidenceBand.STRONG_REFERENCE),
            _sample("drop-2", 180, "INDETERMINATE", SearchEvidenceBand.USABLE_AMBIGUOUS),
        ),
        overflowed=True,
        overflow_count=2,
    )
    assert [item.candidate.candidate_id for item in report.results] == [
        first.candidate_id,
        second.candidate_id,
    ]
    assert report.results[0].status is CandidateVerificationStatus.VERIFIED
    assert report.results[1].status is CandidateVerificationStatus.PARTIAL
    assert report.metrics.overflowed is True
    assert report.metrics.overflow_count == 2


def test_nonmonotonic_narrowing_keeps_the_candidate_partial() -> None:
    candidate = _candidate(suffix="n")
    report = verify_disappearance_candidates(
        (candidate,),
        (
            _sample("anchor-n", 0, "PRESENT", SearchEvidenceBand.STRONG_REFERENCE),
            _sample("drop-n", 60, "ABSENT", SearchEvidenceBand.MATERIAL_DROP),
        ),
        nonmonotonic_candidate_ids=(candidate.candidate_id,),
    )
    assert report.results[0].status is CandidateVerificationStatus.PARTIAL
    assert report.results[0].reason_code == "nonmonotonic_evidence"


def test_same_timestamp_uncertain_identity_preserves_both_samples() -> None:
    candidate = _candidate(suffix="d")
    duplicate_one = _sample("interior-d1", 20, "INDETERMINATE", SearchEvidenceBand.USABLE_AMBIGUOUS)
    duplicate_two = _sample("interior-d2", 20, "INDETERMINATE", SearchEvidenceBand.USABLE_AMBIGUOUS)
    report = verify_disappearance_candidates(
        (candidate,),
        (
            _sample("anchor-d", 0, "PRESENT", SearchEvidenceBand.STRONG_REFERENCE),
            duplicate_one,
            duplicate_two,
            _sample("drop-d", 60, "ABSENT", SearchEvidenceBand.MATERIAL_DROP),
        ),
    )
    assert report.metrics.actual_frame_count == 4
    assert {"interior-d1", "interior-d2"}.issubset(report.results[0].sample_ids)


def test_proven_identical_observation_id_is_deduplicated() -> None:
    candidate = _candidate(suffix="same")
    duplicate_one = _sample(
        "interior-same", 20, "INDETERMINATE", SearchEvidenceBand.USABLE_AMBIGUOUS
    )
    duplicate_two = _sample(
        "interior-same", 20, "INDETERMINATE", SearchEvidenceBand.USABLE_AMBIGUOUS
    )
    report = verify_disappearance_candidates(
        (candidate,),
        (
            _sample("anchor-same", 0, "PRESENT", SearchEvidenceBand.STRONG_REFERENCE),
            duplicate_one,
            duplicate_two,
            _sample("drop-same", 60, "ABSENT", SearchEvidenceBand.MATERIAL_DROP),
        ),
    )
    assert report.metrics.actual_frame_count == 3
    assert report.results[0].sample_ids.count("interior-same") == 1


def test_proven_identical_frame_provenance_is_deduplicated() -> None:
    anchor = _observation(
        "successor-observation-v1-anchor-dedup",
        0,
        SuccessorObservationState.PRESENT,
    )
    drop = _observation(
        "successor-observation-v1-drop-dedup",
        60,
        SuccessorObservationState.ABSENT,
    )
    duplicate_one = _observation(
        "successor-observation-v1-interior-one",
        20,
        SuccessorObservationState.INDETERMINATE,
    )
    duplicate_two = replace(
        duplicate_one,
        observation_id="successor-observation-v1-interior-two",
    )
    candidate = replace(
        _candidate(suffix="provenance"),
        anchor_observation_id=anchor.observation_id,
        drop_observation_id=drop.observation_id,
        supporting_observation_ids=(anchor.observation_id, drop.observation_id),
    )
    report = verify_disappearance_candidates(
        (candidate,),
        (anchor, duplicate_one, duplicate_two, drop),
    )
    assert report.metrics.actual_frame_count == 3
    assert report.results[0].sample_ids.count(duplicate_one.observation_id) == 1
    assert duplicate_two.observation_id not in report.results[0].sample_ids


def test_same_timestamp_operational_failure_remains_unresolved() -> None:
    candidate = _candidate(suffix="failure")
    report = verify_disappearance_candidates(
        (candidate,),
        (
            _sample("anchor-failure", 0, "PRESENT", SearchEvidenceBand.STRONG_REFERENCE),
            _sample("interior-a", 20, "INDETERMINATE", SearchEvidenceBand.USABLE_AMBIGUOUS),
            _sample("interior-z", 20, "CLASSIFIER_FAILED", SearchEvidenceBand.USABLE_AMBIGUOUS),
            _sample("drop-failure", 60, "ABSENT", SearchEvidenceBand.MATERIAL_DROP),
        ),
    )
    result = report.results[0]
    assert result.status is CandidateVerificationStatus.UNRESOLVED
    assert result.reason_code == "operational_failure"
    assert {"interior-a", "interior-z"}.issubset(result.sample_ids)


def test_same_timestamp_conflicting_classifier_states_remain_visible() -> None:
    candidate = _candidate(suffix="conflict")
    report = verify_disappearance_candidates(
        (candidate,),
        (
            _sample("anchor-conflict", 0, "PRESENT", SearchEvidenceBand.STRONG_REFERENCE),
            _sample("interior-present", 20, "PRESENT", SearchEvidenceBand.STRONG_REFERENCE),
            _sample("interior-absent", 20, "ABSENT", SearchEvidenceBand.MATERIAL_DROP),
            _sample("drop-conflict", 60, "ABSENT", SearchEvidenceBand.MATERIAL_DROP),
        ),
    )
    assert {"interior-present", "interior-absent"}.issubset(report.results[0].sample_ids)


@pytest.mark.parametrize(
    ("anchor_seconds", "drop_seconds", "anchor_actual", "drop_actual"),
    [
        (20, 20, 20, 20),
        (50, 10, 50, 10),
        (0, 60, None, 60),
        (0, 60, 0, None),
        (0, 60, -1, 60),
        (0, 60, 0, 61),
    ],
)
def test_invalid_actual_frame_chronology_cannot_be_verified(
    anchor_seconds: int,
    drop_seconds: int,
    anchor_actual: int | None,
    drop_actual: int | None,
) -> None:
    candidate = _candidate(suffix="chronology")
    report = verify_disappearance_candidates(
        (candidate,),
        (
            _sample(
                "anchor-chronology",
                anchor_seconds,
                "PRESENT",
                SearchEvidenceBand.STRONG_REFERENCE,
                actual_seconds=anchor_actual,
                frame_missing=anchor_actual is None,
            ),
            _sample(
                "drop-chronology",
                drop_seconds,
                "ABSENT",
                SearchEvidenceBand.MATERIAL_DROP,
                actual_seconds=drop_actual,
                frame_missing=drop_actual is None,
            ),
        ),
    )
    result = report.results[0]
    assert result.status is CandidateVerificationStatus.UNRESOLVED
    assert result.reason_code == "invalid_actual_frame_chronology"


def test_cancellation_is_internal_and_returns_no_partial_results() -> None:
    checks = 0

    def cancel() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 2

    report = verify_disappearance_candidates(
        (_candidate(),),
        (
            _sample("anchor-a", 0, "PRESENT", SearchEvidenceBand.STRONG_REFERENCE),
            _sample("drop-a", 60, "ABSENT", SearchEvidenceBand.MATERIAL_DROP),
        ),
        should_cancel=cancel,
    )
    assert report.completion is CandidateVerificationCompletion.CANCELLED
    assert report.results == ()
