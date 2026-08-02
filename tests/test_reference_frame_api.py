from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NoReturn

import pytest
from fastapi.testclient import TestClient

from vigi_vision import reference_frame_api
from vigi_vision.channel_selection import Channel
from vigi_vision.recording import RecordingSegment, RecordingWindow
from vigi_vision.reference_frame_api import (
    ReferenceFrameApiStartupError,
    create_reference_frame_app,
    create_reference_frame_app_from_environment,
)
from vigi_vision.reference_frame_models import (
    FrameSelectionPolicy,
    ReferenceFrameChannelNotFoundError,
    ReferenceFrameOutcome,
    ReferenceFrameRequest,
    ReferenceFrameResolution,
    ReferenceFrameResourceNotFoundError,
    ReferenceFrameResult,
    TimingPrecisionStatus,
)
from vigi_vision.reference_frame_resources import ReferenceFrameImageResource


@dataclass(frozen=True, slots=True)
class FakeService:
    result: ReferenceFrameResult
    outcome: ReferenceFrameOutcome = ReferenceFrameOutcome.CREATED
    failure: Exception | None = None

    def execute_or_resolve(self, request: ReferenceFrameRequest) -> ReferenceFrameResolution:
        _ = request
        if self.failure is not None:
            raise self.failure
        return ReferenceFrameResolution(self.result, self.outcome)


@dataclass(frozen=True, slots=True)
class FakeResources:
    image: ReferenceFrameImageResource
    failure: Exception | None = None

    def resolve_image(self, resource_id: str) -> ReferenceFrameImageResource:
        _ = resource_id
        if self.failure is not None:
            raise self.failure
        return self.image


@dataclass(frozen=True, slots=True)
class FakeChannelInventory:
    inventory: tuple[Channel, ...]
    failure: Exception | None = None

    def channels(self) -> tuple[Channel, ...]:
        if self.failure is not None:
            raise self.failure
        return self.inventory


def test_channel_list_returns_online_channels_and_shared_default(tmp_path: Path) -> None:
    result = _result()
    image_path = tmp_path / "frame.jpg"
    _ = image_path.write_bytes(b"\xff\xd8frame\xff\xd9")
    inventory = FakeChannelInventory(
        (
            Channel(7, "Dining", "Dining", online=True),
            Channel(2, "Entrance", "Entrance", online=False),
            Channel(3, "Counter", "Counter", online=True),
            Channel(0, "Invalid", "Invalid", online=True),
            Channel(1, "Main", "Main", online=True),
        )
    )
    app = create_reference_frame_app(
        FakeService(result),
        FakeResources(ReferenceFrameImageResource(result.resource_id, image_path)),
        channel_inventory=inventory,
    )

    response = TestClient(app).get("/api/v1/reference-frames/channels")

    assert response.status_code == 200
    assert response.json() == {
        "channels": [
            {"channel_id": 7, "name": "Dining", "alias": "Dining", "online": True},
            {"channel_id": 3, "name": "Counter", "alias": "Counter", "online": True},
            {"channel_id": 1, "name": "Main", "alias": "Main", "online": True},
        ],
        "default_channel_id": 1,
    }


def test_channel_list_failure_is_safe(tmp_path: Path) -> None:
    marker = "rtsp://user:password@nvr.example/private"
    result = _result()
    image_path = tmp_path / "frame.jpg"
    _ = image_path.write_bytes(b"\xff\xd8frame\xff\xd9")
    app = create_reference_frame_app(
        FakeService(result),
        FakeResources(ReferenceFrameImageResource(result.resource_id, image_path)),
        channel_inventory=FakeChannelInventory((), RuntimeError(marker)),
    )

    response = TestClient(app).get("/api/v1/reference-frames/channels")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "The reference-frame operation could not be completed safely.",
            "details": None,
        }
    }
    assert marker not in response.text


def test_create_reference_frame_returns_created_response(tmp_path: Path) -> None:
    # Given
    image_path = tmp_path / "frame.jpg"
    _ = image_path.write_bytes(b"\xff\xd8frame\xff\xd9")
    result = _result()
    app = create_reference_frame_app(
        FakeService(result),
        FakeResources(ReferenceFrameImageResource(result.resource_id, image_path)),
    )
    client = TestClient(app)

    # When
    response = client.post(
        "/api/v1/reference-frames",
        json={
            "channel_id": 1,
            "requested_time": "2026-07-20T12:34:18+09:00",
            "source_timezone": "Asia/Seoul",
        },
    )

    # Then
    assert response.status_code == 201
    assert response.json()["outcome"] == "created"
    assert response.json()["image_url"] == (f"/api/v1/reference-frames/{result.resource_id}/image")
    assert response.json()["timing"]["precision_status"] == "measured_clip_relative"
    assert response.json()["timing"]["estimated_source_time_utc"] is None


