"""Focused production-boundary coverage for Phase 7E public execution."""

# Test doubles intentionally implement only the transport methods under test.
# pyright: reportAny=false, reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnusedCallResult=false

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from vigi_vision.object_presence_values import BinaryMask
from vigi_vision.recording_search_7e_1d import Phase7EStatus
from vigi_vision.recording_search_7e_b4_process import StaticMaskWorkerSpec
from vigi_vision.recording_search_7e_public import (
    Phase7EPublicError,
    Phase7EPublicRequest,
    Phase7EPublicService,
    Phase7EPublicStatus,
    approved_phase7e_policy,
    build_phase7e_service,
)
from vigi_vision.reference_frame_api import create_reference_frame_app


class _UnusedReferenceFrameService:
    def execute_or_resolve(self, request: object) -> object:
        raise AssertionError(request)


class _UnusedResources:
    def resolve_image(self, resource_id: str) -> object:
        raise AssertionError(resource_id)


def _unused_confirmation_loader(_value: str) -> None:
    return None


class _UnavailablePhase7EService:
    def recover_abandoned(self) -> int:
        return 0

    def prepare_http(self, investigation_id: str, search_end: str, request_id: str) -> object:
        _ = search_end
        return SimpleNamespace(
            request=SimpleNamespace(
                investigation_id=investigation_id,
                run_id=f"search-run-{request_id.replace('-', '')}",
            )
        )

    def resolve_existing(self, prepared: object) -> None:
        _ = prepared

    def execute_prepared(self, prepared: object, **kwargs: object) -> Phase7EPublicStatus:
        _ = kwargs
        request = prepared.request  # type: ignore[attr-defined]
        return self.status(request.investigation_id, request.run_id)

    def status(self, investigation_id: str, run_id: str) -> Phase7EPublicStatus:
        return Phase7EPublicStatus(
            Phase7EStatus(investigation_id, run_id, 0, "UNAVAILABLE", None, None)
        )


