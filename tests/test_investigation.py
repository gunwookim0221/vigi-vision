from datetime import datetime, timedelta, timezone
from typing import NoReturn

import pytest

from vigi_vision.investigation import (
    AnchorTime,
    CameraAssignment,
    CameraRole,
    InvalidAnchorTimeError,
    InvalidInvestigationDefinitionError,
    InvestigationPlanner,
    MissingRequiredCameraRoleError,
    RelativeWindow,
    Scenario,
    ScenarioCameraRule,
    parse_kst_anchor,
    validate_scenario_profiles,
)
from vigi_vision.profiles import UnknownProfileError


def _role(value: str) -> CameraRole:
    return CameraRole(value)


def _rule(
    role: str,
    profile_id: str,
    start_offset_seconds: int,
    end_offset_seconds: int,
    *,
    required: bool,
) -> ScenarioCameraRule:
    return ScenarioCameraRule(
        role=_role(role),
        profile_id=profile_id,
        window_policy=RelativeWindow(start_offset_seconds, end_offset_seconds),
        required=required,
    )


def _scenario(*rules: ScenarioCameraRule) -> Scenario:
    return Scenario("restaurant-checkout", rules)


def _anchor() -> AnchorTime:
    return AnchorTime(datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc), "Asia/Seoul")


def test_parse_kst_anchor_normalizes_to_utc() -> None:
    # Given
    entered_time = "2026-07-20 12:00:00"

    # When
    anchor = parse_kst_anchor(entered_time)

    # Then
    assert anchor.anchor_utc == datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc)
    assert anchor.source_timezone == "Asia/Seoul"


@pytest.mark.parametrize("entered_time", ["2026-07-20T12:00:00", "2026-07-20 12:00:00+09:00"])
def test_parse_kst_anchor_rejects_input_outside_declared_format(entered_time: str) -> None:
    # Given / When / Then
    with pytest.raises(InvalidAnchorTimeError):
        _ = parse_kst_anchor(entered_time)


@pytest.mark.parametrize(
    "anchor_utc",
    [
        datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc).replace(tzinfo=None),
        datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc, microsecond=1),
        datetime(2026, 7, 20, 12, 0, tzinfo=timezone(timedelta(hours=9))),
    ],
)
def test_anchor_rejects_noncanonical_utc_instants(anchor_utc: datetime) -> None:
    # Given / When / Then
    with pytest.raises(InvalidAnchorTimeError):
        _ = AnchorTime(anchor_utc, "Asia/Seoul")


def test_planner_applies_relative_offsets_to_existing_recording_windows() -> None:
    # Given
    scenario = validate_scenario_profiles(
        _scenario(_rule("counter", "counter", -60, 60, required=True))
    )
    assignments = (CameraAssignment(1, _role("counter")),)

    # When
    plan = InvestigationPlanner().plan(_anchor(), scenario, assignments)

    # Then
    assert plan.items[0].recording_window.start_utc == datetime(
        2026, 7, 20, 2, 59, tzinfo=timezone.utc
    )
    assert plan.items[0].recording_window.end_utc == datetime(
        2026, 7, 20, 3, 1, tzinfo=timezone.utc
    )


def test_planner_rejects_a_missing_required_role() -> None:
    # Given
    scenario = validate_scenario_profiles(
        _scenario(_rule("counter", "counter", -60, 60, required=True))
    )

    # When / Then
    with pytest.raises(MissingRequiredCameraRoleError):
        _ = InvestigationPlanner().plan(_anchor(), scenario, ())


def test_planner_omits_a_missing_optional_role() -> None:
    # Given
    scenario = validate_scenario_profiles(
        _scenario(_rule("dining", "dining", -300, 300, required=False))
    )

    # When
    plan = InvestigationPlanner().plan(_anchor(), scenario, ())

    # Then
    assert plan.items == ()


