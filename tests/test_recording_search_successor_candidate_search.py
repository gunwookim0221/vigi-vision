from __future__ import annotations

from datetime import datetime, timedelta, timezone

from vigi_vision.recording_search_successor_candidate_search import (
    EvidenceNarrowingCancelledError,
    EvidenceNarrowingCompletion,
    EvidenceNarrowingPolicy,
    SuccessorCandidateFormationPolicy,
    SuccessorSearchSample,
    form_disappearance_candidates,
    narrow_candidate_interval,
)
from vigi_vision.recording_search_successor_search_evidence import (
    SearchEvidence,
    SearchEvidenceBand,
)

UTC = timezone.utc
START = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)


def _evidence(band: SearchEvidenceBand, *, scene_only: bool = False) -> SearchEvidence:
    if band is SearchEvidenceBand.STRONG_REFERENCE:
        return SearchEvidence(band, "strong", scene_stable=True)
    if band is SearchEvidenceBand.MATERIAL_DROP:
        return SearchEvidence(
            band,
            "drop",
            scene_stable=True,
            object_degradation=True,
        )
    if scene_only:
        return SearchEvidence(
            SearchEvidenceBand.USABLE_AMBIGUOUS,
            "scene_instability_suppressed_direction",
            scene_stable=False,
            scene_discontinuity=True,
            scene_only_suppressed=True,
        )
    return SearchEvidence(band, band.value.lower(), scene_stable=True)


def _sample(
    index: int,
    band: SearchEvidenceBand | None,
    *,
    state: str | None = None,
    gap: bool = False,
) -> SuccessorSearchSample:
    timestamp = START + timedelta(seconds=index * 100)
    return SuccessorSearchSample(
        f"obs-{index}",
        None if gap else timestamp,
        None if band is None else _evidence(band),
        state,
        timestamp,
        not gap,
        gap,
    )


def test_strong_strong_drop_creates_one_conservative_candidate() -> None:
    result = form_disappearance_candidates(
        [_sample(0, SearchEvidenceBand.STRONG_REFERENCE),
         _sample(1, SearchEvidenceBand.STRONG_REFERENCE),
         _sample(2, SearchEvidenceBand.MATERIAL_DROP)]
    )
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.interval_start_utc == START + timedelta(seconds=100)
    assert candidate.interval_end_utc == START + timedelta(seconds=200)
    assert candidate.provisional


def test_ambiguous_between_anchor_and_drop_does_not_move_directional_state() -> None:
    result = form_disappearance_candidates(
        [_sample(0, SearchEvidenceBand.STRONG_REFERENCE),
         _sample(1, SearchEvidenceBand.USABLE_AMBIGUOUS),
         _sample(2, SearchEvidenceBand.MATERIAL_DROP)]
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].interval_start_utc == START


def test_recovery_preserves_candidate_and_repeated_drop_qualifies_same_run() -> None:
    result = form_disappearance_candidates(
        [_sample(0, SearchEvidenceBand.STRONG_REFERENCE),
         _sample(1, SearchEvidenceBand.MATERIAL_DROP),
         _sample(2, SearchEvidenceBand.STRONG_REFERENCE),
         _sample(3, SearchEvidenceBand.MATERIAL_DROP)]
    )
    assert len(result.candidates) == 2
    assert result.candidates[0].provisional
    assert result.candidates[0].recovery_observation_id == "obs-2"
    assert result.candidates[1].provisional
    repeated = form_disappearance_candidates(
        [_sample(0, SearchEvidenceBand.STRONG_REFERENCE),
         _sample(1, SearchEvidenceBand.MATERIAL_DROP),
         _sample(2, SearchEvidenceBand.MATERIAL_DROP)]
    )
    assert len(repeated.candidates) == 1
    assert repeated.candidates[0].qualified


