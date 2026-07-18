from pathlib import Path
from secrets import token_urlsafe

import httpx
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    OpenAIError,
    PermissionDeniedError,
    RateLimitError,
)
from openai.types.responses.response_input_content_param import ResponseInputContentParam
from pydantic import BaseModel

from vigi_vision.analysis import (
    AnalysisRequestError,
    AnalysisResponseError,
    OpenAiAnalyzer,
    OpenAiErrorMetadata,
    OpenAiFailureKind,
    SceneAnalysis,
    TemporalAnalysisRequest,
    diagnose_openai_error,
    parse_scene_analysis,
)
from vigi_vision.profiles import CounterAnalysis, get_profile
from vigi_vision.temporal_profiles import get_temporal_profile
from vigi_vision.video import FrameRecord, VideoMetadata

_SECRET = token_urlsafe()
_REQUEST = httpx.Request("POST", "https://api.openai.invalid/responses")


def _response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, request=_REQUEST)


def test_parse_scene_analysis_returns_structured_result() -> None:
    # Given
    raw_response = (
        '{"summary":"A quiet entrance.","person_visible":true,'
        '"notable_observations":["A bicycle is parked."],'
        '"limitations":"This is one still frame."}'
    )

    # When
    analysis = parse_scene_analysis(raw_response)

    # Then
    assert analysis.person_visible is True
    assert analysis.summary == "A quiet entrance."


def test_parse_scene_analysis_rejects_invalid_model_response() -> None:
    # Given
    raw_response = '{"summary":"missing required fields"}'

    # When / Then
    with pytest.raises(AnalysisResponseError, match="structured-response parsing"):
        _ = parse_scene_analysis(raw_response)


def test_scene_analysis_schema_prohibits_extra_properties() -> None:
    # Given
    schema = SceneAnalysis.model_json_schema()

    # When / Then
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    ("error", "expected_kind", "expected_status"),
    [
        (
            AuthenticationError(_SECRET, response=_response(401), body=None),
            OpenAiFailureKind.AUTHENTICATION,
            401,
        ),
        (
            PermissionDeniedError(_SECRET, response=_response(403), body=None),
            OpenAiFailureKind.PERMISSION_OR_MODEL_ACCESS,
            403,
        ),
        (
            NotFoundError(_SECRET, response=_response(404), body=None),
            OpenAiFailureKind.PERMISSION_OR_MODEL_ACCESS,
            404,
        ),
        (
            RateLimitError(
                _SECRET,
                response=_response(429),
                body={"error": {"code": "insufficient_quota"}},
            ),
            OpenAiFailureKind.QUOTA_OR_BILLING,
            429,
        ),
        (
            RateLimitError(_SECRET, response=_response(429), body=None),
            OpenAiFailureKind.RATE_LIMIT,
            429,
        ),
        (APITimeoutError(request=_REQUEST), OpenAiFailureKind.TIMEOUT_OR_NETWORK, None),
        (
            APIConnectionError(message=_SECRET, request=_REQUEST),
            OpenAiFailureKind.TIMEOUT_OR_NETWORK,
            None,
        ),
        (
            BadRequestError(_SECRET, response=_response(400), body=None),
            OpenAiFailureKind.INVALID_REQUEST,
            400,
        ),
        (
            APIStatusError(_SECRET, response=_response(500), body=None),
            OpenAiFailureKind.UNEXPECTED_API_FAILURE,
            500,
        ),
    ],
)
def test_diagnose_openai_error_classifies_without_exposing_exception_contents(
    error: OpenAIError,
    expected_kind: OpenAiFailureKind,
    expected_status: int | None,
) -> None:
    # Given

    # When
    diagnostic = diagnose_openai_error(error)

    # Then
    assert diagnostic.kind is expected_kind
    assert diagnostic.exception_type == type(error).__name__
    assert diagnostic.status_code == expected_status
    assert _SECRET not in str(diagnostic)


def test_analyzer_rejects_missing_api_key_before_reading_image() -> None:
    # Given
    analyzer = OpenAiAnalyzer("")

    # When / Then
    with pytest.raises(AnalysisRequestError) as exception_info:
        _ = analyzer.analyze(Path("not-read.jpg"))

    assert exception_info.value.kind is OpenAiFailureKind.MISSING_API_KEY


def test_analyzer_routes_profile_to_its_prompt_and_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given
    image_path = tmp_path / "captured-frame.jpg"
    profile = get_profile("counter")
    raw_response = (
        '{"profile":"counter","staff_visible":false,"customer_visible":false,'
        '"customer_at_counter":false,'
        '"counter_occupied":false,"possible_payment_interaction":false,'
        '"summary":"No customer is visible at the counter.","confidence":"high",'
        '"evidence":["The counter area is visible without a customer."],"recommendations":[],'
        '"notable_observations":[],"limitations":"One still frame."}'
    )

    def _request_analysis(
        _: OpenAiAnalyzer,
        analyzed_path: Path,
        prompt: str,
        response_model: type[BaseModel],
        response_name: str,
    ) -> str:
        assert analyzed_path == image_path
        assert prompt == profile.prompt
        assert response_model is CounterAnalysis
        assert response_name == "counter_analysis"
        return raw_response

    monkeypatch.setattr(OpenAiAnalyzer, "_request_analysis", _request_analysis)

    # When
    analysis = OpenAiAnalyzer(_SECRET).analyze_profile(image_path, profile)

    # Then
    assert isinstance(analysis, CounterAnalysis)
    assert analysis.counter_occupied is False


