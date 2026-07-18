import pytest

from vigi_vision.profiles import (
    PROFILE_REGISTRY,
    CounterAnalysis,
    ProfileResponseError,
    UnknownProfileError,
    get_profile,
    parse_profile_analysis,
    resolve_profile_alias,
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
                "profile",
                "summary",
                "confidence",
                "evidence",
                "recommendations",
                "limitations",
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
                "profile",
                "summary",
                "confidence",
                "evidence",
                "recommendations",
                "limitations",
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
                "profile",
                "summary",
                "confidence",
                "evidence",
                "recommendations",
                "limitations",
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
        '{"profile":"counter","staff_visible":true,"customer_visible":true,'
        '"customer_at_counter":true,'
        '"counter_occupied":true,"possible_payment_interaction":true,'
        '"summary":"A customer appears to be at the counter.",'
        '"confidence":"moderate",'
        '"evidence":["A customer appears positioned directly in front of the counter."],'
        '"recommendations":["If confirmation of payment is required, review a short '
        'video segment."],'
        '"notable_observations":["A customer is at the counter."],'
        '"limitations":"A single frame cannot confirm payment."}'
    )

    # When
    analysis = parse_profile_analysis(raw_response, get_profile("counter"))

    # Then
    assert isinstance(analysis, CounterAnalysis)
    assert analysis.profile == "counter"
    assert analysis.possible_payment_interaction is True
    assert analysis.confidence == "moderate"


def test_profile_summary_rejects_more_than_two_sentences() -> None:
    # Given
    raw_response = (
        '{"profile":"counter","staff_visible":true,"customer_visible":true,'
        '"customer_at_counter":true,'
        '"counter_occupied":true,"possible_payment_interaction":true,'
        '"summary":"First observation. Second observation. Third observation.",'
        '"confidence":"moderate","evidence":[],"recommendations":[],'
        '"notable_observations":[],"limitations":"One still frame."}'
    )

    # When / Then
    with pytest.raises(ProfileResponseError):
        _ = parse_profile_analysis(raw_response, get_profile("counter"))


@pytest.mark.parametrize(
    ("alias", "canonical_name"),
    [
        ("counter", "counter"),
        ("카운터", "counter"),
        ("dining", "dining"),
        ("홀", "dining"),
        ("식사공간", "dining"),
        ("entrance", "entrance"),
        ("입구", "entrance"),
        ("신발장", "entrance"),
    ],
)
def test_profile_alias_resolution_returns_canonical_english_id(
    alias: str, canonical_name: str
) -> None:
    # Given

    # When
    resolved_name = resolve_profile_alias(alias)

    # Then
    assert resolved_name == canonical_name
    assert get_profile(resolved_name).name == canonical_name


def test_profile_registry_rejects_unknown_profile() -> None:
    # Given

    # When / Then
    with pytest.raises(UnknownProfileError, match="counter, dining, entrance"):
        _ = get_profile("warehouse")
