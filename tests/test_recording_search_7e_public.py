"""Focused production-boundary coverage for Phase 7E public execution."""

# Test doubles intentionally implement only the transport methods under test.
# pyright: reportAny=false, reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnusedCallResult=false

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    approved_successor_object_presence_policy,
    build_phase7e_service,
)
from vigi_vision.recording_search_successor import SuccessorPlanRequest
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

    def evidence(self, investigation_id: str, run_id: str) -> dict[str, object] | None:
        _ = investigation_id, run_id
        return None

    def evidence_frame(self, investigation_id: str, run_id: str, digest: str) -> bytes | None:
        _ = investigation_id, run_id, digest
        return None


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


def test_successor_policy_uses_versioned_baseline_support_mode() -> None:
    policy = approved_successor_object_presence_policy()
    assert policy.baseline_support_mode is True
    assert policy.classifier_policy_version == "efficient-sam-ti-baseline-support-v2"
    assert policy.classifier_preprocessing_version == "phase7e-baseline-support-v2"


def test_public_composition_keeps_legacy_object_policy_for_schema5_to7(tmp_path: Path) -> None:
    service = build_phase7e_service(
        root=tmp_path,
        confirmation_service=SimpleNamespace(),
        recording_planner=SimpleNamespace(),
        replay_extractor=SimpleNamespace(),
        ffmpeg=Path("ffmpeg"),
        ffprobe=Path("ffprobe"),
        mask_predictor=None,
    )
    assert service.object_policy.baseline_support_mode is False


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


def test_visual_evidence_routes_are_additive_and_identity_scoped() -> None:
    class _EvidencePhase7EService(_UnavailablePhase7EService):
        def evidence(self, investigation_id: str, run_id: str) -> dict[str, object] | None:
            assert investigation_id == "object-disappearance-v3-ch1-20260720T033428Z"
            assert run_id == "search-run-" + "a" * 32
            return {
                "version": "phase7e-successor-evidence-v1",
                "run_id": run_id,
            }

        def evidence_frame(self, investigation_id: str, run_id: str, digest: str) -> bytes | None:
            assert investigation_id == "object-disappearance-v3-ch1-20260720T033428Z"
            assert run_id == "search-run-" + "a" * 32
            return b"jpeg-bytes" if digest == "b" * 64 else None

    app = create_reference_frame_app(
        _UnusedReferenceFrameService(),
        _UnusedResources(),
        phase7e_service=_EvidencePhase7EService(),
    )
    prefix = (
        "/api/v1/recording-searches/object-disappearance-v3-ch1-20260720T033428Z/search-run-"
        + "a" * 32
    )
    with TestClient(app) as client:
        manifest = client.get(prefix + "/evidence")
        frame = client.get(prefix + "/evidence/" + "b" * 64)
        missing = client.get(prefix + "/evidence/" + "c" * 64)
    assert manifest.status_code == 200
    assert manifest.json()["version"] == "phase7e-successor-evidence-v1"
    assert frame.status_code == 200
    assert frame.headers["content-type"] == "image/jpeg"
    assert frame.content == b"jpeg-bytes"
    assert missing.status_code == 404


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


def test_future_selected_baseline_is_the_public_effective_search_start() -> None:
    anchor = datetime(2026, 7, 20, 3, 34, 28, tzinfo=timezone.utc)
    baseline = anchor + timedelta(seconds=60)
    confirmed = SimpleNamespace(
        investigation_id="object-disappearance-v3-ch1-20260720T033428Z",
        channel_id=1,
        anchor_time_utc=anchor,
        requested_time_utc=baseline,
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
        object(),
        object(),
        policy,
        classifier_policy,
        object_policy,
        SimpleNamespace(),
        lambda: datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc),
    )

    prepared = service.prepare_http(
        confirmed.investigation_id,
        "2026-07-20T12:44:28",
        "12345678-1234-4234-8234-123456789abc",
    )

    assert prepared.request.start_utc == baseline
    assert prepared.request.duration_seconds == 540


def test_future_selected_baseline_reaches_successor_admission_with_same_start() -> None:
    anchor = datetime(2026, 7, 20, 3, 34, 28, tzinfo=timezone.utc)
    baseline = anchor + timedelta(seconds=60)
    confirmed = SimpleNamespace(
        investigation_id="object-disappearance-v3-ch1-20260720T033428Z",
        channel_id=1,
        anchor_time_utc=anchor,
        requested_time_utc=baseline,
        source_timezone="Asia/Seoul",
    )

    class _Confirmation:
        def load_confirmed(self, investigation_id: str) -> object:
            assert investigation_id == confirmed.investigation_id
            return confirmed

    class _Successor:
        def prepare(
            self,
            loaded: object,
            *,
            search_end_time_text: str,
            run_id: str,
            now_utc: datetime,
        ) -> object:
            effective = max(loaded.anchor_time_utc, loaded.requested_time_utc)
            plan = SuccessorPlanRequest.from_text(
                channel_id=loaded.channel_id,
                anchor_time_utc=effective,
                search_end_time_text=search_end_time_text,
                source_timezone=loaded.source_timezone,
                now_utc=now_utc,
            )
            return SimpleNamespace(
                request=SimpleNamespace(
                    investigation_id=loaded.investigation_id,
                    run_id=run_id,
                    channel_id=loaded.channel_id,
                    anchor_time_utc=effective,
                    end_utc=plan.search_end_utc,
                    source_timezone=loaded.source_timezone,
                ),
                plan=plan,
            )

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
        lambda: datetime(2026, 7, 20, 5, 0, tzinfo=timezone.utc),
        successor_execution=_Successor(),
    )

    prepared = service.prepare_http(
        confirmed.investigation_id,
        "2026-07-20T13:04:28",
        "12345678-1234-4234-8234-123456789abc",
    )

    assert prepared.successor is not None
    assert prepared.successor.plan.anchor_time_utc == baseline


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
