from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from vigi_vision.recording_search_c1_planner import (
    CoarseSamplingIdentity,
    CoarseSamplingPlan,
    build_coarse_sampling_plan,
)
from vigi_vision.recording_search_c2_models import CoarseCandidateBracket
from vigi_vision.recording_search_d1_planner import (
    maximum_narrowing_iterations,
    midpoint_target,
    source_bracket_identity,
)
from vigi_vision.recording_search_models import RecordingSearchPolicy, default_policy

UTC = timezone.utc
START = datetime(2026, 7, 20, 3, 0, tzinfo=UTC)
IDENTITY = CoarseSamplingIdentity(
    "object-disappearance-v3-ch1-20260720T030000Z",
    "search-run-abcdef12",
    "object-disappearance-v3-ch1-20260720T030000Z",
    "baseline-test",
)


def _plan(end_seconds: int = 20) -> CoarseSamplingPlan:
    return build_coarse_sampling_plan(
        default_policy(START, START + timedelta(seconds=end_seconds)).model_copy(
            update={"coarse_interval_seconds": 1}
        )
    )


def _policy(end_seconds: int = 20) -> RecordingSearchPolicy:
    return default_policy(START, START + timedelta(seconds=end_seconds))


def _bracket(lower_seconds: int = 0, upper_seconds: int = 11) -> CoarseCandidateBracket:
    upper = START + timedelta(seconds=upper_seconds)
    support_times = tuple(upper + timedelta(seconds=index) for index in range(3))
    return CoarseCandidateBracket(
        investigation_id=IDENTITY.investigation_id,
        search_run_id=IDENTITY.search_run_id,
        identity=IDENTITY,
        plan_id=_plan(20).plan_id,
        policy_version="recording-search-mvp-v1",
        baseline_observation_id="baseline-test",
        last_present_observation_id="observation-present",
        last_present_probe_request_id="probe-request-present",
        last_present_canonical_frame_id="frame-present",
        last_present_requested_time_utc=START + timedelta(seconds=lower_seconds),
        first_absent_requested_time_utc=upper,
        support_target_times=support_times,
        support_probe_request_ids=(
            "probe-request-absent-0",
            "probe-request-absent-1",
            "probe-request-absent-2",
        ),
        support_observation_ids=(
            "observation-absent-0",
            "observation-absent-1",
            "observation-absent-2",
        ),
        support_canonical_frame_ids=("frame-absent-0", "frame-absent-1", "frame-absent-2"),
        support_decode_session_id="decode-session-test",
        support_decoded_frame_times=tuple(
            value + timedelta(microseconds=100_000) for value in support_times
        ),
        support_decoded_pts=(1, 2, 3),
        support_decoded_ordinals=(1, 2, 3),
        manifest_digest="a" * 64,
    )


def test_midpoint_uses_floor_for_odd_whole_second_intervals() -> None:
    target = midpoint_target(_bracket(0, 11), _policy(), iteration=0)

    assert target is not None
    assert target.requested_time_utc == START + timedelta(seconds=5)


def test_midpoint_identity_is_deterministic_and_iteration_bound() -> None:
    bracket = _bracket(0, 10)
    first = midpoint_target(bracket, _policy(), iteration=0)
    second = midpoint_target(bracket, _policy(), iteration=0)
    changed = midpoint_target(bracket, _policy(), iteration=1)

    assert first == second
    assert first is not None
    assert changed is not None
    assert first.target_id != changed.target_id


def test_midpoint_stops_when_interval_reaches_policy_precision() -> None:
    target = midpoint_target(_bracket(0, 1), _policy(), iteration=0)

    assert target is None


def test_maximum_iterations_are_finite_and_deterministic() -> None:
    assert maximum_narrowing_iterations(11, 1) == 4
    assert maximum_narrowing_iterations(11, 1) == maximum_narrowing_iterations(11, 1)


@pytest.mark.parametrize(
    ("lower", "upper"),
    [
        (START.replace(tzinfo=None), START + timedelta(seconds=2)),
        (START + timedelta(seconds=3), START + timedelta(seconds=2)),
        (START, START),
    ],
)
def test_midpoint_rejects_invalid_bracket_times(lower: datetime, upper: datetime) -> None:
    with pytest.raises(ValueError, match=r".*"):
        _ = replace(
            _bracket(),
            last_present_requested_time_utc=lower,
            first_absent_requested_time_utc=upper,
        )


def test_source_bracket_identity_is_stable() -> None:
    bracket = _bracket()

    assert source_bracket_identity(bracket) == source_bracket_identity(bracket)
