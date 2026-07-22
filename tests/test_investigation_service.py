from collections.abc import MutableSequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypeAlias, final

import pytest

from vigi_vision.investigation import (
    AnchorTime,
    CameraAssignment,
    CameraRole,
    InvestigationItem,
    InvestigationPlan,
    MissingRequiredCameraRoleError,
    RelativeWindow,
    Scenario,
    ScenarioCameraRule,
)
from vigi_vision.investigation_artifacts import (
    InvestigationArtifactError,
    InvestigationResult,
)
from vigi_vision.investigation_collection import (
    CollectionItemResult,
    CollectionResult,
    CollectionStatus,
)
from vigi_vision.investigation_manifest import InvestigationManifest
from vigi_vision.investigation_service import InvestigationRequest, InvestigationService
from vigi_vision.recording import RecordingWindow
from vigi_vision.replay import ReplayClip

PlannerOutcome: TypeAlias = InvestigationPlan | Exception
CollectorOutcome: TypeAlias = CollectionResult | Exception
ArtifactOutcome: TypeAlias = InvestigationResult | Exception


@final
class StubPlanner:
    calls: int
    received: tuple[AnchorTime, Scenario, tuple[CameraAssignment, ...]] | None

    def __init__(self, outcome: PlannerOutcome, events: MutableSequence[str]) -> None:
        self.outcome = outcome
        self.events = events
        self.calls = 0
        self.received = None

    def plan(
        self,
        anchor_time: AnchorTime,
        scenario: Scenario,
        assignments: tuple[CameraAssignment, ...],
    ) -> InvestigationPlan:
        self.calls += 1
        self.received = (anchor_time, scenario, assignments)
        self.events.append("plan")
        match self.outcome:  # noqa: RUF100  # noqa: MATCH_OK — test outcome is closed.
            case InvestigationPlan() as plan:
                return plan
            case Exception() as error:
                raise error


@final
class StubCollector:
    calls: int
    received: InvestigationPlan | None

    def __init__(self, outcome: CollectorOutcome, events: MutableSequence[str]) -> None:
        self.outcome = outcome
        self.events = events
        self.calls = 0
        self.received = None

    def collect(self, investigation_plan: InvestigationPlan) -> CollectionResult:
        self.calls += 1
        self.received = investigation_plan
        self.events.append("collect")
        match self.outcome:  # noqa: RUF100  # noqa: MATCH_OK — test outcome is closed.
            case CollectionResult() as result:
                return result
            case Exception() as error:
                raise error


@final
class StubArtifactBuilder:
    calls: int
    received: CollectionResult | None

    def __init__(self, outcome: ArtifactOutcome, events: MutableSequence[str]) -> None:
        self.outcome = outcome
        self.events = events
        self.calls = 0
        self.received = None

    def build(self, collection_result: CollectionResult) -> InvestigationResult:
        self.calls += 1
        self.received = collection_result
        self.events.append("artifact")
        match self.outcome:  # noqa: RUF100  # noqa: MATCH_OK — test outcome is closed.
            case InvestigationResult() as result:
                return result
            case Exception() as error:
                raise error


def _request() -> InvestigationRequest:
    role = CameraRole("counter")
    scenario = Scenario(
        "restaurant-checkout",
        (ScenarioCameraRule(role, "counter", RelativeWindow(-60, 60), required=True),),
    )
    return InvestigationRequest(
        AnchorTime(datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc), "Asia/Seoul"),
        scenario,
        (CameraAssignment(1, role),),
    )


def _plan() -> InvestigationPlan:
    request = _request()
    start_utc = request.anchor_time.anchor_utc - timedelta(seconds=60)
    first_window = RecordingWindow(1, start_utc, request.anchor_time.anchor_utc)
    second_window = RecordingWindow(
        2,
        request.anchor_time.anchor_utc - timedelta(seconds=30),
        request.anchor_time.anchor_utc + timedelta(seconds=30),
    )
    role = request.camera_assignments[0].role
    return InvestigationPlan(
        request.scenario.scenario_id,
        request.anchor_time,
        (
            InvestigationItem("counter-one", 1, role, "counter", first_window),
            InvestigationItem("counter-two", 2, role, "counter", second_window),
        ),
    )


