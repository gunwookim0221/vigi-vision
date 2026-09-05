import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol, final

import pytest
from fastapi.testclient import TestClient
from tests.test_investigation_confirmation import Context, build_context
from tests.test_investigation_confirmation_phase6c import write_schema_two_package

from vigi_vision import investigation_confirmation_repository as confirmation_repository_module
from vigi_vision.investigation_confirmation_api_models import (
    InvestigationConfirmationResponse,
)
from vigi_vision.investigation_confirmation_models import ConfirmationRequest
from vigi_vision.reference_frame_api import create_reference_frame_app
from vigi_vision.reference_frame_api_models import ReferenceFrameErrorResponse
from vigi_vision.reference_frame_models import ReferenceFrameRequest

JsonScalar = bool | float | int | str | None
JsonValue = JsonScalar | dict[str, JsonScalar]
JsonBody = dict[str, JsonValue]


class _ResponsePayload(Protocol):
    @property
    def content(self) -> bytes: ...


@final
class UnusedReferenceFrameService:
    def execute_or_resolve(self, request: ReferenceFrameRequest) -> NoReturn:
        _ = request
        raise AssertionError


@final
class UnusedReferenceFrameResources:
    def resolve_image(self, resource_id: str) -> NoReturn:
        _ = resource_id
        raise AssertionError


@dataclass(frozen=True, slots=True)
class FailingConfirmationService:
    failure: RuntimeError

    def confirm(self, request: ConfirmationRequest) -> NoReturn:
        _ = request
        raise self.failure

    def load_confirmation_manifest(self, investigation_id: str) -> NoReturn:
        _ = investigation_id
        raise self.failure

    def reconfirm_for_recording_search(self, investigation_id: str) -> NoReturn:
        _ = investigation_id
        raise self.failure


def _client(tmp_path: Path) -> tuple[TestClient, Context]:
    context = build_context(tmp_path)
    app = create_reference_frame_app(
        UnusedReferenceFrameService(),
        UnusedReferenceFrameResources(),
        confirmation_service=context.service,
    )
    return TestClient(app), context


def _body(
    resource_id: str,
    *,
    candidate_offset: int = -10,
    reference_time: str = "2026-07-20T12:34:28",
) -> JsonBody:
    return {
        "reference_frame_resource_id": resource_id,
        "reference_time": reference_time,
        "source_timezone": "Asia/Seoul",
        "candidate_offset_seconds": candidate_offset,
        "source_width": 1280,
        "source_height": 720,
        "roi": {
            "x": 10,
            "y": 20,
            "width": 120,
            "height": 80,
            "coordinate_space": "source_pixels",
            "provenance": "assisted_then_adjusted",
        },
    }


def _confirmation_payload(response: _ResponsePayload) -> InvestigationConfirmationResponse:
    return InvestigationConfirmationResponse.model_validate_json(response.content)


def _error_code(response: _ResponsePayload) -> str:
    return ReferenceFrameErrorResponse.model_validate_json(response.content).error.code


def _add_unknown(body: JsonBody) -> None:
    body["unexpected"] = 1


def _remove_width(body: JsonBody) -> None:
    _ = body.pop("source_width")


def _set_boolean_width(body: JsonBody) -> None:
    body["source_width"] = True


def _set_string_width(body: JsonBody) -> None:
    body["source_width"] = "1280"


def _set_fractional_x(body: JsonBody) -> None:
    body["roi"] = {
        "x": 10.5,
        "y": 20,
        "width": 120,
        "height": 80,
        "coordinate_space": "source_pixels",
        "provenance": "assisted_then_adjusted",
    }


def _set_boolean_x(body: JsonBody) -> None:
    body["roi"] = {
        "x": True,
        "y": 20,
        "width": 120,
        "height": 80,
        "coordinate_space": "source_pixels",
        "provenance": "assisted_then_adjusted",
    }


def _set_string_x(body: JsonBody) -> None:
    body["roi"] = {
        "x": "10",
        "y": 20,
        "width": 120,
        "height": 80,
        "coordinate_space": "source_pixels",
        "provenance": "assisted_then_adjusted",
    }


def _set_invalid_coordinate_space(body: JsonBody) -> None:
    body["roi"] = {
        "x": 10,
        "y": 20,
        "width": 120,
        "height": 80,
        "coordinate_space": "normalized",
        "provenance": "assisted_then_adjusted",
    }


