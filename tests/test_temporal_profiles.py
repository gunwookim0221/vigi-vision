from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from vigi_vision.temporal_profiles import (
    CounterTemporalAnalysis,
    TemporalProfileResponseError,
    TemporalReferenceError,
    get_temporal_profile,
    parse_temporal_profile_analysis,
)
from vigi_vision.video import FrameRecord


def _frames() -> tuple[FrameRecord, ...]:
    return (
        FrameRecord(1, 0, "Frame 1 — 00:00.0", Path("frame-001.jpg")),
        FrameRecord(2, 3_000, "Frame 2 — 00:03.0", Path("frame-002.jpg")),
    )


def _complete_counter_response() -> str:
    return (
        '{"profile":"counter","summary":"The counter remains clear.","confidence":"moderate",'
        '"evidence":[{"frame_indices":[1],"timestamps":[0.0],'
        '"description":"The counter is visible."}],'
        '"observed_changes":[{"from_frame":1,"to_frame":2,'
        '"description":"No visible counter change is sampled."}],'
        '"possible_events":[{"description":"A brief service interaction may have occurred.",'
        '"supporting_frames":[1,2],'
        '"confidence_note":"Possible only; sparse samples cannot confirm it."}],'
        '"unresolved_temporal_questions":["The unsampled interval is unknown."],'
        '"recommendations":[{"basis":"limitation",'
        '"description":"Review more continuous footage if confirmation is needed."}],'
        '"limitations":"Sparse samples do not establish continuity.",'
        '"video_duration_seconds":3.0,"sampled_frame_count":2,"sampled_timestamps":[0.0,3.0],'
        '"counter_occupancy_trend":"no_change_visible",'
        '"customer_counter_presence":"not_visible",'
        '"possible_service_interaction":false,"possible_exchange_sequence":false}'
    )


def test_temporal_profile_parser_accepts_complete_nested_counter_response() -> None:
    # Given
    raw_response = _complete_counter_response()

    # When
    analysis = parse_temporal_profile_analysis(
        raw_response, get_temporal_profile("counter"), _frames()
    )

    # Then
    assert analysis.evidence[0].frame_indices == (1,)
    assert analysis.observed_changes[0].to_frame == 2
    assert analysis.possible_events[0].supporting_frames == (1, 2)
    assert analysis.recommendations[0].basis == "limitation"


def test_temporal_profile_parser_retains_safe_invalid_field_locations() -> None:
    # Given
    raw_response = '{"profile":"counter"}'

    # When / Then
    with pytest.raises(TemporalProfileResponseError) as exception_info:
        _ = parse_temporal_profile_analysis(
            raw_response, get_temporal_profile("counter"), _frames()
        )

    assert "summary" in exception_info.value.validation_fields
    assert "evidence" in exception_info.value.validation_fields


def test_temporal_profile_parser_rejects_extra_response_fields() -> None:
    # Given
    raw_response = (
        '{"profile":"counter","summary":"The samples are quiet.","confidence":"low",'
        '"evidence":[],"observed_changes":[],"possible_events":[],'
        '"unresolved_temporal_questions":[],"recommendations":[],"limitations":"Sparse samples.",'
        '"video_duration_seconds":3.0,"sampled_frame_count":2,"sampled_timestamps":[0.0,3.0],'
        '"unexpected":true}'
    )

    # When / Then
    with pytest.raises(TemporalProfileResponseError):
        _ = parse_temporal_profile_analysis(
            raw_response, get_temporal_profile("counter"), _frames()
        )


def test_temporal_contract_rejects_unsampled_references() -> None:
    # Given
    raw_response = (
        '{"profile":"counter","summary":"The samples are quiet.","confidence":"low",'
        '"evidence":[{"frame_indices":[3],"timestamps":[6.0],'
        '"description":"A counter is visible."}],'
        '"observed_changes":[],"possible_events":[],"unresolved_temporal_questions":[],'
        '"recommendations":[],"limitations":"Sparse samples.","video_duration_seconds":3.0,'
        '"sampled_frame_count":2,"sampled_timestamps":[0.0,3.0],'
        '"counter_occupancy_trend":"no_change_visible",'
        '"customer_counter_presence":"not_visible",'
        '"possible_service_interaction":false,"possible_exchange_sequence":false}'
    )

    # When / Then
    with pytest.raises(TemporalReferenceError):
        _ = parse_temporal_profile_analysis(
            raw_response, get_temporal_profile("counter"), _frames()
        )


def test_observed_change_requires_chronological_sampled_frames() -> None:
    # Given
    response = {
        "profile": "counter",
        "summary": "The counter is visible.",
        "confidence": "low",
        "evidence": [],
        "observed_changes": [{"from_frame": 2, "to_frame": 1, "description": "An invalid order."}],
        "possible_events": [],
        "unresolved_temporal_questions": [],
        "recommendations": [],
        "limitations": "Sparse samples.",
        "video_duration_seconds": 3.0,
        "sampled_frame_count": 2,
        "sampled_timestamps": [0.0, 3.0],
        "counter_occupancy_trend": "no_change_visible",
        "customer_counter_presence": "not_visible",
        "possible_service_interaction": False,
        "possible_exchange_sequence": False,
    }

    # When / Then
    with pytest.raises(ValueError, match="move forward"):
        _ = CounterTemporalAnalysis.model_validate(response)


def test_temporal_response_error_survives_a_context_manager() -> None:
    # Given
    @contextmanager
    def sampling_context() -> Generator[object, None, None]:
        yield object()

    # When / Then
    error = TemporalProfileResponseError("counter", "ValidationError")
    with pytest.raises(TemporalProfileResponseError), sampling_context():
        raise error
