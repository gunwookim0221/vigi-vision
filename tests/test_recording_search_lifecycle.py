from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import repeat
from pathlib import Path
from typing import NoReturn, cast

import pytest
from fastapi.testclient import TestClient
from tests.test_investigation_confirmation import build_context, build_request
from tests.test_investigation_confirmation_phase6c import write_schema_two_package
from typing_extensions import override

from vigi_vision.channel_selection import Channel
from vigi_vision.recording_search_models import (
    ReconfirmationRequiredError,
    RecordingSearchArtifactError,
    RecordingSearchBaselineError,
    RecordingSearchManifest,
    RecordingSearchManifestCorruptError,
    RecordingSearchRequest,
    RecordingSearchState,
    RecordingSearchTransitionError,
)
from vigi_vision.recording_search_repository import RecordingSearchRepository
from vigi_vision.recording_search_service import RecordingSearchService, RecordingSearchStartResult
from vigi_vision.reference_frame_api import create_reference_frame_app
from vigi_vision.reference_frame_models import ReferenceFrameRequest

_NOW = datetime(2026, 8, 2, 4, 5, 6, tzinfo=timezone.utc)
_END = "2026-07-20T13:00:00+09:00"


@dataclass(frozen=True, slots=True)
class _Inventory:
    values: tuple[Channel, ...] = (Channel(1, "Counter", "Counter", online=True),)

    def channels(self) -> tuple[Channel, ...]:
        return self.values


@dataclass(frozen=True, slots=True)
class _UnusedReferenceFrameService:
    def execute_or_resolve(self, request: ReferenceFrameRequest) -> NoReturn:
        _ = request
        raise AssertionError


@dataclass(frozen=True, slots=True)
class _UnusedResources:
    def resolve_image(self, resource_id: str) -> NoReturn:
        _ = resource_id
        raise AssertionError


class _FailingRepository(RecordingSearchRepository):
    @override
    def create(self, manifest: RecordingSearchManifest) -> NoReturn:
        _ = manifest
        raise RecordingSearchArtifactError


def _build_service(
    tmp_path: Path, *, inventory: _Inventory | None = None
) -> RecordingSearchService:
    context = build_context(tmp_path)
    _ = context.service.confirm(build_request(context.resource_id))
    repository = RecordingSearchRepository(tmp_path / "investigation-searches")
    return RecordingSearchService(
        confirmation_service=context.service,
        repository=repository,
        channel_inventory=_Inventory() if inventory is None else inventory,
        artifact_root=tmp_path,
        now_utc=lambda: _NOW,
    )


def _request(investigation_id: str) -> RecordingSearchRequest:
    return RecordingSearchRequest(
        investigation_id=investigation_id,
        search_end_time_text=_END,
        source_timezone="Asia/Seoul",
    )


def _start_concurrent(
    service: RecordingSearchService, investigation_id: str
) -> RecordingSearchStartResult:
    return service.start(_request(investigation_id))


def test_valid_schema_three_baseline_creates_running_run_without_search_work(
    tmp_path: Path,
) -> None:
    service = _build_service(tmp_path)
    context = build_context(tmp_path / "second")
    _ = context.service.confirm(build_request(context.resource_id))
    service = RecordingSearchService(
        confirmation_service=context.service,
        repository=RecordingSearchRepository(tmp_path / "second-searches"),
        channel_inventory=_Inventory(),
        artifact_root=tmp_path / "second",
        now_utc=lambda: _NOW,
    )

    result = service.start(_request(context.investigation_id))

    assert result.outcome.value == "started"
    assert isinstance(result.manifest, RecordingSearchManifest)
    assert result.manifest.state is RecordingSearchState.RUNNING
    assert result.manifest.canonical_observation_ids == ()
    assert result.manifest.target_alias_ids == ()
    assert result.baseline_bytes
    assert result.run_handle is not None
    assert result.manifest.search_run_id.startswith("search-run-")
    result.run_handle.release()