def _remove_coordinate_space(body: JsonBody) -> None:
    body["roi"] = {
        "x": 10,
        "y": 20,
        "width": 120,
        "height": 80,
        "provenance": "assisted_then_adjusted",
    }


def _set_small_width(body: JsonBody) -> None:
    body["roi"] = {
        "x": 10,
        "y": 20,
        "width": 3,
        "height": 80,
        "coordinate_space": "source_pixels",
        "provenance": "assisted_then_adjusted",
    }


def _set_negative_width(body: JsonBody) -> None:
    body["roi"] = {
        "x": 10,
        "y": 20,
        "width": -4,
        "height": 80,
        "coordinate_space": "source_pixels",
        "provenance": "assisted_then_adjusted",
    }


def _set_invalid_provenance(body: JsonBody) -> None:
    body["roi"] = {
        "x": 10,
        "y": 20,
        "width": 120,
        "height": 80,
        "coordinate_space": "source_pixels",
        "provenance": "server_trusted",
    }


_INVALID_BODY_CASES: tuple[tuple[Callable[[JsonBody], None], str], ...] = (
    (_add_unknown, "invalid_confirmation"),
    (_remove_width, "invalid_confirmation"),
    (_set_boolean_width, "invalid_confirmation"),
    (_set_string_width, "invalid_confirmation"),
    (_set_fractional_x, "invalid_confirmation"),
    (_set_boolean_x, "invalid_confirmation"),
    (_set_string_x, "invalid_confirmation"),
    (_set_invalid_coordinate_space, "invalid_confirmation"),
    (_remove_coordinate_space, "invalid_confirmation"),
    (_set_small_width, "invalid_confirmation"),
    (_set_negative_width, "invalid_confirmation"),
    (_set_invalid_provenance, "invalid_confirmation"),
)


def test_confirmation_api_creates_and_retrieves_safe_schema_three_result(tmp_path: Path) -> None:
    client, context = _client(tmp_path)

    response = client.post("/api/v1/investigation-confirmations", json=_body(context.resource_id))

    assert response.status_code == 201
    payload = _confirmation_payload(response)
    assert payload.status == "confirmed"
    assert payload.schema_version == 3
    assert payload.outcome.value == "created"
    assert payload.confirmation.channel_id == 1
    assert payload.confirmation.reference_frame_resource_id == context.resource_id
    assert payload.confirmation.requested_time_utc.isoformat() == "2026-07-20T03:34:18+00:00"
    assert payload.confirmation.source_timezone == "Asia/Seoul"
    assert payload.confirmation.timing.timing_precision_status.value == "measured_clip_relative"
    assert payload.confirmation.roi.coordinate_space == "source_pixels"
    assert str(tmp_path) not in response.text
    assert ".claim" not in response.text
    assert ".staging" not in response.text

    investigation_id = payload.investigation_id
    retrieved = client.get(f"/api/v1/investigation-confirmations/{investigation_id}")

    assert retrieved.status_code == 200
    assert _confirmation_payload(retrieved) == payload


def test_confirmation_api_identical_retry_reuses_without_rewriting(tmp_path: Path) -> None:
    client, context = _client(tmp_path)
    body = _body(context.resource_id)

    first = client.post("/api/v1/investigation-confirmations", json=body)
    first_payload = _confirmation_payload(first)
    manifest_path = context.investigation_root / first_payload.investigation_id / "manifest.json"
    before = manifest_path.read_bytes()
    second = client.post("/api/v1/investigation-confirmations", json=body)

    assert first.status_code == 201
    assert second.status_code == 200
    second_payload = _confirmation_payload(second)
    assert second_payload.outcome.value == "reused"
    assert second_payload.confirmed_at_utc == first_payload.confirmed_at_utc
    assert manifest_path.read_bytes() == before


def test_confirmation_api_final_result_wins_over_leftover_claim(tmp_path: Path) -> None:
    client, context = _client(tmp_path)
    first = client.post("/api/v1/investigation-confirmations", json=_body(context.resource_id))
    claim = context.investigation_root / f".{context.investigation_id}.claim"
    _ = claim.write_text("not-json", encoding="utf-8")

    second = client.post("/api/v1/investigation-confirmations", json=_body(context.resource_id))

    assert first.status_code == 201
    assert second.status_code == 200
    assert _confirmation_payload(second).outcome.value == "reused"
    assert claim.read_text(encoding="utf-8") == "not-json"


