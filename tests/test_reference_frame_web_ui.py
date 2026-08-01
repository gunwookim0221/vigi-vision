from typing import NoReturn, final

from fastapi.testclient import TestClient

from vigi_vision.reference_frame_api import create_reference_frame_app
from vigi_vision.reference_frame_models import ReferenceFrameRequest


@final
class UnusedExecutor:
    def execute_or_resolve(self, request: ReferenceFrameRequest) -> NoReturn:
        _ = request
        raise AssertionError


@final
class UnusedResources:
    def resolve_image(self, resource_id: str) -> NoReturn:
        _ = resource_id
        raise AssertionError


def test_web_ui_serves_an_accessible_candidate_form() -> None:
    client = _client()

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'for="channel-id"' in response.text
    assert 'id="channel-id"' in response.text
    assert 'for="reference-time"' in response.text
    assert 'id="reference-time"' in response.text
    assert 'aria-live="polite"' in response.text
    assert 'type="submit"' in response.text


def test_web_ui_serves_its_static_assets() -> None:
    client = _client()

    stylesheet = client.get("/static/reference-frame-ui.css")
    candidate_stylesheet = client.get("/static/reference-frame-candidates.css")
    script = client.get("/static/reference-frame-ui.js")

    assert stylesheet.status_code == 200
    assert "focus-visible" in stylesheet.text
    assert "prefers-reduced-motion" in stylesheet.text
    assert candidate_stylesheet.status_code == 200
    assert ".candidate-thumbnail" in candidate_stylesheet.text
    assert "object-fit: contain" in candidate_stylesheet.text
    assert script.status_code == 200
    assert 'fetch("/api/v1/reference-frame-candidate-sets"' in script.text
    assert "channel_id: Number(channelIdInput.value)" in script.text
    assert "reference_time: referenceTimeInput.value" in script.text
    assert "candidate.reference_frame.image_url" in script.text
    assert "candidate-thumbnail-placeholder" in script.text


def test_web_ui_script_handles_all_required_safe_result_states() -> None:
    script = _client().get("/static/reference-frame-ui.js")

    assert script.status_code == 200
    assert "loading" in script.text
    assert "partial" in script.text
    assert "all-failed" in script.text
    assert "created" in script.text
    assert "reused" in script.text
    assert "failure.code" in script.text
    assert "failure.message" in script.text
    assert "No candidate positions were returned." in script.text
    assert "requestSequence" in script.text


def test_web_ui_script_renders_server_values_as_text_not_html() -> None:
    script = _client().get("/static/reference-frame-ui.js")

    assert script.status_code == 200
    assert "textContent" in script.text
    assert "innerHTML" not in script.text
    assert "candidate.reference_frame.image_url" in script.text
    assert 'startsWith("/api/v1/reference-frames/")' in script.text


def _client() -> TestClient:
    return TestClient(create_reference_frame_app(UnusedExecutor(), UnusedResources()))
