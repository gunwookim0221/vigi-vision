"""Focused production-boundary coverage for Phase 7E public execution."""

# Test doubles intentionally implement only the transport methods under test.
# pyright: reportAny=false, reportArgumentType=false, reportCallIssue=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnusedCallResult=false

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from vigi_vision.recording_search_7e_1d import Phase7EStatus
from vigi_vision.recording_search_7e_public import (
    Phase7EPublicError,
    Phase7EPublicRequest,
    Phase7EPublicStatus,
    Phase8HandoffRepository,
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


def test_phase8_handoff_persists_strict_pair_and_reuses_it(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    media = media_root / "inv-01" / "run-01"
    media.mkdir(parents=True)
    common_id = "rr-common-session-v1-" + "0" * 64
    clip = media / f"{common_id}.mp4"
    clip.write_bytes(b"session")
    repository = Phase8HandoffRepository(tmp_path / "phase8", media_root=media_root)
    run = SimpleNamespace(
        schema_version=7,
        result_kind="FOUND",
        investigation_id="inv-01",
        run_id="run-01",
    )
    arguments = {
        "terminal_result_id": "rr-terminal-result-v1-" + "1" * 64,
        "common_session_id": common_id,
        "selected_observation_ids": [],
        "selected_support_group_ids": [],
        "stream_index": 0,
        "width": 32,
        "height": 32,
        "duration_ticks": 1,
        "time_base_num": 1,
        "time_base_den": 1,
        "frame_rate_num": 1,
        "frame_rate_den": 1,
        "level": 41,
        "codec": "h264",
        "profile": "High",
        "pixel_format": "yuv420p",
        "audio_stream_count": 0,
        "interval_start": "2026-07-20T03:00:00Z",
        "interval_end": "2026-07-20T03:00:01Z",
    }
    first = repository.create_or_reuse(run, clip, **arguments)
    before = {
        path.name: path.read_bytes()
        for path in (tmp_path / "phase8" / "inv-01" / "run-01").iterdir()
    }
    second = repository.create_or_reuse(run, clip, **arguments)
    assert first.identity == second.identity
    assert repository.status("inv-01", "run-01") == ("READY", None)
    assert before == {
        path.name: path.read_bytes()
        for path in (tmp_path / "phase8" / "inv-01" / "run-01").iterdir()
    }
    for name in ("source-clip.json", "phase8-request.json", "manifest.json"):
        document = json.loads((tmp_path / "phase8" / "inv-01" / "run-01" / name).read_text())
        assert set(document) == {"family", "identity", "payload"}


def test_phase8_status_is_read_only_for_missing_run(tmp_path: Path) -> None:
    repository = Phase8HandoffRepository(tmp_path / "phase8")
    assert repository.status("inv-01", "run-01") == (None, None)
    assert not (tmp_path / "phase8").exists()


def test_phase8_handoff_rejects_media_outside_configured_root(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign.mp4"
    foreign.write_bytes(b"session")
    repository = Phase8HandoffRepository(tmp_path / "phase8", media_root=tmp_path / "media")
    run = SimpleNamespace(
        schema_version=7,
        result_kind="FOUND",
        investigation_id="inv-01",
        run_id="run-01",
    )
    with pytest.raises(Phase7EPublicError) as error:
        repository.create_or_reuse(
            run,
            foreign,
            terminal_result_id="rr-terminal-result-v1-" + "1" * 64,
            common_session_id="rr-common-session-v1-" + "0" * 64,
            selected_observation_ids=[],
            selected_support_group_ids=[],
            stream_index=0,
            width=32,
            height=32,
            duration_ticks=1,
            time_base_num=1,
            time_base_den=1,
            frame_rate_num=1,
            frame_rate_den=1,
            level=41,
            codec="h264",
            profile="High",
            pixel_format="yuv420p",
            audio_stream_count=0,
            interval_start="2026-07-20T03:00:00Z",
            interval_end="2026-07-20T03:00:01Z",
        )
    assert getattr(error.value, "code", None) == "phase8_media_unavailable"


def test_phase7e_post_is_cli_only_and_validation_is_safe() -> None:
    app = create_reference_frame_app(
        _UnusedReferenceFrameService(),
        _UnusedResources(),
        phase7e_service=_UnavailablePhase7EService(),
    )
    body = {
        "investigation_id": "object-disappearance-ch1-20260720T120000Z",
        "search_end_time_text": "2026-07-20 12:00:00",
        "source_timezone": "Asia/Seoul",
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
    assert valid.status_code == 503
    assert valid.json()["error"]["code"] == "recording_search_execution_requires_cli"
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "invalid_recording_search_request"
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "invalid_request"
    assert status_response.status_code == 404
    assert "phase8" not in status_response.text