def test_schema_two_requires_explicit_reconfirmation_and_publishes_no_run(tmp_path: Path) -> None:
    context, legacy_id, _ = write_schema_two_package(tmp_path)
    repository = RecordingSearchRepository(tmp_path / "searches")
    service = RecordingSearchService(
        confirmation_service=context.service,
        repository=repository,
        channel_inventory=_Inventory(),
        artifact_root=tmp_path,
        now_utc=lambda: _NOW,
    )

    with pytest.raises(ReconfirmationRequiredError):
        _ = service.start(_request(legacy_id))
    assert not repository.root.exists()


@pytest.mark.parametrize("mutation", ["digest", "size", "jpeg", "missing", "dimensions", "roi"])
def test_invalid_baseline_fails_before_run_publication(tmp_path: Path, mutation: str) -> None:
    context = build_context(tmp_path)
    created = context.service.confirm(build_request(context.resource_id))
    frame = context.resource_root / context.resource_id / "frame.jpg"
    if mutation == "digest":
        _ = frame.write_bytes(frame.read_bytes() + b"changed")
    elif mutation == "size":
        _ = frame.write_bytes(frame.read_bytes()[:-1])
    elif mutation == "jpeg":
        _ = frame.write_bytes(b"not-a-jpeg")
    elif mutation == "missing":
        frame.unlink()
    elif mutation == "dimensions":
        manifest_path = created.artifact_directory / "manifest.json"
        payload = cast("dict[str, object]", json.loads(manifest_path.read_text(encoding="utf-8")))
        confirmation = cast("dict[str, object]", payload["confirmation"])
        confirmation["source_width"] = 1279
        _ = manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        manifest_path = created.artifact_directory / "manifest.json"
        payload = cast("dict[str, object]", json.loads(manifest_path.read_text(encoding="utf-8")))
        confirmation = cast("dict[str, object]", payload["confirmation"])
        roi = cast("dict[str, object]", confirmation["roi"])
        roi["x"] = 1279
        _ = manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    service = RecordingSearchService(
        confirmation_service=context.service,
        repository=RecordingSearchRepository(tmp_path / "searches"),
        channel_inventory=_Inventory(),
        artifact_root=tmp_path,
        now_utc=lambda: _NOW,
    )

    with pytest.raises(RecordingSearchBaselineError):
        _ = service.start(_request(context.investigation_id))
    assert not (tmp_path / "searches").exists()


