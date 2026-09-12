from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from vigi import (
    RecordDay,
    RecordDaysResponse,
    RecordSearchProcessResponse,
    RecordSearchResultsResponse,
)
from vigi import RecordSegment as SdkRecordSegment

from vigi_vision.recording import RecordingPlanner
from vigi_vision.recording_models import RecordingSegment
from vigi_vision.recording_search_c1_planner import build_coarse_sampling_plan
from vigi_vision.recording_search_models import default_policy
from vigi_vision.recording_search_successor import (
    DEFAULT_HORIZON_SECONDS,
    MAXIMUM_HORIZON_SECONDS,
    SuccessorPlanError,
    SuccessorPlanningPolicy,
    SuccessorPlanRequest,
    SuccessorPlanService,
    TargetAvailability,
    build_successor_plan,
)

UTC = timezone.utc
ANCHOR = datetime(2026, 9, 4, 5, 17, 32, tzinfo=UTC)


def _segment(start: datetime, end: datetime, channel_id: int = 1) -> RecordingSegment:
    return RecordingSegment(
        channel_id=channel_id,
        recording_day=start.date(),
        start_epoch_seconds=int(start.timestamp()),
        end_epoch_seconds=int(end.timestamp()),
        start_utc=start,
        end_utc=end,
    )


def _request(duration_seconds: int = 1_800) -> SuccessorPlanRequest:
    return SuccessorPlanRequest(
        channel_id=1,
        anchor_time_utc=ANCHOR,
        search_end_utc=ANCHOR + timedelta(seconds=duration_seconds),
        source_timezone="Asia/Seoul",
    )


def test_one_segment_covers_full_range_and_anchor_is_not_a_target() -> None:
    request = _request()
    plan = build_successor_plan(
        request,
        (_segment(ANCHOR - timedelta(minutes=1), request.search_end_utc + timedelta(seconds=1)),),
    )

    assert len(plan.segments) == 1
    assert not plan.gaps
    assert len(plan.targets) == 3
    assert plan.targets[0].requested_time_utc == ANCHOR + timedelta(minutes=10)
    assert all(item.availability is TargetAvailability.AVAILABLE for item in plan.targets)
    assert all(item.requested_time_utc != ANCHOR for item in plan.targets)


@pytest.mark.parametrize(
    ("duration_seconds", "target_count"),
    [(600, 1), (1_800, 3), (3_600, 6), (7_200, 12)],
)
def test_supported_successor_horizons_keep_deterministic_coarse_grid(
    duration_seconds: int, target_count: int
) -> None:
    request = _request(duration_seconds)
    plan = build_successor_plan(request, (_segment(ANCHOR, request.search_end_utc),))

    assert len(plan.targets) == target_count
    assert plan.targets[-1].requested_time_utc == request.search_end_utc


def test_adjacent_segments_cover_range_without_gap() -> None:
    request = _request()
    segments = tuple(
        _segment(ANCHOR + timedelta(minutes=offset), ANCHOR + timedelta(minutes=offset + 10))
        for offset in (0, 10, 20, 30)
    )

    plan = build_successor_plan(request, segments)

    assert not plan.gaps
    assert [item.segment_id for item in plan.targets] == [
        "segment-20260904T052732Z-20260904T053732Z",
        "segment-20260904T053732Z-20260904T054732Z",
        "segment-20260904T053732Z-20260904T054732Z",
    ]


def test_overlapping_segments_are_sorted_and_target_assignment_is_stable() -> None:
    request = _request()
    earlier = _segment(ANCHOR, ANCHOR + timedelta(minutes=15))
    later = _segment(ANCHOR + timedelta(minutes=5), ANCHOR + timedelta(minutes=20))

    first = build_successor_plan(request, (later, earlier))
    second = build_successor_plan(request, (earlier, later, earlier))

    assert first.plan_id == second.plan_id
    assert first.segments == second.segments
    assert first.targets[0].segment_id == "segment-20260904T051732Z-20260904T053232Z"


def test_plan_identity_changes_for_material_coverage_or_cadence_changes() -> None:
    request = _request()
    complete = _segment(ANCHOR, request.search_end_utc)
    baseline = build_successor_plan(request, (complete,))
    clipped = build_successor_plan(
        request,
        (_segment(ANCHOR, request.search_end_utc - timedelta(seconds=1)),),
    )
    faster = build_successor_plan(
        request,
        (complete,),
        policy=SuccessorPlanningPolicy(coarse_interval_seconds=300),
    )

    assert baseline.plan_id != clipped.plan_id
    assert baseline.plan_id != faster.plan_id


def test_gap_is_explicit_and_targets_inside_it_are_unavailable() -> None:
    request = _request()
    first_end = ANCHOR + timedelta(minutes=10)
    second_start = ANCHOR + timedelta(minutes=20)
    plan = build_successor_plan(
        request,
        (_segment(ANCHOR, first_end), _segment(second_start, request.search_end_utc)),
    )

    assert plan.gaps[0].start_utc == first_end
    assert plan.gaps[0].end_utc == second_start
    assert plan.targets[1].availability is TargetAvailability.AVAILABLE
    assert plan.targets[1].segment_id == "segment-20260904T053732Z-20260904T054732Z"


def test_missing_start_and_end_coverage_are_reported_as_gaps() -> None:
    request = _request()
    plan = build_successor_plan(
        request,
        (_segment(ANCHOR + timedelta(minutes=10), ANCHOR + timedelta(minutes=20)),),
    )

    assert [(gap.start_utc, gap.end_utc) for gap in plan.gaps] == [
        (ANCHOR, ANCHOR + timedelta(minutes=10)),
        (ANCHOR + timedelta(minutes=20), request.search_end_utc),
    ]
    assert plan.targets[0].availability is TargetAvailability.AVAILABLE
    assert plan.targets[1].availability is TargetAvailability.UNAVAILABLE
    assert plan.targets[2].availability is TargetAvailability.UNAVAILABLE