def test_tail_drop_is_provisional_and_no_anchor_does_not_fabricate_bound() -> None:
    tail = form_disappearance_candidates([_sample(1, SearchEvidenceBand.MATERIAL_DROP)])
    assert len(tail.candidates) == 0
    no_anchor = form_disappearance_candidates(
        [_sample(0, None), _sample(1, SearchEvidenceBand.MATERIAL_DROP)]
    )
    assert len(no_anchor.candidates) == 0


def test_seed_reference_allows_isolated_tail_drop_as_provisional() -> None:
    result = form_disappearance_candidates(
        [_sample(1, SearchEvidenceBand.MATERIAL_DROP)],
        seed_reference_time_utc=START,
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].provisional


def test_scene_only_and_state_only_indeterminate_do_not_create_candidate() -> None:
    scene = SuccessorSearchSample(
        "scene",
        START + timedelta(seconds=100),
        _evidence(SearchEvidenceBand.USABLE_AMBIGUOUS, scene_only=True),
        "INDETERMINATE",
    )
    state_only = _sample(2, None, state="INDETERMINATE")
    result = form_disappearance_candidates(
        [_sample(0, SearchEvidenceBand.STRONG_REFERENCE), scene, state_only]
    )
    assert not result.candidates


def test_indeterminate_with_directional_evidence_is_usable() -> None:
    strong = _sample(0, SearchEvidenceBand.STRONG_REFERENCE, state="INDETERMINATE")
    drop = _sample(1, SearchEvidenceBand.MATERIAL_DROP, state="INDETERMINATE")
    result = form_disappearance_candidates([strong, drop])
    assert len(result.candidates) == 1


def test_insufficient_and_gaps_preserve_uncertainty() -> None:
    result = form_disappearance_candidates(
        [_sample(0, SearchEvidenceBand.STRONG_REFERENCE),
         _sample(1, None, gap=True),
         _sample(2, SearchEvidenceBand.MATERIAL_DROP)]
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].coverage_incomplete


def test_operational_sample_between_bounds_marks_coverage_incomplete() -> None:
    result = form_disappearance_candidates(
        [_sample(0, SearchEvidenceBand.STRONG_REFERENCE),
         SuccessorSearchSample(
             "obs-1",
             START + timedelta(seconds=100),
             None,
             "REPLAY_TIMEOUT",
             START + timedelta(seconds=100),
             available=False,
         ),
         _sample(2, SearchEvidenceBand.MATERIAL_DROP)]
    )
    assert result.candidates[0].coverage_incomplete
    narrowed = narrow_candidate_interval(result.candidates[0], lambda _: None)
    assert narrowed.completion is EvidenceNarrowingCompletion.GAP
    assert narrowed.reason_code == "candidate_gap"
    assert narrowed.iterations == 0


def test_candidate_overflow_is_explicit_and_ordered() -> None:
    samples: list[SuccessorSearchSample] = []
    for index in range(7):
        samples.extend(
            [_sample(index * 3, SearchEvidenceBand.STRONG_REFERENCE),
             _sample(index * 3 + 1, SearchEvidenceBand.MATERIAL_DROP),
             _sample(index * 3 + 2, SearchEvidenceBand.STRONG_REFERENCE)]
        )
    result = form_disappearance_candidates(
        samples,
        policy=SuccessorCandidateFormationPolicy(maximum_candidates=3),
    )
    assert len(result.candidates) == 3
    assert result.overflowed
    assert result.overflow_count == 4
    assert len(result.overflow_candidate_ids) == 4


