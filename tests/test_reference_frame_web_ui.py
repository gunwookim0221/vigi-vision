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
    assert '<select id="channel-id"' in response.text
    assert 'id="channel-status"' in response.text
    assert "Loading channels" in response.text
    assert 'for="reference-time"' in response.text
    assert 'id="reference-time"' in response.text
    assert 'id="source-timezone"' in response.text
    assert 'id="apply-reference-time"' in response.text
    assert "Apply date and time" in response.text
    assert 'id="generation-progress"' in response.text
    assert 'id="generation-indicator"' in response.text
    assert 'aria-live="polite"' in response.text
    assert 'type="submit"' in response.text
    assert 'type="radio"' not in response.text
    assert 'id="selection-status"' in response.text
    assert 'id="selected-preview-image"' in response.text
    assert 'id="roi-assisted-mask"' in response.text
    assert 'id="roi-workspace"' in response.text
    assert 'id="roi-stage"' in response.text
    assert 'id="roi-instructions"' in response.text
    assert 'id="roi-edit-instructions"' in response.text
    assert 'id="roi-assisted-button"' in response.text
    assert "Tap to suggest ROI" in response.text
    assert 'id="roi-assisted-guidance"' in response.text
    assert 'id="roi-status"' in response.text
    assert 'data-state="disabled"' in response.text
    assert 'aria-busy="false"' in response.text
    assert response.text.count('id="roi-status"') == 1
    assert 'data-label="Committed ROI"' not in response.text
    assert 'data-label="Draft ROI"' not in response.text
    assert 'id="roi-assisted-marker"' in response.text
    assert 'id="roi-reset"' in response.text
    assert 'data-handle="nw"' in response.text
    assert 'data-handle="se"' in response.text
    assert "Shift+Arrow" in response.text
    assert "Backspace resets" in response.text
    assert "Escape cancels" in response.text
    assert "touch, or pen" in response.text
    assert 'id="roi-summary"' in response.text


def test_web_ui_serves_its_static_assets() -> None:
    client = _client()

    stylesheet = client.get("/static/reference-frame-ui.css")
    candidate_stylesheet = client.get("/static/reference-frame-candidates.css")
    form_stylesheet = client.get("/static/reference-frame-form.css")
    roi_stylesheet = client.get("/static/reference-frame-roi.css")

    assert stylesheet.status_code == 200
    assert "focus-visible" in stylesheet.text
    assert "prefers-reduced-motion" in stylesheet.text
    assert candidate_stylesheet.status_code == 200
    assert ".candidate-thumbnail" in candidate_stylesheet.text
    assert "object-fit: contain" in candidate_stylesheet.text
    assert form_stylesheet.status_code == 200
    assert ".loading-spinner" in form_stylesheet.text
    assert "select:focus-visible" in form_stylesheet.text
    assert "prefers-reduced-motion" in form_stylesheet.text
    assert roi_stylesheet.status_code == 200
    assert "touch-action: none" in roi_stylesheet.text
    assert "roi-overlay-committed" in roi_stylesheet.text
    assert "roi-overlay-draft" in roi_stylesheet.text
    assert ".roi-assisted-mask" in roi_stylesheet.text
    assert 'data-assisted="true"' in roi_stylesheet.text
    assert ".roi-handle" in roi_stylesheet.text
    assert "--roi-handle-hit-size" in roi_stylesheet.text
    assert ".roi-handle:hover:not(:disabled)" in roi_stylesheet.text
    assert "align-items: center" in roi_stylesheet.text
    assert "justify-content: center" in roi_stylesheet.text
    assert "transform: translate(-50%, -50%)" in roi_stylesheet.text
    assert ".roi-stage:focus-visible" in roi_stylesheet.text
    assert '.roi-status[data-state="success"]' in roi_stylesheet.text
    assert '.roi-status[data-state="error"]' in roi_stylesheet.text
    assert "content: attr(data-label)" not in roi_stylesheet.text


def test_web_ui_serves_its_static_scripts() -> None:
    client = _client()

    form_script = client.get("/static/reference-frame-form.js")
    selection_script = client.get("/static/reference-frame-selection.js")
    roi_geometry_script = client.get("/static/reference-frame-roi-geometry.js")
    roi_script = client.get("/static/reference-frame-roi.js")
    assisted_request_script = client.get("/static/reference-frame-roi-assisted-request.js")
    assisted_script = client.get("/static/reference-frame-roi-assisted.js")
    assisted_pointer_script = client.get("/static/reference-frame-roi-assisted-pointer.js")
    roi_interaction_script = client.get("/static/reference-frame-roi-interaction.js")
    script = client.get("/static/reference-frame-ui.js")

    assert form_script.status_code == 200
    assert "source_timezone" in form_script.text
    assert "appliedRequestDirty" in form_script.text
    assert selection_script.status_code == 200
    assert "getSelectedCandidate" in selection_script.text
    assert 'type = "radio"' in selection_script.text
    assert "selected-preview-image" in selection_script.text
    assert roi_geometry_script.status_code == 200
    assert "Math.round" in roi_geometry_script.text
    assert roi_script.status_code == 200
    assert "naturalWidth" in roi_script.text
    assert "sourceRoiToDisplay" in roi_script.text
    assert "getPhase6Snapshot" in roi_script.text
    assert assisted_request_script.status_code == 200
    assert "roi-suggestions" in assisted_request_script.text
    assert "source_width" in assisted_request_script.text
    assert "mask_preview" in assisted_request_script.text
    assert assisted_script.status_code == 200
    assert "Tap to suggest ROI" in assisted_script.text
    assert "AbortController" in assisted_script.text
    assert "renderMaskPreview" in assisted_script.text
    assert "roi-assisted-mask" in assisted_script.text
    assert assisted_pointer_script.status_code == 200
    assert "pointercancel" in assisted_pointer_script.text
    assert "pointToSource" in assisted_pointer_script.text
    assert roi_interaction_script.status_code == 200
    assert "pointercancel" in roi_interaction_script.text
    assert "lostpointercapture" in roi_interaction_script.text
    assert "setPointerCapture" in roi_interaction_script.text
    assert "ArrowRight" in roi_interaction_script.text
    assert "altKey" in roi_interaction_script.text
    assert script.status_code == 200
    assert 'fetch("/api/v1/reference-frame-candidate-sets"' in script.text
    assert "channel_id: Number(channelIdInput.value)" in script.text
    assert "getRequestPayload" in script.text
    assert "generationProgress" in script.text
    assert "aria-busy" in script.text
    assert "Exact source timestamp is not yet verified." in script.text
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


def test_web_ui_script_loads_channel_inventory_safely() -> None:
    script = _client().get("/static/reference-frame-ui.js")

    assert script.status_code == 200
    assert 'fetch("/api/v1/reference-frames/channels")' in script.text
    assert "default_channel_id" in script.text
    assert "vigiVisionReferenceFrameChannels" in script.text


def test_web_ui_script_renders_server_values_as_text_not_html() -> None:
    script = _client().get("/static/reference-frame-ui.js")

    assert script.status_code == 200
    assert "textContent" in script.text
    assert "innerHTML" not in script.text
    assert "candidate.reference_frame.image_url" in script.text
    assert 'startsWith("/api/v1/reference-frames/")' in script.text


def _client() -> TestClient:
    return TestClient(create_reference_frame_app(UnusedExecutor(), UnusedResources()))