def test_create_reference_frame_rejects_naive_time_without_timezone(tmp_path: Path) -> None:
    # Given
    image_path = tmp_path / "frame.jpg"
    _ = image_path.write_bytes(b"\xff\xd8frame\xff\xd9")
    result = _result()
    app = create_reference_frame_app(
        FakeService(result),
        FakeResources(ReferenceFrameImageResource(result.resource_id, image_path)),
    )
    client = TestClient(app)

    # When
    response = client.post(
        "/api/v1/reference-frames",
        json={"channel_id": 1, "requested_time": "2026-07-20T12:34:18"},
    )

    # Then
    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_request",
            "message": "The reference-frame request is invalid.",
            "details": None,
        }
    }


def test_create_reference_frame_returns_reused_response(tmp_path: Path) -> None:
    # Given
    image_path = tmp_path / "frame.jpg"
    _ = image_path.write_bytes(b"\xff\xd8frame\xff\xd9")
    result = _result()
    app = create_reference_frame_app(
        FakeService(result, outcome=ReferenceFrameOutcome.REUSED),
        FakeResources(ReferenceFrameImageResource(result.resource_id, image_path)),
    )
    client = TestClient(app)

    # When
    response = client.post(
        "/api/v1/reference-frames",
        json={
            "channel_id": 1,
            "requested_time": "2026-07-20T03:34:18Z",
        },
    )

    # Then
    assert response.status_code == 200
    assert response.json()["outcome"] == "reused"


def test_get_reference_frame_image_returns_fixed_jpeg(tmp_path: Path) -> None:
    # Given
    image_path = tmp_path / "frame.jpg"
    image_bytes = b"\xff\xd8frame\xff\xd9"
    _ = image_path.write_bytes(image_bytes)
    result = _result()
    app = create_reference_frame_app(
        FakeService(result),
        FakeResources(ReferenceFrameImageResource(result.resource_id, image_path)),
    )
    client = TestClient(app)

    # When
    response = client.get(f"/api/v1/reference-frames/{result.resource_id}/image")

    # Then
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, max-age=3600, immutable"
    assert response.content == image_bytes


def test_api_errors_are_stable_and_redact_exception_text(tmp_path: Path) -> None:
    # Given
    sensitive_marker = "rtsp://user:password@nvr.example/private"
    result = _result()
    image_path = tmp_path / "frame.jpg"
    _ = image_path.write_bytes(b"\xff\xd8frame\xff\xd9")
    app = create_reference_frame_app(
        FakeService(result, failure=RuntimeError(sensitive_marker)),
        FakeResources(ReferenceFrameImageResource(result.resource_id, image_path)),
    )
    client = TestClient(app)

    # When
    response = client.post(
        "/api/v1/reference-frames",
        json={
            "channel_id": 1,
            "requested_time": "2026-07-20T03:34:18Z",
            "unexpected": sensitive_marker,
        },
    )

    # Then
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert sensitive_marker not in response.text


def test_api_redacts_unexpected_service_failure(tmp_path: Path) -> None:
    # Given
    sensitive_marker = "rtsp://user:password@nvr.example/private"
    result = _result()
    image_path = tmp_path / "frame.jpg"
    _ = image_path.write_bytes(b"\xff\xd8frame\xff\xd9")
    app = create_reference_frame_app(
        FakeService(result, failure=RuntimeError(sensitive_marker)),
        FakeResources(ReferenceFrameImageResource(result.resource_id, image_path)),
    )
    client = TestClient(app)

    # When
    response = client.post(
        "/api/v1/reference-frames",
        json={"channel_id": 1, "requested_time": "2026-07-20T03:34:18Z"},
    )

    # Then
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert sensitive_marker not in response.text


def test_api_maps_known_domain_failure_without_revealing_details(tmp_path: Path) -> None:
    # Given
    result = _result()
    image_path = tmp_path / "frame.jpg"
    _ = image_path.write_bytes(b"\xff\xd8frame\xff\xd9")
    app = create_reference_frame_app(
        FakeService(result, failure=ReferenceFrameChannelNotFoundError()),
        FakeResources(ReferenceFrameImageResource(result.resource_id, image_path)),
    )
    client = TestClient(app)

    # When
    response = client.post(
        "/api/v1/reference-frames",
        json={"channel_id": 1, "requested_time": "2026-07-20T03:34:18Z"},
    )

    # Then
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "channel_not_found"


@pytest.mark.parametrize(
    "payload",
    [
        {"channel_id": 0, "requested_time": "2026-07-20T03:34:18Z"},
        {"channel_id": 1, "requested_time": "2026-07-20T03:34:18.5Z"},
        {"channel_id": 1, "requested_time": "2099-07-20T03:34:18Z"},
        {
            "channel_id": 1,
            "requested_time": "2026-07-20T03:34:18+00:00",
            "source_timezone": "Asia/Seoul",
        },
    ],
)
def test_create_reference_frame_rejects_invalid_input(
    tmp_path: Path, payload: dict[str, int | str]
) -> None:
    # Given
    image_path = tmp_path / "frame.jpg"
    _ = image_path.write_bytes(b"\xff\xd8frame\xff\xd9")
    result = _result()
    app = create_reference_frame_app(
        FakeService(result),
        FakeResources(ReferenceFrameImageResource(result.resource_id, image_path)),
    )
    client = TestClient(app)

    # When
    response = client.post("/api/v1/reference-frames", json=payload)

    # Then
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_create_reference_frame_rejects_malformed_json_safely(tmp_path: Path) -> None:
    # Given
    image_path = tmp_path / "frame.jpg"
    _ = image_path.write_bytes(b"\xff\xd8frame\xff\xd9")
    result = _result()
    app = create_reference_frame_app(
        FakeService(result),
        FakeResources(ReferenceFrameImageResource(result.resource_id, image_path)),
    )
    client = TestClient(app)

    # When
    response = client.post(
        "/api/v1/reference-frames",
        content=b'{"channel_id":',
        headers={"Content-Type": "application/json"},
    )

    # Then
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "malformed_json"


