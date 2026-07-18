import pytest

from vigi_vision.profiles import (
    PROFILE_REGISTRY,
    CounterAnalysis,
    UnknownProfileError,
    get_profile,
    parse_profile_analysis,
)


def test_profile_registry_returns_counter_schema() -> None:
    # Given

    # When
    profile = get_profile("counter")

    # Then
    assert profile.response_model is CounterAnalysis
    assert profile.response_model.model_json_schema()["additionalProperties"] is False


@pytest.mark.parametrize(
    ("profile_name", "expected_fields"),
    [
        (
            "counter",
            {
                "staff_visible",
                "customer_visible",
                "customer_at_counter",
                "counter_occupied",
                "possible_payment_interaction",
            },
        ),
        (
            "dining",
            {
                "estimated_people_count",
                "occupied_tables",
                "empty_tables",
                "standing_people",
                "crowd_level",
            },
        ),
        (
            "entrance",
            {
                "estimated_shoe_pairs_on_rack",
                "estimated_shoe_pairs_on_floor",
                "person_near_entrance",
                "entrance_clear",
                "scattered_footwear",
            },
        ),
    ],
)
def test_profile_registry_defines_required_structured_fields(
    profile_name: str, expected_fields: set[str]
) -> None:
    # Given
    profile = get_profile(profile_name)

    # When
    schema_fields = set(profile.response_model.model_fields)

    # Then
    assert schema_fields >= expected_fields
    assert set(PROFILE_REGISTRY) == {"counter", "dining", "entrance"}


def test_profile_parser_validates_counter_response() -> None:
    # Given
    raw_response = (
        '{"staff_visible":true,"customer_visible":true,"customer_at_counter":true,'
        '"counter_occupied":true,"possible_payment_interaction":true,'
        '"notable_observations":["A customer is at the counter."],'
        '"limitations":"A single frame cannot confirm payment."}'
    )

    # When
    analysis = parse_profile_analysis(raw_response, get_profile("counter"))

    # Then
    assert isinstance(analysis, CounterAnalysis)
    assert analysis.possible_payment_interaction is True


def test_profile_registry_rejects_unknown_profile() -> None:
    # Given

    # When / Then
    with pytest.raises(UnknownProfileError, match="counter, dining, entrance"):
        _ = get_profile("warehouse")