def test_temporal_analysis_uses_local_metadata_over_model_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given
    image_path = tmp_path / "frame.jpg"
    _ = image_path.write_bytes(b"jpeg")
    request = TemporalAnalysisRequest(
        profile=get_temporal_profile("counter"),
        metadata=VideoMetadata(duration_seconds=3.0, width=1280, height=720),
        frames=(
            FrameRecord(1, 0, "Frame 1 — 00:00.0", image_path),
            FrameRecord(2, 3_000, "Frame 2 — 00:03.0", image_path),
        ),
    )
    raw_response = (
        '{"profile":"counter","summary":"A sparse change is visible.","confidence":"moderate",'
        '"evidence":[{"frame_indices":[1],"timestamps":[0.0],'
        '"description":"At 00:00 the counter is clear."}],"observed_changes":[],'
        '"possible_events":[],"unresolved_temporal_questions":["The interval is unsampled."],'
        '"recommendations":[],"limitations":"Sparse samples.","video_duration_seconds":99.0,'
        '"sampled_frame_count":99,"sampled_timestamps":[99.0],'
        '"counter_occupancy_trend":"no_change_visible",'
        '"customer_counter_presence":"not_visible",'
        '"possible_service_interaction":false,"possible_exchange_sequence":false}'
    )

    def _request_temporal_analysis(
        _: OpenAiAnalyzer, analyzed_request: TemporalAnalysisRequest
    ) -> str:
        assert analyzed_request == request
        return raw_response

    monkeypatch.setattr(OpenAiAnalyzer, "_request_temporal_analysis", _request_temporal_analysis)

    # When
    analysis = OpenAiAnalyzer(_SECRET).analyze_temporal(request)

    # Then
    assert analysis.video_duration_seconds == 3.0
    assert analysis.sampled_frame_count == 2
    assert analysis.sampled_timestamps == (0.0, 3.0)


def test_temporal_request_interleaves_authoritative_labels_and_images(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given
    image_path = tmp_path / "frame.jpg"
    _ = image_path.write_bytes(b"jpeg")
    request = TemporalAnalysisRequest(
        profile=get_temporal_profile("counter"),
        metadata=VideoMetadata(duration_seconds=3.0, width=1280, height=720),
        frames=(
            FrameRecord(1, 0, "Frame 1 — 00:00.0", image_path),
            FrameRecord(2, 3_000, "Frame 2 — 00:03.0", image_path),
        ),
    )
    captured_content: list[ResponseInputContentParam] = []
    raw_response = (
        '{"profile":"counter","summary":"No change is confirmed.","confidence":"low",'
        '"evidence":[],"observed_changes":[],"possible_events":[],'
        '"unresolved_temporal_questions":[],"recommendations":[],"limitations":"Sparse samples.",'
        '"video_duration_seconds":3.0,"sampled_frame_count":2,"sampled_timestamps":[0.0,3.0],'
        '"counter_occupancy_trend":"no_change_visible",'
        '"customer_counter_presence":"not_visible",'
        '"possible_service_interaction":false,"possible_exchange_sequence":false}'
    )

    def _request_content(
        _analyzer: OpenAiAnalyzer,
        content: list[ResponseInputContentParam],
        _response_model: type[BaseModel],
        _response_name: str,
    ) -> str:
        nonlocal captured_content
        captured_content.extend(content)
        return raw_response

    monkeypatch.setattr(OpenAiAnalyzer, "_request_content", _request_content)

    # When
    _ = OpenAiAnalyzer(_SECRET).analyze_temporal(request)

    # Then
    assert tuple(item["type"] for item in captured_content) == (
        "input_text",
        "input_text",
        "input_image",
        "input_text",
        "input_image",
    )
    assert captured_content[1] == {"type": "input_text", "text": "Frame 1 — 00:00.0"}
    assert captured_content[3] == {"type": "input_text", "text": "Frame 2 — 00:03.0"}


def test_request_error_includes_safe_openai_metadata() -> None:
    # Given
    error = AnalysisRequestError(
        OpenAiFailureKind.INVALID_REQUEST,
        "BadRequestError",
        OpenAiErrorMetadata(400, "invalid_json_schema", "text.format.schema", "req_safe"),
    )

    # When
    message = str(error)

    # Then
    assert "[HTTP 400]" in message
    assert "[code invalid_json_schema]" in message
    assert "[param text.format.schema]" in message
    assert "[request req_safe]" in message


@pytest.mark.parametrize(
    ("code", "param", "expected_code", "expected_param"),
    [
        (
            "invalid_json_schema",
            "text.format.schema",
            "invalid_json_schema",
            "text.format.schema",
        ),
        ("data:image/jpeg;base64,private", "sk-private-api-key", None, None),
    ],
)
def test_diagnose_openai_error_redacts_sensitive_status_metadata(
    code: str,
    param: str,
    expected_code: str | None,
    expected_param: str | None,
) -> None:
    # Given
    error = BadRequestError(_SECRET, response=_response(400), body=None)
    error.code = code
    error.param = param

    # When
    diagnostic = diagnose_openai_error(error)

    # Then
    assert diagnostic.metadata.code == expected_code
    assert diagnostic.metadata.param == expected_param
    match expected_code:
        case str() as safe_code:
            assert f"[code {safe_code}]" in str(diagnostic)
        case None:
            assert code not in str(diagnostic)
    match expected_param:
        case str() as safe_param:
            assert f"[param {safe_param}]" in str(diagnostic)
        case None:
            assert param not in str(diagnostic)