def test_confirmation_api_different_roi_returns_immutable_conflict(tmp_path: Path) -> None:
    client, context = _client(tmp_path)
    first = client.post("/api/v1/investigation-confirmations", json=_body(context.resource_id))
    conflicting = _body(context.resource_id)
    conflicting["roi"] = {
        "x": 20,
        "y": 20,
        "width": 120,
        "height": 80,
        "coordinate_space": "source_pixels",
        "provenance": "assisted_then_adjusted",
    }

    response = client.post("/api/v1/investigation-confirmations", json=conflicting)

    assert first.status_code == 201
    assert response.status_code == 409
    assert _error_code(response) == "confirmation_conflict"


def test_confirmation_api_different_resource_returns_immutable_conflict(
    tmp_path: Path,
) -> None:
    context = build_context(tmp_path)
    other = build_context(tmp_path, requested_time_text="2026-07-20T12:34:08")
    client = TestClient(
        create_reference_frame_app(
            UnusedReferenceFrameService(),
            UnusedReferenceFrameResources(),
            confirmation_service=context.service,
        )
    )

    first = client.post(
        "/api/v1/investigation-confirmations",
        json=_body(context.resource_id, candidate_offset=-10),
    )
    payload = _confirmation_payload(first)
    manifest_path = context.investigation_root / payload.investigation_id / "manifest.json"
    original = manifest_path.read_bytes()

    response = client.post(
        "/api/v1/investigation-confirmations",
        json=_body(other.resource_id, candidate_offset=-20),
    )

    assert first.status_code == 201
    assert response.status_code == 409
    assert _error_code(response) == "confirmation_conflict"
    assert manifest_path.read_bytes() == original
    assert payload.confirmation.reference_frame_resource_id == context.resource_id
    assert other.resource_id not in manifest_path.read_text(encoding="utf-8")


def test_confirmation_api_rejects_resource_from_another_investigation(
    tmp_path: Path,
) -> None:
    context = build_context(tmp_path)
    other = build_context(
        tmp_path,
        requested_time_text="2026-07-20T12:50:00",
        confirmation_time_text="2026-07-20T12:50:10",
    )
    client = TestClient(
        create_reference_frame_app(
            UnusedReferenceFrameService(),
            UnusedReferenceFrameResources(),
            confirmation_service=context.service,
        )
    )
    foreign = client.post(
        "/api/v1/investigation-confirmations",
        json=_body(
            other.resource_id,
            candidate_offset=-10,
            reference_time="2026-07-20T12:50:10",
        ),
    )
    foreign_payload = _confirmation_payload(foreign)
    foreign_manifest_path = (
        context.investigation_root / foreign_payload.investigation_id / "manifest.json"
    )
    foreign_manifest = foreign_manifest_path.read_bytes()

    response = client.post(
        "/api/v1/investigation-confirmations",
        json=_body(
            other.resource_id,
            candidate_offset=-10,
            reference_time="2026-07-20T12:34:28",
        ),
    )

    assert response.status_code == 409
    assert _error_code(response) == "stale_selection"
    assert foreign.status_code == 201
    assert not (context.investigation_root / context.investigation_id).exists()
    assert foreign_manifest_path.read_bytes() == foreign_manifest


def test_confirmation_api_native_publication_failure_is_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_context(tmp_path)
    other = build_context(
        tmp_path,
        requested_time_text="2026-07-20T12:34:48",
        confirmation_time_text="2026-07-20T12:34:58",
    )
    client = TestClient(
        create_reference_frame_app(
            UnusedReferenceFrameService(),
            UnusedReferenceFrameResources(),
            confirmation_service=context.service,
        )
    )
    first = client.post(
        "/api/v1/investigation-confirmations",
        json=_body(context.resource_id),
    )
    original_payload = _confirmation_payload(first)
    original_manifest = (
        context.investigation_root / original_payload.investigation_id / "manifest.json"
    ).read_bytes()

    failure_marker = "foreign staging path and native publication details"

    def fail_publication(source: Path, destination: Path) -> bool:
        _ = (source, destination)
        raise OSError(failure_marker)

    monkeypatch.setattr(
        confirmation_repository_module,
        "publish_directory_no_replace",
        fail_publication,
    )
    response = client.post(
        "/api/v1/investigation-confirmations",
        json=_body(
            other.resource_id,
            candidate_offset=-10,
            reference_time="2026-07-20T12:34:58",
        ),
    )

    failed_id = other.investigation_id
    assert response.status_code == 500
    assert _error_code(response) == "artifact_failure"
    assert failure_marker not in response.text
    assert str(tmp_path) not in response.text
    assert ".staging" not in response.text
    assert ".claim" not in response.text
    assert client.get(f"/api/v1/investigation-confirmations/{failed_id}").status_code == 404
    assert (
        context.investigation_root / original_payload.investigation_id / "manifest.json"
    ).read_bytes() == original_manifest
    assert not any(context.investigation_root.glob(f".{failed_id}-*.staging"))
    assert not (context.investigation_root / f".{failed_id}.claim").exists()