def test_get_reference_frame_image_maps_unknown_resource_safely(tmp_path: Path) -> None:
    # Given
    image_path = tmp_path / "frame.jpg"
    _ = image_path.write_bytes(b"\xff\xd8frame\xff\xd9")
    result = _result()
    app = create_reference_frame_app(
        FakeService(result),
        FakeResources(
            ReferenceFrameImageResource(result.resource_id, image_path),
            failure=ReferenceFrameResourceNotFoundError(),
        ),
    )
    client = TestClient(app)

    # When
    response = client.get("/api/v1/reference-frames/unknown-resource/image")

    # Then
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"


def test_get_reference_frame_image_rejects_encoded_traversal_safely(tmp_path: Path) -> None:
    # Given
    image_path = tmp_path / "frame.jpg"
    _ = image_path.write_bytes(b"\xff\xd8frame\xff\xd9")
    result = _result()
    app = create_reference_frame_app(
        FakeService(result),
        FakeResources(ReferenceFrameImageResource(result.resource_id, image_path)),
    )
    client = TestClient(app)

    # When
    response = client.get("/api/v1/reference-frames/%2e%2e%2fprivate/image")

    # Then
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"


def test_openapi_describes_safe_creation_and_jpeg_contract(tmp_path: Path) -> None:
    # Given
    image_path = tmp_path / "frame.jpg"
    _ = image_path.write_bytes(b"\xff\xd8frame\xff\xd9")
    result = _result()
    app = create_reference_frame_app(
        FakeService(result),
        FakeResources(ReferenceFrameImageResource(result.resource_id, image_path)),
    )
    client = TestClient(app)

    # When
    document = client.get("/openapi.json").text
    image_path = '"/api/v1/reference-frames/{resource_id}/image"'
    image_operation = document[document.index(image_path) : document.index('"components"')]

    # Then
    assert '"content":{"image/jpeg":{}}' in image_operation
    assert '"$ref":"#/components/schemas/ReferenceFrameErrorResponse"' in image_operation
    assert '"$ref":"#/components/schemas/HTTPValidationError"' not in image_operation
    assert '"measured_clip_relative"' in document
    assert '"exact"' not in document
    assert "password" not in document
    assert "rtsp://" not in document


def test_environment_composition_redacts_setup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    sensitive_marker = "VIGI_PASSWORD=secret"

    def fail_settings(_: Path) -> NoReturn:
        raise RuntimeError(sensitive_marker)

    monkeypatch.setattr(reference_frame_api, "load_capture_settings", fail_settings)

    # When / Then
    with pytest.raises(ReferenceFrameApiStartupError) as exception_info:
        _ = create_reference_frame_app_from_environment()

    assert sensitive_marker not in str(exception_info.value)


def _result() -> ReferenceFrameResult:
    requested_time = datetime(2026, 7, 20, 3, 34, 18, tzinfo=timezone.utc)
    segment = RecordingSegment(
        channel_id=1,
        recording_day=requested_time.date(),
        start_epoch_seconds=int((requested_time - timedelta(minutes=1)).timestamp()),
        end_epoch_seconds=int((requested_time + timedelta(minutes=1)).timestamp()),
        start_utc=requested_time - timedelta(minutes=1),
        end_utc=requested_time + timedelta(minutes=1),
    )
    window = RecordingWindow(
        1,
        requested_time - timedelta(seconds=2),
        requested_time + timedelta(seconds=4),
    )
    resource_id = (
        "channel-1_20260720T033418Z_"
        "segment-20260720T033300Z-20260720T033500Z_nearest-decoded-frame_gpv-1"
    )
    return ReferenceFrameResult(
        resource_id=resource_id,
        manifest_schema_version=1,
        generation_policy_version=1,
        channel_id=1,
        requested_time_text="2026-07-20T12:34:18+09:00",
        source_timezone="Asia/Seoul",
        requested_time_utc=requested_time,
        selected_segment=segment,
        extraction_window=window,
        frame_selection_policy=FrameSelectionPolicy.NEAREST_DECODED_FRAME,
        jpeg_relative_path=Path(resource_id) / "frame.jpg",
        manifest_relative_path=Path(resource_id) / "manifest.json",
        width=1280,
        height=720,
        decoded_local_pts_seconds=2.0,
        estimated_source_time_utc=None,
        offset_from_requested_seconds=None,
        timing_precision_status=TimingPrecisionStatus.MEASURED_CLIP_RELATIVE,
        warnings=("Source timestamp mapping is unavailable pending real-NVR replay validation.",),
    )
