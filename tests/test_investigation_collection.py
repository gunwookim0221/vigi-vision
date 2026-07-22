from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypeAlias, final

import pytest

from vigi_vision.investigation import AnchorTime, CameraRole, InvestigationItem, InvestigationPlan
from vigi_vision.investigation_collection import CollectionStatus, InvestigationCollector
from vigi_vision.recording import RecordingUnavailableError, RecordingWindow, ReplayRequest
from vigi_vision.replay import (
    ReplayAuthenticationError,
    ReplayClip,
    ReplayExtractionError,
    ReplayTimeoutError,
    ReplayUnavailableError,
)

PlannerOutcome: TypeAlias = ReplayRequest | Exception
ExtractorOutcome: TypeAlias = ReplayClip | Exception


@final
class StubRecordingPlanner:
    outcomes: dict[int, PlannerOutcome]
    calls: list[RecordingWindow]

    def __init__(self, outcomes: Mapping[int, PlannerOutcome]) -> None:
        self.outcomes = dict(outcomes)
        self.calls = []

    def plan(self, window: RecordingWindow) -> ReplayRequest:
        self.calls.append(window)
        outcome = self.outcomes[window.channel_id]
        match outcome:  # noqa: RUF100  # noqa: MATCH_OK — PlannerOutcome is closed by this test fake.
            case ReplayRequest():
                return outcome
            case Exception() as error:
                raise error


@final
class StubReplayExtractor:
    outcomes: dict[int, ExtractorOutcome]
    calls: list[ReplayRequest]

    def __init__(self, outcomes: Mapping[int, ExtractorOutcome]) -> None:
        self.outcomes = dict(outcomes)
        self.calls = []

    def extract(self, request: ReplayRequest) -> ReplayClip:
        self.calls.append(request)
        outcome = self.outcomes[request.window.channel_id]
        match outcome:  # noqa: RUF100  # noqa: MATCH_OK — ExtractorOutcome is closed by this test fake.
            case ReplayClip():
                return outcome
            case Exception() as error:
                raise error


def _item(channel_id: int, role: str) -> InvestigationItem:
    start_utc = datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc)
    window = RecordingWindow(channel_id, start_utc, start_utc + timedelta(seconds=30))
    return InvestigationItem(
        item_id=f"restaurant-checkout-channel-{channel_id}-{role}",
        channel_id=channel_id,
        role=CameraRole(role),
        profile_id=role,
        recording_window=window,
    )


def _plan(*items: InvestigationItem) -> InvestigationPlan:
    anchor = AnchorTime(datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc), "Asia/Seoul")
    return InvestigationPlan("restaurant-checkout", anchor, items)


def _request(item: InvestigationItem) -> ReplayRequest:
    return ReplayRequest(item.recording_window, f"rtsp://nvr.example.test/replay/{item.channel_id}")


def _clip(item: InvestigationItem) -> ReplayClip:
    window = item.recording_window
    return ReplayClip(
        channel_id=item.channel_id,
        requested_start_utc=window.start_utc,
        requested_end_utc=window.end_utc,
        replay_url=f"rtsp://nvr.example.test/replay/{item.channel_id}",
        temporary_mp4_path=Path(f"clip-{item.channel_id}.mp4"),
        duration_seconds=window.duration_seconds,
    )


def test_collector_returns_success_for_every_planned_item_in_order() -> None:
    # Given
    first_item = _item(2, "entrance")
    second_item = _item(1, "counter")
    plan = _plan(first_item, second_item)
    recording_planner = StubRecordingPlanner({2: _request(first_item), 1: _request(second_item)})
    first_clip = _clip(first_item)
    second_clip = _clip(second_item)
    replay_extractor = StubReplayExtractor({2: first_clip, 1: second_clip})

    # When
    result = InvestigationCollector(recording_planner, replay_extractor).collect(plan)

    # Then
    assert tuple(item.item_id for item in result.items) == (first_item.item_id, second_item.item_id)
    assert tuple(item.collection_status for item in result.items) == (
        CollectionStatus.SUCCESS,
        CollectionStatus.SUCCESS,
    )
    assert result.items[0].replay_clip is first_clip
    assert result.items[1].replay_clip is second_clip
    assert tuple(window.channel_id for window in recording_planner.calls) == (2, 1)
    assert tuple(request.window.channel_id for request in replay_extractor.calls) == (2, 1)


def test_collector_isolates_replay_unavailable_and_continues_remaining_items() -> None:
    # Given
    unavailable_item = _item(1, "counter")
    successful_item = _item(2, "entrance")
    plan = _plan(unavailable_item, successful_item)
    recording_planner = StubRecordingPlanner(
        {1: _request(unavailable_item), 2: _request(successful_item)}
    )
    replay_extractor = StubReplayExtractor({1: ReplayUnavailableError(), 2: _clip(successful_item)})

    # When
    result = InvestigationCollector(recording_planner, replay_extractor).collect(plan)

    # Then
    assert tuple(item.collection_status for item in result.items) == (
        CollectionStatus.RECORDING_UNAVAILABLE,
        CollectionStatus.SUCCESS,
    )
    assert result.items[0].replay_clip is None
    assert result.items[0].failure_reason == str(ReplayUnavailableError())
    assert result.items[1].replay_clip is not None
    assert tuple(request.window.channel_id for request in replay_extractor.calls) == (1, 2)


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (ReplayAuthenticationError(), CollectionStatus.AUTHENTICATION_FAILED),
        (ReplayTimeoutError(), CollectionStatus.TIMEOUT),
        (ReplayExtractionError(), CollectionStatus.EXTRACTION_FAILED),
    ],
)
def test_collector_classifies_existing_replay_errors(
    error: Exception, expected_status: CollectionStatus
) -> None:
    # Given
    item = _item(1, "counter")
    recording_planner = StubRecordingPlanner({1: _request(item)})
    replay_extractor = StubReplayExtractor({1: error})

    # When
    result = InvestigationCollector(recording_planner, replay_extractor).collect(_plan(item))

    # Then
    assert result.items[0].collection_status is expected_status
    assert result.items[0].failure_reason == str(error)
    assert result.items[0].replay_clip is None


def test_collector_classifies_recording_unavailable_before_extraction() -> None:
    # Given
    item = _item(1, "counter")
    recording_planner = StubRecordingPlanner({1: RecordingUnavailableError()})
    replay_extractor = StubReplayExtractor({})

    # When
    result = InvestigationCollector(recording_planner, replay_extractor).collect(_plan(item))

    # Then
    assert result.items[0].collection_status is CollectionStatus.RECORDING_UNAVAILABLE
    assert replay_extractor.calls == []


def test_collector_isolates_unexpected_errors_without_exposing_exception_text() -> None:
    # Given
    failed_item = _item(1, "counter")
    successful_item = _item(2, "entrance")
    recording_planner = StubRecordingPlanner(
        {1: RuntimeError("rtsp://operator:secret@nvr.example.test"), 2: _request(successful_item)}
    )
    replay_extractor = StubReplayExtractor({2: _clip(successful_item)})

    # When
    result = InvestigationCollector(recording_planner, replay_extractor).collect(
        _plan(failed_item, successful_item)
    )

    # Then
    assert tuple(item.collection_status for item in result.items) == (
        CollectionStatus.UNEXPECTED_ERROR,
        CollectionStatus.SUCCESS,
    )
    assert result.items[0].failure_reason is not None
    assert "secret" not in result.items[0].failure_reason
    assert tuple(window.channel_id for window in recording_planner.calls) == (1, 2)