def test_confirmation_api_unsupported_methods_remain_405(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    collection_response = client.get("/api/v1/investigation-confirmations")
    item_response = client.post(
        "/api/v1/investigation-confirmations/object-disappearance-ch1-20260720T033428Z",
        json={},
    )

    assert collection_response.status_code == 405
    assert item_response.status_code == 405
    assert "internal_error" not in collection_response.text
    assert "internal_error" not in item_response.text


def test_phase7_handoff_contract_remains_unchanged(tmp_path: Path) -> None:
    client, context = _client(tmp_path)
    created = client.post("/api/v1/investigation-confirmations", json=_body(context.resource_id))
    payload = _confirmation_payload(created)

    loaded = context.service.load_confirmed(payload.investigation_id)

    assert not hasattr(loaded, "confirmed_at_utc")


@pytest.mark.parametrize(("mutation", "expected_code"), _INVALID_BODY_CASES)
def test_confirmation_api_rejects_untrusted_or_invalid_body(
    tmp_path: Path, mutation: Callable[[JsonBody], None], expected_code: str
) -> None:
    client, context = _client(tmp_path)
    body = _body(context.resource_id)
    mutation(body)

    response = client.post("/api/v1/investigation-confirmations", json=body)

    assert response.status_code == 422
    assert _error_code(response) == expected_code


@pytest.mark.parametrize(
    "field",
    [
        "status",
        "confirmed_at_utc",
        "artifact_directory_relative",
        "confirmation",
        "timing",
        "claim_owner",
    ],
)
def test_confirmation_api_rejects_client_authored_trusted_fields(
    tmp_path: Path, field: str
) -> None:
    client, context = _client(tmp_path)
    body = _body(context.resource_id)
    body[field] = "forbidden"

    response = client.post("/api/v1/investigation-confirmations", json=body)

    assert response.status_code == 422
    assert _error_code(response) == "invalid_confirmation"


def test_confirmation_api_maps_stale_dimensions_and_roi_bounds(tmp_path: Path) -> None:
    client, context = _client(tmp_path)
    stale = _body(context.resource_id)
    stale["source_width"] = 640
    out_of_bounds = _body(context.resource_id)
    out_of_bounds["roi"] = {
        "x": 1278,
        "y": 20,
        "width": 120,
        "height": 80,
        "coordinate_space": "source_pixels",
        "provenance": "assisted_then_adjusted",
    }

    stale_response = client.post("/api/v1/investigation-confirmations", json=stale)
    bounds_response = client.post("/api/v1/investigation-confirmations", json=out_of_bounds)

    assert stale_response.status_code == 409
    assert _error_code(stale_response) == "stale_selection"
    assert bounds_response.status_code == 422
    assert _error_code(bounds_response) == "invalid_confirmation"


def test_confirmation_api_maps_unknown_resource_and_unknown_investigation(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    missing_resource = _body("missing-resource")

    resource_response = client.post("/api/v1/investigation-confirmations", json=missing_resource)
    investigation_response = client.get(
        "/api/v1/investigation-confirmations/object-disappearance-ch1-20260720T033428Z"
    )

    assert resource_response.status_code == 404
    assert _error_code(resource_response) == "resource_not_found"
    assert investigation_response.status_code == 404
    assert _error_code(investigation_response) == "investigation_not_found"


def test_confirmation_api_does_not_present_staging_or_legacy_state(tmp_path: Path) -> None:
    client, context = _client(tmp_path)
    context.investigation_root.mkdir(parents=True)
    staging = context.investigation_root / ".object-disappearance-ch1-20260720T033428Z-op.staging"
    staging.mkdir()
    legacy = context.investigation_root / "object-disappearance-ch1-20260720T033429Z"
    legacy.mkdir()
    _ = (legacy / "manifest.json").write_text('{"schema_version": 1}', encoding="utf-8")

    staging_response = client.get(
        "/api/v1/investigation-confirmations/object-disappearance-ch1-20260720T033428Z"
    )
    legacy_response = client.get(
        "/api/v1/investigation-confirmations/object-disappearance-ch1-20260720T033429Z"
    )

    assert staging_response.status_code == 404
    assert _error_code(staging_response) == "investigation_not_found"
    assert legacy_response.status_code == 404
    assert _error_code(legacy_response) == "investigation_not_found"


def test_confirmation_api_maps_in_progress_claim_without_leakage(tmp_path: Path) -> None:
    client, context = _client(tmp_path)
    claim = context.investigation_root / f".{context.investigation_id}.claim"
    claim.parent.mkdir(parents=True)
    _ = claim.write_text(
        json.dumps(
            {
                "operation_id": "1234567890abcdef1234567890abcdef",
                "created_at_utc": "2026-08-02T04:05:06Z",
                "heartbeat_at_utc": "2026-08-02T04:05:06Z",
            }
        ),
        encoding="utf-8",
    )

    response = client.post("/api/v1/investigation-confirmations", json=_body(context.resource_id))

    assert response.status_code == 409
    assert _error_code(response) == "confirmation_in_progress"
    assert "1234567890abcdef" not in response.text
    assert str(tmp_path) not in response.text


def test_confirmation_api_maps_corrupt_published_state_safely(tmp_path: Path) -> None:
    client, context = _client(tmp_path)
    created = client.post("/api/v1/investigation-confirmations", json=_body(context.resource_id))
    created_payload = _confirmation_payload(created)
    manifest_path = context.investigation_root / created_payload.investigation_id / "manifest.json"
    _ = manifest_path.write_text("not-json", encoding="utf-8")

    response = client.get(f"/api/v1/investigation-confirmations/{created_payload.investigation_id}")

    assert response.status_code == 500
    assert _error_code(response) == "confirmation_corrupt"
    assert "not-json" not in response.text
    assert str(tmp_path) not in response.text


def test_confirmation_api_unexpected_failure_is_safe(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    marker = "CREDENTIALS rtsp://user:password@nvr.example/private"
    app = create_reference_frame_app(
        UnusedReferenceFrameService(),
        UnusedReferenceFrameResources(),
        confirmation_service=FailingConfirmationService(RuntimeError(marker)),
    )

    response = TestClient(app).post(
        "/api/v1/investigation-confirmations", json=_body(context.resource_id)
    )

    assert response.status_code == 500
    assert _error_code(response) == "internal_error"
    assert marker not in response.text


def test_confirmation_api_malformed_json_uses_bad_request_envelope(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    response = client.post(
        "/api/v1/investigation-confirmations",
        content="{",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert _error_code(response) == "invalid_request"


def test_confirmation_api_openapi_exposes_only_designed_paths(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    schema_text = client.get("/openapi.json").text

    assert '"/api/v1/investigation-confirmations"' in schema_text
    assert '"/api/v1/investigation-confirmations/{investigation_id}"' in schema_text


def test_confirmation_api_reconfirms_schema_two_without_mutating_original(tmp_path: Path) -> None:
    context, legacy_id, before = write_schema_two_package(tmp_path)
    client = TestClient(
        create_reference_frame_app(
            UnusedReferenceFrameService(),
            UnusedReferenceFrameResources(),
            confirmation_service=context.service,
        )
    )

    response = client.post(
        f"/api/v1/investigation-confirmations/{legacy_id}/reconfirm-for-recording-search",
        json={},
    )

    assert response.status_code == 201
    payload = _confirmation_payload(response)
    assert payload.schema_version == 3
    assert payload.investigation_id.startswith("object-disappearance-v3-")
    assert (context.investigation_root / legacy_id / "manifest.json").read_bytes() == before


def test_confirmation_api_reconfirm_rejects_client_owned_facts(tmp_path: Path) -> None:
    context, legacy_id, _ = write_schema_two_package(tmp_path)
    client = TestClient(
        create_reference_frame_app(
            UnusedReferenceFrameService(),
            UnusedReferenceFrameResources(),
            confirmation_service=context.service,
        )
    )

    response = client.post(
        f"/api/v1/investigation-confirmations/{legacy_id}/reconfirm-for-recording-search",
        json={"source_width": 1},
    )

    assert response.status_code == 422
    assert _error_code(response) == "invalid_confirmation"