def test_unknown_channel_fails_safely(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    _ = context.service.confirm(build_request(context.resource_id))
    service = RecordingSearchService(
        confirmation_service=context.service,
        repository=RecordingSearchRepository(tmp_path / "searches"),
        channel_inventory=_Inventory((Channel(2, "Other", "Other", online=True),)),
        artifact_root=tmp_path,
        now_utc=lambda: _NOW,
    )

    with pytest.raises(RecordingSearchBaselineError):
        _ = service.start(_request(context.investigation_id))
    assert not (tmp_path / "searches").exists()


def test_offline_channel_fails_before_run_publication(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    _ = context.service.confirm(build_request(context.resource_id))
    service = RecordingSearchService(
        confirmation_service=context.service,
        repository=RecordingSearchRepository(tmp_path / "searches"),
        channel_inventory=_Inventory((Channel(1, "Counter", "Counter", online=False),)),
        artifact_root=tmp_path,
        now_utc=lambda: _NOW,
    )

    with pytest.raises(RecordingSearchBaselineError):
        _ = service.start(_request(context.investigation_id))
    assert not (tmp_path / "searches").exists()


def test_duplicate_start_is_deterministic_and_terminal_run_can_restart(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    context = build_context(tmp_path / "restart")
    _ = context.service.confirm(build_request(context.resource_id))
    service = RecordingSearchService(
        confirmation_service=context.service,
        repository=RecordingSearchRepository(tmp_path / "restart-searches"),
        channel_inventory=_Inventory(),
        artifact_root=tmp_path / "restart",
        now_utc=lambda: _NOW,
    )
    request = _request(context.investigation_id)

    first = service.start(request)
    duplicate = service.start(request)
    assert duplicate.outcome.value == "already_running"
    assert duplicate.manifest.search_run_id == first.manifest.search_run_id

    assert first.run_handle is not None
    _ = first.run_handle.mark_terminal(RecordingSearchState.FAILED, "baseline_validation_failed")
    restarted = service.start(request)
    assert restarted.manifest.search_run_id != first.manifest.search_run_id
    assert restarted.run_handle is not None
    restarted.run_handle.release()


def test_concurrent_services_have_one_start_winner(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    _ = context.service.confirm(build_request(context.resource_id))
    repository_root = tmp_path / "searches"
    services = tuple(
        RecordingSearchService(
            confirmation_service=context.service,
            repository=RecordingSearchRepository(repository_root),
            channel_inventory=_Inventory(),
            artifact_root=tmp_path,
            now_utc=lambda: _NOW,
            lock_timeout_seconds=0.2,
        )
        for _ in range(2)
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(_start_concurrent, services, repeat(context.investigation_id)))

    assert sorted(result.outcome.value for result in results) == ["already_running", "started"]
    winner = next(result for result in results if result.run_handle is not None)
    assert winner.run_handle is not None
    winner.run_handle.release()
    for service in services:
        service.close()


def test_failed_publication_releases_lock_for_next_start(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    _ = context.service.confirm(build_request(context.resource_id))
    request = _request(context.investigation_id)
    failing = RecordingSearchService(
        confirmation_service=context.service,
        repository=_FailingRepository(tmp_path / "searches"),
        channel_inventory=_Inventory(),
        artifact_root=tmp_path,
        now_utc=lambda: _NOW,
    )

    with pytest.raises(RecordingSearchArtifactError):
        _ = failing.start(request)
    failing.close()

    retry = RecordingSearchService(
        confirmation_service=context.service,
        repository=RecordingSearchRepository(tmp_path / "searches"),
        channel_inventory=_Inventory(),
        artifact_root=tmp_path,
        now_utc=lambda: _NOW,
    )
    result = retry.start(request)
    assert result.outcome.value == "started"
    assert result.run_handle is not None
    result.run_handle.release()


def test_reopened_running_run_reconciles_to_interrupted_and_is_idempotent(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    _ = context.service.confirm(build_request(context.resource_id))
    repository = RecordingSearchRepository(tmp_path / "searches")
    service = RecordingSearchService(
        confirmation_service=context.service,
        repository=repository,
        channel_inventory=_Inventory(),
        artifact_root=tmp_path,
        now_utc=lambda: _NOW,
    )
    started = service.start(_request(context.investigation_id))
    assert started.run_handle is not None
    started.run_handle.release()

    reopened = RecordingSearchService(
        confirmation_service=context.service,
        repository=repository,
        channel_inventory=_Inventory(),
        artifact_root=tmp_path,
        now_utc=lambda: _NOW,
    )
    status = reopened.status(context.investigation_id, started.manifest.search_run_id)
    again = reopened.status(context.investigation_id, started.manifest.search_run_id)

    assert status.state is RecordingSearchState.INTERRUPTED
    assert again == status
    assert status.failure_reason == "process_lock_released"


def test_repository_rejects_terminal_state_transition_and_malformed_manifest(
    tmp_path: Path,
) -> None:
    service = _build_service(tmp_path)
    context = build_context(tmp_path / "repository")
    _ = context.service.confirm(build_request(context.resource_id))
    repository = RecordingSearchRepository(tmp_path / "repository-searches")
    service = RecordingSearchService(
        confirmation_service=context.service,
        repository=repository,
        channel_inventory=_Inventory(),
        artifact_root=tmp_path / "repository",
        now_utc=lambda: _NOW,
    )
    started = service.start(_request(context.investigation_id))
    assert started.run_handle is not None
    terminal = started.run_handle.mark_terminal(RecordingSearchState.FAILED, "unexpected_error")
    with pytest.raises(RecordingSearchTransitionError):
        _ = repository.transition(
            context.investigation_id,
            terminal.search_run_id,
            RecordingSearchState.RUNNING,
        )
    manifest_path = (
        repository.run_path(context.investigation_id, terminal.search_run_id) / "manifest.json"
    )
    _ = manifest_path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(RecordingSearchManifestCorruptError):
        _ = repository.load(context.investigation_id, terminal.search_run_id)


def test_recording_search_api_start_status_and_duplicate_are_safe(tmp_path: Path) -> None:
    context = build_context(tmp_path)
    _ = context.service.confirm(build_request(context.resource_id))
    repository = RecordingSearchRepository(tmp_path / "searches")
    service = RecordingSearchService(
        confirmation_service=context.service,
        repository=repository,
        channel_inventory=_Inventory(),
        artifact_root=tmp_path,
        now_utc=lambda: _NOW,
    )
    app = create_reference_frame_app(
        _UnusedReferenceFrameService(),
        _UnusedResources(),
        recording_search_service=service,
    )
    client = TestClient(app)
    body = {
        "investigation_id": context.investigation_id,
        "search_end_time_text": _END,
        "source_timezone": "Asia/Seoul",
    }

    first = client.post("/api/v1/recording-searches", json=body)
    duplicate = client.post("/api/v1/recording-searches", json=body)
    run_id = cast("str", first.json()["search_run_id"])
    status = client.get(f"/api/v1/recording-searches/{context.investigation_id}/{run_id}")

    assert first.status_code == 201
    assert first.json()["state"] == "RUNNING"
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "already_running"
    assert status.status_code == 200
    assert status.json()["search_run_id"] == run_id
    assert str(tmp_path) not in status.text

    unknown = client.post("/api/v1/recording-searches", json={**body, "extra": "nope"})
    malformed = client.post(
        "/api/v1/recording-searches",
        content="{",
        headers={"content-type": "application/json"},
    )
    invalid_identity = client.post(
        "/api/v1/recording-searches",
        json={**body, "investigation_id": "not-an-investigation"},
    )

    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "invalid_request"
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "invalid_request"
    assert invalid_identity.status_code == 422
    assert invalid_identity.json()["error"]["code"] == "invalid_request"
    assert "not-an-investigation" not in invalid_identity.text
    missing = client.get(
        f"/api/v1/recording-searches/{context.investigation_id}/search-run-deadbeef"
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "search_run_not_found"
    service.close()
    interrupted = client.get(f"/api/v1/recording-searches/{context.investigation_id}/{run_id}")
    assert interrupted.status_code == 200
    assert interrupted.json()["state"] == "INTERRUPTED"

    manifest_path = repository.run_path(context.investigation_id, run_id) / "manifest.json"
    _ = manifest_path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    corrupt = client.get(f"/api/v1/recording-searches/{context.investigation_id}/{run_id}")
    assert corrupt.status_code == 500
    assert corrupt.json()["error"]["code"] == "search_manifest_corrupt"
    assert str(tmp_path) not in corrupt.text


def test_recording_search_api_reconfirmation_and_interruption_errors_are_safe(
    tmp_path: Path,
) -> None:
    context, legacy_id, _ = write_schema_two_package(tmp_path)
    service = RecordingSearchService(
        confirmation_service=context.service,
        repository=RecordingSearchRepository(tmp_path / "searches"),
        channel_inventory=_Inventory(),
        artifact_root=tmp_path,
        now_utc=lambda: _NOW,
    )
    app = create_reference_frame_app(
        _UnusedReferenceFrameService(),
        _UnusedResources(),
        recording_search_service=service,
    )
    client = TestClient(app)
    body = {
        "investigation_id": legacy_id,
        "search_end_time_text": _END,
        "source_timezone": "Asia/Seoul",
    }

    reconfirmation = client.post("/api/v1/recording-searches", json=body)

    assert reconfirmation.status_code == 409
    assert reconfirmation.json()["error"]["code"] == "reconfirmation_required"
    assert str(tmp_path) not in reconfirmation.text
    service.close()