def test_planner_expands_one_role_to_multiple_sorted_channels() -> None:
    # Given
    scenario = validate_scenario_profiles(
        _scenario(_rule("counter", "counter", -60, 60, required=True))
    )
    assignments = (
        CameraAssignment(7, _role("counter")),
        CameraAssignment(2, _role("counter")),
    )

    # When
    plan = InvestigationPlanner().plan(_anchor(), scenario, assignments)

    # Then
    assert tuple(item.channel_id for item in plan.items) == (2, 7)


def test_planner_ignores_camera_roles_absent_from_the_scenario() -> None:
    # Given
    scenario = validate_scenario_profiles(
        _scenario(_rule("counter", "counter", -60, 60, required=True))
    )
    assignments = (
        CameraAssignment(1, _role("counter")),
        CameraAssignment(2, _role("storage")),
    )

    # When
    plan = InvestigationPlanner().plan(_anchor(), scenario, assignments)

    # Then
    assert tuple(item.channel_id for item in plan.items) == (1,)


def test_scenario_rejects_duplicate_role_rules() -> None:
    # Given / When / Then
    with pytest.raises(InvalidInvestigationDefinitionError):
        _ = _scenario(
            _rule("counter", "counter", -60, 60, required=True),
            _rule("counter", "counter", -30, 30, required=False),
        )


def test_profile_validation_rejects_unknown_profile_ids() -> None:
    # Given
    scenario = _scenario(_rule("storage", "storage", -60, 60, required=True))

    # When / Then
    with pytest.raises(UnknownProfileError):
        _ = validate_scenario_profiles(scenario)


def test_planner_orders_items_by_rule_then_channel_and_derives_stable_safe_ids() -> None:
    # Given
    scenario = validate_scenario_profiles(
        _scenario(
            _rule("entrance", "entrance", -30, 480, required=True),
            _rule("counter", "counter", -60, 60, required=True),
        )
    )
    assignments = (
        CameraAssignment(3, _role("counter")),
        CameraAssignment(1, _role("entrance")),
        CameraAssignment(2, _role("counter")),
    )

    # When
    first_plan = InvestigationPlanner().plan(_anchor(), scenario, assignments)
    second_plan = InvestigationPlanner().plan(_anchor(), scenario, assignments)

    # Then
    assert tuple(item.channel_id for item in first_plan.items) == (1, 2, 3)
    assert tuple(item.item_id for item in first_plan.items) == tuple(
        item.item_id for item in second_plan.items
    )
    assert all("rtsp" not in item.item_id for item in first_plan.items)
    assert all("@" not in item.item_id for item in first_plan.items)
    assert all("/" not in item.item_id for item in first_plan.items)


def test_planner_rejects_conflicting_channel_assignments() -> None:
    # Given
    scenario = validate_scenario_profiles(
        _scenario(_rule("counter", "counter", -60, 60, required=True))
    )
    assignments = (
        CameraAssignment(1, _role("counter")),
        CameraAssignment(1, _role("entrance")),
    )

    # When / Then
    with pytest.raises(InvalidInvestigationDefinitionError):
        _ = InvestigationPlanner().plan(_anchor(), scenario, assignments)


def test_planner_does_not_invoke_collection_or_analysis_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def _forbidden(*_args: NoReturn, **_kwargs: NoReturn) -> NoReturn:
        raise AssertionError

    scenario = validate_scenario_profiles(
        _scenario(_rule("counter", "counter", -60, 60, required=True))
    )
    assignments = (CameraAssignment(1, _role("counter")),)
    monkeypatch.setattr("vigi_vision.recording.RecordingPlanner.connect", _forbidden)
    monkeypatch.setattr("vigi_vision.replay.ReplayExtractor.extract", _forbidden)
    monkeypatch.setattr("vigi_vision.analysis.OpenAiAnalyzer.analyze", _forbidden)

    # When
    plan = InvestigationPlanner().plan(_anchor(), scenario, assignments)

    # Then
    assert len(plan.items) == 1