def test_target_on_segment_boundary_uses_segment_starting_at_boundary() -> None:
    request = _request(600)
    boundary = ANCHOR + timedelta(minutes=5)
    segments = (_segment(ANCHOR, boundary), _segment(boundary, request.search_end_utc))

    plan = build_successor_plan(request, segments)

    assert plan.targets[-1].requested_time_utc == request.search_end_utc
    assert plan.targets[-1].segment_id == "segment-20260904T052232Z-20260904T052732Z"


def test_wrong_channel_is_ignored_without_fabricating_coverage() -> None:
    request = _request(600)
    plan = build_successor_plan(
        request,
        (_segment(ANCHOR, request.search_end_utc, channel_id=2),),
    )

    assert not plan.segments
    assert len(plan.gaps) == 1
    assert all(item.availability is TargetAvailability.UNAVAILABLE for item in plan.targets)


def test_default_and_maximum_horizons_are_enforced() -> None:
    request = SuccessorPlanRequest.from_text(
        channel_id=1,
        anchor_time_utc=ANCHOR,
        search_end_time_text=None,
        source_timezone="Asia/Seoul",
        now_utc=ANCHOR + timedelta(hours=3),
    )
    assert request.search_end_utc == ANCHOR + timedelta(seconds=DEFAULT_HORIZON_SECONDS)

    accepted = _request(MAXIMUM_HORIZON_SECONDS)
    plan = build_successor_plan(accepted, ())
    assert plan.horizon_seconds == MAXIMUM_HORIZON_SECONDS
    assert len(plan.targets) == 12


def test_over_maximum_horizon_is_rejected() -> None:
    with pytest.raises(SuccessorPlanError):
        _ = build_successor_plan(_request(MAXIMUM_HORIZON_SECONDS + 1), ())


def test_future_end_and_non_later_end_are_rejected_by_existing_time_boundary() -> None:
    with pytest.raises(SuccessorPlanError):
        _ = SuccessorPlanRequest.from_text(
            channel_id=1,
            anchor_time_utc=ANCHOR,
            search_end_time_text="2026-09-04T14:17:31",
            source_timezone="Asia/Seoul",
            now_utc=ANCHOR + timedelta(hours=3),
        )
    with pytest.raises(SuccessorPlanError):
        _ = SuccessorPlanRequest.from_text(
            channel_id=1,
            anchor_time_utc=ANCHOR,
            search_end_time_text="2026-09-04T18:30:00",
            source_timezone="Asia/Seoul",
            now_utc=ANCHOR + timedelta(hours=3),
        )


class _FakeRecords:
    def __init__(self, segments: tuple[SdkRecordSegment, ...]) -> None:
        self._segments: tuple[SdkRecordSegment, ...] = segments
        self.list_results_calls: list[tuple[int, int, str, int, int]] = []

    def list_days(self, channel_id: int, start_month: str, end_month: str) -> RecordDaysResponse:
        _ = channel_id, start_month, end_month
        return RecordDaysResponse(days=(RecordDay(day="20260904"),), error_code=0)

    def get_free_process(self) -> RecordSearchProcessResponse:
        return RecordSearchProcessResponse(process_id=7, error_code=0)

    def list_results(
        self,
        channel_id: int,
        process_id: int,
        day: str,
        start_index: int = 0,
        end_index: int = 99,
    ) -> RecordSearchResultsResponse:
        self.list_results_calls.append((channel_id, process_id, day, start_index, end_index))
        return RecordSearchResultsResponse(results=self._segments, error_code=0)


class _NoReplayStream:
    def build_replay_url(self, *_args: object, **_kwargs: object) -> str:
        raise AssertionError from None


class _FakeClient:
    def __init__(self, records: _FakeRecords) -> None:
        self.records: _FakeRecords = records
        self.stream: _NoReplayStream = _NoReplayStream()


def test_public_recording_planner_composition_discovers_multiple_segments_without_media() -> None:
    three_minutes = timedelta(minutes=3)
    source_segments: list[SdkRecordSegment] = []
    cursor = ANCHOR
    for index in range(40):
        if index == 10:
            cursor += three_minutes
        source_segments.append(
            SdkRecordSegment(
                start_time=str(int(cursor.timestamp())),
                end_time=str(int((cursor + three_minutes).timestamp())),
            )
        )
        cursor += three_minutes
    records = _FakeRecords(tuple(reversed(source_segments)))
    service = SuccessorPlanService(RecordingPlanner(_FakeClient(records), "nvr.example.test"))

    plan = service.plan(_request(MAXIMUM_HORIZON_SECONDS))

    assert len(plan.segments) > 20
    assert len(plan.gaps) == 1
    assert len({item.segment_id for item in plan.targets if item.segment_id is not None}) > 1
    assert any(item.availability is TargetAvailability.UNAVAILABLE for item in plan.targets)
    assert records.list_results_calls


def test_legacy_600_second_coarse_policy_remains_unchanged() -> None:
    start = datetime(2026, 7, 20, 3, 0, tzinfo=UTC)
    policy = default_policy(start, start + timedelta(seconds=600)).model_copy(
        update={"maximum_search_duration_seconds": 600}
    )

    plan = build_coarse_sampling_plan(policy)

    assert plan.target_times == (start + timedelta(seconds=300), start + timedelta(seconds=600))