def test_strong_midpoint_moves_left_and_drop_midpoint_moves_right() -> None:
    candidate = form_disappearance_candidates(
        [_sample(0, SearchEvidenceBand.STRONG_REFERENCE),
         _sample(1, SearchEvidenceBand.MATERIAL_DROP),
         _sample(2, SearchEvidenceBand.MATERIAL_DROP)]
    ).candidates[0]
    calls = 0

    def sample(midpoint: datetime) -> SuccessorSearchSample:
        nonlocal calls
        calls += 1
        band = (
            SearchEvidenceBand.STRONG_REFERENCE
            if calls == 1
            else SearchEvidenceBand.MATERIAL_DROP
        )
        actual = midpoint + timedelta(seconds=10 if calls == 1 else -10)
        return SuccessorSearchSample(f"mid-{calls}", actual, _evidence(band))

    result = narrow_candidate_interval(
        candidate,
        sample,
        policy=EvidenceNarrowingPolicy(target_width_seconds=1, maximum_iterations=2),
    )
    assert result.interval_start_utc > candidate.interval_start_utc
    assert result.interval_end_utc < candidate.interval_end_utc
    assert result.iterations == 2


def test_ambiguous_insufficient_gap_and_cancel_stop_without_false_direction() -> None:
    candidate = form_disappearance_candidates(
        [_sample(0, SearchEvidenceBand.STRONG_REFERENCE),
         _sample(1, SearchEvidenceBand.MATERIAL_DROP),
         _sample(2, SearchEvidenceBand.MATERIAL_DROP)]
    ).candidates[0]
    for band, completion in (
        (SearchEvidenceBand.USABLE_AMBIGUOUS, EvidenceNarrowingCompletion.AMBIGUOUS),
        (SearchEvidenceBand.INSUFFICIENT, EvidenceNarrowingCompletion.INSUFFICIENT),
    ):
        result = narrow_candidate_interval(
            candidate,
            lambda midpoint, band=band: SuccessorSearchSample(
                "mid", midpoint, _evidence(band)
            ),
        )
        assert result.completion is completion
        assert result.interval_start_utc == candidate.interval_start_utc
        assert result.interval_end_utc == candidate.interval_end_utc
    gap = narrow_candidate_interval(candidate, lambda _midpoint: None)
    assert gap.completion is EvidenceNarrowingCompletion.GAP
    assert gap.reason_code == "midpoint_gap"
    cancelled = narrow_candidate_interval(
        candidate, lambda _midpoint: None, should_cancel=lambda: True
    )
    assert cancelled.completion is EvidenceNarrowingCompletion.CANCELLED

    def cancelled_sampler(_midpoint: datetime) -> SuccessorSearchSample:
        raise EvidenceNarrowingCancelledError

    callback_cancelled = narrow_candidate_interval(candidate, cancelled_sampler)
    assert callback_cancelled.completion is EvidenceNarrowingCompletion.CANCELLED
    assert callback_cancelled.reason_code == "cancelled"


def test_nonmonotonic_and_no_progress_are_safe() -> None:
    candidate = form_disappearance_candidates(
        [_sample(0, SearchEvidenceBand.STRONG_REFERENCE),
         _sample(1, SearchEvidenceBand.MATERIAL_DROP),
         _sample(2, SearchEvidenceBand.MATERIAL_DROP)]
    ).candidates[0]
    calls = 0

    def nonmonotonic(midpoint: datetime) -> SuccessorSearchSample:
        nonlocal calls
        calls += 1
        band = (
            SearchEvidenceBand.MATERIAL_DROP
            if calls == 1
            else SearchEvidenceBand.STRONG_REFERENCE
        )
        return SuccessorSearchSample(f"n-{calls}", midpoint, _evidence(band))

    result = narrow_candidate_interval(
        candidate,
        nonmonotonic,
        policy=EvidenceNarrowingPolicy(target_width_seconds=1, maximum_iterations=3),
    )
    assert result.completion is EvidenceNarrowingCompletion.NONMONOTONIC
    assert result.interval_start_utc == candidate.interval_start_utc
    assert result.interval_end_utc == candidate.interval_end_utc
    no_progress = narrow_candidate_interval(
        candidate,
        lambda _midpoint: SuccessorSearchSample(
            "same", candidate.interval_start_utc, _evidence(SearchEvidenceBand.STRONG_REFERENCE)
        ),
    )
    assert no_progress.completion is EvidenceNarrowingCompletion.NO_PROGRESS
