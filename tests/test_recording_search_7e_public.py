"""Focused production-boundary coverage for Phase 7E public execution."""

# Test doubles intentionally implement only the transport methods under test.
# pyright: reportAny=false, reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnusedCallResult=false

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from vigi_vision.recording_search_7e_1d import Phase7EStatus
from vigi_vision.recording_search_7e_public import (
    Phase7EPublicRequest,
    Phase7EPublicStatus,
    approved_phase7e_policy,
)
from vigi_vision.reference_frame_api import create_reference_frame_app


class _UnusedReferenceFrameService:
    def execute_or_resolve(self, request: object) -> object:
        raise AssertionError(request)


class _UnusedResources:
    def resolve_image(self, resource_id: str) -> object:
        raise AssertionError(resource_id)


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
        "rr-policy-v1-3e976cea4523523b81762351983a19d50a3febe036413b165831f74edaa6904d"
    )
    assert classifier.identity == (
        "rr-classifier-policy-v1-6cf4b00da268a53dc7efde13a4fd563800fd5ee7210653a6362b0bb644afda7f"
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