def test_public_request_is_closed_and_strict() -> None:
    request = Phase7EPublicRequest(
        investigation_id="inv-01",
        search_end_time_text="2026-07-20 12:00:00",
        source_timezone="Asia/Seoul",
    )
    assert request.investigation_id == "inv-01"
    with pytest.raises(ValidationError):
        _ = Phase7EPublicRequest(
            investigation_id="inv-01",
            search_end_time_text="2026-07-20 12:00:00",
            source_timezone="Asia/Seoul",
            channel_id=1,  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        _ = Phase7EPublicRequest(
            investigation_id=1,  # type: ignore[arg-type]
            search_end_time_text="2026-07-20 12:00:00",
            source_timezone="Asia/Seoul",
        )


def test_policy_snapshots_reproduce_approved_identities() -> None:
    policy, classifier, _ = approved_phase7e_policy()
    assert policy.identity == (
        "rr-policy-v1-85724f1281baed3d092528763890d640dcf6dd303ce8398d1194ffaac6b7560a"
    )
    assert classifier.identity == (
        "rr-classifier-policy-v1-d20a9d64543ce5bfa2b0d4861952593d36342ad51489588de06c89efa258838f"
    )


def test_phase7e_post_accepts_the_closed_browser_contract_and_validation_is_safe() -> None:
    app = create_reference_frame_app(
        _UnusedReferenceFrameService(),
        _UnusedResources(),
        phase7e_service=_UnavailablePhase7EService(),
    )
    body = {
        "investigation_id": "object-disappearance-ch1-20260720T120000Z",
        "search_end": "2026-07-20T12:05:00",
        "request_id": "12345678-1234-4234-8234-123456789abc",
    }
    with TestClient(app) as client:
        valid = client.post("/api/v1/recording-searches", json=body)
        unknown = client.post("/api/v1/recording-searches", json={**body, "channel_id": 1})
        malformed = client.post(
            "/api/v1/recording-searches",
            content="{",
            headers={"content-type": "application/json"},
        )
        status_response = client.get(
            "/api/v1/recording-searches/object-disappearance-ch1-20260720T120000Z/search-run-missing"
        )
    assert valid.status_code == 202
    assert valid.json() == {
        "request_id": body["request_id"],
        "investigation_id": body["investigation_id"],
        "run_id": "search-run-12345678123442348234123456789abc",
        "status": "ACCEPTED",
        "status_url": (
            "/api/v1/recording-searches/"
            "object-disappearance-ch1-20260720T120000Z/"
            "search-run-12345678123442348234123456789abc"
        ),
    }
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "invalid_recording_search_request"
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "invalid_request"
    assert status_response.status_code == 404
    assert "phase8" not in status_response.text


def test_new_ten_minute_request_does_not_fall_back_to_legacy_executor() -> None:
    confirmed = SimpleNamespace(
        channel_id=1,
        anchor_time_utc=datetime(2026, 7, 20, 3, 34, 28, tzinfo=timezone.utc),
        source_timezone="Asia/Seoul",
    )

    class _Confirmation:
        def load_confirmed(self, investigation_id: str) -> object:
            assert investigation_id == "object-disappearance-v3-ch1-20260720T033428Z"
            return confirmed

    policy, classifier_policy, object_policy = approved_phase7e_policy()
    service = Phase7EPublicService(
        SimpleNamespace(),
        SimpleNamespace(),
        _Confirmation(),
        None,
        None,
        policy,
        classifier_policy,
        object_policy,
        SimpleNamespace(),
        lambda: datetime(2026, 7, 20, 4, 0, 0, 123456, tzinfo=timezone.utc),
    )

    with pytest.raises(Phase7EPublicError) as raised:
        service.prepare_http(
            "object-disappearance-v3-ch1-20260720T033428Z",
            "2026-07-20T12:44:28",
            "12345678-1234-4234-8234-123456789abc",
        )
    assert raised.value.code == "successor_unavailable"


def test_successor_unavailable_is_a_safe_http_503_not_input_422() -> None:
    confirmed = SimpleNamespace(
        channel_id=1,
        anchor_time_utc=datetime(2026, 7, 20, 3, 34, 28, tzinfo=timezone.utc),
        source_timezone="Asia/Seoul",
    )

    class _Confirmation:
        def load_confirmed(self, investigation_id: str) -> object:
            _ = investigation_id
            return confirmed

    policy, classifier_policy, object_policy = approved_phase7e_policy()
    service = Phase7EPublicService(
        SimpleNamespace(),
        SimpleNamespace(),
        _Confirmation(),
        None,
        None,
        policy,
        classifier_policy,
        object_policy,
        SimpleNamespace(),
        lambda: datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc),
    )
    app = create_reference_frame_app(
        _UnusedReferenceFrameService(),
        _UnusedResources(),
        phase7e_service=service,
    )
    client = TestClient(app)
    response = client.post(
        "/api/v1/recording-searches",
        json={
            "investigation_id": "object-disappearance-v3-ch1-20260720T033428Z",
            "search_end": "2026-07-20T12:44:28",
            "request_id": "12345678-1234-4234-8234-123456789abc",
        },
    )
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "successor_unavailable",
        "message": "The long-range recording search is unavailable.",
        "details": None,
    }
    too_long = client.post(
        "/api/v1/recording-searches",
        json={
            "investigation_id": "object-disappearance-v3-ch1-20260720T033428Z",
            "search_end": "2026-07-20T14:34:29",
            "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        },
    )
    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "invalid_recording_search_request"
    client.close()


def test_production_composition_preserves_successor_readiness_state(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    mask = BinaryMask.from_rows(((True,),))
    configured = build_phase7e_service(
        root=tmp_path / "configured",
        confirmation_service=SimpleNamespace(load_confirmed=_unused_confirmation_loader),
        recording_planner=SimpleNamespace(),
        replay_extractor=SimpleNamespace(),
        ffmpeg=tmp_path / "ffmpeg",
        ffprobe=tmp_path / "ffprobe",
        mask_predictor=StaticMaskWorkerSpec(mask, mask),
    )
    assert configured.successor_execution is not None
    assert configured.successor_readiness == "configured"

    with caplog.at_level("WARNING", logger="uvicorn.error.vigi_vision.phase7e"):
        unavailable = build_phase7e_service(
            root=tmp_path / "unavailable",
            confirmation_service=SimpleNamespace(load_confirmed=_unused_confirmation_loader),
            recording_planner=SimpleNamespace(),
            replay_extractor=SimpleNamespace(),
            ffmpeg=tmp_path / "ffmpeg",
            ffprobe=tmp_path / "ffprobe",
            mask_predictor=None,
        )
    assert unavailable.successor_execution is None
    assert unavailable.successor_readiness == "unavailable"
    assert [record.message for record in caplog.records].count(
        '{"event":"phase7e.successor_unavailable","readiness":"unavailable","stage":"classifier_wiring"}'
    ) == 1