def _partial_collection(plan: InvestigationPlan) -> CollectionResult:
    failed_item, successful_item = plan.items
    successful_window = successful_item.recording_window
    replay_clip = ReplayClip(
        successful_item.channel_id,
        successful_window.start_utc,
        successful_window.end_utc,
        "rtsp://nvr.example.test/replay/2",
        Path("temporary-replay.mp4"),
        successful_window.duration_seconds,
    )
    return CollectionResult(
        plan,
        (
            CollectionItemResult.failure(
                failed_item,
                CollectionStatus.RECORDING_UNAVAILABLE,
                "Recording unavailable.",
            ),
            CollectionItemResult.success(successful_item, replay_clip),
        ),
    )


def _result(plan: InvestigationPlan, collection: CollectionResult) -> InvestigationResult:
    manifest = InvestigationManifest(
        "restaurant-checkout-20260720T030000Z",
        plan.scenario_id,
        plan.anchor_time.anchor_utc,
        plan.anchor_time.source_timezone,
        (),
    )
    return InvestigationResult(plan, collection, manifest, Path("artifacts/investigation"))


def test_service_orchestrates_each_existing_boundary_once_in_order() -> None:
    # Given
    plan = _plan()
    collection = CollectionResult(plan, ())
    expected = _result(plan, collection)
    events: list[str] = []
    planner = StubPlanner(plan, events)
    collector = StubCollector(collection, events)
    artifacts = StubArtifactBuilder(expected, events)

    # When
    result = InvestigationService(planner, collector, artifacts).execute(_request())

    # Then
    assert result is expected
    assert events == ["plan", "collect", "artifact"]
    assert planner.calls == collector.calls == artifacts.calls == 1
    assert planner.received == (
        _request().anchor_time,
        _request().scenario,
        _request().camera_assignments,
    )
    assert collector.received is plan
    assert artifacts.received is collection


def test_service_returns_planner_failures_without_collecting_or_building() -> None:
    # Given
    plan = _plan()
    collection = CollectionResult(plan, ())
    events: list[str] = []
    planner = StubPlanner(MissingRequiredCameraRoleError(CameraRole("counter")), events)
    collector = StubCollector(collection, events)
    artifacts = StubArtifactBuilder(_result(plan, collection), events)

    # When / Then
    with pytest.raises(MissingRequiredCameraRoleError):
        _ = InvestigationService(planner, collector, artifacts).execute(_request())
    assert events == ["plan"]
    assert collector.calls == artifacts.calls == 0


def test_service_passes_partial_collection_results_unchanged_to_artifact_building() -> None:
    # Given
    plan = _plan()
    partial_collection = _partial_collection(plan)
    expected = _result(plan, partial_collection)
    events: list[str] = []
    planner = StubPlanner(plan, events)
    collector = StubCollector(partial_collection, events)
    artifacts = StubArtifactBuilder(expected, events)

    # When
    result = InvestigationService(planner, collector, artifacts).execute(_request())

    # Then
    assert result.collection_result is partial_collection
    assert artifacts.received is partial_collection
    assert tuple(item.collection_status for item in partial_collection.items) == (
        CollectionStatus.RECORDING_UNAVAILABLE,
        CollectionStatus.SUCCESS,
    )
    assert collector.calls == artifacts.calls == 1


def test_service_propagates_artifact_failures_after_one_plan_and_collection() -> None:
    # Given
    plan = _plan()
    collection = CollectionResult(plan, ())
    events: list[str] = []
    planner = StubPlanner(plan, events)
    collector = StubCollector(collection, events)
    artifacts = StubArtifactBuilder(InvestigationArtifactError("snapshot failed"), events)

    # When / Then
    with pytest.raises(InvestigationArtifactError):
        _ = InvestigationService(planner, collector, artifacts).execute(_request())
    assert events == ["plan", "collect", "artifact"]
    assert planner.calls == collector.calls == artifacts.calls == 1
